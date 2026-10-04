import importlib.util
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "preview_media", Path(__file__).parents[1] / "scripts/preview_media.py"
)
media = importlib.util.module_from_spec(spec)
spec.loader.exec_module(media)


def test_layout_strips_only_redundant_prefix(tmp_path):
    source = tmp_path / "Video/Data/Video/PC/Guide/PC/test.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"video")
    media.video_layout(tmp_path)
    assert (tmp_path / "Video/Guide/PC/test.mp4").read_bytes() == b"video"
    assert not (tmp_path / "Video/Data").exists()
    media.video_layout(tmp_path)


def test_layout_collision_preserves_both_files(tmp_path):
    source, destination = tmp_path / "a", tmp_path / "b"
    source.write_bytes(b"source")
    destination.write_bytes(b"target")
    with pytest.raises(RuntimeError, match="collision"):
        media.merge(source, destination)
    assert source.read_bytes() == b"source"
    assert destination.read_bytes() == b"target"


def test_video_layout_archives_old_collision_and_keeps_current_canonical(tmp_path, monkeypatch):
    source = tmp_path / "Video/Data/Video/PC/UI/test.mp4"
    destination = tmp_path / "Video/UI/test.mp4"
    source.parent.mkdir(parents=True)
    destination.parent.mkdir(parents=True)
    source.write_bytes(b"current")
    destination.write_bytes(b"old")
    monkeypatch.setenv("ZMD_VERSION", "1.5.3")

    media.video_layout(tmp_path)

    assert destination.read_bytes() == b"current"
    assert (tmp_path / "Raw/VideoHistory/before-1.5.3/UI/test.mp4").read_bytes() == b"old"


def test_video_layout_rewrites_export_unit_paths(tmp_path):
    manifest = tmp_path / ".baseline/export-units/Video-a.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"outputs":["Raw/a.usm","Video/Data/Video/PC/UI/a.mp4"],"output_sizes":{"Raw/a.usm":1,"Video/Data/Video/PC/UI/a.mp4":2}}')

    assert media.rewrite_video_unit_outputs(tmp_path) == 1
    value = __import__("json").loads(manifest.read_text())
    assert value["outputs"] == ["Raw/a.usm", "Video/UI/a.mp4"]
    assert value["output_sizes"]["Video/UI/a.mp4"] == 2


def test_compatible_requires_h264_yuv420p_and_aac():
    info = {"streams": [{"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p"}]}
    assert media.compatible_video(info)
    info["streams"][0]["codec_name"] = "vp9"
    assert not media.compatible_video(info)
    assert not media.compatible_video({})


def test_failed_video_keeps_original_and_removes_temporary(tmp_path, monkeypatch):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"original")
    monkeypatch.setattr(media, "probe", lambda _: {"streams": [{"codec_type": "video"}]})
    def fail(command, **kwargs):
        Path(command[-1]).write_bytes(b"partial")
        raise subprocess.TimeoutExpired(command, 1)
    monkeypatch.setattr(media, "checked_run", fail)
    with pytest.raises(subprocess.TimeoutExpired):
        media.transcode_video(source)
    assert source.read_bytes() == b"original"
    assert not (tmp_path / ".clip.preview-tmp.mp4").exists()


def test_audio_failure_skips_and_keeps_wem(tmp_path, monkeypatch):
    source = tmp_path / "Audio/sample.wem"
    source.parent.mkdir()
    source.write_bytes(b"original")
    def fail(*args):
        raise RuntimeError("unsupported codec")
    monkeypatch.setattr(media, "transcode_audio", fail)
    assert media.process(tmp_path, ("audio",)) == 1
    assert source.exists()
    assert (tmp_path / "media-preview-report.json").exists()


def test_audio_success_preserves_wem_in_raw(tmp_path, monkeypatch):
    source = tmp_path / "Audio/voice/chinese/test.wem"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"original")
    monkeypatch.setattr(media, "transcode_audio", lambda s, d: d.write_bytes(b"mp3"))
    assert media.process(tmp_path, ("audio",)) == 0
    assert source.with_suffix(".mp3").read_bytes() == b"mp3"
    assert (tmp_path / "Raw/AudioWEM/voice/chinese/test.wem").read_bytes() == b"original"


def test_changed_audio_replaces_preview_and_archives_previous_wem(tmp_path, monkeypatch):
    source = tmp_path / "Audio/voice/chinese/test.wem"
    preserved = tmp_path / "Raw/AudioWEM/voice/chinese/test.wem"
    source.parent.mkdir(parents=True)
    preserved.parent.mkdir(parents=True)
    source.write_bytes(b"new")
    source.with_suffix(".mp3").write_bytes(b"old-mp3")
    preserved.write_bytes(b"old")
    monkeypatch.setenv("ZMD_VERSION", "1.5.4")
    monkeypatch.setattr(media, "transcode_audio", lambda s, d: d.write_bytes(b"new-mp3"))

    assert media.process(tmp_path, ("audio",)) == 0
    assert source.with_suffix(".mp3").read_bytes() == b"new-mp3"
    assert preserved.read_bytes() == b"new"
    assert (tmp_path / "Raw/AudioWEMHistory/before-1.5.4/voice/chinese/test.wem").read_bytes() == b"old"


def test_unchanged_audio_reuses_existing_preview(tmp_path, monkeypatch):
    source = tmp_path / "Audio/test.wem"
    preserved = tmp_path / "Raw/AudioWEM/test.wem"
    source.parent.mkdir(parents=True)
    preserved.parent.mkdir(parents=True)
    source.write_bytes(b"same")
    preserved.write_bytes(b"same")
    source.with_suffix(".mp3").write_bytes(b"mp3")
    monkeypatch.setattr(media, "validate_decode", lambda _: None)
    monkeypatch.setattr(media, "probe", lambda _: {"streams": [{"codec_type": "audio", "codec_name": "mp3"}]})
    monkeypatch.setattr(media, "transcode_audio", lambda *_: pytest.fail("must not transcode"))

    assert media.process(tmp_path, ("audio",)) == 0
    assert not source.exists()
    assert preserved.read_bytes() == b"same"
