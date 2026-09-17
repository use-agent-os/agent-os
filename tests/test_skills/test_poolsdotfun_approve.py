"""Unit tests for pools_write.py approve command.

Asserts:
- Omitting --amount defaults to unlimited allowance (2**256 - 1).
- Passing --amount 0 sets allowance to 0 (revocation), not unlimited.
- Passing a positive --amount (e.g. 5, 12.34) correctly parses decimal units.
- Dry-run replay command includes --amount when provided, ensuring PLAN_HASH matches on confirm.
- Dry-run replay command includes optional flags (--signer-env, --rpc).
- Negative amount or missing value flags raise ValueError.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = (
    ROOT / "src" / "agentos" / "skills" / "bundled" / "poolsdotfun-token-launcher" / "scripts"
)
SCRIPT_PATH = SCRIPTS_DIR / "pools_write.py"

TEST_KEY = "0x4f3edf983ac636a65a842ce7c78d9aa706d3b113bce9c46f30d7d21715b23b1d"
TEST_ADDRESS = "0x90F8bf6A479f320ead074411a4B0e7944Ea8c9C1"


@pytest.fixture
def pools_write(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.syspath_prepend(str(SCRIPTS_DIR))
    monkeypatch.setenv("POOLSFUN_PRIVATE_KEY", TEST_KEY)
    spec = importlib.util.spec_from_file_location("pools_write", SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pools_write"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mock_client():
    client = MagicMock()
    # Mock ERC20 allowance read to 0
    client.read.return_value = 0
    return client


def extract_plan_hash_and_replay(output: str) -> tuple[str, str]:
    hash_match = re.search(r"PLAN_HASH\s+([0-9a-fA-Fx]+)", output)
    assert hash_match, f"PLAN_HASH not found in output:\n{output}"
    plan_hash = hash_match.group(1)

    cmd_match = re.search(r"python3 pools_write\.py approve[^\n]+", output)
    assert cmd_match, f"Replay command not found in output:\n{output}"
    replay_cmd = cmd_match.group(0).strip()

    return plan_hash, replay_cmd


def test_approve_default_unlimited(pools_write, mock_client, capsys):
    args = {"_": ["approve"], "paired": "weth"}
    pools_write.cmd_approve(mock_client, args)

    out = capsys.readouterr().out
    assert "new allowance" in out
    assert "unlimited" in out
    plan_hash, replay_cmd = extract_plan_hash_and_replay(out)
    assert "--amount" not in replay_cmd
    assert f"--confirm {plan_hash}" in replay_cmd


def test_approve_zero_revocation(pools_write, mock_client, capsys, monkeypatch):
    """--amount 0 must set allowance to 0 (revoke), NOT unlimited (2**256 - 1)."""
    args = {"_": ["approve"], "paired": "weth", "amount": "0"}
    pools_write.cmd_approve(mock_client, args)

    out = capsys.readouterr().out
    assert "new allowance" in out
    assert "0" in out
    assert "unlimited" not in out

    plan_hash, replay_cmd = extract_plan_hash_and_replay(out)
    assert "--amount 0" in replay_cmd
    assert f"--confirm {plan_hash}" in replay_cmd

    # Now verify that confirming with the exact plan hash works and encodes 0
    send_mock = MagicMock()
    monkeypatch.setattr(pools_write, "_send", send_mock)

    confirm_args = {
        "_": ["approve"],
        "paired": "weth",
        "amount": "0",
        "broadcast": True,
        "confirm": plan_hash,
    }
    pools_write.cmd_approve(mock_client, confirm_args)
    assert send_mock.call_count == 1

    # Verify the encoded data passed to _send has amount 0
    _, _, _, data, _ = send_mock.call_args[0]
    expected_data = pools_write.encode_function_data(
        pools_write.ERC20_ABI, "approve", [pools_write.PARTY_FACTORY, 0]
    )
    assert data == expected_data


def test_approve_explicit_amount_and_replay_consistency(
    pools_write, mock_client, capsys, monkeypatch
):
    """Explicit --amount must be included in replay command to avoid hash mismatch."""
    args = {"_": ["approve"], "paired": "usdg", "amount": "100.5"}
    pools_write.cmd_approve(mock_client, args)

    out = capsys.readouterr().out
    assert "100.5" in out
    plan_hash, replay_cmd = extract_plan_hash_and_replay(out)
    assert "--amount 100.5" in replay_cmd
    assert f"--confirm {plan_hash}" in replay_cmd

    # Parse replay_cmd using pools_write.parse_args
    # replay_cmd: python3 pools_write.py approve --paired usdg --amount 100.5 ...
    tokens = replay_cmd.split()[2:]  # drop 'python3', 'pools_write.py'
    parsed_replay_args = pools_write.parse_args(tokens)

    send_mock = MagicMock()
    monkeypatch.setattr(pools_write, "_send", send_mock)

    # Re-executing with parsed replay arguments must succeed without hash mismatch
    pools_write.cmd_approve(mock_client, parsed_replay_args)
    assert send_mock.call_count == 1

    expected_amount = int(100.5 * 10**18)
    _, _, _, data, _ = send_mock.call_args[0]
    expected_data = pools_write.encode_function_data(
        pools_write.ERC20_ABI, "approve", [pools_write.PARTY_FACTORY, expected_amount]
    )
    assert data == expected_data


def test_approve_replay_preserves_optional_flags(pools_write, mock_client, capsys, monkeypatch):
    monkeypatch.setenv("CUSTOM_KEY", TEST_KEY)
    args = {
        "_": ["approve"],
        "paired": "weth",
        "amount": "5",
        "signer-env": "CUSTOM_KEY",
        "rpc": "https://custom.rpc.test",
    }
    pools_write.cmd_approve(mock_client, args)

    out = capsys.readouterr().out
    _, replay_cmd = extract_plan_hash_and_replay(out)
    assert "--amount 5" in replay_cmd
    assert "--signer-env CUSTOM_KEY" in replay_cmd
    assert "--rpc https://custom.rpc.test" in replay_cmd


def test_approve_negative_or_missing_amount_raises(pools_write, mock_client):
    with pytest.raises(ValueError, match="cannot be negative"):
        pools_write.cmd_approve(mock_client, {"_": ["approve"], "amount": "-1"})

    with pytest.raises(ValueError, match="needs a value"):
        pools_write.cmd_approve(mock_client, {"_": ["approve"], "amount": True})


def test_approve_hash_mismatch_rejected(pools_write, mock_client):
    args = {
        "_": ["approve"],
        "paired": "weth",
        "amount": "10",
        "broadcast": True,
        "confirm": "0xdeadbeef12345678",
    }
    with pytest.raises(RuntimeError, match="does not match this plan"):
        pools_write.cmd_approve(mock_client, args)
