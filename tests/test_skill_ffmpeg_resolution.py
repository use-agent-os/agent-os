"""Issue #2435: three bundled media skills resolve ffmpeg three different ways.

``video-merger``, ``subtitle-burner`` and ``video-still-animator`` each carry
a hand-copied resolver that probes the usual Windows install locations when
ffmpeg is not on ``PATH``. The copies had drifted: ``animate.py`` did not
probe ``C:\\ffmpeg\\bin``, returned early -- skipping every fixed location --
whenever ``LOCALAPPDATA`` was unset, and lacked the absolute-path check the
other two have. So on a machine where ``video-merger`` found ffmpeg,
``video-still-animator`` failed with ``ffmpeg not found``.

Bundled skills ship as independent directories, so the copies cannot share a
module. What they can share is a test: the drift guard below runs all three
resolvers under the same monkeypatched environment and asserts they probe the
same locations in the same order.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

BUNDLED = Path(__file__).resolve().parents[1] / "src" / "agentos" / "skills" / "bundled"

RESOLVERS = {
    "video-still-animator": (BUNDLED / "video-still-animator/scripts/animate.py", "resolve_ffmpeg"),
    "subtitle-burner": (BUNDLED / "subtitle-burner/scripts/burn.py", "_resolve_ffmpeg"),
    "video-merger": (BUNDLED / "video-merger/src/video_merger.py", "_resolve_ffmpeg_binary"),
}


def _load(path: Path) -> ModuleType:
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(f"ffmpeg_res_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULES: dict[str, ModuleType] = {}


@pytest.fixture(scope="module")
def resolvers() -> dict[str, Callable[[str], str]]:
    loaded: dict[str, Callable[[str], str]] = {}
    for skill, (path, name) in RESOLVERS.items():
        MODULES[skill] = module = _load(path)
        function = getattr(module, name)
        # video-merger's resolver takes the tool name too; bind it to ffmpeg.
        loaded[skill] = (
            (lambda f: lambda explicit: f(explicit, "ffmpeg"))(function)
            if (skill == "video-merger")
            else function
        )
    return loaded


def _patch_glob(monkeypatch: pytest.MonkeyPatch, fake: Callable[..., list[str]]) -> None:
    """Two scripts bind ``glob`` at import; the third imports it per call."""
    import glob as glob_module

    monkeypatch.setattr(glob_module, "glob", fake)
    for module in MODULES.values():
        if hasattr(module, "glob"):
            monkeypatch.setattr(module, "glob", fake)


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Windows host with nothing on PATH, LOCALAPPDATA and USERPROFILE set."""
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(shutil, "which", lambda _cmd, *a, **k: None)
    monkeypatch.setenv("LOCALAPPDATA", r"C:\home\u\AppData\Local")
    monkeypatch.setenv("USERPROFILE", r"C:\home\u")


def _probed(
    monkeypatch: pytest.MonkeyPatch, resolve: Callable[[str], str], *, exists=()
) -> list[str]:
    """Every path the resolver asks ``os.path.isfile`` about, in order."""
    seen: list[str] = []
    existing = set(exists)

    def isfile(path: str) -> bool:
        seen.append(path)
        return path in existing

    monkeypatch.setattr(os.path, "isfile", isfile)
    _patch_glob(monkeypatch, lambda pattern, **k: [])
    resolve("ffmpeg")
    return seen


ANIMATE = "video-still-animator"
FIXED_LOCATIONS = [
    # Built with ``os.path.join`` exactly as the scripts build it, because the
    # separator it inserts is the runner's, not the monkeypatched ``os.name``'s:
    # on a Linux runner this is ``C:\home\u/scoop/...``, and that is what the
    # resolver probes there too.
    os.path.join(r"C:\home\u", "scoop", "apps", "ffmpeg", "current", "bin", "ffmpeg.exe"),
    r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
    r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    r"C:\ffmpeg\bin\ffmpeg.exe",
]


# ── the issue ───────────────────────────────────────────────────────────────


def test_animator_finds_ffmpeg_in_c_ffmpeg_bin(
    windows: None, monkeypatch: pytest.MonkeyPatch, resolvers: dict[str, Callable[[str], str]]
) -> None:
    target = r"C:\ffmpeg\bin\ffmpeg.exe"
    monkeypatch.setattr(os.path, "isfile", lambda p: p == target)

    assert resolvers[ANIMATE]("ffmpeg") == target


def test_animator_still_probes_fixed_locations_without_localappdata(
    windows: None, monkeypatch: pytest.MonkeyPatch, resolvers: dict[str, Callable[[str], str]]
) -> None:
    """The early return skipped every candidate, not just the winget glob."""
    monkeypatch.delenv("LOCALAPPDATA")
    target = r"C:\ProgramData\chocolatey\bin\ffmpeg.exe"
    monkeypatch.setattr(os.path, "isfile", lambda p: p == target)

    assert resolvers[ANIMATE]("ffmpeg") == target


# ── the three copies agree ──────────────────────────────────────────────────


def test_the_three_resolvers_probe_the_same_locations_in_the_same_order(
    windows: None, monkeypatch: pytest.MonkeyPatch, resolvers: dict[str, Callable[[str], str]]
) -> None:
    """The drift guard. A location added to one skill must be added to all."""
    probed = {skill: _probed(monkeypatch, resolve) for skill, resolve in resolvers.items()}

    for skill, paths in probed.items():
        assert paths == FIXED_LOCATIONS, skill


def test_the_three_winget_globs_name_the_same_binary() -> None:
    animate = _load(RESOLVERS[ANIMATE][0]).WINGET_FFMPEG_GLOB
    burn = _load(RESOLVERS["subtitle-burner"][0])._WINGET_FFMPEG_GLOB
    merger = _load(RESOLVERS["video-merger"][0])._WINGET_FFMPEG_GLOB

    assert burn == merger
    assert animate == merger + "/ffmpeg.exe"


@pytest.mark.parametrize("skill", list(RESOLVERS))
@pytest.mark.parametrize("location", FIXED_LOCATIONS)
def test_every_skill_finds_ffmpeg_at_every_fixed_location(
    windows: None,
    monkeypatch: pytest.MonkeyPatch,
    resolvers: dict[str, Callable[[str], str]],
    skill: str,
    location: str,
) -> None:
    monkeypatch.setattr(os.path, "isfile", lambda p: p == location)
    _patch_glob(monkeypatch, lambda pattern, **k: [])

    assert resolvers[skill]("ffmpeg") == location


@pytest.mark.parametrize("skill", list(RESOLVERS))
def test_every_skill_prefers_the_winget_install_when_present(
    windows: None,
    monkeypatch: pytest.MonkeyPatch,
    resolvers: dict[str, Callable[[str], str]],
    skill: str,
) -> None:
    winget_bin = (
        r"C:\home\u\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_x\ffmpeg-7-full_build\bin"
    )
    winget_exe = os.path.join(winget_bin, "ffmpeg.exe")  # the runner's separator, as the code

    def fake_glob(pattern: str, **k) -> list[str]:
        return [winget_exe] if pattern.endswith("ffmpeg.exe") else [winget_bin]

    _patch_glob(monkeypatch, fake_glob)
    monkeypatch.setattr(os.path, "isfile", lambda p: p in {winget_exe, FIXED_LOCATIONS[-1]})

    assert resolvers[skill]("ffmpeg") == winget_exe


@pytest.mark.parametrize("skill", list(RESOLVERS))
def test_every_skill_returns_the_explicit_name_when_nothing_is_found(
    windows: None,
    monkeypatch: pytest.MonkeyPatch,
    resolvers: dict[str, Callable[[str], str]],
    skill: str,
) -> None:
    """So subprocess raises the canonical ``not found`` error, not a fake path."""
    monkeypatch.setattr(os.path, "isfile", lambda p: False)
    _patch_glob(monkeypatch, lambda pattern, **k: [])

    assert resolvers[skill]("ffmpeg") == "ffmpeg"


@pytest.mark.parametrize("skill", list(RESOLVERS))
def test_every_skill_returns_a_path_lookup_hit_unchanged(
    monkeypatch: pytest.MonkeyPatch, resolvers: dict[str, Callable[[str], str]], skill: str
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _cmd, *a, **k: "/usr/bin/ffmpeg")

    assert resolvers[skill]("ffmpeg") == "/usr/bin/ffmpeg"


@pytest.mark.parametrize("skill", list(RESOLVERS))
def test_every_skill_accepts_an_explicit_absolute_path_that_is_not_on_path(
    windows: None,
    monkeypatch: pytest.MonkeyPatch,
    resolvers: dict[str, Callable[[str], str]],
    skill: str,
) -> None:
    """``--ffmpeg-path C:\\tools\\ffmpeg.exe`` must be honoured as given; the
    animator used to fall through to the probe list and lose it."""
    explicit = r"C:\tools\ffmpeg.exe"
    monkeypatch.setattr(os.path, "isfile", lambda p: p == explicit)

    assert resolvers[skill](explicit) == explicit


@pytest.mark.parametrize("skill", list(RESOLVERS))
def test_every_skill_does_not_probe_windows_locations_on_posix(
    monkeypatch: pytest.MonkeyPatch, resolvers: dict[str, Callable[[str], str]], skill: str
) -> None:
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(shutil, "which", lambda _cmd, *a, **k: None)
    probed: list[str] = []
    monkeypatch.setattr(os.path, "isfile", lambda p: probed.append(p) or False)

    assert resolvers[skill]("ffmpeg") == "ffmpeg"
    assert not [p for p in probed if p.startswith("C:")]


def test_a_missing_userprofile_does_not_break_the_scoop_probe(
    windows: None, monkeypatch: pytest.MonkeyPatch, resolvers: dict[str, Callable[[str], str]]
) -> None:
    monkeypatch.delenv("USERPROFILE")
    monkeypatch.setattr(os.path, "isfile", lambda p: p == FIXED_LOCATIONS[-1])
    _patch_glob(monkeypatch, lambda pattern, **k: [])

    assert resolvers[ANIMATE]("ffmpeg") == FIXED_LOCATIONS[-1]


def test_the_skill_doc_lists_every_fallback_location() -> None:
    """SKILL.md tells the operator where the script looks; it must not lag."""
    doc = (BUNDLED / "video-still-animator" / "SKILL.md").read_text(encoding="utf-8")

    for needle in ("winget", "scoop", "chocolatey", r"C:\Program Files\ffmpeg", r"C:\ffmpeg\bin"):
        assert needle.lower() in doc.lower(), needle
