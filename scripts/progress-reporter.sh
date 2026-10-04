#!/bin/bash
set -uo pipefail

version="${ZMD_VERSION:-unknown}"
state="/data/.work/State/stream-export-${version}"
scratch="/data/.work/Scratch/${version}/units"
interval="${ENDFIELD_PROGRESS_INTERVAL_SECONDS:-30}"

while true; do
  completed=0
  total=0
  current="none"
  phase="waiting"

  if [[ -d "$state/completed" ]]; then
    completed="$(find "$state/completed" -maxdepth 1 -type f | wc -l | tr -d ' ')"
  fi
  if [[ -f "$state/units.tsv" ]]; then
    total="$(wc -l < "$state/units.tsv" | tr -d ' ')"
  fi
  if [[ -f "$state/current.tsv" ]]; then
    IFS=$'\t' read -r _ block chunk phase < "$state/current.tsv" || true
    current="${block:-unknown}/${chunk:-unknown}"
  elif [[ -d "$scratch" ]]; then
    current_path="$(find "$scratch" -mindepth 1 -maxdepth 1 -type d -print -quit 2>/dev/null || true)"
    if [[ -n "$current_path" ]]; then
      current="${current_path##*/}"
      phase="commit-or-cleanup"
    fi
  fi

  printf '[%s] heartbeat progress=%s/%s current=%s stage=%s\n' \
    "$(date -Iseconds)" "$completed" "$total" "$current" "$phase" > /proc/1/fd/1
  sleep "$interval"
done
