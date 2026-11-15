"""WAL-25: ``[trading] unlock_mode`` decides how the vault unlocks.

The key was declared, documented and validated but read by nothing:
``wallet.setup`` defaulted to ``auto`` in the handler and the CLI, so a
gateway configured ``manual`` still wrote the vault password to
``unlock.key`` and unlocked for any caller.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agentos.trading.vault import Vault
from tests.test_gateway.test_rpc_trading import _agent_ctx, call, ctx, stack  # noqa: F401
from tests.test_trading.conftest import FAST_KDF, PASSWORD


def _vault(root: Path, policy: list[str]) -> Vault:
    return Vault(root, configured_mode=lambda: policy[0], **FAST_KDF)


async def test_setup_without_a_mode_follows_a_manual_config(
    stack: dict[str, Any],  # noqa: F811
    ctx: Any,  # noqa: F811
) -> None:
    stack["config"].trading.unlock_mode = "manual"
    res = await call("wallet.setup", {"password": PASSWORD}, ctx)
    assert res.ok, res.error
    assert res.payload["unlockMode"] == "manual"
    vault = stack["service"].vault
    assert not vault.unlock_path.exists()
    vault.lock()
    assert stack["service"].ensure_unlocked() is False
    # The agent's next read cannot unlock it either.
    res = await call("wallet.status", {}, _agent_ctx(stack))
    assert res.ok and res.payload["unlocked"] is False


async def test_explicit_auto_is_refused_under_a_manual_config(
    stack: dict[str, Any],  # noqa: F811
    ctx: Any,  # noqa: F811
) -> None:
    stack["config"].trading.unlock_mode = "manual"
    res = await call("wallet.setup", {"password": PASSWORD, "unlockMode": "auto"}, ctx)
    assert res.ok is False and res.error.code == "wallet.unlock_mode_policy"
    assert not stack["service"].vault.initialized
    assert not stack["service"].vault.unlock_path.exists()


async def test_setup_without_a_mode_is_auto_by_default(
    stack: dict[str, Any],  # noqa: F811
    ctx: Any,  # noqa: F811
) -> None:
    res = await call("wallet.setup", {"password": PASSWORD}, ctx)
    assert res.ok and res.payload["unlockMode"] == "auto"


def test_existing_auto_vault_is_not_auto_unlocked_under_manual(tmp_path: Path) -> None:
    policy = ["auto"]
    vault = _vault(tmp_path / "wallets", policy)
    vault.setup(PASSWORD, "auto")
    assert vault.unlock_path.exists()
    policy[0] = "manual"
    fresh = _vault(tmp_path / "wallets", policy)
    assert fresh.unlock_mode == "manual"
    assert fresh.stored_unlock_mode == "auto"
    assert fresh.try_auto_unlock() is False
    assert fresh.unlocked is False
    status = fresh.status()
    assert status["unlockMode"] == "manual"
    assert status["storedUnlockMode"] == "auto"
    assert status["configuredUnlockMode"] == "manual"
    assert status["unlockKeyPresent"] is True
    # Never removed implicitly: the operator's explicit switch does it.
    assert fresh.unlock_path.exists()
    fresh.set_unlock_mode("manual", PASSWORD)
    assert not fresh.unlock_path.exists()
    assert fresh.status()["storedUnlockMode"] == "manual"


def test_manual_policy_refuses_switching_to_auto(tmp_path: Path) -> None:
    from agentos.trading.vault import UnlockModePolicyError

    vault = _vault(tmp_path / "wallets", ["manual"])
    vault.setup(PASSWORD)
    assert not vault.unlock_path.exists()
    with pytest.raises(UnlockModePolicyError):
        vault.set_unlock_mode("auto", PASSWORD)
    assert not vault.unlock_path.exists()


def test_change_password_does_not_write_unlock_key_under_manual(tmp_path: Path) -> None:
    policy = ["auto"]
    vault = _vault(tmp_path / "wallets", policy)
    vault.setup(PASSWORD, "auto")
    before = vault.unlock_path.read_bytes()
    policy[0] = "manual"
    vault.change_password(PASSWORD, PASSWORD + "-new")
    assert vault.unlock_path.read_bytes() == before


def test_cli_setup_sends_no_default_mode_and_prints_the_gateways(monkeypatch) -> None:
    from agentos.cli import wallet_cmd

    calls: list[tuple[str, dict[str, Any]]] = []

    class _Client:
        async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            calls.append((method, params))
            return {"initialized": True, "unlockMode": "manual"}

    def run(fn: Any, **_: Any) -> Any:
        import asyncio

        return asyncio.run(fn(_Client()))

    monkeypatch.setattr(wallet_cmd, "run_gateway_sync", run)
    result = CliRunner().invoke(wallet_cmd.app, ["setup"], input="pw-pw-pw\npw-pw-pw\n")
    assert result.exit_code == 0, result.output
    assert calls == [("wallet.setup", {"password": "pw-pw-pw"})]
    assert "manual" in result.output
    assert "unlock.key" not in result.output
