"""Test gmgn-holder-analysis argument validation."""
from __future__ import annotations

import subprocess
import sys

SCRIPT = "src/agentos/skills/bundled/gmgn-holder-analysis/scripts/analyze.py"


class TestArgumentValidation:
    def test_no_args_exits_with_usage(self) -> None:
        result = subprocess.run(
            [sys.executable, SCRIPT],
            capture_output=True, text=True,
        )
        assert result.returncode == 2
        assert "Usage:" in result.stderr

    def test_help_flag_exits_with_usage(self) -> None:
        for flag in ("-h", "--help", "-help"):
            result = subprocess.run(
                [sys.executable, SCRIPT, flag],
                capture_output=True, text=True,
            )
            assert result.returncode == 2, f"flag {flag} should exit 2"
            assert "Usage:" in result.stderr

    def test_only_token_exits_with_usage(self) -> None:
        result = subprocess.run(
            [sys.executable, SCRIPT, "0xabc"],
            capture_output=True, text=True,
        )
        assert result.returncode == 2
        assert "Usage:" in result.stderr
