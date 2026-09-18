"""Offline regression tests for the gmgn-holder-analysis script (issue #957).

``analyze.py`` indexed ``sys.argv[1]`` and ``sys.argv[2]`` at import time with
no length check, so running it with no arguments -- or with ``--help`` -- died
with an unhandled ``IndexError`` traceback instead of printing usage. Mirrors
the guard the sibling gmgn-wallet-score script already carries.
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
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _run_analyze_imported(
    monkeypatch: pytest.MonkeyPatch,
    *,
    holders: list[dict[str, Any]],
    devs: list[dict[str, Any]],
    created_tokens_result: Any | Exception = None,
) -> str:
    def fake_run(cmd: list[str], **kwargs: Any) -> _FakeCompleted:
        assert cmd[0] == "gmgn-cli", cmd
        argv = cmd[1:]
        if argv[:2] == ["token", "holders"]:
            if "--tag" in argv and argv[argv.index("--tag") + 1] == "dev":
                return _FakeCompleted(json.dumps({"list": devs}))
            return _FakeCompleted(json.dumps({"list": holders}))
        if argv[:2] == ["portfolio", "created-tokens"]:
            if isinstance(created_tokens_result, Exception):
                return _FakeCompleted(
                    stderr=str(created_tokens_result),
                    returncode=1,
                )
            if created_tokens_result is None:
                return _FakeCompleted(json.dumps({}))
            return _FakeCompleted(json.dumps(created_tokens_result))
        raise AssertionError(f"unexpected gmgn-cli invocation: {argv}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "0xTokenAddress1234567890", "bsc", "en"])

    spec = importlib.util.spec_from_file_location("gmgn_analyze_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        spec.loader.exec_module(module)

    return buf.getvalue()


def test_secondary_created_tokens_failure_does_not_abort_analysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When creator tokens history fails (rate limit/error), primary analysis must complete."""
    holders = [
        {
            "address": "0xCreatorDev111111111111",
            "balance": 1000.0,
            "usd_value": 500.0,
            "amount_percentage": 0.05,
            "addr_type": 0,
            "maker_token_tags": ["creator"],
            "start_holding_at": 1700000000,
        },
        {
            "address": "0xNormalHolder222222222222",
            "balance": 2000.0,
            "usd_value": 1000.0,
            "amount_percentage": 0.10,
            "addr_type": 0,
            "start_holding_at": 1700000000,
        },
    ]
    devs = [
        {
            "address": "0xCreatorDev111111111111",
            "maker_token_tags": ["creator"],
            "balance": 1000.0,
            "realized_profit": 0.0,
        }
    ]

    stdout = _run_analyze_imported(
        monkeypatch,
        holders=holders,
        devs=devs,
        created_tokens_result=RuntimeError("500 Internal Server Error"),
    )

    assert "Holder Chip Analysis" in stdout
    assert "Dump Risk" in stdout
    assert "Dev Wallets" in stdout
    assert "OUTPUT COMPLETE" in stdout


def test_secondary_created_tokens_success_includes_token_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When creator tokens history succeeds, token history is printed."""
    holders = [
        {
            "address": "0xCreatorDev111111111111",
            "balance": 1000.0,
            "usd_value": 500.0,
            "amount_percentage": 0.05,
            "addr_type": 0,
            "maker_token_tags": ["creator"],
            "start_holding_at": 1700000000,
        }
    ]
    devs = [
        {
            "address": "0xCreatorDev111111111111",
            "maker_token_tags": ["creator"],
            "balance": 1000.0,
            "realized_profit": 0.0,
        }
    ]

    stdout = _run_analyze_imported(
        monkeypatch,
        holders=holders,
        devs=devs,
        created_tokens_result={
            "tokens": [{"symbol": "OLDCOIN", "market_cap": 1200000, "is_open": True}],
            "open_count": 1,
            "inner_count": 0,
            "creator_ath_info": {
                "token_name": "Old Coin",
                "token_symbol": "OLDCOIN",
                "ath_mc": 5000000,
            },
        },
    )

    assert "Holder Chip Analysis" in stdout
    assert "Token history" in stdout
    assert "OLDCOIN" in stdout
    assert "All-time high MC" in stdout


def test_secondary_created_tokens_non_dict_payload_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When created-tokens returns a list or raw wrapped payload, it should not crash."""
    holders = [
        {
            "address": "0xCreatorDev111111111111",
            "balance": 1000.0,
            "usd_value": 500.0,
            "amount_percentage": 0.05,
            "addr_type": 0,
            "maker_token_tags": ["creator"],
            "start_holding_at": 1700000000,
        }
    ]
    devs = [
        {
            "address": "0xCreatorDev111111111111",
            "maker_token_tags": ["creator"],
            "balance": 1000.0,
            "realized_profit": 0.0,
        }
    ]

    stdout = _run_analyze_imported(
        monkeypatch,
        holders=holders,
        devs=devs,
        created_tokens_result=[{"symbol": "OLD", "market_cap": 100000}],
    )

    assert "Holder Chip Analysis" in stdout
    assert "Dev Wallets" in stdout
    assert "OUTPUT COMPLETE" in stdout
