"""DCA mandates (``docs/dca.md``): planning helpers, the runner, the ledger and the card.

Offline and on a fake clock: ``service._now`` is pinned and moved by hand, the
chain, the swap provider and the price feed are the shared fakes. The card
fixtures under ``tests/fixtures/dca_cards/`` are the frontend's renderer
fixtures; they are built by :func:`_card_world` and pinned against it here.
Regenerate them with::

    AGENTOS_REGEN_DCA_FIXTURES=1 uv run pytest tests/test_trading/test_dca.py -k fixture
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from agentos.trading import dca
from agentos.trading import service as service_module
from agentos.trading.chains import BASE, ROBINHOOD
from agentos.trading.ledger import SCHEMA_VERSION, Ledger
from agentos.trading.service import TradingError, TradingService
from tests.test_trading.conftest import PASSWORD
from tests.test_trading.fakes import USDC, WETH, FakeAggregator, FakeChain, FakePrices
from tests.test_trading.test_service import _wire_swap_effects

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "dca_cards"
FIXTURE_NAMES = (
    "mandate-active",
    "mandate-awaiting",
    "mandate-completed",
    "mandates",
    "mandates-empty",
)
T0 = 1_789_894_800.0  # 2026-09-20T09:00:00Z
DAY = 86_400
HOUR = 3_600
# A fixed key so the wallet (and so every payload) is the same on every run.
KEY = "0x" + "4c" * 32

#: The AgentOS DEX's "no route right now": a 200 with every money field nulled.
NO_ROUTE = (200, {"liquidityAvailable": False, "explanation": "No quotes available"})


class Clock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _events(service: TradingService, name: str) -> list[dict[str, Any]]:
    return [p for e, p in service.events if e == name]  # type: ignore[attr-defined]


async def _settle(service: TradingService) -> None:
    """Let every background confirm task finish (``wait=False`` buys)."""
    for _ in range(50):
        tasks = list(service._confirm_tasks)
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)


class World:
    """A service on a pinned clock with one funded wallet on Base."""

    def __init__(
        self,
        service: TradingService,
        chain: FakeChain,
        aggregator: FakeAggregator,
        prices: FakePrices,
        clock: Clock,
        wallet: str,
    ) -> None:
        self.service = service
        self.chain = chain
        self.aggregator = aggregator
        self.prices = prices
        self.clock = clock
        self.wallet = wallet
        self.set_price(2000.0)

    def set_price(self, price: float | None, *, usd: float = 10.0) -> None:
        """WETH's spot price, and a quote/receipt that buys ``usd`` worth at it."""
        if price is None:
            self.prices.spot.pop(("base", WETH), None)
            return
        self.prices.spot[("base", WETH)] = price
        out = int(usd / price * 10**18)
        self.aggregator.amount_out = out
        _wire_swap_effects(self.chain, self.wallet, out_amount=out)

    def at(self, ts: float) -> None:
        self.clock.now = ts

    async def create(self, **overrides: Any) -> dict[str, Any]:
        params: dict[str, Any] = {
            "chain": BASE,
            "token": "WETH",
            "usd_per_run": 10,
            "cap_usd": 300,
            "every_seconds": DAY,
            "name": "DCA ETH",
            "initiator": "manual",
        }
        params.update(overrides)
        return await self.service.dca_create(**params)

    async def due(self) -> None:
        await self.service.dca_run_due()
        await _settle(self.service)

    def runs(self, mandate_id: str) -> list[dict[str, Any]]:
        return self.service.ledger.list_runs(mandate_id)

    def row(self, mandate_id: str) -> dict[str, Any]:
        row = self.service.ledger.get_mandate(mandate_id)
        assert row is not None
        return row


@pytest.fixture
def ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stable mandate and order ids, so payloads can be pinned."""
    mandates = iter(["dca_1a2b3c4d", "dca_5e6f7a8b", "dca_9c0d1e2f", "dca_3a4b5c6d"])
    orders = itertools.count(1)
    monkeypatch.setattr(dca, "new_mandate_id", lambda: next(mandates))
    monkeypatch.setattr(service_module, "new_order_id", lambda: f"ord_{next(orders):012x}")


@pytest.fixture
async def world(
    service: TradingService,
    base_chain: FakeChain,
    fake_aggregator: FakeAggregator,
    fake_prices: FakePrices,
    monkeypatch: pytest.MonkeyPatch,
    ids: None,
) -> World:
    monkeypatch.setattr(service_module, "CHAIN_CATCHUP_POLL_S", 0.0)
    clock = Clock()
    service._now = clock
    service.vault.setup(PASSWORD, "auto")
    wallet = (await service.import_wallet("Key main", private_key=KEY))["address"]
    base_chain.set_native(wallet, 10**18)
    base_chain.set_erc20(USDC, wallet, 1_000 * 10**6)
    return World(service, base_chain, fake_aggregator, fake_prices, clock, wallet)


# ── pure planning ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("every", "label"),
    [
        (86_400, "every day"),
        (21_600, "every 6 hours"),
        (1_800, "every 30 minutes"),
        (604_800, "every week"),
        (90, "every 90 seconds"),
        (3_600, "every hour"),
        (172_800, "every 2 days"),
        (60, "every minute"),
    ],
)
def test_schedule_label(every: int, label: str) -> None:
    assert dca.schedule_label(every) == label


def test_next_run_after_is_drift_free() -> None:
    assert dca.next_run_after(100.0, 60, 50.0) == 100.0
    assert dca.next_run_after(100.0, 60, 100.0) == 160.0
    assert dca.next_run_after(100.0, 60, 159.9) == 160.0
    # A late run does not push the schedule; a long gap is one slot, not a burst.
    assert dca.next_run_after(100.0, 60, 171.0) == 220.0
    assert dca.next_run_after(T0, DAY, T0 + 5.5 * DAY) == T0 + 6 * DAY
    with pytest.raises(ValueError):
        dca.next_run_after(0.0, 0, 1.0)


def test_run_size_takes_the_remainder_rounded_down() -> None:
    assert dca.run_size(10, 300, 0, 0) == 10
    assert dca.run_size(10, 25, 20, 0) == 5
    assert dca.run_size(10, 25, 15, 6.667) == 3.33
    assert dca.run_size(10, 25, 25, 0) == 0.0
    assert dca.run_size(10, 25, 30, 0) == 0.0


@pytest.mark.parametrize(
    ("usd", "dust"),
    [(10, 0.5), (1, 0.5), (0.9, 0.45), (0.3, 0.15), (0.05, 0.02), (0.04, 0.02), (0.01, 0.01)],
)
def test_dust_is_the_smaller_of_fifty_cents_and_half_a_buy(usd: float, dust: float) -> None:
    assert dca.dust_for(usd) == pytest.approx(dust)


def test_a_run_too_small_to_place_always_means_the_mandate_is_done() -> None:
    """``run_size < dust_for`` (the runner's cap skip) iff ``completion_reason`` says done."""
    cents = [0.01, 0.02, 0.03, 0.04, 0.05, 0.1, 0.3, 0.49, 0.5, 0.55, 1.0, 10.0]
    for usd in cents:
        for cap in cents:
            for spent in (0.0, 0.004, 0.01, 0.019, 0.02, 0.025, 0.04, 0.3, 0.51, 9.6):
                skip = dca.run_size(usd, cap, spent, 0.0) < dca.dust_for(usd)
                done = dca.completion_reason(
                    usd_per_run=usd,
                    cap_usd=cap,
                    spent_usd=spent,
                    reserved_usd=0.0,
                    runs_done=0,
                    runs_max=None,
                    in_flight=0,
                )
                assert skip == (done == "cap reached"), (usd, cap, spent)


def test_reasons_and_payload_pieces() -> None:
    assert dca.split_reason("max_price: WETH at $2,700 above $2,600") == (
        "max_price",
        "WETH at $2,700 above $2,600",
    )
    assert dca.split_reason("transaction reverted on-chain") == (
        None,
        "transaction reverted on-chain",
    )
    assert dca.pause_reason(3, "insufficient_balance", "USDC") == (
        "paused after 3 runs: insufficient USDC"
    )
    assert dca.run_note("DCA ETH", 3, 30) == "DCA ETH · buy 3/30"
    assert dca.run_note("DCA ETH", 3, None) == "DCA ETH · buy 3/∞"
    assert dca.iso(T0) == "2026-09-20T09:00:00Z" and dca.iso(None) is None
    assert dca.amount_json(0, 18, None) == {"raw": "0", "human": "0", "usd": 0.0}
    assert dca.amount_json(5 * 10**14, 18, None)["usd"] is None
    assert dca.amount_json(123_456_789, 6, 1.0) == {
        "raw": "123456789",
        "human": "123.456789",
        "usd": 123.456789,
    }
    # 20 billion tokens of an 18-decimal token: 29 digits of base units.
    assert dca.amount_json(2 * 10**28 + 1, 18, None)["human"] == ("20000000000.000000000000000001")


# ── create / approve ───────────────────────────────────────────────────────


async def test_operator_create_is_active_and_agent_create_parks(world: World) -> None:
    active = await world.create()
    mandate = active["mandate"]
    assert active["kind"] == "mandate" and active["version"] == 1
    assert active["request"] == {"kind": "get", "params": {"mandateId": mandate["id"]}}
    assert mandate["status"] == "active" and mandate["initiator"] == "manual"
    assert mandate["schedule"]["nextRunAt"] == dca.iso(T0)  # startNow: first buy on the tick
    assert mandate["approvedAt"] == dca.iso(T0) and mandate["expiresAt"] is None
    assert mandate["token"]["symbol"] == "WETH" and mandate["quote"]["symbol"] == "USDC"
    assert mandate["wallet"] == {"address": world.wallet, "label": "Key main", "inApp": True}

    pending = await world.create(initiator="agent", session_key="agent:desk:main")
    parked = pending["mandate"]
    assert parked["status"] == "awaiting_approval" and parked["sessionKey"] == "agent:desk:main"
    assert parked["schedule"]["nextRunAt"] is None and parked["schedule"]["anchorAt"] is None
    assert parked["expiresAt"] == dca.iso(T0 + dca.PENDING_TTL)
    # A proposal never buys, however long the tick waits.
    world.at(T0 + HOUR)
    await world.due()
    assert world.runs(parked["id"]) == []

    changed = _events(world.service, "trading.dca.changed")
    changed_ids = [e["mandate"]["id"] for e in changed]
    assert changed_ids[:2] == [mandate["id"], parked["id"]]
    assert set(changed_ids[2:]) == {mandate["id"]}  # the run: placed, then filled
    assert {"reason": "dca", "mandateId": parked["id"]}.items() <= next(
        e for e in _events(world.service, "trading.changed") if e.get("mandateId") == parked["id"]
    ).items()


async def test_start_next_waits_one_interval(world: World) -> None:
    created = await world.create(start_now=False, every_seconds=6 * HOUR)
    assert created["mandate"]["schedule"]["nextRunAt"] == dca.iso(T0 + 6 * HOUR)
    await world.due()
    assert world.runs(created["mandate"]["id"]) == []


async def test_approve_then_first_run_fires_on_tick(world: World) -> None:
    pending = (await world.create(initiator="agent"))["mandate"]
    world.at(T0 + 600)
    approved = (await world.service.dca_approve(pending["id"]))["mandate"]
    assert approved["status"] == "active" and approved["approvedAt"] == dca.iso(T0 + 600)
    assert approved["schedule"]["anchorAt"] == dca.iso(T0 + 600)
    assert approved["expiresAt"] is None
    await world.service.tick()
    await _settle(world.service)
    runs = world.runs(pending["id"])
    assert [r["status"] for r in runs] == ["filled"]
    order = world.service.get_order(runs[0]["order_id"])
    assert order["mandateId"] == pending["id"] and order["initiator"] == "agent"
    assert order["note"] == "DCA ETH · buy 1/∞" and order["status"] == "confirmed"
    card = (await world.service.dca_get(pending["id"]))["mandate"]
    assert card["runs"]["done"] == 1 and card["budget"]["spentUsd"] == pytest.approx(10.0)
    assert card["schedule"]["nextRunAt"] == dca.iso(T0 + 600 + DAY)
    with pytest.raises(TradingError) as err:
        await world.service.dca_approve(pending["id"])
    assert err.value.code == "trading.dca.bad_state"


async def test_pending_mandate_expires_after_a_day(world: World) -> None:
    pending = (await world.create(initiator="agent"))["mandate"]
    world.at(T0 + dca.PENDING_TTL + 1)
    await world.due()
    row = world.row(pending["id"])
    assert row["status"] == "expired" and row["status_reason"] == "no decision within 24 h"
    with pytest.raises(TradingError) as err:
        await world.service.dca_approve(pending["id"])
    assert err.value.code == "trading.dca.bad_state"


async def test_create_validation(world: World) -> None:
    cases: list[tuple[dict[str, Any], str]] = [
        ({"usd_per_run": 0}, "trading.dca.invalid"),
        ({"usd_per_run": 0.005}, "trading.dca.invalid"),
        ({"usd_per_run": 10, "cap_usd": 5}, "trading.dca.invalid"),
        ({"every_seconds": 59}, "trading.dca.invalid"),
        ({"cap_usd": None, "runs_max": None}, "trading.dca.invalid"),
        ({"runs_max": 0}, "trading.dca.invalid"),
        ({"max_price_usd": -1}, "trading.dca.invalid"),
        ({"token": "USDC"}, "trading.dca.invalid"),
        ({"slippage_pct": 50}, "trading.dca.invalid"),
        ({"token": "NOPE"}, "trading.token_not_found"),
        ({"chain": ROBINHOOD, "token": "AAPL"}, "trading.dca.invalid"),  # no canonical USDC
        ({"initiator": "robot"}, "trading.invalid"),
    ]
    for overrides, code in cases:
        with pytest.raises(TradingError) as err:
            await world.create(**overrides)
        assert err.value.code == code, (overrides, err.value)
    with pytest.raises(TradingError, match=r"usdPerRun must be at least \$0\.01"):
        await world.create(usd_per_run=0.009, cap_usd=1)
    # A run limit alone sets the cap to usdPerRun × runsMax.
    capped = (await world.create(cap_usd=None, runs_max=30))["mandate"]
    assert capped["budget"]["capUsd"] == 300 and capped["runs"]["max"] == 30
    world.service.config.enabled = False
    with pytest.raises(TradingError) as err:
        await world.create()
    assert err.value.code == "trading.disabled"


async def test_not_found(world: World) -> None:
    for call in (
        world.service.dca_get("dca_00000000"),
        world.service.dca_pause("dca_00000000"),
        world.service.dca_run_now("dca_00000000"),
    ):
        with pytest.raises(TradingError) as err:
            await call
        assert err.value.code == "trading.dca.not_found"


# ── the schedule ───────────────────────────────────────────────────────────


async def test_drift_free_schedule_and_one_catch_up_after_a_gap(world: World) -> None:
    mandate_id = (await world.create())["mandate"]["id"]
    await world.due()
    assert world.row(mandate_id)["next_run_at"] == T0 + DAY
    # Late by a few minutes: the next slot is still anchored on T0.
    world.at(T0 + DAY + 300)
    await world.due()
    assert world.row(mandate_id)["next_run_at"] == T0 + 2 * DAY
    # The gateway was down for three and a half days: one buy, then the next slot.
    world.at(T0 + 5.5 * DAY)
    await world.due()
    await world.due()
    runs = world.runs(mandate_id)
    assert [r["n"] for r in runs] == [3, 2, 1] and {r["status"] for r in runs} == {"filled"}
    assert world.row(mandate_id)["next_run_at"] == T0 + 6 * DAY
    # Nothing is due before then.
    world.at(T0 + 6 * DAY - 1)
    await world.due()
    assert len(world.runs(mandate_id)) == 3


async def test_buy_now_counts_but_does_not_move_the_schedule(world: World) -> None:
    mandate_id = (await world.create(start_now=False))["mandate"]["id"]
    world.at(T0 + HOUR)
    answer = await world.service.dca_run_now(mandate_id, wait=True)
    assert answer["run"]["manual"] is True and answer["run"]["status"] == "filled"
    assert answer["run"]["orderId"] and answer["run"]["txHash"]
    assert answer["run"]["explorerUrl"].startswith("https://basescan.org/tx/")
    assert answer["mandate"]["runs"]["done"] == 1
    assert world.row(mandate_id)["next_run_at"] == T0 + DAY
    run_events = _events(world.service, "trading.dca.run")
    assert run_events and run_events[-1]["mandateId"] == mandate_id
    assert run_events[-1]["run"]["status"] == "filled"


# ── sizing and completion ──────────────────────────────────────────────────


async def test_last_buy_is_sized_to_the_remainder_then_cap_reached(world: World) -> None:
    mandate_id = (await world.create(cap_usd=25, every_seconds=HOUR))["mandate"]["id"]
    for step in range(3):
        world.at(T0 + step * HOUR)
        if step == 2:
            world.set_price(2000.0, usd=5.0)  # the quote for the smaller last buy
        await world.due()
    runs = list(reversed(world.runs(mandate_id)))
    assert [r["usd"] for r in runs] == [pytest.approx(10), pytest.approx(10), pytest.approx(5)]
    card = (await world.service.dca_get(mandate_id))["mandate"]
    assert card["status"] == "completed" and card["statusReason"] == "cap reached"
    assert card["budget"]["spentUsd"] == pytest.approx(25.0)
    assert card["budget"]["remainingUsd"] == pytest.approx(0.0)
    assert card["budget"]["progress"] == pytest.approx(1.0)
    assert card["schedule"]["nextRunAt"] is None
    world.at(T0 + 3 * HOUR)
    await world.due()
    assert len(world.runs(mandate_id)) == 3


async def test_buys_under_fifty_cents_run_and_complete(world: World) -> None:
    world.set_price(2000.0, usd=0.04)
    mandate_id = (
        await world.create(usd_per_run=0.04, cap_usd=0.55, runs_max=2, every_seconds=HOUR)
    )["mandate"]["id"]
    for step in range(2):
        world.at(T0 + step * HOUR)
        await world.due()
    runs = world.runs(mandate_id)
    assert [r["status"] for r in runs] == ["filled", "filled"]
    assert [r["usd"] for r in runs] == [pytest.approx(0.04), pytest.approx(0.04)]
    row = world.row(mandate_id)
    assert row["status"] == "completed" and row["status_reason"] == "runs reached"
    # A cap under $0.50 buys once instead of completing with nothing bought.
    world.set_price(2000.0, usd=0.3)
    world.at(T0 + 3 * HOUR)
    small_id = (await world.create(usd_per_run=0.3, cap_usd=0.3, every_seconds=HOUR))["mandate"][
        "id"
    ]
    await world.due()
    assert [r["status"] for r in world.runs(small_id)] == ["filled"]
    small = world.row(small_id)
    assert small["status"] == "completed" and small["status_reason"] == "cap reached"
    assert small["spent_usd"] == pytest.approx(0.3)


@pytest.mark.parametrize(
    ("usd", "cap", "size"),
    [(0.04, 0.01, 0.01), (0.05, 0.019, 0.01), (10, 0.49, 0.49), (10, 0.004, 0.0)],
)
async def test_a_cap_reached_skip_completes_in_the_same_run(
    world: World, usd: float, cap: float, size: float
) -> None:
    mandate_id = (await world.create(usd_per_run=usd, cap_usd=max(usd, 1)))["mandate"]["id"]
    # Shrink the cap behind the service's back: the next run finds too little left.
    world.service.ledger.update_mandate(mandate_id, now=T0, cap_usd=cap)
    run = await world.service._dca_fire(mandate_id, manual=False)
    assert run is not None
    assert run["status"] == "skipped" and run["reason"].startswith("cap_reached: ")
    assert run["usd"] == pytest.approx(size)  # the attempt is sized even though it skipped
    row = world.row(mandate_id)
    assert row["status"] == "completed" and row["status_reason"] == "cap reached"
    assert row["next_run_at"] is None


async def test_raising_the_cap_keeps_a_small_mandate_active(world: World) -> None:
    world.set_price(2000.0, usd=0.04)
    mandate_id = (await world.create(usd_per_run=0.04, cap_usd=0.04, every_seconds=HOUR))[
        "mandate"
    ]["id"]
    raised = (await world.service.dca_update(mandate_id, cap_usd=0.08))["mandate"]
    assert raised["status"] == "active" and raised["budget"]["capUsd"] == pytest.approx(0.08)
    assert raised["budget"]["spentUsd"] == 0
    await world.due()
    row = world.row(mandate_id)
    assert row["status"] == "active" and row["spent_usd"] == pytest.approx(0.04)
    # A cap under what is spent completes it.
    lowered = (await world.service.dca_update(mandate_id, cap_usd=0.03))["mandate"]
    assert lowered["status"] == "completed" and lowered["statusReason"] == "cap reached"
    with pytest.raises(TradingError, match=r"at least \$0\.01"):
        await world.service.dca_update(
            (await world.create(usd_per_run=1, cap_usd=10))["mandate"]["id"], usd_per_run=0.001
        )


async def test_completion_at_runs_max(world: World) -> None:
    mandate_id = (await world.create(cap_usd=100, runs_max=2, every_seconds=HOUR))["mandate"]["id"]
    for step in range(2):
        world.at(T0 + step * HOUR)
        await world.due()
    row = world.row(mandate_id)
    assert row["status"] == "completed" and row["status_reason"] == "runs reached"
    assert row["runs_done"] == 2 and row["spent_usd"] == pytest.approx(20.0)
    notes = [o["note"] for o in world.service.ledger.orders_for_mandate(mandate_id)]
    assert notes == ["DCA ETH · buy 1/2", "DCA ETH · buy 2/2"]


# ── guards ─────────────────────────────────────────────────────────────────


async def test_max_price_skip_does_not_count_toward_the_bad_streak(world: World) -> None:
    mandate_id = (await world.create(max_price_usd=2500, every_seconds=HOUR))["mandate"]["id"]
    world.set_price(2700.0)
    for step in range(4):
        world.at(T0 + step * HOUR)
        await world.due()
    row = world.row(mandate_id)
    assert row["status"] == "active" and row["bad_streak"] == 0 and row["runs_skipped"] == 4
    runs = world.runs(mandate_id)
    assert {r["status"] for r in runs} == {"skipped"}
    assert runs[0]["reason"] == "max_price: WETH at $2,700 above $2,500"
    assert world.service.ledger.orders_for_mandate(mandate_id) == []  # no buy placed
    card = (await world.service.dca_get(mandate_id))["mandate"]
    assert card["history"][0]["reasonCode"] == "max_price"
    assert card["history"][0]["reason"] == "WETH at $2,700 above $2,500"
    assert card["history"][0]["priceUsd"] == 2700.0
    # The price comes back under the guard: the next run buys.
    world.set_price(2400.0)
    world.at(T0 + 4 * HOUR)
    await world.due()
    assert world.runs(mandate_id)[0]["status"] == "filled"


async def test_unknown_price_with_a_max_price_is_a_skip_and_a_warning(world: World) -> None:
    mandate_id = (await world.create(max_price_usd=2500))["mandate"]["id"]
    world.set_price(None)
    await world.due()
    run = world.runs(mandate_id)[0]
    assert run["status"] == "skipped" and run["reason"].startswith("max_price: price unknown")
    card = await world.service.dca_get(mandate_id)
    assert "price unknown: max-price guard cannot be checked" in card["warnings"]


async def test_three_insufficient_balance_skips_pause_the_mandate(world: World) -> None:
    world.chain.set_erc20(USDC, world.wallet, 5 * 10**6)
    mandate_id = (await world.create(every_seconds=HOUR))["mandate"]["id"]
    for step in range(2):
        world.at(T0 + step * HOUR)
        await world.due()
        assert world.row(mandate_id)["status"] == "active"
    world.at(T0 + 2 * HOUR)
    await world.due()
    row = world.row(mandate_id)
    assert row["status"] == "paused"
    assert row["status_reason"] == "paused after 3 runs: insufficient USDC"
    assert row["bad_streak"] == 3 and row["runs_skipped"] == 3 and row["runs_done"] == 0
    runs = world.runs(mandate_id)
    assert all(r["reason"].startswith("insufficient_balance: Key main holds 5 USDC") for r in runs)
    assert all(r["usd"] == pytest.approx(10) for r in runs)  # what each run tried to spend
    # Paused: the schedule does not fire.
    world.at(T0 + 3 * HOUR)
    await world.due()
    assert len(world.runs(mandate_id)) == 3
    # Resume resets the streak: one more bad run does not pause again.
    await world.service.dca_resume(mandate_id)
    assert world.row(mandate_id)["bad_streak"] == 0
    world.at(T0 + 4 * HOUR)
    await world.due()
    assert world.row(mandate_id)["status"] == "active"


async def test_daily_cap_rejection_is_a_skip(world: World) -> None:
    world.service.config.daily_cap_usd = 5.0
    mandate_id = (await world.create())["mandate"]["id"]
    await world.due()
    run = world.runs(mandate_id)[0]
    assert run["status"] == "skipped" and run["reason"].startswith("daily_cap: daily cap 5.00 USD")
    order = world.service.ledger.get_order(run["order_id"])
    assert order is not None and order["status"] == "rejected"
    row = world.row(mandate_id)
    assert row["bad_streak"] == 1 and row["runs_skipped"] == 1


async def test_failed_quote_is_a_failed_run(world: World) -> None:
    world.aggregator.quote_error = NO_ROUTE
    mandate_id = (await world.create())["mandate"]["id"]
    await world.due()
    run = world.runs(mandate_id)[0]
    assert run["status"] == "failed" and run["order_id"]
    assert world.row(mandate_id)["runs_failed"] == 1
    card = (await world.service.dca_get(mandate_id))["mandate"]
    assert card["history"][0]["reasonCode"].startswith("trading.")


# ── parked buys ────────────────────────────────────────────────────────────


async def test_buy_above_the_threshold_parks_and_settles_when_approved(world: World) -> None:
    created = await world.create(usd_per_run=150, cap_usd=600, every_seconds=HOUR)
    mandate_id = created["mandate"]["id"]
    assert created["mandate"]["guards"]["buysNeedApproval"] is True
    assert any("above the $100 approval threshold" in w for w in created["warnings"])
    world.set_price(2000.0, usd=150.0)
    await world.due()
    run = world.runs(mandate_id)[0]
    assert run["status"] == "parked" and run["reason"].startswith("needs_approval: order 150.00")
    card = (await world.service.dca_get(mandate_id))["mandate"]
    assert card["budget"]["reservedUsd"] == pytest.approx(150.0)
    assert card["budget"]["remainingUsd"] == pytest.approx(450.0)
    assert _events(world.service, "trading.approval.requested")
    # The user approves the order in the Book: the run is filled.
    await world.service.approve(run["order_id"], wait=True)
    run = world.runs(mandate_id)[0]
    assert run["status"] == "filled" and run["usd"] == pytest.approx(150.0)
    row = world.row(mandate_id)
    assert row["runs_done"] == 1 and row["spent_usd"] == pytest.approx(150.0)


async def test_parked_buy_that_expires_is_an_expired_run(world: World) -> None:
    mandate_id = (await world.create(usd_per_run=150, cap_usd=600))["mandate"]["id"]
    world.set_price(2000.0, usd=150.0)
    await world.due()
    world.at(T0 + 901)  # past the 900 s approval TTL
    await world.service.expire_orders()
    run = world.runs(mandate_id)[0]
    assert run["status"] == "expired"
    row = world.row(mandate_id)
    assert row["runs_done"] == 0 and row["bad_streak"] == 0 and row["status"] == "active"
    card = (await world.service.dca_get(mandate_id))["mandate"]
    assert card["budget"]["reservedUsd"] == 0.0


async def test_rejected_parked_buy_and_stop_rejects_what_is_waiting(world: World) -> None:
    mandate_id = (await world.create(usd_per_run=150, cap_usd=600, every_seconds=HOUR))["mandate"][
        "id"
    ]
    world.set_price(2000.0, usd=150.0)
    await world.due()
    first = world.runs(mandate_id)[0]
    await world.service.reject(first["order_id"], "not today")
    assert world.runs(mandate_id)[0]["status"] == "rejected"
    world.at(T0 + HOUR)
    await world.due()
    second = world.runs(mandate_id)[0]
    assert second["status"] == "parked"
    stopped = await world.service.dca_stop(mandate_id, reason="enough")
    assert stopped["mandate"]["status"] == "stopped"
    assert stopped["mandate"]["statusReason"] == "user: enough"
    order = world.service.ledger.get_order(second["order_id"])
    assert order is not None and order["status"] == "rejected"
    assert world.runs(mandate_id)[0]["status"] == "rejected"


# ── update / state machine ─────────────────────────────────────────────────


async def test_update_re_anchors_the_schedule_and_can_complete(world: World) -> None:
    mandate_id = (await world.create())["mandate"]["id"]
    await world.due()
    world.at(T0 + 2 * HOUR)
    updated = (await world.service.dca_update(mandate_id, every_seconds=6 * HOUR, usd_per_run=20))[
        "mandate"
    ]
    assert updated["schedule"]["everySeconds"] == 6 * HOUR
    assert updated["schedule"]["label"] == "every 6 hours"
    assert updated["schedule"]["anchorAt"] == dca.iso(T0 + 2 * HOUR)
    assert updated["schedule"]["nextRunAt"] == dca.iso(T0 + 8 * HOUR)
    assert updated["budget"]["usdPerRun"] == 20
    renamed = await world.service.dca_update(mandate_id, name="  Slow   ETH ", max_price_usd=0)
    assert renamed["mandate"]["name"] == "Slow ETH"
    assert renamed["mandate"]["guards"]["maxPriceUsd"] is None
    with pytest.raises(TradingError) as err:
        await world.service.dca_update(mandate_id, every_seconds=30)
    assert err.value.code == "trading.dca.invalid"
    with pytest.raises(TradingError) as err:
        await world.service.dca_update(mandate_id, token="PEPE")
    assert err.value.code == "trading.dca.invalid"
    with pytest.raises(TradingError) as err:
        await world.service.dca_update(mandate_id)
    assert err.value.code == "trading.dca.invalid"
    # A cap under what is already spent completes the mandate.
    done = (await world.service.dca_update(mandate_id, cap_usd=10))["mandate"]
    assert done["status"] == "completed" and done["statusReason"] == "cap reached"
    with pytest.raises(TradingError) as err:
        await world.service.dca_update(mandate_id, usd_per_run=5)
    assert err.value.code == "trading.dca.bad_state"


async def test_state_machine_and_bad_state_errors(world: World) -> None:
    service = world.service
    mandate_id = (await world.create())["mandate"]["id"]
    for call in (service.dca_approve, service.dca_reject, service.dca_resume):
        with pytest.raises(TradingError) as err:
            await call(mandate_id)
        assert err.value.code == "trading.dca.bad_state"
    paused = (await service.dca_pause(mandate_id))["mandate"]
    assert paused["status"] == "paused"
    with pytest.raises(TradingError) as err:
        await service.dca_pause(mandate_id)
    assert err.value.code == "trading.dca.bad_state"
    # Paused for three days: resuming does not make up the missed buys.
    world.at(T0 + 3.2 * DAY)
    await world.due()
    assert world.runs(mandate_id) == []
    resumed = (await service.dca_resume(mandate_id))["mandate"]
    assert resumed["status"] == "active" and resumed["statusReason"] is None
    assert resumed["schedule"]["nextRunAt"] == dca.iso(T0 + 4 * DAY)
    # Buy now works on a paused mandate too.
    await service.dca_pause(mandate_id)
    await service.dca_run_now(mandate_id, wait=True)
    assert world.runs(mandate_id)[0]["status"] == "filled"
    stopped = (await service.dca_stop(mandate_id))["mandate"]
    assert stopped["status"] == "stopped" and stopped["statusReason"] == "user"
    for call in (service.dca_pause, service.dca_resume, service.dca_stop, service.dca_run_now):
        with pytest.raises(TradingError) as err:
            await call(mandate_id)
        assert err.value.code == "trading.dca.bad_state"
    pending_id = (await world.create(initiator="agent"))["mandate"]["id"]
    rejected = (await service.dca_reject(pending_id, reason="too much"))["mandate"]
    assert rejected["status"] == "rejected" and rejected["statusReason"] == "user: too much"
    with pytest.raises(TradingError) as err:
        await service.dca_run_now(pending_id)
    assert err.value.code == "trading.dca.bad_state"


async def test_list_filters_live_and_wallet(world: World) -> None:
    first = (await world.create())["mandate"]["id"]
    second = (await world.create(initiator="agent", name="Proposal"))["mandate"]["id"]
    third = (await world.create(name="Old"))["mandate"]["id"]
    await world.service.dca_stop(third)
    live = await world.service.dca_list()
    assert [m["id"] for m in live["mandates"]] == [second, first]
    assert live["request"] == {"kind": "list", "params": {"all": False}}
    everything = await world.service.dca_list(all=True, wallet="Key main")
    assert [m["id"] for m in everything["mandates"]] == [second, first, third]
    assert everything["totals"]["count"] == 3 and everything["totals"]["active"] == 1
    assert everything["request"]["params"] == {"all": True, "wallet": "Key main"}


async def test_tick_does_not_overlap_a_running_pass(world: World) -> None:
    mandate_id = (await world.create())["mandate"]["id"]
    async with world.service._dca_lock:
        await world.service.dca_run_due()  # the pass in progress holds the lock: no-op
    assert world.runs(mandate_id) == []
    await world.due()
    assert len(world.runs(mandate_id)) == 1


async def test_reconcile_settles_a_run_whose_order_finished_unheard(world: World) -> None:
    mandate_id = (await world.create(usd_per_run=150, cap_usd=600))["mandate"]["id"]
    world.set_price(2000.0, usd=150.0)
    await world.due()
    run = world.runs(mandate_id)[0]
    # The order expires behind the engine's back (a restart ate the event).
    world.service.ledger.update_order(run["order_id"], status="expired", reason="expired")
    world.at(T0 + HOUR)
    await world.due()
    assert world.service.ledger.get_run(run["run_id"])["status"] == "expired"  # type: ignore[index]


# ── ledger ─────────────────────────────────────────────────────────────────


def test_migration_v6_to_v7_keeps_orders_and_adds_mandates(tmp_path: Path) -> None:
    path = tmp_path / "trading.sqlite"
    store = Ledger(path)
    store.insert_order(
        {
            "order_id": "ord_old",
            "created_at": 1.0,
            "updated_at": 1.0,
            "chain_id": 8453,
            "wallet": "0xabc",
            "token_in": USDC,
            "token_out": WETH,
            "amount_raw": "10",
            "amount_human": "0.00001",
            "status": "confirmed",
            "initiator": "manual",
        }
    )
    store.close()
    # Turn the file back into what version 6 left on disk: no mandate tables,
    # no ``orders.mandate_id`` (rebuilt by hand; DROP COLUMN trips on some
    # SQLite builds over the commented CREATE TABLE).
    conn = sqlite3.connect(path)
    kept = [r[1] for r in conn.execute("PRAGMA table_info(orders)") if r[1] != "mandate_id"]
    conn.executescript(
        f"CREATE TABLE orders_v6 AS SELECT {', '.join(kept)} FROM orders; "
        "DROP TABLE orders; ALTER TABLE orders_v6 RENAME TO orders; "
        "DROP TABLE mandates; DROP TABLE mandate_runs; UPDATE schema_version SET version = 6;"
    )
    conn.commit()
    columns = {r[1] for r in conn.execute("PRAGMA table_info(orders)")}
    assert "mandate_id" not in columns
    conn.close()

    migrated = Ledger(path)
    try:
        order = migrated.get_order("ord_old")
        assert order is not None and order["status"] == "confirmed" and order["mandate_id"] is None
        conn = sqlite3.connect(path)
        version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        conn.close()
        assert version == SCHEMA_VERSION == 9
        assert {"mandates", "mandate_runs", "idx_orders_mandate", "idx_mandates_status"} <= tables
    finally:
        migrated.close()


def test_ledger_spend_counts_confirmed_and_reserves_open_orders() -> None:
    store = Ledger(":memory:")
    try:
        base = {
            "created_at": 1.0,
            "updated_at": 1.0,
            "chain_id": 8453,
            "wallet": "0xabc",
            "token_in": USDC,
            "token_out": WETH,
            "amount_raw": "10",
            "amount_human": "10",
            "initiator": "agent",
            "mandate_id": "dca_x",
        }
        rows = [
            ("a", "confirmed", 10.0, "5000"),
            ("b", "confirmed", 10.0, "7000"),
            ("c", "awaiting_approval", 150.0, None),
            ("d", "submitted", 10.0, None),
            ("e", "failed", 10.0, None),
            ("f", "rejected", 10.0, None),
        ]
        for order_id, status, usd, received in rows:
            store.insert_order(
                {
                    **base,
                    "order_id": order_id,
                    "status": status,
                    "value_usd": usd,
                    "received_out_raw": received,
                }
            )
        store.insert_order({**base, "order_id": "other", "status": "confirmed", "mandate_id": None})
        store.insert_entry(
            ts=1.0,
            chain_id=8453,
            wallet="0xabc",
            kind="swap",
            tx_hash="0x1",
            gas_usd=0.02,
            order_id="a",
        )
        assert store.mandate_spend("dca_x") == (20.0, 160.0, 12_000, pytest.approx(0.02))
        assert [o["order_id"] for o in store.orders_for_mandate("dca_x")] == list("abcdef")
        # A run row adopts the order its mandate inserts next.
        run_id = store.insert_run({"mandate_id": "dca_y", "n": 1, "at": 1.0, "status": "pending"})
        store.insert_order({**base, "order_id": "g", "status": "quoted", "mandate_id": "dca_y"})
        assert store.get_run(run_id)["order_id"] == "g"  # type: ignore[index]
        assert store.run_for_order("g")["run_id"] == run_id  # type: ignore[index]
    finally:
        store.close()


# ── the card fixtures ──────────────────────────────────────────────────────


async def _card_world(world: World) -> dict[str, dict[str, Any]]:
    """Three mandates with a history worth drawing, as the frontend fixtures show them."""
    service = world.service
    payloads: dict[str, dict[str, Any]] = {
        "mandates-empty": await service.dca_list(all=True),
    }
    active_id = (await world.create(max_price_usd=2600, slippage_pct=1.0))["mandate"]["id"]
    # A short one that ran to its cap within the first afternoon.
    world.at(T0 + HOUR)
    done_id = (
        await world.create(usd_per_run=10, cap_usd=25, every_seconds=HOUR, name="Weekend ETH")
    )["mandate"]["id"]
    prices = [2000.0, 2100.0, 1950.0, 2250.0, 2400.0, 2300.0, 2700.0]
    for day, price in enumerate(prices):
        world.set_price(price)
        world.at(T0 + day * DAY + (0 if day == 0 else 240 + 60 * day))
        await world.due()
        if day == 0:
            for hour in (1, 2, 3):
                world.at(T0 + hour * HOUR)
                world.set_price(price, usd=5.0 if hour == 3 else 10.0)  # last buy: $5 left
                await world.due()
    # Day eight: the buy waits for the user (the threshold was lowered for it).
    world.set_price(2350.0)
    world.service.config.approval_threshold_usd = 5.0
    world.at(T0 + 7 * DAY + 90)
    await world.due()
    world.service.config.approval_threshold_usd = 100.0
    world.at(T0 + 7 * DAY + 600)
    # A proposal from the agent, with the cached balance behind a warning.
    service.ledger.set_balance(8453, world.wallet, USDC, 1_000 * 10**6)
    awaiting_id = (
        await world.create(
            usd_per_run=150,
            cap_usd=1500,
            every_seconds=7 * DAY,
            name="Weekly ETH",
            initiator="agent",
            session_key="agent:desk:main",
        )
    )["mandate"]["id"]
    world.at(T0 + 7 * DAY + 900)
    payloads["mandate-active"] = await service.dca_get(active_id)
    payloads["mandate-awaiting"] = await service.dca_get(awaiting_id)
    payloads["mandate-completed"] = await service.dca_get(done_id)
    payloads["mandates"] = await service.dca_list(all=True)
    return payloads


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


async def test_card_fixtures_match_the_engine(world: World) -> None:
    payloads = await _card_world(world)
    active = payloads["mandate-active"]["mandate"]
    statuses = [r["status"] for r in reversed(active["history"])]
    assert statuses == ["filled"] * 6 + ["skipped", "parked"]
    assert active["runs"] == {"done": 6, "max": None, "skipped": 1, "failed": 0, "attempts": 8}
    assert active["budget"]["spentUsd"] == pytest.approx(60.0)
    assert active["budget"]["reservedUsd"] == pytest.approx(10.0)
    assert active["acquired"]["avgPriceUsd"] is not None
    assert active["acquired"]["currentPriceUsd"] == 2350.0
    filled = [r for r in active["history"] if r["status"] == "filled"]
    assert len({round(r["priceUsd"]) for r in filled}) == 6  # a chart with real data
    assert payloads["mandate-awaiting"]["mandate"]["status"] == "awaiting_approval"
    assert len(payloads["mandate-awaiting"]["warnings"]) == 2
    completed = payloads["mandate-completed"]["mandate"]
    assert completed["status"] == "completed" and completed["statusReason"] == "cap reached"
    listing = payloads["mandates"]
    assert [m["status"] for m in listing["mandates"]] == [
        "awaiting_approval",
        "active",
        "completed",
    ]
    assert payloads["mandates-empty"]["mandates"] == []
    assert payloads["mandates-empty"]["totals"]["count"] == 0

    if os.environ.get("AGENTOS_REGEN_DCA_FIXTURES"):
        FIXTURES.mkdir(parents=True, exist_ok=True)
        for name in FIXTURE_NAMES:
            (FIXTURES / f"{name}.json").write_text(_dump(payloads[name]), encoding="utf-8")
    for name in FIXTURE_NAMES:
        pinned = (FIXTURES / f"{name}.json").read_text(encoding="utf-8")
        assert pinned == _dump(payloads[name]), f"{name}.json is stale: regenerate it"
