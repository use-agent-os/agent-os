"""Tests for the bundled video-merger skill's handling of AI-generated clips
that lack per-stream duration metadata or audio tracks."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src" / "agentos" / "skills" / "bundled" / "video-merger" / "src"


def _load_video_merger_module():
    spec = importlib.util.spec_from_file_location(
        "video_merger_skill_src", SRC_DIR / "video_merger.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


video_merger = _load_video_merger_module()
VideoMerger = video_merger.VideoMerger


def _completed(stdout: str = ""):
    result = MagicMock()
    result.stdout = stdout
    return result


def _sequential(*responses):
    """Return a subprocess.run side_effect that yields responses in order."""
    it = iter(responses)
    return lambda *a, **k: next(it)


@pytest.fixture
def merger():
    with patch.object(video_merger.subprocess, "run", return_value=_completed()):
        return VideoMerger()


class TestGetVideoInfoDurationFallback:
    def test_uses_stream_duration_when_present(self, merger):
        with patch.object(
            video_merger.subprocess, "run", return_value=_completed("1920\n1080\n12.5\n")
        ) as run:
            width, height, duration = merger.get_video_info("clip.mp4")
        assert (width, height, duration) == (1920, 1080, 12.5)
        assert run.call_count == 1

    def test_falls_back_to_format_duration_when_stream_duration_is_na(self, merger):
        side_effect = _sequential(
            _completed("1920\n1080\nN/A\n"),
            _completed("7.84\n"),
        )
        with patch.object(video_merger.subprocess, "run", side_effect=side_effect) as run:
            width, height, duration = merger.get_video_info("sora_clip.mp4")
        assert (width, height, duration) == (1920, 1080, 7.84)
        assert run.call_count == 2
        fallback_cmd = run.call_args_list[1].args[0]
        assert "-show_entries" in fallback_cmd
        assert "format=duration" in fallback_cmd

    def test_falls_back_when_stream_duration_line_is_missing_entirely(self, merger):
        side_effect = _sequential(
            _completed("1920\n1080\n"),
            _completed("3.2\n"),
        )
        with patch.object(video_merger.subprocess, "run", side_effect=side_effect):
            width, height, duration = merger.get_video_info("clip.mp4")
        assert (width, height, duration) == (1920, 1080, 3.2)

    def test_raises_clear_error_when_format_duration_also_unavailable(self, merger):
        side_effect = _sequential(
            _completed("1920\n1080\nN/A\n"),
            _completed("N/A\n"),
        )
        with patch.object(video_merger.subprocess, "run", side_effect=side_effect):
            with pytest.raises(ValueError, match="无法获取视频时长"):
                merger.get_video_info("broken.mp4")


class TestHasAudioStream:
    def test_true_when_ffprobe_reports_an_audio_index(self, merger):
        with patch.object(video_merger.subprocess, "run", return_value=_completed("0\n")):
            assert merger._has_audio_stream("with_audio.mp4") is True

    def test_false_when_ffprobe_reports_nothing(self, merger):
        with patch.object(video_merger.subprocess, "run", return_value=_completed("")):
            assert merger._has_audio_stream("silent.mp4") is False


class TestMergeSkipsAudioFilterForSilentInputs:
    def test_final_ffmpeg_command_omits_audio_flags_when_no_audio_stream(self, merger, tmp_path):
        input_dir = tmp_path / "clips"
        input_dir.mkdir()
        (input_dir / "1_a.mp4").write_bytes(b"fake")
        (input_dir / "2_b.mp4").write_bytes(b"fake")
        output_path = tmp_path / "out.mp4"

        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if "ffprobe" in cmd[0]:
                if "-select_streams" in cmd and "a" in cmd:
                    return _completed("")  # no audio stream
                return _completed("1920\n1080\n5.0\n")
            return _completed()

        with (
            patch.object(video_merger.subprocess, "run", side_effect=fake_run),
            patch("os.path.exists", return_value=True),
            patch("os.path.getsize", return_value=1024),
        ):
            ok = merger.merge(str(input_dir), str(output_path))

        assert ok is True
        final_cmd = calls[-1]
        assert "-af" not in final_cmd
        assert "-c:a" not in final_cmd
        assert "-an" in final_cmd

    def test_keeps_audio_flags_when_audio_stream_present(self, merger, tmp_path):
        input_dir = tmp_path / "clips"
        input_dir.mkdir()
        (input_dir / "1_a.mp4").write_bytes(b"fake")
        output_path = tmp_path / "out.mp4"

        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if "ffprobe" in cmd[0]:
                if "-select_streams" in cmd and "a" in cmd:
                    return _completed("0\n")  # has audio stream
                return _completed("1920\n1080\n5.0\n")
            return _completed()

        with (
            patch.object(video_merger.subprocess, "run", side_effect=fake_run),
            patch("os.path.exists", return_value=True),
            patch("os.path.getsize", return_value=1024),
        ):
            ok = merger.merge(str(input_dir), str(output_path))

        assert ok is True
        final_cmd = calls[-1]
        assert "-af" in final_cmd
        assert "-c:a" in final_cmd
        assert "-an" not in final_cmd
