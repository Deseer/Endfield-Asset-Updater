from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .downloader import download_packs, md5_file
from .manifest import atomic_json, fetch_latest, pack_filename, sanitize_manifest
from .resources import (
    apply_full_plan,
    apply_patch_plan,
    build_patch_plan,
    fetch_patch_manifest,
    fetch_resource_index,
    fetch_resources,
    resource_snapshot,
    serialize_actions,
    serialize_full_actions,
)


class Pipeline:
    def __init__(self, data_root: Path, workers: int = 4) -> None:
        self.root = data_root.resolve()
        self.workers = workers
        self.work = self.root / ".work"
        self.state = self.work / "State"
        self.packages = self.work / "Packages"
        self.vfs_root = self.work / "VFS"
        self.exports = self.root
        self.logs = self.work / "Logs"
        for path in (self.state, self.packages, self.vfs_root, self.logs):
            path.mkdir(parents=True, exist_ok=True)

    def latest(self) -> dict[str, Any]:
        payload = fetch_latest()
        version = str(payload["version"])
        atomic_json(self.state / f"official-manifest-{version}.json", sanitize_manifest(payload))
        return payload

    def resource_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        version = str(payload["version"])
        streaming_assets = self.find_streaming_assets(self.vfs_root / version)
        resource_list = fetch_resources(payload)
        manifests = {
            str(resource["name"]): fetch_patch_manifest(resource)
            for resource in resource_list["resources"]
        }
        indexes = {
            str(resource["name"]): fetch_resource_index(resource)
            for resource in resource_list["resources"]
        }
        resource_version = str(resource_list["res_version"])
        atomic_json(self.state / f"resources-{version}.json", resource_list)
        for name, manifest in manifests.items():
            atomic_json(self.state / f"resource-patch-{name}-{resource_version}.json", manifest)
        for name, index in indexes.items():
            atomic_json(self.state / f"resource-index-{name}-{resource_version}.json", index)
        cache_path = self.state / "resource-cache.json"
        cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
        exported_units = self._reusable_exported_units()
        actions, full_actions, unavailable, current = build_patch_plan(
            streaming_assets / "VFS", resource_list, manifests, indexes, cache,
            exported_units=exported_units,
            exported_identities=set(exported_units.values()),
        )
        atomic_json(cache_path, cache)
        atomic_json(self.state / "resource-snapshot.json", resource_snapshot(resource_list, indexes))
        result = {
            "game_version": version,
            "resource_version": resource_version,
            "resource_groups": [resource["name"] for resource in resource_list["resources"]],
            "current_files": current,
            "patch_files": len(actions),
            "patch_bytes": sum(action.patch_size for action in actions),
            "full_files": len(full_actions),
            "full_bytes": sum(action.target_size for action in full_actions),
            "estimated_peak_bytes": max((action.peak_bytes for action in actions), default=0),
            "unavailable": unavailable,
            "actions": serialize_actions(actions),
            "full_actions": serialize_full_actions(full_actions),
        }
        atomic_json(self.state / f"resource-plan-{resource_version}.json", result)
        return result

    def _reusable_exported_units(self) -> dict[str, str]:
        result: dict[str, str] = {}
        logical_names: dict[str, str] = {}
        # Unit manifests are published together with export-manifest.json only
        # after a completed atomic export. Trust that committed inventory here:
        # walking and stat'ing every output turns a cheap CDN plan into tens of
        # thousands of random reads on the external disk.
        if not (self.root / "export-manifest.json").is_file():
            return result
        manifests = self.root / ".baseline" / "export-units"
        if not manifests.is_dir():
            return result
        index_path = self.root / ".baseline" / "export-unit-index.json"
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
            units = index["units"]
            if isinstance(units, dict):
                for logical_name, identity in units.items():
                    relative = Path(str(logical_name))
                    if (not relative.is_absolute() and ".." not in relative.parts
                            and isinstance(identity, str) and identity):
                        result[str(logical_name)] = identity
                if result:
                    return result
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
            pass
        for path in manifests.glob("*.json"):
            if path.name.startswith("Table-"):
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                logical_name = str(value["logical_name"])
                identity = str(value["identity"])
                outputs = value["outputs"]
                if not isinstance(outputs, list) or not outputs:
                    continue
                valid = True
                for output in outputs:
                    relative = Path(str(output))
                    if relative.is_absolute() or ".." in relative.parts:
                        valid = False
                        break
                if valid:
                    result[logical_name] = identity
                    logical_names[path.stem] = logical_name
            except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
                continue
        if result:
            atomic_json(
                index_path,
                {"format": 1, "units": result, "logical_names": logical_names},
            )
        return result

    def sync_resources(
        self,
        payload: dict[str, Any],
        memory_limit_mb: int,
        plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        plan = plan or self.resource_plan(payload)
        if plan["unavailable"]:
            raise RuntimeError(
                f"{len(plan['unavailable'])} CDN resources have no matching local base; "
                f"see {self.state / ('resource-plan-' + plan['resource_version'] + '.json')}"
            )
        actions = []
        full_actions = []
        from .resources import FullAction, PatchAction

        for value in plan["actions"]:
            value = dict(value)
            value.pop("patch_url", None)
            actions.append(PatchAction(**value))
        for value in plan["full_actions"]:
            value = dict(value)
            value.pop("file_url", None)
            full_actions.append(FullAction(**value))
        streaming_assets = self.find_streaming_assets(self.vfs_root / str(payload["version"]))
        cache = apply_patch_plan(
            actions,
            streaming_assets / "VFS",
            self.work / "Scratch" / "resource-patches" / plan["resource_version"],
            self.state / "resource-cache.json",
            plan["resource_version"],
            memory_limit_mb,
        )
        cache = apply_full_plan(
            full_actions,
            streaming_assets / "VFS",
            self.work / "Scratch" / "resource-full" / plan["resource_version"],
            self.state / "resource-cache.json",
            plan["resource_version"],
            workers=self.workers,
        )
        return {
            **plan,
            "applied_files": len(actions),
            "downloaded_full_files": len(full_actions),
            "cache_files": len(cache["files"]),
        }

    def download(self, payload: dict[str, Any]) -> Path:
        version = str(payload["version"])
        destination = self.packages / version
        packs = payload["pkg"]["packs"]
        download_packs(packs, destination, self.workers)
        inventory = {
            "version": version,
            "verified_at": int(time.time()),
            "packages": [
                {
                    "name": pack_filename(pack),
                    "bytes": int(pack["package_size"]),
                    "md5": pack["md5"],
                }
                for pack in packs
            ],
        }
        atomic_json(self.state / f"packages-{version}.json", inventory)
        return destination

    def verify_packages(self, payload: dict[str, Any]) -> Path:
        version = str(payload["version"])
        package_dir = self.packages / version
        verification_path = self.state / f"verified-input-{version}.json"
        expected_snapshot = []
        for pack in payload["pkg"]["packs"]:
            path = package_dir / pack_filename(pack)
            if not path.is_file():
                raise RuntimeError(f"missing package: {path}")
            if path.stat().st_size != int(pack["package_size"]):
                raise RuntimeError(f"package size mismatch: {path}")
            expected_snapshot.append(
                {
                    "name": path.name,
                    "bytes": path.stat().st_size,
                    "mtime_ns": path.stat().st_mtime_ns,
                    "md5": str(pack["md5"]).lower(),
                }
            )
        if verification_path.exists():
            cached = json.loads(verification_path.read_text(encoding="utf-8"))
            if cached.get("packages") == expected_snapshot:
                return package_dir
        for pack in payload["pkg"]["packs"]:
            path = package_dir / pack_filename(pack)
            if md5_file(path) != str(pack["md5"]).lower():
                raise RuntimeError(f"package MD5 mismatch: {path}")
        atomic_json(
            verification_path,
            {"version": version, "verified_at": int(time.time()), "packages": expected_snapshot},
        )
        return package_dir

    def extract_vfs(self, payload: dict[str, Any]) -> Path:
        version = str(payload["version"])
        package_dir = self.verify_packages(payload)
        first = package_dir / pack_filename(payload["pkg"]["packs"][0])
        destination = self.vfs_root / version
        marker = destination / ".extraction-complete.json"
        if marker.exists():
            return self.find_streaming_assets(destination)
        if destination.exists() and any(destination.iterdir()):
            raise RuntimeError(
                f"incomplete extraction directory is not empty; inspect it first: {destination}"
            )
        destination.mkdir(parents=True, exist_ok=True)
        seven_zip = shutil.which("7zz") or shutil.which("7z")
        if not seven_zip:
            raise RuntimeError("7-Zip (7zz or 7z) is required")
        command = [
            seven_zip,
            "x",
            str(first),
            f"-o{destination}",
            "-y",
            "-bb1",
            "Endfield_Data/StreamingAssets/*",
        ]
        log_path = self.logs / f"extract-{version}.log"
        with log_path.open("w", encoding="utf-8") as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"7-Zip extraction failed; see {log_path}")
        streaming_assets = self.find_streaming_assets(destination)
        inventory = self.inventory(streaming_assets)
        inventory.update({"version": version, "completed_at": int(time.time())})
        atomic_json(marker, inventory)
        return streaming_assets

    @staticmethod
    def find_streaming_assets(root: Path) -> Path:
        candidates = [path for path in root.rglob("StreamingAssets") if path.is_dir()]
        with_vfs = [path for path in candidates if (path / "VFS").is_dir()]
        if len(with_vfs) != 1:
            raise RuntimeError(f"expected one StreamingAssets/VFS tree, found {len(with_vfs)}")
        return with_vfs[0]

    @staticmethod
    def inventory(root: Path) -> dict[str, int]:
        file_count = 0
        byte_count = 0
        for path in root.rglob("*"):
            if path.is_file():
                file_count += 1
                byte_count += path.stat().st_size
        return {"file_count": file_count, "bytes": byte_count}

    def write_export_marker(self, version: str) -> Path:
        root = self.exports
        expected = ["MasterData", "Raw", "Textures", "Audio", "Video"]
        missing = [name for name in expected if not (root / name).is_dir()]
        if missing:
            raise RuntimeError(f"export is incomplete, missing directories: {missing}")
        sections = {name: self.inventory(root / name) for name in expected}
        marker = root / "export-manifest.json"
        atomic_json(
            marker,
            {"version": version, "completed_at": int(time.time()), "sections": sections},
        )
        return marker
