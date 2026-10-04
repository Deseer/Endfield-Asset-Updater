from pathlib import Path
import json

import pytest

from zmd_resource_service.pipeline import Pipeline


def test_pipeline_uses_hidden_work_root_and_unversioned_exports(tmp_path: Path):
    pipeline = Pipeline(tmp_path)
    assert pipeline.work == tmp_path / ".work"
    assert pipeline.state == tmp_path / ".work" / "State"
    assert pipeline.packages == tmp_path / ".work" / "Packages"
    assert pipeline.vfs_root == tmp_path / ".work" / "VFS"
    assert pipeline.exports == tmp_path


def test_inventory(tmp_path: Path):
    (tmp_path / "a").write_bytes(b"abc")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "b").write_bytes(b"12345")
    assert Pipeline.inventory(tmp_path) == {"file_count": 2, "bytes": 8}


def test_find_streaming_assets(tmp_path: Path):
    expected = tmp_path / "Game" / "Endfield_Data" / "StreamingAssets"
    (expected / "VFS").mkdir(parents=True)
    assert Pipeline.find_streaming_assets(tmp_path) == expected


def test_find_streaming_assets_rejects_missing(tmp_path: Path):
    with pytest.raises(RuntimeError, match="found 0"):
        Pipeline.find_streaming_assets(tmp_path)


def test_reusable_exported_units_requires_completed_export(tmp_path: Path):
    manifests = tmp_path / ".baseline" / "export-units"
    manifests.mkdir(parents=True)
    (manifests / "Bundle-a.json").write_text(json.dumps({
        "logical_name": "Bundle/a.chk",
        "identity": "abc:10",
        "outputs": ["Raw/a.bundle"],
    }))

    assert Pipeline(tmp_path)._reusable_exported_units() == {}


def test_reusable_exported_units_trusts_published_manifest_without_output_scan(tmp_path: Path):
    (tmp_path / "export-manifest.json").write_text(json.dumps({"version": "1.0.0"}))
    manifests = tmp_path / ".baseline" / "export-units"
    manifests.mkdir(parents=True)
    (manifests / "Bundle-a.json").write_text(json.dumps({
        "logical_name": "Bundle/a.chk",
        "identity": "abc:10",
        "outputs": ["Raw/not-stat-ed.bundle"],
        "output_sizes": {"Raw/not-stat-ed.bundle": 999},
    }))
    (manifests / "Bundle-unsafe.json").write_text(json.dumps({
        "logical_name": "Bundle/unsafe.chk",
        "identity": "unsafe:1",
        "outputs": ["../escape"],
    }))

    assert Pipeline(tmp_path)._reusable_exported_units() == {"Bundle/a.chk": "abc:10"}
    index = json.loads((tmp_path / ".baseline" / "export-unit-index.json").read_text())
    assert index["units"] == {"Bundle/a.chk": "abc:10"}


def test_reusable_exported_units_prefers_compact_index(tmp_path: Path):
    (tmp_path / "export-manifest.json").write_text(json.dumps({"version": "1.0.0"}))
    baseline = tmp_path / ".baseline"
    (baseline / "export-units").mkdir(parents=True)
    (baseline / "export-unit-index.json").write_text(json.dumps({
        "format": 1,
        "units": {"Bundle/a.chk": "abc:10", "../unsafe": "bad:1"},
    }))

    assert Pipeline(tmp_path)._reusable_exported_units() == {"Bundle/a.chk": "abc:10"}
