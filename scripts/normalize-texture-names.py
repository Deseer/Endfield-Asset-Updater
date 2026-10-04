#!/usr/bin/env python3
"""Remove exporter PathIDs from visible texture names without dropping variants."""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PATH_ID = re.compile(r"^(?P<stem>.+)_(?P<id>[0-9a-fA-F]{16})\.png$")
CHARACTER = re.compile(r"^(?P<key>chr_\d+_[a-z0-9]+)(?:_.+)?\.png$", re.IGNORECASE)


def png_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"not a PNG: {path}")
    return struct.unpack(">II", header[16:24])


def discover(root: Path, candidate_list: Path | None = None):
    groups: dict[Path, list[tuple[Path, str]]] = defaultdict(list)
    if candidate_list:
        candidates = (
            root.parent / line.strip()
            for line in candidate_list.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    else:
        candidates = root.rglob("*.png")
    for path in candidates:
        if not path.is_file():
            continue
        match = PATH_ID.match(path.name)
        if not match:
            continue
        base = path.with_name(match.group("stem") + ".png")
        groups[base].append((path, match.group("id").lower()))
    # Character exports from older runs may already have lost their PathID and
    # use a mixture of base/large/variant/dimension suffixes.  Fold those files
    # back into one semantic group so a rerun converges on the same names.
    character_root = root / "chr_thumb"
    if character_root.is_dir():
        for path in character_root.iterdir():
            if not path.is_file() or PATH_ID.match(path.name):
                continue
            match = CHARACTER.match(path.name)
            if not match:
                continue
            base = path.with_name(match.group("key") + ".png")
            if path == base:
                groups.setdefault(base, [])
            else:
                groups[base].append((path, None))
    return groups


def recover_interrupted(root: Path, candidate_list: Path | None) -> int:
    if candidate_list:
        directories = {
            (root.parent / line.strip()).parent
            for line in candidate_list.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        temporary_paths = (
            temporary
            for directory in directories
            if directory.is_dir()
            for temporary in directory.glob(".*.normalize-*")
        )
    else:
        temporary_paths = root.rglob(".*.normalize-*")
    recovered = 0
    for temporary in temporary_paths:
        marker = temporary.name.find(".normalize-", 1)
        if marker < 0:
            continue
        original = temporary.with_name(temporary.name[1:marker])
        if not original.exists():
            os.replace(temporary, original)
            recovered += 1
    return recovered


def destinations(base: Path, suffixed: list[tuple[Path, str]]):
    records = [(base, None, *png_size(base))] if base.is_file() else []
    records.extend((path, path_id, *png_size(path)) for path, path_id in suffixed)
    records.sort(key=lambda row: (-(row[2] * row[3]), row[1] or ""))
    largest_pixels = records[0][2] * records[0][3]
    largest_index = next(
        (idx for idx, row in enumerate(records) if row[0] == base and row[2] * row[3] == largest_pixels),
        0,
    )
    if base.parent.name == "chr_thumb" and base.stem.startswith("chr_"):
        labels = {
            (312, 552): "portrait",
            (312, 312): "s",
            # Normalize every full illustration to the same public suffix,
            # including the two Endministrators whose game asset is bare.
            (2048, 2048): "large",
            (900, 352): "banner",
            (236, 1352): "strip",
            (304, 512): "304x512",
        }
        result = []
        counts: dict[str, int] = defaultdict(int)
        for source, path_id, width, height in sorted(records, key=lambda row: (labels.get((row[2], row[3]), "other"), str(row[0]))):
            label = labels.get((width, height), f"{width}x{height}")
            counts[label] += 1
            suffix = "" if counts[label] == 1 else f"_{counts[label]:02d}"
            target = (
                base.with_name(f"{base.stem}{suffix}.png")
                if label == "portrait"
                else base.with_name(f"{base.stem}_{label}{suffix}.png")
            )
            result.append((source, target, path_id, width, height))
        return result
    primary = records.pop(largest_index)
    ordered = [primary, *records]
    width_counts: dict[tuple[int, int], int] = defaultdict(int)
    result = []
    for idx, (source, path_id, width, height) in enumerate(ordered):
        if idx == 0:
            target = base
        elif width * height == largest_pixels:
            width_counts[(width, height)] += 1
            target = base.with_name(f"{base.stem}_variant_{width_counts[(width, height)] + 1:02d}.png")
        else:
            width_counts[(width, height)] += 1
            suffix = f"_thumb_{width}x{height}"
            if width_counts[(width, height)] > 1:
                suffix += f"_variant_{width_counts[(width, height)]:02d}"
            target = base.with_name(base.stem + suffix + ".png")
        result.append((source, target, path_id, width, height))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--map", type=Path)
    parser.add_argument("--changed-list", type=Path)
    parser.add_argument("--candidate-list", type=Path)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()

    root = args.root.resolve()
    recovered = recover_interrupted(root, args.candidate_list)
    if recovered:
        print(f"recovered_interrupted={recovered}")
    groups = discover(root, args.candidate_list)
    grouped = sorted(groups.items())
    # PNG headers are tiny; bounded parallel reads hide external-disk metadata
    # latency without retaining image pixels or increasing memory materially.
    with ThreadPoolExecutor(max_workers=8) as pool:
        plans = list(pool.map(lambda item: destinations(*item), grouped))
    changes = [item for plan in plans for item in plan if item[0] != item[1]]
    print(f"groups={len(groups)} files={sum(len(p) for p in plans)} renames={len(changes)}")
    if args.manifest:
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.manifest.with_suffix(args.manifest.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                [
                    {
                        "source": str(source.relative_to(root)),
                        "target": str(target.relative_to(root)),
                    }
                    for source, target, *_ in changes
                ],
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, args.manifest)
    if args.changed_list:
        args.changed_list.parent.mkdir(parents=True, exist_ok=True)
        args.changed_list.write_text(
            "".join(str(source.relative_to(root.parent)) + "\n" for source, *_ in changes),
            encoding="utf-8",
        )
    if not args.apply:
        return 0

    mapping: dict[str, str] = {}
    for plan in plans:
        token = uuid.uuid4().hex
        staged = []
        for index, (source, target, path_id, width, height) in enumerate(plan):
            temporary = source.with_name(f".{source.name}.normalize-{token}-{index}")
            os.replace(source, temporary)
            staged.append((temporary, target, path_id, width, height))
        for temporary, target, path_id, width, height in staged:
            os.replace(temporary, target)
            if path_id:
                mapping[path_id] = str(target.relative_to(root))

    if args.map:
        args.map.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.map.with_suffix(args.map.suffix + ".tmp")
        temporary.write_text(json.dumps(mapping, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, args.map)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
