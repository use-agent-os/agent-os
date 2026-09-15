"""subtitle-burner must derive ffprobe by name, not by rewriting the whole path (#2279).

``_probe_resolution`` used to derive ``ffprobe`` from a resolved ``ffmpeg``
path with ``ffmpeg_bin.replace("ffmpeg.exe", "ffprobe.exe").replace("/ffmpeg",
"/ffprobe")``. ``str.replace`` rewrites *every* occurrence, so any install
whose directory is named ``ffmpeg`` -- a manual ``/opt/ffmpeg/bin`` install
(the layout this same file already expects on Windows at
``C:\\ffmpeg\\bin\\ffmpeg.exe``) or Homebrew's ``Cellar/ffmpeg/<version>/bin``
-- had that directory rewritten too, producing a path that does not exist.

The guard right after it (``if ffprobe == ffmpeg_bin: ...fallback...``) only
fires when *no* replacement happened; here a replacement did happen, just the
wrong one, so the correct fallback derivation was skipped. ``_probe_resolution``
then silently returned ``None`` -- no exception, no log -- and
``play_w``/``play_h`` stayed ``0``, so ``PlayResX``/``PlayResY`` were dropped
from the ``force_style`` chain (``play_res`` defaults to ``"auto"``, so this
is the default path, not an opt-in one). The subtitles still burn, just at
libass's own script resolution instead of the source video's, which is only
visible by eye.

The fix drops the string-rewrite step entirely and always derives ffprobe
via ``Path(ffmpeg_bin).with_name(...)``, which only ever touches the file
name -- the same derivation the removed fallback branch already used
correctly.
"""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# The name _probe_resolution actually derives and looks for, on whatever
# platform these tests are really running on (this file's CI runs both
# ubuntu-latest and windows-latest) -- fixtures below must create a file
# under this name, not a hardcoded POSIX "ffprobe", or the file-existence
# guard correctly (and confusingly) fails on Windows for an unrelated
# reason: no file by that name, not the bug under test.
_PROBE_NAME = "ffprobe.exe" if os.name == "nt" else "ffprobe"

_SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "subtitle-burner"
    / "scripts"
)


def _load():
    """Import burn.py from the skill's scripts dir without touching PATH."""
    entry = str(_SCRIPTS)
    added = entry not in sys.path
    if added:
        sys.path.insert(0, entry)
    try:
        return importlib.import_module("burn")
    finally:
        if added:
            sys.path.remove(entry)


@pytest.fixture(scope="module")
def burn():
    return _load()


def _fake_ffprobe_run(monkeypatch: pytest.MonkeyPatch, w: int, h: int) -> None:
    result = MagicMock()
    result.returncode = 0
    result.stdout = f"{w}x{h}\n".encode()
    monkeypatch.setattr(subprocess, "run", MagicMock(return_value=result))


# --- the exact real-world layouts named in the issue ------------------------


def test_manual_opt_ffmpeg_install_derives_ffprobe_beside_it(
    tmp_path: Path, burn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails without the fix: the buggy replace() also rewrote the "ffmpeg"
    directory, producing /opt/ffprobe/bin/ffprobe, which is never created
    below -- Path(...).is_file() is False and shutil.which finds nothing,
    so _probe_resolution returns None instead of probing."""
    install = tmp_path / "opt" / "ffmpeg" / "bin"
    install.mkdir(parents=True)
    ffmpeg_bin = install / "ffmpeg"
    ffmpeg_bin.touch()
    (install / _PROBE_NAME).touch()
    _fake_ffprobe_run(monkeypatch, 1920, 1080)

    result = burn._probe_resolution(str(ffmpeg_bin), tmp_path / "video.mp4")

    assert result == (1920, 1080)


def test_homebrew_cellar_install_derives_ffprobe_beside_it(
    tmp_path: Path, burn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails without the fix: the buggy replace() also rewrote the
    "Cellar/ffmpeg" segment, producing .../Cellar/ffprobe/7.1/bin/ffprobe,
    which does not exist."""
    install = tmp_path / "opt" / "homebrew" / "Cellar" / "ffmpeg" / "7.1" / "bin"
    install.mkdir(parents=True)
    ffmpeg_bin = install / "ffmpeg"
    ffmpeg_bin.touch()
    (install / _PROBE_NAME).touch()
    _fake_ffprobe_run(monkeypatch, 3840, 2160)

    result = burn._probe_resolution(str(ffmpeg_bin), tmp_path / "video.mp4")

    assert result == (3840, 2160)


def test_ffmpeg_named_directory_with_exe_suffixed_binary_derives_ffprobe_beside_it(
    tmp_path: Path, burn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact layout this file's own Windows fallback candidates use
    (C:\\ffmpeg\\bin\\ffmpeg.exe) also has a directory literally named
    "ffmpeg", so it's exposed to the same bug regardless of the binary's
    own suffix. (Exercised here with the real, non-mocked derivation this
    process's actual platform uses -- flipping os.name to fake a different
    platform is not reproducible in this sandbox: Path(...).with_name(...)
    would need to construct a real WindowsPath, which Python 3.12+ refuses
    to instantiate on a non-Windows host.)"""
    install = tmp_path / "ffmpeg" / "bin"
    install.mkdir(parents=True)
    ffmpeg_bin = install / "ffmpeg.exe"
    ffmpeg_bin.touch()
    (install / _PROBE_NAME).touch()
    _fake_ffprobe_run(monkeypatch, 1280, 720)

    result = burn._probe_resolution(str(ffmpeg_bin), tmp_path / "video.mp4")

    assert result == (1280, 720)


# --- guards: layouts the bug never touched, must keep working --------------


def test_plain_usr_bin_install_still_works(
    tmp_path: Path, burn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard: passes either way by design -- no "ffmpeg"-named directory,
    so the old buggy replace() happened to derive this one correctly too."""
    install = tmp_path / "usr" / "bin"
    install.mkdir(parents=True)
    ffmpeg_bin = install / "ffmpeg"
    ffmpeg_bin.touch()
    (install / _PROBE_NAME).touch()
    _fake_ffprobe_run(monkeypatch, 640, 480)

    result = burn._probe_resolution(str(ffmpeg_bin), tmp_path / "video.mp4")

    assert result == (640, 480)


def test_bare_command_name_falls_back_to_path_lookup(
    burn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare "ffmpeg" (resolved via PATH, no directory at all) must derive
    a bare "ffprobe" (matching this platform's own naming -- "ffprobe.exe"
    on Windows, "ffprobe" elsewhere) and look it up the same way, not crash
    on a path operation."""
    monkeypatch.setattr(
        shutil, "which", lambda name: f"/resolved/{name}" if name == _PROBE_NAME else None
    )
    _fake_ffprobe_run(monkeypatch, 100, 200)

    result = burn._probe_resolution("ffmpeg", Path("video.mp4"))

    assert result == (100, 200)


def test_missing_ffprobe_returns_none_without_crashing(
    tmp_path: Path, burn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard: passes either way by design -- no ffprobe anywhere near
    ffmpeg, and not on PATH either, must degrade to None, not raise."""
    install = tmp_path / "opt" / "ffmpeg" / "bin"
    install.mkdir(parents=True)
    ffmpeg_bin = install / "ffmpeg"
    ffmpeg_bin.touch()
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    result = burn._probe_resolution(str(ffmpeg_bin), tmp_path / "video.mp4")

    assert result is None


# --- real ffmpeg/ffprobe, no mocks --------------------------------------


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="requires real ffmpeg/ffprobe binaries",
)
def test_real_ffprobe_under_an_ffmpeg_named_directory(tmp_path: Path, burn) -> None:
    """End to end with real binaries, no mocked subprocess: copies the
    system ffmpeg/ffprobe into a directory literally named "ffmpeg" (the
    exact shape of the issue's own repro) and probes a real generated
    video. Fails without the fix: returns None."""
    install = tmp_path / "opt" / "ffmpeg" / "bin"
    install.mkdir(parents=True)
    shutil.copy(shutil.which("ffmpeg"), install / "ffmpeg")
    shutil.copy(shutil.which("ffprobe"), install / "ffprobe")
    (install / "ffmpeg").chmod(0o755)
    (install / "ffprobe").chmod(0o755)

    video_path = tmp_path / "video.mp4"
    subprocess.run(
        [
            str(install / "ffmpeg"),
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=352x288:d=1",
            "-y",
            str(video_path),
        ],
        capture_output=True,
        timeout=30,
        check=True,
    )

    result = burn._probe_resolution(str(install / "ffmpeg"), video_path)

    assert result == (352, 288)
