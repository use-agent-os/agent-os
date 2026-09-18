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


def test_merge_creates_nested_output_directory(tmp_path: Path):
    mod = _import_video_merger()
    merger = _create_merger(mod)

    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    (input_dir / "01_clip.mp4").write_bytes(b"data")
    (input_dir / "02_clip.mp4").write_bytes(b"data")

    nested_out = tmp_path / "nested" / "output" / "dir" / "final.mp4"
    assert not nested_out.parent.exists()

    def fake_run(cmd, *args, **kwargs):
        if str(nested_out) in cmd:
            nested_out.write_bytes(b"dummy mp4")
        return MagicMock(stdout="")

    with (
        patch.object(merger, "get_video_info", return_value=(1280, 720, 10.0)),
        patch("subprocess.run", side_effect=fake_run),
    ):
        success = merger.merge(str(input_dir), str(nested_out))
        assert success is True
        assert nested_out.parent.exists()
        assert nested_out.exists()


def test_merge_single_chunk_creates_nested_output_directory(tmp_path: Path):
    mod = _import_video_merger()
    merger = _create_merger(mod)

    input_clips = [str(tmp_path / "clip_01.mp4")]
    nested_chunk = tmp_path / "chunks" / "sub" / "chunk_001.mp4"
    assert not nested_chunk.parent.exists()

    def fake_run(cmd, *args, **kwargs):
        if str(nested_chunk) in cmd:
            nested_chunk.write_bytes(b"dummy chunk")
        return MagicMock(stdout="")

    with (
        patch.object(merger, "get_video_info", return_value=(1280, 720, 10.0)),
        patch("subprocess.run", side_effect=fake_run),
    ):
        success = merger._merge_single_chunk(input_clips, str(nested_chunk), "1280x720")
        assert success is True
        assert nested_chunk.parent.exists()
        assert nested_chunk.exists()
