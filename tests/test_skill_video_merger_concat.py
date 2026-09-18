"""video-merger writes ffmpeg concat-demuxer manifests that survive quotes and
Windows paths (#2122).

The bundled library is vendored under a hyphenated skill directory, so it is
loaded straight from its file rather than imported as a package.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

_VIDEO_MERGER = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "video-merger"
    / "src"
    / "video_merger.py"
)


def _load() -> ModuleType:
    name = "_video_merger_under_test"
    spec = importlib.util.spec_from_file_location(name, _VIDEO_MERGER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def video_merger() -> ModuleType:
    return _load()


def test_single_quote_in_filename_is_escaped(video_merger: ModuleType, tmp_path: Path) -> None:
    clip = tmp_path / "01_user's_intro.mp4"
    line = video_merger._concat_manifest_line(str(clip))

    assert line.endswith("\n")
    # ffmpeg's token rule: close the quote, escape the quote, reopen.
    assert "'\\''" in line
    body = line[len("file ") : -1]
    assert body.startswith("'") and body.endswith("'")
    assert body.count("'") == 2 + 3  # outer pair + one escaped quote


def test_plain_path_is_written_absolute_and_quoted(
    video_merger: ModuleType, tmp_path: Path
) -> None:
    clip = tmp_path / "01_clip.mp4"
    line = video_merger._concat_manifest_line(str(clip))

    expected = os.path.abspath(str(clip))
    if os.name == "nt":
        expected = expected.replace("\\", "/")
    assert line == f"file '{expected}'\n"


def test_windows_backslashes_become_forward_slashes(
    video_merger: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(video_merger.os, "name", "nt")
    monkeypatch.setattr(video_merger.os.path, "abspath", lambda p: p)

    line = video_merger._concat_manifest_line("C:\\Videos\\Segments\\01_clip.mp4")

    assert line == "file 'C:/Videos/Segments/01_clip.mp4'\n"


def test_posix_backslash_in_filename_is_left_alone(
    video_merger: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(video_merger.os, "name", "posix")
    monkeypatch.setattr(video_merger.os.path, "abspath", lambda p: p)

    line = video_merger._concat_manifest_line("/videos/odd\\name.mp4")

    assert line == "file '/videos/odd\\name.mp4'\n"


def test_merge_writes_escaped_manifest_as_utf8(
    video_merger: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through ``merge``: the manifest ffmpeg is handed is escaped,
    UTF-8, and lists every clip in order."""
    clips = [tmp_path / "01_user's_intro.mp4", tmp_path / "02_片段.mp4"]
    for clip in clips:
        clip.write_bytes(b"")

    merger = video_merger.VideoMerger.__new__(video_merger.VideoMerger)
    merger.ffmpeg_path = "ffmpeg"
    merger.ffprobe_path = "ffprobe"
    monkeypatch.setattr(merger, "get_sorted_videos", lambda _d: [str(c) for c in clips])
    monkeypatch.setattr(merger, "get_video_info", lambda _p: (1080, 1920, 1.0))

    captured: dict[str, str] = {}

    class _StopError(Exception):
        pass

    def fake_run(cmd: list[str], **_kw: object) -> None:
        manifest = cmd[cmd.index("-i") + 1]
        captured["manifest"] = Path(manifest).read_text(encoding="utf-8")
        raise _StopError

    monkeypatch.setattr(video_merger.subprocess, "run", fake_run)

    with pytest.raises(_StopError):
        merger.merge(input_dir=str(tmp_path), output_path=str(tmp_path / "out.mp4"))

    lines = captured["manifest"].splitlines()
    assert lines == [video_merger._concat_manifest_line(str(c)).rstrip("\n") for c in clips]
    assert "'\\''" in lines[0]
    assert "片段" in lines[1]
