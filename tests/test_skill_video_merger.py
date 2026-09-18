from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

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


def test_get_video_info_numeric_duration():
    mod = _import_video_merger()
    merger = _create_merger(mod)

    mock_res = MagicMock()
    mock_res.stdout = "1920\n1080\n12.5\n"

    with patch("subprocess.run", return_value=mock_res) as mock_run:
        w, h, d = merger.get_video_info("sample.mp4")
        assert w == 1920
        assert h == 1080
        assert d == 12.5
        assert mock_run.call_count == 1


def test_get_video_info_na_duration_fallback():
    mod = _import_video_merger()
    merger = _create_merger(mod)

    res_stream = MagicMock()
    res_stream.stdout = "1920\n1080\nN/A\n"

    res_format = MagicMock()
    res_format.stdout = "15.0\n"

    with patch("subprocess.run", side_effect=[res_stream, res_format]) as mock_run:
        w, h, d = merger.get_video_info("sample.mp4")
        assert w == 1920
        assert h == 1080
        assert d == 15.0
        assert mock_run.call_count == 2
        args, _ = mock_run.call_args_list[1]
        assert "format=duration" in args[0]


def test_has_audio_stream():
    mod = _import_video_merger()
    merger = _create_merger(mod)

    res_with_audio = MagicMock(stdout="audio\n")
    res_no_audio = MagicMock(stdout="")

    with patch("subprocess.run", side_effect=[res_with_audio, res_no_audio]):
        assert merger.has_audio_stream("video_with_audio.mp4") is True
        assert merger.has_audio_stream("silent_video.mp4") is False


def test_merge_uses_an_for_silent_video(tmp_path: Path):
    mod = _import_video_merger()
    merger = _create_merger(mod)

    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    (input_dir / "01_clip.mp4").write_bytes(b"data")
    (input_dir / "02_clip.mp4").write_bytes(b"data")
    out_file = tmp_path / "out.mp4"

    commands_executed: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        commands_executed.append(cmd)
        if "-f" in cmd and "concat" in cmd:
            pass
        elif out_file.name in str(cmd):
            out_file.write_bytes(b"merged-content")
        return MagicMock(stdout="")

    with (
        patch.object(merger, "get_video_info", return_value=(1280, 720, 10.0)),
        patch.object(merger, "has_audio_stream", return_value=False),
        patch("subprocess.run", side_effect=fake_run),
    ):
        success = merger.merge(str(input_dir), str(out_file))
        assert success is True
        final_cmd = [c for c in commands_executed if str(out_file) in c][0]
        assert "-an" in final_cmd
        assert "-af" not in final_cmd
