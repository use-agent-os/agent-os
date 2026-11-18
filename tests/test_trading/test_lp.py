"""LP-card payload builders (``agentos.trading.lp``) against an offline V4 world.

The contract is ``docs/lp-cards.md``; ``tests/fixtures/lp_cards/*.json`` are the
renderer fixtures and are regenerated from this same world, so they are pinned
here against the builders' real output.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from agentos.trading import lp, lp_book
from agentos.trading.chains import BASE, ROBINHOOD
from agentos.trading.prices import PriceInfo
from agentos.trading.service import TradingError
from tests.test_trading.lp_world import (
    BAR,
    BONER,
    CLANKER_LOCKER,
    LIB,
    OUTSIDER,
    PEPE,
    USDG,
    WALLET,
    WETH,
    _matches,
    boner_world,
    empty_world,
    env_for,
    pepe_world,
    serve_transfer_logs,
    transfer_log,
    world_prices,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "lp_cards"
KINDS = ("pool", "ranges", "position", "positions")


def _build(kind: str) -> dict[str, Any]:
    world = pepe_world()
    if kind == "pool":
        return lp.build_pool(env_for(world), PEPE)
    if kind == "ranges":
        return lp.build_ranges(env_for(world), PEPE)
    if kind == "position":
        return lp.build_position(env_for(world), 48_213)
    env = env_for(world, nft_ids=world.indexer)
    rows = lp.build_chain_positions(env, [WALLET.lower(), OUTSIDER.lower()], False)
    wallets = [
        {"address": LIB.hexutil.checksum_address(WALLET), "label": "Main", "inApp": True},
        {"address": LIB.hexutil.checksum_address(OUTSIDER), "label": None, "inApp": False},
    ]
    return lp.positions_payload([env], wallets, rows)


def _shape(value: Any) -> Any:
    """Keys and JSON types, recursively; lists by their first element."""
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_shape(value[0])] if value else []
    if isinstance(value, bool) or value is None:
        return type(value).__name__
    if isinstance(value, int | float):
        return "number"
    return type(value).__name__


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(lp, "_full_range_refused", {})
    monkeypatch.setattr(lp, "_stated_spans", {})
    monkeypatch.setattr(lp, "_in_flight", {})
    monkeypatch.setattr(lp, "_shared_logs", {})
    monkeypatch.setattr(lp, "LOG_RETRY_DELAY_S", 0.0)
    monkeypatch.setattr(lp, "PRICE_RETRY_DELAY_S", 0.0)
    monkeypatch.setattr(lp, "_price_book", lp.PriceBook())
    # No test reaches GeckoTerminal: it "answers" with no prices unless a test says otherwise.
    monkeypatch.setattr(lp, "_gecko_window", lambda network, window: {})


# ── the envelope and the four kinds ────────────────────────────────────────


def test_pool_card() -> None:
    card = _build("pool")
    assert card["version"] == 1 and card["kind"] == "pool"
    assert card["chain"] == {
        "id": 8453,
        "key": "base",
        "name": "Base",
        "explorer": "https://basescan.org",
    }
    assert card["asOfBlock"] == 21_044_901 and card["partialScan"] is False
    assert card["token"]["symbol"] == "PEPE" and card["quote"]["symbol"] == "WETH"
    pool = card["pool"]
    assert pool["feePct"] == "dynamic" and pool["tickSpacing"] == 200
    assert pool["hook"] == LIB.hexutil.checksum_address(pool["hook"])
    # PEPE has no listed price: it is implied by the pool (quote price × pool price).
    assert card["token"]["priceUsd"] == pytest.approx(2.5e-5, rel=0.05)
    assert pool["mcapUsd"] == pytest.approx(2.5e6, rel=0.05)
    reserves = card["reserves"]
    assert int(reserves["base"]["raw"]) > 0 and int(reserves["quote"]["raw"]) > 0
    assert pool["tvlUsd"] == pytest.approx(reserves["base"]["usd"] + reserves["quote"]["usd"])
    assert card["safety"]["locked"] is True
    assert card["safety"]["launcher"]["name"] == "Clanker v4.1"
    assert "locker" in card["safety"]["note"]
    top = card["topRanges"]
    assert 0 < len(top) <= lp.TOP_RANGES
    liquidity = [int(r["liquidity"]) for r in top]
    assert liquidity == sorted(liquidity, reverse=True)
    # The top segment above the price is liquidity only the locker holds.
    locked_only = next(r for r in top if r["tickLower"] == 190_000)
    assert locked_only["owner"] == CLANKER_LOCKER
    assert all(r["mcapLower"] < r["mcapUpper"] for r in top)
    assert all(r["priceLower"] < r["priceUpper"] for r in top)


def test_ranges_card_segments_are_sorted_and_share_sums_to_one() -> None:
    card = _build("ranges")
    segments = card["segments"]
    assert [s["tickLower"] for s in segments] == sorted(s["tickLower"] for s in segments)
    assert sum(s["share"] for s in segments) == pytest.approx(1.0)
    active = [s for s in segments if s["active"]]
    assert len(active) == 1
    assert active[0]["tickLower"] <= card["current"]["tick"] < active[0]["tickUpper"]
    assert card["scan"] == {
        "mode": "ticks",
        "scannedWords": 36,
        "fullWords": 36,
        "truncated": False,
    }
    # PEPE is currency1, so the lowest ticks are the highest PEPE prices: that
    # segment sits above the current price and holds only PEPE.
    lowest = segments[0]
    assert int(lowest["base"]["raw"]) > 0 and lowest["quote"]["raw"] == "0"
    assert lowest["priceLower"] > card["pool"]["priceUsd"] / card["quote"]["priceUsd"]
    assert card["current"]["mcapUsd"] == card["pool"]["mcapUsd"]


def test_position_card_above_range() -> None:
    card = _build("position")
    pos = card["position"]
    assert pos["tokenId"] == "48213" and pos["status"] == "above-range"
    # Price ran above the range: the position is all quote (WETH).
    assert pos["principal"]["base"]["raw"] == "0"
    assert int(pos["principal"]["quote"]["raw"]) > 0
    assert pos["distancePct"] is not None and pos["distancePct"] > 0
    assert pos["owner"] == {
        "address": LIB.hexutil.checksum_address(WALLET),
        "label": "Main",
        "inApp": True,
    }
    assert pos["band"] and "→" in pos["band"]
    assert pos["valueUsd"] == pytest.approx(pos["principal"]["usd"] + pos["fees"]["usd"])
    assert pos["pool"]["tvlUsd"] is None


def test_positions_card_sorting_and_totals() -> None:
    card = _build("positions")
    assert card["chain"] is None and card["chains"][0]["key"] == "base"
    ids = [p["tokenId"] for p in card["positions"]]
    # Out of range first (above and below alike), then in range, each by value;
    # the unpriced one last in its group; the closed one omitted.
    assert [p["status"] for p in card["positions"]] == [
        "below-range",
        "above-range",
        "in-range",
        "in-range",
    ]
    assert ids == ["47100", "48213", "48214", "48215"]
    below = card["positions"][0]
    # Price fell below the outsider's range: all base (PEPE), no quote left.
    assert below["principal"]["quote"]["raw"] == "0" and below["distancePct"] > 0
    assert below["owner"]["inApp"] is False
    unpriced = card["positions"][-1]
    assert unpriced["valueUsd"] is None and unpriced["token"]["priceUsd"] is None
    assert unpriced["principal"]["usd"] is None and unpriced["token"]["symbol"] == "BAR"
    totals = card["totals"]
    assert totals["count"] == 4 and totals["outOfRange"] == 2
    priced = [p["valueUsd"] for p in card["positions"] if p["valueUsd"] is not None]
    assert totals["valueUsd"] == pytest.approx(sum(priced))
    assert any("no USD price" in w for w in card["warnings"])
    assert card["wallets"][1]["inApp"] is False
    assert card["partialScan"] is False


def test_positions_include_closed_with_all() -> None:
    world = pepe_world()
    env = env_for(world, nft_ids=lambda owner, **_: [48_213, 48_214, 48_215, 48_216])
    rows = lp.build_chain_positions(env, [WALLET.lower()], True)
    closed = [r for r in rows if r["status"] == "closed"]
    assert [r["tokenId"] for r in closed] == ["48216"]
    assert closed[0]["fees"]["usd"] == 0.0 and closed[0]["distancePct"] is None
    ordered = lp.sort_positions(rows)
    assert ordered[-1]["status"] == "closed"


def test_sort_rule_is_group_then_value_then_unpriced() -> None:
    def row(token_id: int, status: str, value: float | None) -> dict[str, Any]:
        return {
            "tokenId": str(token_id),
            "status": status,
            "valueUsd": value,
            "chain": {"id": 8453},
        }

    rows = [
        row(1, "in-range", 50.0),
        row(2, "below-range", None),
        row(3, "in-range", None),
        row(4, "above-range", 5.0),
        row(5, "below-range", 900.0),
        row(6, "in-range", 70.0),
        row(7, "closed", 1.0),
    ]
    assert [r["tokenId"] for r in lp.sort_positions(rows)] == ["5", "4", "2", "6", "1", "3", "7"]


# ── scans, prices, errors ──────────────────────────────────────────────────


def test_indexer_short_of_balance_marks_partial() -> None:
    world = pepe_world()
    env = env_for(world, nft_ids=lambda owner, **_: [48_213])
    rows = lp.build_chain_positions(env, [WALLET.lower()], False)
    assert [r["tokenId"] for r in rows] == ["48213"]
    assert env.partial is True
    assert any("found 1 of the 4" in w for w in env.warnings)


def test_positions_from_logs_when_node_serves_full_range() -> None:
    world = pepe_world()
    world.logs_refused = False
    seen: list[dict[str, Any]] = []

    def get_logs(params: dict[str, Any]) -> list[dict[str, Any]]:
        seen.append(params)
        topic = "0x" + "0" * 24 + WALLET[2:].lower()
        return [
            {"topics": [LIB.abi.TOPIC_ERC721_TRANSFER, "0x" + "0" * 64, topic, hex(t)]}
            for t in (48_213, 48_214, 48_215, 48_216)
        ]

    world.get_logs = get_logs  # type: ignore[method-assign]
    env = env_for(world)  # no indexer at all
    rows = lp.build_chain_positions(env, [WALLET.lower()], False)
    assert len(rows) == 3 and env.partial is False
    assert seen[0]["toBlock"] == "latest"


def test_wallet_without_positions_is_an_exact_empty_answer() -> None:
    world = pepe_world()
    env = env_for(world, nft_ids=lambda owner, **_: pytest.fail("no lookup for an empty wallet"))
    assert lp.build_chain_positions(env, [OUTSIDER.lower().replace("3", "4")], False) == []
    assert env.partial is False


def test_empty_positions_payload_is_a_card() -> None:
    env = env_for(empty_world(ROBINHOOD))
    card = lp.positions_payload([env], [], [])
    assert card["kind"] == "positions" and card["positions"] == []
    assert card["totals"] == {"valueUsd": 0.0, "feesUsd": 0.0, "count": 0, "outOfRange": 0}
    assert card["asOfBlocks"] == {"robinhood": 73_859_199}


def test_unknown_prices_are_null_never_zero() -> None:
    world = pepe_world()
    card = lp.build_pool(env_for(world, prices=world_prices(world, {})), PEPE)
    assert card["token"]["priceUsd"] is None and card["quote"]["priceUsd"] is None
    assert card["pool"]["priceUsd"] is None and card["pool"]["mcapUsd"] is None
    assert card["pool"]["tvlUsd"] is None
    assert card["reserves"]["base"]["usd"] is None and card["reserves"]["quote"]["usd"] is None
    assert all(r["mcapLower"] is None and r["mcapUpper"] is None for r in card["topRanges"])
    assert all(r["priceLower"] is not None for r in card["topRanges"])


def test_truncated_bitmap_walk_is_a_partial_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lp, "MAX_BITMAP_WORDS", 10)
    card = lp.build_ranges(env_for(pepe_world()), PEPE)
    assert card["partialScan"] is True
    assert card["scan"]["truncated"] is True
    assert card["scan"]["scannedWords"] == 10 and card["scan"]["fullWords"] == 36
    assert any("partial scan" in w for w in card["warnings"])


def test_pool_by_id_uses_pair_tokens_and_orients_on_the_known_quote() -> None:
    world = pepe_world()
    pool_id = next(iter(world.pools))
    env = env_for(world)
    env.pair_tokens = lambda pid: [PEPE.lower(), WETH.lower()]
    card = lp.build_pool(env, pool_id)
    assert card["pool"]["poolId"] == pool_id
    assert card["token"]["symbol"] == "PEPE"  # not currency0 (WETH): see lp.make_side


def test_pool_not_found_is_a_coded_error() -> None:
    with pytest.raises(TradingError) as err:
        lp.build_pool(env_for(empty_world(BASE)), BAR)
    assert err.value.code == "trading.lp.not_found"
    with pytest.raises(TradingError) as err:
        lp.build_position(env_for(pepe_world()), 999_999)
    assert err.value.code == "trading.lp.not_found"


def test_no_infinity_or_nan_reaches_json() -> None:
    world = pepe_world()
    pepe = next(iter(world.pools.values()))
    world.add_position(pepe, 49_000, -887_200, 887_200, 10**20, OUTSIDER)
    card = lp.build_ranges(env_for(world), PEPE)
    json.dumps(card, allow_nan=False)


# ── the fixtures the frontend renders ──────────────────────────────────────


@pytest.mark.parametrize("kind", KINDS)
def test_fixture_matches_the_builder(kind: str) -> None:
    fixture = json.loads((FIXTURES / f"{kind}.json").read_text(encoding="utf-8"))
    built = json.loads(json.dumps(_build(kind)))
    assert _shape(fixture) == _shape(built)
    built["fetchedAt"] = fixture["fetchedAt"]
    assert fixture == built


# ── engine glue ────────────────────────────────────────────────────────────


class _Prices:
    def __init__(self) -> None:
        self.asked: list[list[str]] = []

    async def prices(self, chain: Any, addresses: Any) -> dict[str, PriceInfo]:
        self.asked.append(list(addresses))
        return {
            a.lower(): PriceInfo(price_usd=2500.0 if a.lower() == WETH.lower() else None)
            for a in addresses
        }

    async def pair_tokens(self, chain: Any, pair: str) -> list[str]:
        return []


class _Vault:
    initialized = True

    def list(self) -> list[Any]:
        return []


class _Service:
    def __init__(self) -> None:
        self.config = type("C", (), {"rpc_urls": {"base": "https://rpc.example/base-key"}})()
        self.prices = _Prices()
        self.vault = _Vault()
        self.lp_book = lp_book.LpBook(self)  # type: ignore[arg-type]

    async def resolve_token(self, chain: Any, value: str) -> Any:
        raise TradingError("trading.invalid", f"Unknown token symbol {value!r}")


async def test_engine_prices_first_then_geckoterminal(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _Service()
    gecko_asked: list[tuple[str, list[str]]] = []

    def gecko(network: str, window: list[str]) -> dict[str, Any]:
        gecko_asked.append((network, window))
        return {a: "0.5" for a in window}

    monkeypatch.setattr(lp, "_gecko_window", gecko)
    env = lp._engine_env(service, BASE, asyncio.get_running_loop())  # type: ignore[arg-type]
    # The engine's RPC override reaches the client verbatim; no env variable involved.
    assert env.client.url == "https://rpc.example/base-key"
    got = await asyncio.to_thread(env.prices, [WETH, PEPE])
    assert got == {WETH.lower(): 2500.0, PEPE.lower(): 0.5}
    assert gecko_asked == [("base", [PEPE.lower()])]
    # Found prices are reused by the next read in the process: nothing is asked again.
    again = lp._engine_env(service, BASE, asyncio.get_running_loop())  # type: ignore[arg-type]
    assert await asyncio.to_thread(again.prices, [PEPE, WETH]) == got
    assert len(gecko_asked) == 1 and len(service.prices.asked) == 1


async def test_the_retry_waits_out_the_engines_hold_on_a_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The engine holds an unanswered chunk for a few seconds; a retry after one
    second would only be handed that same miss back."""
    from agentos.trading import prices as prices_mod
    from agentos.trading.prices import PriceService

    monkeypatch.setattr(prices_mod, "UNAVAILABLE_HOLD_S", 0.4)
    monkeypatch.setattr(prices_mod, "UNAVAILABLE_JITTER_S", 0.0)
    monkeypatch.setattr(lp, "PRICE_RETRY_DELAY_S", 0.05)
    sent: list[float] = []

    def dexscreener(request: httpx.Request) -> httpx.Response:
        sent.append(time.monotonic())
        if len(sent) == 1:
            return httpx.Response(429)
        pair = {
            "chainId": "base",
            "pairAddress": "0xpair",
            "baseToken": {"address": WETH.lower(), "symbol": "WETH"},
            "quoteToken": {"symbol": "USDC"},
            "priceUsd": "2500",
            "liquidity": {"usd": 1_000_000},
        }
        return httpx.Response(200, json=[pair])

    service = _Service()
    async with httpx.AsyncClient(transport=httpx.MockTransport(dexscreener)) as http:
        service.prices = PriceService(http=http)  # type: ignore[assignment]
        env = lp._engine_env(service, BASE, asyncio.get_running_loop())  # type: ignore[arg-type]
        got = await asyncio.to_thread(env.prices, [WETH])
    assert got == {WETH.lower(): 2500.0}
    # Two requests: the 429, then one after the hold -- not one after the 0.05 s
    # pause. The hold's remainder is read off ``time.time`` (about 16 ms coarse on
    # Windows) while this gap is ``time.monotonic``, so leave the clocks some room.
    assert len(sent) == 2 and sent[1] - sent[0] >= 0.3


# ── prices under concurrency ────────────────────────────────────────────────

SIS = "0x5155000000000000000000000000000000005155"


class _FlakyPrices(_Prices):
    """The engine's source as the live one behaves on Robinhood: USDG is only ever a
    pair's quote token there, so it never has a price, and a burst gets throttled."""

    def __init__(self, fail_every: int = 0) -> None:
        super().__init__()
        self.fail_every = fail_every
        self.calls = 0

    async def prices(self, chain: Any, addresses: Any) -> dict[str, PriceInfo]:
        self.asked.append(list(addresses))
        self.calls += 1
        await asyncio.sleep(0.02)
        throttled = self.fail_every and self.calls % self.fail_every == 0
        return {
            a.lower(): PriceInfo(price_usd=None, unavailable=bool(throttled)) for a in addresses
        }


class _Gecko:
    """GeckoTerminal: answers slowly, and ``None`` (a 429) whenever ``fails`` says so."""

    def __init__(self, fails: Any, table: dict[str, str]) -> None:
        self.fails = fails
        self.table = table
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, network: str, window: list[str]) -> dict[str, Any] | None:
        with self._lock:
            self.calls += 1
            call = self.calls
        time.sleep(0.05)
        if self.fails(call):
            return None
        return {a: self.table[a] for a in window if a in self.table}


async def _nine_reads(service: Any) -> list[tuple[dict[str, float | None], list[str]]]:
    """Nine robinhood reads at once, each on its own env (as the gateway runs them)."""
    loop = asyncio.get_running_loop()

    def read() -> tuple[dict[str, float | None], list[str]]:
        env = lp._engine_env(service, ROBINHOOD, loop)
        got = lp.usd_prices(env, [USDG, SIS])
        return got, env.warnings

    return list(await asyncio.gather(*(asyncio.to_thread(read) for _ in range(9))))


async def test_concurrent_reads_share_one_lookup_and_retry_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _Service()
    service.prices = _FlakyPrices()
    # The first GeckoTerminal call is throttled; the one retry gets through.
    gecko = _Gecko(lambda call: call == 1, {USDG: "1.0015", SIS: "0.0000084"})
    monkeypatch.setattr(lp, "_gecko_window", gecko)
    reads = await _nine_reads(service)
    assert all(got[USDG] == pytest.approx(1.0015) for got, _ in reads)
    assert all(got[SIS] == pytest.approx(8.4e-6) for got, _ in reads)
    assert all(warnings == [] for _, warnings in reads)
    # One lookup for all nine reads: the engine asked once, GeckoTerminal once plus the retry.
    assert service.prices.calls == 1 and gecko.calls == 2


async def test_a_throttled_engine_source_is_retried_once(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _Service()
    service.prices = _FlakyPrices(fail_every=1)
    monkeypatch.setattr(lp, "_gecko_window", _Gecko(lambda call: False, {USDG: "1.0"}))
    loop = asyncio.get_running_loop()
    env = lp._engine_env(service, ROBINHOOD, loop)
    await asyncio.to_thread(env.prices, [SIS])
    assert service.prices.asked == [[SIS], [SIS]]


async def test_usdg_is_never_unpriced_when_every_source_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _Service()
    service.prices = _FlakyPrices(fail_every=2)
    gecko = _Gecko(lambda call: True, {})
    monkeypatch.setattr(lp, "_gecko_window", gecko)
    reads = await _nine_reads(service)
    for got, warnings in reads:
        assert got[USDG] == 1.0
        assert got[SIS] is None  # not a stable: unpriced, never guessed
        assert warnings == ["USDG priced at $1.00 (provider had no quote)"]
    # A miss is not remembered: the next read asks the sources again.
    before = gecko.calls
    await _nine_reads(service)
    assert gecko.calls > before


async def test_intermittent_failures_never_lose_usdg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rounds of nine concurrent reads against sources that fail every other call."""
    for round_ in range(6):
        monkeypatch.setattr(lp, "_price_book", lp.PriceBook())
        service = _Service()
        service.prices = _FlakyPrices(fail_every=2)
        gecko = _Gecko(lambda call, r=round_: (call + r) % 2 == 0, {USDG: "1.0003"})
        monkeypatch.setattr(lp, "_gecko_window", gecko)
        for got, warnings in await _nine_reads(service):
            assert got[USDG] in (pytest.approx(1.0003), 1.0)
            if got[USDG] == 1.0:
                assert "USDG priced at $1.00 (provider had no quote)" in warnings


def test_price_book_waits_on_a_lookup_in_flight() -> None:
    book = lp.PriceBook()
    started, release = threading.Event(), threading.Event()
    asked: list[list[str]] = []

    def slow(addresses: list[str]) -> dict[str, float | None]:
        asked.append(addresses)
        started.set()
        release.wait(5)
        return {a: 2.0 for a in addresses}

    results: list[dict[str, float | None]] = []
    first = threading.Thread(target=lambda: results.append(book.lookup("x", ["0xA"], slow)))
    first.start()
    started.wait(5)
    second = threading.Thread(target=lambda: results.append(book.lookup("x", ["0xa"], slow)))
    second.start()
    release.set()
    first.join(5)
    second.join(5)
    assert asked == [["0xa"]] and results == [{"0xa": 2.0}, {"0xa": 2.0}]
    # A failing owner resolves its waiters with a miss and remembers nothing.

    def boom(addresses: list[str]) -> dict[str, float | None]:
        raise RuntimeError("down")

    with pytest.raises(RuntimeError):
        book.lookup("x", ["0xb"], boom)
    assert book.lookup("x", ["0xb"], lambda a: {"0xb": 3.0}) == {"0xb": 3.0}


def test_unpriced_quote_says_why_tvl_and_mcap_are_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = boner_world()
    card = lp.build_pool(env_for(world, prices=world_prices(world, {BONER: 0.0467})), BONER)
    # USDG falls back to par, so this pool is priced after all -- and says how.
    assert card["quote"]["priceUsd"] == 1.0 and card["pool"]["tvlUsd"] is not None
    assert card["pool"]["mcapUsd"] is not None
    assert "USDG priced at $1.00 (provider had no quote)" in card["warnings"]
    # A quote asset with no price and no par to fall back on (USDG, were it not a stable).
    monkeypatch.setattr(lp, "STABLE_SYMBOLS", frozenset())
    unpriced = "no USD price for USDG; TVL and mcap unavailable"
    card = lp.build_pool(env_for(world, prices=world_prices(world, {BONER: 0.0467})), BONER)
    assert card["quote"]["priceUsd"] is None and card["token"]["priceUsd"] == 0.0467
    assert card["pool"]["tvlUsd"] is None and card["pool"]["mcapUsd"] is None
    assert unpriced in card["warnings"]
    ranges = lp.build_ranges(env_for(world, prices=world_prices(world, {BONER: 0.0467})), BONER)
    assert ranges["pool"]["mcapUsd"] is None and ranges["current"]["mcapUsd"] is None
    assert unpriced in ranges["warnings"]


async def test_no_chain_tries_base_then_robinhood(monkeypatch: pytest.MonkeyPatch) -> None:
    worlds = {"base": empty_world(BASE), "robinhood": pepe_world()}
    worlds["robinhood"].spec = ROBINHOOD
    tried: list[str] = []

    def fake_env(service: Any, spec: Any, loop: Any) -> lp.ChainEnv:
        tried.append(spec.key)
        world = worlds[spec.key]
        return lp.ChainEnv(spec=spec, client=world, prices=world_prices(world), lib=LIB)

    monkeypatch.setattr(lp, "_engine_env", fake_env)
    with pytest.raises(TradingError) as err:
        await lp.lp_pool(_Service(), chain=None, target=BAR)  # type: ignore[arg-type]
    assert err.value.code == "trading.lp.not_found" and tried == ["base", "robinhood"]
    with pytest.raises(TradingError) as err:
        await lp.lp_pool(_Service(), chain=BASE, target="NOPE")  # type: ignore[arg-type]
    assert err.value.code == "trading.invalid"


async def test_rpc_failure_is_a_coded_rpc_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class Down:
        def block_number(self) -> int:
            raise RuntimeError("eth_blockNumber: HTTP 503")

    def fake_env(service: Any, spec: Any, loop: Any) -> lp.ChainEnv:
        return lp.ChainEnv(spec=spec, client=Down(), prices=lambda a: {}, lib=LIB)

    monkeypatch.setattr(lp, "_engine_env", fake_env)
    with pytest.raises(TradingError) as err:
        await lp.lp_position(_Service(), chain=BASE, token_id=1)  # type: ignore[arg-type]
    assert err.value.code == "trading.rpc" and "Base" in str(err.value)
    with pytest.raises(TradingError) as err:
        await lp.lp_positions(_Service(), wallets=[WALLET])  # type: ignore[arg-type]
    assert err.value.code == "trading.rpc"


def test_library_is_the_skills_and_never_reads_its_env(monkeypatch: pytest.MonkeyPatch) -> None:
    lib = lp.unilp()
    assert Path(lib.v4_math.__file__).parent.name == "unilp"
    assert "senior-unilp-manager" in lib.v4_math.__file__
    assert lib.chains._env_loaded is True
    assert lib.v4_math.__name__ == "_agentos_unilp.v4_math"
    monkeypatch.setenv("RPC_BASE_URL", "https://from-env.example")
    client = lib.rpc.RpcClient(dict(lib.chains.CHAINS["base"]), "https://engine.example")
    assert client.url == "https://engine.example"


def write_fixtures() -> None:
    """Regenerate ``tests/fixtures/lp_cards``:
    ``AGENTOS_STATE_DIR=$(mktemp -d) uv run python -m tests.test_trading.test_lp``."""
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for kind in KINDS:
        text = json.dumps(_build(kind), indent=2, ensure_ascii=False) + "\n"
        (FIXTURES / f"{kind}.json").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    write_fixtures()


def test_refused_full_history_walks_back_until_the_wallet_is_complete() -> None:
    world = pepe_world()
    world.block = 30_000_000
    batches: list[list[dict[str, Any]]] = []
    minted_at = 29_500_000  # inside the first batch of 100k-block windows

    def batch(calls: list[dict[str, Any]], chunk_size: int = 20) -> list[Any]:
        batches.append(calls)
        out: list[Any] = []
        for call in calls:
            window = call["params"][0]
            lo, hi = int(window["fromBlock"], 16), int(window["toBlock"], 16)
            hit = lo <= minted_at <= hi
            topic = "0x" + "0" * 24 + WALLET[2:].lower()
            out.append(
                [
                    {"topics": [LIB.abi.TOPIC_ERC721_TRANSFER, "0x" + "0" * 64, topic, hex(t)]}
                    for t in (48_213, 48_214, 48_215, 48_216)
                ]
                if hit
                else []
            )
        return out

    world.batch = batch  # type: ignore[method-assign]
    env = env_for(world)  # no indexer; get_logs refuses the full range
    rows = lp.build_chain_positions(env, [WALLET.lower()], True)
    assert len(rows) == 4 and env.partial is False
    assert len(batches) == 1  # complete after the first batch: no further windows
    first = batches[0][0]["params"][0]
    assert first["toBlock"] == hex(30_000_000)
    assert int(first["toBlock"], 16) - int(first["fromBlock"], 16) + 1 == BASE.approval_log_span


class _ValueCappedNode:
    """Robinhood Chain's public node since 2026-11: a filter with one value per
    topic position is served over 10,000,000 blocks, one with several values in
    a position over 100,000 only -- and no filter over the whole history."""

    ONE, MANY = 10_000_000, 100_000

    def __init__(self, world: Any, minted: dict[str, tuple[int, list[int]]]) -> None:
        self.world, self.minted = world, minted
        self.url = "https://value-capped.example"
        self.requests = 0
        self.filters: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.world, name)

    def _logs(self, params: dict[str, Any]) -> list[Any]:
        topics = params["topics"]
        received = len(topics) > 2
        owners = topics[2] if received else topics[1]
        owners = owners if isinstance(owners, list) else [owners]
        allowed = self.ONE if len(owners) == 1 else self.MANY
        head = self.world.block
        lo = int(params["fromBlock"], 16)
        hi = head if params["toBlock"] == "latest" else int(params["toBlock"], 16)
        if hi - lo + 1 > allowed:
            hint = "" if len(owners) == 1 else ", or send one value per position"
            raise RuntimeError(
                f"eth_getLogs: query spans {hi - lo + 1} blocks ({lo} to {hi}), but only "
                f"{allowed} are allowed for this request; narrow the block range{hint}"
            )
        self.filters.append(params)
        out: list[Any] = []
        for owner in owners if received else []:
            block, ids = self.minted.get("0x" + owner[-40:], (0, []))
            if lo <= block <= hi:
                out += [
                    {
                        "topics": [LIB.abi.TOPIC_ERC721_TRANSFER, "0x" + "0" * 64, owner, hex(t)],
                        "blockNumber": hex(block),
                    }
                    for t in ids
                ]
        return out

    def get_logs(self, params: dict[str, Any]) -> list[Any]:
        self.requests += 1
        return self._logs(params)

    def batch(self, calls: list[dict[str, Any]], chunk_size: int = 20) -> list[Any]:
        self.requests += 1
        out: list[Any] = []
        for call in calls:
            try:
                out.append(self._logs(call["params"][0]))
            except RuntimeError as exc:
                out.append({"error": {"code": -32602, "message": str(exc)}})
        return out


def test_a_node_capping_many_valued_filters_is_walked_one_wallet_at_a_time() -> None:
    """Robinhood Chain, 2026-11-18: every V4 position of fifteen wallets went
    missing. The node stopped serving the whole history, and the walk that
    replaced it asked for all the wallets in one filter, which the node serves
    100,000 blocks at a time: 1,170 windows, far past the time budget. One
    wallet per filter is served 10,000,000 blocks at a time."""
    world = pepe_world()
    world.block = 117_250_000
    node = _ValueCappedNode(
        world,
        {
            WALLET.lower(): (50_955_037, [48_213, 48_214, 48_215, 48_216]),
            OUTSIDER.lower(): (30_000_000, [47_100]),
        },
    )
    env = env_for(world)
    env.client = node
    rows = lp.build_chain_positions(env, [WALLET.lower(), OUTSIDER.lower()], True)
    assert len(rows) == 5 and env.partial is False and env.warnings == []
    assert node.requests <= 8
    assert node.filters and all(not isinstance(f["topics"][-1], list) for f in node.filters)


def test_one_wallet_is_walked_at_the_span_the_node_states() -> None:
    world = pepe_world()
    world.block = 117_250_000
    node = _ValueCappedNode(world, {WALLET.lower(): (50_955_037, [48_213, 48_214, 48_215, 48_216])})
    env = env_for(world)
    env.client = node
    rows = lp.build_chain_positions(env, [WALLET.lower()], True)
    assert len(rows) == 4 and env.partial is False
    assert node.requests <= 4


def test_window_walk_stops_at_the_wallets_creation_block() -> None:
    world = pepe_world()
    world.block = 30_000_000
    asked: list[int] = []

    def batch(calls: list[dict[str, Any]], chunk_size: int = 20) -> list[Any]:
        asked.extend(int(c["params"][0]["fromBlock"], 16) for c in calls)
        return [[] for _ in calls]

    world.batch = batch  # type: ignore[method-assign]
    env = env_for(world)
    env.first_blocks = {WALLET.lower(): 29_750_000}
    lp.build_chain_positions(env, [WALLET.lower()], False)
    assert min(asked) == 29_750_000 and len(asked) == 3
    assert env.partial is True


def test_a_node_refusing_the_wide_span_falls_back_to_the_narrow_one() -> None:
    world = pepe_world()
    world.block = 26_000_000
    spans: list[int] = []

    def batch(calls: list[dict[str, Any]], chunk_size: int = 20) -> list[Any]:
        window = calls[0]["params"][0]
        span = int(window["toBlock"], 16) - int(window["fromBlock"], 16) + 1
        spans.append(span)
        if span > BASE.max_log_span:
            return [{"error": {"code": -32614, "message": "limited to a 2,000 range"}}]
        return [[] for _ in calls]

    world.batch = batch  # type: ignore[method-assign]
    env = env_for(world)
    lp.build_chain_positions(env, [WALLET.lower()], False)
    assert spans[0] == BASE.approval_log_span
    assert set(spans[1:]) == {BASE.max_log_span}


def test_a_busy_node_keeps_the_wide_span_in_the_window_walk() -> None:
    """A timeout or a 429 in a window batch is not a span refusal: the same
    windows are asked again instead of dropping to 2,000-block windows."""
    world = pepe_world()
    world.block = 30_000_000
    spans: list[int] = []
    busy: list[Any] = [
        RuntimeError("batch: HTTP 429"),
        {"error": {"code": -32000, "message": "log query timed out"}},
    ]

    def batch(calls: list[dict[str, Any]], chunk_size: int = 20) -> list[Any]:
        window = calls[0]["params"][0]
        spans.append(int(window["toBlock"], 16) - int(window["fromBlock"], 16) + 1)
        if busy:
            answer = busy.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return [answer] * len(calls)
        return [[] for _ in calls]

    world.batch = batch  # type: ignore[method-assign]
    env = env_for(world)
    env.first_blocks = {WALLET.lower(): 29_000_000}
    lp.build_chain_positions(env, [WALLET.lower()], False)
    # Two busy answers, then the same 100k windows served; never the 2k span.
    assert spans[:3] == [BASE.approval_log_span] * 3
    assert BASE.max_log_span not in spans


def test_full_history_falls_back_to_the_public_node_for_logs_only() -> None:
    world = pepe_world()
    world.block = 30_000_000
    world.batch = lambda calls, chunk_size=20: pytest.fail("no window walk needed")  # type: ignore[method-assign]
    topic = "0x" + "0" * 24 + WALLET[2:].lower()

    class PublicNode:
        def get_logs(self, params: dict[str, Any]) -> list[dict[str, Any]]:
            assert params["toBlock"] == "latest"
            return [
                {"topics": [LIB.abi.TOPIC_ERC721_TRANSFER, "0x" + "0" * 64, topic, hex(t)]}
                for t in (48_213, 48_214, 48_215, 48_216)
            ]

    env = env_for(world)
    env.log_client = PublicNode()
    rows = lp.build_chain_positions(env, [WALLET.lower()], False)
    assert len(rows) == 3 and env.partial is False


def test_the_configured_endpoint_is_asked_for_the_history_before_the_public_node() -> None:
    """Base, 2026-11-18: the public node answered every ``eth_getLogs`` with
    "request limit reached" while the user's own endpoint served the whole
    history in one request -- and was never asked."""
    world = pepe_world()
    world.logs_refused = False
    topic = "0x" + "0" * 24 + WALLET[2:].lower()
    asked: list[str] = []

    def get_logs(params: dict[str, Any]) -> list[dict[str, Any]]:
        asked.append(params["toBlock"])
        if len(params["topics"]) < 3:
            return []
        return [
            {"topics": [LIB.abi.TOPIC_ERC721_TRANSFER, "0x" + "0" * 64, topic, hex(t)]}
            for t in (48_213, 48_214, 48_215, 48_216)
        ]

    world.get_logs = get_logs  # type: ignore[method-assign]

    class PublicNode:
        url = "https://public.example"

        def get_logs(self, params: dict[str, Any]) -> list[dict[str, Any]]:
            pytest.fail("the public node is only asked when the configured one refuses")

    env = env_for(world)
    env.log_client = PublicNode()
    rows = lp.build_chain_positions(env, [WALLET.lower()], True)
    assert len(rows) == 4 and env.partial is False and asked == ["latest", "latest"]


# ── choosing the pool (live-chain regressions, 2026-09-27) ─────────────────


def _boner_price_at(tick: int) -> float:
    # BONER is currency1 (USDG sorts first): USDG per BONER at ``tick``.
    return float(LIB.v4_math.token_price_in_quote_at_tick(tick, True, 6, 18))


def test_pool_at_an_unusual_fee_is_found_through_initialize_logs() -> None:
    """``lp pool BONER --chain robinhood`` showed an empty 0.3 % pool at a stale price.

    The real BONER/USDG market is a 0.9 % pool the fee-tier guesses never
    produce; the Initialize log names it, and it has the liquidity.
    """
    world = boner_world()
    world.logs_refused = False
    live = next(p for p in world.pools.values() if p.key["fee"] == 9000)
    card = lp.build_pool(env_for(world, prices=world_prices(world, {USDG: 1.0})), BONER)
    assert card["pool"]["poolId"] == live.pool_id and card["pool"]["feePct"] == "0.9%"
    assert int(card["pool"]["liquidity"]) > 0 and card["pool"]["tvlUsd"] > 0
    # BONER has no listed price: it is implied by the pool that trades, not the stale one.
    price = _boner_price_at(306_994)
    assert price == pytest.approx(0.0466, rel=0.01)
    assert card["pool"]["priceUsd"] == pytest.approx(price)
    assert card["pool"]["mcapUsd"] == pytest.approx(price * 1e9, rel=1e-6)
    assert any("2 V4 pools hold BONER (1 with active liquidity)" in w for w in card["warnings"])


def _spoil_scan(monkeypatch: pytest.MonkeyPatch, pool_id: str, *, check_fails: bool) -> None:
    """The bitmap walk of ``pool_id`` comes back truncated (and, if asked, failing its check)."""
    orig = LIB.v4_pool.walk_tick_ranges

    def walk(client: Any, chain: Any, pid: str, state: Any, **kwargs: Any) -> Any:
        res = orig(client, chain, pid, state, **kwargs)
        if str(pid).lower() == pool_id.lower():
            res["truncated"] = {"scannedWords": 600, "fullWords": 6932}
            if check_fails:
                res["check"] = {"ok": False}
        return res

    monkeypatch.setattr(LIB.v4_pool, "walk_tick_ranges", walk)


@pytest.mark.parametrize("build", [lp.build_pool, lp.build_ranges])
def test_a_losing_pools_scan_does_not_mark_the_chosen_card(
    monkeypatch: pytest.MonkeyPatch, build: Any
) -> None:
    """``lp pool ETH --chain base`` showed the deepest pool with a self-check failure
    and two partial-scan warnings that belonged to the pools it had beaten."""
    world = boner_world()
    world.logs_refused = False
    live = next(p for p in world.pools.values() if p.key["fee"] == 9000)
    stale = next(p for p in world.pools.values() if p.key["fee"] == 3000)
    _spoil_scan(monkeypatch, stale.pool_id, check_fails=True)
    card = build(env_for(world, prices=world_prices(world, {USDG: 1.0})), BONER)
    assert card["pool"]["poolId"] == live.pool_id
    assert card["partialScan"] is False
    assert not any("self-check" in w or "partial scan" in w for w in card["warnings"])
    assert any("2 V4 pools hold BONER (1 with active liquidity)" in w for w in card["warnings"])
    assert sum("other pools were not fully scanned" in w for w in card["warnings"]) == 1
    if "scan" in card:
        assert card["scan"]["truncated"] is False


@pytest.mark.parametrize("build", [lp.build_pool, lp.build_ranges])
def test_the_chosen_pools_own_truncation_still_flags(
    monkeypatch: pytest.MonkeyPatch, build: Any
) -> None:
    world = boner_world()
    world.logs_refused = False
    live = next(p for p in world.pools.values() if p.key["fee"] == 9000)
    _spoil_scan(monkeypatch, live.pool_id, check_fails=True)
    card = build(env_for(world, prices=world_prices(world, {USDG: 1.0})), BONER)
    assert card["pool"]["poolId"] == live.pool_id
    assert card["partialScan"] is True
    assert any("partial scan: 600 of 6932" in w for w in card["warnings"])
    assert any("self-check failed" in w for w in card["warnings"])
    assert not any("other pools were not fully scanned" in w for w in card["warnings"])


def test_empty_pool_never_beats_a_live_one_even_when_nothing_is_priced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no prices a live pool's TVL is ``None`` and an empty one's ``0.0``; the
    old sort put ``0.0`` first. Both pools here sit in the conventional tiers."""
    monkeypatch.setattr(lp, "STABLE_SYMBOLS", frozenset())  # not even USDG at par
    world = boner_world(live_fee=(10_000, 200))
    live = next(p for p in world.pools.values() if p.key["fee"] == 10_000)
    card = lp.build_pool(env_for(world, prices=world_prices(world, {})), BONER)
    assert card["pool"]["poolId"] == live.pool_id
    assert card["pool"]["tvlUsd"] is None and card["pool"]["priceUsd"] is None


def test_known_quote_pool_beats_a_deeper_pool_against_another_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = boner_world(live_fee=(10_000, 200))
    world.logs_refused = False
    other = "0x7777777777777777777777777777777777777777"
    world.tokens[other] = ("HIMS", 18, 10**27)
    c0, c1 = sorted([BONER, other])
    key = dict(
        LIB.v4_pool.normalize_pool_key(
            {
                "currency0": c0,
                "currency1": c1,
                "fee": 3000,
                "tickSpacing": 60,
                "hooks": lp.NATIVE_ADDRESS,
            }
        )
    )
    deep = world.add_pool(key, tick=0)
    world.add_position(deep, 9_000_001, -600, 600, 10**30, OUTSIDER)
    prices = world_prices(world, {USDG: 1.0, other: 1.0})
    env = env_for(world, prices=prices)
    # HIMS is priced but not one of the chain's quote assets: shown only on request.
    orig = LIB.v4_pool.derive_vanilla_candidates

    def with_other(chain: Any, token: str, quotes: Any = None) -> Any:
        return orig(chain, token, quotes) + orig(chain, token, [other])

    monkeypatch.setattr(LIB.v4_pool, "derive_vanilla_candidates", with_other)
    card = lp.build_pool(env, BONER)
    # The HIMS pool holds far more, and is priced; the USDG pool is still the answer.
    assert card["quote"]["symbol"] == "USDG"
    assert any("3 V4 pools hold BONER" in w for w in card["warnings"])


def test_empty_chosen_pool_takes_the_listed_price_not_its_stale_one() -> None:
    world = boner_world()  # logs refused: only the empty 0.3 % pool is visible
    prices = world_prices(world, {USDG: 1.0, BONER: 0.04673})
    card = lp.build_pool(env_for(world, prices=prices), BONER)
    assert card["pool"]["feePct"] == "0.3%" and card["pool"]["liquidity"] == "0"
    assert _boner_price_at(316_386) == pytest.approx(0.0182, rel=0.01)
    assert card["pool"]["priceUsd"] == pytest.approx(0.04673)
    assert card["pool"]["mcapUsd"] == pytest.approx(0.04673 * 1e9)


def test_pool_id_at_an_unusual_fee_recovers_its_key_from_the_initialize_log() -> None:
    world = boner_world()
    world.logs_refused = False
    live = next(p for p in world.pools.values() if p.key["fee"] == 9000)
    env = env_for(world, prices=world_prices(world, {USDG: 1.0}))
    # The price index knows the pair, but no conventional fee tier hashes to the id.
    env.pair_tokens = lambda pid: [BONER.lower(), USDG.lower()]
    card = lp.build_pool(env, live.pool_id)
    assert card["pool"]["poolId"] == live.pool_id and card["token"]["symbol"] == "BONER"


def test_pool_id_nobody_can_resolve_is_pool_key_unknown() -> None:
    world = boner_world()  # the node refuses full-history logs
    live = next(p for p in world.pools.values() if p.key["fee"] == 9000)
    env = env_for(world)
    env.pair_tokens = lambda pid: []
    with pytest.raises(TradingError) as err:
        lp.build_pool(env, live.pool_id)
    assert err.value.code == "trading.lp.pool_key_unknown"
    assert "agentos trade lp pool <token> --chain robinhood" in str(err.value)


class _Node:
    """A node whose ``eth_getLogs`` fails with ``errors`` in turn, then serves ``logs``."""

    def __init__(self, url: str, *errors: Exception, logs: list[Any] | None = None) -> None:
        self.url, self.errors, self.logs, self.asked = url, list(errors), logs or [], 0

    def get_logs(self, params: dict[str, Any]) -> list[Any]:
        self.asked += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.logs


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("eth_getLogs: log query timed out", "transient"),
        ("eth_getLogs: HTTP 503", "transient"),
        ("eth_getLogs: HTTP 429", "transient"),
        ("Temporary internal error. Please retry", "transient"),
        ("too many requests, slow down", "transient"),
        ("context deadline exceeded", "transient"),
        ("eth_getLogs: eth_getLogs is limited to a 2,000 range", "refused"),
        ("range over 100000 blocks is not supported", "refused"),
        ("query exceeds max block span 10000", "refused"),
        ("block range too large", "refused"),
        ("query returned more than 10000 results", "oversized"),
        ("query returned more than 10,000 results", "oversized"),
        ("too many results, narrow the filter", "oversized"),
        (
            "Log response size exceeded. You can make eth_getLogs requests with up to a 2K "
            "block range",
            "oversized",
        ),
        ("something else entirely", "unknown"),
    ],
)
def test_log_failures_are_classified_by_message(message: str, kind: str) -> None:
    assert lp.log_failure_kind(LIB.rpc.RpcError("eth_getLogs", {"message": message})) == kind


@pytest.mark.parametrize(
    ("message", "span"),
    [
        ("eth_getLogs is limited to a 2,000 range", 2_000),
        ("range over 100000 blocks is not supported", 100_000),
        ("query exceeds max block span 10000", 10_000),
        ("block range too large", None),
        (
            "query spans 117246425 blocks (9070 to 117255494), but only 10000000 are "
            "allowed for this request; narrow the block range",
            10_000_000,
        ),
        (
            "query spans 9998977 blocks (107256509 to 117255485), but only 100000 are allowed "
            "for this request; narrow the block range, or send one value per position",
            100_000,
        ),
    ],
)
def test_a_span_refusal_states_its_span(message: str, span: int | None) -> None:
    assert lp.stated_span(LIB.rpc.RpcError("eth_getLogs", {"message": message})) == span


def test_full_range_refusal_is_remembered_but_a_timeout_is_not() -> None:
    """2026-09-27 18:56: Robinhood's "log query timed out" (a JSON-RPC error) was
    remembered as a span refusal for ten minutes, and BONER resolved to its empty
    0.3 % pool in every read after it."""
    env = env_for(pepe_world())
    refusing = _Node(
        "https://refuses.example",
        LIB.rpc.RpcError("eth_getLogs", {"message": "eth_getLogs is limited to a 2,000 range"}),
    )
    env.log_client = refusing
    assert lp.full_range_logs(env, WETH, []) is None
    assert lp.full_range_logs(env, WETH, []) is None
    assert refusing.asked == 1 and "https://refuses.example" in lp._full_range_refused
    timeout = LIB.rpc.RpcError("eth_getLogs", {"code": -32000, "message": "log query timed out"})
    timing_out = _Node("https://busy.example", *[timeout] * 4)
    env.log_client = timing_out
    assert lp.full_range_logs(env, WETH, []) is None
    assert lp.full_range_logs(env, WETH, []) is None
    # Each read asked twice (one retry) and nothing was remembered.
    assert timing_out.asked == 4 and "https://busy.example" not in lp._full_range_refused


def _boner_logs(world: Any, *, full: Exception | None) -> tuple[Any, list[tuple[int, int]]]:
    """``get_logs`` that fails the full history with ``full`` and serves windows."""
    live = next(p for p in world.pools.values() if int(p.key["fee"]) == 9000)
    inits = world.initialize_logs()
    windows: list[tuple[int, int]] = []

    def get_logs(params: dict[str, Any]) -> list[Any]:
        if params["toBlock"] == "latest" and full is not None:
            raise full
        low = int(params["fromBlock"], 16)
        high = world.block if params["toBlock"] == "latest" else int(params["toBlock"], 16)
        windows.append((low, high))
        at = {live.pool_id: world.block - 1_000_000}
        return [
            e
            for e in inits
            if _matches(e["topics"], params["topics"]) and low <= at.get(e["topics"][1], 0) <= high
        ]

    world.logs_refused = False
    world.get_logs = get_logs  # type: ignore[method-assign]
    return live, windows


def test_a_result_size_refusal_is_not_remembered_and_the_window_still_finds_the_pool() -> None:
    """Code review 2026-09-27: "query returned more than 10000 results" (one
    filter's answer) was remembered as the node's span rule for ten minutes,
    and BONER's 0.9 % pool answered ``trading.lp.not_found`` in every read."""
    world = boner_world()
    oversized = LIB.rpc.RpcError(
        "eth_getLogs", {"message": "query returned more than 10000 results"}
    )
    live, windows = _boner_logs(world, full=oversized)
    env = env_for(world, prices=world_prices(world, {USDG: 1.0}))
    env.log_client = world
    world.url = "https://public.example"  # type: ignore[attr-defined]
    card = lp.build_pool(env, BONER)
    assert card["pool"]["poolId"] == live.pool_id and card["pool"]["feePct"] == "0.9%"
    assert lp._full_range_refused == {}
    assert windows and max(high - low + 1 for low, high in windows) <= lp.RECENT_LOG_CHUNK
    assert any("matched too many logs" in w for w in card["warnings"])


def test_a_span_refusal_is_remembered_and_still_searches_the_recent_window() -> None:
    world = boner_world()
    refusal = LIB.rpc.RpcError(
        "eth_getLogs", {"message": "range over 100000 blocks is not supported"}
    )
    live, windows = _boner_logs(world, full=refusal)
    world.url = "https://metered.example"  # type: ignore[attr-defined]
    prices = world_prices(world, {USDG: 1.0})
    env = env_for(world, prices=prices)
    env.log_client = world
    assert lp.build_pool(env, BONER)["pool"]["poolId"] == live.pool_id
    assert lp._full_range_refused["https://metered.example"][1] == 100_000
    # Remembered: the next read skips the full-history request, not the search.
    lp._token_pools_path(env).unlink()  # not found again from the earlier read
    _boner_logs(world, full=AssertionError("the refused full history was asked again"))
    env = env_for(world, prices=prices)
    env.log_client = world
    card = lp.build_pool(env, BONER)
    assert card["pool"]["poolId"] == live.pool_id and card["partialScan"] is True
    assert any("was refused" in w for w in card["warnings"])


def test_a_node_limited_to_narrow_spans_is_not_walked() -> None:
    """Base's public node serves 2,000 blocks: a 2M-block walk would be 1,000 requests."""
    env = env_for(pepe_world())
    node = _Node(
        "https://mainnet.base.example",
        LIB.rpc.RpcError("eth_getLogs", {"message": "eth_getLogs is limited to a 2,000 range"}),
    )
    env.log_client = node
    assert lp.full_range_logs(env, WETH, [], recent_fallback=True) is None
    assert lp.full_range_logs(env, WETH, [], recent_fallback=True) is None
    assert node.asked == 1 and env.partial is False and env.warnings == []


def test_concurrent_identical_log_requests_share_one_answer() -> None:
    """Nine desk turns reading one wallet sent nine full-history requests at
    once, and Robinhood Chain's node answered most of them with HTTP 429."""
    import threading
    import time

    class Slow(_Node):
        def get_logs(self, params: dict[str, Any]) -> list[Any]:
            time.sleep(0.2)
            return super().get_logs(params)

    node = Slow("https://public.example", logs=[{"topics": ["0x01"]}])
    envs = [env_for(pepe_world()) for _ in range(6)]
    got: list[Any] = []
    for env in envs:
        env.log_client = node
    threads = [
        threading.Thread(target=lambda e=env: got.append(lp.full_range_logs(e, WETH, [None])))
        for env in envs
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert node.asked == 1 and got == [[{"topics": ["0x01"]}]] * 6
    # Shared for a few seconds after, too; another question is asked on its own.
    assert lp.full_range_logs(envs[0], WETH, [None]) == [{"topics": ["0x01"]}]
    assert node.asked == 1
    lp.full_range_logs(envs[0], PEPE, [None])
    assert node.asked == 2


def test_a_timeout_is_retried_once() -> None:
    env = env_for(pepe_world())
    node = _Node(
        "https://busy.example",
        LIB.rpc.RpcError("eth_getLogs", {"message": "log query timed out"}),
        logs=[{"topics": []}],
    )
    env.log_client = node
    assert lp.full_range_logs(env, WETH, []) == [{"topics": []}]
    assert node.asked == 2


def test_two_timeouts_fall_back_to_the_recent_blocks() -> None:
    world = boner_world()
    live = next(p for p in world.pools.values() if int(p.key["fee"]) == 9000)
    windows: list[tuple[int, int]] = []
    inits = world.initialize_logs()

    def get_logs(params: dict[str, Any]) -> list[Any]:
        if params["toBlock"] == "latest":
            raise LIB.rpc.RpcError("eth_getLogs", {"message": "log query timed out"})
        low, high = int(params["fromBlock"], 16), int(params["toBlock"], 16)
        windows.append((low, high))
        # The live pool was initialised a million blocks ago, the stale one long before.
        at = {live.pool_id: world.block - 1_000_000}
        return [
            e
            for e in inits
            if _matches(e["topics"], params["topics"]) and low <= at.get(e["topics"][1], 0) <= high
        ]

    world.logs_refused = False
    world.get_logs = get_logs  # type: ignore[method-assign]
    card = lp.build_pool(env_for(world, prices=world_prices(world, {USDG: 1.0})), BONER)
    assert card["pool"]["poolId"] == live.pool_id
    assert max(high - low + 1 for low, high in windows) <= lp.RECENT_LOG_CHUNK
    assert min(low for low, _ in windows) == world.block - lp.RECENT_LOG_WINDOW + 1
    assert any("timed out twice" in w for w in card["warnings"]) and card["partialScan"] is True


def test_a_pool_found_once_is_found_after_the_logs_fail() -> None:
    world = boner_world()
    live = next(p for p in world.pools.values() if int(p.key["fee"]) == 9000)
    world.logs_refused = False
    prices = world_prices(world, {USDG: 1.0})
    assert lp.build_pool(env_for(world, prices=prices), BONER)["pool"]["poolId"] == live.pool_id

    def timing_out(params: dict[str, Any]) -> list[Any]:
        raise LIB.rpc.RpcError("eth_getLogs", {"message": "log query timed out"})

    world.get_logs = timing_out  # type: ignore[method-assign]
    card = lp.build_pool(env_for(world, prices=prices), BONER)
    assert card["pool"]["poolId"] == live.pool_id
    assert card["pool"]["feePct"] == "0.9%"


# ── finding a wallet's positions (live-chain regressions) ──────────────────


def test_net_holdings_from_logs_verify_only_what_is_still_held() -> None:
    """A market-making wallet received 3,387 NFTs and held 14: every one of the
    3,387 was checked with ``ownerOf`` and the scan took ~50 s."""
    world = pepe_world()
    logs = []
    for i, token_id in enumerate(range(100_000, 100_400)):
        logs.append(transfer_log(token_id, lp.NATIVE_ADDRESS, WALLET, 1_000 + i))
        logs.append(transfer_log(token_id, WALLET, OUTSIDER, 2_000 + i))
    for token_id in (48_213, 48_214, 48_215, 48_216):
        logs.append(transfer_log(token_id, lp.NATIVE_ADDRESS, WALLET, 5_000, token_id % 7))
    asked = serve_transfer_logs(world, logs)
    env = env_for(world)  # no indexer
    rows = lp.build_chain_positions(env, [WALLET.lower()], True)
    assert len(rows) == 4 and env.partial is False
    assert world.calls.count("ownerOf") == 4
    # Both directions were asked for, over the whole history.
    padded = LIB.hexutil.pad(WALLET.lower(), size=32)
    assert [LIB.abi.TOPIC_ERC721_TRANSFER, None, [padded]] in asked
    assert [LIB.abi.TOPIC_ERC721_TRANSFER, [padded]] in asked


def test_received_ids_are_checked_newest_first_when_the_outgoing_logs_fail() -> None:
    world = pepe_world()
    logs = [
        transfer_log(t, lp.NATIVE_ADDRESS, WALLET, 1_000 + t - 100_000)
        for t in range(100_000, 101_000)
    ]
    logs += [
        transfer_log(t, lp.NATIVE_ADDRESS, WALLET, 900_000)
        for t in (48_213, 48_214, 48_215, 48_216)
    ]
    serve_transfer_logs(world, logs)
    served = world.get_logs

    def only_incoming(params: dict[str, Any]) -> list[Any]:
        if len(params["topics"]) == 2:
            raise LIB.rpc.RpcError("eth_getLogs", {"message": "busy"})
        return served(params)

    world.get_logs = only_incoming  # type: ignore[method-assign]
    env = env_for(world)
    rows = lp.build_chain_positions(env, [WALLET.lower()], True)
    assert len(rows) == 4 and env.partial is False
    # The four live ones are the newest receipts: one wave finds them all.
    assert world.calls.count("ownerOf") <= lp._PositionSearch.WAVE


def test_fees_total_is_unavailable_when_any_fee_is_unpriced() -> None:
    """A big-wallet read that lost its prices showed "FEES $0.00": the rows with
    no fees counted as $0 and the unpriced ones were skipped, so the one number a
    trader acts on was plausible and wrong."""
    world = pepe_world()
    env = env_for(world, nft_ids=world.indexer)
    rows = lp.build_chain_positions(env, [WALLET.lower()], False)
    assert len(rows) >= 2
    priced = lp.positions_payload([env], [], rows)
    assert priced["totals"]["feesUsd"] == pytest.approx(
        sum((r.get("fees") or {}).get("usd") or 0.0 for r in rows)
    )
    # One unpriced fee among priced ones: left out, and said so.
    with_fees = [r for r in rows if r.get("fees")]
    assert len(with_fees) >= 2
    with_fees[0]["fees"]["usd"] = 10.0
    with_fees[1]["fees"]["usd"] = 5.0
    with_fees[0]["fees"]["usd"] = None
    partial = lp.positions_payload([env], [], rows)
    assert partial["totals"]["feesUsd"] == pytest.approx(
        sum((r.get("fees") or {}).get("usd") or 0.0 for r in rows)
    )
    assert any("left out of the fees total" in w for w in partial["warnings"])
    # Every price lost: the rows without fees would sum to $0.00, which is not a total.
    for r in with_fees:
        if (r["fees"].get("usd") or 0) > 0:
            r["fees"]["usd"] = None
    card = lp.positions_payload([env], [], rows)
    assert card["totals"]["feesUsd"] is None
    assert any("fees total is unavailable" in w for w in card["warnings"])
    # The value total is unaffected by an unpriced fee.
    assert card["totals"]["valueUsd"] == priced["totals"]["valueUsd"]


def test_positions_card_is_capped_and_its_totals_cover_everything() -> None:
    """A launch bot's 1,195 positions made a 1.19 MB payload the gateway's
    websocket refused (1009, frame over 1 MiB)."""
    world = pepe_world()
    pepe = next(iter(world.pools.values()))
    for i in range(52):
        lower = 150_000 + 400 * (i % 50)
        world.add_position(pepe, 70_000 + i, lower, lower + 2_000, 10**21 + i, WALLET)
    env = env_for(world, nft_ids=world.indexer)
    rows = lp.build_chain_positions(env, [WALLET.lower()], False)
    card = lp.positions_payload([env], [], rows)
    assert len(rows) == 55  # 52 + the wallet's three open ones (the closed one is left out)
    assert len(card["positions"]) == lp.MAX_POSITIONS_LISTED == 50
    assert card["totals"]["count"] == 55
    assert card["totals"]["outOfRange"] == sum(
        1 for r in rows if r["status"] in ("above-range", "below-range")
    )
    priced = [r["valueUsd"] for r in rows if r["valueUsd"] is not None]
    assert card["totals"]["valueUsd"] == pytest.approx(sum(priced))
    assert any("showing 50 of 55 positions" in w for w in card["warnings"])
    assert card["positions"] == lp.sort_positions(rows)[:50]
    # Exactly the fields docs/lp-cards.md lists for a Position, and a bounded size.
    doc_fields = {
        "chain",
        "tokenId",
        "owner",
        "token",
        "quote",
        "pool",
        "range",
        "status",
        "liquidity",
        "principal",
        "fees",
        "valueUsd",
        "band",
        "distancePct",
    }
    assert all(set(p) == doc_fields for p in card["positions"])
    size = len(json.dumps(card))
    assert size < 100_000, size


def test_fees_are_read_in_one_batch_for_every_position() -> None:
    world = pepe_world()
    batches: list[int] = []
    multicall = world.multicall

    def counting(calls: list[dict[str, Any]], **kw: Any) -> list[dict[str, Any]]:
        if any(c["functionName"] == "getFeeGrowthInside" for c in calls):
            batches.append(len(calls))
        return multicall(calls, **kw)

    world.multicall = counting  # type: ignore[method-assign]
    env = env_for(world, nft_ids=world.indexer)
    rows = lp.build_chain_positions(env, [WALLET.lower()], True)
    live = [r for r in rows if int(r["liquidity"]) > 0]
    assert batches == [2 * len(live)]
    fees = next(r for r in rows if r["tokenId"] == "48214")["fees"]
    assert fees["base"]["raw"] == str(1_850_000 * 10**18)


# ── a token address where a wallet belongs ─────────────────────────────────


def _positions_env(world_by_chain: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_env(service: Any, spec: Any, loop: Any) -> lp.ChainEnv:
        world = world_by_chain[spec.key]
        return lp.ChainEnv(
            spec=spec, client=world, prices=world_prices(world), lib=LIB, nft_ids=world.indexer
        )

    monkeypatch.setattr(lp, "_engine_env", fake_env)


async def test_token_contract_as_wallet_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    worlds = {"base": pepe_world(), "robinhood": empty_world(ROBINHOOD)}
    worlds["base"].codes[PEPE.lower()] = "0x6080604052"
    _positions_env(worlds, monkeypatch)
    with pytest.raises(TradingError) as err:
        await lp.lp_positions(_Service(), wallets=[PEPE])  # type: ignore[arg-type]
    assert err.value.code == "trading.lp.not_a_wallet"
    checksummed = LIB.hexutil.checksum_address(PEPE)
    assert str(err.value) == (
        f"{checksummed} is a token contract (PEPE); use "
        f"`agentos trade lp pool {checksummed} --chain base` for its liquidity"
    )


async def test_smart_wallet_with_code_is_still_scanned(monkeypatch: pytest.MonkeyPatch) -> None:
    worlds = {"base": pepe_world(), "robinhood": empty_world(ROBINHOOD)}
    # A Safe has code but answers no symbol(); an EIP-7702 EOA carries a delegation.
    worlds["base"].codes[WALLET.lower()] = "0x608060405273"
    worlds["base"].codes[OUTSIDER.lower()] = "0xef0100" + "ab" * 20
    _positions_env(worlds, monkeypatch)
    card = await lp.lp_positions(_Service(), wallets=[WALLET, OUTSIDER])  # type: ignore[arg-type]
    assert card["totals"]["count"] == 4
    assert {p["owner"]["address"].lower() for p in card["positions"]} == {
        WALLET.lower(),
        OUTSIDER.lower(),
    }


async def test_token_contract_that_holds_positions_is_scanned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An LP vault issuing ERC-20 shares is a token and a holder: scan it."""
    worlds = {"base": pepe_world(), "robinhood": empty_world(ROBINHOOD)}
    worlds["base"].codes[WALLET.lower()] = "0x6080"
    worlds["base"].tokens[WALLET.lower()] = ("VAULT", 18, 10**24)
    _positions_env(worlds, monkeypatch)
    card = await lp.lp_positions(_Service(), wallets=[WALLET])  # type: ignore[arg-type]
    assert card["totals"]["count"] == 3


# ── the time budget (2026-09-27: 1,200 positions took 65 s; the exec timeout is 45 s) ──


def test_positions_stop_at_the_deadline_and_name_what_was_skipped() -> None:
    import time

    world = pepe_world()
    pepe = next(iter(world.pools.values()))
    for i in range(40):
        world.add_position(pepe, 70_000 + i, 150_000, 152_000, 10**21, WALLET)
    held = world.indexer(WALLET)

    def slow_indexer(owner: str, on_page: Any = None, deadline: Any = None) -> list[int]:
        on_page(held[:10])  # page one arrives at once ...
        time.sleep(3)  # ... the rest never does within the budget
        return held

    world.batch = lambda calls, chunk_size=20: [[] for _ in calls]  # type: ignore[method-assign]
    env = env_for(world, nft_ids=slow_indexer)
    env.budget_s = 1.0
    env.deadline = time.monotonic() + 0.6
    began = time.monotonic()
    rows = lp.build_chain_positions(env, [WALLET.lower()], True)
    assert time.monotonic() - began < 2.0
    assert len(rows) == 10 and env.partial is True
    budget = next(w for w in env.warnings if "time budget" in w)
    assert "1 s time budget ran out before the indexer's remaining pages" in budget
    assert "--budget-seconds" in budget
    assert any("found 10 of the 44 position(s)" in w for w in env.warnings)


def test_a_verify_wave_stuck_past_the_deadline_cuts_discovery_not_the_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verify wave (or log batch) in flight at the deadline was waited for, and
    the chain overran its hard budget with 970 positions already verified."""
    import time

    monkeypatch.setattr(lp, "DISCOVERY_GRACE_S", 0.2)
    world = pepe_world()
    pepe = next(iter(world.pools.values()))
    for i in range(40):
        world.add_position(pepe, 70_000 + i, 150_000, 152_000, 10**21, WALLET)
    held = world.indexer(WALLET)
    multicall = world.multicall

    def stalls_on_the_second_page(calls: list[dict[str, Any]], **kw: Any) -> list[Any]:
        if any(c["functionName"] == "ownerOf" and int(c["args"][0]) in held[10:] for c in calls):
            time.sleep(2.0)
        return multicall(calls, **kw)

    def indexer(owner: str, on_page: Any = None, deadline: Any = None) -> list[int]:
        on_page(held[:10])
        on_page(held[10:])
        return []

    world.multicall = stalls_on_the_second_page  # type: ignore[method-assign]
    world.batch = lambda calls, chunk_size=20: [[] for _ in calls]  # type: ignore[method-assign]
    env = env_for(world, nft_ids=indexer)
    env.budget_s = 1.0
    env.deadline = time.monotonic() + 0.4
    began = time.monotonic()
    rows = lp.build_chain_positions(env, [WALLET.lower()], True)
    assert time.monotonic() - began < 1.2
    assert len(rows) == 10 and env.partial is True
    assert any("found 10 of the 44 position(s)" in w for w in env.warnings)
    assert any(
        "1 s time budget ran out before the indexer's remaining pages" in w for w in env.warnings
    )


async def test_a_chain_past_the_hard_budget_answers_with_what_it_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-09-27: 0xbf05… came back "No Uniswap V4 positions" (count 0) while
    the same card's warnings said "Base: found 970 of the 1205 position(s)"."""
    import time

    worlds = {"base": pepe_world(), "robinhood": empty_world(ROBINHOOD)}
    base_world = worlds["base"]
    pepe = next(iter(base_world.pools.values()))
    for i in range(20):
        base_world.add_position(pepe, 70_000 + i, 150_000, 152_000, 10**21, WALLET)
    table = world_prices(base_world, {WETH: 2512.37, PEPE: 0.00002})

    def stalled_prices(addresses: list[str]) -> dict[str, float | None]:
        time.sleep(3.0)  # the price source never answers within the hard budget
        return table(addresses)

    def fake_env(service: Any, spec: Any, loop: Any) -> lp.ChainEnv:
        world = worlds[spec.key]
        return lp.ChainEnv(
            spec=spec,
            client=world,
            prices=stalled_prices if spec.key == "base" else world_prices(world),
            lib=LIB,
            nft_ids=world.indexer,
        )

    monkeypatch.setattr(lp, "_engine_env", fake_env)
    monkeypatch.setattr(lp, "POSITIONS_BUDGET_MIN_S", 0.1)
    monkeypatch.setattr(lp, "POSITIONS_RESERVE_MIN_S", 0.1)
    monkeypatch.setattr(lp, "POSITIONS_HARD_GRACE_S", 0.2)
    began = time.monotonic()
    card = await lp.lp_positions(_Service(), wallets=[WALLET], budget_s=0.6)  # type: ignore[arg-type]
    assert time.monotonic() - began < 2.5
    open_ids = {
        str(t)
        for t, p in base_world.positions.items()
        if p.owner == WALLET.lower() and p.liquidity > 0
    }
    assert card["partialScan"] is True
    assert card["totals"]["count"] == len(open_ids) == 23
    assert {r["tokenId"] for r in card["positions"]} == open_ids
    assert any("Base did not answer within the 0.6 s time budget" in w for w in card["warnings"])
    # The fees were read while the prices hung: the rows carry them.
    assert not any("uncollected fees were not read in time" in w for w in card["warnings"])
    assert all(r["fees"] is not None for r in card["positions"])


async def test_positions_found_before_their_pools_were_read_are_still_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    worlds = {"base": pepe_world(), "robinhood": empty_world(ROBINHOOD)}
    _positions_env(worlds, monkeypatch)
    pool_states = lp.pool_states
    calls = {"n": 0}

    def first_read_hangs(env: lp.ChainEnv, candidates: list[dict[str, Any]]) -> list[Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            time.sleep(2.0)
        return pool_states(env, candidates)

    monkeypatch.setattr(lp, "pool_states", first_read_hangs)
    monkeypatch.setattr(lp, "POSITIONS_BUDGET_MIN_S", 0.1)
    monkeypatch.setattr(lp, "POSITIONS_RESERVE_MIN_S", 0.1)
    monkeypatch.setattr(lp, "POSITIONS_HARD_GRACE_S", 0.2)
    card = await lp.lp_positions(
        _Service(),  # type: ignore[arg-type]
        chains=[BASE],
        wallets=[WALLET],
        budget_s=0.6,
    )
    assert card["partialScan"] is True and card["totals"]["count"] == 3
    assert len(card["positions"]) == 3
    assert any("Base did not answer within the 0.6 s time budget" in w for w in card["warnings"])
    assert any("uncollected fees were not read in time" in w for w in card["warnings"])


def test_price_lookups_are_bounded_to_the_listed_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lp, "PRICE_LOOKUP_MAX", 1)
    monkeypatch.setattr(lp, "MAX_POSITIONS_LISTED", 1)
    world = pepe_world()
    asked: list[list[str]] = []
    table = world_prices(world, {WETH: 2512.37, PEPE: 0.00002})

    def prices(addresses: list[str]) -> dict[str, float | None]:
        asked.append(sorted(addresses))
        return table(addresses)

    env = env_for(world, prices=prices, nft_ids=world.indexer)
    rows = lp.build_chain_positions(env, [WALLET.lower()], False)
    # The quote assets, then only the listed row's token: never FOO or BAR.
    assert asked == [[WETH.lower()], [PEPE.lower()]]
    top = lp.sort_positions(rows)[0]
    assert top["token"]["symbol"] == "PEPE" and top["valueUsd"] is not None
    foo = next(r for r in rows if r["tokenId"] == "48215")
    assert foo["valueUsd"] is None
    assert any("USD prices were looked up for 1 of the 3 tokens" in w for w in env.warnings)


def test_an_owner_of_the_node_failed_is_asked_once_more() -> None:
    world = pepe_world()
    multicall = world.multicall
    failed: set[int] = set()

    def flaky(calls: list[dict[str, Any]], **kw: Any) -> list[dict[str, Any]]:
        out = multicall(calls, **kw)
        for call, result in zip(calls, out, strict=True):
            token = int(call["args"][0]) if call["functionName"] == "ownerOf" else None
            if token == 48_214 and token not in failed:
                failed.add(token)
                result.update(status="failure", result=None, error=lp._NODE_FAULT)
        return out

    world.multicall = flaky  # type: ignore[method-assign]
    env = env_for(world, nft_ids=world.indexer)
    rows = lp.build_chain_positions(env, [WALLET.lower()], True)
    assert {r["tokenId"] for r in rows} >= {"48214"} and env.partial is False


def test_positions_budget_is_bounded() -> None:
    assert lp.positions_budget(None) == lp.POSITIONS_BUDGET_S == 25.0
    assert lp.positions_budget(60) == 60.0
    for bad in (1, 10_000, float("nan")):
        with pytest.raises(TradingError) as err:
            lp.positions_budget(bad)
        assert err.value.code == "trading.invalid"


async def test_a_chain_past_the_hard_budget_is_dropped_not_waited_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    worlds = {"base": pepe_world(), "robinhood": empty_world(ROBINHOOD)}
    _positions_env(worlds, monkeypatch)
    monkeypatch.setattr(lp, "POSITIONS_BUDGET_MIN_S", 0.1)
    monkeypatch.setattr(lp, "POSITIONS_HARD_GRACE_S", 0.2)
    build = lp.build_chain_positions

    def stuck_on_base(env: lp.ChainEnv, owners: list[str], closed: bool) -> list[Any]:
        if env.spec.key == "base":
            time.sleep(1.5)
        return build(env, owners, closed)

    monkeypatch.setattr(lp, "build_chain_positions", stuck_on_base)
    began = time.monotonic()
    card = await lp.lp_positions(_Service(), wallets=[WALLET], budget_s=0.3)  # type: ignore[arg-type]
    assert time.monotonic() - began < 1.2
    assert card["partialScan"] is True and card["totals"]["count"] == 0
    assert any("Base did not answer within the 0.3 s time budget" in w for w in card["warnings"])


async def test_each_named_chain_is_read_once(monkeypatch: pytest.MonkeyPatch) -> None:
    worlds = {"base": pepe_world(), "robinhood": empty_world(ROBINHOOD)}
    _positions_env(worlds, monkeypatch)
    card = await lp.lp_positions(
        _Service(),  # type: ignore[arg-type]
        chains=[BASE, ROBINHOOD, BASE],
        wallets=[WALLET],
    )
    assert [c["key"] for c in card["chains"]] == ["base", "robinhood"]
    assert card["totals"]["count"] == 3


# ── fee tiers and pairs (``--fee``, ``TOKEN/QUOTE``) ────────────────────────


@pytest.mark.parametrize(
    ("value", "fee"),
    [
        ("0.05", 500),
        ("0.05%", 500),
        (" 0.05 % ", 500),
        (0.05, 500),
        ("500", 500),
        (500, 500),
        ("0.3", 3000),
        ("0.3%", 3000),
        ("3000", 3000),
        ("1", 10_000),
        (1, 10_000),
        ("1%", 10_000),
        ("10000", 10_000),
        ("0.01", 100),
        ("100", 100),  # 100 and up without a % sign are V4 units
        ("0.9", 9000),
        ("0", 0),
        ("dynamic", lp.DYNAMIC_FEE),
        ("DYNAMIC", lp.DYNAMIC_FEE),
        (None, None),
        ("", None),
    ],
)
def test_parse_fee(value: Any, fee: int | None) -> None:
    assert lp.parse_fee(value) == fee


@pytest.mark.parametrize(
    "value", ["abc", "-1", "0.00001", "101%", "1000001", "1e400", "nan", True, [500]]
)
def test_parse_fee_refuses_what_is_not_a_tier(value: Any) -> None:
    with pytest.raises(TradingError) as exc:
        lp.parse_fee(value)
    assert exc.value.code == "trading.invalid"


def test_fee_label() -> None:
    assert [lp.fee_label(f) for f in (100, 500, 3000, 10_000, 9000)] == [
        "0.01%",
        "0.05%",
        "0.3%",
        "1%",
        "0.9%",
    ]
    assert lp.fee_label(lp.DYNAMIC_FEE) == "dynamic"


def test_split_pair() -> None:
    assert lp.split_pair("ETH/USDC", None) == ("ETH", "USDC")
    assert lp.split_pair(" ETH / USDC ", "usdc") == ("ETH", "USDC")
    assert lp.split_pair("ETH", "USDC") == ("ETH", "USDC")
    assert lp.split_pair("0x" + "ab" * 32, None) == ("0x" + "ab" * 32, None)
    for bad, quote in [("ETH/", None), ("/USDC", None), ("A/B/C", None), ("ETH/USDC", "WETH")]:
        with pytest.raises(TradingError) as exc:
            lp.split_pair(bad, quote)
        assert exc.value.code == "trading.invalid", bad


def test_fee_picks_the_tier_not_the_deepest_pool() -> None:
    # BONER/USDG in a funded 1 % pool and an empty 0.3 % one, both conventional tiers.
    world = boner_world(live_fee=(10_000, 200))
    empty = next(p for p in world.pools.values() if p.key["fee"] == 3000)
    live = next(p for p in world.pools.values() if p.key["fee"] == 10_000)
    env = env_for(world, prices=world_prices(world, {USDG: 1.0}))
    assert lp.build_pool(env, BONER)["pool"]["poolId"] == live.pool_id
    card = lp.build_pool(env, BONER, None, lp.parse_fee("0.3"))
    assert card["pool"]["poolId"] == empty.pool_id and card["pool"]["feePct"] == "0.3%"
    ranges = lp.build_ranges(env, BONER, USDG, lp.parse_fee("1%"))
    assert ranges["pool"]["poolId"] == live.pool_id
    # A named poolId must be on the named tier.
    assert lp.build_pool(env, live.pool_id, None, 10_000)["pool"]["poolId"] == live.pool_id
    with pytest.raises(TradingError) as wrong:
        lp.build_pool(env, live.pool_id, None, 500)
    assert wrong.value.code == "trading.invalid" and "1% pool, not 0.05%" in str(wrong.value)


def test_a_missing_tier_names_the_tiers_that_exist() -> None:
    world = boner_world(live_fee=(10_000, 200))
    env = env_for(world, prices=world_prices(world, {USDG: 1.0}))
    with pytest.raises(TradingError) as exc:
        lp.build_pool(env, BONER, USDG, lp.parse_fee("0.05"))
    assert exc.value.code == "trading.lp.not_found"
    message = str(exc.value)
    assert "no 0.05% Uniswap V4 pool for BONER/USDG" in message
    assert "tiers that exist: 0.3%, 1%" in message
    assert exc.value.details["tiers"] == ["0.3%", "1%"]
    assert exc.value.details["feePct"] == "0.05%"


def test_a_named_unusual_tier_is_found_without_initialize_logs() -> None:
    # The live BONER market is 0.9 % at tick spacing 90: no conventional tier and,
    # with logs refused, invisible to a plain read -- but naming the fee derives it.
    world = boner_world()
    assert world.logs_refused
    live = next(p for p in world.pools.values() if p.key["fee"] == 9000)
    env = env_for(world, prices=world_prices(world, {USDG: 1.0}))
    assert lp.build_pool(env, BONER)["pool"]["feePct"] == "0.3%"  # the stale one
    card = lp.build_pool(env, BONER, None, lp.parse_fee("0.9"))
    assert card["pool"]["poolId"] == live.pool_id and int(card["pool"]["liquidity"]) > 0


async def test_reads_take_a_pair_and_a_fee(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, str | None, int | None]] = []

    def builder(env: Any, target: str, quote: str | None, fee: int | None) -> dict[str, Any]:
        seen.append((target, quote, fee))
        return {"kind": "pool"}

    async def resolve(service: Any, spec: Any, value: str) -> str:
        return {"ETH": lp.NATIVE_ADDRESS, "USDC": "0x" + "cc" * 20}[value]

    monkeypatch.setattr(lp, "_resolve_address", resolve)
    monkeypatch.setattr(lp, "_engine_env", lambda service, spec, loop: object())
    monkeypatch.setattr(lp, "start", lambda env: env)
    await lp._read_pool_kind(
        object(),  # type: ignore[arg-type]
        builder,
        chain=BASE,
        target="ETH/USDC",
        quote=None,
        fee=500,
    )
    assert seen == [(lp.NATIVE_ADDRESS, "0x" + "cc" * 20, 500)]
