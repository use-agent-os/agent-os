from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "gmgn-holder-analysis"
    / "scripts"
    / "analyze.py"
)


@pytest.mark.parametrize("args", [[], ["0x1234"]])
def test_missing_required_arguments_prints_usage_without_traceback(args: list[str]) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "Usage:" in result.stderr
    assert "<token_address> <chain>" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_prints_usage_without_running_analysis(flag: str) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), flag],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0
    assert "Usage:" in result.stdout
    assert "<token_address> <chain>" in result.stdout
    assert result.stderr == ""
    assert "Traceback" not in result.stdout
