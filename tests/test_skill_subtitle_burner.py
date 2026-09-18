"""subtitle-burner — locating the ffprobe that sets PlayRes.

``play_res`` defaults to ``auto``, and SKILL.md leans on that: ``font_size`` is
documented as source-video pixels and ``margin_v`` as "bottom margin in
source-video pixels (because ``play_res=auto`` sets PlayRes to the input
W×H)". Both promises rest on the probe finding ffprobe, and the probe fails
silently -- the subtitles still burn, just at the wrong size.

Offline: ffmpeg and ffprobe are never executed. The binaries are empty files
in a tmp tree and ``subprocess.run`` is replaced, so what is asserted is the
argv the script built.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

BUNDLED = Path(__file__).resolve().parents[1] / "src" / "agentos" / "skills" / "bundled"
SCRIPT = BUNDLED / "subtitle-burner" / "scripts" / "burn.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("subtitle_burn", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


burn = _load()


@pytest.mark.parametrize(
    ("ffmpeg_bin", "expected"),
    [
        pytest.param("/usr/bin/ffmpeg", "/usr/bin/ffprobe", id="ordinary_prefix"),
        pytest.param("/usr/local/bin/ffmpeg", "/usr/local/bin/ffprobe", id="local_prefix"),
        pytest.param("/opt/ffmpeg/bin/ffmpeg", "/opt/ffmpeg/bin/ffprobe", id="ffmpeg_directory"),
        pytest.param(
            "/opt/homebrew/Cellar/ffmpeg/7.1/bin/ffmpeg",
            "/opt/homebrew/Cellar/ffmpeg/7.1/bin/ffprobe",
            id="homebrew_cellar",
        ),
        pytest.param(
            "/home/u/.local/share/ffmpeg/ffmpeg",
            "/home/u/.local/share/ffmpeg/ffprobe",
            id="ffmpeg_directory_and_binary",
        ),
        pytest.param("C:/ffmpeg/bin/ffmpeg.exe", "C:/ffmpeg/bin/ffprobe.exe", id="windows_exe"),
        pytest.param("/opt/tools/ffmpeg-7.1", "/opt/tools/ffprobe-7.1", id="versioned_name"),
        pytest.param("ffmpeg", "ffprobe", id="bare_name_on_path"),
    ],
)
def test_ffprobe_is_taken_from_beside_ffmpeg(ffmpeg_bin: str, expected: str) -> None:
    """Only the file name changes; a directory called ffmpeg stays put."""
    assert burn._ffprobe_beside(ffmpeg_bin).replace("\\", "/") == expected


def test_a_binary_under_another_name_falls_back_to_plain_ffprobe() -> None:
    """Nothing to carry across, so ask for ffprobe in the same directory."""
    probe = burn._ffprobe_beside("/usr/bin/avconv").replace("\\", "/")

    assert probe == ("/usr/bin/ffprobe.exe" if os.name == "nt" else "/usr/bin/ffprobe")


def test_the_fallback_name_follows_the_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulated rather than skipped, so it runs on both CI jobs."""
    monkeypatch.setattr(os, "name", "nt")
    assert burn._ffprobe_beside("/tools/avconv").replace("\\", "/") == "/tools/ffprobe.exe"

    monkeypatch.setattr(os, "name", "posix")
    assert burn._ffprobe_beside("/tools/avconv").replace("\\", "/") == "/tools/ffprobe"


def _install_fake_ffmpeg(tmp_path: Path, *, directory: str = "ffmpeg") -> Path:
    """An ffmpeg/ffprobe pair under a directory named like the issue's layout."""
    bin_dir = tmp_path / directory / "bin"
    bin_dir.mkdir(parents=True)
    for tool in ("ffmpeg", "ffprobe"):
        binary = bin_dir / tool
        binary.write_text("", encoding="utf-8")
        binary.chmod(0o755)
    return bin_dir / "ffmpeg"


def _fake_run(recorded: list[list[str]], stdout: bytes = b"1920x1080\n") -> Any:
    def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        recorded.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout, b"")

    return run


def test_the_probe_reaches_ffprobe_under_an_ffmpeg_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The issue's case, end to end: /opt/ffmpeg/bin/ffmpeg found its probe."""
    ffmpeg_bin = _install_fake_ffmpeg(tmp_path)
    recorded: list[list[str]] = []
    monkeypatch.setattr(burn.subprocess, "run", _fake_run(recorded))

    assert burn._probe_resolution(str(ffmpeg_bin), tmp_path / "video.mp4") == (1920, 1080)
    assert recorded[0][0] == str(ffmpeg_bin.with_name("ffprobe"))


def test_the_probe_still_works_from_an_ordinary_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard: passes either way, and pins that the common layout is unaffected."""
    ffmpeg_bin = _install_fake_ffmpeg(tmp_path, directory="usr")
    recorded: list[list[str]] = []
    monkeypatch.setattr(burn.subprocess, "run", _fake_run(recorded))

    assert burn._probe_resolution(str(ffmpeg_bin), tmp_path / "video.mp4") == (1920, 1080)


def test_a_missing_ffprobe_is_still_reported_as_no_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard: the fix must not turn "not installed" into a crash."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    only_ffmpeg = bin_dir / "ffmpeg"
    only_ffmpeg.write_text("", encoding="utf-8")
    monkeypatch.setattr(burn.shutil, "which", lambda name: None)

    assert burn._probe_resolution(str(only_ffmpeg), tmp_path / "video.mp4") is None


def _burn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ffmpeg_bin: Path) -> list[list[str]]:
    """Run main() against stubbed binaries and return every argv it built."""
    video = tmp_path / "in.mp4"
    video.write_bytes(b"")
    srt = tmp_path / "subs.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
    recorded: list[list[str]] = []
    monkeypatch.setattr(burn.subprocess, "run", _fake_run(recorded))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "burn.py",
            "--input",
            str(video),
            "--subtitles",
            str(srt),
            "--output",
            str(tmp_path / "out.mp4"),
            "--ffmpeg-path",
            str(ffmpeg_bin),
        ],
    )
    assert burn.main() == 0
    return recorded


def _force_style(argv: list[str]) -> str:
    return argv[argv.index("-vf") + 1]


def test_play_res_reaches_the_style_chain_from_an_ffmpeg_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The documented promise: margin_v and font_size in source-video pixels.

    Without PlayRes libass uses its own script resolution, so the margin is
    quietly measured in the wrong space -- the subtitles still burn.
    """
    ffmpeg_bin = _install_fake_ffmpeg(tmp_path)

    recorded = _burn(tmp_path, monkeypatch, ffmpeg_bin)
    capsys.readouterr()

    vf = _force_style(recorded[-1])
    assert "PlayResX=1920" in vf
    assert "PlayResY=1080" in vf


def test_an_explicit_play_res_is_unaffected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Guard: --play-res WxH never probed, and still does not."""
    ffmpeg_bin = _install_fake_ffmpeg(tmp_path)
    video = tmp_path / "in.mp4"
    video.write_bytes(b"")
    srt = tmp_path / "subs.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
    recorded: list[list[str]] = []
    monkeypatch.setattr(burn.subprocess, "run", _fake_run(recorded))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "burn.py",
            "--input",
            str(video),
            "--subtitles",
            str(srt),
            "--output",
            str(tmp_path / "out.mp4"),
            "--ffmpeg-path",
            str(ffmpeg_bin),
            "--play-res",
            "720x1280",
        ],
    )

    assert burn.main() == 0
    capsys.readouterr()
    vf = _force_style(recorded[-1])
    assert "PlayResX=720" in vf
    assert "PlayResY=1280" in vf
