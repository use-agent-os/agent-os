"""Price triggers (``docs/triggers.md``): the pure helpers, the checker, the ledger and the card.

Offline and on a fake clock: ``service._now`` is pinned and moved by hand, the
chain, the swap provider and the price feed are the shared fakes, and a tick
is driven by calling :meth:`TradingService.trigger_check` with WETH's spot
price set on :class:`FakePrices`. The card fixtures under
``tests/fixtures/trigger_cards/`` are the frontend's renderer fixtures; they
are built by :func:`_card_world` and pinned against it here. Regenerate them
with::

    AGENTOS_REGEN_TRIGGER_FIXTURES=1 uv run pytest tests/test_trading/test_triggers.py -k fixture
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from agentos.trading import service as service_module
from agentos.trading import triggers
from agentos.trading.chains import BASE, NATIVE_ADDRESS, ROBINHOOD
from agentos.trading.dca import iso
from agentos.trading.ledger import SCHEMA_VERSION, Ledger
from agentos.trading.service import TradingError, TradingService
from tests.test_trading.conftest import PASSWORD
from tests.test_trading.fakes import (
    ROUTER,
    SWAP_TARGETS,
    USDC,
    WETH,
    FakeAggregator,
    FakeChain,
    FakePrices,
    decode_fake_raw,
    transfer_log,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "trigger_cards"
FIXTURE_NAMES = (
    "trigger-armed",
    "trigger-awaiting",
    "trigger-done",
    "trigger-alert",
    "triggers",
    "triggers-empty",
)
T0 = 1_789_894_800.0  # 2026-09-20T09:00:00Z
TICK = 30.0
HOUR = 3_600
DAY = 86_400
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
    """Let every background confirm task finish (``wait=False`` orders)."""
    for _ in range(50):
        tasks = list(service._confirm_tasks)
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)


class World:
    """A service on a pinned clock with one wallet holding ETH, WETH and USDC on Base.

    Every quote is priced at WETH's spot price (USDC at $1), and every swap
    that lands moves exactly what was quoted: a sell debits WETH and credits
    USDC, a buy the other way round.
    """

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
        self.last_quote: dict[str, Any] = {}
        self.set_price(2000.0)
        self._wire()

    def set_price(self, price: float | None) -> None:
        """WETH's spot price (``None``: the feed has none)."""
        if price is None:
            self.prices.spot.pop(("base", WETH), None)
        else:
            self.prices.spot[("base", WETH)] = price

    def weth(self) -> int:
        return self.chain.get_erc20(WETH, self.wallet)

    def usdc(self) -> int:
        return self.chain.get_erc20(USDC, self.wallet)

    def _wire(self) -> None:
        aggregator, chain, world = self.aggregator, self.chain, self
        original = aggregator.handle

        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/quote":
                params = dict(request.url.params)
                amount = int(params["sellAmountRaw"])
                token_in = str(params["sell"]).lower()
                price = world.prices.spot.get(("base", WETH)) or 2000.0
                if token_in == WETH:  # 18 decimals of WETH → 6 of USDC
                    out = int(amount * price) // 10**12
                else:
                    out = int(amount * 10**12 / price)
                aggregator.amount_out = out
                world.last_quote = {
                    "in": token_in,
                    "out": str(params["buy"]).lower(),
                    "amount": amount,
                    "received": out,
                }
            return original(request)

        aggregator.handle = handle  # type: ignore[method-assign]

        def on_send(raw: str) -> str:
            tx = decode_fake_raw(raw)
            chain._seq += 1
            tx_hash = "0x" + format(0xFEED00 + chain._seq, "x").rjust(64, "0")
            gas_wei = 100_000 * 10**8
            wallet = world.wallet.lower()
            chain.nonces[wallet] = tx.get("nonce", 0) + 1
            chain.native[wallet] -= gas_wei
            if tx["to"].lower() in SWAP_TARGETS:
                q = world.last_quote
                chain.set_erc20(q["in"], wallet, chain.get_erc20(q["in"], wallet) - q["amount"])
                chain.set_erc20(q["out"], wallet, chain.get_erc20(q["out"], wallet) + q["received"])
                logs = [
                    transfer_log(
                        tx_hash=tx_hash,
                        log_index=3,
                        block=chain.block,
                        token=q["out"],
                        sender=ROUTER,
                        recipient=wallet,
                        amount=q["received"],
                    )
                ]
                chain.receipt(tx_hash, status=1, logs=logs)
            else:
                chain.receipt(tx_hash, status=1)
            return tx_hash

        chain.on_send = on_send

    def at(self, ts: float) -> None:
        self.clock.now = ts

    async def create(self, **overrides: Any) -> dict[str, Any]:
        params: dict[str, Any] = {
            "chain": BASE,
            "kind": "sell",
            "token": "WETH",
            "direction": "below",
            "price": "1900",
            "amount_pct": 50,
            "initiator": "manual",
        }
        params.update(overrides)
        return await self.service.trigger_create(**params)

    async def check(self, price: float | None = ..., *, step: float = TICK) -> None:  # type: ignore[assignment]
        """One tick ``step`` seconds on, at ``price`` (``...``: the price as it is)."""
        self.clock.now += step
        if price is not ...:
            self.set_price(price)
        await self.service.trigger_check()
        await _settle(self.service)

    def row(self, trigger_id: str) -> dict[str, Any]:
        row = self.service.ledger.get_trigger(trigger_id)
        assert row is not None
        return row

    def fires(self, trigger_id: str) -> list[dict[str, Any]]:
        return self.service.ledger.list_fires(trigger_id)


@pytest.fixture
def ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stable trigger and order ids, so payloads can be pinned."""
    numbers = itertools.count(1)
    orders = itertools.count(1)
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
    base_chain.set_erc20(WETH, wallet, 10**17)  # 0.1 WETH
    return World(service, base_chain, fake_aggregator, fake_prices, clock, wallet)


# ── pure helpers ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kind", "direction", "price", "trail", "name"),
    [
        ("sell", "below", 3800, None, "Stop-loss ETH"),
        ("sell", "above", 4200, None, "Take-profit ETH"),
        ("sell", "trail", None, 10, "Trailing stop ETH"),
        ("buy", "below", 3500, None, "Buy ETH under $3,500"),
        ("buy", "above", 4200, None, "Buy ETH over $4,200"),
        ("alert", "below", 3800, None, "Alert ETH under $3,800"),
        ("alert", "above", 5000, None, "Alert ETH over $5,000"),
        ("alert", "trail", None, 10, "Alert ETH −10 % from peak"),
        ("alert", "trail", None, 7.5, "Alert ETH −7.5 % from peak"),
        ("buy", "below", 0.00123, None, "Buy ETH under $0.00123"),
    ],
)
def test_default_names(
    kind: str, direction: str, price: float | None, trail: float | None, name: str
) -> None:
    assert triggers.default_name(kind, direction, "ETH", price, trail) == name


def test_relative_prices_resolve_against_the_price_now() -> None:
    assert triggers.parse_price("3800", "below", None) == (3800.0, None)
    assert triggers.parse_price("$3,800.5", "above", None) == (3800.5, None)
    assert triggers.parse_price(4000, "above", 1.0) == (4000.0, None)
    assert triggers.parse_price("-10%", "below", 2000.0) == (1800.0, 2000.0)
    assert triggers.parse_price("10%", "below", 2000.0) == (1800.0, 2000.0)
    assert triggers.parse_price("+15%", "above", 2000.0) == (2300.0, 2000.0)
    assert triggers.parse_price("15 %", "above", 2000.0) == (2300.0, 2000.0)
    refusals = [
        (("-10%", "below", None), "ETH has no price right now: give an absolute price"),
        (("+10%", "below", 2000.0), "use -10%"),
        (("-10%", "above", 2000.0), "use +15%"),
        (("100%", "below", 2000.0), "under 100 %"),
        (("0%", "above", 2000.0), "must not be 0 %"),
        (("0", "below", 2000.0), "greater than zero"),
        (("abc", "below", 2000.0), "a number or a percent"),
        (("3800", "trail", 2000.0), "trail takes trailPct"),
    ]
    for (text, direction, current), message in refusals:
        with pytest.raises(ValueError, match=message.replace("+", r"\+").replace("$", r"\$")):
            triggers.parse_price(text, direction, current, symbol="ETH")


def test_conditions_and_distance() -> None:
    assert triggers.evaluate("below", None, 3800, None, None) is None
    assert triggers.evaluate("below", 3800, 3800, None, None) is True
    assert triggers.evaluate("below", 3801, 3800, None, None) is False
    assert triggers.evaluate("above", 5000, 5000, None, None) is True
    assert triggers.evaluate("above", 4999, 5000, None, None) is False
    assert triggers.evaluate("trail", 1800, None, 10, None) is False  # no peak yet
    assert triggers.evaluate("trail", 1800, None, 10, 2000) is True
    assert triggers.evaluate("trail", 1801, None, 10, 2000) is False
    assert triggers.stop_price(2000, 10) == pytest.approx(1800)
    assert triggers.distance_pct("below", 4000, 3800, None, None) == pytest.approx(-5.0)
    assert triggers.distance_pct("above", 4000, 5000, None, None) == pytest.approx(25.0)
    assert triggers.distance_pct("below", 3700, 3800, None, None) == 0.0
    assert triggers.distance_pct("trail", 1900, None, 10, 2000) == pytest.approx(-100 / 19)
    assert triggers.distance_pct("trail", 1900, None, 10, None) is None
    assert triggers.distance_pct("below", None, 3800, None, None) is None
    assert triggers.condition_label("below", 3800, None) == "under $3,800"
    assert triggers.condition_label("above", 5000, None) == "over $5,000"
    assert triggers.condition_label("trail", None, 10) == "10 % below peak"
    assert triggers.action_label("sell", "ETH", "USDC", amount_pct=50) == "sell 50 % of ETH → USDC"
    assert triggers.action_label("sell", "ETH", "USDC", amount_human="0.05") == (
        "sell 0.05 ETH → USDC"
    )
    assert triggers.action_label("buy", "ETH", "USDC", amount_usd=50) == "buy $50 of ETH with USDC"
    assert triggers.action_label("alert", "ETH", "USDC") == "notify"
    assert triggers.fire_note("Stop-loss ETH", 3790) == "Stop-loss ETH · fired at $3,790"
    assert triggers.filled_reason(
        "sell",
        token_symbol="ETH",
        token_human="0.05",
        quote_symbol="USDC",
        quote_human="189.4",
        price=3788,
    ) == ("sold 0.05 ETH for 189.4 USDC at $3,788")
    assert triggers.not_reached_reason(T0) == "not reached by 2026-09-20"
    assert triggers.price_text(0.5) == "$0.50" and triggers.price_text(0.9998) == "$0.9998"
    assert triggers.new_trigger_id().startswith("trg_") and len(triggers.new_trigger_id()) == 12


@pytest.mark.parametrize(
    ("terms", "problem"),
    [
        ({"kind": "swap"}, "kind must be"),
        ({"direction": "sideways"}, "direction must be"),
        ({"kind": "buy", "direction": "trail", "price": None, "trail_pct": 10}, "cannot trail"),
        ({"direction": "trail", "price": None}, "trailPct is required"),
        ({"direction": "trail", "price": None, "trail_pct": 100}, "under 100"),
        ({"direction": "trail", "price": None, "trail_pct": 0}, "above 0"),
        ({"direction": "trail", "trail_pct": 10}, "price does not go with trail"),
        ({"trail_pct": 10}, "trailPct only goes with trail"),
        ({"price": None}, "price is required for below"),
        ({"price": "-3"}, "greater than zero"),
        ({"amount_pct": None}, "a sell needs one size"),
        ({"amount_usd": 10}, "a sell takes one size"),
        ({"amount_pct": 0}, "amountPct must be above 0"),
        ({"amount_pct": 101}, "at most 100"),
        ({"amount_pct": None, "amount": "0"}, "amount must be greater than zero"),
        ({"amount_pct": None, "amount": "x"}, "amount must be a number"),
        ({"amount_pct": None, "amount_usd": -1}, "amountUsd must be greater than zero"),
        ({"kind": "alert"}, "an alert takes no size"),
        ({"kind": "buy"}, "amountUsd only"),
        ({"kind": "buy", "amount_pct": None}, "a buy needs amountUsd"),
        ({"valid_for_seconds": 59}, "at least 60"),
    ],
)
def test_validate_terms(terms: dict[str, Any], problem: str) -> None:
    base: dict[str, Any] = {
        "kind": "sell",
        "direction": "below",
        "price": "3800",
        "trail_pct": None,
        "amount_usd": None,
        "amount_pct": 50,
        "amount": None,
        "valid_for_seconds": None,
    }
    found = triggers.validate_terms(**{**base, **terms})
    assert found is not None and problem in found, found
    assert triggers.validate_terms(**base) is None


# ── create / approve ───────────────────────────────────────────────────────


async def test_operator_create_arms_and_agent_create_parks(world: World) -> None:
    armed = await world.create()
    trigger = armed["trigger"]
    assert armed["kind"] == "trigger" and armed["version"] == 1
    assert armed["request"] == {"kind": "get", "params": {"triggerId": trigger["id"]}}
    assert trigger["status"] == "armed" and trigger["initiator"] == "manual"
    assert trigger["name"] == "Stop-loss WETH"
    assert trigger["armedAt"] == iso(T0) and trigger["approvedAt"] == iso(T0)
    assert trigger["market"]["armedPriceUsd"] == 2000.0 and trigger["expiresAt"] is None
    assert trigger["condition"]["label"] == "under $1,900"
    assert trigger["condition"]["hits"] == 0 and trigger["condition"]["confirmTicks"] == 2
    assert trigger["market"]["distancePct"] == pytest.approx(-5.0)
    assert trigger["market"]["balance"]["human"] == "0.1"
    assert trigger["action"]["estimatedUsd"] == pytest.approx(100.0)  # 50 % of 0.1 × $2,000
    assert trigger["action"]["needsApproval"] is False
    assert trigger["action"]["label"] == "sell 50 % of WETH → USDC"
    assert trigger["quote"]["symbol"] == "USDC" and trigger["token"]["priceUsd"] == 2000.0
    assert trigger["wallet"] == {"address": world.wallet, "label": "Key main", "inApp": True}

    pending = await world.create(
        kind="buy", amount_pct=None, amount_usd=50, initiator="agent", session_key="agent:desk:x"
    )
    proposal = pending["trigger"]
    assert proposal["status"] == "awaiting_approval" and proposal["sessionKey"] == "agent:desk:x"
    assert proposal["armedAt"] is None and proposal["expiresAt"] == iso(T0 + triggers.PENDING_TTL)
    assert proposal["name"] == "Buy WETH under $1,900"
    assert proposal["market"]["balance"]["human"] == "1000"  # the quote it spends
    # A proposal is never checked, however low the price goes.
    await world.check(1800)
    await world.check(1800)
    assert world.fires(proposal["id"]) == []
    assert world.row(proposal["id"])["hits"] == 0

    changed = _events(world.service, "trading.trigger.changed")
    assert [e["trigger"]["id"] for e in changed][:2] == [trigger["id"], proposal["id"]]
    assert {"reason": "trigger", "triggerId": proposal["id"]}.items() <= next(
        e for e in _events(world.service, "trading.changed") if e.get("triggerId") == proposal["id"]
    ).items()


async def test_approve_arms_at_the_price_then(world: World) -> None:
    proposal = (await world.create(initiator="agent", direction="trail", price=None, trail_pct=10))[
        "trigger"
    ]
    assert proposal["condition"]["peakPriceUsd"] is None
    world.at(T0 + 600)
    world.set_price(2100.0)
    approved = (await world.service.trigger_approve(proposal["id"]))["trigger"]
    assert approved["status"] == "armed" and approved["approvedAt"] == iso(T0 + 600)
    assert approved["armedAt"] == iso(T0 + 600) and approved["expiresAt"] is None
    assert approved["market"]["armedPriceUsd"] == 2100.0
    assert approved["condition"]["peakPriceUsd"] == 2100.0
    assert approved["condition"]["stopPriceUsd"] == pytest.approx(1890.0)
    with pytest.raises(TradingError) as err:
        await world.service.trigger_approve(proposal["id"])
    assert err.value.code == "trading.trigger.bad_state"


async def test_pending_trigger_expires_after_a_day(world: World) -> None:
    proposal = (await world.create(initiator="agent"))["trigger"]
    world.at(T0 + triggers.PENDING_TTL + 1)
    await world.check(step=0)
    row = world.row(proposal["id"])
    assert row["status"] == "expired" and row["status_reason"] == "no decision within 24 h"
    with pytest.raises(TradingError) as err:
        await world.service.trigger_approve(proposal["id"])
    assert err.value.code == "trading.trigger.bad_state"


async def test_relative_price_at_creation_and_refusal_without_a_price(world: World) -> None:
    below = (await world.create(price="-10%"))["trigger"]
    assert below["condition"]["priceUsd"] == 1800.0
    assert below["condition"]["fromPriceUsd"] == 2000.0
    above = (await world.create(direction="above", price="+15%"))["trigger"]
    assert above["condition"]["priceUsd"] == 2300.0 and above["name"] == "Take-profit WETH"
    world.set_price(None)
    with pytest.raises(TradingError) as err:
        await world.create(price="-10%")
    assert err.value.code == "trading.trigger.invalid"
    assert str(err.value) == "WETH has no price right now: give an absolute price"
    # An absolute price still arms without a price, and says why it waits.
    blind = await world.create(price="1900")
    assert blind["trigger"]["status"] == "armed"
    assert blind["trigger"]["market"]["armedPriceUsd"] is None
    assert "price unknown: the trigger waits until WETH has a price" in blind["warnings"]


async def test_create_validation(world: World) -> None:
    cases: list[tuple[dict[str, Any], str]] = [
        ({"kind": "swap"}, "trading.trigger.invalid"),
        ({"direction": "trail", "price": None, "trail_pct": 120}, "trading.trigger.invalid"),
        (
            {"kind": "buy", "direction": "trail", "price": None, "trail_pct": 5},
            "trading.trigger.invalid",
        ),
        ({"amount_pct": None}, "trading.trigger.invalid"),
        ({"amount_usd": 10}, "trading.trigger.invalid"),
        ({"kind": "alert"}, "trading.trigger.invalid"),
        ({"price": "0"}, "trading.trigger.invalid"),
        ({"valid_for_seconds": 30}, "trading.trigger.invalid"),
        ({"token": "USDC", "quote": "USDC"}, "trading.trigger.invalid"),  # given quote == token
        ({"wallet": "all"}, "trading.trigger.invalid"),
        ({"slippage_pct": 50}, "trading.trigger.invalid"),
        ({"token": "NOPE"}, "trading.token_not_found"),
        ({"chain": ROBINHOOD, "token": "AAPL"}, "trading.trigger.invalid"),  # no canonical USDC
        ({"amount_pct": None, "amount": "0.0000000000000000001"}, "trading.trigger.invalid"),
        ({"initiator": "robot"}, "trading.invalid"),
    ]
    for overrides, code in cases:
        with pytest.raises(TradingError) as err:
            await world.create(**overrides)
        assert err.value.code == code, (overrides, err.value)
    with pytest.raises(TradingError, match="quote is required on Robinhood"):
        await world.create(chain=ROBINHOOD, token="AAPL")
    # An alert never trades: watching USDC itself falls back to the native coin
    # as its counter, and Robinhood Chain needs no quote for it.
    on_usdc = (await world.create(kind="alert", token="USDC", amount_pct=None))["trigger"]
    assert on_usdc["token"]["symbol"] == "USDC" and on_usdc["quote"]["symbol"] == "ETH"
    on_rh = await world.create(kind="alert", chain=ROBINHOOD, token="AAPL", amount_pct=None)
    assert on_rh["trigger"]["status"] == "armed" and on_rh["trigger"]["quote"]["symbol"] == "ETH"
    # "Sell all my USDC" with no quote: the default quote (USDC) is the token
    # itself, so the trigger sells it for the native coin.
    all_usdc = (await world.create(token="USDC", direction="above", price="0.5", amount_pct=100))[
        "trigger"
    ]
    assert all_usdc["token"]["symbol"] == "USDC" and all_usdc["quote"]["symbol"] == "ETH"
    assert all_usdc["quote"]["address"] == NATIVE_ADDRESS
    assert all_usdc["action"]["label"] == "sell 100 % of USDC → ETH"
    buy_usdc = (
        await world.create(
            kind="buy", token="USDC", direction="below", price="2", amount_pct=None, amount_usd=5
        )
    )["trigger"]
    assert buy_usdc["quote"]["symbol"] == "ETH"
    named = (await world.create(name="  My   stop ", amount_pct=None, amount="0.05"))["trigger"]
    assert named["name"] == "My stop" and named["action"]["amount"]["human"] == "0.05"
    assert named["action"]["label"] == "sell 0.05 WETH → USDC"
    assert named["action"]["estimatedUsd"] == pytest.approx(100.0)
    world.service.config.enabled = False
    with pytest.raises(TradingError) as err:
        await world.create()
    assert err.value.code == "trading.disabled"


async def test_creation_warnings(world: World) -> None:
    already = await world.create(price="2100", amount_pct=100)
    assert already["warnings"][:2] == [
        "WETH is already at $2,000, under $2,100: this fires after the next two checks",
        "a sell of ≈$200 is above the $100 approval threshold and will wait for you when it fires",
    ]
    assert already["trigger"]["action"]["needsApproval"] is True
    world.chain.set_erc20(WETH, world.wallet, 0)
    empty = await world.create()
    assert "Key main holds no WETH" in empty["warnings"]


async def test_not_found(world: World) -> None:
    for call in (
        world.service.trigger_get("trg_00000000"),
        world.service.trigger_pause("trg_00000000"),
        world.service.trigger_fire_now("trg_00000000"),
    ):
        with pytest.raises(TradingError) as err:
            await call
        assert err.value.code == "trading.trigger.not_found"


# ── checking ───────────────────────────────────────────────────────────────


async def test_two_checks_confirm_and_a_miss_resets(world: World) -> None:
    trigger_id = (await world.create(kind="alert", amount_pct=None))["trigger"]["id"]
    await world.check(1890)
    row = world.row(trigger_id)
    assert row["hits"] == 1 and row["last_price_usd"] == 1890.0
    assert row["last_checked_at"] == T0 + TICK
    await world.check(1910)  # one miss: back to zero
    assert world.row(trigger_id)["hits"] == 0
    await world.check(1890)
    assert world.row(trigger_id)["hits"] == 1 and world.fires(trigger_id) == []
    await world.check(1880)
    assert world.row(trigger_id)["status"] == "done"
    assert [f["status"] for f in world.fires(trigger_id)] == ["alerted"]


async def test_unknown_price_neither_fires_nor_resets(world: World) -> None:
    trigger_id = (await world.create(kind="alert", amount_pct=None))["trigger"]["id"]
    await world.check(1890)
    before = world.row(trigger_id)
    await world.check(None)
    after = world.row(trigger_id)
    assert after["hits"] == 1 and after["status"] == "armed"
    assert after["last_checked_at"] == before["last_checked_at"]  # nothing was read
    await world.check(None)
    assert world.fires(trigger_id) == []
    await world.check(1885)
    assert world.row(trigger_id)["status"] == "done"


async def test_above_condition(world: World) -> None:
    trigger_id = (
        await world.create(kind="alert", direction="above", price="2100", amount_pct=None)
    )["trigger"]["id"]
    await world.check(2099)
    assert world.row(trigger_id)["hits"] == 0
    await world.check(2100)
    await world.check(2150)
    assert world.row(trigger_id)["status"] == "done"


async def test_trail_tracks_the_peak_and_fires_off_it(world: World) -> None:
    trigger_id = (
        await world.create(
            kind="alert", direction="trail", price=None, trail_pct=10, amount_pct=None
        )
    )["trigger"]["id"]
    assert world.row(trigger_id)["peak_price_usd"] == 2000.0
    await world.check(2100)
    await world.check(2200)
    assert world.row(trigger_id)["peak_price_usd"] == 2200.0
    await world.check(2150)  # a dip does not lower the peak
    await world.check(1990)  # 1990 > 2200 × 0.9 = 1980: not yet
    row = world.row(trigger_id)
    assert row["peak_price_usd"] == 2200.0 and row["hits"] == 0
    card = (await world.service.trigger_get(trigger_id))["trigger"]
    assert card["condition"]["stopPriceUsd"] == pytest.approx(1980.0)
    assert card["condition"]["label"] == "10 % below peak"
    await world.check(1975)
    assert world.row(trigger_id)["hits"] == 1
    await world.check(1960)
    row = world.row(trigger_id)
    assert row["status"] == "done" and row["status_reason"] == "alerted at $1,960"


async def test_alert_fires_once_and_is_done(world: World) -> None:
    trigger_id = (
        await world.create(kind="alert", direction="above", price="5000", amount_pct=None)
    )["trigger"]["id"]
    await world.check(5010)
    await world.check(5100)
    await world.check(5200)
    await world.check(5300)
    fired = _events(world.service, "trading.trigger.fired")
    assert len(fired) == 1 and fired[0]["triggerId"] == trigger_id
    assert fired[0]["fire"]["status"] == "alerted" and fired[0]["fire"]["priceUsd"] == 5100.0
    assert fired[0]["trigger"]["status"] == "done"
    card = (await world.service.trigger_get(trigger_id))["trigger"]
    assert card["status"] == "done" and card["statusReason"] == "alerted at $5,100"
    assert card["result"] is None and card["market"]["balance"] is None
    assert card["triggeredAt"] == iso(T0 + 2 * TICK)
    assert world.service.ledger.orders_for_trigger(trigger_id) == []


# ── firing ─────────────────────────────────────────────────────────────────


async def test_sell_by_pct_sizes_from_the_balance_at_fire_time(world: World) -> None:
    trigger_id = (await world.create())["trigger"]["id"]
    world.chain.set_erc20(WETH, world.wallet, 4 * 10**16)  # 0.04 WETH by the time it fires
    await world.check(1890)
    await world.check(1880)
    orders = world.service.ledger.orders_for_trigger(trigger_id)
    assert len(orders) == 1
    order = world.service.get_order(orders[0]["order_id"])
    assert order["triggerId"] == trigger_id and order["mandateId"] is None
    assert order["amountIn"] == "0.02" and order["initiator"] == "agent"
    assert order["note"] == "Stop-loss WETH · fired at $1,880"
    assert order["status"] == "confirmed"
    assert world.weth() == 2 * 10**16 and world.usdc() == 1_000 * 10**6 + 37_600_000
    card = (await world.service.trigger_get(trigger_id))["trigger"]
    assert card["status"] == "done"
    assert card["statusReason"] == "sold 0.02 WETH for 37.6 USDC at $1,880"
    fire = card["fires"][0]
    assert fire["status"] == "filled" and fire["orderId"] == order["orderId"]
    assert fire["txHash"] and fire["explorerUrl"].startswith("https://basescan.org/tx/")
    result = card["result"]
    assert result["orderId"] == order["orderId"] and result["txHash"] == fire["txHash"]
    assert result["amountIn"]["human"] == "0.02" and result["amountOut"]["human"] == "37.6"
    assert result["priceUsd"] == pytest.approx(1880.0) and result["gasUsd"] is not None
    fired = _events(world.service, "trading.trigger.fired")
    assert len(fired) == 1 and fired[0]["fire"]["orderId"] == order["orderId"]


async def test_result_gas_counts_the_approval_and_the_card_shows_what_moved(
    world: World,
) -> None:
    # The sell needs an ERC-20 approve first: its gas is the order's too.
    world.aggregator.approval_needed = True
    world.chain.set_erc20(WETH, world.wallet, 5 * 10**16)  # 0.05 WETH: under the threshold
    trigger_id = (await world.create(amount_pct=100))["trigger"]["id"]
    await world.check(1890)
    await world.check(1880)
    (order_row,) = world.service.ledger.orders_for_trigger(trigger_id)
    order_id = str(order_row["order_id"])
    assert order_row["status"] == "confirmed" and order_row["approval_tx_hash"]
    entries = [
        e
        for e in world.service.ledger.list_entries(wallet=world.wallet)
        if e.get("order_id") == order_id
    ]
    assert sorted(e["kind"] for e in entries) == ["approval", "swap"]
    booked = sum(float(e["gas_usd"]) for e in entries)
    assert world.service.ledger.order_gas_usd([order_id])[order_id] == pytest.approx(booked)
    card = (await world.service.trigger_get(trigger_id))["trigger"]
    assert card["status"] == "done"
    assert card["result"]["gasUsd"] == pytest.approx(booked)
    # A done card says what was sold, not what the (now empty) wallet holds.
    assert world.weth() == 0
    assert card["result"]["amountIn"]["human"] == "0.05"
    assert card["action"]["estimatedUsd"] == pytest.approx(94.0)
    assert card["action"]["estimatedUsd"] == card["result"]["amountIn"]["usd"]
    assert card["market"]["balance"] is None and card["market"]["distancePct"] is None


async def test_a_stopped_card_has_no_balance(world: World) -> None:
    trigger_id = (await world.create())["trigger"]["id"]
    live = (await world.service.trigger_get(trigger_id))["trigger"]
    assert live["market"]["balance"]["human"] == "0.1"
    assert live["action"]["estimatedUsd"] == pytest.approx(100.0)
    stopped = (await world.service.trigger_stop(trigger_id))["trigger"]
    assert stopped["status"] == "stopped" and stopped["result"] is None
    assert stopped["market"]["balance"] is None


async def test_buy_by_usd(world: World) -> None:
    trigger_id = (await world.create(kind="buy", amount_pct=None, amount_usd=50))["trigger"]["id"]
    await world.check(1850)
    await world.check(1850)
    (order_row,) = world.service.ledger.orders_for_trigger(trigger_id)
    assert order_row["token_in"] == USDC and order_row["token_out"] == WETH
    assert order_row["amount_raw"] == str(50 * 10**6) and order_row["status"] == "confirmed"
    row = world.row(trigger_id)
    assert row["status"] == "done"
    assert row["status_reason"].startswith("bought 0.027027 WETH for 50 USDC at $1,85")
    assert world.usdc() == 950 * 10**6


async def test_sell_by_fixed_amount_and_by_usd(world: World) -> None:
    fixed = (await world.create(amount_pct=None, amount="0.03"))["trigger"]["id"]
    usd = (await world.create(amount_pct=None, amount_usd=19))["trigger"]["id"]
    await world.check(1900)
    await world.check(1900)
    by_id = {
        r["trigger_id"]: r["amount_raw"]
        for t in (fixed, usd)
        for r in world.service.ledger.orders_for_trigger(t)
    }
    assert by_id == {fixed: str(3 * 10**16), usd: str(10**16)}  # $19 at $1,900 = 0.01 WETH
    assert world.row(fixed)["status"] == world.row(usd)["status"] == "done"


async def test_insufficient_balance_pauses_at_once(world: World) -> None:
    world.chain.set_erc20(WETH, world.wallet, 0)
    sell_id = (await world.create())["trigger"]["id"]
    world.chain.set_erc20(USDC, world.wallet, 20 * 10**6)
    buy_id = (await world.create(kind="buy", amount_pct=None, amount_usd=50))["trigger"]["id"]
    await world.check(1890)
    await world.check(1890)
    sell = world.row(sell_id)
    assert sell["status"] == "paused" and sell["status_reason"] == "paused: nothing to sell"
    assert world.fires(sell_id)[0]["status"] == "skipped"
    assert world.fires(sell_id)[0]["reason"] == "insufficient_balance: Key main holds no WETH"
    buy = world.row(buy_id)
    assert buy["status"] == "paused" and buy["status_reason"] == "paused: insufficient USDC"
    assert world.fires(buy_id)[0]["reason"].startswith(
        "insufficient_balance: Key main holds 20 USDC"
    )
    assert world.service.ledger.orders_for_trigger(sell_id) == []
    assert world.service.ledger.orders_for_trigger(buy_id) == []
    # Paused: nothing more is tried.
    await world.check(1890)
    await world.check(1890)
    assert len(world.fires(sell_id)) == 1
    fired = _events(world.service, "trading.trigger.fired")
    assert {f["fire"]["status"] for f in fired} == {"skipped"} and len(fired) == 2


async def test_failed_fire_re_arms_and_three_pause(world: World) -> None:
    world.aggregator.quote_error = NO_ROUTE
    trigger_id = (await world.create())["trigger"]["id"]
    for attempt in (1, 2):
        await world.check(1890)
        await world.check(1890)
        row = world.row(trigger_id)
        assert row["status"] == "armed" and row["hits"] == 0, attempt
        assert row["bad_streak"] == attempt and row["fires_failed"] == attempt
    fire = world.fires(trigger_id)[0]
    assert fire["status"] == "failed" and fire["reason"].startswith("trading.no_route")
    await world.check(1890)  # one check is not a fire: it must re-confirm
    assert len(world.fires(trigger_id)) == 2
    await world.check(1890)
    row = world.row(trigger_id)
    assert row["status"] == "paused" and row["bad_streak"] == 3
    assert row["status_reason"] == "paused after 3 failed fires: trading.no_route"
    card = (await world.service.trigger_get(trigger_id))["trigger"]
    assert [f["status"] for f in card["fires"]] == ["failed"] * 3
    assert card["fires"][0]["reasonCode"] == "trading.no_route"
    assert [f["n"] for f in card["fires"]] == [3, 2, 1]


async def test_resume_resets_hits_streak_and_peak(world: World) -> None:
    world.aggregator.quote_error = NO_ROUTE
    trigger_id = (await world.create(direction="trail", price=None, trail_pct=10, amount_pct=100))[
        "trigger"
    ]["id"]
    await world.check(2500)
    for _ in range(3):
        await world.check(2200)
        await world.check(2200)
    row = world.row(trigger_id)
    assert row["status"] == "paused" and row["peak_price_usd"] == 2500.0
    # Paused through a rally and back: the old peak would fire at once.
    world.aggregator.quote_error = None
    world.set_price(2300.0)
    resumed = (await world.service.trigger_resume(trigger_id))["trigger"]
    assert resumed["status"] == "armed" and resumed["statusReason"] is None
    assert resumed["condition"]["peakPriceUsd"] == 2300.0 and resumed["condition"]["hits"] == 0
    row = world.row(trigger_id)
    assert row["bad_streak"] == 0 and row["fires_failed"] == 3
    await world.check(2200)
    await world.check(2200)
    assert world.row(trigger_id)["status"] == "armed"


async def test_pause_and_resume_a_below_trigger(world: World) -> None:
    trigger_id = (await world.create())["trigger"]["id"]
    await world.check(1890)
    paused = (await world.service.trigger_pause(trigger_id))["trigger"]
    assert paused["status"] == "paused" and paused["statusReason"] == "user"
    await world.check(1880)
    await world.check(1880)
    assert world.fires(trigger_id) == [] and world.row(trigger_id)["hits"] == 1
    resumed = (await world.service.trigger_resume(trigger_id))["trigger"]
    assert resumed["condition"]["hits"] == 0 and resumed["condition"]["peakPriceUsd"] is None


# ── parked orders ──────────────────────────────────────────────────────────


async def _parked(world: World) -> str:
    """A sell over the approval threshold that fired and waits for the user."""
    trigger_id = (await world.create(amount_pct=100))["trigger"]["id"]
    await world.check(1890)
    await world.check(1890)
    assert world.fires(trigger_id)[0]["status"] == "parked"
    assert world.row(trigger_id)["status"] == "triggered"
    return trigger_id


async def test_parked_order_confirmed_is_done(world: World) -> None:
    trigger_id = await _parked(world)
    assert _events(world.service, "trading.approval.requested")
    fired = _events(world.service, "trading.trigger.fired")
    assert len(fired) == 1 and fired[0]["fire"]["status"] == "parked"
    assert fired[0]["fire"]["reasonCode"] == "needs_approval"
    # A triggered trigger is not checked again while its order waits.
    await world.check(1890)
    await world.check(1890)
    assert len(world.fires(trigger_id)) == 1
    order_id = world.fires(trigger_id)[0]["order_id"]
    await world.service.approve(order_id, wait=True)
    row = world.row(trigger_id)
    assert row["status"] == "done"
    assert row["status_reason"].startswith("sold 0.1 WETH for 189 USDC at $1,890")
    assert len(_events(world.service, "trading.trigger.fired")) == 1


async def test_parked_order_rejected_stops_the_trigger(world: World) -> None:
    trigger_id = await _parked(world)
    await world.service.reject(world.fires(trigger_id)[0]["order_id"], "not now")
    row = world.row(trigger_id)
    assert row["status"] == "stopped" and row["status_reason"] == "sell rejected by you"
    assert world.fires(trigger_id)[0]["status"] == "rejected"


async def test_parked_order_expired_pauses_the_trigger(world: World) -> None:
    trigger_id = await _parked(world)
    world.at(world.clock.now + 901)  # past the 900 s approval TTL
    await world.service.expire_orders()
    row = world.row(trigger_id)
    assert row["status"] == "paused"
    assert row["status_reason"] == "the sell waited for approval and expired"
    assert world.fires(trigger_id)[0]["status"] == "expired"
    # It does not re-arm by itself.
    await world.check(1800)
    await world.check(1800)
    assert len(world.fires(trigger_id)) == 1


async def test_stop_rejects_a_parked_order(world: World) -> None:
    trigger_id = await _parked(world)
    order_id = world.fires(trigger_id)[0]["order_id"]
    stopped = (await world.service.trigger_stop(trigger_id, reason="changed my mind"))["trigger"]
    assert stopped["status"] == "stopped" and stopped["statusReason"] == "user: changed my mind"
    order = world.service.ledger.get_order(order_id)
    assert order is not None and order["status"] == "rejected"
    assert world.fires(trigger_id)[0]["status"] == "rejected"
    assert world.row(trigger_id)["status_reason"] == "user: changed my mind"


async def test_daily_cap_rejection_is_a_failed_fire(world: World) -> None:
    world.service.config.daily_cap_usd = 5.0
    trigger_id = (await world.create())["trigger"]["id"]
    await world.check(1890)
    await world.check(1890)
    fire = world.fires(trigger_id)[0]
    assert fire["status"] == "failed" and fire["reason"].startswith("daily_cap: daily cap 5.00")
    row = world.row(trigger_id)
    assert row["status"] == "armed" and row["bad_streak"] == 1


# ── state machine ──────────────────────────────────────────────────────────


async def test_valid_until_expiry(world: World) -> None:
    trigger_id = (await world.create(valid_for_seconds=HOUR))["trigger"]["id"]
    card = (await world.service.trigger_get(trigger_id))["trigger"]
    assert card["validUntil"] == iso(T0 + HOUR)
    paused_id = (await world.create(valid_for_seconds=HOUR))["trigger"]["id"]
    await world.service.trigger_pause(paused_id)
    world.at(T0 + HOUR)
    await world.check(1950, step=0)
    for tid in (trigger_id, paused_id):
        row = world.row(tid)
        assert row["status"] == "expired" and row["status_reason"] == "not reached by 2026-09-20"


async def test_fire_now(world: World) -> None:
    service = world.service
    trigger_id = (await world.create(amount_pct=10))["trigger"]["id"]
    answer = await service.trigger_fire_now(trigger_id, wait=True)  # at $2,000, far off the line
    assert answer["fire"]["manual"] is True and answer["fire"]["status"] == "filled"
    assert answer["fire"]["priceUsd"] == 2000.0 and answer["fire"]["txHash"]
    assert answer["trigger"]["status"] == "done"
    assert answer["trigger"]["statusReason"] == "sold 0.01 WETH for 20 USDC at $2,000"
    for call in (service.trigger_fire_now, service.trigger_pause, service.trigger_stop):
        with pytest.raises(TradingError) as err:
            await call(trigger_id)
        assert err.value.code == "trading.trigger.bad_state"
    # Paused: fire now works too, and an alert is notified at once.
    alert_id = (await world.create(kind="alert", amount_pct=None))["trigger"]["id"]
    await service.trigger_pause(alert_id)
    alerted = await service.trigger_fire_now(alert_id)
    assert alerted["fire"]["status"] == "alerted" and alerted["trigger"]["status"] == "done"
    # A proposal cannot be fired.
    proposal_id = (await world.create(initiator="agent"))["trigger"]["id"]
    with pytest.raises(TradingError) as err:
        await service.trigger_fire_now(proposal_id)
    assert err.value.code == "trading.trigger.bad_state"


async def test_a_failed_manual_fire_of_a_paused_trigger_stays_paused(world: World) -> None:
    trigger_id = (await world.create())["trigger"]["id"]
    await world.service.trigger_pause(trigger_id)
    world.aggregator.quote_error = NO_ROUTE
    answer = await world.service.trigger_fire_now(trigger_id)
    assert answer["fire"]["status"] == "failed"
    assert answer["trigger"]["status"] == "paused"
    assert answer["trigger"]["statusReason"] == "fire failed: trading.no_route"
    # From armed, a failed manual fire re-arms like any other.
    other = (await world.create())["trigger"]["id"]
    assert (await world.service.trigger_fire_now(other))["trigger"]["status"] == "armed"


async def test_state_machine_and_bad_state_errors(world: World) -> None:
    service = world.service
    trigger_id = (await world.create())["trigger"]["id"]
    for call in (service.trigger_approve, service.trigger_reject, service.trigger_resume):
        with pytest.raises(TradingError) as err:
            await call(trigger_id)
        assert err.value.code == "trading.trigger.bad_state"
    await service.trigger_pause(trigger_id)
    with pytest.raises(TradingError) as err:
        await service.trigger_pause(trigger_id)
    assert err.value.code == "trading.trigger.bad_state"
    stopped = (await service.trigger_stop(trigger_id))["trigger"]
    assert stopped["status"] == "stopped" and stopped["statusReason"] == "user"
    assert stopped["market"]["distancePct"] is None
    for call in (service.trigger_pause, service.trigger_resume, service.trigger_stop):
        with pytest.raises(TradingError) as err:
            await call(trigger_id)
        assert err.value.code == "trading.trigger.bad_state"
    pending_id = (await world.create(initiator="agent"))["trigger"]["id"]
    rejected = (await service.trigger_reject(pending_id, reason="too risky"))["trigger"]
    assert rejected["status"] == "rejected" and rejected["statusReason"] == "user: too risky"


async def test_list_filters_live_and_wallet(world: World) -> None:
    first = (await world.create())["trigger"]["id"]
    second = (await world.create(initiator="agent", name="Proposal"))["trigger"]["id"]
    third = (await world.create(name="Old"))["trigger"]["id"]
    await world.service.trigger_stop(third)
    live = await world.service.trigger_list()
    assert [t["id"] for t in live["triggers"]] == [second, first]
    assert live["request"] == {"kind": "list", "params": {"all": False}}
    assert live["totals"] == {"count": 2, "armed": 1, "awaiting": 1, "triggered": 0}
    everything = await world.service.trigger_list(all=True, wallet="Key main")
    assert [t["id"] for t in everything["triggers"]] == [second, first, third]
    assert everything["request"]["params"] == {"all": True, "wallet": "Key main"}


async def test_tick_checks_triggers_and_a_running_pass_is_not_doubled(world: World) -> None:
    trigger_id = (await world.create(kind="alert", amount_pct=None))["trigger"]["id"]
    world.set_price(1890.0)
    async with world.service._trigger_lock:
        await world.service.trigger_check()  # the pass in progress holds the lock: no-op
    assert world.row(trigger_id)["hits"] == 0
    await world.service.tick()
    assert world.row(trigger_id)["hits"] == 1


async def test_prices_are_read_once_per_chain_per_pass(world: World) -> None:
    for _ in range(3):
        await world.create(kind="alert", amount_pct=None)
    world.prices.requests.clear()
    await world.check(1950)
    reads = [r for r in world.prices.requests if r.url.path.startswith("/tokens/v1/base/")]
    assert len(reads) == 1


# ── restart safety ─────────────────────────────────────────────────────────


async def test_reconcile_settles_a_fire_whose_order_finished_unheard(world: World) -> None:
    trigger_id = await _parked(world)
    fire = world.fires(trigger_id)[0]
    # The order expires behind the engine's back (a restart ate the event).
    world.service.ledger.update_order(fire["order_id"], status="expired", reason="expired")
    await world.check(1890)
    assert world.service.ledger.get_fire(fire["fire_id"])["status"] == "expired"  # type: ignore[index]
    assert world.row(trigger_id)["status"] == "paused"


async def test_reconcile_writes_off_an_orphan_fire_and_unsticks_its_trigger(world: World) -> None:
    trigger_id = (await world.create())["trigger"]["id"]
    ledger = world.service.ledger
    # The process died after the claim and the fire row, before the order.
    ledger.update_trigger(trigger_id, now=T0, status="triggered", triggered_at=T0)
    fire_id = ledger.insert_fire(
        {"trigger_id": trigger_id, "n": 1, "at": T0, "status": "pending", "price_usd": 1890.0}
    )
    await world.check(1950)
    assert ledger.get_fire(fire_id)["status"] == "pending"  # type: ignore[index]
    world.at(T0 + triggers.ORPHAN_FIRE_S + 1)
    await world.check(1950, step=0)
    fire = ledger.get_fire(fire_id)
    assert fire is not None and fire["status"] == "failed"
    assert fire["reason"].startswith("trading.interrupted")
    row = world.row(trigger_id)
    assert row["status"] == "armed" and row["bad_streak"] == 1
    # A filled fire whose trigger never heard of it: done, not fired again.
    ledger.update_trigger(trigger_id, now=T0, status="triggered", triggered_at=T0)
    ledger.insert_fire(
        {"trigger_id": trigger_id, "n": 2, "at": T0, "status": "alerted", "price_usd": 1890.0}
    )
    world.at(T0 + 2 * triggers.ORPHAN_FIRE_S)
    await world.check(1800, step=0)
    assert world.row(trigger_id)["status"] == "done"
    assert len(world.fires(trigger_id)) == 2


# ── ledger ─────────────────────────────────────────────────────────────────


def _order(order_id: str, **fields: Any) -> dict[str, Any]:
    return {
        "order_id": order_id,
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
        **fields,
    }


def test_migration_v7_to_v8_keeps_orders_and_mandates(tmp_path: Path) -> None:
    path = tmp_path / "trading.sqlite"
    store = Ledger(path)
    store.insert_order(_order("ord_old", mandate_id="dca_old"))
    store.insert_mandate(
        {
            "mandate_id": "dca_old",
            "name": "DCA ETH",
            "status": "active",
            "chain_id": 8453,
            "wallet": "0xabc",
            "token_out": WETH,
            "token_in": USDC,
            "usd_per_run": 10.0,
            "cap_usd": 100.0,
            "every_seconds": 86_400,
            "initiator": "manual",
            "created_at": 1.0,
            "updated_at": 1.0,
        }
    )
    store.close()
    # Turn the file back into what version 7 left on disk: no trigger tables,
    # no ``orders.trigger_id`` (rebuilt by hand; DROP COLUMN trips on some
    # SQLite builds over the commented CREATE TABLE).
    conn = sqlite3.connect(path)
    kept = [r[1] for r in conn.execute("PRAGMA table_info(orders)") if r[1] != "trigger_id"]
    conn.executescript(
        f"CREATE TABLE orders_v7 AS SELECT {', '.join(kept)} FROM orders; "
        "DROP TABLE orders; ALTER TABLE orders_v7 RENAME TO orders; "
        "DROP TABLE triggers; DROP TABLE trigger_fires; UPDATE schema_version SET version = 7;"
    )
    conn.commit()
    assert "trigger_id" not in {r[1] for r in conn.execute("PRAGMA table_info(orders)")}
    conn.close()

    migrated = Ledger(path)
    try:
        order = migrated.get_order("ord_old")
        assert order is not None and order["status"] == "confirmed"
        assert order["mandate_id"] == "dca_old" and order["trigger_id"] is None
        mandate = migrated.get_mandate("dca_old")
        assert mandate is not None and mandate["status"] == "active"
        conn = sqlite3.connect(path)
        version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        conn.close()
        assert version == SCHEMA_VERSION == 9
        assert {
            "triggers",
            "trigger_fires",
            "idx_orders_trigger",
            "idx_triggers_status",
            "idx_trigger_fires",
            "idx_orders_mandate",
        } <= tables
    finally:
        migrated.close()


def test_ledger_ties_a_trigger_order_to_its_open_fire() -> None:
    store = Ledger(":memory:")
    try:
        fire_id = store.insert_fire({"trigger_id": "trg_y", "n": 1, "at": 1.0, "status": "pending"})
        done_id = store.insert_fire({"trigger_id": "trg_y", "n": 2, "at": 2.0, "status": "failed"})
        store.insert_order(_order("g", status="quoted", trigger_id="trg_y"))
        assert store.get_fire(fire_id)["order_id"] == "g"  # type: ignore[index]
        assert store.get_fire(done_id)["order_id"] is None  # type: ignore[index]
        assert store.fire_for_order("g")["fire_id"] == fire_id  # type: ignore[index]
        assert [o["order_id"] for o in store.orders_for_trigger("trg_y")] == ["g"]
        assert store.fire_attempts("trg_y") == 2 and store.fire_attempts("trg_z") == 0
        assert [f["fire_id"] for f in store.open_fires()] == [fire_id]
        assert store.open_fires("trg_z") == []
        assert store.update_fire(fire_id, expect_status=["parked"], status="filled") is None
        assert store.update_fire(fire_id, expect_status=["pending"], status="filled") is not None
        assert [f["n"] for f in store.list_fires("trg_y")] == [2, 1]
        with pytest.raises(ValueError):
            store.add_trigger_counts("trg_y", now=1.0, runs_done=1)
    finally:
        store.close()


# ── the card fixtures ──────────────────────────────────────────────────────


async def _card_world(world: World) -> dict[str, dict[str, Any]]:
    """Six triggers in every live state, as the frontend fixtures show them."""
    service = world.service
    payloads: dict[str, dict[str, Any]] = {
        "triggers-empty": await service.trigger_list(all=True),
    }
    # A take-profit that ran in the first minute: 0.02 WETH sold over $2,100.
    done_id = (await world.create(direction="above", price="+5%", amount_pct=None, amount="0.02"))[
        "trigger"
    ]["id"]
    await world.check(2110)
    await world.check(2120)
    # Three armed on the same price: a trailing alert, a stop-loss and a sell
    # over the approval threshold the user fires by hand (it waits for them).
    alert_id = (
        await world.create(
            kind="alert", direction="trail", price=None, trail_pct=5, amount_pct=None
        )
    )["trigger"]["id"]
    armed_id = (await world.create(price="2050", slippage_pct=1.0, valid_for_seconds=7 * DAY))[
        "trigger"
    ]["id"]
    trim_id = (
        await world.create(
            direction="above", price="2400", amount_pct=None, amount_usd=150, name="Trim WETH"
        )
    )["trigger"]["id"]
    await service.trigger_fire_now(trim_id)
    paused_id = (await world.create(kind="buy", price="1800", amount_pct=None, amount_usd=25))[
        "trigger"
    ]["id"]
    await service.trigger_pause(paused_id)
    await world.check(2150)
    await world.check(2180)
    await world.check(2045)  # under $2,050 and under the trail's stop: one check each
    # A proposal from the agent: buy the dip 10 % under the price now.
    world.at(world.clock.now + 20)
    awaiting_id = (
        await world.create(
            kind="buy",
            price="-10%",
            amount_pct=None,
            amount_usd=150,
            initiator="agent",
            session_key="agent:desk:main",
        )
    )["trigger"]["id"]
    world.at(world.clock.now + 10)
    payloads["trigger-armed"] = await service.trigger_get(armed_id)
    payloads["trigger-awaiting"] = await service.trigger_get(awaiting_id)
    payloads["trigger-done"] = await service.trigger_get(done_id)
    payloads["trigger-alert"] = await service.trigger_get(alert_id)
    payloads["triggers"] = await service.trigger_list()
    return payloads


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


async def test_card_fixtures_match_the_engine(world: World) -> None:
    payloads = await _card_world(world)
    armed = payloads["trigger-armed"]["trigger"]
    assert armed["status"] == "armed" and armed["name"] == "Stop-loss WETH"
    assert armed["condition"]["hits"] == 1 and armed["market"]["priceUsd"] == 2045.0
    assert armed["validUntil"] is not None and armed["action"]["slippagePct"] == 1.0
    awaiting = payloads["trigger-awaiting"]["trigger"]
    assert awaiting["status"] == "awaiting_approval" and awaiting["kind"] == "buy"
    assert awaiting["condition"]["fromPriceUsd"] == 2045.0
    assert awaiting["condition"]["priceUsd"] == pytest.approx(1840.5)
    assert awaiting["action"]["needsApproval"] is True
    assert payloads["trigger-awaiting"]["warnings"] == [
        "a buy of ≈$150 is above the $100 approval threshold and will wait for you when it fires"
    ]
    done = payloads["trigger-done"]["trigger"]
    assert done["status"] == "done" and done["result"]["amountIn"]["human"] == "0.02"
    assert done["statusReason"] == "sold 0.02 WETH for 42.4 USDC at $2,120"
    alert = payloads["trigger-alert"]["trigger"]
    assert alert["kind"] == "alert" and alert["condition"]["peakPriceUsd"] == 2180.0
    assert alert["condition"]["stopPriceUsd"] == pytest.approx(2071.0)
    listing = payloads["triggers"]
    assert sorted(t["status"] for t in listing["triggers"]) == [
        "armed",
        "armed",
        "awaiting_approval",
        "paused",
        "triggered",
    ]
    assert listing["totals"] == {"count": 5, "armed": 2, "awaiting": 1, "triggered": 1}
    assert payloads["triggers-empty"]["triggers"] == []
    assert payloads["triggers-empty"]["totals"]["count"] == 0

    if os.environ.get("AGENTOS_REGEN_TRIGGER_FIXTURES"):
        FIXTURES.mkdir(parents=True, exist_ok=True)
        for name in FIXTURE_NAMES:
            (FIXTURES / f"{name}.json").write_text(_dump(payloads[name]), encoding="utf-8")
    for name in FIXTURE_NAMES:
        pinned = (FIXTURES / f"{name}.json").read_text(encoding="utf-8")
        assert pinned == _dump(payloads[name]), f"{name}.json is stale: regenerate it"
