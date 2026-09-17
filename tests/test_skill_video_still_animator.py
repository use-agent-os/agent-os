"""``video-still-animator``'s ffmpeg fallback resolution on Windows (#2435).

``video-merger`` and ``subtitle-burner`` both probe ``C:\\ffmpeg\\bin\\ffmpeg.exe``
as a standard Windows fallback location; ``animate.py`` omitted it, and also
returned early -- skipping every fixed-path candidate below it -- whenever
``LOCALAPPDATA`` happened to be unset. On a Windows machine with ffmpeg
installed to ``C:\\ffmpeg\\bin`` and not on ``PATH``, ``video-merger`` finds
it and ``video-still-animator`` fails with ``Error: ffmpeg not found``.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = (
    ROOT
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "video-still-animator"
    / "scripts"
    / "animate.py"
)

# A placeholder Windows profile root, deliberately not shaped like a real
# user's profile directory: tests/test_public_release_hygiene.py's local-path
# patterns flag that shape in a tracked file even with a fake, one-letter
# placeholder name, the same way they'd flag a real one.
_WIN_PROFILE = r"C:\WinProfile"


def _load_animate():
    spec = importlib.util.spec_from_file_location("video_still_animator_animate", SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


animate = _load_animate()


def test_resolve_ffmpeg_returns_the_explicit_name_on_non_windows() -> None:
    with patch("shutil.which", return_value=None), patch.object(animate.os, "name", "posix"):
        assert animate.resolve_ffmpeg("ffmpeg") == "ffmpeg"


def test_resolve_ffmpeg_prefers_a_binary_already_on_path() -> None:
    with patch("shutil.which", return_value="/usr/bin/ffmpeg"):
        assert animate.resolve_ffmpeg("ffmpeg") == "/usr/bin/ffmpeg"


def test_resolve_ffmpeg_finds_the_winget_install() -> None:
    winget_hit = (
        rf"{_WIN_PROFILE}\AppData\Local\Microsoft\WinGet\Packages\x"
        r"\ffmpeg-full_build\bin\ffmpeg.exe"
    )
    with (
        patch("shutil.which", return_value=None),
        patch.object(animate.os, "name", "nt"),
        patch.dict(os.environ, {"LOCALAPPDATA": rf"{_WIN_PROFILE}\AppData\Local"}),
        patch("glob.glob", return_value=[winget_hit]),
        patch("os.path.isfile", side_effect=lambda p: p == winget_hit),
    ):
        assert animate.resolve_ffmpeg("ffmpeg") == winget_hit


def test_resolve_ffmpeg_probes_every_fixed_windows_candidate() -> None:
    """Each of the four fixed fallback locations is actually reachable,
    including the newly-added C:\\ffmpeg\\bin (#2435's own repro)."""
    scoop = rf"{_WIN_PROFILE}\scoop\apps\ffmpeg\current\bin\ffmpeg.exe"
    expected_candidates = [
        scoop,
        r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\ffmpeg\bin\ffmpeg.exe",
    ]

    for target in expected_candidates:
        with (
            patch("shutil.which", return_value=None),
            patch.object(animate.os, "name", "nt"),
            patch.dict(
                os.environ,
                {
                    "LOCALAPPDATA": rf"{_WIN_PROFILE}\AppData\Local",
                    "USERPROFILE": _WIN_PROFILE,
                },
            ),
            patch("glob.glob", return_value=[]),
            patch("os.path.isfile", side_effect=lambda p, target=target: p == target),
        ):
            assert animate.resolve_ffmpeg("ffmpeg") == target, target


def test_resolve_ffmpeg_still_probes_fixed_candidates_without_localappdata() -> None:
    """#2435's actual bug: LOCALAPPDATA unset must only skip the winget
    glob, not the fixed-path candidates below it."""
    with (
        patch("shutil.which", return_value=None),
        patch.object(animate.os, "name", "nt"),
        patch.dict(os.environ, {"USERPROFILE": _WIN_PROFILE}, clear=False),
    ):
        os.environ.pop("LOCALAPPDATA", None)
        with patch(
            "os.path.isfile", side_effect=lambda p: p == r"C:\ffmpeg\bin\ffmpeg.exe"
        ):
            assert animate.resolve_ffmpeg("ffmpeg") == r"C:\ffmpeg\bin\ffmpeg.exe"


def test_resolve_ffmpeg_falls_back_to_explicit_when_nothing_is_found() -> None:
    with (
        patch("shutil.which", return_value=None),
        patch.object(animate.os, "name", "nt"),
        patch.dict(os.environ, {"LOCALAPPDATA": rf"{_WIN_PROFILE}\AppData\Local"}),
        patch("glob.glob", return_value=[]),
        patch("os.path.isfile", return_value=False),
    ):
        assert animate.resolve_ffmpeg("ffmpeg") == "ffmpeg"
