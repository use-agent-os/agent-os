from __future__ import annotations

from dataclasses import replace

import httpx
import pytest

from agentos.trading.chains import BASE, ROBINHOOD
from agentos.trading.discovery import BlockscoutDiscovery, _parse_token_balances
from tests.test_trading.fakes import USDC, WALLET, WETH, FakeIndexer


@pytest.fixture
def indexer() -> FakeIndexer:
    return FakeIndexer()


@pytest.fixture
async def discovery(indexer: FakeIndexer):
    clock = {"now": 1_000.0}
    async with httpx.AsyncClient(transport=httpx.MockTransport(indexer.handle)) as http:
        d = BlockscoutDiscovery(http=http, ttl_s=300, now=lambda: clock["now"])
        d.clock = clock  # type: ignore[attr-defined]
        yield d


class TestBlockscoutDiscovery:
    async def test_lists_erc20_holdings_only(
        self, discovery: BlockscoutDiscovery, indexer: FakeIndexer
    ) -> None:
        indexer.hold(WALLET, USDC.upper(), 5)
        indexer.hold(WALLET, WETH, 0)  # held nothing: not a holding
        indexer.hold(WALLET, "0x3333333333333333333333333333333333333333", 1, kind="ERC-721")
        assert await discovery.holdings(BASE, WALLET) == [USDC]
        # The request went to this chain's instance, for the checksummed address.
        url = str(indexer.requests[-1].url)
        assert url.startswith("https://base.blockscout.com/api/v2/addresses/0x1111")
        assert url.endswith("/token-balances")
        assert indexer.requests[-1].headers["user-agent"].startswith("agentos-trading/")

    async def test_cached_per_ttl_and_per_chain(
        self, discovery: BlockscoutDiscovery, indexer: FakeIndexer
    ) -> None:
        indexer.hold(WALLET, USDC, 5)
        await discovery.holdings(BASE, WALLET)
        await discovery.holdings(BASE, WALLET)
        assert len(indexer.requests) == 1
        other = replace(BASE, chain_id=1, key="eth", blockscout_url="https://eth.blockscout.com")
        await discovery.holdings(other, WALLET)
        assert len(indexer.requests) == 2
        assert "eth.blockscout.com" in str(indexer.requests[-1].url)
        discovery.clock["now"] += 301  # type: ignore[attr-defined]
        await discovery.holdings(BASE, WALLET)
        assert len(indexer.requests) == 3

    async def test_robinhood_has_no_indexer(self) -> None:
        """Its Blockscout sits behind a Cloudflare challenge; nothing is asked."""
        assert ROBINHOOD.blockscout_url is None

        def never(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request expected")

        async with httpx.AsyncClient(transport=httpx.MockTransport(never)) as http:
            assert await BlockscoutDiscovery(http=http).holdings(ROBINHOOD, WALLET) == []

    async def test_unknown_address_is_empty_but_outage_keeps_last_good(
        self, discovery: BlockscoutDiscovery, indexer: FakeIndexer
    ) -> None:
        # Blockscout 404s an address it never saw: a real "holds nothing".
        assert await discovery.holdings(BASE, WALLET) == []
        indexer.hold(WALLET, USDC, 5)
        discovery.clock["now"] += 301  # type: ignore[attr-defined]
        assert await discovery.holdings(BASE, WALLET) == [USDC]
        # An outage must not shrink the scan set to nothing.
        indexer.status = 503
        discovery.clock["now"] += 301  # type: ignore[attr-defined]
        assert await discovery.holdings(BASE, WALLET) == [USDC]

    async def test_oversized_answer_is_no_answer(self, indexer: FakeIndexer) -> None:
        """An exchange wallet's 2.6 MB token list is not downloaded, and not trusted."""
        for i in range(50):
            indexer.hold(WALLET, f"0x{i:040x}", 1)
        async with httpx.AsyncClient(transport=httpx.MockTransport(indexer.handle)) as http:
            small = BlockscoutDiscovery(http=http, max_body_bytes=500)
            assert await small.holdings(BASE, WALLET) == []
            big = BlockscoutDiscovery(http=http)
            assert len(await big.holdings(BASE, WALLET)) == 50

    async def test_transport_error_is_no_answer(self) -> None:
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as http:
            d = BlockscoutDiscovery(http=http)
            assert await d.holdings(BASE, WALLET) == []

    async def test_chain_without_indexer(self) -> None:
        async with httpx.AsyncClient() as http:
            d = BlockscoutDiscovery(http=http)
            assert await d.holdings(replace(BASE, blockscout_url=None), WALLET) == []


class TestParse:
    def test_items_wrapper_dedupe_and_cap(self) -> None:
        payload = {
            "items": [
                {"token": {"address": USDC, "type": "ERC-20"}, "value": "1"},
                {"token": {"address": USDC.upper(), "type": "ERC-20"}, "value": "1"},
                {"token": {"address_hash": WETH, "type": "ERC_20"}, "value": "2"},
                {"token": {"address": "0xnothex", "type": "ERC-20"}, "value": "2"},
                {"token": {"address": WETH, "type": "ERC-20"}, "value": "abc"},
                "garbage",
            ]
        }
        assert _parse_token_balances(payload, 10) == ([USDC, WETH], True)
        # Under the cap the answer is a subset, and says so (#3506).
        assert _parse_token_balances(payload, 1) == ([USDC], False)

    def test_not_a_list_is_no_answer(self) -> None:
        assert _parse_token_balances({"message": "rate limited"}, 10) is None
        assert _parse_token_balances("nope", 10) is None
