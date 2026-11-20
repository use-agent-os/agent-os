"""The AgentOS token lists: parsing, merging, versions and the re-check loop.

Ported from the AgentOS DEX frontend's ``src/lib/token-lists`` tests: the
network is a function and the clock only moves when a test moves it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agentos.trading.token_lists import (
    RETRY_DELAYS_S,
    REVALIDATE_TTL_S,
    TOKEN_LIST_URLS,
    FetchResult,
    ListToken,
    ParsedList,
    TokenListStore,
    is_version_newer,
    merge_token_lists,
    parse_token_list,
)

CHAIN = 8453
A = "0x" + "a" * 40
B = "0x" + "b" * 40
C = "0x" + "c" * 40
MAIN = "https://lists.test/main.json"
STOCKS = "https://lists.test/stocks.json"


def _row(address: str = A, **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "chainId": CHAIN,
        "address": address,
        "symbol": "TKN",
        "name": "Token",
        "decimals": 18,
    }
    row.update(over)
    return row


def _list(rows: list[dict[str, Any]], version: tuple[int, int, int] | None = None, **over: Any):
    body: dict[str, Any] = {"name": "Test list", "tokens": rows}
    if version is not None:
        body["version"] = dict(zip(("major", "minor", "patch"), version, strict=True))
    body.update(over)
    return body


class TestBuiltInLists:
    def test_each_chain_has_its_token_list_then_its_stock_list(self) -> None:
        base = "https://raw.githubusercontent.com/use-agent-os/agent-os-token-list/main"
        assert TOKEN_LIST_URLS == {
            8453: (f"{base}/base_tokens.json", f"{base}/base_stock_tokens.json"),
            4663: (f"{base}/robinhood_tokens.json", f"{base}/robinhood_stock_tokens.json"),
        }


class TestParse:
    def test_a_uniswap_token_list_object(self) -> None:
        parsed = parse_token_list(
            _list(
                [_row(A.upper().replace("0X", "0x"), logoURI="https://img/a", tags=["Stock"])],
                (1, 2, 3),
                timestamp="2026-11-20T06:54:08Z",
            ),
            CHAIN,
        )
        assert parsed is not None
        assert parsed.name == "Test list" and parsed.version == (1, 2, 3)
        assert parsed.timestamp == "2026-11-20T06:54:08Z"
        (token,) = parsed.tokens
        assert token == ListToken(CHAIN, A, "TKN", "Token", 18, "https://img/a", ("stock",))
        assert token.stock is True

    def test_a_bare_array_is_a_list_too(self) -> None:
        parsed = parse_token_list([_row(A), _row(B)], CHAIN)
        assert parsed is not None and [t.address for t in parsed.tokens] == [A, B]
        assert parsed.version is None and parsed.tokens[0].stock is False

    @pytest.mark.parametrize("body", [None, "<html>", 7, {"error": "nope"}, {"tokens": "x"}])
    def test_anything_else_is_not_a_list(self, body: Any) -> None:
        assert parse_token_list(body, CHAIN) is None

    @pytest.mark.parametrize(
        "row",
        [
            "0xnot-a-row",
            _row(chainId=4663),  # another chain's token
            _row(chainId="8453"),
            _row(chainId=True),
            _row(address="0x1234"),
            _row(address="0x" + "g" * 40),
            _row(address="0x" + "0" * 40),  # the zero address
            _row(address="0x" + "e" * 40),  # the native sentinel
            _row(decimals="18"),
            _row(decimals=18.0),
            _row(decimals=True),
            _row(decimals=-1),
            _row(decimals=37),
            _row(symbol=""),
            _row(symbol="   "),
            _row(symbol=None),
        ],
    )
    def test_a_row_that_is_not_a_token_of_this_chain_is_skipped(self, row: Any) -> None:
        parsed = parse_token_list({"tokens": [row, _row(B)]}, CHAIN)
        assert parsed is not None and [t.address for t in parsed.tokens] == [B]

    def test_zero_decimals_are_zero_not_eighteen(self) -> None:
        parsed = parse_token_list([_row(decimals=0)], CHAIN)
        assert parsed is not None and parsed.tokens[0].decimals == 0

    def test_one_row_per_address_and_a_missing_name_falls_back_to_the_symbol(self) -> None:
        parsed = parse_token_list(
            [_row(A, name=None), _row(A.upper().replace("0X", "0x"), symbol="DUP")], CHAIN
        )
        assert parsed is not None
        assert [(t.symbol, t.name) for t in parsed.tokens] == [("TKN", "TKN")]

    def test_a_logo_must_be_an_http_url(self) -> None:
        parsed = parse_token_list(
            [_row(A, logoURI="javascript:alert(1)"), _row(B, logoURI="ipfs://x")], CHAIN
        )
        assert parsed is not None and [t.logo_uri for t in parsed.tokens] == [None, None]

    def test_a_stock_tokens_full_name_keeps_its_suffix(self) -> None:
        name = "SPDR Portfolio S&P 500 High Dividend ETF Trust Units • Robinhood Token"
        parsed = parse_token_list([_row(name=name)], CHAIN)
        assert parsed is not None and parsed.tokens[0].name == name

    def test_a_malformed_version_is_no_version(self) -> None:
        for version in ({"major": 1, "minor": 0}, {"major": -1, "minor": 0, "patch": 0}, "1.0.0"):
            parsed = parse_token_list({"tokens": [_row()], "version": version}, CHAIN)
            assert parsed is not None and parsed.version is None


class TestMerge:
    def test_the_first_list_wins_and_a_later_one_only_decorates(self) -> None:
        first = [ListToken(CHAIN, A, "USDC", "USD Coin", 6)]
        later = [
            ListToken(CHAIN, A, "FAKE", "Not USDC", 18, "https://img/a", ("stock",)),
            ListToken(CHAIN, B, "AAPLc", "Apple Inc.", 8, None, ("stock",)),
        ]
        merged = merge_token_lists([first, later])
        assert list(merged) == [A, B]
        assert merged[A] == ListToken(CHAIN, A, "USDC", "USD Coin", 6, "https://img/a", ("stock",))
        assert merged[B].stock is True

    def test_a_logo_already_there_is_kept(self) -> None:
        merged = merge_token_lists(
            [
                [ListToken(CHAIN, A, "USDC", "USD Coin", 6, "https://img/first")],
                [ListToken(CHAIN, A, "USDC", "USD Coin", 6, "https://img/second")],
            ]
        )
        assert merged[A].logo_uri == "https://img/first"


class TestVersions:
    @pytest.mark.parametrize(
        ("new", "held", "newer"),
        [
            ((1, 0, 1), (1, 0, 0), True),
            ((1, 1, 0), (1, 0, 9), True),
            ((2, 0, 0), (1, 9, 9), True),
            ((1, 0, 0), (1, 0, 0), False),
            ((1, 0, 0), (1, 0, 1), False),
            ((0, 9, 9), (1, 0, 0), False),
        ],
    )
    def test_by_version(self, new, held, newer: bool) -> None:
        assert is_version_newer(ParsedList(version=new), ParsedList(version=held)) is newer

    def test_the_same_version_falls_back_to_the_timestamp(self) -> None:
        old = ParsedList(version=(1, 0, 0), timestamp="2026-11-19T00:00:00Z")
        new = ParsedList(version=(1, 0, 0), timestamp="2026-11-20T00:00:00Z")
        assert is_version_newer(new, old) is True
        assert is_version_newer(old, new) is False
        assert is_version_newer(new, new) is False

    def test_a_first_copy_is_newer_and_nothing_is_not(self) -> None:
        assert is_version_newer(ParsedList(), None) is True
        assert is_version_newer(None, ParsedList()) is False
        # Gaining a version is an upgrade; two unversioned, undated copies are equal.
        assert is_version_newer(ParsedList(version=(0, 0, 1)), ParsedList()) is True
        assert is_version_newer(ParsedList(), ParsedList()) is False
        assert is_version_newer(ParsedList(timestamp="junk"), ParsedList(timestamp="x")) is False


class Network:
    """A fake of the two list URLs: bodies, ETags, and a log of what was asked."""

    def __init__(self) -> None:
        self.bodies: dict[str, Any] = {}
        self.etags: dict[str, str] = {}
        self.down: set[str] = set()
        self.calls: list[tuple[str, str | None]] = []

    def asked(self, url: str) -> int:
        return [called for called, _ in self.calls].count(url)

    def publish(self, url: str, body: Any, etag: str) -> None:
        self.bodies[url] = body
        self.etags[url] = etag

    async def fetch(self, url: str, etag: str | None) -> FetchResult | None:
        self.calls.append((url, etag))
        if url in self.down:
            return None
        if url not in self.bodies:
            return FetchResult(status=404)
        if etag is not None and etag == self.etags[url]:
            return FetchResult(status=304, etag=etag)
        return FetchResult(status=200, body=self.bodies[url], etag=self.etags[url])


@pytest.fixture
def net() -> Network:
    network = Network()
    network.publish(MAIN, _list([_row(A, symbol="USDC", decimals=6)], (1, 0, 0)), "main-1")
    network.publish(
        STOCKS, _list([_row(B, symbol="AAPLc", decimals=8, tags=["stock"])], (1, 0, 0)), "stocks-1"
    )
    return network


@pytest.fixture
def clock() -> dict[str, float]:
    return {"now": 1_000_000.0}


@pytest.fixture
def store(net: Network, clock: dict[str, float]) -> TokenListStore:
    return TokenListStore(fetch=net.fetch, now=lambda: clock["now"], urls={CHAIN: (MAIN, STOCKS)})


class TestStore:
    async def test_first_use_downloads_every_list_of_the_chain(
        self, store: TokenListStore, net: Network
    ) -> None:
        tokens = await store.tokens(CHAIN)
        assert net.calls == [(MAIN, None), (STOCKS, None)]
        assert {a: (t.symbol, t.decimals, t.stock) for a, t in tokens.items()} == {
            A: ("USDC", 6, False),
            B: ("AAPLc", 8, True),
        }
        record = store.record(CHAIN, STOCKS)
        assert record is not None and record.version == (1, 0, 0) and record.etag == "stocks-1"

    async def test_a_chain_without_lists_has_no_tokens_and_asks_nothing(
        self, store: TokenListStore, net: Network
    ) -> None:
        assert await store.tokens(4663) == {}
        assert net.calls == []

    async def test_a_fresh_copy_is_used_without_asking(
        self, store: TokenListStore, net: Network, clock: dict[str, float]
    ) -> None:
        first = await store.tokens(CHAIN)
        clock["now"] += REVALIDATE_TTL_S - 1
        assert await store.tokens(CHAIN) is first
        await store.settle()
        assert len(net.calls) == 2

    async def test_a_stale_copy_answers_at_once_and_is_rechecked_with_its_etag(
        self, store: TokenListStore, net: Network, clock: dict[str, float]
    ) -> None:
        first = await store.tokens(CHAIN)
        clock["now"] += REVALIDATE_TTL_S + 1
        assert await store.tokens(CHAIN) is first
        await store.settle()
        assert net.calls[2:] == [(MAIN, "main-1"), (STOCKS, "stocks-1")]
        # Both answered 304: the check counted, so the window starts again.
        assert await store.tokens(CHAIN) is first
        await store.settle()
        assert len(net.calls) == 4

    async def test_a_newer_version_replaces_the_held_list(
        self, store: TokenListStore, net: Network, clock: dict[str, float]
    ) -> None:
        first = await store.tokens(CHAIN)
        net.publish(
            STOCKS,
            _list(
                [
                    _row(B, symbol="AAPLc", decimals=8, tags=["stock"]),
                    _row(C, symbol="TSLAc", decimals=8, tags=["stock"]),
                ],
                (1, 0, 1),
            ),
            "stocks-2",
        )
        clock["now"] += REVALIDATE_TTL_S + 1
        assert C not in await store.tokens(CHAIN)  # the caller is never kept waiting
        await store.settle()
        latest = await store.tokens(CHAIN)
        assert latest is not first and latest[C].symbol == "TSLAc" and latest[C].stock
        record = store.record(CHAIN, STOCKS)
        assert record is not None and record.version == (1, 0, 1) and record.etag == "stocks-2"

    async def test_an_older_or_equal_version_is_never_a_rollback(
        self, store: TokenListStore, net: Network, clock: dict[str, float]
    ) -> None:
        first = await store.tokens(CHAIN)
        # A CDN edge still serving last week's file, or the same version re-encoded.
        for version, etag in (((0, 9, 9), "main-old"), ((1, 0, 0), "main-same")):
            net.publish(MAIN, _list([_row(A, symbol="STALE", decimals=18)], version), etag)
            clock["now"] += REVALIDATE_TTL_S + 1
            await store.tokens(CHAIN)
            await store.settle()
            assert await store.tokens(CHAIN) is first
            assert first[A].symbol == "USDC" and first[A].decimals == 6
        # It answered, though: the next check is conditional on what it sent.
        record = store.record(CHAIN, MAIN)
        assert record is not None and record.version == (1, 0, 0) and record.etag == "main-same"

    async def test_a_failed_first_download_is_retried_after_2_5_15_then_60_seconds(
        self, store: TokenListStore, net: Network, clock: dict[str, float]
    ) -> None:
        net.down.add(STOCKS)
        tokens = await store.tokens(CHAIN)
        assert list(tokens) == [A]  # the list that answered is usable on its own
        assert net.asked(STOCKS) == 1
        expected = 1
        for delay in (*RETRY_DELAYS_S, RETRY_DELAYS_S[-1]):
            clock["now"] += delay - 0.5
            await store.tokens(CHAIN)
            assert net.asked(STOCKS) == expected  # held: no hammering inside the delay
            clock["now"] += 1
            await store.tokens(CHAIN)
            expected += 1
            assert net.asked(STOCKS) == expected
        assert RETRY_DELAYS_S == (2.0, 5.0, 15.0, 60.0)
        # It comes back: the next due call has it, and the back-off is forgotten.
        net.down.clear()
        clock["now"] += RETRY_DELAYS_S[-1] + 1
        assert B in await store.tokens(CHAIN)
        assert net.asked(MAIN) == 1

    @pytest.mark.parametrize("failure", ["down", "404", "not-a-list", "raises"])
    async def test_a_failed_recheck_keeps_the_held_copy_and_backs_off(
        self, store: TokenListStore, net: Network, clock: dict[str, float], failure: str
    ) -> None:
        first = await store.tokens(CHAIN)
        if failure == "down":
            net.down.add(MAIN)
        elif failure == "404":
            del net.bodies[MAIN]
        elif failure == "not-a-list":
            net.publish(MAIN, {"message": "rate limited"}, "main-2")
        else:
            real = net.fetch

            async def boom(url: str, etag: str | None) -> FetchResult | None:
                if url == MAIN:
                    net.calls.append((url, etag))
                    raise RuntimeError("boom")
                return await real(url, etag)

            store._fetch = boom  # type: ignore[method-assign]
        clock["now"] += REVALIDATE_TTL_S + 1
        assert await store.tokens(CHAIN) is first
        await store.settle()
        assert net.asked(MAIN) == 2
        # Held for the first retry delay, then asked again — still serving the copy.
        clock["now"] += RETRY_DELAYS_S[0] - 0.5
        assert await store.tokens(CHAIN) is first
        await store.settle()
        assert net.asked(MAIN) == 2
        clock["now"] += 1
        assert await store.tokens(CHAIN) is first
        await store.settle()
        assert net.asked(MAIN) == 3

    async def test_concurrent_first_callers_share_one_download(
        self, store: TokenListStore, net: Network
    ) -> None:
        results = await asyncio.gather(*(store.tokens(CHAIN) for _ in range(5)))
        assert len(net.calls) == 2
        assert all(result is results[0] for result in results)

    async def test_only_one_recheck_runs_behind_callers(
        self, store: TokenListStore, net: Network, clock: dict[str, float]
    ) -> None:
        await store.tokens(CHAIN)
        clock["now"] += REVALIDATE_TTL_S + 1
        for _ in range(5):
            await store.tokens(CHAIN)
        await store.settle()
        assert len(net.calls) == 4

    async def test_aclose_cancels_a_recheck_in_flight(
        self, store: TokenListStore, net: Network, clock: dict[str, float]
    ) -> None:
        await store.tokens(CHAIN)
        gate = asyncio.Event()
        real = net.fetch

        async def slow(url: str, etag: str | None) -> FetchResult | None:
            await gate.wait()
            return await real(url, etag)

        store._fetch = slow  # type: ignore[method-assign]
        clock["now"] += REVALIDATE_TTL_S + 1
        await store.tokens(CHAIN)
        await store.aclose()
        assert len(net.calls) == 2
