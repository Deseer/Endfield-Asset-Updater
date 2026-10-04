#!/usr/bin/env python3
"""Atomically convert exported MP4 video to broadly playable H.264/AAC."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def probe(path: Path) -> tuple[str, list[str]]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name,codec_type",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    streams = json.loads(result.stdout).get("streams", [])
    video = next(
        (stream.get("codec_name", "") for stream in streams if stream.get("codec_type") == "video"),
        "",
    )
    audio = [
        stream.get("codec_name", "")
        for stream in streams
        if stream.get("codec_type") == "audio"
    ]
    return video, audio


def transcode(path: Path) -> None:
    temporary = path.with_name(f".{path.name}.h264-tmp.mp4")
    temporary.unlink(missing_ok=True)
    command = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(temporary),
    ]
    try:
        subprocess.run(command, check=True)
        video, audio = probe(temporary)
        if video != "h264" or any(codec != "aac" for codec in audio):
            raise RuntimeError(f"unexpected output codecs video={video} audio={audio}")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "/data/Video").resolve()
    if root != Path("/data/Video"):
        raise SystemExit(f"refusing unexpected video root: {root}")
    files = sorted(root.rglob("*.mp4"))
    converted = skipped = errors = 0
    print(f"video compatibility scan: files={len(files)}", flush=True)
    for index, path in enumerate(files, 1):
        try:
            video, audio = probe(path)
            compatible = video == "h264" and all(codec == "aac" for codec in audio)
            if compatible:
                skipped += 1
            else:
                transcode(path)
                converted += 1
        except Exception as exc:
            errors += 1
            print(f"video compatibility error: file={path} error={exc}", file=sys.stderr, flush=True)
        if index % 10 == 0 or index == len(files):
            print(
                f"video compatibility progress: {index}/{len(files)} "
                f"converted={converted} skipped={skipped} errors={errors}",
                flush=True,
            )
    print(
        f"video compatibility complete: converted={converted} skipped={skipped} errors={errors}",
        flush=True,
    )
    # A bad derived video must not block the rest of the export/final cleanup.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
