"""Offline regression tests for the gmgn-holder-analysis script.

- Issue #957: ``analyze.py`` missing args/help guards.
- Robust parsing: Tolerates null/sparse fields, wrapped API envelopes, and empty responses
  without throwing TypeErrors or KeyErrors.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

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


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "args",
    [pytest.param((), id="no-args"), pytest.param(("0xabc",), id="one-arg")],
)
def test_missing_args_print_usage_and_exit_2(args: tuple[str, ...]) -> None:
    result = _run(*args)
    assert result.returncode == 2
    assert "Usage:" in result.stderr
    assert "<token_address>" in result.stderr
    assert "<chain>" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_flag_prints_usage_and_exits_0(flag: str) -> None:
    result = _run(flag)
    assert result.returncode == 0
    assert "Usage:" in result.stdout
    assert "<token_address>" in result.stdout
    assert "Traceback" not in result.stderr


class _FakeCompleted:
    def __init__(self, stdout: str) -> None:
        self.returncode = 0
        self.stdout = stdout
        self.stderr = ""


def _run_analyze(
    monkeypatch: pytest.MonkeyPatch,
    holders_payload: Any,
    devs_payload: Any,
    created_payload: Any = None,
    lang: str = "en",
) -> str:
    """Execute analyze.py in-process with a stubbed ``gmgn-cli``."""
    seen: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> _FakeCompleted:
        seen.append(cmd)
        assert cmd[0] == "gmgn-cli", cmd
        argv = cmd[1:]
        if argv[:2] == ["token", "holders"]:
            if "--tag" in argv and "dev" in argv:
                return _FakeCompleted(json.dumps(devs_payload))
            return _FakeCompleted(json.dumps(holders_payload))
        if argv[:2] == ["portfolio", "created-tokens"]:
            return _FakeCompleted(json.dumps(created_payload or {}))
        raise AssertionError(f"unexpected gmgn-cli invocation: {argv}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys, "argv", [str(SCRIPT), "0x1234567890123456789012345678901234567890", "eth", lang]
    )

    spec = importlib.util.spec_from_file_location("gmgn_analyze_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        spec.loader.exec_module(module)

    assert seen, "analyze.py never called gmgn-cli"
    return buf.getvalue()


def test_analyze_handles_sparse_and_null_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """Null fields from sparse API responses must not crash with TypeError."""
    holders_payload = {
        "list": [
            {
                "address": "0x1111111111111111111111111111111111111111",
                "amount_percentage": None,
                "balance": None,
                "usd_value": None,
                "sell_amount_percentage": None,
                "buy_tx_count_cur": None,
                "sell_tx_count_cur": None,
                "start_holding_at": None,
                "profit": None,
                "unrealized_pnl": None,
                "unrealized_profit": None,
                "avg_cost": None,
                "native_balance": None,
                "addr_type": None,
                "tags": None,
                "maker_token_tags": None,
            },
            {
                "address": "0x2222222222222222222222222222222222222222",
                "amount_percentage": 0.15,
                "balance": 1000.0,
                "usd_value": 500.0,
                "sell_amount_percentage": 0.1,
                "buy_tx_count_cur": 2,
                "sell_tx_count_cur": 0,
                "start_holding_at": 1700000000,
                "profit": 100.0,
                "unrealized_pnl": 0.25,
                "unrealized_profit": 50.0,
                "avg_cost": 0.4,
                "native_balance": 1000000000000000000,
                "addr_type": 0,
                "tags": ["smart_degen"],
                "maker_token_tags": ["whale"],
            },
        ]
    }
    devs_payload = {
        "list": [
            {
                "address": "0xdev1111111111111111111111111111111111111",
                "maker_token_tags": ["creator"],
                "amount_percentage": None,
                "balance": None,
                "realized_profit": None,
            }
        ]
    }
    created_payload = {
        "tokens": None,
        "inner_count": None,
        "open_count": None,
        "creator_ath_info": None,
    }

    stdout = _run_analyze(
        monkeypatch,
        holders_payload,
        devs_payload,
        created_payload,
        lang="en",
    )
    assert "Holder Chip Analysis" in stdout
    assert "Dump Risk" in stdout
    assert "OUTPUT COMPLETE" in stdout


def test_analyze_handles_wrapped_envelope_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wrapped responses with {code: 0, data: {list: [...]}} must be unwrapped properly."""
    holders_payload = {
        "code": 0,
        "data": {
            "list": [
                {
                    "address": "0x3333333333333333333333333333333333333333",
                    "amount_percentage": 0.2,
                    "balance": 2000.0,
                    "usd_value": 1000.0,
                    "addr_type": 0,
                }
            ]
        },
    }
    devs_payload = {
        "code": 0,
        "data": {
            "list": [],
        },
    }

    stdout = _run_analyze(monkeypatch, holders_payload, devs_payload, lang="zh")
    assert "Holder 筹码分析" in stdout
    assert "OUTPUT COMPLETE" in stdout
