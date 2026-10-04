import importlib.util
import json
import os
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "finalize_export", Path(__file__).parents[1] / "scripts/finalize-export.py"
)
finalize = importlib.util.module_from_spec(spec)
spec.loader.exec_module(finalize)


def test_preserve_vfs_baseline_moves_exact_compressed_inputs(tmp_path):
    streaming = tmp_path / ".work/VFS/1.5.3/Endfield_Data/StreamingAssets"
    chunk = streaming / "VFS/Table/table.chk"
    chunk.parent.mkdir(parents=True)
    chunk.write_bytes(b"compressed")
    state = tmp_path / ".work/State"
    state.mkdir(parents=True)
    (state / "resource-cache.json").write_text(json.dumps({"files": []}))
    snapshot = {"resource_version": "r2", "index_fingerprints": {"main": "digest"}}
    (state / "resource-snapshot.json").write_text(json.dumps(snapshot))

    previous = os.environ.get("ENDFIELD_PRESERVE_VFS")
    os.environ["ENDFIELD_PRESERVE_VFS"] = "1"
    try:
        finalize.preserve_vfs_baseline(tmp_path, "1.5.3")
    finally:
        if previous is None:
            os.environ.pop("ENDFIELD_PRESERVE_VFS", None)
        else:
            os.environ["ENDFIELD_PRESERVE_VFS"] = previous

    assert (tmp_path / ".baseline/StreamingAssets/VFS/Table/table.chk").read_bytes() == b"compressed"
    assert (tmp_path / ".baseline/resource-cache.json").is_file()
    assert not streaming.exists()
    assert json.loads((tmp_path / ".baseline/resource-snapshot.json").read_text()) == snapshot


def test_disk_saving_baseline_keeps_metadata_without_vfs(tmp_path, monkeypatch):
    streaming = tmp_path / ".work/VFS/1.5.3/Endfield_Data/StreamingAssets"
    chunk = streaming / "VFS/Video/video.chk"
    chunk.parent.mkdir(parents=True)
    chunk.write_bytes(b"compressed")
    state = tmp_path / ".work/State"
    state.mkdir(parents=True)
    (state / "resource-cache.json").write_text(json.dumps({"files": []}))
    (state / "resource-snapshot.json").write_text(json.dumps({"resource_version": "r2"}))
    monkeypatch.setenv("ENDFIELD_PRESERVE_VFS", "0")

    finalize.preserve_vfs_baseline(tmp_path, "1.5.3")

    assert chunk.is_file()
    assert (tmp_path / ".baseline/resource-cache.json").is_file()
    assert (tmp_path / ".baseline/resource-snapshot.json").is_file()
    assert not (tmp_path / ".baseline/StreamingAssets").exists()
