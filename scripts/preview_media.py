#!/usr/bin/env python3
"""Sequential, resumable playable media exports; originals stay in Raw."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path


def log(message):
    print(f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {message}", flush=True)


def atomic_json(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.replace(temporary, path)


def identical(a, b):
    if a.stat().st_size != b.stat().st_size:
        return False
    def digest(path):
        h = hashlib.sha256()
        with path.open("rb") as stream:
            for data in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(data)
        return h.digest()
    return digest(a) == digest(b)


def collision_archive(destination, archive_root, destination_root):
    relative = destination.relative_to(destination_root)
    archive = archive_root / relative
    archive.parent.mkdir(parents=True, exist_ok=True)
    if archive.exists():
        if identical(destination, archive):
            destination.unlink()
            return archive
        index = 2
        while True:
            candidate = archive.with_name(f"{archive.stem}_variant_{index:02d}{archive.suffix}")
            if not candidate.exists():
                archive = candidate
                break
            if identical(destination, candidate):
                destination.unlink()
                return candidate
            index += 1
    os.replace(destination, archive)
    return archive


def merge(source, destination, archive_root=None, destination_root=None):
    """Rename whole subtrees when possible; never overwrite conflicts."""
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.rename(source, destination)
    elif source.is_dir() and destination.is_dir():
        for child in source.iterdir():
            merge(child, destination / child.name, archive_root, destination_root)
        source.rmdir()
    elif source.is_file() and destination.is_file() and identical(source, destination):
        source.unlink()
    elif (
        source.is_file()
        and destination.is_file()
        and archive_root is not None
        and destination_root is not None
    ):
        archived = collision_archive(destination, archive_root, destination_root)
        os.replace(source, destination)
        log(f"video layout collision: archived old={archived} current={destination}")
    else:
        raise RuntimeError(f"layout collision, both preserved: {source} -> {destination}")


def rewrite_video_unit_outputs(root):
    manifests = root / ".baseline/export-units"
    if not manifests.is_dir():
        return 0
    prefix = "Video/Data/Video/PC/"
    changed = 0
    for path in manifests.glob("*.json"):
        try:
            value = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        outputs = value.get("outputs")
        if not isinstance(outputs, list):
            continue
        rewritten = ["Video/" + item[len(prefix):] if isinstance(item, str) and item.startswith(prefix) else item for item in outputs]
        if rewritten == outputs:
            continue
        value["outputs"] = rewritten
        sizes = value.get("output_sizes")
        if isinstance(sizes, dict):
            value["output_sizes"] = {
                ("Video/" + key[len(prefix):] if key.startswith(prefix) else key): size
                for key, size in sizes.items()
            }
        atomic_json(path, value)
        changed += 1
    return changed


def video_layout(root):
    legacy = root / "Video/Data/Video/PC"
    if not legacy.is_dir():
        rewrite_video_unit_outputs(root)
        return
    version = os.environ.get("ZMD_VERSION", "unknown")
    archive_root = root / "Raw/VideoHistory" / f"before-{version}"
    with (root / "media-layout-moves.jsonl").open("a") as journal:
        for source in list(legacy.iterdir()):
            if source.name == ".DS_Store":
                source.unlink()
                continue
            destination = root / "Video" / source.name
            journal.write(json.dumps({"source": str(source.relative_to(root)),
                                      "destination": str(destination.relative_to(root))}) + "\n")
            journal.flush()
            merge(source, destination, archive_root, root / "Video")
            log(f"video layout: {source.relative_to(root)} -> {destination.relative_to(root)}")
    for directory in (legacy, legacy.parent, legacy.parent.parent):
        (directory / ".DS_Store").unlink(missing_ok=True)
        try:
            directory.rmdir()
        except OSError:
            pass
    rewritten = rewrite_video_unit_outputs(root)
    if rewritten:
        log(f"video layout: rewrote unit manifests={rewritten}")


def probe(path):
    result = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries",
        "stream=codec_name,codec_type,pix_fmt,width,height:format=duration",
        "-of", "json", str(path)], check=True, capture_output=True, text=True, timeout=30)
    return json.loads(result.stdout)


def streams(info, kind):
    return [s for s in info.get("streams", []) if s.get("codec_type") == kind]


def compatible_video(info):
    videos = streams(info, "video")
    return (bool(videos) and all(v.get("codec_name") == "h264" and
                                v.get("pix_fmt") == "yuv420p" for v in videos)
            and all(a.get("codec_name") == "aac" for a in streams(info, "audio")))


def checked_run(command, timeout=3600):
    # Keep external-tool stderr bounded in RAM even for malformed inputs.
    with tempfile.TemporaryFile() as error:
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=error, timeout=timeout)
        if result.returncode:
            error.seek(max(0, error.tell() - 2000))
            raise RuntimeError(error.read().decode(errors="replace"))


def validate_decode(path):
    checked_run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-threads", "1",
                 "-i", str(path), "-map", "0:v?", "-map", "0:a?", "-threads", "1",
                 "-f", "null", "-"], timeout=3600)


def transcode_video(source):
    before = probe(source)
    if not streams(before, "video"):
        raise RuntimeError("no video stream")
    temporary = source.with_name(f".{source.stem}.preview-tmp.mp4")
    try:
        checked_run([
            "ffmpeg", "-nostdin", "-y", "-v", "error", "-threads", "1",
            "-filter_threads", "1", "-i", str(source), "-map", "0:v:0", "-map", "0:a?",
            "-c:v", "libx264", "-threads", "1", "-preset", "fast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags",
            "+faststart", str(temporary)])
        after = probe(temporary)
        if not compatible_video(after) or len(streams(after, "audio")) != len(streams(before, "audio")):
            raise RuntimeError("output codec/track validation failed")
        old_duration = float(before.get("format", {}).get("duration", 0))
        new_duration = float(after.get("format", {}).get("duration", 0))
        if old_duration and abs(old_duration - new_duration) > max(0.25, old_duration * 0.01):
            raise RuntimeError("output duration differs from input")
        validate_decode(temporary)
        os.replace(temporary, source)
    finally:
        temporary.unlink(missing_ok=True)


def transcode_audio(source, destination):
    temporary = destination.with_name(f".{destination.stem}.preview-tmp.mp3")
    try:
        with tempfile.TemporaryFile() as decode_error, tempfile.TemporaryFile() as encode_error:
            decoder = subprocess.Popen(["vgmstream-cli", "-i", "-p", str(source)],
                                       stdout=subprocess.PIPE, stderr=decode_error)
            encoder = None
            try:
                encoder = subprocess.Popen([
                    "ffmpeg", "-nostdin", "-y", "-v", "error", "-threads", "1",
                    "-i", "pipe:0", "-map", "0:a:0", "-c:a", "libmp3lame", "-q:a", "2",
                    "-threads", "1", str(temporary)], stdin=decoder.stdout,
                    stdout=subprocess.DEVNULL, stderr=encode_error)
                decoder.stdout.close()
                encoder.wait(timeout=600)
                decoder.wait(timeout=30)
                if decoder.returncode or encoder.returncode:
                    messages = []
                    for error in (decode_error, encode_error):
                        error.seek(max(0, error.tell() - 1000))
                        messages.append(error.read().decode(errors="replace"))
                    raise RuntimeError("; ".join(messages))
            finally:
                for process in (encoder, decoder):
                    if process is not None and process.poll() is None:
                        process.kill()
                        process.wait()
        if not any(s.get("codec_name") == "mp3" for s in streams(probe(temporary), "audio")):
            raise RuntimeError("output is not MP3 audio")
        validate_decode(temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def preserve_audio_source(root, base, source):
    relative = source.relative_to(base)
    preserved = root / "Raw/AudioWEM" / relative
    if preserved.is_file() and identical(source, preserved):
        source.unlink()
        return
    if preserved.exists():
        version = "".join(c for c in os.environ.get("ZMD_VERSION", "unknown")
                          if c.isalnum() or c in ".-_") or "unknown"
        history = root / "Raw/AudioWEMHistory" / f"before-{version}" / relative
        if history.exists():
            if not identical(preserved, history):
                raise RuntimeError(f"audio history collision: {history}")
            preserved.unlink()
        else:
            history.parent.mkdir(parents=True, exist_ok=True)
            os.replace(preserved, history)
    preserved.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, preserved)


def process(root, kinds=("video", "audio"), limit=0):
    video_layout(root)
    report_path = root / "media-preview-report.json"
    report = json.loads(report_path.read_text()) if report_path.exists() else {}
    for kind in kinds:
        base, suffix = (root / "Video", ".mp4") if kind == "video" else (root / "Audio", ".wem")
        log(f"preview {kind} scanning {base}")
        files = [p for p in base.rglob(f"*{suffix}") if not p.name.startswith(".")]
        files.sort(key=lambda p: ("unmapped" in p.parts, str(p)))
        if limit:
            files = files[:limit]
        counts = {"total": len(files), "processed": 0, "converted": 0, "skipped": 0, "errors": []}
        report[kind] = counts
        log(f"preview {kind} scan: files={len(files)} single-worker threads=1")
        start = time.monotonic()
        for index, source in enumerate(files, 1):
            relative = str(source.relative_to(root))
            log(f"preview {kind} start {index}/{len(files)} file={relative}")
            try:
                if kind == "video":
                    if compatible_video(probe(source)):
                        counts["skipped"] += 1
                    else:
                        transcode_video(source)
                        counts["converted"] += 1
                else:
                    destination = source.with_suffix(".mp3")
                    preserved = root / "Raw/AudioWEM" / source.relative_to(base)
                    unchanged = preserved.is_file() and identical(source, preserved)
                    if unchanged and destination.exists():
                        validate_decode(destination)
                        if not any(s.get("codec_name") == "mp3" for s in streams(probe(destination), "audio")):
                            raise RuntimeError("existing MP3 invalid; preserving WEM")
                        counts["skipped"] += 1
                    else:
                        transcode_audio(source, destination)
                        counts["converted"] += 1
                    # Preserve each original, including on interrupted/retried jobs.
                    preserve_audio_source(root, base, source)
            except Exception as exc:
                counts["errors"].append({"file": relative, "error": str(exc)[:2000]})
                log(f"preview {kind} ERROR skipped file={relative} error={str(exc)[:1000]}")
            counts["processed"] = index
            report["updated_at"] = datetime.now().astimezone().isoformat()
            if index % 25 == 0 or index == len(files):
                atomic_json(report_path, report)
            log(f"preview {kind} progress={index}/{len(files)} converted={counts['converted']} "
                f"skipped={counts['skipped']} errors={len(counts['errors'])} elapsed={time.monotonic()-start:.0f}s")
        atomic_json(report_path, report)
    return sum(len(report.get(k, {}).get("errors", [])) for k in ("video", "audio"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=("video", "audio", "all"), default="all")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--layout-only", action="store_true")
    args = parser.parse_args()
    root = Path("/data")
    if args.layout_only:
        video_layout(root)
        return 0
    process(root, ("video", "audio") if args.kind == "all" else (args.kind,), args.limit)
    # Individual failures are retained in the report, not allowed to stop other files.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
