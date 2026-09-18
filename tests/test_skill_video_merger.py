"""Regression test for the bundled ``video-merger`` skill's core library.

Issue #2858: ``VideoMerger.merge()`` (full mode) passed ``output_path``
straight to ffmpeg without ensuring its parent directory exists, unlike
``merge_chunks()`` which already does. These drive the real ``ffmpeg``/
``ffprobe`` binaries against tiny generated clips, because the defect is in
whether the directory gets created before the subprocess call, not in any
value the module computes.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT / "src" / "agentos" / "skills" / "bundled" / "video-merger" / "src" / "video_merger.py"
)

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not available in this environment",
)


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_agentos_test_video_merger", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_segment(path: Path, color: str) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=64x64:d=1:r=24",
            str(path),
        ],
        check=True,
    )


@pytest.fixture
def segments(tmp_path: Path) -> Path:
    input_dir = tmp_path / "segments"
    input_dir.mkdir()
    _make_segment(input_dir / "001_part.mp4", "red")
    _make_segment(input_dir / "002_part.mp4", "blue")
    return input_dir


def test_merge_creates_missing_nested_output_directory(segments: Path, tmp_path: Path) -> None:
    module = _load_module()
    merger = module.VideoMerger()
    output_path = tmp_path / "dist" / "output" / "final.mp4"
    assert not output_path.parent.exists()

    ok = merger.merge(str(segments), str(output_path))

    assert ok is True
    assert output_path.is_file()
    assert output_path.stat().st_size > 0


def test_merge_still_works_when_the_output_directory_already_exists(
    segments: Path, tmp_path: Path
) -> None:
    """Boundary: an existing output directory is not disturbed by the fix."""
    module = _load_module()
    merger = module.VideoMerger()
    output_dir = tmp_path / "existing"
    output_dir.mkdir()
    output_path = output_dir / "final.mp4"

    ok = merger.merge(str(segments), str(output_path))

    assert ok is True
    assert output_path.is_file()


def test_merge_chunks_creating_a_missing_directory_is_unaffected(
    segments: Path, tmp_path: Path
) -> None:
    """Boundary: merge_chunks already handled this (line-level precedent for
    this fix) and must keep working exactly as before."""
    module = _load_module()
    merger = module.VideoMerger()
    output_dir = tmp_path / "chunks" / "nested"
    assert not output_dir.exists()

    ok = merger.merge_chunks(str(segments), str(output_dir), chunk_duration=1)

    assert ok is True
    assert sorted(p.name for p in output_dir.glob("*.mp4"))
