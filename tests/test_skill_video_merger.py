from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "video-merger"
    / "src"
    / "video_merger.py"
)


def _import_video_merger():
    spec = importlib.util.spec_from_file_location("video_merger_module", MODULE_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _create_merger(mod):
    with patch.object(mod.VideoMerger, "_check_dependencies"):
        return mod.VideoMerger(ffmpeg_path="ffmpeg", ffprobe_path="ffprobe")


def test_get_video_info_numeric_stream_duration():
    mod = _import_video_merger()
    merger = _create_merger(mod)

    mock_res = MagicMock(stdout="1920\n1080\n12.5\n")
    with patch("subprocess.run", return_value=mock_res) as mock_run:
        w, h, d = merger.get_video_info("sample.mp4")
        assert (w, h, d) == (1920, 1080, 12.5)
        assert mock_run.call_count == 1


def test_get_video_info_fallback_on_na_stream_duration():
    mod = _import_video_merger()
    merger = _create_merger(mod)

    res_stream = MagicMock(stdout="1920\n1080\nN/A\n")
    res_format = MagicMock(stdout="15.0\n")

    with patch("subprocess.run", side_effect=[res_stream, res_format]) as mock_run:
        w, h, d = merger.get_video_info("sample.mp4")
        assert (w, h, d) == (1920, 1080, 15.0)
        assert mock_run.call_count == 2
        fallback_cmd = mock_run.call_args_list[1].args[0]
        assert "format=duration" in fallback_cmd


def test_get_video_info_fallback_on_missing_stream_duration():
    mod = _import_video_merger()
    merger = _create_merger(mod)

    res_stream = MagicMock(stdout="1280\n720\n")
    res_format = MagicMock(stdout="8.5\n")

    with patch("subprocess.run", side_effect=[res_stream, res_format]) as mock_run:
        w, h, d = merger.get_video_info("sample.mp4")
        assert (w, h, d) == (1280, 720, 8.5)
        assert mock_run.call_count == 2


def test_get_video_info_raises_when_format_duration_unavailable():
    mod = _import_video_merger()
    merger = _create_merger(mod)

    res_stream = MagicMock(stdout="1920\n1080\nN/A\n")
    res_format = MagicMock(stdout="N/A\n")

    with patch("subprocess.run", side_effect=[res_stream, res_format]):
        with pytest.raises(ValueError, match="无法获取视频时长"):
            merger.get_video_info("corrupt.mp4")


def test_get_video_info_raises_when_dimensions_missing():
    mod = _import_video_merger()
    merger = _create_merger(mod)

    res_empty = MagicMock(stdout="\n")
    with patch("subprocess.run", return_value=res_empty):
        with pytest.raises(ValueError, match="无法获取视频尺寸"):
            merger.get_video_info("empty.mp4")


def test_has_audio_stream():
    mod = _import_video_merger()
    merger = _create_merger(mod)

    res_with_audio = MagicMock(stdout="aac\n")
    res_silent = MagicMock(stdout="")

    with patch("subprocess.run", side_effect=[res_with_audio, res_silent]):
        assert merger.has_audio_stream("with_audio.mp4") is True
        assert merger.has_audio_stream("silent.mp4") is False
        # Also test private alias
        assert hasattr(merger, "_has_audio_stream")


def test_merge_uses_an_for_silent_video(tmp_path: Path):
    mod = _import_video_merger()
    merger = _create_merger(mod)

    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    (input_dir / "01_clip.mp4").write_bytes(b"video-1")
    (input_dir / "02_clip.mp4").write_bytes(b"video-2")
    out_file = tmp_path / "out.mp4"

    executed_commands: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        executed_commands.append(list(cmd))
        if out_file.name in str(cmd):
            out_file.write_bytes(b"rendered")
        return MagicMock(stdout="")

    with (
        patch.object(merger, "get_video_info", return_value=(1280, 720, 10.0)),
        patch.object(merger, "has_audio_stream", return_value=False),
        patch("subprocess.run", side_effect=fake_run),
    ):
        success = merger.merge(str(input_dir), str(out_file))
        assert success is True
        final_cmd = [c for c in executed_commands if str(out_file) in c][0]
        assert "-an" in final_cmd
        assert "-af" not in final_cmd
        assert "-c:a" not in final_cmd


def test_merge_includes_audio_filters_when_audio_present(tmp_path: Path):
    mod = _import_video_merger()
    merger = _create_merger(mod)

    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    (input_dir / "01_clip.mp4").write_bytes(b"video-1")
    (input_dir / "02_clip.mp4").write_bytes(b"video-2")
    out_file = tmp_path / "out.mp4"

    executed_commands: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        executed_commands.append(list(cmd))
        if out_file.name in str(cmd):
            out_file.write_bytes(b"rendered")
        return MagicMock(stdout="")

    with (
        patch.object(merger, "get_video_info", return_value=(1280, 720, 10.0)),
        patch.object(merger, "has_audio_stream", return_value=True),
        patch("subprocess.run", side_effect=fake_run),
    ):
        success = merger.merge(str(input_dir), str(out_file))
        assert success is True
        final_cmd = [c for c in executed_commands if str(out_file) in c][0]
        assert "-an" not in final_cmd
        assert "-af" in final_cmd
        assert "-c:a" in final_cmd


def test_merge_single_chunk_silent_uses_an(tmp_path: Path):
    mod = _import_video_merger()
    merger = _create_merger(mod)

    video_list = ["clip1.mp4", "clip2.mp4"]
    out_file = tmp_path / "chunk_01.mp4"

    executed_commands: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        executed_commands.append(list(cmd))
        return MagicMock(stdout="")

    with (
        patch.object(merger, "get_video_info", return_value=(1920, 1080, 30.0)),
        patch.object(merger, "has_audio_stream", return_value=False),
        patch("subprocess.run", side_effect=fake_run),
    ):
        success = merger._merge_single_chunk(
            video_list, str(out_file), "1920x1080", 0.5, 24, 22, "medium"
        )
        assert success is True
        final_cmd = [c for c in executed_commands if str(out_file) in c][0]
        assert "-an" in final_cmd
        assert "-af" not in final_cmd
