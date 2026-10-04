import json
from pathlib import Path

import pytest

from zmd_resource_service import watcher
from zmd_resource_service.pipeline import Pipeline


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_completion_reads_export_and_media_manifests(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher, "DATA_ROOT", tmp_path)
    write_json(tmp_path / "export-manifest.json", {"version": "1.5.3"})
    write_json(
        tmp_path / "media-preview-report.json",
        {
            "video": {"processed": 2, "total": 2, "errors": []},
            "audio": {"processed": 3, "total": 3, "errors": []},
        },
    )
    assert watcher.exported_version() == "1.5.3"
    assert watcher.media_is_complete()


def test_same_client_version_detects_resource_hotfix(tmp_path):
    write_json(tmp_path / "export-manifest.json", {"version": "1.5.3"})
    before = {"resource_version": "r1", "index_fingerprints": {"main": "old"}}
    write_json(tmp_path / ".baseline/resource-snapshot.json", before)
    assert not watcher.resources_changed(before, tmp_path)
    assert watcher.resources_changed({**before, "resource_version": "r2"}, tmp_path)
    assert watcher.resources_changed({**before, "index_fingerprints": {"main": "new"}}, tmp_path)


def test_missing_snapshot_requires_verified_bootstrap(tmp_path):
    assert watcher.resources_changed({"resource_version": "r1", "index_fingerprints": {}}, tmp_path)


def test_watcher_runs_update_for_same_client_hotfix(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher, "DATA_ROOT", tmp_path)
    write_json(tmp_path / "export-manifest.json", {"version": "1.5.3"})
    write_json(tmp_path / ".baseline/resource-snapshot.json", {"resource_version": "r1"})
    monkeypatch.setattr(watcher, "fetch_latest", lambda **_: {"version": "1.5.3"})
    monkeypatch.setattr(watcher, "fetch_resource_snapshot", lambda *_, **__: {"resource_version": "r2"})
    updates = []

    def update(latest):
        updates.append(latest["version"])
        raise KeyboardInterrupt

    monkeypatch.setattr(watcher, "perform_update", update)
    assert watcher.run_forever() == 0
    assert updates == ["1.5.3"]


def test_watcher_does_not_reexport_matching_resource_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher, "DATA_ROOT", tmp_path)
    snapshot = {"resource_version": "r1", "index_fingerprints": {"main": "x"}}
    write_json(tmp_path / "export-manifest.json", {"version": "1.5.3"})
    write_json(tmp_path / ".baseline/resource-snapshot.json", snapshot)
    monkeypatch.setattr(watcher, "fetch_latest", lambda **_: {"version": "1.5.3"})
    monkeypatch.setattr(watcher, "fetch_resource_snapshot", lambda *_, **__: snapshot)
    monkeypatch.setattr(watcher, "perform_update", lambda _: pytest.fail("must not reexport"))

    def stop(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(watcher.time, "sleep", stop)
    assert watcher.run_forever() == 0


def test_media_errors_are_not_complete(tmp_path):
    write_json(
        tmp_path / "media-preview-report.json",
        {
            "video": {"processed": 2, "total": 2, "errors": [{"file": "bad"}]},
            "audio": {"processed": 3, "total": 3, "errors": []},
        },
    )
    assert not watcher.media_is_complete(tmp_path)


def test_media_preview_reuses_complete_report_until_media_changes(tmp_path):
    pipeline = Pipeline(tmp_path)
    write_json(
        tmp_path / "media-preview-report.json",
        {
            "video": {"processed": 2, "total": 2, "errors": []},
            "audio": {"processed": 3, "total": 3, "errors": []},
        },
    )
    assert not watcher.media_preview_required(pipeline, "1.5.3")
    changed = pipeline.state / "stream-export-1.5.3" / "media-changed"
    changed.parent.mkdir(parents=True)
    changed.touch()
    assert watcher.media_preview_required(pipeline, "1.5.3")


def test_load_target_resumes_saved_in_progress_version(tmp_path):
    pipeline = Pipeline(tmp_path)
    state_path = pipeline.state / "watcher-state.json"
    write_json(state_path, {"target_version": "1.5.2", "stage": "stream-export"})
    saved = {"version": "1.5.2", "pkg": {"file_path": "https://cdn/v1_token/files"}}
    write_json(pipeline.state / "official-manifest-1.5.2.json", saved)
    payload, version, stage = watcher.load_target(pipeline, {"version": "1.5.3"})
    assert payload == saved
    assert version == "1.5.2"
    assert stage == "stream-export"


def test_disk_check_preserves_reserve(tmp_path, monkeypatch):
    class Usage:
        free = 10 * 1024**3

    monkeypatch.setattr(watcher.shutil, "disk_usage", lambda _: Usage())
    watcher.check_disk(tmp_path, 2 * 1024**3)
    with pytest.raises(RuntimeError, match="insufficient disk"):
        watcher.check_disk(tmp_path, 3 * 1024**3)


def test_prepare_streaming_assets_restores_hidden_baseline(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher, "DATA_ROOT", tmp_path)
    baseline = tmp_path / ".baseline"
    chunk = baseline / "StreamingAssets/VFS/Table/table.chk"
    chunk.parent.mkdir(parents=True)
    chunk.write_bytes(b"old-compressed-block")
    write_json(baseline / "resource-cache.json", {"files": [{"logical_name": "Table/table.chk"}]})
    write_json(baseline / "baseline-manifest.json", {"version": "1.5.3"})

    pipeline = Pipeline(tmp_path)
    restored = watcher.prepare_streaming_assets(pipeline, "1.5.4")

    assert (restored / "VFS/Table/table.chk").read_bytes() == b"old-compressed-block"
    assert (pipeline.state / "resource-cache.json").is_file()
    assert not (baseline / "StreamingAssets").exists()


def test_prepare_streaming_assets_restores_metadata_without_vfs(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher, "DATA_ROOT", tmp_path)
    baseline = tmp_path / ".baseline"
    write_json(baseline / "resource-cache.json", {"files": []})
    pipeline = Pipeline(tmp_path)

    restored = watcher.prepare_streaming_assets(pipeline, "1.5.4")

    assert (restored / "VFS").is_dir()
    assert (pipeline.state / "resource-cache.json").is_file()


def test_prune_vfs_uses_current_indexes_and_filters_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher, "DATA_ROOT", tmp_path)
    pipeline = Pipeline(tmp_path)
    streaming = watcher.prepare_streaming_assets(pipeline, "1.5.4")
    keep = streaming / "VFS/Table/keep.chk"
    stale = streaming / "VFS/Table/stale.chk"
    keep.parent.mkdir(parents=True, exist_ok=True)
    keep.write_bytes(b"keep")
    stale.write_bytes(b"stale")
    write_json(
        pipeline.state / "resource-index-main-r2.json",
        {"files": [{"name": "VFS/Table/keep.chk", "md5": "unused"}]},
    )
    write_json(
        pipeline.state / "resource-cache.json",
        {
            "files": [
                {"logical_name": "Table/keep.chk", "storage_name": "Table/keep.chk"},
                {"logical_name": "Table/stale.chk", "storage_name": "Table/stale.chk"},
            ]
        },
    )

    assert watcher.prune_vfs_to_current_indexes(pipeline, "1.5.4", "r2") == 1
    assert keep.is_file()
    assert not stale.exists()
    cache = json.loads((pipeline.state / "resource-cache.json").read_text())
    assert [item["logical_name"] for item in cache["files"]] == ["Table/keep.chk"]
