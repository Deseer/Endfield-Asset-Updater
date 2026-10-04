from __future__ import annotations

import hashlib
import os
import shutil
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from .manifest import pack_filename
from .privacy import redact_text

Progress = Callable[[str], None]


def md5_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _download_one(
    pack: dict[str, Any], destination: Path, progress: Progress, retries: int = 4
) -> Path:
    filename = pack_filename(pack)
    final_path = destination / filename
    partial_path = destination / f"{filename}.part"
    expected_size = int(pack["package_size"])
    expected_md5 = str(pack["md5"]).lower()

    if final_path.exists():
        if final_path.stat().st_size == expected_size and md5_file(final_path) == expected_md5:
            progress(f"verified existing {filename}")
            return final_path
        raise RuntimeError(f"existing package failed verification: {final_path}")

    for attempt in range(1, retries + 1):
        offset = partial_path.stat().st_size if partial_path.exists() else 0
        if offset > expected_size:
            raise RuntimeError(f"partial package is larger than manifest size: {partial_path}")
        if offset == expected_size and offset > 0:
            if md5_file(partial_path) == expected_md5:
                os.replace(partial_path, final_path)
                progress(f"recovered complete partial {filename}")
                return final_path
            partial_path.unlink()
            offset = 0
        headers = {"User-Agent": "endfield-resource-service/0.1"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        request = urllib.request.Request(pack["url"], headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                status = getattr(response, "status", 200)
                if offset and status != 206:
                    offset = 0
                    partial_path.unlink(missing_ok=True)
                mode = "ab" if offset else "wb"
                with partial_path.open(mode) as stream:
                    shutil.copyfileobj(response, stream, length=8 * 1024 * 1024)
                    stream.flush()
                    os.fsync(stream.fileno())
            actual_size = partial_path.stat().st_size
            if actual_size != expected_size:
                raise RuntimeError(
                    f"size mismatch for {filename}: {actual_size} != {expected_size}"
                )
            actual_md5 = md5_file(partial_path)
            if actual_md5 != expected_md5:
                raise RuntimeError(
                    f"MD5 mismatch for {filename}: {actual_md5} != {expected_md5}"
                )
            os.replace(partial_path, final_path)
            progress(f"downloaded and verified {filename}")
            return final_path
        except (OSError, urllib.error.URLError, RuntimeError) as error:
            if attempt == retries:
                raise RuntimeError(f"failed {filename} after {attempt} attempts: {redact_text(str(error))}") from error
            progress(f"retrying {filename} ({attempt}/{retries}): {redact_text(str(error))}")
            time.sleep(min(2**attempt, 10))
    raise AssertionError("unreachable")


def download_packs(
    packs: list[dict[str, Any]], destination: Path, workers: int, progress: Progress = print
) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    results: list[Path] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_download_one, pack, destination, progress): pack for pack in packs
        }
        for future in as_completed(futures):
            results.append(future.result())
    return sorted(results)
