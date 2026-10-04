#!/usr/bin/env python3
"""Persistent per-VFS-unit identity and output ownership for incremental export."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath


def safe_relative(value: str) -> Path:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"unsafe relative path: {value}")
    return Path(*path.parts)


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.replace(temporary, path)


def protected_map_outputs(manifest: Path | None) -> set[str]:
    if manifest is None or not manifest.is_file():
        return set()
    try:
        records = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(records, list):
        return set()
    return {
        (Path("Textures/other") / safe_relative(str(record["target"]))).as_posix()
        for record in records
        if isinstance(record, dict) and isinstance(record.get("target"), str)
    }


def resource_identity(state: Path, version: str, logical_name: str) -> str:
    resources = json.loads((state / f"resources-{version}.json").read_text())
    resource_version = str(resources["res_version"])
    wanted = logical_name[4:] if logical_name.startswith("VFS/") else logical_name
    for index in state.glob(f"resource-index-*-{resource_version}.json"):
        payload = json.loads(index.read_text())
        for entry in payload.get("files", []):
            name = str(entry["name"])
            name = name[4:] if name.startswith("VFS/") else name
            if name == wanted:
                return f"{str(entry['md5']).lower()}:{int(entry['size'])}"
    raise RuntimeError(f"resource identity not found: {logical_name}")


def resource_plan_path(state: Path, version: str) -> Path:
    resources = json.loads((state / f"resources-{version}.json").read_text())
    path = state / f"resource-plan-{resources['res_version']}.json"
    if not path.is_file():
        raise RuntimeError(f"resource plan not found: {path}")
    return path


def changed_resource_paths(plan: Path) -> list[str]:
    payload = json.loads(plan.read_text())
    result: set[str] = set()
    for section in ("actions", "full_actions"):
        for action in payload.get(section, []):
            value = str(action.get("target_name", ""))
            if value:
                result.add(safe_relative(value).as_posix())
    return sorted(result)


def manifest_logical_names(manifests: Path) -> list[tuple[str, str]]:
    """Return the persisted unit key and logical CDN path for valid manifests."""
    result: list[tuple[str, str]] = []
    if not manifests.is_dir():
        return result
    index = read_manifest(manifests.parent / "export-unit-index.json")
    indexed_names = index.get("logical_names") if index else None
    if isinstance(indexed_names, dict):
        for key, logical_name in sorted(indexed_names.items()):
            if isinstance(key, str) and key and isinstance(logical_name, str) and logical_name:
                result.append((key, safe_relative(logical_name).as_posix()))
        if result:
            return result
    for manifest in sorted(manifests.glob("*.json")):
        payload = read_manifest(manifest)
        logical_name = payload.get("logical_name") if payload else None
        if not isinstance(logical_name, str) or not logical_name:
            continue
        result.append((manifest.stem, safe_relative(logical_name).as_posix()))
    return result


def source_identity_map(path: Path | None) -> dict[str, str]:
    result: dict[str, str] = {}
    if path is None or not path.is_file():
        return result
    for line in path.read_text().splitlines():
        fields = line.split("\t")
        if len(fields) != 5:
            continue
        block, chunk, digest, count, byte_count = fields
        key = f"{block}-{Path(chunk).stem}"
        result[key] = f"{digest.lower()}:{int(count)}:{int(byte_count)}"
    return result


def build_unit_index(manifests: Path, output: Path, source_identities: Path | None = None) -> int:
    """Build the compact CDN planning index after all unit commits finish."""
    units: dict[str, str] = {}
    logical_names: dict[str, str] = {}
    source_units: dict[str, str] = {}
    current_sources = source_identity_map(source_identities)
    if manifests.is_dir():
        for manifest in sorted(manifests.glob("*.json")):
            if manifest.name.startswith("Table-"):
                continue
            payload = read_manifest(manifest)
            if not payload:
                continue
            logical_name = payload.get("logical_name")
            identity = payload.get("identity")
            outputs = payload.get("outputs")
            if (not isinstance(logical_name, str) or not logical_name
                    or not isinstance(identity, str) or not identity
                    or not isinstance(outputs, list) or not outputs):
                continue
            units[safe_relative(logical_name).as_posix()] = identity
            logical_names[manifest.stem] = safe_relative(logical_name).as_posix()
            source_fingerprint = current_sources.get(manifest.stem) or payload.get("source_fingerprint")
            if isinstance(source_fingerprint, str) and source_fingerprint:
                source_units[manifest.stem] = source_fingerprint
    atomic_json(output, {
        "format": 1,
        "units": units,
        "logical_names": logical_names,
        "source_units": source_units,
    })
    return len(units)


def set_source_fingerprint(manifest: Path, fingerprint: str) -> bool:
    payload = read_manifest(manifest)
    if not payload:
        return False
    if payload.get("source_fingerprint") == fingerprint:
        return True
    payload["source_fingerprint"] = fingerprint
    atomic_json(manifest, payload)
    return True


def reuse_source_fingerprint(
    manifests: Path,
    current_manifest: Path,
    fingerprint: str,
    version: str,
    logical_name: str,
    identity: str,
) -> bool:
    """Reuse an atomically published unit when VFS logical content is identical."""
    block = current_manifest.stem.split("-", 1)[0]
    candidates: list[Path] = []
    indexed_candidates = False
    index = read_manifest(manifests.parent / "export-unit-index.json")
    source_units = index.get("source_units") if index else None
    if isinstance(source_units, dict):
        candidates = [
            manifests / f"{key}.json"
            for key, value in source_units.items()
            if key.startswith(f"{block}-") and value == fingerprint
        ]
        indexed_candidates = bool(candidates)
    if not candidates:
        candidates = sorted(manifests.glob(f"{block}-*.json"))
    for candidate in candidates:
        payload = read_manifest(candidate)
        if not payload or (not indexed_candidates and payload.get("source_fingerprint") != fingerprint):
            continue
        outputs = payload.get("outputs")
        if not isinstance(outputs, list) or not outputs:
            continue
        payload.update({
            "version": version,
            "logical_name": safe_relative(logical_name).as_posix(),
            "identity": identity,
            "source_fingerprint": fingerprint,
        })
        atomic_json(current_manifest, payload)
        return True
    return False


def read_manifest(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def output_paths(path: Path) -> list[str]:
    if not path.exists():
        return []
    result = []
    for line in path.read_text().splitlines():
        if line:
            result.append(safe_relative(line).as_posix())
    return sorted(set(result))


def carry_forward_outputs(manifest: Path, output: Path, root: Path) -> int:
    """Keep still-present outputs when only a subset of a VFS unit was derived."""
    old = read_manifest(manifest) or {}
    carried = []
    for value in old.get("outputs", []):
        relative = safe_relative(str(value)).as_posix()
        if (root / safe_relative(relative)).is_file():
            carried.append(relative)
    if carried:
        with output.open("a", encoding="utf-8") as handle:
            handle.writelines(f"{value}\n" for value in carried)
    return len(carried)


def reusable_manifest(
    manifest: Path, logical_name: str, identity: str, root: Path | None = None
) -> bool:
    old = read_manifest(manifest)
    if not old or old.get("logical_name") != logical_name or old.get("identity") != identity:
        return False
    if root is None:
        return True
    outputs = old.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        return False
    sizes = old.get("output_sizes") if isinstance(old.get("output_sizes"), dict) else {}
    for value in outputs:
        relative = safe_relative(str(value)).as_posix()
        target = root / safe_relative(relative)
        if not target.is_file():
            return False
        expected_size = sizes.get(relative)
        if expected_size is not None and target.stat().st_size != int(expected_size):
            return False
    return True


def files_equal(left: Path, right: Path) -> bool:
    try:
        if left.stat().st_size != right.stat().st_size:
            return False
        with left.open("rb") as left_file, right.open("rb") as right_file:
            while True:
                left_block = left_file.read(4 * 1024 * 1024)
                right_block = right_file.read(4 * 1024 * 1024)
                if left_block != right_block:
                    return False
                if not left_block:
                    return True
    except OSError:
        return False


def write_changed_list(source_root: Path, existing_root: Path, output: Path) -> int:
    source_root = source_root.resolve()
    changed: list[str] = []
    if source_root.is_dir():
        for source in sorted(path for path in source_root.rglob("*") if path.is_file()):
            relative = source.relative_to(source_root).as_posix()
            destination = existing_root / safe_relative(relative)
            if not destination.is_file() or not files_equal(source, destination):
                changed.append(relative)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text("".join(f"{path}\n" for path in changed))
    os.replace(temporary, output)
    return len(changed)


def bootstrap_video_outputs(
    source_root: Path, existing_raw: Path, existing_video: Path, output: Path
) -> bool:
    source_root = source_root.resolve()
    rows: list[str] = []
    sources = sorted(path for path in source_root.rglob("*.usm") if path.is_file())
    if not sources:
        return False
    for source in sources:
        relative = source.relative_to(source_root)
        if not files_equal(source, existing_raw / relative):
            return False
        video_relative = relative.with_suffix(".mp4")
        video = existing_video / video_relative
        if not video.is_file() or video.stat().st_size <= 0:
            return False
        rows.append((Path("Video") / video_relative).as_posix())
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text("".join(f"{value}\n" for value in rows))
    os.replace(temporary, output)
    return True


def tree_fingerprint(root: Path) -> str:
    root = root.resolve()
    digest = hashlib.sha256()
    files = sorted(path for path in root.rglob("*") if path.is_file())
    for path in files:
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(path.stat().st_size.to_bytes(8, "big"))
        with path.open("rb") as handle:
            while block := handle.read(4 * 1024 * 1024):
                digest.update(block)
    return digest.hexdigest()


def reuse_by_fingerprint(
    manifests: Path, fingerprint: str, root: Path, output: Path
) -> bool:
    if not manifests.is_dir():
        return False
    for manifest in sorted(manifests.glob("*.json")):
        old = read_manifest(manifest)
        if not old or old.get("raw_fingerprint") != fingerprint:
            continue
        logical_name = str(old.get("logical_name", ""))
        identity = str(old.get("identity", ""))
        if not reusable_manifest(manifest, logical_name, identity, root):
            continue
        rows = [safe_relative(str(value)).as_posix() for value in old["outputs"]]
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text("".join(f"{value}\n" for value in rows))
        os.replace(temporary, output)
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    identity = sub.add_parser("identity")
    identity.add_argument("state", type=Path)
    identity.add_argument("version")
    identity.add_argument("logical_name")
    can_skip = sub.add_parser("can-skip")
    can_skip.add_argument("manifest", type=Path)
    can_skip.add_argument("logical_name")
    can_skip.add_argument("identity")
    can_skip.add_argument("root", type=Path, nargs="?")
    prune = sub.add_parser("prune-stale")
    prune.add_argument("manifest", type=Path)
    prune.add_argument("outputs", type=Path)
    prune.add_argument("root", type=Path)
    prune.add_argument("--protected-map-manifest", type=Path)
    carry = sub.add_parser("carry-forward")
    carry.add_argument("manifest", type=Path)
    carry.add_argument("outputs", type=Path)
    carry.add_argument("root", type=Path)
    record = sub.add_parser("record")
    record.add_argument("manifest", type=Path)
    record.add_argument("version")
    record.add_argument("logical_name")
    record.add_argument("identity")
    record.add_argument("outputs", type=Path)
    record.add_argument("root", type=Path, nargs="?")
    record.add_argument("raw_fingerprint", nargs="?")
    record.add_argument("source_fingerprint", nargs="?")
    retain = sub.add_parser("retain-listed")
    retain.add_argument("root", type=Path)
    retain.add_argument("paths", type=Path)
    changed = sub.add_parser("changed-list")
    changed.add_argument("source_root", type=Path)
    changed.add_argument("existing_root", type=Path)
    changed.add_argument("output", type=Path)
    bootstrap_video = sub.add_parser("bootstrap-video")
    bootstrap_video.add_argument("source_root", type=Path)
    bootstrap_video.add_argument("existing_raw", type=Path)
    bootstrap_video.add_argument("existing_video", type=Path)
    bootstrap_video.add_argument("output", type=Path)
    fingerprint = sub.add_parser("tree-fingerprint")
    fingerprint.add_argument("root", type=Path)
    reuse_fingerprint = sub.add_parser("reuse-fingerprint")
    reuse_fingerprint.add_argument("manifests", type=Path)
    reuse_fingerprint.add_argument("fingerprint")
    reuse_fingerprint.add_argument("root", type=Path)
    reuse_fingerprint.add_argument("output", type=Path)
    plan_path = sub.add_parser("plan-path")
    plan_path.add_argument("state", type=Path)
    plan_path.add_argument("version")
    changed_paths = sub.add_parser("changed-paths")
    changed_paths.add_argument("plan", type=Path)
    manifest_names = sub.add_parser("manifest-logical-names")
    manifest_names.add_argument("manifests", type=Path)
    set_source = sub.add_parser("set-source-fingerprint")
    set_source.add_argument("manifest", type=Path)
    set_source.add_argument("fingerprint")
    reuse_source = sub.add_parser("reuse-source-fingerprint")
    reuse_source.add_argument("manifests", type=Path)
    reuse_source.add_argument("current_manifest", type=Path)
    reuse_source.add_argument("fingerprint")
    reuse_source.add_argument("version")
    reuse_source.add_argument("logical_name")
    reuse_source.add_argument("identity")
    build_index = sub.add_parser("build-index")
    build_index.add_argument("manifests", type=Path)
    build_index.add_argument("output", type=Path)
    build_index.add_argument("source_identities", type=Path, nargs="?")
    args = parser.parse_args()

    if args.command == "identity":
        print(resource_identity(args.state, args.version, args.logical_name))
        return 0
    if args.command == "can-skip":
        return 0 if reusable_manifest(
            args.manifest, args.logical_name, args.identity, args.root
        ) else 1
    if args.command == "prune-stale":
        old = read_manifest(args.manifest) or {}
        current = set(output_paths(args.outputs))
        protected = protected_map_outputs(args.protected_map_manifest)
        root = args.root.resolve()
        for value in old.get("outputs", []):
            if value in current or value in protected:
                continue
            target = (root / safe_relative(str(value))).resolve()
            if root not in target.parents:
                raise RuntimeError(f"output escapes root: {value}")
            target.unlink(missing_ok=True)
        return 0
    if args.command == "carry-forward":
        print(carry_forward_outputs(args.manifest, args.outputs, args.root))
        return 0
    if args.command == "record":
        outputs = output_paths(args.outputs)
        sizes = {}
        if args.root is not None:
            for value in outputs:
                target = args.root / safe_relative(value)
                if target.is_file():
                    sizes[value] = target.stat().st_size
        atomic_json(
            args.manifest,
            {
                "version": args.version,
                "logical_name": args.logical_name,
                "identity": args.identity,
                "outputs": outputs,
                "output_sizes": sizes,
                "raw_fingerprint": args.raw_fingerprint,
                "source_fingerprint": args.source_fingerprint,
            },
        )
        return 0
    if args.command == "retain-listed":
        root = args.root.resolve()
        keep = set(output_paths(args.paths))
        if root.is_dir():
            for path in root.rglob("*"):
                if path.is_file() and path.relative_to(root).as_posix() not in keep:
                    path.unlink()
            for path in sorted(
                (path for path in root.rglob("*") if path.is_dir()),
                key=lambda value: len(value.parts),
                reverse=True,
            ):
                try:
                    path.rmdir()
                except OSError:
                    pass
        return 0
    if args.command == "changed-list":
        print(write_changed_list(args.source_root, args.existing_root, args.output))
        return 0
    if args.command == "bootstrap-video":
        return 0 if bootstrap_video_outputs(
            args.source_root, args.existing_raw, args.existing_video, args.output
        ) else 1
    if args.command == "tree-fingerprint":
        print(tree_fingerprint(args.root))
        return 0
    if args.command == "reuse-fingerprint":
        return 0 if reuse_by_fingerprint(
            args.manifests, args.fingerprint, args.root, args.output
        ) else 1
    if args.command == "plan-path":
        print(resource_plan_path(args.state, args.version))
        return 0
    if args.command == "changed-paths":
        for value in changed_resource_paths(args.plan):
            print(value)
        return 0
    if args.command == "manifest-logical-names":
        for key, logical_name in manifest_logical_names(args.manifests):
            print(f"{key}\t{logical_name}")
        return 0
    if args.command == "set-source-fingerprint":
        return 0 if set_source_fingerprint(args.manifest, args.fingerprint) else 1
    if args.command == "reuse-source-fingerprint":
        return 0 if reuse_source_fingerprint(
            args.manifests,
            args.current_manifest,
            args.fingerprint,
            args.version,
            args.logical_name,
            args.identity,
        ) else 1
    if args.command == "build-index":
        print(build_unit_index(args.manifests, args.output, args.source_identities))
        return 0
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
