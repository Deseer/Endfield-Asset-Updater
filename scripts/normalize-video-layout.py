#!/usr/bin/env python3
"""Move legacy raw USM files out of friendly Video and into Raw."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


ROOT = Path("/Volumes/wd/ZmdResourceService")


def digest(path: Path) -> bytes:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.digest()


def main() -> int:
    video = ROOT / "Video"
    raw = ROOT / "Raw"
    if not video.is_dir() or not raw.is_dir():
        raise SystemExit("expected Video and Raw directories are missing")

    moved = 0
    duplicates = 0
    for source in video.rglob("*.usm"):
        destination = raw / source.relative_to(video)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if source.stat().st_size != destination.stat().st_size or digest(source) != digest(destination):
                raise RuntimeError(f"refusing conflicting USM: {source} -> {destination}")
            source.unlink()
            duplicates += 1
        else:
            os.replace(source, destination)
            moved += 1

    for metadata in video.rglob(".DS_Store"):
        metadata.unlink()
    for directory in sorted((path for path in video.rglob("*") if path.is_dir()), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    print(f"video layout normalized: moved={moved}, identical_duplicates={duplicates}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
