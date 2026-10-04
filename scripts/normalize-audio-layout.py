#!/usr/bin/env python3
"""Merge AudioDialog v1dN shard folders into one folder per language."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def is_voice_shard(path: Path) -> bool:
    name = path.name.lower()
    return name.startswith("v1d") and len(name) > 3 and name[3:].isdigit()


def merge_shard(shard: Path, language_root: Path) -> tuple[int, int]:
    moved = 0
    duplicates = 0
    for source_root, _, files in os.walk(shard):
        source_dir = Path(source_root)
        relative_dir = source_dir.relative_to(shard)
        destination_dir = language_root / relative_dir
        destination_dir.mkdir(parents=True, exist_ok=True)

        for name in files:
            source = source_dir / name
            if name == ".DS_Store":
                source.unlink()
                continue
            destination = destination_dir / name
            if destination.exists():
                if source.stat().st_size != destination.stat().st_size:
                    raise RuntimeError(
                        f"audio layout collision with different sizes: {source} -> {destination}"
                    )
                source.unlink()
                duplicates += 1
                continue

            os.replace(source, destination)
            moved += 1

    for source_root, dirs, _ in os.walk(shard, topdown=False):
        for name in dirs:
            (Path(source_root) / name).rmdir()
    shard.rmdir()
    return moved, duplicates


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio_root", type=Path)
    args = parser.parse_args()

    voice_root = args.audio_root / "voice"
    if not voice_root.is_dir():
        raise SystemExit(f"voice directory not found: {voice_root}")

    total_moved = 0
    total_duplicates = 0

    # A single directory with tens of thousands of WEMs can monopolize the
    # macOS/OrbStack bind-mount metadata path. Spread legacy unmapped files over
    # 256 stable ID shards before any export is resumed.
    unmapped_root = args.audio_root / "unmapped"
    if unmapped_root.is_dir():
        for source in unmapped_root.iterdir():
            if not source.is_file() or source.suffix.lower() != ".wem":
                continue
            try:
                source_id = int(source.stem)
            except ValueError:
                continue
            destination_dir = unmapped_root / f"{source_id & 0xff:02x}"
            destination_dir.mkdir(exist_ok=True)
            destination = destination_dir / source.name
            if destination.exists():
                if source.stat().st_size != destination.stat().st_size:
                    raise RuntimeError(
                        f"unmapped audio collision with different sizes: {source} -> {destination}"
                    )
                source.unlink()
                total_duplicates += 1
            else:
                os.replace(source, destination)
                total_moved += 1

    for language_root in sorted(path for path in voice_root.iterdir() if path.is_dir()):
        shards = sorted(path for path in language_root.iterdir() if path.is_dir() and is_voice_shard(path))
        for shard in shards:
            moved, duplicates = merge_shard(shard, language_root)
            total_moved += moved
            total_duplicates += duplicates
            print(f"merged {shard}: moved={moved}, identical_duplicates={duplicates}")

    print(f"audio layout normalized: moved={total_moved}, identical_duplicates={total_duplicates}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
