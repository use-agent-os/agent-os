"""How the wallet-hardening fixes hold together once they are all in one engine.

Each fix has its own regression file; these tests pin the points where two of
them meet, so a later change to one cannot quietly weaken the other:

* the operator's lock latch (WAL-22) against the sealed ``unlock.key``
  (WAL-01) and the configured ``unlock_mode`` (WAL-25);
* the ledger seal key (WAL-05) across the plain-text -> sealed ``unlock.key``
  migration and a pre-seal vault's first unlock;
* the order of the checks before a signature: the row's seal (WAL-05), then
  the operator's approval binding (WAL-12), then the live off switch (WAL-11).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentos.trading.chains import BASE
from agentos.trading.service import TradingError, TradingService
from agentos.trading.vault import LEDGER_SEAL, Vault
from tests.test_trading.conftest import FAST_KDF, PASSWORD
from tests.test_trading.fakes import FakeChain
from tests.test_trading.test_send_tools import _wire_send_effects
from tests.test_trading.test_signing_integrity import ALICE, BAD_1, BAD_2, _agent_send

BENIGN = "0x" + "11" * 20
ATTACKER = "0x" + "ee" * 20


def _auto_vault(root: Path) -> Vault:
    """A vault as the gateway builds it with ``[trading] unlock_mode = "auto"``."""
    return Vault(root, configured_mode=lambda: "auto", **FAST_KDF)


def _write_legacy_plaintext(vault: Vault, password: str) -> None:
    with vault.unlock_path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(password)


def _raw_update(service: TradingService, sql: str, params: tuple[Any, ...]) -> None:
    """A write that skips the engine (and so its seal), as an outside writer does."""
    conn = service.ledger._conn  # noqa: SLF001 - the point is to bypass the engine
    conn.execute(sql, params)
    conn.commit()


# ── lock latch x sealed unlock.key x unlock_mode ──────────────────────────


def test_operator_lock_survives_a_restart_in_auto_mode_with_a_sealed_unlock_key(
    vault_root: Path,
) -> None:
    vault = _auto_vault(vault_root)
    vault.setup(PASSWORD)  # no mode given: the configured auto
    vault.create("main")
    assert vault.unlock_mode == "auto"
    assert str(vault.status()["unlockKeyProtection"]).startswith("sealed:")
    seal_key = vault.ledger_seal_key()
    assert seal_key is not None
    vault.lock()

    restarted = _auto_vault(vault_root)
    # The sealed unlock.key would open the vault; the operator's latch wins.
    assert restarted.try_auto_unlock() is False
    assert restarted.unlocked is False
    assert restarted.ledger_seal_key() is None
    status = restarted.status()
    assert status["lockedByOperator"] is True
    assert status["unlockMode"] == "auto"
    assert str(status["unlockKeyProtection"]).startswith("sealed:")

    # The password is the way out; the seal key is the one rows were sealed with.
    restarted.unlock(PASSWORD)
    assert restarted.operator_locked is False
    assert restarted.ledger_seal_key() == seal_key
    # And the next restart auto-unlocks again, from the sealed file.
    again = _auto_vault(vault_root)
    assert again.try_auto_unlock() is True
    assert again.ledger_seal_key() == seal_key


def test_operator_lock_also_outranks_a_legacy_plaintext_unlock_key(vault_root: Path) -> None:
    vault = _auto_vault(vault_root)
    vault.setup(PASSWORD)
    vault.lock()
    _write_legacy_plaintext(vault, PASSWORD)
    restarted = _auto_vault(vault_root)
    assert restarted.try_auto_unlock() is False
    # Nothing was read, so nothing was migrated either: the latch stops it first.
    assert restarted.status()["unlockKeyProtection"] == "plaintext"


def test_a_manual_policy_keeps_a_latched_vault_locked_and_unlock_still_lifts_it(
    vault_root: Path,
) -> None:
    Vault(vault_root, **FAST_KDF).setup(PASSWORD, "auto")
    Vault(vault_root, **FAST_KDF).lock()
    manual = Vault(vault_root, configured_mode=lambda: "manual", **FAST_KDF)
    assert manual.try_auto_unlock() is False
    manual.unlock(PASSWORD)
    assert manual.unlocked is True and manual.operator_locked is False
    # Under manual, the stored password is never re-sealed by an unlock.
    assert manual.unlock_mode == "manual"


def test_auto_unlock_does_not_lift_the_latch_but_set_unlock_mode_does(vault_root: Path) -> None:
    vault = _auto_vault(vault_root)
    vault.setup(PASSWORD)
    vault.lock()
    restarted = _auto_vault(vault_root)
    assert restarted.try_auto_unlock() is False
    assert restarted.operator_locked is True
    restarted.set_unlock_mode("auto", PASSWORD)
    assert restarted.unlocked is True and restarted.operator_locked is False
    assert _auto_vault(vault_root).try_auto_unlock() is True


# ── ledger seal key across the unlock.key migrations ──────────────────────


def test_the_seal_key_is_unchanged_by_the_plaintext_to_sealed_migration(vault_root: Path) -> None:
    vault = _auto_vault(vault_root)
    vault.setup(PASSWORD)
    seal_key = vault.ledger_seal_key()
    _write_legacy_plaintext(vault, PASSWORD)

    migrated = _auto_vault(vault_root)
    assert migrated.try_auto_unlock() is True
    assert str(migrated.status()["unlockKeyProtection"]).startswith("sealed:")
    assert migrated.ledger_seal_key() == seal_key
    assert migrated.take_new_seal() is False

    after = _auto_vault(vault_root)
    assert after.try_auto_unlock() is True
    assert after.ledger_seal_key() == seal_key


def test_a_pre_seal_vault_with_a_plaintext_unlock_key_migrates_both_at_once(
    vault_root: Path,
) -> None:
    """An install from before both fixes: no seal salt, a plain-text password file."""
    _auto_vault(vault_root).setup(PASSWORD)
    index_path = vault_root / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index.pop(LEDGER_SEAL)
    index_path.write_text(json.dumps(index), encoding="utf-8")
    _write_legacy_plaintext(Vault(vault_root, **FAST_KDF), PASSWORD)

    first = _auto_vault(vault_root)
    assert first.try_auto_unlock() is True
    assert first.take_new_seal() is True  # the service adopts the live rows once
    assert str(first.status()["unlockKeyProtection"]).startswith("sealed:")
    seal_key = first.ledger_seal_key()

    second = _auto_vault(vault_root)
    assert second.try_auto_unlock() is True  # from the sealed file now
    assert second.take_new_seal() is False
    assert second.ledger_seal_key() == seal_key


async def test_rows_sealed_before_the_migration_still_verify_after_it(
    funded_service: TradingService, base_chain: FakeChain
) -> None:
    service = funded_service
    orders = await service.send(
        chain=BASE,
        wallet=service.test_wallet,  # type: ignore[attr-defined]
        token="ETH",
        recipients=[{"to": BENIGN, "amount": "0.001"}],
        initiator="agent",
        session_key=None,
        note=None,
    )
    order_id = str(orders[0]["orderId"])
    assert orders[0]["status"] == "awaiting_approval"

    # A gateway restart onto a plain-text unlock.key (an install from before
    # it was sealed): the new process migrates it, and the seal key it derives
    # is the one the parked order was sealed with.
    _write_legacy_plaintext(service.vault, PASSWORD)
    service.vault = Vault(service.vault.root, **FAST_KDF)
    service.vault.configured_mode = service._configured_unlock_mode  # noqa: SLF001
    assert service.ensure_unlocked() is True
    assert str(service.vault.status()["unlockKeyProtection"]).startswith("sealed:")
    row, verdict = service._check_sealed("order", order_id)  # noqa: SLF001
    assert row is not None and verdict == "ok"

    result = await service.approve(order_id)
    assert result["status"] == "submitted"
    assert len(base_chain.sent) == 1


# ── seal -> approval binding -> off switch, before every signature ────────


async def test_a_batch_with_one_edited_leg_signs_no_leg(
    funded_service: TradingService, base_chain: FakeChain
) -> None:
    service = funded_service
    _wire_send_effects(base_chain, service.test_wallet)  # type: ignore[attr-defined]
    orders = await _agent_send(
        service,
        [{"to": ALICE, "amount": "1"}, {"to": BAD_1, "amount": "2"}, {"to": BAD_2, "amount": "3"}],
    )
    batch_id = orders[0]["batchId"]
    digest = service.batch_approval(batch_id)["digest"]
    # The second leg is rewritten in the ledger file after the operator read
    # the batch; the first leg is untouched and would otherwise sign first.
    _raw_update(
        service,
        "UPDATE orders SET recipient = ? WHERE order_id = ?",
        (ATTACKER, orders[1]["orderId"]),
    )

    result = await service.approve(orders[0]["orderId"], approval_digest=digest)

    assert base_chain.sent == []
    assert result["status"] == "failed"
    statuses = {o["orderId"]: (o["status"], o["reason"]) for o in service.batch(batch_id)}
    assert all(status == "failed" for status, _ in statuses.values())
    assert all(str(reason).startswith("trading.integrity:") for _, reason in statuses.values())
    alerts = [p for e, p in service.events if e == "trading.integrity.alert"]  # type: ignore[attr-defined]
    assert [a["id"] for a in alerts] == [orders[1]["orderId"]]


async def test_the_permit_signature_is_held_to_the_seal_and_the_approval(
    funded_service: TradingService,
) -> None:
    """``_presign_order_check``: the last look before a permit is signed."""
    service = funded_service
    orders = await service.send(
        chain=BASE,
        wallet=service.test_wallet,  # type: ignore[attr-defined]
        token="ETH",
        recipients=[{"to": BENIGN, "amount": "0.001"}],
        initiator="agent",
        session_key=None,
        note=None,
    )
    order_id = str(orders[0]["orderId"])
    service._presign_order_check(order_id)  # noqa: SLF001 - a parked, sealed row passes

    # ``approved`` is engine state, not a sealed term, so the seal still holds;
    # with no approval held for it, it is not signed.
    _raw_update(service, "UPDATE orders SET status = 'approved' WHERE order_id = ?", (order_id,))
    with pytest.raises(TradingError) as info:
        service._presign_order_check(order_id)  # noqa: SLF001
    assert info.value.code == "trading.approval_changed"

    # A rewritten term fails the seal first, whatever the approval says.
    _raw_update(service, "UPDATE orders SET recipient = ? WHERE order_id = ?", (ATTACKER, order_id))
    with pytest.raises(TradingError) as info:
        service._presign_order_check(order_id)  # noqa: SLF001
    assert info.value.code == "trading.integrity"


async def test_trading_switched_off_refuses_the_approval_before_any_check(
    funded_service: TradingService, base_chain: FakeChain
) -> None:
    service = funded_service
    orders = await service.send(
        chain=BASE,
        wallet=service.test_wallet,  # type: ignore[attr-defined]
        token="ETH",
        recipients=[{"to": BENIGN, "amount": "0.001"}],
        initiator="agent",
        session_key=None,
        note=None,
    )
    order_id = str(orders[0]["orderId"])
    service.config.enabled = False
    with pytest.raises(TradingError) as info:
        await service.approve(order_id)
    assert info.value.code == "trading.disabled"
    assert service.get_order(order_id)["status"] == "awaiting_approval"
    assert base_chain.sent == []
