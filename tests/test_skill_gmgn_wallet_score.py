"""Offline regression tests for the gmgn-wallet-score script."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT / "src" / "agentos" / "skills" / "bundled" / "gmgn-wallet-score" / "scripts" / "score.py"
)


def test_score_script_without_args_exits_with_code_2_and_usage() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "Usage:" in result.stderr
    assert "<wallet>" in result.stderr
    assert "<chain>" in result.stderr


def test_score_script_with_single_arg_exits_with_code_2() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "0x123"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "Usage:" in result.stderr
    assert "<wallet>" in result.stderr
    assert "<chain>" in result.stderr


def test_score_script_help_flag_exits_with_code_0() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "Usage:" in result.stdout


def test_score_script_reconfigures_stdout_encoding() -> None:
    """The script must safely reconfigure stdout/stderr on restrictive encodings."""
    import os

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "ascii"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "-h"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0
    assert "Usage:" in result.stdout


def test_score_script_survives_non_utf8_stdout_with_cjk_output(monkeypatch) -> None:
    """The script must survive non-UTF-8 console stdout when outputting CJK characters."""
    import contextlib
    import importlib.util
    import io
    import json

    stats = {
        "buy": 0,
        "sell": 0,
        "realized_profit": 0,
        "bought_cost": 0,
        "realized_profit_pnl": 0,
        "pnl_stat": {
            "token_num": 0,
            "winrate": 0,
            "avg_holding_period": 0,
            "pnl_gt_5x_num": 0,
            "pnl_2x_5x_num": 0,
            "pnl_0x_2x_num": 0,
            "pnl_nd5_0x_num": 0,
            "pnl_lt_nd5_num": 0,
        },
        "common": {"created_token_count": 0},
    }

    class _FakeCompleted:
        returncode = 0
        stdout = json.dumps(stats)
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: _FakeCompleted())
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "0xWalletAddress", "sol", "zh"])

    spec = importlib.util.spec_from_file_location("gmgn_score_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            spec.loader.exec_module(module)
        except SystemExit as exc:
            assert exc.code == 0
    assert "近 7 天没有真实买卖记录" in buf.getvalue()
