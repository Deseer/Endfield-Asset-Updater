import importlib.util
import json
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "export_unit_state", Path(__file__).parents[1] / "scripts/export-unit-state.py"
)
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)


def test_resource_identity_reads_current_version_indexes(tmp_path):
    (tmp_path / "resources-1.5.3.json").write_text(json.dumps({"res_version": "r3"}))
    (tmp_path / "resource-index-main-r3.json").write_text(
        json.dumps({"files": [{"name": "VFS/Block/a.chk", "md5": "ABC", "size": 12}]})
    )
    assert state.resource_identity(tmp_path, "1.5.3", "Block/a.chk") == "abc:12"


def test_output_paths_are_unique_and_safe(tmp_path):
    path = tmp_path / "outputs.txt"
    path.write_text("Raw/a\nTextures/b.png\nRaw/a\n")
    assert state.output_paths(path) == ["Raw/a", "Textures/b.png"]


def test_retain_listed_removes_only_unlisted_staging_files(tmp_path, monkeypatch):
    root = tmp_path / "raw"
    (root / "a").mkdir(parents=True)
    (root / "a/keep.ab").write_bytes(b"keep")
    (root / "a/drop.ab").write_bytes(b"drop")
    paths = tmp_path / "paths.txt"
    paths.write_text("a/keep.ab\n")
    monkeypatch.setattr(
        "sys.argv", ["export-unit-state.py", "retain-listed", str(root), str(paths)]
    )
    assert state.main() == 0
    assert (root / "a/keep.ab").is_file()
    assert not (root / "a/drop.ab").exists()


def test_changed_list_compares_content_and_missing_files(tmp_path):
    source = tmp_path / "source"
    existing = tmp_path / "existing"
    source.mkdir()
    existing.mkdir()
    (source / "same.bin").write_bytes(b"same")
    (existing / "same.bin").write_bytes(b"same")
    (source / "changed.bin").write_bytes(b"new!")
    (existing / "changed.bin").write_bytes(b"old!")
    (source / "missing.bin").write_bytes(b"missing")
    output = tmp_path / "changed.txt"

    assert state.write_changed_list(source, existing, output) == 2
    assert output.read_text().splitlines() == ["changed.bin", "missing.bin"]


def test_changed_list_replaces_partial_output_atomically(tmp_path):
    source = tmp_path / "source"
    existing = tmp_path / "existing"
    source.mkdir()
    existing.mkdir()
    (source / "only.bin").write_bytes(b"new")
    output = tmp_path / "changed.txt"
    output.write_text("stale-partial-entry\n")

    state.write_changed_list(source, existing, output)

    assert output.read_text() == "only.bin\n"
    assert not (tmp_path / "changed.txt.tmp").exists()


def test_reusable_manifest_checks_outputs_and_recorded_sizes(tmp_path):
    root = tmp_path / "data"
    output = root / "Video/a.mp4"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"video")
    manifest = tmp_path / "unit.json"
    manifest.write_text(json.dumps({
        "logical_name": "A/a.chk",
        "identity": "abc:10",
        "outputs": ["Video/a.mp4"],
        "output_sizes": {"Video/a.mp4": 5},
    }))

    assert state.reusable_manifest(manifest, "A/a.chk", "abc:10", root)
    output.write_bytes(b"changed")
    assert not state.reusable_manifest(manifest, "A/a.chk", "abc:10", root)


def test_bootstrap_video_requires_identical_raw_and_existing_mp4(tmp_path):
    source = tmp_path / "staged"
    raw = tmp_path / "Raw"
    video = tmp_path / "Video"
    for root in (source, raw):
        (root / "Data/Video").mkdir(parents=True)
        (root / "Data/Video/a.usm").write_bytes(b"same")
    (video / "Data/Video").mkdir(parents=True)
    (video / "Data/Video/a.mp4").write_bytes(b"converted")
    outputs = tmp_path / "outputs.txt"

    assert state.bootstrap_video_outputs(source, raw, video, outputs)
    assert outputs.read_text() == "Video/Data/Video/a.mp4\n"
    (raw / "Data/Video/a.usm").write_bytes(b"different")
    assert not state.bootstrap_video_outputs(source, raw, video, outputs)


def test_raw_fingerprint_reuses_outputs_across_repacked_chunks(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "a.bin").write_bytes(b"same-inner-content")
    fingerprint = state.tree_fingerprint(raw)
    root = tmp_path / "data"
    derived = root / "Audio/a.wem"
    derived.parent.mkdir(parents=True)
    derived.write_bytes(b"derived")
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    (manifests / "Audio-old.json").write_text(json.dumps({
        "logical_name": "old.chk",
        "identity": "old:1",
        "raw_fingerprint": fingerprint,
        "outputs": ["Audio/a.wem"],
        "output_sizes": {"Audio/a.wem": 7},
    }))
    outputs = tmp_path / "outputs.txt"

    assert state.reuse_by_fingerprint(manifests, fingerprint, root, outputs)
    assert outputs.read_text() == "Audio/a.wem\n"


def test_resource_plan_exposes_only_patch_and_full_targets(tmp_path):
    plan = tmp_path / "resource-plan-r4.json"
    plan.write_text(json.dumps({
        "actions": [{"target_name": "VFS/A/changed.chk"}],
        "full_actions": [{"target_name": "B/new.chk"}],
        "current_files": 99,
    }))

    assert state.changed_resource_paths(plan) == ["B/new.chk", "VFS/A/changed.chk"]


def test_resource_plan_path_uses_target_resource_version(tmp_path):
    (tmp_path / "resources-1.5.3.json").write_text(json.dumps({"res_version": "r4"}))
    expected = tmp_path / "resource-plan-r4.json"
    expected.write_text("{}")

    assert state.resource_plan_path(tmp_path, "1.5.3") == expected


def test_manifest_logical_names_returns_only_valid_safe_entries(tmp_path):
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    (manifests / "JsonData-good.json").write_text(json.dumps({
        "logical_name": "775A31D1/good.chk",
        "identity": "abc:1",
    }))
    (manifests / "broken.json").write_text("not json")
    (manifests / "missing-name.json").write_text(json.dumps({"identity": "def:2"}))

    assert state.manifest_logical_names(manifests) == [
        ("JsonData-good", "775A31D1/good.chk")
    ]


def test_source_fingerprint_backfill_preserves_manifest_outputs(tmp_path):
    manifest = tmp_path / "Bundle-a.json"
    manifest.write_text(json.dumps({
        "logical_name": "old/a.chk",
        "identity": "old:1",
        "outputs": ["Raw/a.bundle", "Textures/a.png"],
    }))

    assert state.set_source_fingerprint(manifest, "content:2:10")
    payload = json.loads(manifest.read_text())
    assert payload["source_fingerprint"] == "content:2:10"
    assert payload["outputs"] == ["Raw/a.bundle", "Textures/a.png"]


def test_source_fingerprint_reuses_same_block_without_scanning_outputs(tmp_path):
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    previous = manifests / "Bundle-old.json"
    previous.write_text(json.dumps({
        "version": "1.0.0",
        "logical_name": "A/old.chk",
        "identity": "old:10",
        "source_fingerprint": "content:2:10",
        "outputs": ["Raw/a.bundle", "Textures/a.png"],
        "output_sizes": {"Raw/a.bundle": 5, "Textures/a.png": 8},
    }))
    current = manifests / "Bundle-new.json"

    assert state.reuse_source_fingerprint(
        manifests, current, "content:2:10", "1.1.0", "B/new.chk", "new:11"
    )
    payload = json.loads(current.read_text())
    assert payload["version"] == "1.1.0"
    assert payload["logical_name"] == "B/new.chk"
    assert payload["identity"] == "new:11"
    assert payload["outputs"] == ["Raw/a.bundle", "Textures/a.png"]


def test_source_fingerprint_reuses_compact_index_entry(tmp_path):
    manifests = tmp_path / "export-units"
    manifests.mkdir()
    (manifests / "Bundle-old.json").write_text(json.dumps({
        "logical_name": "A/old.chk",
        "identity": "old:10",
        "outputs": ["Raw/a.bundle"],
    }))
    (tmp_path / "export-unit-index.json").write_text(json.dumps({
        "format": 1,
        "source_units": {"Bundle-old": "content:2:10"},
    }))
    current = manifests / "Bundle-new.json"

    assert state.reuse_source_fingerprint(
        manifests, current, "content:2:10", "1.1.0", "B/new.chk", "new:11"
    )
    assert json.loads(current.read_text())["outputs"] == ["Raw/a.bundle"]


def test_build_unit_index_keeps_only_reusable_safe_units(tmp_path):
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    (manifests / "Bundle-a.json").write_text(json.dumps({
        "logical_name": "A/new.chk", "identity": "new:3", "outputs": ["Raw/a"]
    }))
    (manifests / "Bundle-empty.json").write_text(json.dumps({
        "logical_name": "A/empty.chk", "identity": "empty:0", "outputs": []
    }))
    (manifests / "Table-a.json").write_text(json.dumps({
        "logical_name": "T/a.chk", "identity": "table:1", "outputs": ["MasterData/a"]
    }))
    output = tmp_path / "export-unit-index.json"
    source_identities = tmp_path / "source-identities.tsv"
    source_identities.write_text(
        "Bundle\ta.chk\tAABB\t2\t10\nTable\ta.chk\tCCDD\t1\t5\n"
    )

    assert state.build_unit_index(manifests, output, source_identities) == 1

    index = json.loads(output.read_text())
    assert index["units"] == {"A/new.chk": "new:3"}
    assert index["logical_names"] == {"Bundle-a": "A/new.chk"}
    assert index["source_units"] == {"Bundle-a": "aabb:2:10"}


def test_manifest_logical_names_prefers_compact_index(tmp_path):
    manifests = tmp_path / "export-units"
    manifests.mkdir()
    (tmp_path / "export-unit-index.json").write_text(json.dumps({
        "format": 1,
        "units": {"A/new.chk": "new:3"},
        "logical_names": {"Bundle-a": "A/new.chk"},
    }))

    assert state.manifest_logical_names(manifests) == [("Bundle-a", "A/new.chk")]
