#!/bin/bash
set -euo pipefail

if [ -z "${ZMD_VERSION:-}" ]; then
  echo "ZMD_VERSION is required" >&2
  exit 2
fi

mkdir -p /data/.work/State
mkdir -p /data/.work/Logs
exec 9>/data/.work/State/export.lock
if ! flock -n 9; then
  echo "another ZMD export is already running; wait for it to finish" >&2
  exit 3
fi

version_root="/data/.work/VFS/${ZMD_VERSION}"
streaming_assets="$(find "$version_root" -type d -name StreamingAssets -print -quit)"
if [ -z "$streaming_assets" ] || [ ! -d "$streaming_assets/VFS" ]; then
  echo "StreamingAssets/VFS was not found below $version_root" >&2
  exit 2
fi

out="/data"
scratch="/data/.work/Scratch/${ZMD_VERSION}"
stages=" ${ZMD_EXPORT_STAGES:-masterdata textures audio video raw} "
mkdir -p "$out" "$scratch"

run_logged() {
  stage="$1"
  shift
  log="/data/.work/Logs/${stage}-${ZMD_VERSION}.log"
  {
    echo "[$(date -Iseconds)] starting ${stage}"
    "$@"
    echo "[$(date -Iseconds)] completed ${stage}"
  } 2>&1 | tee -a "$log"
}

has_stage() {
  case "$stages" in
    *" $1 "*) return 0 ;;
    *) return 1 ;;
  esac
}

# Keep small and immediately useful outputs ahead of the very large Raw dump.
if has_stage masterdata; then
  mkdir -p "$out/MasterData"
  run_logged masterdata ionice -c 3 nice -n 19 endfield-dump dump \
    --vfs "$streaming_assets" \
    --out "$out/MasterData" \
    --block Table \
    --threads "${ZMD_THREADS:-1}"
fi

# Export every Texture2D, including material/PBR textures. Both initial and
# current bundle layers are selected.
if has_stage textures; then
  mkdir -p "$out/Textures"
  run_logged textures ionice -c 3 nice -n 19 endfield-dump extract \
    --vfs "$streaming_assets" \
    --out "$out/Textures" \
    --block InitialBundle \
    --block Bundle \
    --threads "${ZMD_THREADS:-1}" \
    --scratch "$scratch/textures" \
    --format png \
    --png-compression fast \
    --max-memory-gb "${ZMD_MAX_MEMORY_GB:-4}" \
    --classify \
    --skip-missing
fi

# Preserve original audio streams without lossy transcoding.
if has_stage audio; then
  mkdir -p "$out/Audio"
  run_logged audio ionice -c 3 nice -n 19 endfield-dump audio \
    --vfs "$streaming_assets" \
    --out "$out/Audio" \
    --language all \
    --block all \
    --format wem \
    --threads "${ZMD_THREADS:-1}"
fi

if has_stage video; then
  mkdir -p "$out/Video"
  run_logged video ionice -c 3 nice -n 19 endfield-dump video \
    --vfs "$streaming_assets" \
    --out "$out/Video" \
    --block all \
    --format mp4 \
    --threads "${ZMD_THREADS:-1}"
fi

# Raw is intentionally last. It preserves every logical VFS file, including
# bundle content that the current CLI cannot convert to a friendly format.
if has_stage raw; then
  mkdir -p "$out/Raw"
  run_logged raw ionice -c 3 nice -n 19 endfield-dump dump \
    --vfs "$streaming_assets" \
    --out "$out/Raw" \
    --threads "${ZMD_THREADS:-1}"
fi
