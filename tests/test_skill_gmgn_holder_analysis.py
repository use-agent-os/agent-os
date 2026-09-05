"""Offline regression tests for the gmgn-holder-analysis script.

The script ``analyze.py`` reads positional CLI arguments (token address, chain,
optional language).  Without a guard it crashes with an ``IndexError`` when
invoked with no arguments or only ``--help`` — the agent sees a traceback
instead of a usage message.  The sibling ``gmgn-wallet-score/scripts/score.py``
was already guarded (see ``tests/test_skill_gmgn_wallet_score.py``); this
suite applies the same contract to ``analyze.py``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

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


def test_analyze_script_without_args_exits_with_code_2_and_usage() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "Usage:" in result.stderr
    assert "<token>" in result.stderr
    assert "<chain>" in result.stderr


def test_analyze_script_with_single_arg_exits_with_code_2() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "0x123"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "Usage:" in result.stderr
    assert "<chain>" in result.stderr


def test_analyze_script_help_flag_exits_with_code_0() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "Usage:" in result.stdout
