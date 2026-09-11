"""subtitle-burner skill — load, eligibility, and path escaping tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agentos.skills.eligibility import EligibilityContext, check_eligibility
from agentos.skills.loader import SkillLoader

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "src" / "agentos" / "skills" / "bundled"
SCRIPTS = BUNDLED / "subtitle-burner" / "scripts"


def _spec() -> object:
    return SkillLoader(bundled_dir=BUNDLED).get_by_name("subtitle-burner")


def _burn_module() -> object:
    sys.path.insert(0, str(SCRIPTS))
    try:
        import burn  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)
    return burn


def test_skill_loads() -> None:
    spec = _spec()
    assert spec is not None
    assert spec.name == "subtitle-burner"
    assert spec.provenance.origin == "agentos-original"
    assert spec.provenance.license == "MIT"


def test_eligibility_with_bins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agentos.skills.eligibility.shutil.which",
        lambda name: f"/usr/bin/{name}" if name in {"python", "python3", "ffmpeg"} else None,
    )
    spec = _spec()
    assert spec is not None
    assert check_eligibility(spec, EligibilityContext.auto())


@pytest.mark.parametrize(
    ("raw_path", "expected"),
    [
        # Standard POSIX paths without colons
        ("/home/user/video.srt", "/home/user/video.srt"),
        # POSIX paths with colons at various positions (including indices 0, 1, 2)
        ("/a:b/sub.srt", "/a\\:b/sub.srt"),
        (":leading.srt", "\\:leading.srt"),
        ("08:30.srt", "08\\:30.srt"),
        ("10:00:00.srt", "10\\:00\\:00.srt"),
        ("/tmp/2026-09-11_08:30:00/sub.srt", "/tmp/2026-09-11_08\\:30\\:00/sub.srt"),
        # Windows drive paths (drive letter colon escaped as C\:, others escaped as \:)
        (r"C:\Users\test\video.srt", "C\\:/Users/test/video.srt"),
        (r"C:\Users\test\08:30:00.srt", "C\\:/Users/test/08\\:30\\:00.srt"),
        (r"C:\a:b:c\sub.srt", "C\\:/a\\:b\\:c/sub.srt"),
        ("D:/media/clip_01:23.srt", "D\\:/media/clip_01\\:23.srt"),
        # Single quotes inside path
        ("/path/with 'quotes'/sub.srt", "/path/with \\'quotes\\'/sub.srt"),
        (
            r"C:\path with 'quotes'\08:30.srt",
            "C\\:/path with \\'quotes\\'/08\\:30.srt",
        ),
    ],
)
def test_escape_subtitle_path(raw_path: str, expected: str) -> None:
    burn = _burn_module()
    assert burn._escape_subtitle_path(raw_path) == expected


def test_missing_files_exit_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    burn = _burn_module()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "burn.py",
            "--input",
            str(tmp_path / "nonexistent.mp4"),
            "--subtitles",
            str(tmp_path / "nonexistent.srt"),
            "--output",
            str(tmp_path / "out.mp4"),
        ],
    )
    assert burn.main() == 1
