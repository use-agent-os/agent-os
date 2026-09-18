import os
import sys
from pathlib import Path
from unittest.mock import patch

# Ensure the script directory is on sys.path so animate can be imported
SCRIPT_DIR = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "video-still-animator"
    / "scripts"
)
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import animate  # noqa: E402


def test_resolve_ffmpeg_which_found():
    with patch("shutil.which", return_value="/usr/bin/ffmpeg"):
        assert animate.resolve_ffmpeg("ffmpeg") == "/usr/bin/ffmpeg"


def test_resolve_ffmpeg_non_windows_fallback():
    with patch("shutil.which", return_value=None), patch("os.name", "posix"):
        assert animate.resolve_ffmpeg("custom-ffmpeg") == "custom-ffmpeg"


def test_resolve_ffmpeg_c_ffmpeg_bin_fallback():
    def mock_isfile(path: str) -> bool:
        return path == r"C:\ffmpeg\bin\ffmpeg.exe"

    with (
        patch("shutil.which", return_value=None),
        patch("os.name", "nt"),
        patch.dict(os.environ, {"LOCALAPPDATA": "", "USERPROFILE": r"C:\Users\test"}),
        patch("os.path.isfile", side_effect=mock_isfile),
    ):
        resolved = animate.resolve_ffmpeg("ffmpeg")
        assert resolved == r"C:\ffmpeg\bin\ffmpeg.exe"


def test_resolve_ffmpeg_windows_all_candidates():
    expected_candidates = [
        r"C:\Users\test\scoop\apps\ffmpeg\current\bin\ffmpeg.exe",
        r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\ffmpeg\bin\ffmpeg.exe",
    ]

    for target in expected_candidates:
        with (
            patch("shutil.which", return_value=None),
            patch("os.name", "nt"),
            patch.dict(
                os.environ,
                {"LOCALAPPDATA": r"C:\Users\test\AppData\Local", "USERPROFILE": r"C:\Users\test"},
            ),
            patch("glob.glob", return_value=[]),
            patch("os.path.isfile", side_effect=lambda p, target=target: p == target),
        ):
            assert animate.resolve_ffmpeg("ffmpeg") == target
