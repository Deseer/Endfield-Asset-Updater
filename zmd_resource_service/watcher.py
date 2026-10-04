from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from .manifest import atomic_json, fetch_latest, sanitize_manifest
from .pipeline import Pipeline
from .privacy import redact_text
from .resources import _logical_index_name, fetch_resource_snapshot


DATA_ROOT = Path("/data")
PERSISTENT_STATUS = DATA_ROOT / "update-watcher-status.json"


def log(message: str) -> None:
    print(f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] watcher {redact_text(message)}", flush=True)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def exported_version(root: Path | None = None) -> str | None:
    root = root or DATA_ROOT
    manifest = read_json(root / "export-manifest.json")
    return str(manifest["version"]) if manifest and manifest.get("version") else None


def resources_changed(snapshot: dict[str, Any], root: Path | None = None) -> bool:
    root = root or DATA_ROOT
    previous = read_json(root / ".baseline/resource-snapshot.json")
    # Old installations need one verified update to establish index identity.
    return previous != snapshot


def media_is_complete(root: Path | None = None) -> bool:
    root = root or DATA_ROOT
    report = read_json(root / "media-preview-report.json")
    if not report:
        return False
    for category in ("video", "audio"):
        section = report.get(category, {})
        if section.get("processed") != section.get("total") or section.get("errors"):
            return False
    return True


def media_preview_required(pipeline: Pipeline, version: str) -> bool:
    changed = pipeline.state / f"stream-export-{version}" / "media-changed"
    return changed.exists() or not media_is_complete(pipeline.root)


def notify(title: str, body: str) -> None:
    helper = Path("/opt/bark-skill/scripts/notify.py")
    if not helper.is_file():
        log("warning: Bark helper is not mounted")
        return
    result = subprocess.run(
        ["python3", str(helper), "--title", title, "--body", body], check=False
    )
    if result.returncode:
        log("warning: Bark notification delivery failed")


def watcher_state(pipeline: Pipeline) -> tuple[Path, dict[str, Any] | None]:
    path = pipeline.state / "watcher-state.json"
    return path, read_json(path)


def write_stage(path: Path, version: str, stage: str, **extra: Any) -> None:
    atomic_json(
        path,
        {
            "target_version": version,
            "stage": stage,
            "updated_at": datetime.now().astimezone().isoformat(),
            **extra,
        },
    )


def load_target(pipeline: Pipeline, latest: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
    state_path, state = watcher_state(pipeline)
    if state and state.get("target_version") and state.get("stage"):
        version = str(state["target_version"])
        saved = read_json(pipeline.state / f"official-manifest-{version}.json")
        if not saved:
            raise RuntimeError(f"missing saved official manifest for in-progress {version}")
        return saved, version, str(state["stage"])

    version = str(latest["version"])
    atomic_json(pipeline.state / f"official-manifest-{version}.json", sanitize_manifest(latest))
    write_stage(state_path, version, "resource-plan")
    return latest, version, "resource-plan"


def prepare_streaming_assets(pipeline: Pipeline, version: str) -> Path:
    expected = pipeline.vfs_root / version / "Endfield_Data" / "StreamingAssets"
    existing_vfs = expected / "VFS"
    if existing_vfs.is_dir() and any(existing_vfs.rglob("*")):
        return expected

    baseline = DATA_ROOT / ".baseline"
    baseline_streaming = baseline / "StreamingAssets"
    if baseline_streaming.is_dir():
        if expected.exists():
            shutil.rmtree(expected)
        expected.parent.mkdir(parents=True, exist_ok=True)
        os.replace(baseline_streaming, expected)
        manifest = read_json(baseline / "baseline-manifest.json") or {}
        log(
            f"restored VFS baseline version={manifest.get('version', 'unknown')} "
            f"for target={version}"
        )
    baseline_cache = baseline / "resource-cache.json"
    work_cache = pipeline.state / "resource-cache.json"
    if baseline_cache.is_file() and not work_cache.exists():
        os.replace(baseline_cache, work_cache)
    (expected / "VFS").mkdir(parents=True, exist_ok=True)
    return expected


def prune_vfs_to_current_indexes(
    pipeline: Pipeline, version: str, resource_version: str
) -> int:
    indexes = list(pipeline.state.glob(f"resource-index-*-{resource_version}.json"))
    if not indexes:
        raise RuntimeError(f"no resource indexes found for {resource_version}")
    expected: set[str] = set()
    for path in indexes:
        payload = read_json(path)
        if not payload or not isinstance(payload.get("files"), list):
            raise RuntimeError(f"invalid resource index: {path}")
        expected.update(_logical_index_name(str(item["name"])) for item in payload["files"])

    vfs = pipeline.find_streaming_assets(pipeline.vfs_root / version) / "VFS"
    removed = 0
    for path in vfs.rglob("*"):
        if path.is_file() and path.relative_to(vfs).as_posix() not in expected:
            path.unlink()
            removed += 1
    for path in sorted(
        (p for p in vfs.rglob("*") if p.is_dir()),
        key=lambda value: len(value.parts),
        reverse=True,
    ):
        try:
            path.rmdir()
        except OSError:
            pass

    cache_path = pipeline.state / "resource-cache.json"
    cache = read_json(cache_path) or {}
    records = []
    for record in cache.get("files", []):
        if not isinstance(record, dict) or str(record.get("logical_name")) not in expected:
            continue
        storage = record.get("storage_name")
        if storage and (vfs / str(storage)).is_file():
            records.append(record)
    cache["files"] = records
    atomic_json(cache_path, cache)
    log(f"VFS baseline pruned expected={len(expected)} removed={removed} cache={len(records)}")
    return removed


def check_disk(root: Path, download_bytes: int) -> None:
    free = shutil.disk_usage(root).free
    reserve = 8 * 1024**3
    if free < download_bytes + reserve:
        raise RuntimeError(
            f"insufficient disk: free={free} required={download_bytes + reserve} "
            f"(download plus 8 GiB reserve)"
        )


def run_stream_export(version: str) -> None:
    env = dict(os.environ)
    env["ZMD_VERSION"] = version
    reporter = subprocess.Popen(["/bin/bash", "/app/scripts/progress-reporter.sh"], env=env)
    try:
        subprocess.run(
            ["/bin/bash", "/app/scripts/stream-export.sh"], env=env, check=True
        )
    finally:
        reporter.terminate()
        try:
            reporter.wait(timeout=10)
        except subprocess.TimeoutExpired:
            reporter.kill()
            reporter.wait()


def perform_update(latest: dict[str, Any]) -> str:
    workers = int(os.environ.get("ENDFIELD_DOWNLOAD_WORKERS", "1"))
    memory_mb = int(os.environ.get("ZMD_PATCH_MEMORY_MB", "1536"))
    pipeline = Pipeline(DATA_ROOT, workers=workers)
    state_path, _ = watcher_state(pipeline)
    payload, version, stage = load_target(pipeline, latest)
    prepare_streaming_assets(pipeline, version)
    force_reexport = pipeline.state / "force-reexport-units.txt"
    if force_reexport.is_file() and force_reexport.stat().st_size > 0 and stage != "stream-export":
        rename_candidates = pipeline.work / "Reports" / "texture-name-renames.txt"
        rename_marker = DATA_ROOT / ".baseline" / "texture-names-normalized-v1"
        if rename_candidates.is_file() and not rename_marker.exists():
            log("normalizing visible texture collision names")
            subprocess.run(
                [
                    "python3",
                    "/app/scripts/normalize-texture-names.py",
                    str(DATA_ROOT / "Textures"),
                    "--candidate-list",
                    str(rename_candidates),
                    "--apply",
                    "--map",
                    str(DATA_ROOT / ".baseline" / "texture-name-map.json"),
                ],
                check=True,
            )
            rename_marker.touch()
        stage = "stream-export"
        write_stage(state_path, version, stage, reason="forced-texture-reexport")
    log(f"update target={version} resume_stage={stage}")

    if stage == "resource-plan":
        log("resource planning: checking indexes and verifying uncached VFS hashes")
        plan = pipeline.resource_plan(payload)
        if plan["unavailable"]:
            raise RuntimeError(f"resource plan has {len(plan['unavailable'])} unavailable files")
        download_bytes = int(plan["full_bytes"]) + int(plan["patch_bytes"])
        check_disk(DATA_ROOT, download_bytes)
        log(
            f"plan target={version} current={plan['current_files']} "
            f"patch={plan['patch_files']} full={plan['full_files']} "
            f"download_bytes={download_bytes}"
        )
        write_stage(
            state_path,
            version,
            "resource-sync",
            resource_version=plan["resource_version"],
            download_bytes=download_bytes,
        )
        stage = "resource-sync"
    else:
        plan = None

    if stage == "resource-sync":
        if plan is None:
            saved_state = read_json(state_path) or {}
            saved_resource_version = saved_state.get("resource_version")
            if saved_resource_version:
                plan = read_json(pipeline.state / f"resource-plan-{saved_resource_version}.json")
        result = pipeline.sync_resources(payload, memory_mb, plan=plan)
        log(
            f"resource sync complete target={version} applied={result['applied_files']} "
            f"full={result['downloaded_full_files']} cache={result['cache_files']}"
        )
        prune_vfs_to_current_indexes(
            pipeline, version, str(result["resource_version"])
        )
        write_stage(state_path, version, "stream-export")
        stage = "stream-export"

    if stage == "stream-export":
        run_stream_export(version)
        write_stage(state_path, version, "media-preview")
        stage = "media-preview"

    if stage == "media-preview":
        if media_preview_required(pipeline, version):
            env = dict(os.environ)
            env["ZMD_VERSION"] = version
            subprocess.run(
                ["python3", "-u", "/app/scripts/preview_media.py"], env=env, check=True
            )
        else:
            log("media preview unchanged: reusing completed verification report")
        write_stage(state_path, version, "finalize")
        stage = "finalize"

    if stage == "finalize":
        subprocess.run(
            ["python3", "/app/scripts/finalize-export.py", "/data", version], check=True
        )
        if exported_version() != version or not media_is_complete():
            raise RuntimeError("final completion manifests did not validate")
        atomic_json(
            PERSISTENT_STATUS,
            {
                "status": "complete",
                "version": version,
                "completed_at": datetime.now().astimezone().isoformat(),
            },
        )
        notify(
            "Endfield 自动更新完成",
            f"版本 {version} 的素材、MasterData 和可预览音视频已更新。",
        )
        log(f"update complete target={version}")
    return version


def run_forever() -> int:
    poll_seconds = max(60, int(os.environ.get("ENDFIELD_POLL_SECONDS", "300")))
    retry_seconds = max(300, int(os.environ.get("ENDFIELD_RETRY_SECONDS", "3600")))
    log(f"service started poll_seconds={poll_seconds} retry_seconds={retry_seconds}")
    while True:
        try:
            latest = fetch_latest(timeout=30)
            latest_version = str(latest["version"])
            local_version = exported_version()
            in_progress = read_json(DATA_ROOT / ".work/State/watcher-state.json")
            snapshot = None if in_progress else fetch_resource_snapshot(latest, timeout=30)
            hotfix = snapshot is not None and resources_changed(snapshot)
            if in_progress or local_version != latest_version or hotfix:
                log(
                    f"version change local={local_version or 'none'} "
                    f"official={latest_version} in_progress={bool(in_progress)} "
                    f"resource_changed={hotfix} resource_version={snapshot.get('resource_version') if snapshot else 'resuming'}"
                )
                perform_update(latest)
                continue
            log(f"scan complete local={local_version} official={latest_version} "
                f"resource_version={snapshot['resource_version']} index_hashes=matched no_change")
            time.sleep(poll_seconds)
        except KeyboardInterrupt:
            return 0
        except Exception as error:
            target = read_json(DATA_ROOT / ".work/State/watcher-state.json") or {}
            version = str(target.get("target_version") or "unknown")
            log(f"update ERROR target={version} type={type(error).__name__} error={error}")
            print(redact_text(traceback.format_exc()), flush=True)
            atomic_json(
                PERSISTENT_STATUS,
                {
                    "status": "failed",
                    "version": version,
                    "failed_at": datetime.now().astimezone().isoformat(),
                    "error_type": type(error).__name__,
                    "error": redact_text(str(error))[:2000],
                    "retry_seconds": retry_seconds,
                },
            )
            notify(
                "Endfield 自动更新失败",
                f"版本 {version} 更新报错，已保留断点，{retry_seconds // 60} 分钟后重试。",
            )
            time.sleep(retry_seconds)


if __name__ == "__main__":
    raise SystemExit(run_forever())
