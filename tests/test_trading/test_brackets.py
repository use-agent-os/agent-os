"""Brackets (``docs/brackets.md``): take-profit + stop-loss as one OCO object.

Offline, on the fake clock and price feed of ``test_triggers.py`` (its
``World`` drives the checker). The card fixtures under
``tests/fixtures/trigger_cards/bracket*.json`` are the frontend's renderer
fixtures; they are built by :func:`_card_world` and pinned here. Regenerate
them with::

    AGENTOS_REGEN_TRIGGER_FIXTURES=1 uv run pytest tests/test_trading/test_brackets.py -k fixture
"""

from __future__ import annotations

import itertools
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from agentos.trading import service as service_module
from agentos.trading import triggers
from agentos.trading.chains import BASE
from agentos.trading.dca import iso
from agentos.trading.ledger import SCHEMA_VERSION, Ledger
from agentos.trading.service import TradingError, TradingService
from tests.test_trading.conftest import PASSWORD
from tests.test_trading.fakes import USDC, WETH, FakeAggregator, FakeChain, FakePrices
from tests.test_trading.test_triggers import (
    DAY,
    FIXTURES,
    HOUR,
    KEY,
    NO_ROUTE,
    T0,
    TICK,
    Clock,
    World,
    _dump,
    _events,
)

BRACKET_FIXTURES = (
    "bracket-armed",
    "bracket-awaiting",
    "bracket-done",
    "bracket-alert",
    "brackets",
    "brackets-empty",
)


@pytest.fixture
def ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stable bracket, trigger and order ids, so payloads can be pinned."""
    groups = itertools.count(1)
    numbers = itertools.count(1)
    orders = itertools.count(1)
    monkeypatch.setattr(triggers, "new_bracket_id", lambda: f"brk_{next(groups):08x}")
    monkeypatch.setattr(triggers, "new_trigger_id", lambda: f"trg_{next(numbers):08x}")
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
    base_chain.set_erc20(WETH, wallet, 10**17)  # 0.1 WETH: $200 at $2,000
    return World(service, base_chain, fake_aggregator, fake_prices, clock, wallet)


async def protect(world: World, **overrides: Any) -> dict[str, Any]:
    """``trade protect WETH --tp 2100 --sl 1800 --pct 40`` (40 % stays under the $100 threshold)."""
    params: dict[str, Any] = {
        "chain": BASE,
        "kind": "sell",
        "token": "WETH",
        "take_profit": "2100",
        "stop_loss": "1800",
        "amount_pct": 40,
        "initiator": "manual",
    }
    params.update(overrides)
    return await world.service.bracket_create(**params)


def legs(world: World, bracket_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    tp, sl = world.service.ledger.group_triggers(bracket_id)
    assert (tp["leg"], sl["leg"]) == ("tp", "sl")
    return tp, sl


async def card(world: World, bracket_id: str) -> dict[str, Any]:
    bracket: dict[str, Any] = (await world.service.bracket_get(bracket_id))["bracket"]
    return bracket


async def _tp_parked(world: World, **overrides: Any) -> str:
    """A take-profit of the whole position (over the threshold): it fired and waits."""
    bracket_id = (await protect(world, amount_pct=100, **overrides))["bracket"]["id"]
    await world.check(2110)
    await world.check(2120)
    tp, sl = legs(world, bracket_id)
    assert tp["status"] == "triggered" and world.fires(tp["trigger_id"])[0]["status"] == "parked"
    return bracket_id


# ── pure helpers ───────────────────────────────────────────────────────────


def _leg(leg: str, status: str, reason: str | None = None) -> dict[str, Any]:
    return {"leg": leg, "status": status, "status_reason": reason}


@pytest.mark.parametrize(
    ("tp", "sl", "expected"),
    [
        (("armed", None), ("armed", None), ("armed", None)),
        (
            ("triggered", None),
            ("paused", "on hold: take-profit fired"),
            ("triggered", "take-profit triggered"),
        ),
        (
            ("triggered", "fired by hand"),
            ("paused", "on hold: take-profit fired"),
            ("triggered", "take-profit: fired by hand"),
        ),
        (("awaiting_approval", None), ("awaiting_approval", None), ("awaiting_approval", None)),
        (
            ("done", "sold 0.05 ETH for 230 USDC at $4,560"),
            ("stopped", "take-profit filled"),
            ("done", "take-profit: sold 0.05 ETH for 230 USDC at $4,560"),
        ),
        (
            ("stopped", "take-profit filled"),
            ("done", "sold 0.1 ETH for 340 USDC at $3,400"),
            ("done", "stop-loss: sold 0.1 ETH for 340 USDC at $3,400"),
        ),
        (
            ("stopped", "user"),
            ("paused", "paused: nothing to sell"),
            ("paused", "stop-loss paused: nothing to sell"),
        ),
        (
            ("paused", "paused: nothing to sell"),
            ("paused", "paused: nothing to sell"),
            ("paused", "paused: nothing to sell"),
        ),
        (("done", "sold …"), ("armed", None), ("armed", triggers.PARTIAL_TP_REASON)),
        (("stopped", "sell rejected by you"), ("armed", None), ("armed", "stop-loss armed")),
        (("rejected", "user"), ("rejected", "user"), ("rejected", "user")),
        (("expired", "x"), ("stopped", "y"), ("stopped", "stop-loss: y")),
    ],
)
def test_bracket_status_precedence(
    tp: tuple[str, str | None], sl: tuple[str, str | None], expected: tuple[str, str | None]
) -> None:
    assert triggers.bracket_status([_leg("tp", *tp), _leg("sl", *sl)]) == expected
    # Order does not matter, and Trigger payload objects read the same.
    objects = [
        {"bracket": {"leg": leg}, "status": s, "statusReason": r}
        for leg, (s, r) in (("sl", sl), ("tp", tp))
    ]
    assert triggers.bracket_status(objects) == expected


def test_bracket_words_and_reasons() -> None:
    assert triggers.LEGS == ("tp", "sl") and triggers.OCO_HOLD == "on hold: "
    assert triggers.leg_word("tp") == "take-profit" and triggers.leg_word("sl") == "stop-loss"
    assert triggers.hold_reason("tp") == "on hold: take-profit fired"
    assert triggers.hold_reason("stop-loss") == "on hold: stop-loss fired"
    assert triggers.oco_stopped_reason("sell", "tp") == "take-profit filled"
    assert triggers.oco_stopped_reason("sell", "sl") == "stop-loss filled"
    assert triggers.oco_stopped_reason("alert", "tp") == "range left over the top"
    assert triggers.oco_stopped_reason("alert", "sl") == "range left under the floor"
    assert triggers.bracket_default_name("sell", "ETH") == "Protect ETH"
    assert triggers.bracket_default_name("alert", "ETH") == "Watch ETH"
    new_id = triggers.new_bracket_id()
    assert new_id.startswith(triggers.BRACKET_ID_PREFIX) and len(new_id) == 12
    assert triggers.bracket_lines_problem(3000, 3400) == (
        "take-profit $3,000 must be above stop-loss $3,400"
    )
    assert triggers.bracket_lines_problem(4560, 3420) is None
    assert triggers.bracket_lines_problem(4560, None) is None
    assert triggers.release("trail", 2100.0, 5.0) == {
        "status": "armed",
        "status_reason": None,
        "hits": 0,
        "bad_streak": 0,
        "peak_price_usd": 2100.0,
        "last_price_usd": 2100.0,
        "last_checked_at": 5.0,
    }
    assert triggers.is_on_hold(_leg("sl", "paused", "on hold: take-profit fired"))
    assert not triggers.is_on_hold(_leg("sl", "paused", "user"))


def test_nearest_leg() -> None:
    tp = {"leg": "tp", "direction": "above", "price_usd": 2400.0}
    sl = {"leg": "sl", "direction": "below", "price_usd": 1800.0}
    assert triggers.nearest_leg([tp, sl], 2000.0) == "sl"  # −10 % vs +20 %
    assert triggers.nearest_leg([tp, sl], 2300.0) == "tp"
    assert triggers.nearest_leg([tp, sl], None) == "sl"  # no price: the stop
    assert triggers.nearest_leg([tp], 2000.0) == "tp"
    objects = [
        {"bracket": {"leg": "tp"}, "market": {"distancePct": 3.0}},
        {"bracket": {"leg": "sl"}, "market": {"distancePct": -5.0}},
    ]
    assert triggers.nearest_leg(objects) == "tp"


@pytest.mark.parametrize(
    ("terms", "problem"),
    [
        ({"kind": "buy"}, "kind must be sell or alert"),
        ({"take_profit": None}, "takeProfit is required"),
        ({"stop_loss": None}, "one of stopLoss or trailPct"),
        ({"trail_pct": 10}, "exclude each other"),
        ({"stop_loss": None, "trail_pct": 100}, "trailPct must be above 0 and under 100"),
        ({"take_profit": "-10%"}, "takeProfit: above takes a percent over"),
        ({"stop_loss": "+10%"}, "stopLoss: below takes a percent under"),
        ({"amount_pct": None}, "a sell needs one size"),
        ({"amount_usd": 10}, "takes one size"),
        ({"amount_pct": 120}, "at most 100"),
        ({"kind": "alert"}, "an alert takes no size"),
        ({"kind": "alert", "amount_pct": None, "tp_pct": 50}, "an alert takes no size"),
        ({"amount_pct": None, "amount": "0.05", "tp_pct": 50}, "tpPct works with amountPct"),
        ({"tp_pct": 0}, "tpPct must be above 0"),
        ({"amount_pct": 50, "tp_pct": 60}, "tpPct must not exceed amountPct (50 %)"),
        ({"valid_for_seconds": 30}, "at least 60"),
    ],
)
def test_validate_bracket_terms(terms: dict[str, Any], problem: str) -> None:
    base: dict[str, Any] = {
        "kind": "sell",
        "take_profit": "+20%",
        "stop_loss": "-10%",
        "trail_pct": None,
        "amount_usd": None,
        "amount_pct": 100,
        "amount": None,
        "tp_pct": None,
        "valid_for_seconds": None,
    }
    found = triggers.validate_bracket_terms(**{**base, **terms})
    assert found is not None and problem in found, found
    assert triggers.validate_bracket_terms(**base) is None


# ── create ─────────────────────────────────────────────────────────────────


async def test_operator_create_arms_both_legs(world: World) -> None:
    answer = await protect(world, take_profit="+20%", stop_loss="-10%", amount_pct=None)
    assert answer["kind"] == "bracket" and answer["version"] == 1
    bracket = answer["bracket"]
    assert answer["request"] == {"kind": "get", "params": {"bracketId": bracket["id"]}}
    assert bracket["id"] == "brk_00000001" and bracket["name"] == "Protect WETH"
    assert bracket["status"] == "armed" and bracket["statusReason"] is None
    assert bracket["kind"] == "sell" and bracket["initiator"] == "manual"
    assert bracket["takeProfit"]["name"] == "Take-profit WETH"
    assert bracket["stopLoss"]["name"] == "Stop-loss WETH"
    for leg_name, leg in (("tp", bracket["takeProfit"]), ("sl", bracket["stopLoss"])):
        assert leg["bracket"] == {"id": "brk_00000001", "name": "Protect WETH", "leg": leg_name}
        assert leg["status"] == "armed" and leg["armedAt"] == iso(T0)
    lines = bracket["lines"]
    assert lines["takeProfitUsd"] == 2400.0 and lines["stopLossUsd"] == 1800.0
    assert lines["fromPriceUsd"] == 2000.0 and lines["trailPct"] is None
    assert lines["takeProfitLabel"] == "over $2,400" and lines["stopLossLabel"] == "under $1,800"
    action = bracket["action"]
    assert action["amountPct"] == 100.0 and action["tpPct"] is None  # the default size
    assert action["label"] == "sell 100 % of WETH → USDC"
    assert action["estimatedUsd"] == pytest.approx(200.0) and action["needsApproval"] is True
    market = bracket["market"]
    assert market["priceUsd"] == 2000.0 and market["balance"]["human"] == "0.1"
    assert market["upsidePct"] == pytest.approx(20.0)
    assert market["downsidePct"] == pytest.approx(-10.0)
    assert market["positionPct"] == pytest.approx(100 / 3)
    assert market["rewardRisk"] == 2.0 and market["nearest"] == "sl"
    assert bracket["fired"] is None and bracket["result"] is None
    tp, sl = legs(world, bracket["id"])
    for column in ("wallet", "token", "quote", "slippage_pct", "valid_until", "initiator"):
        assert tp[column] == sl[column]
    assert tp["group_name"] == sl["group_name"] == "Protect WETH"
    assert (tp["direction"], sl["direction"]) == ("above", "below")
    # The approval threshold warning is the same on both legs: said once, unprefixed.
    assert answer["warnings"] == [
        "a sell of ≈$200 is above the $100 approval threshold and will wait for you when it fires"
    ]
    changed = _events(world.service, "trading.bracket.changed")
    assert [e["bracket"]["id"] for e in changed] == [bracket["id"]]
    assert any(
        e.get("reason") == "bracket" and e.get("bracketId") == bracket["id"]
        for e in _events(world.service, "trading.changed")
    )
    leg_events = _events(world.service, "trading.trigger.changed")
    assert {e["trigger"]["id"] for e in leg_events} == {tp["trigger_id"], sl["trigger_id"]}


async def test_agent_create_parks_both_legs_in_one_card(world: World) -> None:
    answer = await protect(world, initiator="agent", session_key="agent:desk:x")
    bracket = answer["bracket"]
    assert bracket["status"] == "awaiting_approval" and bracket["sessionKey"] == "agent:desk:x"
    assert bracket["expiresAt"] == iso(T0 + triggers.PENDING_TTL) and bracket["armedAt"] is None
    tp, sl = legs(world, bracket["id"])
    assert tp["status"] == sl["status"] == "awaiting_approval"
    assert tp["expires_at"] == sl["expires_at"] == T0 + triggers.PENDING_TTL
    # Proposals are never checked.
    await world.check(2200)
    await world.check(2200)
    assert world.fires(tp["trigger_id"]) == []
    approved = (await world.service.bracket_approve(bracket["id"]))["bracket"]
    assert approved["status"] == "armed" and approved["expiresAt"] is None
    assert approved["approvedAt"] == iso(T0 + 2 * TICK)
    assert approved["takeProfit"]["market"]["armedPriceUsd"] == 2200.0
    with pytest.raises(TradingError) as err:
        await world.service.bracket_approve(bracket["id"])
    assert err.value.code == "trading.bracket.bad_state"
    assert str(err.value) == "cannot approve bracket brk_00000001: it is armed"


async def test_names_lines_and_sizes(world: World) -> None:
    named = (await protect(world, name="  My   ETH  "))["bracket"]
    assert named["name"] == "My ETH" and named["takeProfit"]["name"] == "Take-profit WETH"
    trail = (await protect(world, stop_loss=None, trail_pct=10))["bracket"]
    assert trail["stopLoss"]["name"] == "Trailing stop WETH"
    assert trail["lines"]["stopLossUsd"] == pytest.approx(1800.0)
    assert trail["lines"]["trailPct"] == 10.0
    assert trail["lines"]["stopLossLabel"] == "10 % below peak"
    alert = (await protect(world, kind="alert", amount_pct=None, take_profit="4560"))["bracket"]
    assert alert["name"] == "Watch WETH" and alert["action"]["label"] == "notify"
    assert alert["takeProfit"]["name"] == "Alert WETH over $4,560"
    assert alert["stopLoss"]["name"] == "Alert WETH under $1,800"
    assert alert["market"]["balance"] is None and alert["action"]["estimatedUsd"] is None
    trail_alert = (
        await protect(world, kind="alert", amount_pct=None, stop_loss=None, trail_pct=10)
    )["bracket"]
    assert trail_alert["stopLoss"]["name"] == "Alert WETH −10 % from peak"
    partial = (await protect(world, amount_pct=100, tp_pct=50))["bracket"]
    assert partial["action"]["tpPct"] == 50.0 and partial["action"]["amountPct"] == 100.0
    assert partial["takeProfit"]["action"]["amountPct"] == 50.0
    assert partial["stopLoss"]["action"]["amountPct"] == 100.0
    assert partial["action"]["label"] == "sell 50 % of WETH at take-profit, 100 % at stop → USDC"
    whole = (await protect(world, amount_pct=60, tp_pct=60))["bracket"]  # equal: dropped
    assert whole["action"]["tpPct"] is None and whole["takeProfit"]["action"]["amountPct"] == 60
    fixed = (await protect(world, amount_pct=None, amount="0.05"))["bracket"]
    assert fixed["action"]["amount"]["human"] == "0.05"
    assert fixed["action"]["label"] == "sell 0.05 WETH → USDC"
    met = await protect(world, take_profit="1950", stop_loss="1500")
    assert met["warnings"][0] == (
        "take-profit: WETH is already at $2,000, over $1,950: this fires after the next two checks"
    )


async def test_create_validation(world: World) -> None:
    cases: list[tuple[dict[str, Any], str, str | None]] = [
        ({"kind": "buy"}, "trading.bracket.invalid", "kind must be sell or alert"),
        (
            {"take_profit": "1700"},
            "trading.bracket.invalid",
            "take-profit $1,700 must be above stop-loss $1,800",
        ),
        (
            {"take_profit": "1750", "stop_loss": None, "trail_pct": 10},
            "trading.bracket.invalid",
            "take-profit $1,750 must be above stop-loss $1,800",
        ),
        ({"tp_pct": 50, "amount_pct": 40}, "trading.bracket.invalid", None),
        ({"tp_pct": -1}, "trading.bracket.invalid", None),
        ({"amount_pct": None, "amount": "0.05", "tp_pct": 10}, "trading.bracket.invalid", None),
        ({"kind": "alert"}, "trading.bracket.invalid", "an alert takes no size"),
        ({"amount_usd": 10}, "trading.bracket.invalid", None),
        ({"wallet": "all"}, "trading.bracket.invalid", None),
        ({"slippage_pct": 50}, "trading.bracket.invalid", None),
        ({"token": "USDC", "quote": "USDC"}, "trading.bracket.invalid", None),
        ({"token": "NOPE"}, "trading.token_not_found", None),
        ({"trail_pct": "x", "stop_loss": None}, "trading.bracket.invalid", None),
        ({"initiator": "robot"}, "trading.invalid", None),
    ]
    for overrides, code, message in cases:
        with pytest.raises(TradingError) as err:
            await protect(world, **overrides)
        assert err.value.code == code, (overrides, err.value)
        if message is not None:
            assert str(err.value) == message
    world.set_price(None)
    with pytest.raises(TradingError) as err:
        await protect(world, take_profit="+20%")
    assert str(err.value) == "WETH has no price right now: give an absolute price"
    blind = await protect(world)  # absolute lines arm without a price
    assert blind["bracket"]["status"] == "armed" and blind["bracket"]["market"]["nearest"] is None
    world.service.config.enabled = False
    with pytest.raises(TradingError) as err:
        await protect(world)
    assert err.value.code == "trading.disabled"


# ── legs are not triggers of their own ─────────────────────────────────────


async def test_trigger_list_hides_legs_and_reads_still_work(world: World) -> None:
    plain = (await world.service.trigger_create(
        chain=BASE, kind="alert", token="WETH", direction="below", price="1900",
        initiator="manual",
    ))["trigger"]  # fmt: skip
    assert plain["bracket"] is None
    bracket_id = (await protect(world))["bracket"]["id"]
    listing = await world.service.trigger_list(all=True)
    assert [t["id"] for t in listing["triggers"]] == [plain["id"]]
    assert listing["totals"]["count"] == 1
    tp, _ = legs(world, bracket_id)
    leg = (await world.service.trigger_get(tp["trigger_id"]))["trigger"]
    assert leg["bracket"] == {"id": bracket_id, "name": "Protect WETH", "leg": "tp"}


async def test_leg_writes_are_refused(world: World) -> None:
    bracket_id = (await protect(world))["bracket"]["id"]
    tp, sl = legs(world, bracket_id)
    service = world.service
    for op, call in (
        ("approve", service.trigger_approve),
        ("reject", service.trigger_reject),
        ("pause", service.trigger_pause),
        ("resume", service.trigger_resume),
        ("stop", service.trigger_stop),
        ("fire", service.trigger_fire_now),
    ):
        with pytest.raises(TradingError) as err:
            await call(tp["trigger_id"])
        assert err.value.code == "trading.trigger.bad_state"
        assert str(err.value) == (
            f"{tp['trigger_id']} is the take-profit leg of bracket {bracket_id}: "
            f"use trading.bracket.{op}"
        )
    with pytest.raises(TradingError, match="is the stop-loss leg"):
        await service.trigger_pause(sl["trigger_id"])
    assert legs(world, bracket_id)[0]["status"] == "armed"


# ── one cancels the other ──────────────────────────────────────────────────


async def test_tp_claim_holds_the_sl(world: World) -> None:
    bracket_id = await _tp_parked(world)
    tp, sl = legs(world, bracket_id)
    assert sl["status"] == "paused" and sl["status_reason"] == "on hold: take-profit fired"
    bracket = await card(world, bracket_id)
    assert bracket["status"] == "triggered" and bracket["statusReason"] == "take-profit triggered"
    # The stop on hold is not checked, however far the price falls.
    await world.check(1700)
    await world.check(1700)
    assert world.fires(sl["trigger_id"]) == []
    fired = _events(world.service, "trading.trigger.fired")
    assert fired[-1]["trigger"]["bracket"]["leg"] == "tp"
    for call in (world.service.bracket_pause, world.service.bracket_resume):
        with pytest.raises(TradingError) as err:
            await call(bracket_id)
        assert err.value.code == "trading.bracket.bad_state"
    with pytest.raises(TradingError) as err:
        await world.service.bracket_fire_now(bracket_id)
    assert err.value.code == "trading.bracket.bad_state"


async def test_tp_filled_stops_the_sl(world: World) -> None:
    bracket_id = (await protect(world))["bracket"]["id"]
    await world.check(2110)
    await world.check(2120)
    tp, sl = legs(world, bracket_id)
    assert tp["status"] == "done"
    assert tp["status_reason"] == "sold 0.04 WETH for 84.8 USDC at $2,120"
    assert sl["status"] == "stopped" and sl["status_reason"] == "take-profit filled"
    bracket = await card(world, bracket_id)
    assert bracket["status"] == "done" and bracket["fired"] == "tp"
    assert bracket["statusReason"] == "take-profit: sold 0.04 WETH for 84.8 USDC at $2,120"
    assert bracket["result"] == bracket["takeProfit"]["result"]
    assert bracket["result"]["amountIn"]["human"] == "0.04"
    assert bracket["action"]["estimatedUsd"] == bracket["result"]["amountIn"]["usd"]
    assert bracket["market"]["nearest"] is None and bracket["market"]["balance"] is None
    last = _events(world.service, "trading.bracket.changed")[-1]["bracket"]
    assert last["status"] == "done" and last["stopLoss"]["status"] == "stopped"
    for call in (world.service.bracket_stop, world.service.bracket_fire_now):
        with pytest.raises(TradingError) as err:
            await call(bracket_id)
        assert err.value.code == "trading.bracket.bad_state"


async def test_sl_filled_stops_the_tp(world: World) -> None:
    bracket_id = (await protect(world))["bracket"]["id"]
    await world.check(1790)
    await world.check(1780)
    tp, sl = legs(world, bracket_id)
    assert sl["status"] == "done" and sl["status_reason"].startswith("sold 0.04 WETH")
    assert tp["status"] == "stopped" and tp["status_reason"] == "stop-loss filled"
    bracket = await card(world, bracket_id)
    assert bracket["status"] == "done" and bracket["fired"] == "sl"
    assert bracket["result"]["orderId"] == bracket["stopLoss"]["result"]["orderId"]


async def test_partial_take_profit_releases_the_stop_for_the_rest(world: World) -> None:
    bracket_id = (await protect(world, tp_pct=20))["bracket"]["id"]
    await world.check(2110)
    await world.check(2120)
    tp, sl = legs(world, bracket_id)
    assert tp["status"] == "done" and tp["status_reason"].startswith("sold 0.02 WETH")
    assert sl["status"] == "armed" and sl["hits"] == 0
    bracket = await card(world, bracket_id)
    assert bracket["status"] == "armed"
    assert bracket["statusReason"] == "take-profit filled · stop-loss guards the rest"
    assert bracket["fired"] == "tp" and bracket["result"]["amountIn"]["human"] == "0.02"
    assert bracket["market"]["upsidePct"] is None and bracket["market"]["nearest"] == "sl"
    # The stop then sells 40 % of what is left (0.08 WETH): the bracket is done.
    await world.check(1790)
    await world.check(1790)
    tp, sl = legs(world, bracket_id)
    assert sl["status"] == "done" and sl["status_reason"].startswith("sold 0.032 WETH")
    assert tp["status"] == "done"
    done = await card(world, bracket_id)
    assert done["status"] == "done" and done["fired"] == "sl"


async def test_failed_fire_releases_the_hold(world: World) -> None:
    world.aggregator.quote_error = NO_ROUTE
    bracket_id = (await protect(world))["bracket"]["id"]
    await world.check(2110)
    await world.check(2120)
    tp, sl = legs(world, bracket_id)
    assert world.fires(tp["trigger_id"])[0]["status"] == "failed"
    assert tp["status"] == "armed" and tp["bad_streak"] == 1
    assert sl["status"] == "armed" and sl["status_reason"] is None and sl["hits"] == 0
    assert (await card(world, bracket_id))["status"] == "armed"


async def test_parked_order_rejected_stops_the_leg_and_releases_the_sibling(world: World) -> None:
    bracket_id = await _tp_parked(world, stop_loss=None, trail_pct=10)
    tp, sl = legs(world, bracket_id)
    world.set_price(2200.0)
    await world.service.reject(world.fires(tp["trigger_id"])[0]["order_id"], "not now")
    tp, sl = legs(world, bracket_id)
    assert tp["status"] == "stopped" and tp["status_reason"] == "sell rejected by you"
    assert sl["status"] == "armed" and sl["status_reason"] is None
    assert sl["peak_price_usd"] == 2200.0  # a trail released restarts from the price now
    bracket = await card(world, bracket_id)
    assert bracket["status"] == "armed" and bracket["statusReason"] == "stop-loss armed"


async def test_parked_order_expired_pauses_the_leg_and_releases_the_sibling(world: World) -> None:
    bracket_id = await _tp_parked(world)
    world.at(world.clock.now + 901)
    await world.service.expire_orders()
    tp, sl = legs(world, bracket_id)
    assert tp["status"] == "paused"
    assert tp["status_reason"] == "the sell waited for approval and expired"
    assert sl["status"] == "armed"
    assert (await card(world, bracket_id))["status"] == "armed"


async def test_skipped_pauses_both(world: World) -> None:
    world.chain.set_erc20(WETH, world.wallet, 0)
    bracket_id = (await protect(world))["bracket"]["id"]
    await world.check(2110)
    await world.check(2120)
    tp, sl = legs(world, bracket_id)
    assert tp["status"] == sl["status"] == "paused"
    assert tp["status_reason"] == sl["status_reason"] == "paused: nothing to sell"
    bracket = await card(world, bracket_id)
    assert bracket["status"] == "paused" and bracket["statusReason"] == "paused: nothing to sell"
    world.chain.set_erc20(WETH, world.wallet, 10**17)
    resumed = (await world.service.bracket_resume(bracket_id))["bracket"]
    assert resumed["status"] == "armed"
    assert resumed["takeProfit"]["status"] == resumed["stopLoss"]["status"] == "armed"


async def test_a_sibling_the_user_paused_is_not_released(world: World) -> None:
    bracket_id = (await protect(world))["bracket"]["id"]
    await world.service.bracket_pause(bracket_id)
    world.aggregator.quote_error = NO_ROUTE
    answer = await world.service.bracket_fire_now(bracket_id, leg="tp")
    assert answer["fire"]["status"] == "failed"
    tp, sl = legs(world, bracket_id)
    assert tp["status"] == "paused" and tp["status_reason"] == "fire failed: trading.no_route"
    assert sl["status"] == "paused" and sl["status_reason"] == "user"


async def test_fire_now_the_nearest_or_the_given_leg(world: World) -> None:
    service = world.service
    alert = {"kind": "alert", "amount_pct": None, "take_profit": "2400"}
    near_sl = (await protect(world, **alert))["bracket"]["id"]
    answer = await service.bracket_fire_now(near_sl)  # $2,000: −10 % beats +20 %
    assert answer["kind"] == "bracket" and answer["fire"]["status"] == "alerted"
    assert answer["fire"]["manual"] is True
    bracket = answer["bracket"]
    assert bracket["status"] == "done" and bracket["fired"] == "sl"
    assert bracket["takeProfit"]["statusReason"] == "range left under the floor"
    world.set_price(2300.0)
    near_tp = (await protect(world, **alert))["bracket"]["id"]
    assert (await service.bracket_fire_now(near_tp))["bracket"]["fired"] == "tp"
    world.set_price(2000.0)
    given = (await protect(world, **alert))["bracket"]["id"]
    done = (await service.bracket_fire_now(given, leg="tp"))["bracket"]
    assert done["fired"] == "tp" and done["stopLoss"]["statusReason"] == "range left over the top"
    with pytest.raises(TradingError) as err:
        await service.bracket_fire_now(given, leg="xx")
    assert err.value.code == "trading.bracket.invalid"
    # A sell: Sell now on the stop fills and stops the take-profit.
    sell = (await protect(world))["bracket"]["id"]
    sold = await service.bracket_fire_now(sell, leg="sl", wait=True)
    assert sold["fire"]["status"] == "filled" and sold["bracket"]["status"] == "done"
    assert sold["bracket"]["takeProfit"]["statusReason"] == "stop-loss filled"


async def test_pause_resume_stop_reject_act_on_both(world: World) -> None:
    service = world.service
    bracket_id = (await protect(world, stop_loss=None, trail_pct=10))["bracket"]["id"]
    paused = (await service.bracket_pause(bracket_id))["bracket"]
    assert paused["status"] == "paused" and paused["statusReason"] == "user"
    assert {paused["takeProfit"]["status"], paused["stopLoss"]["status"]} == {"paused"}
    with pytest.raises(TradingError) as err:
        await service.bracket_pause(bracket_id)
    assert err.value.code == "trading.bracket.bad_state"
    world.set_price(2300.0)
    resumed = (await service.bracket_resume(bracket_id))["bracket"]
    assert resumed["status"] == "armed"
    assert resumed["stopLoss"]["condition"]["peakPriceUsd"] == 2300.0
    with pytest.raises(TradingError) as err:
        await service.bracket_resume(bracket_id)
    assert err.value.code == "trading.bracket.bad_state"
    for call in (service.bracket_approve, service.bracket_reject):
        with pytest.raises(TradingError) as err:
            await call(bracket_id)
        assert err.value.code == "trading.bracket.bad_state"
    stopped = (await service.bracket_stop(bracket_id, reason="done with it"))["bracket"]
    assert stopped["status"] == "stopped" and stopped["statusReason"] == "user: done with it"
    assert {stopped["takeProfit"]["status"], stopped["stopLoss"]["status"]} == {"stopped"}
    pending = (await protect(world, initiator="agent"))["bracket"]["id"]
    rejected = (await service.bracket_reject(pending, reason="too risky"))["bracket"]
    assert rejected["status"] == "rejected" and rejected["statusReason"] == "user: too risky"
    with pytest.raises(TradingError) as err:
        await service.bracket_get("brk_ffffffff")
    assert err.value.code == "trading.bracket.not_found"


async def test_stop_rejects_a_parked_order(world: World) -> None:
    bracket_id = await _tp_parked(world)
    tp, _ = legs(world, bracket_id)
    order_id = world.fires(tp["trigger_id"])[0]["order_id"]
    stopped = (await world.service.bracket_stop(bracket_id))["bracket"]
    assert stopped["status"] == "stopped"
    order = world.service.ledger.get_order(order_id)
    assert order is not None and order["status"] == "rejected"
    tp, sl = legs(world, bracket_id)
    assert tp["status"] == sl["status"] == "stopped"


async def test_valid_until_and_pending_ttl_expire_both(world: World) -> None:
    timed = (await protect(world, valid_for_seconds=HOUR))["bracket"]
    assert timed["validUntil"] == iso(T0 + HOUR)
    pending = (await protect(world, initiator="agent"))["bracket"]["id"]
    world.at(T0 + HOUR)
    await world.check(1950, step=0)
    tp, sl = legs(world, timed["id"])
    assert tp["status"] == sl["status"] == "expired"
    assert tp["status_reason"] == "not reached by 2026-09-20"
    assert (await card(world, timed["id"]))["status"] == "expired"
    world.at(T0 + triggers.PENDING_TTL + 1)
    with pytest.raises(TradingError) as err:
        await world.service.bracket_approve(pending)
    assert err.value.code == "trading.bracket.bad_state"
    tp, sl = legs(world, pending)
    assert tp["status"] == sl["status"] == "expired"


async def test_list(world: World) -> None:
    service = world.service
    assert (await service.bracket_list())["brackets"] == []
    first = (await protect(world))["bracket"]["id"]
    second = (await protect(world, initiator="agent", name="Proposal"))["bracket"]["id"]
    old = (await protect(world, name="Old"))["bracket"]["id"]
    await service.bracket_stop(old)
    live = await service.bracket_list()
    assert [b["id"] for b in live["brackets"]] == [second, first]
    assert live["totals"] == {"count": 2, "armed": 1, "awaiting": 1, "triggered": 0}
    assert live["kind"] == "brackets" and live["request"] == {
        "kind": "list",
        "params": {"all": False},
    }
    everything = await service.bracket_list(all=True, wallet="Key main")
    assert [b["id"] for b in everything["brackets"]] == [second, first, old]
    assert everything["request"]["params"] == {"all": True, "wallet": "Key main"}


async def test_reconcile_after_restart_settles_the_leg_and_its_sibling(world: World) -> None:
    bracket_id = await _tp_parked(world)
    tp, _ = legs(world, bracket_id)
    fire = world.fires(tp["trigger_id"])[0]
    # The order expires behind the engine's back (a restart ate the event).
    world.service.ledger.update_order(fire["order_id"], status="expired", reason="expired")
    await world.check(2000)
    tp, sl = legs(world, bracket_id)
    assert tp["status"] == "paused" and sl["status"] == "armed"


# ── ledger ─────────────────────────────────────────────────────────────────


def _trigger_row(trigger_id: str, **fields: Any) -> dict[str, Any]:
    return {
        "trigger_id": trigger_id,
        "kind": "sell",
        "name": "Stop-loss WETH",
        "status": "armed",
        "chain_id": 8453,
        "wallet": "0xabc",
        "token": WETH,
        "quote": USDC,
        "direction": "below",
        "price_usd": 1800.0,
        "initiator": "manual",
        "created_at": 1.0,
        "updated_at": 1.0,
        **fields,
    }


def test_ledger_groups() -> None:
    store = Ledger(":memory:")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            store.insert_triggers([_trigger_row("trg_a"), _trigger_row("trg_a")])
        assert store.list_triggers(legs="all") == []  # all or nothing
        store.insert_trigger(_trigger_row("trg_plain"))
        store.insert_triggers(
            [
                _trigger_row("trg_s1", group_id="brk_1", group_name="Protect", leg="sl"),
                _trigger_row(
                    "trg_t1", group_id="brk_1", group_name="Protect", leg="tp", direction="above"
                ),
            ]
        )
        store.insert_triggers(
            [
                _trigger_row("trg_t2", group_id="brk_2", leg="tp", created_at=2.0, wallet="0xdef"),
                _trigger_row(
                    "trg_s2", group_id="brk_2", leg="sl", created_at=2.0, status="stopped"
                ),
            ]
        )
        assert [r["trigger_id"] for r in store.list_triggers()] == ["trg_plain"]
        assert len(store.list_triggers(legs="only")) == 4
        assert len(store.list_triggers(legs="all")) == 5
        with pytest.raises(ValueError):
            store.list_triggers(legs="some")
        assert [r["leg"] for r in store.group_triggers("brk_1")] == ["tp", "sl"]
        assert store.group_triggers("brk_none") == []
        assert store.list_groups() == ["brk_2", "brk_1"]
        assert store.list_groups(statuses=["stopped"]) == ["brk_2"]
        assert store.list_groups(statuses=["armed"], wallet="0xABC") == ["brk_1"]
        assert store.list_groups(statuses=[]) == []
    finally:
        store.close()


def test_migration_v8_to_v9_keeps_triggers(tmp_path: Path) -> None:
    path = tmp_path / "trading.sqlite"
    store = Ledger(path)
    store.insert_trigger(_trigger_row("trg_old"))
    store.close()
    # Turn the file back into what version 8 left on disk: no group columns.
    conn = sqlite3.connect(path)
    kept = [
        r[1]
        for r in conn.execute("PRAGMA table_info(triggers)")
        if r[1] not in ("group_id", "group_name", "leg")
    ]
    conn.executescript(
        f"CREATE TABLE triggers_v8 AS SELECT {', '.join(kept)} FROM triggers; "
        "DROP TABLE triggers; ALTER TABLE triggers_v8 RENAME TO triggers; "
        "UPDATE schema_version SET version = 8;"
    )
    conn.commit()
    assert "group_id" not in {r[1] for r in conn.execute("PRAGMA table_info(triggers)")}
    conn.close()

    migrated = Ledger(path)
    try:
        row = migrated.get_trigger("trg_old")
        assert row is not None and row["status"] == "armed"
        assert row["group_id"] is None and row["group_name"] is None and row["leg"] is None
        assert [r["trigger_id"] for r in migrated.list_triggers()] == ["trg_old"]
        conn = sqlite3.connect(path)
        version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        conn.close()
        assert version == SCHEMA_VERSION == 9
        assert "idx_triggers_group" in names
    finally:
        migrated.close()


# ── the card fixtures ──────────────────────────────────────────────────────


async def _card_world(world: World) -> dict[str, dict[str, Any]]:
    """Brackets in each state the frontend fixtures show."""
    service = world.service
    payloads: dict[str, dict[str, Any]] = {
        "brackets-empty": await service.bracket_list(all=True),
    }
    # Armed at $2,000: take profit 20 % up, stop 10 % down, the whole position.
    armed_id = (
        await protect(
            world,
            take_profit="+20%",
            stop_loss="-10%",
            amount_pct=None,
            slippage_pct=1.0,
            valid_for_seconds=7 * DAY,
        )
    )["bracket"]["id"]
    # A take-profit that ran in the first minute: 40 % sold over $2,100.
    done_id = (await protect(world, take_profit="2100", stop_loss="1900"))["bracket"]["id"]
    # A range alert: tell me when WETH leaves $1,850 – $2,300.
    alert_id = (
        await protect(world, kind="alert", amount_pct=None, take_profit="2300", stop_loss="1850")
    )["bracket"]["id"]
    await world.check(2110)
    await world.check(2120)
    await world.check(2000)
    # A proposal from the agent: half at +15 %, a 10 % trailing stop on all of it.
    world.at(world.clock.now + 20)
    awaiting_id = (
        await protect(
            world,
            take_profit="+15%",
            stop_loss=None,
            trail_pct=10,
            amount_pct=100,
            tp_pct=50,
            initiator="agent",
            session_key="agent:desk:main",
        )
    )["bracket"]["id"]
    world.at(world.clock.now + 10)
    payloads["bracket-armed"] = await service.bracket_get(armed_id)
    payloads["bracket-awaiting"] = await service.bracket_get(awaiting_id)
    payloads["bracket-done"] = await service.bracket_get(done_id)
    payloads["bracket-alert"] = await service.bracket_get(alert_id)
    payloads["brackets"] = await service.bracket_list()
    return payloads


async def test_card_fixtures_match_the_engine(world: World) -> None:
    payloads = await _card_world(world)
    armed = payloads["bracket-armed"]["bracket"]
    assert armed["status"] == "armed" and armed["name"] == "Protect WETH"
    assert armed["market"]["priceUsd"] == 2000.0
    assert armed["market"]["positionPct"] == pytest.approx(33.333333333)
    assert armed["market"]["rewardRisk"] == 2.0 and armed["market"]["nearest"] == "sl"
    assert armed["lines"]["takeProfitUsd"] == 2400.0 and armed["lines"]["stopLossUsd"] == 1800.0
    awaiting = payloads["bracket-awaiting"]["bracket"]
    assert awaiting["status"] == "awaiting_approval" and awaiting["action"]["tpPct"] == 50.0
    assert awaiting["stopLoss"]["condition"]["direction"] == "trail"
    assert awaiting["sessionKey"] == "agent:desk:main"
    done = payloads["bracket-done"]["bracket"]
    assert done["status"] == "done" and done["fired"] == "tp"
    assert done["stopLoss"]["status"] == "stopped"
    assert done["stopLoss"]["statusReason"] == "take-profit filled"
    assert done["result"]["amountIn"]["human"] == "0.04"
    alert = payloads["bracket-alert"]["bracket"]
    assert alert["kind"] == "alert" and alert["status"] == "armed"
    assert alert["name"] == "Watch WETH" and alert["action"]["label"] == "notify"
    listing = payloads["brackets"]
    assert [b["id"] for b in listing["brackets"]] == [
        payloads["bracket-awaiting"]["bracket"]["id"],
        alert["id"],
        armed["id"],
    ]
    assert listing["totals"] == {"count": 3, "armed": 2, "awaiting": 1, "triggered": 0}
    assert payloads["brackets-empty"]["brackets"] == []

    if os.environ.get("AGENTOS_REGEN_TRIGGER_FIXTURES"):
        FIXTURES.mkdir(parents=True, exist_ok=True)
        for name in BRACKET_FIXTURES:
            (FIXTURES / f"{name}.json").write_text(_dump(payloads[name]), encoding="utf-8")
    for name in BRACKET_FIXTURES:
        pinned = (FIXTURES / f"{name}.json").read_text(encoding="utf-8")
        assert pinned == _dump(payloads[name]), f"{name}.json is stale: regenerate it"


@pytest.mark.parametrize(
    ("raw", "decimals", "text"),
    [
        (10**18, 18, "1"),
        (298, 6, "0.000298"),
        (110_000_000_000, 18, "0.00000011"),  # 1.1e-7 ETH: never "for 0 ETH"
        (123_456_789, 18, "0.000000000123"),
        (1, 18, "0.000000000000000001"),
        (0, 18, "0"),
    ],
)
def test_human_keeps_a_dust_amount_visible(raw: int, decimals: int, text: str) -> None:
    assert triggers.human(raw, decimals) == text


def test_range_alert_legs_are_the_ceiling_and_the_floor() -> None:
    assert triggers.leg_word("tp", "alert") == "ceiling"
    assert triggers.leg_word("sl", "alert") == "floor"
    assert triggers.hold_reason("tp", "alert") == "on hold: ceiling fired"
    tp = {"leg": "tp", "kind": "alert", "status": "done", "status_reason": "alerted at $1"}
    sl = {
        "leg": "sl",
        "kind": "alert",
        "status": "stopped",
        "status_reason": "range left over the top",
    }
    assert triggers.bracket_status([tp, sl]) == ("done", "ceiling: alerted at $1")


def test_a_sibling_stopped_by_the_user_is_named_in_the_reason() -> None:
    tp = {
        "leg": "tp",
        "kind": "sell",
        "status": "done",
        "status_reason": "sold 1 USDC for 0.0004 ETH at $1",
    }
    sl = {"leg": "sl", "kind": "sell", "status": "stopped", "status_reason": "user"}
    assert triggers.bracket_status([tp, sl]) == (
        "done",
        "take-profit: sold 1 USDC for 0.0004 ETH at $1 · stop-loss stopped by you",
    )
    # The ordinary one-cancels-the-other ending stays short.
    sl["status_reason"] = "take-profit filled"
    assert triggers.bracket_status([tp, sl]) == (
        "done",
        "take-profit: sold 1 USDC for 0.0004 ETH at $1",
    )
