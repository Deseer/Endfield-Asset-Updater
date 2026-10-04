import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "missing_json_units", Path(__file__).parents[1] / "scripts/missing-json-units.py"
)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def test_only_missing_json_chunks_are_forced(tmp_path):
    existing = tmp_path / "Data/Json/LevelConfig/old.json"
    existing.parent.mkdir(parents=True)
    existing.write_text("{}")
    lines = [
        "JsonData\tunchanged.chk\tData/Json/LevelConfig/old.json\t2\n",
        "JsonData\tmissing.chk\tData/Json/LevelConfig/map02_lv009.json\t10\n",
        "JsonData\tmissing.chk\tData/Json/LevelData/new.json\t10\n",
        "Bundle\tignored.chk\tData/Bundles/new.ab\t10\n",
        "[Table] (not present, skipped)\n",
    ]
    assert audit.missing_units(lines, tmp_path) == {"JsonData-missing"}


def test_empty_audit_does_not_force_anything(tmp_path):
    assert audit.missing_units([], tmp_path) == set()


@pytest.mark.parametrize("root", audit.CRITICAL_CONFIG_ROOTS)
def test_existing_stale_config_forces_only_owning_chunk(tmp_path, root):
    existing = tmp_path / root / "LevelBasicInfoTable.json"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"{}")
    lines = [f"JsonData\tstale.chk\t{root}LevelBasicInfoTable.json\t58686\n"]
    assert audit.missing_units(lines, tmp_path) == {"JsonData-stale"}


def test_noncritical_existing_files_do_not_incur_stats(tmp_path, monkeypatch):
    existing = tmp_path / "Data/Json/LevelData/existing.json"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"{}")

    def forbidden_stat(*args, **kwargs):
        raise AssertionError("noncritical audit must not stat each file")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "stat", forbidden_stat)
        assert audit.missing_units(
            ["JsonData\tx.chk\tData/Json/LevelData/existing.json\t999"], tmp_path
        ) == set()


def test_case_only_json_name_difference_is_not_missing(tmp_path):
    existing = tmp_path / "Data/Json/BuffData/buff_enemy_fullfire.json"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"{}")

    assert audit.missing_units(
        ["JsonData\tx.chk\tData/Json/BuffData/buff_enemy_FullFire.json\t2"],
        tmp_path,
    ) == set()


def test_audit_rejects_traversal(tmp_path):
    with pytest.raises(ValueError):
        audit.missing_units(["JsonData\tx.chk\t../outside.json\t2"], tmp_path)
