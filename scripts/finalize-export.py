#!/usr/bin/env python3
"""Write the final inventory and remove exact ZMD work state."""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path


EXPECTED = ("MasterData", "Raw", "Textures", "Audio", "Video")


def preserve_vfs_baseline(root: Path, version: str) -> None:
    preserve_vfs = os.environ.get("ENDFIELD_PRESERVE_VFS", "0") == "1"
    version_root = root / ".work" / "VFS" / version
    candidates = [
        path for path in version_root.rglob("StreamingAssets")
        if (path / "VFS").is_dir()
    ] if version_root.is_dir() else []
    if preserve_vfs and len(candidates) != 1:
        raise RuntimeError(f"cannot preserve VFS baseline: found {len(candidates)} StreamingAssets trees")
    baseline = root / ".baseline"
    baseline.mkdir(parents=True, exist_ok=True)
    if preserve_vfs:
        destination = baseline / "StreamingAssets"
        if destination.exists():
            raise RuntimeError(f"refusing to overwrite existing VFS baseline: {destination}")
        os.replace(candidates[0], destination)

    cache = root / ".work" / "State" / "resource-cache.json"
    if not cache.is_file():
        try:
            saved_baseline = json.loads((baseline / "baseline-manifest.json").read_text())
        except (OSError, ValueError):
            saved_baseline = {}
        if (baseline / "resource-cache.json").is_file() and saved_baseline.get("version") == version:
            print(f"incremental metadata already preserved at {baseline}")
            return
        raise RuntimeError("cannot preserve VFS baseline without resource-cache.json")
    os.replace(cache, baseline / "resource-cache.json")
    snapshot = root / ".work" / "State" / "resource-snapshot.json"
    if snapshot.is_file():
        os.replace(snapshot, baseline / "resource-snapshot.json")
    temporary = baseline / "baseline-manifest.json.tmp"
    temporary.write_text(
        json.dumps({"version": version, "preserved_at": int(time.time())}, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, baseline / "baseline-manifest.json")
    print(f"preserved incremental metadata for {version} at {baseline} vfs={preserve_vfs}")


def inventory(root: Path) -> dict[str, int]:
    file_count = 0
    byte_count = 0
    for directory, _, files in os.walk(root):
        base = Path(directory)
        for name in files:
            path = base / name
            if name == ".DS_Store":
                path.unlink()
                continue
            file_count += 1
            byte_count += path.stat().st_size
    return {"file_count": file_count, "bytes": byte_count}


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: finalize-export.py DATA_ROOT VERSION")
    root = Path(sys.argv[1]).resolve()
    version = sys.argv[2]
    if root != Path("/data"):
        raise SystemExit(f"refusing unexpected data root: {root}")

    missing = [name for name in EXPECTED if not (root / name).is_dir()]
    if missing:
        raise RuntimeError(f"export is incomplete, missing directories: {missing}")
    marker = root / "export-manifest.json"
    try:
        previous = json.loads(marker.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        previous = {}
    previous_sections = previous.get("sections")
    if isinstance(previous_sections, dict) and all(
        isinstance(previous_sections.get(name), dict) for name in EXPECTED
    ):
        # Per-unit manifests and the compact identity index are authoritative
        # for incremental correctness. Rewalking hundreds of thousands of
        # external-disk files here only refreshes informational counters.
        sections = {name: previous_sections[name] for name in EXPECTED}
        inventory_mode = "reused_previous"
    else:
        sections = {name: inventory(root / name) for name in EXPECTED}
        inventory_mode = "full"
    snapshot_path = root / ".work" / "State" / "resource-snapshot.json"
    snapshot = json.loads(snapshot_path.read_text()) if snapshot_path.is_file() else {}
    preserve_vfs_baseline(root, version)
    temporary = marker.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(
            {"version": version, "completed_at": int(time.time()), "sections": sections,
             "inventory_mode": inventory_mode,
             **snapshot},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, marker)

    (root / ".DS_Store").unlink(missing_ok=True)
    print(f"retained work tree {root / '.work'}")
    state_marker = root / ".work" / "State" / "watcher-state.json"
    state_marker.unlink(missing_ok=True)
    print(f"finalized {marker}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
