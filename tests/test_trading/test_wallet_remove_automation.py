"""Removing a wallet ends its automation; importing the key again revives nothing (WAL-26).

The audit's reproduction: a mandate (10 USD a run, 30 USD cap) and a stop-loss
are approved, two buys go through, the operator removes the wallet and later
imports the same key. The mandate was still ``active`` and its spent budget
read 0 (its orders were deleted with the wallet), so it spent another full cap
(50 USD against an approved 30), and the old stop-loss fired on its old terms.
"""

from __future__ import annotations

from typing import Any

from agentos.trading.chains import BASE
from agentos.trading.ledger import WALLET_REMOVED
from tests.test_trading import test_dca as dca_suite
from tests.test_trading.conftest import PASSWORD
from tests.test_trading.fakes import USDC, WETH
from tests.test_trading.test_dca import HOUR, KEY, T0, World, _settle

# The DCA suite's world (a funded wallet on a pinned clock) and its stable ids.
world = dca_suite.world
ids = dca_suite.ids


async def _automation(world: World) -> tuple[str, str, str]:
    """An approved DCA mandate, an approved stop-loss and an approved bracket."""
    service = world.service
    world.chain.set_erc20(WETH, world.wallet, 10**18)
    mandate = (
        await service.dca_create(
            chain=BASE, token="WETH", usd_per_run=10, cap_usd=30, every_seconds=HOUR,
            name="DCA ETH", initiator="agent",
        )
    )["mandate"]  # fmt: skip
    await service.dca_approve(mandate["id"])
    stop = (
        await service.trigger_create(
            chain=BASE, kind="buy", token="WETH", direction="below", price="1900",
            amount_usd=10, name="Buy the dip", initiator="agent",
        )
    )["trigger"]  # fmt: skip
    await service.trigger_approve(stop["id"])
    bracket = (
        await service.bracket_create(
            chain=BASE, token="WETH", take_profit="2600", stop_loss="1500", initiator="agent",
        )
    )["bracket"]  # fmt: skip
    await service.bracket_approve(bracket["id"])
    return mandate["id"], stop["id"], bracket["id"]


async def _ticks(world: World, n: int, step: float) -> None:
    for _ in range(n):
        world.at(world.clock.now + step)
        await world.service.housekeep()
        await _settle(world.service)


def _status(world: World, kind: str, ident: str) -> dict[str, Any]:
    getter = (
        world.service.ledger.get_mandate if kind == "mandate" else world.service.ledger.get_trigger
    )
    row = getter(ident)
    assert row is not None
    return row


async def test_remove_then_reimport_never_revives_the_automation(world: World) -> None:
    service = world.service
    mandate_id, stop_id, bracket_id = await _automation(world)
    await _ticks(world, 1, 0)
    await _ticks(world, 1, HOUR)
    assert service.ledger.mandate_spend(mandate_id)[0] == 20.0
    spent_on_chain = 1_000 - world.chain.get_erc20(USDC, world.wallet) / 10**6
    assert spent_on_chain == 20.0

    await service.remove_wallet(world.wallet, PASSWORD)

    mandate = _status(world, "mandate", mandate_id)
    assert mandate["status"] == "stopped" and mandate["status_reason"] == WALLET_REMOVED
    assert mandate["next_run_at"] is None
    assert _status(world, "trigger", stop_id)["status"] == "stopped"
    legs = service.ledger.group_triggers(bracket_id)
    assert {leg["status"] for leg in legs} == {"stopped"}
    assert {leg["status_reason"] for leg in legs} == {WALLET_REMOVED}
    # The cap does not reopen because the orders that spent it are gone.
    card = (await service.dca_get(mandate_id))["mandate"]
    assert card["budget"]["spentUsd"] == 20.0

    again = await service.import_wallet("Back again", private_key=KEY)
    assert again["address"] == world.wallet
    sent = len(world.chain.sent)
    await _ticks(world, 4, HOUR)
    world.set_price(1400.0)  # under the stop and the stop-loss leg
    await _ticks(world, 3, 60)

    assert len(world.chain.sent) == sent
    assert _status(world, "mandate", mandate_id)["status"] == "stopped"
    assert _status(world, "trigger", stop_id)["status"] == "stopped"
    assert 1_000 - world.chain.get_erc20(USDC, world.wallet) / 10**6 == spent_on_chain


async def test_a_mandate_resurrected_in_the_file_after_reimport_still_does_not_buy(
    world: World,
) -> None:
    """Belt and braces: even flipped back to ``active`` with its old seal, the key is new."""
    service = world.service
    mandate_id, stop_id, _ = await _automation(world)
    old_seal = service.ledger.sealed_row("mandate", mandate_id)[1]
    await service.remove_wallet(world.wallet, PASSWORD)
    assert service.ledger.sealed_row("mandate", mandate_id)[1] is None
    await service.import_wallet("Back again", private_key=KEY)

    raw = service.ledger._conn  # an out-of-band write: no engine, no seal
    raw.execute(
        "UPDATE mandates SET status = 'active', status_reason = NULL, next_run_at = ? "
        "WHERE mandate_id = ?",
        (T0, mandate_id),
    )
    raw.execute(
        "INSERT INTO seals (kind, id, seal) VALUES ('mandate', ?, ?)", (mandate_id, old_seal)
    )
    raw.execute("UPDATE triggers SET status = 'armed' WHERE trigger_id = ?", (stop_id,))
    sent = len(world.chain.sent)
    world.set_price(1400.0)
    await _ticks(world, 3, HOUR)

    assert len(world.chain.sent) == sent
    assert _status(world, "mandate", mandate_id)["status"] == "stopped"
    assert _status(world, "trigger", stop_id)["status"] == "stopped"


async def test_removal_rejects_what_the_automation_parked(world: World) -> None:
    """A DCA buy waiting for approval goes with the mandate; nothing is left to approve."""
    service = world.service
    service.config.approval_threshold_usd = 5.0  # every 10 USD buy parks
    mandate_id, _, _ = await _automation(world)
    await _ticks(world, 1, 0)
    parked = [o for o in service.ledger.orders_for_mandate(mandate_id)]
    assert [o["status"] for o in parked] == ["awaiting_approval"]
    run = service.ledger.list_runs(mandate_id)[0]
    assert run["status"] == "parked"

    await service.remove_wallet(world.wallet, PASSWORD)

    assert service.ledger.orders_for_mandate(mandate_id) == []  # forgotten with the wallet
    run = service.ledger.list_runs(mandate_id)[0]
    assert run["status"] not in ("pending", "parked")
    assert service.ledger.open_runs(mandate_id) == []


async def test_the_ledger_alone_ends_automation_of_a_removed_wallet(world: World) -> None:
    """``Ledger.remove_wallet`` is the floor under the engine's own stop."""
    service = world.service
    mandate_id, stop_id, _ = await _automation(world)
    service.ledger.remove_wallet(world.wallet)
    assert _status(world, "mandate", mandate_id)["status"] == "stopped"
    assert _status(world, "trigger", stop_id)["status_reason"] == WALLET_REMOVED
    assert service.ledger.sealed_row("trigger", stop_id)[1] is None


async def test_bulk_removal_ends_the_automation_too(world: World) -> None:
    """``remove_wallets`` (``wallet.removeBatch``) stops what each wallet ran, as one removal."""
    service = world.service
    mandate_id, stop_id, bracket_id = await _automation(world)
    other = (await service.create_wallet("Spare"))["address"]

    result = await service.remove_wallets([world.wallet, other], PASSWORD)

    assert sorted(result["removed"]) == sorted([world.wallet, other])
    assert service.vault.list() == []
    mandate = _status(world, "mandate", mandate_id)
    assert mandate["status"] == "stopped" and mandate["status_reason"] == WALLET_REMOVED
    assert _status(world, "trigger", stop_id)["status"] == "stopped"
    assert {leg["status"] for leg in service.ledger.group_triggers(bracket_id)} == {"stopped"}
