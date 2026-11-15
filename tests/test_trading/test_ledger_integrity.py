"""The signer does not trust a ledger row it reads back (WAL-05).

The trading ledger is a SQLite file an unsandboxed agent shell can open as the
same user. The audit's reproduction: an agent parks a small send, rewrites the
parked row's recipient and amount through its own sqlite connection, and the
operator's approval of the benign card signs the rewritten one. Every row that
can lead to a signature now carries the engine's seal
(``agentos.trading.integrity``), checked before anything signs.

These tests stand in for the agent's shell with a second ``sqlite3``
connection to the ledger file, or with raw SQL on the ledger's own connection,
which skips the engine exactly as an outside writer does.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from agentos.trading import integrity
from agentos.trading.chains import BASE
from agentos.trading.ledger import Ledger
from agentos.trading.service import TradingError, TradingService
from agentos.trading.vault import LEDGER_SEAL, Vault
from tests.test_trading import test_dca as dca_suite
from tests.test_trading.conftest import FAST_KDF, PASSWORD
from tests.test_trading.fakes import USDC, WETH, FakeChain, decode_fake_raw, fake_sign_tx
from tests.test_trading.test_dca import (
    HOUR,
    T0,
    World,
    _settle,
)

# The DCA suite's world (a funded wallet on a pinned clock) and its stable ids.
world = dca_suite.world
ids = dca_suite.ids

BENIGN = "0x" + "11" * 20
ATTACKER = "0x" + "ee" * 20
DRAIN_WEI = 95 * 10**16  # 0.95 ETH


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[Ledger]:
    """A ledger on disk, so a second connection (the agent's shell) can reach it."""
    store = Ledger(tmp_path / "state" / "trading.sqlite")
    yield store
    store.close()


def _outside(ledger: Ledger, sql: str, params: tuple[Any, ...] = ()) -> None:
    """A write from another process: its own connection, no engine, no seal."""
    conn = sqlite3.connect(os.fspath(ledger.path), timeout=5.0)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _alerts(service: TradingService) -> list[dict[str, Any]]:
    return [p for e, p in service.events if e == "trading.integrity.alert"]  # type: ignore[attr-defined]


async def _park_send(service: TradingService) -> str:
    orders = await service.send(
        chain=BASE,
        wallet=service.test_wallet,  # type: ignore[attr-defined]
        token="ETH",
        recipients=[{"to": BENIGN, "amount": "0.001"}],
        initiator="agent",
        session_key=None,
        note="pay vendor",
    )
    assert orders[0]["status"] == "awaiting_approval"
    return str(orders[0]["orderId"])


# ── orders ─────────────────────────────────────────────────────────────────


async def test_rewritten_parked_send_is_refused_not_signed(
    funded_service: TradingService, base_chain: FakeChain, ledger: Ledger
) -> None:
    """The audit's reproduction: the operator approves the card, the attacker's row never signs."""
    service = funded_service
    order_id = await _park_send(service)
    _outside(
        ledger,
        "UPDATE orders SET recipient = ?, amount_raw = ?, amount_human = ? WHERE order_id = ?",
        (ATTACKER, str(DRAIN_WEI), "0.95", order_id),
    )

    result = await service.approve(order_id)

    assert base_chain.sent == []
    assert result["status"] == "failed"
    assert str(result["reason"]).startswith("trading.integrity:")
    alert = _alerts(service)[-1]
    assert alert["kind"] == "order" and alert["id"] == order_id
    assert alert["verdict"] == "mismatch"


async def test_the_same_send_untouched_is_approved_and_signed(
    funded_service: TradingService, base_chain: FakeChain
) -> None:
    """Control: the engine's own writes (park, approve, submit) keep the seal valid."""
    service = funded_service
    order_id = await _park_send(service)

    result = await service.approve(order_id)

    assert result["status"] == "submitted"
    sent = [decode_fake_raw(raw) for raw in base_chain.sent]
    assert [tx["to"].lower() for tx in sent] == [BENIGN]
    assert int(sent[0]["value"]) == 10**15
    assert _alerts(service) == []


async def test_a_deleted_seal_is_refused_too(
    funded_service: TradingService, base_chain: FakeChain, ledger: Ledger
) -> None:
    service = funded_service
    order_id = await _park_send(service)
    _outside(ledger, "DELETE FROM seals WHERE kind = 'order' AND id = ?", (order_id,))

    result = await service.approve(order_id)

    assert base_chain.sent == [] and result["status"] == "failed"
    assert _alerts(service)[-1]["verdict"] == "missing"


async def test_a_seal_copied_from_another_order_does_not_fit(
    funded_service: TradingService, base_chain: FakeChain, ledger: Ledger
) -> None:
    """A seal is bound to its row's id: the attacker cannot reuse a benign order's seal."""
    service = funded_service
    first = await _park_send(service)
    second = await _park_send(service)
    _outside(
        ledger,
        "UPDATE orders SET recipient = ? WHERE order_id = ?",
        (ATTACKER, second),
    )
    _outside(
        ledger,
        "UPDATE seals SET seal = (SELECT seal FROM seals WHERE kind = 'order' AND id = ?) "
        "WHERE kind = 'order' AND id = ?",
        (first, second),
    )

    result = await service.approve(second)

    assert base_chain.sent == [] and result["status"] == "failed"


async def test_rewrite_while_a_manual_swap_is_being_quoted_is_refused(
    funded_service: TradingService, base_chain: FakeChain, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row is re-read at execution; a write during the quote round-trip is caught there."""
    service = funded_service
    original = service._priced_routed

    async def quote_then_tamper(*args: Any, **kwargs: Any) -> Any:
        quote = await original(*args, **kwargs)
        service.ledger._conn.execute("UPDATE orders SET amount_raw = '999000000' WHERE 1 = 1")
        return quote

    monkeypatch.setattr(service, "_priced_routed", quote_then_tamper)

    orders = await service.swap(
        chain=BASE,
        wallets=None,
        token_in="USDC",
        token_out="WETH",
        amount_in="10",
        amount_pct=None,
        slippage_pct=None,
        initiator="manual",
        session_key=None,
        note=None,
    )

    assert base_chain.sent == []
    assert orders[0]["status"] == "failed"
    assert str(orders[0]["reason"]).startswith("trading.integrity:")


async def test_a_seal_is_checked_again_right_before_the_signature(
    funded_service: TradingService, base_chain: FakeChain, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_send`` re-reads the ledger: an edit after the row was loaded still stops the key."""
    service = funded_service
    order_id = await _park_send(service)
    evm = service.evm(BASE)
    original = evm.nonce

    async def nonce_then_tamper(address: str) -> int:
        service.ledger._conn.execute(
            "UPDATE orders SET recipient = ? WHERE order_id = ?", (ATTACKER, order_id)
        )
        return await original(address)

    monkeypatch.setattr(evm, "nonce", nonce_then_tamper)

    result = await service.approve(order_id)

    assert base_chain.sent == [] and result["status"] == "failed"


# ── mandates and triggers ──────────────────────────────────────────────────


async def test_a_proposal_flipped_to_active_in_the_file_never_buys(
    world: World, ledger: Ledger
) -> None:
    """An agent cannot approve its own mandate by writing ``status = 'active'``."""
    service = world.service
    pending = (
        await service.dca_create(
            chain=BASE, token="WETH", usd_per_run=10, cap_usd=50, every_seconds=HOUR,
            name="DCA ETH", initiator="agent",
        )
    )["mandate"]  # fmt: skip
    _outside(
        ledger,
        "UPDATE mandates SET status = 'active', approved_at = ?, anchor_at = ?, "
        "next_run_at = ?, expires_at = NULL WHERE mandate_id = ?",
        (T0, T0, T0, pending["id"]),
    )

    await world.due()

    assert world.chain.sent == []
    row = world.row(pending["id"])
    assert row["status"] == "stopped"
    assert row["status_reason"].startswith("integrity check failed (missing)")
    assert _alerts(service)[-1] == {
        "schema_version": 1,
        "kind": "mandate",
        "id": pending["id"],
        "verdict": "missing",
        "wallet": world.wallet.lower(),
        "message": _alerts(service)[-1]["message"],
    }


async def test_an_approved_mandate_rewritten_in_the_file_is_stopped(
    world: World, ledger: Ledger
) -> None:
    service = world.service
    created = (await world.create(usd_per_run=10, cap_usd=50, every_seconds=HOUR))["mandate"]
    # Pointed at another token and a bigger run, behind the engine's back.
    _outside(
        ledger,
        "UPDATE mandates SET usd_per_run = 90, cap_usd = 900, token_out = ? WHERE mandate_id = ?",
        (USDC, created["id"]),
    )
    _outside(ledger, "UPDATE mandates SET token_in = ? WHERE mandate_id = ?", (WETH, created["id"]))

    await world.due()

    assert world.chain.sent == []
    assert world.row(created["id"])["status"] == "stopped"
    assert _alerts(service)[-1]["verdict"] == "mismatch"


async def test_an_operator_update_is_resealed_and_an_old_state_cannot_be_replayed(
    world: World, ledger: Ledger
) -> None:
    """The operator lowers a cap; restoring the old terms and their old seal is refused."""
    service = world.service
    created = (await world.create(usd_per_run=10, cap_usd=300, every_seconds=HOUR))["mandate"]
    old_seal = ledger.sealed_row("mandate", created["id"])[1]
    await service.dca_update(created["id"], cap_usd=20)
    assert ledger.sealed_row("mandate", created["id"])[1] != old_seal

    _outside(ledger, "UPDATE mandates SET cap_usd = 300 WHERE mandate_id = ?", (created["id"],))
    _outside(
        ledger,
        "UPDATE seals SET seal = ? WHERE kind = 'mandate' AND id = ?",
        (old_seal, created["id"]),
    )
    await world.due()

    assert world.chain.sent == []
    assert world.row(created["id"])["status"] == "stopped"


async def test_an_untouched_mandate_buys_and_an_update_keeps_it_buying(world: World) -> None:
    """Control: create, update, run, all through the engine."""
    created = (await world.create(usd_per_run=10, cap_usd=300, every_seconds=HOUR))["mandate"]
    await world.service.dca_update(created["id"], usd_per_run=12)
    world.set_price(2000.0, usd=12.0)
    await world.due()
    runs = world.runs(created["id"])
    assert [r["status"] for r in runs] == ["filled"]
    assert len(world.chain.sent) >= 1


async def test_a_trigger_rewritten_in_the_file_does_not_fire(world: World, ledger: Ledger) -> None:
    service = world.service
    world.chain.set_erc20(WETH, world.wallet, 10**18)
    created = (
        await service.trigger_create(
            chain=BASE, kind="sell", token="WETH", direction="below", price="1900",
            amount="0.02", initiator="agent",
        )
    )["trigger"]  # fmt: skip
    await service.trigger_approve(created["id"])
    # The whole WETH balance instead of 0.02, and no stop price to wait for.
    _outside(
        ledger,
        "UPDATE triggers SET amount_raw = ?, price_usd = 1000000 WHERE trigger_id = ?",
        (str(10**18), created["id"]),
    )

    for _ in range(3):
        world.at(world.clock.now + 60)
        await service.trigger_check()
        await _settle(service)

    assert world.chain.sent == []
    row = service.ledger.get_trigger(created["id"])
    assert row is not None and row["status"] == "stopped"
    assert _alerts(service)[-1]["kind"] == "trigger"


async def test_a_trigger_flipped_to_armed_in_the_file_does_not_fire(
    world: World, ledger: Ledger
) -> None:
    service = world.service
    world.chain.set_erc20(WETH, world.wallet, 10**18)
    created = (
        await service.trigger_create(
            chain=BASE, kind="sell", token="WETH", direction="below", price="2500",
            amount="0.02", initiator="agent",
        )
    )["trigger"]  # fmt: skip
    _outside(
        ledger,
        "UPDATE triggers SET status = 'armed', approved_at = ?, expires_at = NULL "
        "WHERE trigger_id = ?",
        (T0, created["id"]),
    )
    for _ in range(3):
        world.at(world.clock.now + 60)
        await service.trigger_check()
        await _settle(service)

    assert world.chain.sent == []
    row = service.ledger.get_trigger(created["id"])
    assert row is not None and row["status"] == "stopped"


# ── the key: restarts, password changes, the upgrade ───────────────────────


def _restarted(service: TradingService, vault_root: Path) -> TradingService:
    """The same vault directory and ledger file in a new process."""
    fresh = TradingService(
        service._gateway_config,
        vault=Vault(vault_root, **FAST_KDF),
        ledger=Ledger(service.ledger.path),
        prices=service.prices,
        http=service._http,
        broadcast=service._broadcast,
        sign_tx=fake_sign_tx,
        background=False,
    )
    fresh._now = service._now
    return fresh


async def test_seals_survive_a_restart_and_a_password_change(
    world: World, vault_root: Path, http: httpx.AsyncClient
) -> None:
    created = (await world.create(usd_per_run=10, cap_usd=300, every_seconds=HOUR))["mandate"]
    world.service.vault.change_password(PASSWORD, "another long password")
    fresh = _restarted(world.service, vault_root)
    try:
        await fresh.dca_run_due()
        await _settle(fresh)
        runs = fresh.ledger.list_runs(created["id"])
        assert [r["status"] for r in runs] == ["filled"]
        assert fresh.ledger.get_mandate(created["id"])["status"] == "active"  # type: ignore[index]
    finally:
        await fresh.stop()
        fresh.ledger.close()


async def test_the_first_unlock_after_the_upgrade_adopts_live_rows_once(
    world: World, vault_root: Path, ledger: Ledger
) -> None:
    """A vault and a ledger from before seals: what was live keeps working, nothing else."""
    created = (await world.create(usd_per_run=10, cap_usd=300, every_seconds=HOUR))["mandate"]
    proposal = (
        await world.service.dca_create(
            chain=BASE, token="WETH", usd_per_run=10, cap_usd=50, every_seconds=HOUR,
            initiator="agent",
        )
    )["mandate"]  # fmt: skip
    # Pre-upgrade state: no seal salt in the vault, no seals in the ledger.
    index_path = vault_root / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index.pop(LEDGER_SEAL)
    index_path.write_text(json.dumps(index), encoding="utf-8")
    _outside(ledger, "DELETE FROM seals")
    fresh = _restarted(world.service, vault_root)
    try:
        assert fresh.ensure_unlocked() is True
        assert fresh.ledger.sealed_row("mandate", created["id"])[1] is not None
        # A proposal is not adopted: approving it is still the operator's.
        assert fresh.ledger.sealed_row("mandate", proposal["id"])[1] is None
        assert fresh.vault.take_new_seal() is False
        # Flipping it to active after the adoption buys nothing.
        _outside(
            ledger,
            "UPDATE mandates SET status = 'active', anchor_at = ?, next_run_at = ? "
            "WHERE mandate_id = ?",
            (T0, T0, proposal["id"]),
        )
        await fresh.dca_run_due()
        await _settle(fresh)
        assert [r["status"] for r in fresh.ledger.list_runs(created["id"])] == ["filled"]
        assert fresh.ledger.list_runs(proposal["id"]) == []
        assert fresh.ledger.get_mandate(proposal["id"])["status"] == "stopped"  # type: ignore[index]
    finally:
        await fresh.stop()
        fresh.ledger.close()


async def test_approving_needs_the_vault_key(world: World, vault_root: Path) -> None:
    """A manual-mode vault that is locked cannot seal, so it cannot approve."""
    service = world.service
    pending = (
        await service.dca_create(
            chain=BASE, token="WETH", usd_per_run=10, cap_usd=50, every_seconds=HOUR,
            initiator="agent",
        )
    )["mandate"]  # fmt: skip
    service.vault.set_unlock_mode("manual", PASSWORD)
    service.vault = Vault(vault_root, **FAST_KDF)  # after a restart: never unlocked
    with pytest.raises(TradingError) as err:
        await service.dca_approve(pending["id"])
    assert err.value.code == "wallet.locked"
    assert world.row(pending["id"])["status"] == "awaiting_approval"
    service.vault.unlock(PASSWORD)
    approved = (await service.dca_approve(pending["id"]))["mandate"]
    assert approved["status"] == "active"


async def test_edited_token_decimals_do_not_resize_or_cheapen_an_order(
    funded_service: TradingService, ledger: Ledger
) -> None:
    """Variant: ``tokens.decimals`` turns the agent's "0.25" into 250 USDC valued at $0.25."""
    service = funded_service
    await service.resolve_token(BASE, "USDC")  # cached by the engine, and sealed
    _outside(ledger, "UPDATE tokens SET decimals = 9 WHERE address = ?", (USDC,))

    orders = await service.swap(
        chain=BASE,
        wallets=None,
        token_in="USDC",
        token_out="WETH",
        amount_in="0.25",
        amount_pct=None,
        slippage_pct=None,
        initiator="agent",
        session_key=None,
        note=None,
    )

    assert orders[0]["amountInRaw"] == "250000"  # 0.25 USDC at its real 6 decimals
    healed = service.ledger.get_token(BASE.chain_id, USDC)
    assert healed is not None and healed["decimals"] == 6


# ── accounting and the file itself ─────────────────────────────────────────


async def test_erasing_daily_spend_does_not_reopen_the_cap(
    funded_service: TradingService, ledger: Ledger
) -> None:
    service = funded_service
    wallet = str(service.test_wallet)  # type: ignore[attr-defined]
    service.ledger.add_daily_spend(wallet.lower(), 995.0)
    _outside(ledger, "DELETE FROM daily_spend")

    assert service.limits(wallet)["spentTodayUsd"] == pytest.approx(995.0)
    orders = await service.swap(
        chain=BASE,
        wallets=wallet,
        token_in="USDC",
        token_out="WETH",
        amount_in="10",
        amount_pct=None,
        slippage_pct=None,
        initiator="agent",
        session_key=None,
        note=None,
    )
    assert orders[0]["status"] == "rejected"


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_the_ledger_file_is_owner_only(ledger: Ledger) -> None:
    for path in (ledger.path, ledger.path.with_name(ledger.path.name + "-wal")):
        if path.exists():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600


# ── the seal itself ────────────────────────────────────────────────────────


def test_a_seal_binds_the_terms_the_id_and_the_wallet_incarnation() -> None:
    incarnations = {"0xabc": "1.0"}
    sealer = integrity.Sealer(lambda: b"k" * 32, lambda wallet: incarnations.get(wallet))
    row = {"order_id": "ord_1", "wallet": "0xabc", "recipient": BENIGN, "amount_raw": "1"}
    seal = sealer.seal("order", row)
    assert seal is not None and sealer.check("order", row, seal) == "ok"
    for change in ({"recipient": ATTACKER}, {"amount_raw": "2"}, {"order_id": "ord_2"}):
        assert sealer.check("order", {**row, **change}, seal) == "mismatch"
    # The same key imported again is another incarnation of the wallet.
    incarnations["0xabc"] = "2.0"
    assert sealer.check("order", row, seal) == "mismatch"
    # Status and counters are not terms: the engine moves them without re-sealing.
    incarnations["0xabc"] = "1.0"
    assert sealer.check("order", {**row, "status": "approved", "reason": "x"}, seal) == "ok"
    assert integrity.Sealer(lambda: None, lambda w: None).check("order", row, seal) is None


def test_every_column_that_reaches_a_signature_is_a_term() -> None:
    """A new money column must be added to the seal, or it is not protected."""
    for column in ("wallet", "chain_id", "token_in", "token_out", "amount_raw", "recipient"):
        assert column in integrity.ORDER_TERMS
    for column in ("min_out_raw", "expected_out_raw", "slippage_pct", "quote_json", "batch_id"):
        assert column in integrity.ORDER_TERMS
    assert "expires_at" in integrity.ORDER_TERMS
    for column in ("wallet", "token_in", "token_out", "usd_per_run", "cap_usd", "slippage_pct"):
        assert column in integrity.MANDATE_TERMS
    for column in ("wallet", "token", "quote", "amount_raw", "amount_pct", "amount_usd"):
        assert column in integrity.TRIGGER_TERMS
    for column in ("price_usd", "trail_pct", "direction", "valid_until", "kind", "group_id"):
        assert column in integrity.TRIGGER_TERMS
