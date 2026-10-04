from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .downloader import md5_file
from .manifest import atomic_json
from .config import source_config
from .network import open_with_retry

Progress = Callable[[str], None]
PLATFORM = "Windows"
RESOURCE_INDEX_KEY = b"Assets/Beyond/DynamicAssets/Gameplay/UI/Fonts/"


def major_minor(version: str) -> str:
    parts = version.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else version


def resource_token(payload: dict[str, Any]) -> str:
    file_path = str(payload.get("pkg", {}).get("file_path", ""))
    before_tail, separator, _ = file_path.rpartition("/")
    if not separator:
        return ""
    _, separator, token = before_tail.rpartition("_")
    return token if separator else ""


def fetch_resources(payload: dict[str, Any], timeout: int = 30) -> dict[str, Any]:
    config = source_config()
    resource_url = config["launcherUrl"].rsplit("/", 1)[0] + "/get_latest_resources"
    version = str(payload["version"])
    token = resource_token(payload)
    if not token:
        raise ValueError("launcher manifest does not contain a resource token")
    query = urllib.parse.urlencode(
        {
            "appcode": config["appCode"],
            "game_version": major_minor(version),
            "version": version,
            "platform": PLATFORM,
            "rand_str": token,
        }
    )
    request = urllib.request.Request(
        f"{resource_url}?{query}",
        headers={"User-Agent": "endfield-resource-service/0.2"},
    )
    with open_with_retry(request, timeout=timeout) as response:
        result = json.load(response)
    resources = result.get("resources")
    if not isinstance(resources, list) or not resources:
        raise ValueError("resource response does not contain resources")
    for resource in resources:
        if not all(resource.get(key) for key in ("name", "version", "path")):
            raise ValueError("invalid resource entry")
    return result


def fetch_patch_manifest(resource: dict[str, Any], timeout: int = 60) -> dict[str, Any]:
    url = f"{str(resource['path']).rstrip('/')}/patch.json"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "endfield-resource-service/0.2"},
    )
    with open_with_retry(request, timeout=timeout) as response:
        result = json.load(response)
    if not isinstance(result.get("files"), list):
        raise ValueError("resource patch manifest has no files")
    return result


def fetch_resource_index(resource: dict[str, Any], timeout: int = 60) -> dict[str, Any]:
    name = str(resource["name"])
    url = f"{str(resource['path']).rstrip('/')}/index_{name}.json"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "endfield-resource-service/0.3"},
    )
    with open_with_retry(request, timeout=timeout) as response:
        encrypted = base64.b64decode(response.read(), validate=True)
    decrypted = bytes(
        (value - RESOURCE_INDEX_KEY[index % len(RESOURCE_INDEX_KEY)]) % 256
        for index, value in enumerate(encrypted)
    )
    result = json.loads(decrypted)
    if not isinstance(result.get("files"), list):
        raise ValueError("resource index has no files")
    return result


def safe_relative(value: str) -> Path:
    relative = PurePosixPath(value)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError(f"unsafe resource path: {value}")
    return Path(*relative.parts)


def resource_snapshot(resources: dict[str, Any], indexes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Stable content identity; signed URLs and index ordering are not identity."""
    groups = {}
    for name, index in sorted(indexes.items()):
        rows = sorted(
            (_logical_index_name(str(f["name"])), str(f["md5"]).lower(), int(f["size"]))
            for f in index["files"]
        )
        groups[name] = hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()
    return {
        "resource_version": str(resources["res_version"]),
        "index_fingerprints": groups,
    }


def fetch_resource_snapshot(payload: dict[str, Any], timeout: int = 30) -> dict[str, Any]:
    resources = fetch_resources(payload, timeout=timeout)
    indexes = {
        str(r["name"]): fetch_resource_index(r, timeout=timeout)
        for r in resources["resources"]
    }
    return resource_snapshot(resources, indexes)


@dataclass(frozen=True)
class PatchAction:
    resource: str
    resource_path: str
    target_name: str
    target_md5: str
    target_size: int
    storage_name: str
    base_name: str
    base_md5: str
    base_size: int
    patch_path: str
    patch_size: int

    @property
    def patch_url(self) -> str:
        return f"{self.resource_path.rstrip('/')}/Patch/{self.patch_path}"

    @property
    def peak_bytes(self) -> int:
        return self.base_size + self.patch_size + self.target_size


@dataclass(frozen=True)
class FullAction:
    resource: str
    resource_path: str
    target_name: str
    remote_name: str
    target_md5: str
    target_size: int

    @property
    def file_url(self) -> str:
        return f"{self.resource_path.rstrip('/')}/{self.remote_name}"


def _logical_index_name(value: str) -> str:
    relative = PurePosixPath(value)
    if relative.parts and relative.parts[0] == "VFS":
        relative = PurePosixPath(*relative.parts[1:])
    return safe_relative(relative.as_posix()).as_posix()


def _record_by_logical_name(cache: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(record["logical_name"]): record
        for record in cache.get("files", [])
        if isinstance(record, dict) and record.get("logical_name")
    }


def _matches(path: Path, size: int, md5: str) -> bool:
    return path.is_file() and path.stat().st_size == size and md5_file(path) == md5.lower()


def _matches_verified_record(
    path: Path, record: dict[str, Any], size: int, md5: str
) -> bool:
    """Trust only cache entries written after a completed size+MD5 verification."""
    return (
        path.is_file()
        and path.stat().st_size == size
        and int(record.get("size", -1)) == size
        and str(record.get("md5", "")).lower() == md5.lower()
    )


def build_patch_plan(
    vfs_root: Path,
    resources: dict[str, Any],
    manifests: dict[str, dict[str, Any]],
    indexes: dict[str, dict[str, Any]],
    cache: dict[str, Any] | None = None,
    exported_units: dict[str, str] | None = None,
    exported_identities: set[str] | None = None,
) -> tuple[list[PatchAction], list[FullAction], list[str], int]:
    if cache is None:
        cache = {}
    records = _record_by_logical_name(cache)
    exported_units = exported_units or {}
    exported_identities = exported_identities or set()
    actions: list[PatchAction] = []
    full_actions: list[FullAction] = []
    unavailable: list[str] = []
    current = 0
    seen: set[tuple[str, str]] = set()
    resource_by_name = {
        str(resource["name"]): resource for resource in resources["resources"]
    }
    # The same logical file may occur in more than one patch manifest while
    # its complete CDN object exists only in another group's index. Resolve
    # full fallbacks across all groups before deduplicating patch entries.
    all_index_files: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]] = {}
    for resource_name, index in indexes.items():
        resource = resource_by_name[resource_name]
        for entry in index["files"]:
            identity = (
                _logical_index_name(str(entry["name"])),
                str(entry["md5"]).lower(),
            )
            all_index_files.setdefault(identity, (resource, entry))

    for resource in resources["resources"]:
        manifest = manifests[resource["name"]]
        for item in manifest["files"]:
            identity = (str(item["name"]), str(item["md5"]).lower())
            if identity in seen:
                continue
            seen.add(identity)
            target_name, target_md5 = identity
            target_size = int(item.get("size", 0))
            target_identity = f"{target_md5}:{target_size}"
            if (
                exported_units.get(target_name) == target_identity
                or target_identity in exported_identities
            ):
                current += 1
                continue
            target_record = records.get(target_name)
            if target_record:
                target_path = vfs_root / safe_relative(str(target_record["storage_name"]))
                if _matches_verified_record(
                    target_path, target_record, target_size, target_md5
                ):
                    current += 1
                    continue

            selected: PatchAction | None = None
            for variant in item.get("patch", []):
                base_name = str(variant["base_file"])
                base_md5 = str(variant["base_md5"]).lower()
                base_size = int(variant.get("base_size", 0))
                base_record = records.get(base_name)
                storage_name = str(base_record["storage_name"]) if base_record else base_name
                base_path = vfs_root / safe_relative(storage_name)
                if base_record:
                    base_matches = _matches_verified_record(
                        base_path, base_record, base_size, base_md5
                    )
                else:
                    base_matches = _matches(base_path, base_size, base_md5)
                if not base_matches:
                    continue
                selected = PatchAction(
                    resource=str(resource["name"]),
                    resource_path=str(resource["path"]),
                    target_name=target_name,
                    target_md5=target_md5,
                    target_size=target_size,
                    storage_name=storage_name,
                    base_name=base_name,
                    base_md5=base_md5,
                    base_size=base_size,
                    patch_path=str(variant["patch"]),
                    patch_size=int(variant.get("patch_size", 0)),
                )
                break
            if selected:
                actions.append(selected)
            else:
                indexed = all_index_files.get(identity)
                if indexed:
                    full_resource, full = indexed
                    full_actions.append(
                        FullAction(
                            resource=str(full_resource["name"]),
                            resource_path=str(full_resource["path"]),
                            target_name=target_name,
                            remote_name=safe_relative(str(full["name"])).as_posix(),
                            target_md5=target_md5,
                            target_size=target_size,
                        )
                    )
                else:
                    unavailable.append(target_name)

        for entry in indexes[resource["name"]]["files"]:
            target_name = _logical_index_name(str(entry["name"]))
            target_md5 = str(entry["md5"]).lower()
            identity = (target_name, target_md5)
            if identity in seen:
                continue
            seen.add(identity)
            target_size = int(entry["size"])
            target_identity = f"{target_md5}:{target_size}"
            if (
                exported_units.get(target_name) == target_identity
                or target_identity in exported_identities
            ):
                current += 1
                continue
            target_record = records.get(target_name)
            storage_name = str(target_record["storage_name"]) if target_record else target_name
            target_path = vfs_root / safe_relative(storage_name)
            # A same-sized hotfix can change content without changing its name.
            # Without a verified record, compare the actual MD5, not just size.
            if (
                target_record
                and _matches_verified_record(
                    target_path, target_record, target_size, target_md5
                )
            ) or (
                not target_record
                and _matches(target_path, target_size, target_md5)
            ):
                records[target_name] = {
                    "logical_name": target_name, "storage_name": storage_name,
                    "md5": target_md5, "size": target_size,
                }
                current += 1
                continue
            full_actions.append(
                FullAction(
                    resource=str(resource["name"]),
                    resource_path=str(resource["path"]),
                    target_name=target_name,
                    remote_name=safe_relative(str(entry["name"])).as_posix(),
                    target_md5=target_md5,
                    target_size=target_size,
                )
            )
    # Persist newly verified baseline identities through the caller, so future
    # hotfixes need not hash the entire compressed baseline again.
    if cache is not None:
        cache["files"] = list(records.values())
    return actions, full_actions, unavailable, current


def _download_patch(action: PatchAction, scratch: Path, retries: int = 5) -> Path:
    digest = hashlib.sha256(action.patch_url.encode("utf-8")).hexdigest()[:20]
    destination = scratch / f"{digest}.patch"
    partial = destination.with_suffix(".part")
    for attempt in range(1, retries + 1):
        offset = partial.stat().st_size if partial.exists() else 0
        if offset > action.patch_size:
            partial.unlink()
            offset = 0
        elif offset == action.patch_size and offset > 0:
            # A restart may happen after the final fsync but before rename.
            # The patched target is verified by size+MD5 after application.
            os.replace(partial, destination)
            return destination
        headers = {"User-Agent": "endfield-resource-service/0.3"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        request = urllib.request.Request(action.patch_url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                status = getattr(response, "status", 200)
                if offset and status != 206:
                    partial.unlink(missing_ok=True)
                    offset = 0
                with partial.open("ab" if offset else "wb") as stream:
                    while chunk := response.read(1024 * 1024):
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
            if partial.stat().st_size != action.patch_size:
                raise RuntimeError(
                    f"patch size mismatch for {action.target_name}: "
                    f"{partial.stat().st_size} != {action.patch_size}"
                )
            os.replace(partial, destination)
            return destination
        except (OSError, urllib.error.URLError, RuntimeError):
            if attempt == retries:
                raise
            time.sleep(min(2**attempt, 10))
    raise AssertionError("unreachable")


def _apply_patch(action: PatchAction, vfs_root: Path, patch_path: Path) -> None:
    base_path = vfs_root / safe_relative(action.storage_name)
    target_path = vfs_root / safe_relative(action.target_name)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target_path.name}.", dir=target_path.parent)
    os.close(fd)
    try:
        os.unlink(temporary)
        _run_hpatchz(base_path, patch_path, Path(temporary))
        temporary_path = Path(temporary)
        if temporary_path.stat().st_size != action.target_size:
            raise RuntimeError(
                f"patched size mismatch for {action.target_name}: "
                f"{temporary_path.stat().st_size} != {action.target_size}"
            )
        digest = md5_file(temporary_path)
        if digest != action.target_md5:
            raise RuntimeError(
                f"patched MD5 mismatch for {action.target_name}: {digest} != {action.target_md5}"
            )
        os.replace(temporary, target_path)
        if base_path != target_path:
            base_path.unlink(missing_ok=True)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _run_hpatchz(base_path: Path, patch_path: Path, output_path: Path) -> None:
    configured = os.environ.get("HPATCHZ", "")
    bundled = Path(__file__).resolve().parents[1] / ".tools" / "hpatchz"
    executable = configured or shutil.which("hpatchz") or (str(bundled) if bundled.is_file() else "")
    if not executable:
        raise RuntimeError("hpatchz is required; set HPATCHZ or install .tools/hpatchz")
    result = subprocess.run(
        [executable, str(base_path), str(patch_path), str(output_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if result.returncode != 0:
        tail = result.stdout[-2000:]
        raise RuntimeError(f"hpatchz failed for {base_path}: {tail}")


def apply_patch_plan(
    actions: list[PatchAction],
    vfs_root: Path,
    scratch: Path,
    cache_path: Path,
    resource_version: str,
    memory_limit_mb: int,
    progress: Progress = print,
) -> dict[str, Any]:
    scratch.mkdir(parents=True, exist_ok=True)
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    records = {
        str(record["logical_name"]): record
        for record in cache.get("files", [])
        if isinstance(record, dict) and record.get("logical_name")
    }
    limit = memory_limit_mb * 1024 * 1024
    for index, action in enumerate(actions, 1):
        completed = records.get(action.target_name)
        if completed and _matches_verified_record(
            vfs_root / safe_relative(str(completed["storage_name"])),
            completed, action.target_size, action.target_md5,
        ):
            progress(f"patch {index}/{len(actions)}: already applied {action.target_name}")
            continue
        if action.peak_bytes > limit:
            raise RuntimeError(
                f"patch for {action.target_name} needs an estimated "
                f"{action.peak_bytes // 1048576} MiB, above the {memory_limit_mb} MiB budget"
            )
        progress(f"patch {index}/{len(actions)}: {action.target_name}")
        patch_path = _download_patch(action, scratch)
        try:
            _apply_patch(action, vfs_root, patch_path)
        finally:
            patch_path.unlink(missing_ok=True)
        records.pop(action.base_name, None)
        records[action.target_name] = {
            "logical_name": action.target_name,
            "storage_name": action.target_name,
            "md5": action.target_md5,
            "size": action.target_size,
        }
        atomic_json(
            cache_path,
            {"resource_version": resource_version, "files": list(records.values())},
        )
    return {"resource_version": resource_version, "files": list(records.values())}


def _download_full(
    action: FullAction,
    vfs_root: Path,
    scratch: Path,
    retries: int = 5,
    progress: Progress = print,
) -> Path:
    digest = hashlib.sha256(action.file_url.encode("utf-8")).hexdigest()[:20]
    partial = scratch / f"{digest}.full.part"
    for attempt in range(1, retries + 1):
        offset = partial.stat().st_size if partial.exists() else 0
        if offset > action.target_size:
            partial.unlink()
            offset = 0
        elif offset == action.target_size and offset > 0:
            if md5_file(partial) == action.target_md5:
                target = vfs_root / safe_relative(action.target_name)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(partial, target)
                progress(f"full recovered complete partial: {action.target_name}")
                return target
            partial.unlink()
            offset = 0
        headers = {"User-Agent": "endfield-resource-service/0.3"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        request = urllib.request.Request(action.file_url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                status = getattr(response, "status", 200)
                if offset and status != 206:
                    partial.unlink(missing_ok=True)
                    offset = 0
                with partial.open("ab" if offset else "wb") as stream:
                    downloaded = offset
                    last_report = time.monotonic()
                    while chunk := response.read(1024 * 1024):
                        stream.write(chunk)
                        downloaded += len(chunk)
                        now = time.monotonic()
                        if downloaded == action.target_size or now - last_report >= 30:
                            progress(
                                f"full download: {action.target_name} "
                                f"bytes={downloaded}/{action.target_size} "
                                f"percent={downloaded * 100 / action.target_size:.1f}"
                            )
                            last_report = now
                    stream.flush()
                    os.fsync(stream.fileno())
            if partial.stat().st_size != action.target_size:
                raise RuntimeError(
                    f"full resource size mismatch for {action.target_name}: "
                    f"{partial.stat().st_size} != {action.target_size}"
                )
            if md5_file(partial) != action.target_md5:
                raise RuntimeError(f"full resource MD5 mismatch for {action.target_name}")
            target = vfs_root / safe_relative(action.target_name)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(partial, target)
            return target
        except (OSError, urllib.error.URLError, RuntimeError):
            if attempt == retries:
                raise
            time.sleep(min(2**attempt, 10))
    raise AssertionError("unreachable")


def apply_full_plan(
    actions: list[FullAction],
    vfs_root: Path,
    scratch: Path,
    cache_path: Path,
    resource_version: str,
    workers: int = 1,
    progress: Progress = print,
) -> dict[str, Any]:
    scratch.mkdir(parents=True, exist_ok=True)
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    records = _record_by_logical_name(cache)
    worker_count = max(1, min(workers, len(actions))) if actions else 1
    progress(f"full download queue: files={len(actions)} workers={worker_count}")
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {}
        for index, action in enumerate(actions, 1):
            progress(f"full queued {index}/{len(actions)}: {action.target_name}")
            futures[pool.submit(
                _download_full, action, vfs_root, scratch, progress=progress
            )] = action
        completed = 0
        for future in as_completed(futures):
            action = futures[future]
            future.result()
            completed += 1
            records[action.target_name] = {
                "logical_name": action.target_name,
                "storage_name": action.target_name,
                "md5": action.target_md5,
                "size": action.target_size,
            }
            atomic_json(
                cache_path,
                {"resource_version": resource_version, "files": list(records.values())},
            )
            progress(f"full complete {completed}/{len(actions)}: {action.target_name}")
    return {"resource_version": resource_version, "files": list(records.values())}


def serialize_actions(actions: list[PatchAction]) -> list[dict[str, Any]]:
    return [{**asdict(action), "patch_url": action.patch_url} for action in actions]


def serialize_full_actions(actions: list[FullAction]) -> list[dict[str, Any]]:
    return [{**asdict(action), "file_url": action.file_url} for action in actions]
