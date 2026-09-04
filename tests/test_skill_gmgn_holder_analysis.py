"""Regression tests for the gmgn-holder-analysis bundled script."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ANALYZE_SCRIPT = (
    ROOT
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "gmgn-holder-analysis"
    / "scripts"
    / "analyze.py"
)


def test_analyze_script_handles_missing_args_gracefully() -> None:
    """Invoking analyze.py without args prints usage and exits cleanly with code 2."""
    result = subprocess.run(
        [sys.executable, str(ANALYZE_SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "Usage: analyze.py" in result.stdout


def test_analyze_script_handles_help_flag() -> None:
    """Invoking analyze.py with --help prints usage and exits with code 2."""
    result = subprocess.run(
        [sys.executable, str(ANALYZE_SCRIPT), "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "Usage: analyze.py" in result.stdout


def test_analyze_script_handles_single_arg() -> None:
    """Invoking analyze.py with only one argument prints usage and exits with code 2."""
    result = subprocess.run(
        [sys.executable, str(ANALYZE_SCRIPT), "0x1234"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "Usage: analyze.py" in result.stdout
