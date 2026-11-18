"""The liquidity book (``agentos.trading.lp_book``): cache, refresh policy, payload."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.trading import lp, lp_amm, lp_book
from agentos.trading.chains import BASE, ROBINHOOD, ChainSpec
from agentos.trading.ledger import Ledger
from agentos.trading.service import TradingError

from . import lp_world
from .amm_world import (
    AERO_CL,
    UNI_V2,
    UNI_V3,
    USDC,
    WALLET,
    WETH,
    AmmWorld,
    ClPool,
    Nft,
    Pair,
)

LIQ = 10**15
PRICES = {WETH: 2500.0, USDC: 1.0}


class _Vault:
    initialized = True

    def __init__(self, wallets: dict[str, str]) -> None:
        self.records = [
            SimpleNamespace(address=lp_world._cs(a), label=label, created_block={}, imported=True)
            for a, label in wallets.items()
        ]

    def list(self) -> list[Any]:
        return list(self.records)

    def resolve(self, value: str) -> Any:
        for record in self.records:
            if value.lower() in (record.address.lower(), record.label.lower()):
                return record
        raise TradingError("trading.unknown_wallet", f"No wallet {value!r}")


class _Service:
    def __init__(self, ledger: Ledger, specs: list[ChainSpec] | None = None) -> None:
        self.ledger = ledger
        self.vault = _Vault({WALLET: "Main"})
        self.config = SimpleNamespace(rpc_urls={})
        self.events: list[tuple[str, dict[str, Any]]] = []
        self._specs = specs or [BASE]

    def chains(self) -> list[ChainSpec]:
        return list(self._specs)

    async def _emit(self, event: str, payload: dict[str, Any]) -> None:
        self.events.append((event, payload))


class _Setup:
    """A Base chain with V4 positions (``pepe_world``) and classic AMMs side by side."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.v4 = lp_world.pepe_world()
        self.amm = AmmWorld(fallback=self.v4)
        self.amm.tokens.pop(WETH)  # the V4 world's WETH metadata serves both
        self.clock = [1_000_000.0]
        self.ledger = Ledger(tmp_path / "trading.sqlite")
        self.service = _Service(self.ledger)
        self.indexer_calls = 0
        #: The indexer answers with nothing (and the node refuses the logs).
        self.blind = False
        self.down: set[str] = set()
        monkeypatch.setattr(lp, "_engine_env", self.env)
        monkeypatch.setattr(lp_book, "state_dir", lambda *parts: tmp_path.joinpath(*parts))
        self.book = self.new_book()

    def new_book(self) -> lp_book.LpBook:
        return lp_book.LpBook(self.service, now=lambda: self.clock[0])  # type: ignore[arg-type]

    def env(self, service: Any, spec: ChainSpec, loop: Any) -> lp.ChainEnv:
        if spec.key in self.down:
            client: Any = SimpleNamespace(block_number=self.refuse)
        else:
            client = self.amm
        known = {k.lower(): v for k, v in PRICES.items()}
        return lp.ChainEnv(
            spec=spec,
            client=client,
            prices=lambda addresses: {a.lower(): known.get(a.lower()) for a in addresses},
            lib=lp_world.LIB,
            nft_ids=self.indexer,
            vault={WALLET.lower(): "Main"},
        )

    def refuse(self) -> int:
        raise RuntimeError("eth_blockNumber: HTTP 503")

    def indexer(self, owner: str, **kwargs: Any) -> list[int]:
        self.indexer_calls += 1
        return [] if self.blind else list(self.v4.indexer(owner))

    def uniswap_v3(self, token_id: int = 7) -> None:
        if not self.amm.cl_pools:
            self.amm.add_cl_pool(UNI_V3, ClPool("", WETH, USDC, 500, -200_000))
        self.amm.mint(UNI_V3, token_id, Nft(WALLET, WETH, USDC, 500, -201_000, -199_000, LIQ))

    async def read(self, **kwargs: Any) -> dict[str, Any]:
        return await self.book.read(**kwargs)

    async def settle(self) -> None:
        task = self.book._task
        if task is not None:
            await task


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Setup:
    return _Setup(tmp_path, monkeypatch)


async def test_first_read_answers_at_once_then_fills_in(setup: _Setup) -> None:
    setup.uniswap_v3()
    first = await setup.read()
    assert first["kind"] == "book" and first["ready"] is False and first["refreshing"] is True
    assert first["positions"] == [] and first["readAt"] is None
    await setup.settle()
    assert setup.service.events == [("trading.changed", {"reason": "lp"})]
    book = await setup.read()
    assert book["ready"] is True and book["refreshing"] is False
    assert book["readAt"] == setup.clock[0]
    by_protocol = {p["key"]: p["count"] for p in book["protocols"]}
    assert by_protocol == {"uniswap_v4": 3, "uniswap_v3": 1}
    assert book["totals"]["count"] == 4 and book["totals"]["valueUsd"] is not None
    assert book["wallets"] == [{"address": lp_world._cs(WALLET), "label": "Main"}]
    assert book["chains"][0]["key"] == "base" and book["chains"][0]["asOfBlock"] == setup.amm.block
    v4 = next(p for p in book["positions"] if p["protocol"]["key"] == "uniswap_v4")
    assert v4["staked"] is False and v4["id"].startswith("8453:uniswap_v4:")
    # A fresh process serves the same book from the ledger without reading the chain.
    setup.amm.rounds = 0
    again = await setup.new_book().read()
    assert again["totals"] == book["totals"] and setup.amm.rounds == 0


async def test_nothing_is_read_while_the_cache_is_fresh(setup: _Setup) -> None:
    await setup.read()
    await setup.settle()
    setup.amm.rounds = 0
    setup.clock[0] += lp_book.BOOK_TTL_S - 1
    assert (await setup.read())["refreshing"] is False and setup.amm.rounds == 0
    setup.clock[0] += 2
    assert (await setup.read())["refreshing"] is True
    await setup.settle()
    assert setup.amm.rounds > 0


async def test_known_v4_positions_skip_the_search(setup: _Setup) -> None:
    await setup.read()
    await setup.settle()
    assert setup.indexer_calls == 1
    setup.clock[0] += lp_book.BOOK_TTL_S
    await setup.read()
    await setup.settle()
    # The three known ids account for the wallet's balance: no indexer, no logs.
    assert setup.indexer_calls == 1
    assert (await setup.read())["totals"]["count"] == 3
    # A fourth position the cache does not know sends the search out again.
    setup.v4.add_position(
        next(iter(setup.v4.pools.values())), 99_001, -210_000, -200_000, 10**18, WALLET
    )
    setup.clock[0] += lp_book.BOOK_TTL_S
    await setup.read()
    await setup.settle()
    assert setup.indexer_calls == 2 and (await setup.read())["totals"]["count"] == 4


async def test_a_search_that_came_up_short_is_not_repeated_every_minute(setup: _Setup) -> None:
    await setup.read()
    await setup.settle()
    # A position arrives that neither the indexer nor the logs will give up.
    setup.blind = True
    setup.v4.add_position(
        next(iter(setup.v4.pools.values())), 99_002, -210_000, -200_000, 10**18, WALLET
    )

    async def refresh(**kwargs: Any) -> dict[str, Any]:
        setup.clock[0] += lp_book.BOOK_TTL_S
        await setup.read(**kwargs)
        await setup.settle()
        return await setup.read()

    book = await refresh()
    searched = setup.indexer_calls
    assert searched == 2 and book["partialScan"] is True and book["totals"]["count"] == 3
    assert any("position(s) held by" in w for w in book["warnings"])
    # The next refreshes re-read the three it knows and leave the search alone...
    book = await refresh()
    assert setup.indexer_calls == searched and book["totals"]["count"] == 3
    assert any("resumes shortly" in w for w in book["warnings"])
    # ...until the back-off has passed, or someone asks.
    setup.clock[0] += lp_book.FORCED_MIN_S
    await refresh(refresh=True)
    assert setup.indexer_calls == searched + 1
    await refresh()
    assert setup.indexer_calls == searched + 1
    setup.clock[0] += lp_book.V4_SEARCH_RETRY_S
    setup.blind = False
    book = await refresh()
    assert setup.indexer_calls == searched + 2 and book["totals"]["count"] == 4
    assert book["partialScan"] is False


async def test_a_short_search_that_found_something_goes_on_at_the_next_refresh(
    setup: _Setup,
) -> None:
    """Robinhood Chain's node throttles the log walk to a few wallets per time
    budget: a search that is getting somewhere must not wait out the back-off."""
    await setup.read()
    await setup.settle()
    pool = next(iter(setup.v4.pools.values()))
    setup.v4.add_position(pool, 99_003, -210_000, -200_000, 10**18, WALLET)
    setup.v4.add_position(pool, 99_004, -210_000, -200_000, 10**18, WALLET)
    answers = [[99_003], [99_004]]
    setup.indexer = lambda owner, **kwargs: answers.pop(0)  # type: ignore[method-assign]

    async def refresh() -> dict[str, Any]:
        setup.clock[0] += lp_book.BOOK_TTL_S
        await setup.read()
        await setup.settle()
        return await setup.read()

    book = await refresh()
    assert book["totals"]["count"] == 4 and book["partialScan"] is True
    book = await refresh()
    assert answers == [] and book["totals"]["count"] == 5 and book["partialScan"] is False


async def test_gauges_are_walked_on_the_first_read_and_then_only_when_asked(
    setup: _Setup,
) -> None:
    pool = setup.amm.add_cl_pool(AERO_CL, ClPool("", WETH, USDC, 100, -200_000))
    setup.amm.add_gauge("aero-cl", pool)

    async def refresh(**kwargs: Any) -> list[str]:
        setup.amm.asked.clear()
        await setup.read(**kwargs)
        await setup.settle()
        return list(setup.amm.asked)

    first = await refresh()
    # Never read before: the live gauges and the frozen (Aerodrome) ones.
    assert "allPoolsLength" in first and "length" in first
    setup.clock[0] += lp_book.BOOK_TTL_S
    assert "allPoolsLength" not in await refresh()
    # The wallet's balances moved: the live gauges once the floor has passed.
    setup.book.mark_dirty(WALLET)
    setup.clock[0] += lp_book.BOOK_TTL_S
    assert "allPoolsLength" not in await refresh()
    setup.clock[0] += lp_book.DEEP_MIN_S
    moved = await refresh()
    assert "allPoolsLength" in moved and "length" not in moved
    # An explicit refresh walks the frozen ones too.
    setup.clock[0] += lp_book.DEEP_MIN_S
    forced = await refresh(refresh=True)
    assert "allPoolsLength" in forced and "length" in forced
    # ...and periodically without anyone asking.
    setup.clock[0] += lp_book.DEEP_TTL_S
    later = await refresh()
    assert "allPoolsLength" in later and "length" not in later


async def test_a_position_that_left_the_wallet_sends_the_book_to_the_gauges(
    setup: _Setup,
) -> None:
    pool = setup.amm.add_cl_pool(AERO_CL, ClPool("", WETH, USDC, 100, -200_000))
    gauge = setup.amm.add_gauge("aero-cl", pool)
    setup.amm.mint(AERO_CL, 31, Nft(WALLET, WETH, USDC, 100, -201_000, -199_000, LIQ))
    await setup.read()
    await setup.settle()
    assert (await setup.read())["positions"][-1]["staked"] is False
    # Staked between two reads: gone from the wallet, not yet known as a stake.
    setup.amm.stake_nft(gauge, AERO_CL, 31)
    setup.clock[0] += lp_book.BOOK_TTL_S
    await setup.read()
    await setup.settle()
    gone = await setup.read()
    assert all(p["protocol"]["key"] != "aero_slipstream" for p in gone["positions"])
    assert WALLET in setup.book._dirty
    setup.clock[0] += lp_book.DEEP_MIN_S
    await setup.settle()
    await setup.read()
    await setup.settle()
    found = [p for p in (await setup.read())["positions"] if p["staked"]]
    assert [p["tokenId"] for p in found] == ["31"] and found[0]["gauge"].lower() == gauge


async def test_a_half_that_fails_keeps_its_last_rows(
    setup: _Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup.uniswap_v3()
    await setup.read()
    await setup.settle()

    def broken(*args: Any, **kwargs: Any) -> lp_amm.AmmRead:
        raise RuntimeError("eth_call: HTTP 502")

    monkeypatch.setattr(lp_amm, "read_positions", broken)
    setup.clock[0] += lp_book.BOOK_TTL_S
    await setup.read()
    await setup.settle()
    book = await setup.read()
    assert {p["key"]: p["count"] for p in book["protocols"]} == {"uniswap_v4": 3, "uniswap_v3": 1}
    assert book["partialScan"] is True
    assert any("the other protocols could not be read" in w for w in book["warnings"])


async def test_a_chain_that_is_down_is_named_and_retried_gently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = _Setup(tmp_path, monkeypatch)
    setup.service._specs = [BASE, ROBINHOOD]
    setup.down.add("robinhood")
    await setup.read()
    await setup.settle()
    book = await setup.read()
    assert book["ready"] is False and book["partialScan"] is True
    assert any(w.startswith("Robinhood Chain could not be refreshed") for w in book["warnings"])
    assert [c["readAt"] is None for c in book["chains"]] == [False, True]
    # Base is served; the dead chain is not hammered on every poll.
    assert book["totals"]["count"] == 3 and book["refreshing"] is False
    setup.clock[0] += lp_book.RETRY_MIN_S
    assert (await setup.read())["refreshing"] is True
    await setup.settle()
    only_base = await setup.read(chain=BASE)
    assert only_base["ready"] is True and len(only_base["chains"]) == 1


async def test_wallet_filter_pairs_and_removal(setup: _Setup) -> None:
    pair = setup.amm.add_pair(UNI_V2, Pair("", WETH, USDC, 4 * 10**18, 10**10, 10**12))
    setup.amm.pairs[pair].balances[WALLET] = 10**11
    # The ledger knows the wallet holds the LP token: that is the only lead to a pair.
    setup.ledger.set_balance(8453, WALLET, pair, 10**11)
    await setup.read()
    await setup.settle()
    book = await setup.read(wallet="Main")
    row = next(p for p in book["positions"] if p["protocol"]["key"] == "uniswap_v2")
    assert row["status"] == "full-range" and row["share"] == pytest.approx(0.1)
    assert book["totals"]["outOfRange"] == sum(
        1 for p in book["positions"] if p["status"] in ("above-range", "below-range")
    )
    with pytest.raises(TradingError):
        await setup.read(wallet="nobody")
    setup.ledger.remove_wallet(WALLET)
    assert setup.ledger.lp_positions() == [] and setup.ledger.lp_reads() == []


def test_totals_never_report_an_unpriced_part_as_zero() -> None:
    rows = [
        {"status": "in-range", "valueUsd": None, "fees": {"usd": None}, "rewards": None},
        {"status": "above-range", "valueUsd": None, "fees": None, "rewards": None},
    ]
    assert lp_book.totals(rows) == {
        "valueUsd": None,
        "feesUsd": None,
        "rewardsUsd": None,
        "count": 2,
        "outOfRange": 1,
        "unpriced": 2,
    }
    assert lp_book.totals([])["valueUsd"] == 0.0
    ordered = lp_book.sort_rows(
        [
            {"id": "a", "status": "in-range", "valueUsd": 5.0},
            {"id": "b", "status": "full-range", "valueUsd": 9.0},
            {"id": "c", "status": "below-range", "valueUsd": 1.0},
            {"id": "d", "status": "in-range", "valueUsd": None},
        ]
    )
    assert [r["id"] for r in ordered] == ["c", "b", "a", "d"]


async def test_the_real_service_keeps_the_book(
    funded_service: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through ``TradingService`` itself: its ledger, vault, events and shutdown."""
    service = funded_service
    wallet = service.test_wallet.lower()
    worlds = {
        spec.key: AmmWorld(spec=spec, fallback=lp_world.empty_world(spec))
        for spec in (BASE, ROBINHOOD)
    }
    base = worlds["base"]
    base.add_cl_pool(UNI_V3, ClPool("", WETH, USDC, 500, -200_000))
    base.mint(UNI_V3, 7, Nft(wallet, WETH, USDC, 500, -201_000, -199_000, LIQ))
    for world in worlds.values():
        world.tokens.pop(WETH)

    def env(svc: Any, spec: ChainSpec, loop: Any) -> lp.ChainEnv:
        return lp.ChainEnv(
            spec=spec,
            client=worlds[spec.key],
            prices=lambda addresses: {a.lower(): PRICES.get(a.lower()) for a in addresses},
            lib=lp_world.LIB,
            vault={wallet: "Main"},
        )

    monkeypatch.setattr(lp, "_engine_env", env)
    monkeypatch.setattr(lp_book, "state_dir", lambda *parts: tmp_path.joinpath(*parts))
    book = service.lp_book
    assert book is service.lp_book and isinstance(book, lp_book.LpBook)
    # The ledger's balances are the pair candidates; the native row is not a token to ask.
    service.ledger.set_balance(8453, wallet, lp.NATIVE_ADDRESS, 10**18)
    service.ledger.set_balance(8453, wallet, USDC, 10**9)
    first = await book.read()
    assert first["refreshing"] is True
    await book.refresh()
    assert ("trading.changed", {"schema_version": 1, "reason": "lp"}) in service.events
    answer = await book.read(wallet="Main", chain=BASE)
    assert answer["ready"] is True
    assert [p["protocol"]["key"] for p in answer["positions"]] == ["uniswap_v3"]
    assert answer["positions"][0]["owner"]["label"] == "Main"
    assert "factory" in base.asked and base.asked.count("factory") == 1
    # A sync that moved the wallet's balances tells the book to look closer.
    await service._emit("trading.changed", {"reason": "sync", "wallet": service.test_wallet})
    assert wallet in book._dirty
    await service._emit("trading.changed", {"reason": "lp"})
    assert book._dirty == {wallet}
    # Shutting the engine down ends a refresh in flight.
    book._started_at = 0.0
    assert (await book.read(refresh=True))["refreshing"] is True
    await service.stop()
    assert book.refreshing is False
