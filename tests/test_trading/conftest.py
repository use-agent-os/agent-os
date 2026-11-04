from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

from agentos.gateway.config import GatewayConfig, TradingConfig
from agentos.trading.chains import BASE, ROBINHOOD
from agentos.trading.ledger import Ledger
from agentos.trading.prices import PriceService
from agentos.trading.service import TradingService, set_trading_service
from agentos.trading.vault import Vault
from tests.test_trading.fakes import (
    AAPL,
    USDC,
    WALLET,
    WETH,
    FakeAggregator,
    FakeChain,
    FakeIndexer,
    FakePrices,
    FakeRelay,
    FakeUniswap,
    fake_sign_tx,
    make_transport,
)

FAST_KDF: dict[str, Any] = {"kdf": "pbkdf2", "kdf_iterations": 1000}
PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def _isolate_rpc_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The engine honours RPC_BASE_URL / RPC_ROBINHOOD_URL; tests must not."""
    monkeypatch.delenv("RPC_BASE_URL", raising=False)
    monkeypatch.delenv("RPC_ROBINHOOD_URL", raising=False)
    monkeypatch.delenv("UNISWAP_API_KEY", raising=False)


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    return tmp_path / "wallets"


@pytest.fixture
def vault(vault_root: Path) -> Vault:
    return Vault(vault_root, **FAST_KDF)


@pytest.fixture
def ledger() -> Iterator[Ledger]:
    store = Ledger(":memory:")
    yield store
    store.close()


@pytest.fixture
def base_chain() -> FakeChain:
    chain = FakeChain(chain_id=8453, block=1_000)
    chain.tokens[USDC] = ("USDC", "USD Coin", 6)
    chain.tokens[WETH] = ("WETH", "Wrapped Ether", 18)
    return chain


@pytest.fixture
def robinhood_chain() -> FakeChain:
    chain = FakeChain(chain_id=4663, block=5_000)
    chain.tokens[AAPL] = ("AAPL", "Apple • Robinhood Token", 18)
    return chain


@pytest.fixture
def fake_uniswap() -> FakeUniswap:
    return FakeUniswap()


@pytest.fixture
def fake_aggregator() -> FakeAggregator:
    return FakeAggregator()


@pytest.fixture
def fake_prices() -> FakePrices:
    prices = FakePrices()
    prices.spot[("base", USDC)] = 1.0
    prices.spot[("base", WETH)] = 2000.0
    prices.spot[("robinhood", AAPL)] = 150.0
    prices.lists["base"] = [
        {
            "chainId": 8453,
            "address": USDC,
            "symbol": "USDC",
            "name": "USD Coin",
            "decimals": 6,
            "logoURI": "https://img/usdc",
        },
        {
            "chainId": 8453,
            "address": WETH,
            "symbol": "WETH",
            "name": "Wrapped Ether",
            "decimals": 18,
        },
    ]
    prices.lists["robinhood"] = [
        {
            "chainId": 4663,
            "address": AAPL,
            "symbol": "AAPL",
            "name": "Apple • Robinhood Token",
            "decimals": 18,
        },
        {
            "chainId": 4663,
            "address": "0xaaaa000000000000000000000000000000000002",
            "symbol": "AAPL",
            "name": "Apple Fan Coin",
            "decimals": 18,
        },
        {
            "chainId": 4663,
            "address": "0xaaaa000000000000000000000000000000000003",
            "symbol": "WETH",
            "name": "Wrapped Ether",
            "decimals": 18,
        },
    ]
    prices.spot[("robinhood", "0xaaaa000000000000000000000000000000000003")] = 2000.0
    return prices


@pytest.fixture
def fake_indexer() -> FakeIndexer:
    return FakeIndexer()


@pytest.fixture
def fake_relay() -> FakeRelay:
    return FakeRelay()


@pytest.fixture
def transport(
    base_chain: FakeChain,
    robinhood_chain: FakeChain,
    fake_uniswap: FakeUniswap,
    fake_aggregator: FakeAggregator,
    fake_prices: FakePrices,
    fake_indexer: FakeIndexer,
    fake_relay: FakeRelay,
) -> httpx.MockTransport:
    return make_transport(
        chains={
            "mainnet.base.org": base_chain,
            "rpc.mainnet.chain.robinhood.com": robinhood_chain,
        },
        uniswap=fake_uniswap,
        aggregator=fake_aggregator,
        prices=fake_prices,
        indexer=fake_indexer,
        relay=fake_relay,
    )


@pytest_asyncio.fixture
async def http(transport: httpx.MockTransport) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=transport) as client:
        yield client


@pytest.fixture
def trading_config() -> TradingConfig:
    return TradingConfig(
        uniswap_api_key="test-key",
        approval_threshold_usd=100.0,
        daily_cap_usd=1000.0,
        approval_ttl_seconds=900,
        sync_interval_seconds=5,
        price_ttl_seconds=1,
    )


@pytest.fixture
def gateway_config(trading_config: TradingConfig) -> GatewayConfig:
    config = GatewayConfig()
    config.trading = trading_config
    return config


@pytest_asyncio.fixture
async def service(
    gateway_config: GatewayConfig,
    vault: Vault,
    ledger: Ledger,
    http: httpx.AsyncClient,
) -> AsyncIterator[TradingService]:
    events: list[tuple[str, dict[str, Any]]] = []

    async def broadcast(event: str, payload: dict[str, Any]) -> None:
        events.append((event, payload))

    svc = TradingService(
        gateway_config,
        vault=vault,
        ledger=ledger,
        prices=PriceService(http=http, ttl_s=0),
        http=http,
        broadcast=broadcast,
        sign_tx=fake_sign_tx,
        background=False,
    )
    svc.events = events  # type: ignore[attr-defined]
    set_trading_service(svc)
    try:
        yield svc
    finally:
        set_trading_service(None)
        await svc.stop()
        await asyncio.sleep(0)


@pytest_asyncio.fixture
async def funded_service(service: TradingService, base_chain: FakeChain) -> TradingService:
    """A vault with one wallet holding 1 ETH and 1,000 USDC on Base."""
    service.vault.setup(PASSWORD, "auto")
    wallet = await service.create_wallet("Main")
    address = wallet["address"]
    base_chain.set_native(address, 10**18)
    base_chain.set_erc20(USDC, address, 1_000 * 10**6)
    service.test_wallet = address  # type: ignore[attr-defined]
    return service


__all__ = ["BASE", "ROBINHOOD", "WALLET", "PASSWORD", "FAST_KDF"]
