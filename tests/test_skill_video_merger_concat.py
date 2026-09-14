"""video-merger writes FFmpeg concat directives that survive real paths.

The concat manifest used to be built as ``f"file '{os.path.abspath(v)}'"``.
FFmpeg's concat parser reads that path as a token, so a Windows path lost
its backslashes (``C:\\Videos\\clip.mp4`` arriving as ``C:Videosclip.mp4``)
and a filename holding a single quote closed the directive early. Both
turned into an ffmpeg failure on an otherwise valid merge.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_SRC = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "video-merger"
    / "src"
    / "video_merger.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("video_merger_under_test", _SRC)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def concat_entry():
    return _load_module().VideoMerger.concat_entry


def test_backslashes_become_forward_slashes(concat_entry, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os.path, "abspath", lambda p: "C:\\Videos\\Segments\\01_clip.mp4")

    entry = concat_entry("01_clip.mp4")

    assert "\\" not in entry
    assert entry == "file 'C:/Videos/Segments/01_clip.mp4'\n"


def test_single_quote_is_escaped_not_left_to_close_the_directive(
    concat_entry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os.path, "abspath", lambda p: "/videos/01_user's_intro.mp4")

    entry = concat_entry("01_user's_intro.mp4")

    # Closed, escaped, reopened -- the only way to carry a quote through a
    # single-quoted concat directive.
    assert entry == "file '/videos/01_user'\\''s_intro.mp4'\n"


def test_a_plain_posix_path_is_unchanged(concat_entry, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os.path, "abspath", lambda p: "/videos/01_clip.mp4")

    assert concat_entry("01_clip.mp4") == "file '/videos/01_clip.mp4'\n"
