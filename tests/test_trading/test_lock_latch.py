"""An operator's lock stops every signature, unattended ones included (WAL-22).

In ``auto`` mode the vault re-read ``unlock.key`` on the next status call or
agent swap, so ``agentos wallet lock`` stopped nothing: approved DCA mandates
and armed triggers kept buying and selling through the background loop. The
lock is now latched until the operator unlocks with the password, and while it
holds the loops wait instead of failing their way into an auto-pause.
"""

from __future__ import annotations

import pytest

from agentos.trading.chains import BASE
from agentos.trading.service import TradingError
from tests.test_trading import test_dca as dca_suite
from tests.test_trading.conftest import PASSWORD
from tests.test_trading.test_dca import (
    HOUR,
    World,
    _settle,
)

# The DCA suite's world (a funded wallet on a pinned clock) and its stable ids.
world = dca_suite.world
ids = dca_suite.ids


async def test_a_locked_vault_holds_due_dca_buys_until_the_unlock(world: World) -> None:
    service = world.service
    created = (await world.create(usd_per_run=10, cap_usd=300, every_seconds=HOUR))["mandate"]
    service.vault.lock()

    for step in range(4):
        world.at(world.clock.now + HOUR * step)
        await service.housekeep()
        await _settle(service)

    assert world.chain.sent == []
    assert service.vault.unlocked is False
    assert world.runs(created["id"]) == []  # not "failed": nothing was tried
    assert world.row(created["id"])["status"] == "active"  # and so not auto-paused
    with pytest.raises(TradingError) as err:
        await service.dca_run_now(created["id"])
    assert err.value.code == "wallet.locked"

    service.vault.unlock(PASSWORD)
    await service.housekeep()
    await _settle(service)
    assert [r["status"] for r in world.runs(created["id"])] == ["filled"]


async def test_a_locked_vault_holds_armed_triggers_but_alerts_still_ring(world: World) -> None:
    service = world.service
    common = {"chain": BASE, "token": "WETH", "direction": "below", "price": "2500"}
    # A buy-the-dip order (the world's swap fakes model USDC -> WETH).
    stop = (await service.trigger_create(kind="buy", amount_usd=10, initiator="manual", **common))[
        "trigger"
    ]
    alert = (await service.trigger_create(kind="alert", initiator="manual", **common))["trigger"]
    service.vault.lock()

    for _ in range(3):
        world.at(world.clock.now + 60)
        await service.housekeep()
        await _settle(service)

    assert world.chain.sent == []
    assert service.ledger.get_trigger(stop["id"])["status"] == "armed"  # type: ignore[index]
    assert service.ledger.get_trigger(alert["id"])["status"] == "done"  # type: ignore[index]
    with pytest.raises(TradingError) as err:
        await service.trigger_fire_now(stop["id"])
    assert err.value.code == "wallet.locked"

    service.vault.unlock(PASSWORD)
    for _ in range(2):
        world.at(world.clock.now + 60)
        await service.housekeep()
        await _settle(service)
    assert len(world.chain.sent) >= 1
    assert service.ledger.get_trigger(stop["id"])["status"] == "done"  # type: ignore[index]


async def test_approving_while_locked_keeps_the_order_parked(world: World) -> None:
    service = world.service
    orders = await service.swap(
        chain=BASE,
        wallets=None,
        token_in="USDC",
        token_out="WETH",
        amount_in="250",
        amount_pct=None,
        slippage_pct=None,
        initiator="agent",
        session_key=None,
        note=None,
    )
    order_id = orders[0]["orderId"]
    assert orders[0]["status"] == "awaiting_approval"
    service.vault.lock()

    with pytest.raises(TradingError) as err:
        await service.approve(order_id)
    assert err.value.code == "wallet.locked"
    assert service.get_order(order_id)["status"] == "awaiting_approval"
    assert world.chain.sent == []

    service.vault.unlock(PASSWORD)
    world.set_price(2000.0, usd=250.0)
    approved = await service.approve(order_id, wait=True)
    assert approved["status"] == "confirmed"
