"""video-still-animator validates numeric args before invoking ffmpeg (#2269).

Without validation, a bad --duration/--width/--height/--fps/--zoom-rate
reached ffmpeg as-is and failed deep inside the zoompan/scale filter chain,
surfacing only an opaque "Error: ffmpeg exited 1" with no indication of
which argument was wrong. Each case here must be rejected by name before
any subprocess is spawned, so these tests never require an ffmpeg binary.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "video-still-animator"
    / "scripts"
    / "animate.py"
)


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("animate_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def animate() -> Any:
    return _load_module()


@pytest.mark.parametrize(
    ("extra_args", "expected"),
    [
        pytest.param(["--duration", "0"], "--duration must be > 0, got 0.0", id="duration-zero"),
        pytest.param(
            ["--duration", "-5"], "--duration must be > 0, got -5.0", id="duration-negative"
        ),
        pytest.param(["--fps", "0"], "--fps must be > 0, got 0", id="fps-zero"),
        pytest.param(["--fps", "-1"], "--fps must be > 0, got -1", id="fps-negative"),
        pytest.param(
            ["--width", "719"],
            "--width must be a positive even number, got 719",
            id="width-odd",
        ),
        pytest.param(
            ["--width", "-1"],
            "--width must be a positive even number, got -1",
            id="width-negative",
        ),
        pytest.param(
            ["--height", "0"],
            "--height must be a positive even number, got 0",
            id="height-zero",
        ),
        pytest.param(
            ["--height", "721"],
            "--height must be a positive even number, got 721",
            id="height-odd",
        ),
        pytest.param(
            ["--zoom-rate", "-0.5"],
            "--zoom-rate must be >= 0, got -0.5",
            id="zoom-rate-negative",
        ),
    ],
)
def test_invalid_numeric_args_are_rejected_before_ffmpeg_runs(
    animate: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    extra_args: list[str],
    expected: str,
) -> None:
    src = tmp_path / "cover.png"
    src.write_bytes(b"not a real png, but existence is all main() checks before validation")
    out = tmp_path / "out.mp4"

    def _must_not_run(cmd: list[str], **kwargs: Any) -> None:
        raise AssertionError("ffmpeg must not run when validation should have rejected the input")

    monkeypatch.setattr(subprocess, "run", _must_not_run)
    monkeypatch.setattr(
        sys,
        "argv",
        ["animate.py", "--input", str(src), "--output", str(out), *extra_args],
    )

    assert animate.main() == 1
    assert expected in capsys.readouterr().err
    assert not out.exists()


def test_valid_args_pass_validation_and_reach_ffmpeg(
    animate: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The validation gate must not reject the documented defaults or a
    caller-supplied even width/height."""
    src = tmp_path / "cover.png"
    src.write_bytes(b"stand-in bytes; ffmpeg invocation itself is stubbed out")
    out = tmp_path / "out.mp4"

    invoked = {}

    class _FakeCompletedProcess:
        returncode = 0
        stderr = b""

    def _fake_run(cmd: list[str], **kwargs: Any) -> _FakeCompletedProcess:
        invoked["cmd"] = cmd
        return _FakeCompletedProcess()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "animate.py",
            "--input",
            str(src),
            "--output",
            str(out),
            "--duration",
            "3",
            "--width",
            "64",
            "--height",
            "64",
            "--fps",
            "24",
            "--zoom-rate",
            "0",
        ],
    )

    assert animate.main() == 0
    assert invoked, "ffmpeg should have been invoked once validation passed"
