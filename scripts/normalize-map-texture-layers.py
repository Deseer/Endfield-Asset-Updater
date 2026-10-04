#!/usr/bin/env python3
"""Select game-addressed map sprites and give auxiliary textures stable names."""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import uuid
from pathlib import Path


def map_texture_names(config_root: Path) -> set[str]:
    names: set[str] = set()
    for path in config_root.glob("*.json"):
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for lod in ("highChunks", "mediumChunks", "lowChunks"):
            chunks = config.get(lod)
            if isinstance(chunks, dict):
                names.update(name for name in chunks if isinstance(name, str))
    return names


def map_candidates(texture_root: Path, names: set[str]) -> dict[str, list[Path]]:
    result: dict[str, list[Path]] = {}
    for name in names:
        candidates = [texture_root / f"{name}.png"]
        # This directory has millions of files. Check only the known map tile
        # dimensions rather than globbing it once per masterdata entry.
        for role in ("detail", "overlay", "aux"):
            for index in range(1, 17):
                suffix = "" if index == 1 else f"_{index:02d}"
                candidate = texture_root / f"{name}_{role}_600x600{suffix}.png"
                if candidate.is_file():
                    candidates.append(candidate)
                else:
                    break
        for index in range(2, 17):
            candidate = texture_root / f"{name}_variant_{index:02d}.png"
            if candidate.is_file():
                candidates.append(candidate)
            else:
                break
        existing = list(dict.fromkeys(path for path in candidates if path.is_file()))
        if existing:
            result[name] = existing
    return result


def png_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"not a PNG: {path}")
    return struct.unpack(">II", header[16:24])


def provenance_records(root: Path) -> dict[str, list[dict]]:
    records: dict[str, list[dict]] = {}
    if not root.is_dir():
        return records
    for path in root.rglob("*.jsonl"):
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            name = record.get("name")
            if isinstance(name, str):
                records.setdefault(name, []).append(record)
    return records


def authoritative_source(texture_root: Path, records: list[dict]) -> Path | None:
    for record in records:
        paths = record.get("container_paths") or []
        if not any("/sprites/levelmap/" in path.lower() for path in paths if isinstance(path, str)):
            continue
        output = record.get("output")
        if isinstance(output, str):
            candidates = [texture_root / output, texture_root.parent / output]
            for candidate in candidates:
                if candidate.is_file():
                    return candidate
        path_id = record.get("path_id")
        name = record.get("name")
        if isinstance(path_id, str) and isinstance(name, str):
            candidate = texture_root / f"{name}_{path_id}.png"
            if candidate.is_file():
                return candidate
    return None


def provenance_sources(texture_root: Path, records: list[dict]) -> list[Path]:
    sources: list[Path] = []
    for record in records:
        output = record.get("output")
        if not isinstance(output, str):
            continue
        for candidate in (texture_root / output, texture_root.parent / output):
            if candidate.is_file():
                sources.append(candidate)
                break
    return list(dict.fromkeys(sources))


def destinations(
    texture_root: Path,
    name: str,
    sources: list[Path],
    preferred_source: Path | None = None,
) -> list[tuple[Path, Path]]:
    if len(sources) < 2:
        return []
    if preferred_source not in sources:
        return []
    # The visible terrain is the Texture2D imported from the authoritative
    # /sprites/levelmap/ AssetBundle container address.  File size, colour and
    # traversal order are deliberately not used: all have selected shader or
    # debug layers in real exports.
    ordered = [preferred_source, *sorted((path for path in sources if path != preferred_source), key=lambda path: path.name)]
    aux_counts: dict[tuple[int, int], int] = {}
    result = []
    for index, source in enumerate(ordered):
        if index == 0:
            target = texture_root / f"{name}.png"
        else:
            size = png_size(source)
            aux_counts[size] = aux_counts.get(size, 0) + 1
            suffix = f"_aux_{size[0]}x{size[1]}"
            if aux_counts[size] > 1:
                suffix += f"_{aux_counts[size]:02d}"
            target = texture_root / f"{name}{suffix}.png"
        result.append((source, target))
    return result


def apply(plan: list[tuple[Path, Path]]) -> None:
    token = uuid.uuid4().hex
    staged = []
    for index, (source, target) in enumerate(plan):
        temporary = source.with_name(f".{source.name}.map-normalize-{token}-{index}")
        os.replace(source, temporary)
        staged.append((temporary, target))
    for temporary, target in staged:
        os.replace(temporary, target)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("texture_root", type=Path)
    parser.add_argument("map_config_root", type=Path)
    parser.add_argument("--provenance-root", type=Path, action="append", required=True)
    parser.add_argument("--strict-unresolved", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()

    texture_root = args.texture_root.resolve()
    names = map_texture_names(args.map_config_root.resolve())
    provenance: dict[str, list[dict]] = {}
    for root in args.provenance_root:
        for name, records in provenance_records(root.resolve()).items():
            provenance.setdefault(name, []).extend(records)
    candidates = map_candidates(texture_root, names)
    for name in names:
        candidates.setdefault(name, [])
        candidates[name] = list(
            dict.fromkeys([*candidates[name], *provenance_sources(texture_root, provenance.get(name, []))])
        )
    preferred = {
        name: authoritative_source(texture_root, provenance.get(name, []))
        for name in names
    }
    plans = [
        destinations(texture_root, name, candidates.get(name, []), preferred.get(name))
        for name in sorted(names)
    ]
    planned = [pair for plan in plans for pair in plan]
    changes = [pair for pair in planned if pair[0] != pair[1]]
    unresolved = sum(len(candidates.get(name, [])) > 1 and preferred.get(name) is None for name in names)
    print(
        f"map_names={len(names)} authoritative_maps={sum(value is not None for value in preferred.values())} "
        f"layered_maps={sum(bool(plan) for plan in plans)} unresolved={unresolved} renames={len(changes)}"
    )
    if unresolved and args.strict_unresolved:
        raise SystemExit("refusing to guess layered map textures without AssetBundle provenance")
    if unresolved:
        print(
            f"warning: left {unresolved} layered map textures unchanged because provenance is unavailable",
            file=sys.stderr,
        )
    if args.manifest:
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.manifest.with_suffix(args.manifest.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                [
                    {
                        "source": str(source.relative_to(texture_root)),
                        "target": str(target.relative_to(texture_root)),
                    }
                    # Persist the complete canonical/auxiliary inventory, even
                    # when this pass is already converged. Per-unit cleanup uses
                    # these targets to avoid deleting normalized map outputs.
                    for source, target in planned
                ],
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, args.manifest)
    if args.apply:
        for plan in plans:
            if plan:
                apply(plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
