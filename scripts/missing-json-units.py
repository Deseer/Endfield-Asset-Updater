#!/usr/bin/env python3
"""Audit missing JsonData outputs and stale sizes in critical scene configs."""
from __future__ import annotations

import os
import sys
from pathlib import Path, PurePosixPath
from typing import Iterable


CRITICAL_CONFIG_ROOTS = (
    "Data/Json/GameplayConfig/",
    "Data/Json/LevelConfig/",
    "Data/Json/UILevelMapLoadConfig/",
    "Data/Json/MapConfig/",
)


def missing_units(lines: Iterable[str], raw_root: Path) -> set[str]:
    directories: dict[str, dict[str, str]] = {}
    missing: set[str] = set()
    checked = 0
    reported = 0
    for line in lines:
        fields = line.rstrip("\n").split("\t")
        if len(fields) != 4 or fields[0] != "JsonData":
            continue
        _, chunk, name, length = fields
        path = PurePosixPath(name.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe VFS path: {name}")
        parent = str(path.parent)
        if parent not in directories:
            try:
                with os.scandir(raw_root / parent) as entries:
                    # OrbStack shared mounts may stat every is_file() call.
                    # Only critical configs below incur per-file size checks.
                    directories[parent] = {p.name.casefold(): p.name for p in entries}
            except FileNotFoundError:
                directories[parent] = {}
        actual_name = directories[parent].get(path.name.casefold())
        needs_export = actual_name is None
        if not needs_export and path.as_posix().startswith(CRITICAL_CONFIG_ROOTS):
            expected_size = int(length)
            if expected_size < 0:
                raise ValueError(f"invalid VFS length: {length}")
            try:
                needs_export = (raw_root / path.parent / actual_name).stat().st_size != expected_size
            except FileNotFoundError:
                needs_export = True
        if needs_export:
            missing.add("JsonData-" + Path(chunk).stem)
            if reported < 20:
                reason = "missing" if actual_name is None else "size-mismatch"
                print(
                    f"JsonData output audit detail: chunk={chunk} reason={reason} path={path}",
                    file=sys.stderr,
                    flush=True,
                )
                reported += 1
        checked += 1
        if checked % 10000 == 0:
            print(f"JsonData output audit: checked={checked} missing_or_stale_chunks={len(missing)}", file=sys.stderr, flush=True)
    return missing


if __name__ == "__main__":
    units = missing_units(sys.stdin, Path(sys.argv[1]))
    for unit in sorted(units):
        print(unit)
    print(f"JsonData output audit: missing-or-stale-output chunks={len(units)}", file=sys.stderr)
