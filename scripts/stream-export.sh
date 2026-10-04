#!/bin/bash
set -euo pipefail

if [[ -z "${ZMD_VERSION:-}" ]]; then
  echo "ZMD_VERSION is required" >&2
  exit 2
fi

work="/data/.work"
state="$work/State/stream-export-${ZMD_VERSION}"
logs="$work/Logs"
scratch="$work/Scratch/${ZMD_VERSION}"
version_root="$work/VFS/${ZMD_VERSION}"
unit_baseline="/data/.baseline/export-units"
unit_state_tool="/app/scripts/export-unit-state.py"
streaming_assets="$(find "$version_root" -type d -name StreamingAssets -print -quit)"
preserve_vfs="${ENDFIELD_PRESERVE_VFS:-1}"
export_threads="${ENDFIELD_EXPORT_THREADS:-6}"
if [[ -z "$streaming_assets" || ! -d "$streaming_assets/VFS" ]]; then
  echo "StreamingAssets/VFS was not found below $version_root" >&2
  exit 2
fi

mkdir -p "$state/completed" "$logs" "$scratch" "$unit_baseline"
exec 9>"$work/State/export.lock"
if ! flock -n 9; then
  echo "another ZMD export is already running" >&2
  exit 3
fi

log="$logs/stream-export-${ZMD_VERSION}.log"
exec > >(tee -a "$log") 2>&1
echo "[$(date -Iseconds)] stream export starting"

declare -A cdn_changed_paths=()
declare -A force_reexport_units=()
declare -A baseline_logical_names=()
declare -A source_fingerprints=()
force_reexport_file="$work/State/force-reexport-units.txt"
normalization_needed="$state/normalization-needed"
media_changed="$state/media-changed"
if [[ -f "$force_reexport_file" ]]; then
  while IFS= read -r forced_unit; do
    [[ -n "$forced_unit" ]] && force_reexport_units["$forced_unit"]=1
  done < "$force_reexport_file"
  echo "[$(date -Iseconds)] forced texture re-export units=${#force_reexport_units[@]}"
fi
if [[ -f /data/export-manifest.json ]]; then
  resource_plan="$(python3 "$unit_state_tool" plan-path "$work/State" "$ZMD_VERSION")"
  while IFS= read -r changed_path; do
    [[ -n "$changed_path" ]] && cdn_changed_paths["$changed_path"]=1
  done < <(python3 "$unit_state_tool" changed-paths "$resource_plan")
  echo "[$(date -Iseconds)] CDN delta loaded changed_chunks=${#cdn_changed_paths[@]} masterdata=refresh"
  while IFS=$'\t' read -r baseline_key baseline_logical_name; do
    [[ -n "$baseline_key" && -n "$baseline_logical_name" ]] \
      && baseline_logical_names["$baseline_key"]="$baseline_logical_name"
  done < <(python3 "$unit_state_tool" manifest-logical-names "$unit_baseline")
  echo "[$(date -Iseconds)] unit baseline loaded units=${#baseline_logical_names[@]}"
fi

set_phase() {
  block="$1"
  chunk="$2"
  phase="$3"
  current_tmp="$state/current.tsv.tmp"
  printf '%s\t%s\t%s\t%s\n' "$(date -Iseconds)" "$block" "$chunk" "$phase" > "$current_tmp"
  mv "$current_tmp" "$state/current.tsv"
  echo "[$(date -Iseconds)] phase block=$block chunk=$chunk stage=$phase"
}

units="$state/units.tsv"
# Refresh ordering on resume as well; per-unit markers still prevent repeats.
# JsonData contains LevelConfig/LevelData, not just optional raw files.
tmp="$units.tmp"
endfield-dump list --vfs "$streaming_assets" --list-chunks \
  | awk -F '\t' 'NF == 4 {
      priority = 20
      if ($1 == "Table") priority = 0
      else if ($1 == "JsonData") priority = 1
      else if ($1 ~ /Bundle/) priority = 10
      else if ($1 ~ /Video/) priority = 30
      else if ($1 ~ /Audio/) priority = 40
      print priority "\t" $0
    }' \
  | sort -n -k1,1 \
  | cut -f2- > "$tmp"
mv "$tmp" "$units"

# The encrypted .blc indexes carry a data MD5 for every internal file. Hashing
# the sorted path/length/data-MD5 rows lets repacked-but-identical chunks skip
# before extracting tens of thousands of small files onto the shared HDD mount.
source_identities="$state/source-identities.tsv"
source_tmp="$source_identities.tmp"
endfield-dump list --vfs "$streaming_assets" --list-chunk-identities > "$source_tmp"
mv "$source_tmp" "$source_identities"
while IFS=$'\t' read -r source_block source_chunk source_md5 source_count source_bytes; do
  [[ -n "$source_block" && -n "$source_chunk" && -n "$source_md5" ]] || continue
  source_key="${source_block}-${source_chunk%.chk}"
  source_fingerprints["$source_key"]="${source_md5,,}:${source_count}:${source_bytes}"
done < "$source_identities"
echo "[$(date -Iseconds)] VFS content identities loaded units=${#source_fingerprints[@]}"

missing_json_units="$state/missing-json-units.txt"
json_audit_marker=/data/.baseline/json-output-audit-v1
# Historical exports predate per-unit ownership. Run this directory audit once;
# later JsonData additions are covered by the CDN unit identity plan.
if [[ ! -f "$json_audit_marker" ]]; then
  endfield-dump list --vfs "$streaming_assets" --block JsonData --list-file-chunks \
    | python3 /app/scripts/missing-json-units.py /data/Raw > "$missing_json_units"
else
  : > "$missing_json_units"
  echo "[$(date -Iseconds)] JsonData historical output audit already complete"
fi
while IFS= read -r missing_unit; do
  [[ -n "$missing_unit" ]] && force_reexport_units["$missing_unit"]=1
done < "$missing_json_units"

commit_tree() {
  source_root="$1"
  destination_root="$2"
  category="$3"
  [[ -d "$source_root" ]] || return 0
  committed=0
  last_directory=""
  while IFS= read -r -d '' source; do
    relative="${source#"$source_root"/}"
    destination="$destination_root/$relative"
    destination_directory="${destination%/*}"
    if [[ "$destination_directory" != "$last_directory" ]]; then
      mkdir -p "$destination_directory"
      last_directory="$destination_directory"
    fi
    # The source is already a complete file in same-volume staging. A direct
    # rename is atomic and avoids the previous extra temporary rename.
    mv -f "$source" "$destination"
    committed=$((committed + 1))
    if (( committed % 1000 == 0 )); then
      echo "[$(date -Iseconds)] commit progress category=$category files=$committed"
    fi
  done < <(find "$source_root" -type f -print0)
  echo "[$(date -Iseconds)] commit complete category=$category files=$committed"
}

build_changed_raw_list() {
  source_root="$1"
  changed_list="$2"
  echo "[$(date -Iseconds)] phase stage=raw-compare"
  python3 "$unit_state_tool" changed-list "$source_root" /data/Raw "$changed_list" >/dev/null
}

write_marker() {
  marker="$1"
  block="$2"
  chunk="$3"
  marker_tmp="$marker.tmp"
  printf '%s\t%s\t%s\n' "$(date -Iseconds)" "$block" "$chunk" > "$marker_tmp"
  mv "$marker_tmp" "$marker"
}

process_unit() {
  block="$1"
  chunk="$2"
  key="${block}-${chunk%.chk}"
  marker="$state/completed/$key"
  unit_manifest="$unit_baseline/$key.json"
  source_fingerprint="${source_fingerprints[$key]-}"
  chunk_path="$(find "$streaming_assets/VFS" -type f -name "$chunk" -print -quit)"

  if [[ -f "$marker" && -z "${force_reexport_units[$key]+present}" ]]; then
    if [[ "$preserve_vfs" != "1" && -n "$chunk_path" ]]; then rm -f "$chunk_path"; fi
    return 0
  fi
  if [[ -z "$chunk_path" ]]; then
    baseline_logical_name="${baseline_logical_names[$key]-}"
    if [[ -f /data/export-manifest.json \
        && -z "${force_reexport_units[$key]+present}" \
        && -n "$baseline_logical_name" \
        && -z "${cdn_changed_paths[$baseline_logical_name]+present}" ]]; then
      write_marker "$marker" "$block" "$chunk"
      completed_count="$(find "$state/completed" -maxdepth 1 -type f | wc -l | tr -d ' ')"
      total_count="$(wc -l < "$units" | tr -d ' ')"
      echo "[$(date -Iseconds)] unit CDN-unchanged source-free reuse block=$block chunk=$chunk progress=$completed_count/$total_count"
      return 0
    fi
    echo "missing unprocessed chunk: $block $chunk" >&2
    return 4
  fi

  logical_name="${chunk_path#"$streaming_assets/VFS"/}"
  unit="$scratch/units/$key"
  pck_scratch="$scratch/pck/$key"
  bundle_scratch="$scratch/bundles/$key"
  if [[ -f /data/export-manifest.json && "$block" != "Table" \
      && -z "${force_reexport_units[$key]+present}" \
      && -z "${cdn_changed_paths[$logical_name]+present}" ]]; then
    write_marker "$marker" "$block" "$chunk"
    if [[ "$preserve_vfs" != "1" ]]; then rm -f "$chunk_path"; fi
    rm -rf "$unit" "$pck_scratch" "$bundle_scratch"
    completed_count="$(find "$state/completed" -maxdepth 1 -type f | wc -l | tr -d ' ')"
    total_count="$(wc -l < "$units" | tr -d ' ')"
    echo "[$(date -Iseconds)] unit CDN-unchanged reuse block=$block chunk=$chunk progress=$completed_count/$total_count"
    return 0
  fi
  identity="$(python3 "$unit_state_tool" identity "$work/State" "$ZMD_VERSION" "$logical_name")"
  if [[ -z "${force_reexport_units[$key]+present}" \
      && -n "$source_fingerprint" ]] && python3 "$unit_state_tool" reuse-source-fingerprint \
      "$unit_baseline" "$unit_manifest" "$source_fingerprint" \
      "$ZMD_VERSION" "$logical_name" "$identity"; then
    write_marker "$marker" "$block" "$chunk"
    if [[ "$preserve_vfs" != "1" ]]; then rm -f "$chunk_path"; fi
    rm -rf "$scratch/units/$key" "$scratch/pck/$key" "$scratch/bundles/$key"
    completed_count="$(find "$state/completed" -maxdepth 1 -type f | wc -l | tr -d ' ')"
    total_count="$(wc -l < "$units" | tr -d ' ')"
    echo "[$(date -Iseconds)] unit VFS-content-identical reuse block=$block chunk=$chunk progress=$completed_count/$total_count"
    return 0
  fi
  if [[ "$block" != "Table" && -z "${force_reexport_units[$key]+present}" ]] && python3 "$unit_state_tool" can-skip \
      "$unit_manifest" "$logical_name" "$identity" /data; then
    write_marker "$marker" "$block" "$chunk"
    completed_count="$(find "$state/completed" -maxdepth 1 -type f | wc -l | tr -d ' ')"
    total_count="$(wc -l < "$units" | tr -d ' ')"
    echo "[$(date -Iseconds)] unit unchanged skip block=$block chunk=$chunk progress=$completed_count/$total_count"
    return 0
  fi

  raw_complete="$unit/.raw-complete"
  if [[ -f "$raw_complete" && -d "$unit/Raw" ]]; then
    mkdir -p "$pck_scratch" "$bundle_scratch"
    echo "[$(date -Iseconds)] unit resume block=$block chunk=$chunk stage=raw-complete"
  else
    set_phase "$block" "$chunk" "cleanup-staging"
    rm -rf "$unit" "$pck_scratch" "$bundle_scratch"
    mkdir -p "$unit/Raw" "$pck_scratch" "$bundle_scratch"

    echo "[$(date -Iseconds)] unit start block=$block chunk=$chunk"
    set_phase "$block" "$chunk" "raw-dump"
    endfield-dump dump \
      --vfs "$streaming_assets" --out "$unit/Raw" \
      --block "$block" --chunk "$chunk" --threads 1
    raw_complete_tmp="$raw_complete.tmp"
    printf '%s\n' "$(date -Iseconds)" > "$raw_complete_tmp"
    mv "$raw_complete_tmp" "$raw_complete"
    echo "[$(date -Iseconds)] unit raw complete block=$block chunk=$chunk"
  fi

  reuse_existing=0
  incremental_filter=0
  changed_list="$unit/changed-raw.txt"
  existing_outputs="$unit/existing-outputs.txt"
  : > "$existing_outputs"
  raw_fingerprint="$(python3 "$unit_state_tool" tree-fingerprint "$unit/Raw")"
  case "$block" in
    InitBundle|InitialBundle|Bundle|InitAudio|InitialAudio|AuditAudio|Audio|HotfixAudio|AudioChinese|AudioEnglish|AudioJapanese|AudioKorean|AuditVideo|Video)
      if [[ -n "${force_reexport_units[$key]+present}" ]]; then
        echo "[$(date -Iseconds)] unit forced full derive block=$block chunk=$chunk"
      elif python3 "$unit_state_tool" reuse-fingerprint \
          "$unit_baseline" "$raw_fingerprint" /data "$existing_outputs"; then
        reuse_existing=1
        echo "[$(date -Iseconds)] unit raw fingerprint reuse block=$block chunk=$chunk"
      elif [[ "$block" == "Video" || "$block" == "AuditVideo" ]] && python3 "$unit_state_tool" bootstrap-video \
          "$unit/Raw" /data/Raw /data/Video "$existing_outputs"; then
        reuse_existing=1
        echo "[$(date -Iseconds)] unit existing video verified block=$block chunk=$chunk"
      elif [[ -f /data/export-manifest.json ]]; then
        build_changed_raw_list "$unit/Raw" "$changed_list"
        changed_count="$(wc -l < "$changed_list" | tr -d ' ')"
        if (( changed_count == 0 )); then
          reuse_existing=1
          echo "[$(date -Iseconds)] unit raw-identical reuse block=$block chunk=$chunk"
        else
          incremental_filter=1
          echo "[$(date -Iseconds)] unit raw-diff block=$block chunk=$chunk changed=$changed_count"
        fi
      fi
      ;;
  esac

  case "$block:$reuse_existing" in
    Table:*)
      set_phase "$block" "$chunk" "masterdata-copy"
      mkdir -p "$unit/MasterData/Table"
      cp -a "$unit/Raw/Table/." "$unit/MasterData/Table/"
      ;;
    InitBundle:0|InitialBundle:0|Bundle:0)
      set_phase "$block" "$chunk" "texture-extract"
      mkdir -p "$unit/Textures"
      bundle_filter=()
      if (( incremental_filter == 1 )); then
        bundle_filter=(--bundle-list "$changed_list")
      fi
      endfield-dump extract \
        --vfs "$streaming_assets" --out "$unit/Textures" \
        --block "$block" --chunk "$chunk" --threads "$export_threads" \
        "${bundle_filter[@]}" \
        --existing-output /data/Textures \
        --provenance "$unit/Raw/TextureProvenance/${block}-${chunk%.chk}.jsonl" \
        --scratch "$bundle_scratch" --format png --png-compression fast \
        --max-memory-gb 3 --classify
      ;;
    InitAudio:0|InitialAudio:0|AuditAudio:0|Audio:0|HotfixAudio:0|AudioChinese:0|AudioEnglish:0|AudioJapanese:0|AudioKorean:0)
      set_phase "$block" "$chunk" "audio-extract"
      if [[ ! -f /data/MasterData/Table/AudioDialog.json ]]; then
        echo "AudioDialog masterdata must be committed before audio units" >&2
        return 5
      fi
      mkdir -p "$unit/Audio"
      endfield-dump audio \
        --vfs "$streaming_assets" --out "$unit/Audio" \
        --block all --chunk "$chunk" --language all --format wem --threads 1 \
        --audio-dialog-json /data/MasterData/Table/AudioDialog.json \
        --scratch "$pck_scratch" --pck-memory-mb 256
      ;;
    AuditVideo:0|Video:0)
      set_phase "$block" "$chunk" "video-convert"
      mkdir -p "$unit/Video"
      endfield-dump video \
        --vfs "$streaming_assets" --out "$unit/Video" \
        --block all --chunk "$chunk" --format mp4 --threads 1
      ;;
  esac

  if (( incremental_filter == 1 )); then
    python3 "$unit_state_tool" retain-listed "$unit/Raw" "$changed_list"
  fi

  outputs="$unit/outputs.txt"
  : > "$outputs"
  for category in MasterData Raw Textures Audio Video; do
    if [[ -d "$unit/$category" ]]; then
      find "$unit/$category" -type f -print \
        | sed "s#^$unit/##" >> "$outputs"
    fi
  done
  if [[ -s "$existing_outputs" ]]; then
    cat "$existing_outputs" >> "$outputs"
  fi
  if (( incremental_filter == 1 )); then
    carried="$(python3 "$unit_state_tool" carry-forward "$unit_manifest" "$outputs" /data)"
    echo "[$(date -Iseconds)] unit incremental outputs carried-forward=$carried block=$block chunk=$chunk"
  fi
  if [[ "$block" == "JsonData" ]] || grep -q '^Textures/' "$outputs"; then
    touch "$normalization_needed"
  fi
  if grep -Eq '^(Audio|Video)/' "$outputs"; then
    touch "$media_changed"
  fi
  if (( reuse_existing == 1 )); then
    rm -rf "$unit/Raw"
  fi
  python3 "$unit_state_tool" prune-stale \
    "$unit_manifest" "$outputs" /data \
    --protected-map-manifest /data/.baseline/map-texture-selection.json

  for category in MasterData Raw Textures Audio Video; do
    set_phase "$block" "$chunk" "commit-${category}"
    commit_tree "$unit/$category" "/data/$category" "$category"
  done

  python3 "$unit_state_tool" record \
    "$unit_manifest" "$ZMD_VERSION" "$logical_name" "$identity" "$outputs" /data \
    "$raw_fingerprint" "$source_fingerprint"
  write_marker "$marker" "$block" "$chunk"
  set_phase "$block" "$chunk" "delete-source-chunk"
  if [[ "$preserve_vfs" != "1" ]]; then rm -f "$chunk_path"; fi
  rm -rf "$unit" "$pck_scratch" "$bundle_scratch"
  completed_count="$(find "$state/completed" -maxdepth 1 -type f | wc -l | tr -d ' ')"
  total_count="$(wc -l < "$units" | tr -d ' ')"
  echo "[$(date -Iseconds)] unit complete block=$block chunk=$chunk progress=$completed_count/$total_count"
  if [[ -n "${force_reexport_units[$key]+present}" ]]; then
    unset 'force_reexport_units[$key]'
    force_tmp="$force_reexport_file.tmp"
    : > "$force_tmp"
    for remaining_unit in "${!force_reexport_units[@]}"; do
      printf '%s\n' "$remaining_unit" >> "$force_tmp"
    done
    sort -o "$force_tmp" "$force_tmp"
    mv "$force_tmp" "$force_reexport_file"
  fi
}

while IFS=$'\t' read -r block chunk file_count logical_bytes; do
  [[ -n "$block" && -n "$chunk" ]] || continue
  process_unit "$block" "$chunk"
done < "$units"

echo "[$(date -Iseconds)] building compact export unit index"
python3 "$unit_state_tool" build-index \
  "$unit_baseline" /data/.baseline/export-unit-index.json "$source_identities"
touch "$json_audit_marker"

if [[ -f /data/.baseline/texture-names-normalized-v1 && ! -f "$normalization_needed" ]]; then
  echo "[$(date -Iseconds)] texture/map normalization unchanged skip"
else
echo "[$(date -Iseconds)] normalizing exported texture names"
map_provenance_args=()
for provenance_root in \
  /data/Raw/TextureProvenance \
  /data/.baseline/map-provenance \
  "$work/map-provenance-${ZMD_VERSION}-v3/provenance" \
  "$work/map-provenance-${ZMD_VERSION}"; do
  if [[ -d "$provenance_root" ]]; then
    map_provenance_args+=(--provenance-root "$provenance_root")
  fi
done
if (( ${#map_provenance_args[@]} == 0 )); then
  mkdir -p /data/.baseline/map-provenance
  map_provenance_args=(--provenance-root /data/.baseline/map-provenance)
fi
python3 /app/scripts/normalize-map-texture-layers.py \
  /data/Textures/other \
  /data/Raw/Data/Json/UILevelMapLoadConfig \
  "${map_provenance_args[@]}" \
  --manifest /data/.baseline/map-texture-selection.json \
  --apply
mkdir -p /data/.baseline/map-provenance
for provenance_root in \
  "$work/map-provenance-${ZMD_VERSION}-v3/provenance" \
  "$work/map-provenance-${ZMD_VERSION}"; do
  if [[ -d "$provenance_root" ]]; then
    find "$provenance_root" -type f -name '*.jsonl' -exec cp -f {} /data/.baseline/map-provenance/ \;
  fi
done
python3 /app/scripts/normalize-texture-names.py /data/Textures \
  --apply \
  --map /data/.baseline/texture-name-map.json
touch /data/.baseline/texture-names-normalized-v1
rm -f "$normalization_needed"
fi

# Indexes and installer volumes are no longer needed after every chunk has an
# atomic completion marker and all derived outputs have been committed.
if [[ "$preserve_vfs" != "1" ]]; then
  find "$streaming_assets/VFS" -type f -name '*.blc' -delete
fi
package_root="$work/Packages/${ZMD_VERSION}"
if [[ -d "$package_root" ]]; then rm -rf "$package_root"; fi
rm -rf "$scratch"
rm -f "$state/current.tsv"
echo "[$(date -Iseconds)] stream export completed"
