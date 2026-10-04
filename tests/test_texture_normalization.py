import importlib.util
import struct
from pathlib import Path


ROOT = Path(__file__).parents[1]


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


textures = load_script("normalize-texture-names")
maps = load_script("normalize-map-texture-layers")
unit_state = load_script("export-unit-state")


def fake_png(path: Path, width: int, height: int, payload: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", width, height) + b"x" * payload
    )


def test_character_names_converge_across_legacy_and_pathid_exports(tmp_path):
    root = tmp_path / "Textures"
    char = root / "chr_thumb"
    fake_png(char / "chr_0003_endminf.png", 2048, 2048)
    fake_png(char / "chr_0003_endminf_0123456789abcdef.png", 312, 552)
    fake_png(char / "chr_0003_endminf_variant_02.png", 236, 1352)

    groups = textures.discover(root)
    base = char / "chr_0003_endminf.png"
    targets = {target.name for _, target, *_ in textures.destinations(base, groups[base])}

    assert targets == {
        "chr_0003_endminf.png",
        "chr_0003_endminf_large.png",
        "chr_0003_endminf_strip.png",
    }


def test_non_manager_keeps_silhouette_bare_and_full_art_large(tmp_path):
    root = tmp_path / "Textures"
    char = root / "chr_thumb"
    fake_png(char / "chr_0004_pelica.png", 312, 552)
    fake_png(char / "chr_0004_pelica_0123456789abcdef.png", 2048, 2048)

    groups = textures.discover(root)
    base = char / "chr_0004_pelica.png"
    targets = {target.name for _, target, *_ in textures.destinations(base, groups[base])}

    assert targets == {"chr_0004_pelica.png", "chr_0004_pelica_large.png"}


def test_map_sprite_provenance_beats_larger_shader_texture(tmp_path):
    texture_root = tmp_path / "other"
    mask = texture_root / "h_map02_lv009_1_1.png"
    terrain = texture_root / "h_map02_lv009_1_1_variant_02.png"
    fake_png(mask, 600, 600, payload=1000)
    fake_png(terrain, 600, 600, payload=10)

    plan = maps.destinations(texture_root, "h_map02_lv009_1_1", [mask, terrain], terrain)

    assert plan[0] == (terrain, mask)
    assert plan[1][1].name == "h_map02_lv009_1_1_aux_600x600.png"


def test_map_without_provenance_is_not_guessed(tmp_path):
    texture_root = tmp_path / "other"
    first = texture_root / "h_map02_lv009_1_1.png"
    second = texture_root / "h_map02_lv009_1_1_variant_02.png"
    fake_png(first, 600, 600, payload=10)
    fake_png(second, 600, 600, payload=1000)

    assert maps.destinations(texture_root, "h_map02_lv009_1_1", [first, second]) == []


def test_converged_map_plan_still_has_protected_targets(tmp_path):
    texture_root = tmp_path / "other"
    canonical = texture_root / "h_map02_lv009_1_1.png"
    auxiliary = texture_root / "h_map02_lv009_1_1_aux_600x600.png"
    fake_png(canonical, 600, 600, payload=10)
    fake_png(auxiliary, 600, 600, payload=1000)

    plan = maps.destinations(
        texture_root, "h_map02_lv009_1_1", [canonical, auxiliary], canonical
    )

    assert plan == [(canonical, canonical), (auxiliary, auxiliary)]


def test_map_manifest_targets_are_protected_from_unit_pruning(tmp_path):
    manifest = tmp_path / "map-texture-selection.json"
    manifest.write_text(
        '[{"source":"h_map02_lv009_1_1_variant_02.png",'
        '"target":"h_map02_lv009_1_1.png"}]'
    )

    assert unit_state.protected_map_outputs(manifest) == {
        "Textures/other/h_map02_lv009_1_1.png"
    }


def test_incremental_unit_carries_forward_still_present_outputs(tmp_path):
    root = tmp_path / "data"
    kept = root / "Textures/other/h_map02_lv009_1_1.png"
    kept.parent.mkdir(parents=True)
    kept.write_bytes(b"map")
    manifest = tmp_path / "unit.json"
    manifest.write_text(
        '{"outputs":["Textures/other/h_map02_lv009_1_1.png",'
        '"Textures/other/deleted.png"]}'
    )
    outputs = tmp_path / "outputs.txt"
    outputs.write_text("Raw/current.ab\n")

    assert unit_state.carry_forward_outputs(manifest, outputs, root) == 1
    assert unit_state.output_paths(outputs) == [
        "Raw/current.ab",
        "Textures/other/h_map02_lv009_1_1.png",
    ]
