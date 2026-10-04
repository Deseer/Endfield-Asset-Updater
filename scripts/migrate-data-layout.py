#!/usr/bin/env python3
"""Atomically migrate the legacy versioned ZMD data layout."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


WORK_DIRS = ("State", "Packages", "VFS", "Logs", "Scratch")
EXPORT_DIRS = ("MasterData", "Raw", "Textures", "Audio", "Video")


def move_exact(source: Path, destination: Path) -> bool:
    if not source.exists():
        return False
    if destination.exists():
        raise RuntimeError(f"refusing to merge layout paths: {source} -> {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, destination)
    print(f"moved {source} -> {destination}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("data_root", type=Path)
    parser.add_argument("version")
    args = parser.parse_args()

    root = args.data_root.resolve()
    if root != Path("/Volumes/wd/ZmdResourceService"):
        raise SystemExit(f"refusing unexpected data root: {root}")
    if not root.is_dir():
        raise SystemExit(f"data root does not exist: {root}")

    work = root / ".work"
    work.mkdir(exist_ok=True)
    for name in WORK_DIRS:
        move_exact(root / name, work / name)

    legacy = root / "Export" / args.version
    if not legacy.is_dir():
        raise SystemExit(f"legacy export directory does not exist: {legacy}")
    for name in EXPORT_DIRS:
        move_exact(legacy / name, root / name)
    move_exact(legacy / "export-manifest.json", root / "export-manifest.json")

    (legacy / ".DS_Store").unlink(missing_ok=True)
    (root / "Export" / ".DS_Store").unlink(missing_ok=True)
    legacy.rmdir()
    (root / "Export").rmdir()
    print(f"layout migrated: exports are directly below {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
