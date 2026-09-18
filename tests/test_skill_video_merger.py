"""Tests for bundled video-merger skill — path escaping, utf-8 concat lists, and round-trips."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from agentos.skills.loader import SkillLoader

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "src" / "agentos" / "skills" / "bundled"
VIDEO_MERGER_DIR = BUNDLED / "video-merger"
SCRIPTS = VIDEO_MERGER_DIR / "scripts"
SRC = VIDEO_MERGER_DIR / "src"


def _spec_to_loader() -> object:
    return SkillLoader(bundled_dir=BUNDLED).get_by_name("video-merger")


def test_video_merger_skill_loads() -> None:
    spec = _spec_to_loader()
    assert spec is not None
    assert spec.name == "video-merger"
    assert spec.metadata is not None
    assert spec.provenance.origin == "clawhub-mit0"
    assert spec.provenance.license == "MIT-0"


def test_format_concat_entry_normalizes_windows_backslashes_and_quotes() -> None:
    sys.path.insert(0, str(SRC))
    try:
        from video_merger import _format_concat_entry
    finally:
        sys.path.pop(0)

    # Simple path with backslashes
    entry = _format_concat_entry(r"C:\videos\scene1\1_start.mp4")
    assert entry.startswith("file '")
    assert entry.endswith("'\n")
    # Backslashes must be normalized to forward slashes for ffmpeg concat demuxer
    assert "\\" not in entry
    assert "C:/videos/scene1/1_start.mp4" in entry

    # Path containing single quotes
    entry_quotes = _format_concat_entry(r"C:\videos\user's cut\1_scene.mp4")
    assert r"user'\''s cut" in entry_quotes
    assert "\\" not in entry_quotes.replace(r"'\''", "")

    # Path containing Unicode/CJK characters
    entry_unicode = _format_concat_entry(r"C:\videos\分镜头\1_场景一.mp4")
    assert "分镜头/1_场景一.mp4" in entry_unicode


def test_merge_writes_utf8_escaped_concat_manifest(tmp_path: Path) -> None:
    sys.path.insert(0, str(SRC))
    try:
        from video_merger import VideoMerger
    finally:
        sys.path.pop(0)

    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    # Create files with Unicode characters, spaces, and single quotes
    file1 = input_dir / "1_intro_分镜头_start.mp4"
    file2 = input_dir / "2_actor's_scene.mp4"
    file1.write_bytes(b"dummy1")
    file2.write_bytes(b"dummy2")

    output_file = tmp_path / "output.mp4"

    captured_concat_content: list[str] = []

    def fake_run(cmd, *args, **kwargs):
        # When ffmpeg concat is invoked, inspect the concat list file
        if "-f" in cmd and "concat" in cmd:
            idx = cmd.index("-i")
            concat_path = cmd[idx + 1]
            with open(concat_path, encoding="utf-8") as f:
                captured_concat_content.append(f.read())
            # create output dummy file for temp_raw
            raw_out = cmd[-1]
            Path(raw_out).write_bytes(b"raw")
        elif cmd[0] == "ffmpeg" and str(output_file) in cmd:
            # create output file so verification passes
            output_file.write_bytes(b"final_video")
        return MagicMock(returncode=0)

    with (
        patch("shutil.which", return_value="ffmpeg"),
        patch.object(VideoMerger, "_check_dependencies", return_value=None),
        patch.object(VideoMerger, "get_video_info", return_value=(1920, 1080, 10.0)),
        patch("subprocess.run", side_effect=fake_run),
    ):
        merger = VideoMerger()
        success = merger.merge(input_dir=str(input_dir), output_path=str(output_file))
        assert success is True

    assert len(captured_concat_content) == 1
    content = captured_concat_content[0]
    # Check that Unicode characters were written without error
    assert "1_intro_分镜头_start.mp4" in content
    # Check that single quote in filename was escaped properly
    assert r"2_actor'\''s_scene.mp4" in content
    # Check that Windows backslashes are converted to forward slashes
    assert "\\" not in content.replace(r"'\''", "")


def test_merge_chunks_writes_utf8_escaped_concat_manifest(tmp_path: Path) -> None:
    sys.path.insert(0, str(SRC))
    try:
        from video_merger import VideoMerger
    finally:
        sys.path.pop(0)

    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    file1 = input_dir / "1_片段一.mp4"
    file2 = input_dir / "2_片段二.mp4"
    file1.write_bytes(b"dummy1")
    file2.write_bytes(b"dummy2")

    output_dir = tmp_path / "chunks"

    captured_concat_content: list[str] = []

    def fake_run(cmd, *args, **kwargs):
        if "-f" in cmd and "concat" in cmd:
            idx = cmd.index("-i")
            concat_path = cmd[idx + 1]
            with open(concat_path, encoding="utf-8") as f:
                captured_concat_content.append(f.read())
            raw_out = cmd[-1]
            Path(raw_out).write_bytes(b"raw")
        elif cmd[0] == "ffmpeg":
            out = cmd[-1]
            Path(out).write_bytes(b"chunk_video")
        return MagicMock(returncode=0)

    with (
        patch("shutil.which", return_value="ffmpeg"),
        patch.object(VideoMerger, "_check_dependencies", return_value=None),
        patch.object(VideoMerger, "get_video_info", return_value=(1920, 1080, 30.0)),
        patch("subprocess.run", side_effect=fake_run),
    ):
        merger = VideoMerger()
        success = merger.merge_chunks(
            input_dir=str(input_dir), output_dir=str(output_dir), chunk_duration=60
        )
        assert success is True

    assert len(captured_concat_content) >= 1
    for content in captured_concat_content:
        assert "片段" in content
        assert "\\" not in content
