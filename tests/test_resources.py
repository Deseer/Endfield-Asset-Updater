import hashlib
from pathlib import Path

from zmd_resource_service.resources import (
    FullAction,
    PatchAction,
    apply_full_plan,
    apply_patch_plan,
    build_patch_plan,
    major_minor,
    resource_token,
    resource_snapshot,
)
from zmd_resource_service import resources as resource_module


def test_resource_snapshot_ignores_order_and_urls_but_detects_content():
    resources = {"res_version": "r1"}
    files = [{"name": "VFS/A/a.chk", "md5": "ABC", "size": 3},
             {"name": "VFS/A/b.chk", "md5": "DEF", "size": 4}]
    before = resource_snapshot(resources, {"main": {"files": files}})
    assert before == resource_snapshot(resources, {"main": {"files": list(reversed(files)), "url": "ignored"}})
    files[0]["md5"] = "000"
    assert before != resource_snapshot(resources, {"main": {"files": files}})


def test_uncached_same_size_wrong_content_is_not_current(tmp_path):
    (tmp_path / "a.chk").write_bytes(b"old")
    resources = {"resources": [{"name": "main", "version": "r1", "path": "https://cdn.example/files"}]}
    indexes = {"main": {"files": [{"name": "VFS/a.chk", "md5": hashlib.md5(b"new").hexdigest(), "size": 3}]}}
    _, full, _, current = build_patch_plan(tmp_path, resources, {"main": {"files": []}}, indexes)
    assert len(full) == 1
    assert current == 0


def test_verified_uncached_file_adds_persistent_identity(tmp_path):
    (tmp_path / "a.chk").write_bytes(b"new")
    resources = {"resources": [{"name": "main", "version": "r1", "path": "https://cdn.example/files"}]}
    digest = hashlib.md5(b"new").hexdigest()
    indexes = {"main": {"files": [{"name": "VFS/a.chk", "md5": digest, "size": 3}]}}
    cache = {}
    _, full, _, current = build_patch_plan(tmp_path, resources, {"main": {"files": []}}, indexes, cache)
    assert not full and current == 1
    assert cache["files"][0]["md5"] == digest


def test_exported_identity_skips_missing_vfs_even_when_chunk_name_changes(tmp_path):
    digest = hashlib.md5(b"compressed").hexdigest()
    resources = {"resources": [{"name": "main", "version": "r2", "path": "https://cdn"}]}
    indexes = {"main": {"files": [{
        "name": "VFS/repacked/new-name.chk", "md5": digest, "size": 10,
    }]}}

    patches, full, unavailable, current = build_patch_plan(
        tmp_path,
        resources,
        {"main": {"files": []}},
        indexes,
        exported_units={"old/name.chk": f"{digest}:10"},
        exported_identities={f"{digest}:10"},
    )

    assert (patches, full, unavailable, current) == ([], [], [], 1)


def test_resource_helpers():
    payload = {
        "version": "1.4.4",
        "pkg": {"file_path": "https://cdn.example/1.4.4_example-token/files"},
    }
    assert major_minor(payload["version"]) == "1.4"
    assert resource_token(payload) == "example-token"


def test_patch_resume_skips_completed_target_without_old_base(tmp_path, monkeypatch):
    import json
    content = b"patched"
    vfs = tmp_path / "vfs"
    vfs.mkdir()
    (vfs / "new.chk").write_bytes(content)
    digest = hashlib.md5(content).hexdigest()
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({"files": [{
        "logical_name": "new.chk", "storage_name": "new.chk",
        "md5": digest, "size": len(content),
    }]}))
    action = PatchAction(
        resource="main", resource_path="https://cdn.example", target_name="new.chk",
        target_md5=digest, target_size=len(content), storage_name="old.chk",
        base_name="old.chk", base_md5="obsolete", base_size=3,
        patch_path="diff.patch", patch_size=1,
    )

    def no_download(*_):
        raise AssertionError("completed patch must not download or require its old base")

    monkeypatch.setattr(resource_module, "_download_patch", no_download)
    result = apply_patch_plan([action], vfs, tmp_path / "scratch", cache, "r2", 512)
    assert result["files"][0]["md5"] == digest


def test_plan_deduplicates_groups_and_selects_local_base(tmp_path: Path):
    old = b"old resource data"
    old_md5 = hashlib.md5(old).hexdigest()
    base = tmp_path / "block" / "old.chk"
    base.parent.mkdir()
    base.write_bytes(old)
    resources = {
        "resources": [
            {"name": "main", "version": "2", "path": "https://cdn.example/main/files"},
            {"name": "initial", "version": "2", "path": "https://cdn.example/initial/files"},
        ]
    }
    item = {
        "name": "block/new.chk",
        "md5": "1" * 32,
        "size": 20,
        "diffType": 1,
        "patch": [
            {
                "base_file": "block/old.chk",
                "base_md5": old_md5,
                "base_size": len(old),
                "patch": "diff/one.patch",
                "patch_size": 12,
            }
        ],
    }
    indexes = {
        "main": {"files": []},
        "initial": {"files": []},
    }
    actions, full_actions, unavailable, current = build_patch_plan(
        tmp_path,
        resources,
        {"main": {"files": [item]}, "initial": {"files": [item]}},
        indexes,
    )
    assert len(actions) == 1
    assert full_actions == []
    assert actions[0].storage_name == "block/old.chk"
    assert unavailable == []
    assert current == 0


def test_apply_patch_keeps_canonical_storage_and_removes_patch(tmp_path: Path, monkeypatch):
    old = b"old data" * 100
    new = b"new data" * 100
    patch = b"test patch"
    base = tmp_path / "vfs" / "block" / "old.chk"
    base.parent.mkdir(parents=True)
    base.write_bytes(old)
    action = PatchAction(
        resource="main",
        resource_path="https://cdn.example/files",
        target_name="block/new.chk",
        target_md5=hashlib.md5(new).hexdigest(),
        target_size=len(new),
        storage_name="block/old.chk",
        base_name="block/old.chk",
        base_md5=hashlib.md5(old).hexdigest(),
        base_size=len(old),
        patch_path="diff/test.patch",
        patch_size=len(patch),
    )
    scratch = tmp_path / "scratch"

    def fake_download(_action, destination):
        destination.mkdir(parents=True, exist_ok=True)
        result = destination / "test.patch"
        result.write_bytes(patch)
        return result

    def fake_hpatchz(_base, _patch, output):
        output.write_bytes(new)

    monkeypatch.setattr("zmd_resource_service.resources._download_patch", fake_download)
    monkeypatch.setattr("zmd_resource_service.resources._run_hpatchz", fake_hpatchz)
    cache = apply_patch_plan(
        [action], tmp_path / "vfs", scratch, tmp_path / "cache.json", "2", 512
    )
    target = tmp_path / "vfs" / "block" / "new.chk"
    assert target.read_bytes() == new
    assert not base.exists()
    assert not (scratch / "test.patch").exists()
    assert cache["files"][0]["logical_name"] == "block/new.chk"


def test_plan_uses_full_file_without_local_base(tmp_path: Path):
    resources = {
        "resources": [
            {"name": "main", "version": "2", "path": "https://cdn.example/main/files"}
        ]
    }
    item = {
        "name": "block/new.chk",
        "md5": "1" * 32,
        "size": 20,
        "patch": [],
    }
    index = {
        "files": [
            {
                "name": "VFS/block/new.chk",
                "md5": "1" * 32,
                "size": 20,
            }
        ]
    }
    patches, full, unavailable, current = build_patch_plan(
        tmp_path,
        resources,
        {"main": {"files": [item]}},
        {"main": index},
    )
    assert patches == []
    assert len(full) == 1
    assert full[0].file_url == "https://cdn.example/main/files/VFS/block/new.chk"
    assert unavailable == []
    assert current == 0


def test_plan_finds_full_fallback_in_another_resource_group(tmp_path: Path):
    resources = {
        "resources": [
            {"name": "main", "version": "2", "path": "https://cdn.example/main/files"},
            {"name": "initial", "version": "2", "path": "https://cdn.example/initial/files"},
        ]
    }
    item = {"name": "block/new.chk", "md5": "1" * 32, "size": 20, "patch": []}
    indexes = {
        "main": {"files": []},
        "initial": {
            "files": [
                {"name": "VFS/block/new.chk", "md5": "1" * 32, "size": 20}
            ]
        },
    }
    patches, full, unavailable, current = build_patch_plan(
        tmp_path,
        resources,
        {"main": {"files": [item]}, "initial": {"files": [item]}},
        indexes,
    )
    assert patches == []
    assert unavailable == []
    assert current == 0
    assert len(full) == 1
    assert full[0].resource == "initial"
    assert full[0].file_url == "https://cdn.example/initial/files/VFS/block/new.chk"


def test_apply_full_plan_moves_verified_file(tmp_path: Path, monkeypatch):
    content = b"complete resource"
    action = FullAction(
        resource="main",
        resource_path="https://cdn.example/files",
        target_name="block/new.chk",
        remote_name="VFS/block/new.chk",
        target_md5=hashlib.md5(content).hexdigest(),
        target_size=len(content),
    )

    def fake_download(_action, vfs_root, scratch, retries=5, progress=print):
        target = vfs_root / _action.target_name
        target.parent.mkdir(parents=True)
        target.write_bytes(content)
        return target

    monkeypatch.setattr("zmd_resource_service.resources._download_full", fake_download)
    cache = apply_full_plan(
        [action], tmp_path / "vfs", tmp_path / "scratch", tmp_path / "cache.json", "2"
    )
    assert (tmp_path / "vfs" / "block" / "new.chk").read_bytes() == content
    assert cache["files"][0]["storage_name"] == "block/new.chk"


def test_plan_downloads_index_only_chunk_not_just_blc(tmp_path: Path):
    resources = {
        "resources": [
            {"name": "main", "version": "2", "path": "https://cdn.example/main/files"}
        ]
    }
    index = {
        "files": [
            {
                "name": "VFS/voice/language.chk",
                "md5": "2" * 32,
                "size": 1234,
            }
        ]
    }
    patches, full, unavailable, current = build_patch_plan(
        tmp_path,
        resources,
        {"main": {"files": []}},
        {"main": index},
    )
    assert patches == []
    assert unavailable == []
    assert current == 0
    assert len(full) == 1
    assert full[0].target_name == "voice/language.chk"


def test_plan_trusts_verified_cache_without_rehashing_large_file(tmp_path: Path, monkeypatch):
    content = b"verified"
    digest = hashlib.md5(content).hexdigest()
    target = tmp_path / "block/current.chk"
    target.parent.mkdir(parents=True)
    target.write_bytes(content)
    item = {"name": "block/current.chk", "md5": digest, "size": len(content), "patch": []}
    resources = {"resources": [{"name": "main", "version": "2", "path": "https://cdn"}]}
    cache = {"files": [{
        "logical_name": "block/current.chk",
        "storage_name": "block/current.chk",
        "md5": digest,
        "size": len(content),
    }]}
    monkeypatch.setattr(
        "zmd_resource_service.resources.md5_file",
        lambda _: (_ for _ in ()).throw(AssertionError("must not rehash verified cache")),
    )

    patches, full, unavailable, current = build_patch_plan(
        tmp_path, resources, {"main": {"files": [item]}}, {"main": {"files": []}}, cache
    )
    assert (patches, full, unavailable, current) == ([], [], [], 1)


def test_full_download_recovers_complete_partial_without_http(tmp_path: Path, monkeypatch):
    content = b"already fully downloaded"
    action = FullAction(
        resource="main",
        resource_path="https://cdn.example/files",
        target_name="block/complete.chk",
        remote_name="VFS/block/complete.chk",
        target_md5=hashlib.md5(content).hexdigest(),
        target_size=len(content),
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    digest = hashlib.sha256(action.file_url.encode("utf-8")).hexdigest()[:20]
    (scratch / f"{digest}.full.part").write_bytes(content)
    monkeypatch.setattr(
        resource_module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("HTTP must not run")),
    )

    target = resource_module._download_full(action, tmp_path / "vfs", scratch)
    assert target.read_bytes() == content
    assert not (scratch / f"{digest}.full.part").exists()
