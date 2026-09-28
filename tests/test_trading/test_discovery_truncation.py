"""Issue #3506: discovery truncated at MAX_TOKENS in silence.

`_parse_token_balances` stopped at ``max_tokens`` and returned a plain list,
so a truncated answer looked exactly like a complete one: it was cached as
the wallet's holdings, the tokens past the cap were never read, and the
portfolio showed a subset with nothing to say so. The neighbouring cap does
the opposite — an oversized body logs ``trading.discovery_too_large`` and
answers "no discovery this round".

The parser now says whether it saw more, `holdings` logs it, and the sweep
marks the chain's read partial, the way it already does for a balance the
node would not answer.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from agentos.trading.chains import BASE, NATIVE_ADDRESS
from agentos.trading.discovery import BlockscoutDiscovery, _parse_token_balances
from agentos.trading.evm import EvmClient
from agentos.trading.prices import PriceService, TokenMeta, native_token
from agentos.trading.sync import WalletSyncer
from tests.test_trading.fakes import USDC, WALLET, FakeChain, FakeIndexer, FakePrices
from tests.test_trading.test_ledger_sync import _wallet


def _token(index: int) -> str:
    return f"0x{index:040x}"


@pytest.fixture
def indexer() -> FakeIndexer:
    return FakeIndexer()


@pytest.fixture
async def discovery(indexer: FakeIndexer):
    clock = {"now": 1_000.0}
    async with httpx.AsyncClient(transport=httpx.MockTransport(indexer.handle)) as http:
        yield BlockscoutDiscovery(http=http, ttl_s=300, now=lambda: clock["now"], max_tokens=3)


def _payload(count: int) -> dict[str, Any]:
    return {
        "items": [
            {"token": {"address": _token(i), "type": "ERC-20"}, "value": "1"} for i in range(count)
        ]
    }


@pytest.fixture
def prices_fake() -> FakePrices:
    prices = FakePrices()
    prices.spot[("base", USDC)] = 1.0
    prices.lists["base"] = [
        {"chainId": 8453, "address": USDC, "symbol": "USDC", "name": "USD Coin", "decimals": 6}
    ]
    return prices


@pytest.fixture
def chain() -> FakeChain:
    c = FakeChain(chain_id=8453, block=10_000)
    c.tokens[USDC] = ("USDC", "USD Coin", 6)
    return c


# ── the parser says whether it saw more ────────────────────────────────────


def test_a_complete_answer_says_so() -> None:
    tokens, complete = _parse_token_balances(_payload(3), 10)

    assert len(tokens) == 3
    assert complete is True


def test_an_answer_at_the_cap_is_not_complete() -> None:
    tokens, complete = _parse_token_balances(_payload(50), 3)

    assert tokens == [_token(0), _token(1), _token(2)]
    assert complete is False


def test_exactly_the_cap_is_reported_as_truncated() -> None:
    """The parser cannot tell "exactly the cap" from "the cap and more"
    without reading past it, and the safe reading is the pessimistic one."""
    _tokens, complete = _parse_token_balances(_payload(3), 3)

    assert complete is False


def test_a_body_that_is_not_a_list_is_still_no_answer() -> None:
    assert _parse_token_balances({"items": "nope"}, 3) is None


# ── the wallet-level answer ────────────────────────────────────────────────


async def test_a_truncated_wallet_is_remembered_as_truncated(
    discovery: BlockscoutDiscovery, indexer: FakeIndexer
) -> None:
    for index in range(10):
        indexer.hold(WALLET, _token(index), 5)

    tokens = await discovery.holdings(BASE, WALLET)

    assert len(tokens) == 3, "the cap still applies"
    assert discovery.truncated(BASE, WALLET) is True


async def test_a_complete_wallet_is_not_reported_as_truncated(
    discovery: BlockscoutDiscovery, indexer: FakeIndexer
) -> None:
    indexer.hold(WALLET, _token(1), 5)

    await discovery.holdings(BASE, WALLET)

    assert discovery.truncated(BASE, WALLET) is False


async def test_a_wallet_never_asked_about_is_not_reported_as_truncated(
    discovery: BlockscoutDiscovery,
) -> None:
    assert discovery.truncated(BASE, WALLET) is False


async def test_the_truncation_is_logged(
    discovery: BlockscoutDiscovery, indexer: FakeIndexer
) -> None:
    """The body cap already logs; this one used to drop the rest in silence."""
    for index in range(10):
        indexer.hold(WALLET, _token(index), 5)

    with capture_logs() as logs:
        await discovery.holdings(BASE, WALLET)

    truncated = [entry for entry in logs if entry["event"] == "trading.discovery_truncated"]
    assert len(truncated) == 1
    assert truncated[0]["wallet"] == WALLET
    assert truncated[0]["kept"] == 3


async def test_a_complete_answer_logs_nothing(
    discovery: BlockscoutDiscovery, indexer: FakeIndexer
) -> None:
    indexer.hold(WALLET, _token(1), 5)

    with capture_logs() as logs:
        await discovery.holdings(BASE, WALLET)

    assert not [entry for entry in logs if entry["event"] == "trading.discovery_truncated"]


async def test_the_cached_answer_keeps_its_verdict(
    discovery: BlockscoutDiscovery, indexer: FakeIndexer
) -> None:
    """A second call inside the TTL answers from the cache; the flag has to
    survive it, or the partial marker would flap."""
    for index in range(10):
        indexer.hold(WALLET, _token(index), 5)

    await discovery.holdings(BASE, WALLET)
    await discovery.holdings(BASE, WALLET)

    assert len(indexer.requests) == 1
    assert discovery.truncated(BASE, WALLET) is True


async def test_an_indexer_outage_does_not_claim_truncation(
    discovery: BlockscoutDiscovery, indexer: FakeIndexer
) -> None:
    indexer.hold(WALLET, _token(1), 5)
    await discovery.holdings(BASE, WALLET)
    indexer.status = 503

    assert await discovery.holdings(BASE, WALLET) == [_token(1)], "last good answer"
    assert discovery.truncated(BASE, WALLET) is False


# ── the sweep says the reading is partial ──────────────────────────────────


async def test_the_sweep_marks_a_truncated_wallet_partial(
    ledger: Any, chain: Any, prices_fake: Any
) -> None:
    """A balance the node would not answer already marks the chain partial;
    a wallet whose holdings were cut off is the same kind of gap."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "mainnet.base.org":
            return chain.handle(request)
        return prices_fake.handle(request)

    async def discover(spec: Any, address: str) -> list[str]:
        return []

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        prices = PriceService(http=http, ttl_s=0)
        evm = EvmClient("https://mainnet.base.org", http=http)

        async def token_meta(spec: Any, address: str) -> Any:
            if address == NATIVE_ADDRESS:
                return native_token(spec)
            return TokenMeta(spec.chain_id, address, "TOK", "Token", 18)

        syncer = WalletSyncer(
            ledger,
            prices,
            evm_for=lambda spec: evm,
            token_meta=token_meta,
            discover_tokens=discover,
            discovery_truncated=lambda spec, address: True,
        )
        syncer.initial_lookback = 100
        await syncer.sync(_wallet(created_block=None), BASE)

    read = ledger.chain_reads(WALLET, 8453)[0]
    assert read["status"] == "partial"
    assert "listed more tokens" in read["reason"]


async def test_the_sweep_is_ok_when_discovery_was_complete(
    ledger: Any, chain: Any, prices_fake: Any
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "mainnet.base.org":
            return chain.handle(request)
        return prices_fake.handle(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        prices = PriceService(http=http, ttl_s=0)
        evm = EvmClient("https://mainnet.base.org", http=http)

        async def token_meta(spec: Any, address: str) -> Any:
            if address == NATIVE_ADDRESS:
                return native_token(spec)
            return TokenMeta(spec.chain_id, address, "TOK", "Token", 18)

        syncer = WalletSyncer(
            ledger,
            prices,
            evm_for=lambda spec: evm,
            token_meta=token_meta,
            discovery_truncated=lambda spec, address: False,
        )
        syncer.initial_lookback = 100
        await syncer.sync(_wallet(created_block=None), BASE)

    assert ledger.chain_reads(WALLET, 8453)[0]["status"] == "ok"
