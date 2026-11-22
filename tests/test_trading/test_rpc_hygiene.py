"""JSON-RPC calls that buy nothing (issue #3976), and seeing what the engine sends.

On a metered endpoint every JSON-RPC call is billed, each item of a batch array
on its own. Four things around the sync loop's client:

* a token whose ``balanceOf`` never answers is not asked again on every pass;
* every request is counted, by lane and method, and ``trading.status`` shows it;
* a changed ``trading.rpc_urls`` reaches the engine without a restart;
* an endpoint that refuses the key is left alone by the background sync for a while.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from agentos.gateway.config import GatewayConfig, TradingConfig
from agentos.trading.chains import BASE, MULTICALL3, NATIVE_ADDRESS, ROBINHOOD
from agentos.trading.evm import EvmClient, EvmTransportError
from agentos.trading.ledger import Ledger
from agentos.trading.prices import PriceService, TokenMeta, native_token
from agentos.trading.service import SYNC_REQUESTED, TradingService
from agentos.trading.sync import WalletSyncer
from agentos.trading.vault import Vault, WalletRecord
from tests.test_trading.fakes import (
    OTHER,
    USDC,
    WALLET,
    WETH,
    FakeAggregator,
    FakeChain,
    FakeIndexer,
    FakePrices,
    FakeRelay,
    fake_sign_tx,
    make_transport,
)

# A token whose ``balanceOf`` reverts, as an airdropped honeypot's does.
JUNK = "0x9999000000000000000000000000000000000009"
PAID = "https://paid.rpc.test/dummy-key-0123456789"
PUBLIC = "https://public.rpc.test/"
BASE_HOST = "mainnet.base.org"
RH_HOST = "rpc.mainnet.chain.robinhood.com"
CUSTOM_HOST = "custom.rpc.test"
CUSTOM_BASE = f"https://{CUSTOM_HOST}/v2/dummy-key-0123456789"


# ── A: a token that never answers ──────────────────────────────────────────


@pytest.fixture
def node() -> FakeChain:
    chain = FakeChain(chain_id=8453, block=10_000)
    chain.tokens[USDC] = ("USDC", "USD Coin", 6)
    chain.tokens[WETH] = ("WETH", "Wrapped Ether", 18)
    chain.set_erc20(USDC, WALLET, 5)
    chain.fail_balance_of.add(JUNK)
    return chain


@pytest.fixture
async def http_lanes(node: FakeChain) -> AsyncIterator[httpx.AsyncClient]:
    """The paid RPC and the chain's public node, both answered by ``node``."""
    prices = FakePrices()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host in ("paid.rpc.test", "public.rpc.test"):
            return node.handle(request)
        return prices.handle(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        yield http


def _syncer(ledger: Ledger, http: httpx.AsyncClient, evm: EvmClient) -> WalletSyncer:
    prices = PriceService(http=http, ttl_s=0)

    async def token_meta(spec: Any, address: str) -> TokenMeta:
        if address == NATIVE_ADDRESS:
            return native_token(spec)
        return TokenMeta(spec.chain_id, address, "", "", 18)

    async def watch(spec: Any) -> list[str]:
        return [USDC, WETH, JUNK]

    return WalletSyncer(
        ledger, prices, evm_for=lambda spec: evm, token_meta=token_meta, watch_tokens=watch
    )


@pytest.fixture
def evm(http_lanes: httpx.AsyncClient) -> EvmClient:
    # As the service builds it for a custom RPC: bulk reads try the public node first.
    return EvmClient(PAID, http=http_lanes, multicall3=MULTICALL3, read_url=PUBLIC)


@pytest.fixture
def syncer(ledger: Ledger, http_lanes: httpx.AsyncClient, evm: EvmClient) -> WalletSyncer:
    return _syncer(ledger, http_lanes, evm)


def _wallet() -> WalletRecord:
    record = WalletRecord(address=WALLET, label="W", created_at=1.0)
    record.created_block["8453"] = 9_000
    return record


def _reads(node: FakeChain, token: str) -> int:
    """How often ``token`` was asked ``balanceOf`` since ``node.calls`` was cleared."""
    return node.balance_reads().count(token)


def _read_status(ledger: Ledger) -> tuple[str, str | None]:
    row = ledger.chain_reads(WALLET, 8453)[0]
    return str(row["status"]), row.get("reason")


PARTIAL = ("partial", "1 token balance(s) could not be read")


class TestUnanswerableToken:
    async def test_it_is_read_once_then_left_out_of_ordinary_passes(
        self, syncer: WalletSyncer, node: FakeChain, ledger: Ledger
    ) -> None:
        await syncer.sync(_wallet(), BASE)  # the wallet's first pass reads everything
        assert _reads(node, JUNK) == 2  # in the multicall, then alone on the RPC
        assert _read_status(ledger) == PARTIAL
        for _ in range(3):
            node.calls.clear()
            await syncer.sync(_wallet(), BASE)
            assert _reads(node, JUNK) == 0
            assert _reads(node, USDC) == 1  # a held token is still read every pass
            # Unknown is still unknown: the read does not turn ok because it was not tried.
            assert _read_status(ledger) == PARTIAL
        assert ledger.get_balance(8453, WALLET, JUNK) is None

    async def test_an_ordinary_pass_costs_the_paid_rpc_nothing_for_it(
        self, syncer: WalletSyncer, evm: EvmClient
    ) -> None:
        """Measured 2026-11-22: +1 billed ``eth_call`` per wallet per pass, forever."""
        await syncer.sync(_wallet(), BASE)
        evm.rpc_usage.window()
        await syncer.sync(_wallet(), BASE)
        window = evm.rpc_usage.window()
        assert "eth_call" not in window["rpc"]["methods"]
        assert window["read"]["methods"] == {"eth_call": 1}  # the one multicall

    async def test_the_passes_that_read_everything_retry_it(
        self, syncer: WalletSyncer, node: FakeChain, ledger: Ledger
    ) -> None:
        await syncer.sync(_wallet(), BASE)
        for kwargs in ({"read_all": True}, {"include_hidden": True}):
            node.calls.clear()
            await syncer.sync(_wallet(), BASE, **kwargs)
            assert _reads(node, JUNK) == 2
            assert _read_status(ledger) == PARTIAL
            node.calls.clear()
            await syncer.sync(_wallet(), BASE)
            assert _reads(node, JUNK) == 0
            assert _read_status(ledger) == PARTIAL

    async def test_a_move_always_retries_it(
        self, syncer: WalletSyncer, node: FakeChain, ledger: Ledger
    ) -> None:
        """An airdrop: the sweep books it, the read still fails, and it stays out after."""
        await syncer.sync(_wallet(), BASE)
        node.add_transfer(token=JUNK, sender=OTHER, recipient=WALLET, amount=10**18, block=10_001)
        # The head is past the sweep's overlap, so only this pass sees the transfer.
        node.block = 10_100
        node.calls.clear()
        await syncer.sync(_wallet(), BASE)
        assert _reads(node, JUNK) == 2
        assert JUNK in {p.token for p in ledger.positions(WALLET)}
        assert _read_status(ledger) == PARTIAL
        # An open position with no row used to keep it live for good.
        node.calls.clear()
        await syncer.sync(_wallet(), BASE)
        assert _reads(node, JUNK) == 0
        assert _read_status(ledger) == PARTIAL

    async def test_a_token_that_starts_answering_clears_on_the_next_full_read(
        self, syncer: WalletSyncer, node: FakeChain, ledger: Ledger
    ) -> None:
        await syncer.sync(_wallet(), BASE)
        node.fail_balance_of.clear()
        node.set_erc20(JUNK, WALLET, 4)
        await syncer.sync(_wallet(), BASE)
        assert _read_status(ledger) == PARTIAL  # not asked, so not known yet
        await syncer.sync(_wallet(), BASE, read_all=True)
        assert ledger.get_balance(8453, WALLET, JUNK) == 4
        assert _read_status(ledger) == ("ok", None)
        node.calls.clear()
        await syncer.sync(_wallet(), BASE)
        assert _reads(node, JUNK) == 1  # held now: an ordinary live token
        assert _read_status(ledger) == ("ok", None)

    async def test_a_token_that_starts_answering_clears_when_it_moves(
        self, syncer: WalletSyncer, node: FakeChain, ledger: Ledger
    ) -> None:
        await syncer.sync(_wallet(), BASE)
        node.fail_balance_of.clear()
        node.add_transfer(token=JUNK, sender=OTHER, recipient=WALLET, amount=3, block=10_001)
        node.set_erc20(JUNK, WALLET, 3)
        node.block = 10_001
        await syncer.sync(_wallet(), BASE)
        assert ledger.get_balance(8453, WALLET, JUNK) == 3
        assert _read_status(ledger) == ("ok", None)

    async def test_a_held_token_whose_read_fails_is_still_read_every_pass(
        self, syncer: WalletSyncer, node: FakeChain, ledger: Ledger
    ) -> None:
        """It has a row: a failure there is most likely a flaky node, and it clears at once."""
        node.fail_balance_of.clear()
        await syncer.sync(_wallet(), BASE)
        node.fail_balance_of.add(USDC)
        for _ in range(2):
            node.calls.clear()
            await syncer.sync(_wallet(), BASE)
            assert _reads(node, USDC) == 2
            assert _read_status(ledger) == PARTIAL
            assert ledger.get_balance(8453, WALLET, USDC) == 5  # last good, never a zero
        node.fail_balance_of.clear()
        await syncer.sync(_wallet(), BASE)
        assert _read_status(ledger) == ("ok", None)

    async def test_two_unreadable_tokens_are_counted_on_every_pass(
        self, syncer: WalletSyncer, node: FakeChain, ledger: Ledger
    ) -> None:
        """One known-unreadable and one failing now: the count is both, every pass."""
        await syncer.sync(_wallet(), BASE)
        node.fail_balance_of.add(USDC)
        for _ in range(2):
            await syncer.sync(_wallet(), BASE)
            assert _read_status(ledger) == ("partial", "2 token balance(s) could not be read")

    async def test_after_a_restart_it_is_read_once_more(
        self,
        syncer: WalletSyncer,
        node: FakeChain,
        ledger: Ledger,
        http_lanes: httpx.AsyncClient,
        evm: EvmClient,
    ) -> None:
        await syncer.sync(_wallet(), BASE)
        restarted = _syncer(ledger, http_lanes, evm)
        node.calls.clear()
        await restarted.sync(_wallet(), BASE)
        assert _reads(node, JUNK) == 2
        node.calls.clear()
        await restarted.sync(_wallet(), BASE)
        assert _reads(node, JUNK) == 0
        assert _read_status(ledger) == PARTIAL

    async def test_a_rebuild_that_reads_it_resets_what_is_known(
        self, syncer: WalletSyncer, node: FakeChain, ledger: Ledger
    ) -> None:
        """A rebuild reads everything itself; a zero there is a zero, not still unknown."""
        await syncer.sync(_wallet(), BASE)
        node.fail_balance_of.clear()
        await syncer.sync(_wallet(), BASE, full=True)
        assert ledger.get_balance(8453, WALLET, JUNK) == 0
        assert _read_status(ledger) == ("ok", None)
        await syncer.sync(_wallet(), BASE)
        assert _read_status(ledger) == ("ok", None)


# ── B: the usage counter ───────────────────────────────────────────────────


class TestUsageCounter:
    @pytest.fixture
    async def lanes(self) -> AsyncIterator[tuple[EvmClient, dict[str, Any]]]:
        paid, public = FakeChain(chain_id=8453), FakeChain(chain_id=8453)
        paid.set_erc20(USDC, WALLET, 42)
        public.set_erc20(USDC, WALLET, 42)
        rule: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "public.rpc.test":
                return public.handle(request)
            if rule.get("status"):
                return httpx.Response(rule["status"], json={"error": {"message": "no"}})
            if rule.get("timeout"):
                raise httpx.ReadTimeout("timed out", request=request)
            return paid.handle(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            yield EvmClient(PAID, http=http, multicall3=MULTICALL3, read_url=PUBLIC), rule

    @staticmethod
    def _lane(requests: int = 0, calls: int = 0, failed: int = 0, **methods: int) -> dict:
        return {"requests": requests, "calls": calls, "failed": failed, "methods": methods}

    async def test_a_single_call(self, lanes: Any) -> None:
        client, _ = lanes
        await client.block_number()
        usage = client.usage()
        assert usage["rpc"] == self._lane(1, 1, eth_blockNumber=1)
        assert usage["read"] == self._lane()
        assert usage["since"] <= time.time()

    async def test_a_batch_is_one_request_and_one_call_per_item(self, lanes: Any) -> None:
        client, _ = lanes
        await client.batch(
            [("eth_blockNumber", []), ("eth_chainId", []), ("eth_getBalance", [WALLET, "latest"])]
        )
        assert client.usage()["rpc"] == self._lane(
            1, 3, eth_blockNumber=1, eth_chainId=1, eth_getBalance=1
        )

    async def test_the_read_lane_is_counted_on_its_own(self, lanes: Any) -> None:
        client, _ = lanes
        assert await client.erc20_balances(WALLET, [USDC, WETH]) == {USDC: 42, WETH: 0}
        usage = client.usage()
        assert usage["read"] == self._lane(1, 1, eth_call=1)
        assert usage["rpc"] == self._lane()

    async def test_a_refused_request_is_counted_and_carries_its_status(self, lanes: Any) -> None:
        client, rule = lanes
        rule["status"] = 403
        with pytest.raises(EvmTransportError) as refused:
            await client.block_number()
        assert refused.value.status == 403
        assert "dummy-key" not in str(refused.value)
        assert client.usage()["rpc"] == self._lane(1, 1, 1, eth_blockNumber=1)

    async def test_a_timeout_is_a_failure_without_a_status(self, lanes: Any) -> None:
        client, rule = lanes
        rule["timeout"] = True
        with pytest.raises(EvmTransportError) as timed_out:
            await client.block_number()
        assert timed_out.value.status is None
        assert client.usage()["rpc"]["failed"] == 1

    async def test_a_window_is_what_was_sent_since_the_last_one(self, lanes: Any) -> None:
        client, _ = lanes
        await client.block_number()
        await client.block_number()
        assert client.rpc_usage.window()["rpc"] == self._lane(2, 2, eth_blockNumber=2)
        await client.chain_id()
        assert client.rpc_usage.window()["rpc"] == self._lane(1, 1, eth_chainId=1)
        assert client.rpc_usage.window()["rpc"] == self._lane()
        # The running total is untouched by windows.
        assert client.usage()["rpc"]["calls"] == 3


# ── the service: a gate in front of every fake host ───────────────────────


@dataclass
class Gate:
    """Records every request's host; answers a status or times out for a host instead."""

    refuse: dict[str, int | str] = field(default_factory=dict)
    seen: list[str] = field(default_factory=list)

    def hits(self, host: str) -> int:
        return self.seen.count(host)


@pytest.fixture
def gate() -> Gate:
    return Gate()


@pytest.fixture
def transport(
    base_chain: FakeChain,
    robinhood_chain: FakeChain,
    fake_aggregator: FakeAggregator,
    fake_prices: FakePrices,
    fake_indexer: FakeIndexer,
    fake_relay: FakeRelay,
    gate: Gate,
) -> httpx.MockTransport:
    inner = make_transport(
        chains={BASE_HOST: base_chain, RH_HOST: robinhood_chain, CUSTOM_HOST: base_chain},
        aggregator=fake_aggregator,
        prices=fake_prices,
        indexer=fake_indexer,
        relay=fake_relay,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        gate.seen.append(host)
        rule = gate.refuse.get(host)
        if rule == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if isinstance(rule, int):
            return httpx.Response(rule, json={"error": {"message": "refused"}})
        return inner.handle_request(request)

    return httpx.MockTransport(handler)


def _chain_row(status: dict[str, Any], chain_id: int) -> dict[str, Any]:
    return next(c for c in status["chains"] if c["chainId"] == chain_id)


def _read(service: TradingService, chain_id: int) -> dict[str, Any]:
    wallet = service.test_wallet  # type: ignore[attr-defined]
    return next(r for r in service.chain_reads(wallet) if r["chainId"] == chain_id)


class TestStatusUsage:
    async def test_status_reports_usage_per_chain_and_never_a_url(
        self, funded_service: TradingService
    ) -> None:
        service = funded_service
        service.config.rpc_urls = {"8453": CUSTOM_BASE}
        await service.sync_all()
        await service.sync_all()
        status = await service.status()
        base = _chain_row(status, 8453)["rpcUsage"]
        assert base["rpc"]["calls"] == sum(base["rpc"]["methods"].values())
        assert base["rpc"]["methods"]["eth_blockNumber"] >= 2
        assert base["rpc"]["requests"] <= base["rpc"]["calls"]
        # A custom RPC: the bulk balance read went to the public node.
        assert base["read"]["methods"] == {"eth_call": 2}
        assert base["read"]["failed"] == 0 and base["rpc"]["failed"] == 0
        assert isinstance(base["since"], int) and base["since"] <= time.time() * 1000
        robinhood = _chain_row(status, 4663)["rpcUsage"]
        assert robinhood["read"]["requests"] == 0  # no custom RPC there: one node
        assert robinhood["rpc"]["calls"] > 0
        text = json.dumps([base, robinhood])
        assert "http" not in text and CUSTOM_HOST not in text and "dummy-key" not in text

    async def test_a_chain_not_talked_to_yet_has_no_count(self, service: TradingService) -> None:
        status = await service.status()
        assert [c["rpcUsage"] for c in status["chains"]] == [None, None]
        checked = await service.status(check_rpc=True)
        assert _chain_row(checked, 8453)["rpcUsage"]["rpc"]["methods"] == {"eth_chainId": 1}

    async def test_the_engine_logs_usage_about_once_an_hour(
        self, service: TradingService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = [1_800_000_000.0]
        monkeypatch.setattr(service, "_now", lambda: clock[0])
        service._rpc_usage_logged_at = clock[0]
        client = service.evm(BASE)
        await client.block_number()
        await client.block_number()
        with capture_logs() as logs:
            service._log_rpc_usage()  # too early
            clock[0] += 3600
            service._log_rpc_usage()
            await client.chain_id()
            clock[0] += 1800
            service._log_rpc_usage()  # half an hour: not yet
            clock[0] += 1800
            service._log_rpc_usage()
            clock[0] += 3600
            service._log_rpc_usage()  # nothing sent: nothing to say
        lines = [e for e in logs if e["event"] == "trading.rpc_usage"]
        assert [(e["chain"], e["minutes"], e["rpc_calls"], e["methods"]) for e in lines] == [
            ("base", 60, 2, {"eth_blockNumber": 2}),
            ("base", 60, 1, {"eth_chainId": 1}),
        ]
        assert all(e["log_level"] == "info" for e in lines)
        assert "http" not in json.dumps(lines)

    async def test_the_engine_loop_asks_for_the_usage_line(
        self, service: TradingService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class StopLoopError(Exception):
            pass

        async def nothing() -> None:
            return None

        def stop() -> None:
            raise StopLoopError

        monkeypatch.setattr(service, "housekeep", nothing)
        monkeypatch.setattr(service, "_kick_sync", lambda: None)
        monkeypatch.setattr(service, "_log_rpc_usage", stop)
        with pytest.raises(StopLoopError):
            await service._run_loop()


# ── C: a changed RPC URL ───────────────────────────────────────────────────


class TestRpcUrlChange:
    async def test_a_new_url_gets_a_new_client(
        self, service: TradingService, gate: Gate, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        old = service.evm(BASE)
        assert old.url == BASE.rpc_url and service.evm(BASE) is old
        await old.block_number()
        with capture_logs() as logs:
            service.config.rpc_urls = {"8453": CUSTOM_BASE}
            fresh = service.evm(BASE)
        assert fresh is not old and fresh.url == CUSTOM_BASE
        assert fresh.read_url == BASE.rpc_url
        assert service.evm(BASE) is fresh
        assert service.evm(ROBINHOOD).url == ROBINHOOD.rpc_url
        changed = [e for e in logs if e["event"] == "trading.rpc_url_changed"]
        assert [(e["chain"], e["rpc"]) for e in changed] == [("base", f"https://{CUSTOM_HOST}/…")]
        gate.seen.clear()
        await fresh.block_number()
        assert gate.seen == [CUSTOM_HOST]
        # Whoever still holds the old client finishes on it: it was not closed.
        await old.block_number()
        # The count is the chain's, across both clients.
        assert service.evm(BASE).usage()["rpc"]["methods"] == {"eth_blockNumber": 3}

    async def test_a_replaced_trading_section_and_the_env_fallback_count_too(
        self,
        service: TradingService,
        gateway_config: GatewayConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        first = service.evm(BASE)
        gateway_config.trading = TradingConfig(rpc_urls={"base": CUSTOM_BASE})
        assert service.evm(BASE).url == CUSTOM_BASE
        gateway_config.trading = TradingConfig()
        monkeypatch.setenv("RPC_BASE_URL", f"https://{CUSTOM_HOST}/env")
        assert service.evm(BASE).url == f"https://{CUSTOM_HOST}/env"
        monkeypatch.delenv("RPC_BASE_URL")
        back = service.evm(BASE)
        assert back.url == BASE.rpc_url and back is not first
        assert back.rpc_usage is first.rpc_usage

    async def test_the_sync_uses_the_new_url_on_its_next_pass(
        self, funded_service: TradingService, gate: Gate
    ) -> None:
        service = funded_service
        await service.sync_all()
        service.config.rpc_urls = {"8453": CUSTOM_BASE}
        gate.seen.clear()
        await service.sync_all()
        assert gate.hits(CUSTOM_HOST) > 0
        assert _read(service, 8453)["status"] == "ok"

    async def test_the_factory_seam_is_asked_again(
        self, gateway_config: GatewayConfig, vault: Vault, ledger: Ledger, http: httpx.AsyncClient
    ) -> None:
        made: list[EvmClient] = []

        def factory(chain: Any) -> EvmClient:
            client = EvmClient(chain.rpc_url, http=http)
            made.append(client)
            return client

        service = TradingService(
            gateway_config,
            vault=vault,
            ledger=ledger,
            prices=PriceService(http=http, ttl_s=0),
            http=http,
            evm_factory=factory,
            sign_tx=fake_sign_tx,
            background=False,
        )
        try:
            assert service.evm(BASE) is made[0] and service.evm(BASE) is made[0]
            await made[0].block_number()
            service.config.rpc_urls = {"8453": CUSTOM_BASE}
            assert service.evm(BASE) is made[1] and len(made) == 2
            assert made[1].usage()["rpc"]["methods"] == {"eth_blockNumber": 1}
        finally:
            await service.stop()


# ── D: refused credentials ─────────────────────────────────────────────────


@pytest.fixture
def clock(funded_service: TradingService, monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [time.time()]
    monkeypatch.setattr(funded_service, "_now", lambda: now[0])
    return now


class TestRefusedKey:
    async def test_two_refused_passes_pause_the_chain(
        self, funded_service: TradingService, gate: Gate
    ) -> None:
        service = funded_service
        await service.sync_all()
        gate.refuse[BASE_HOST] = 403
        with capture_logs() as logs:
            for expected in (1, 1, 0, 0):
                gate.seen.clear()
                await service.sync_all()
                assert gate.hits(BASE_HOST) == expected
                assert gate.hits(RH_HOST) > 0  # the other chain keeps syncing
        events = [e["event"] for e in logs if e["event"].startswith("trading.rpc_key")]
        assert events == ["trading.rpc_key_refused"]
        refused = _read(service, 8453)
        assert refused["status"] == "failed"
        assert "key refused" in refused["reason"] and "HTTP 403" in refused["reason"]
        assert _read(service, 4663)["status"] == "ok"

    async def test_a_requested_sync_still_tries_and_a_success_ends_the_pause(
        self, funded_service: TradingService, gate: Gate
    ) -> None:
        service = funded_service
        await service.sync_all()
        gate.refuse[BASE_HOST] = 401
        await service.sync_all()
        await service.sync_all()
        gate.seen.clear()
        await service.sync_all(kind=SYNC_REQUESTED)  # the resync button
        assert gate.hits(BASE_HOST) == 1
        del gate.refuse[BASE_HOST]
        with capture_logs() as logs:
            await service.sync_all(kind=SYNC_REQUESTED)
        assert [e["event"] for e in logs if e["event"].startswith("trading.rpc_key")] == [
            "trading.rpc_key_accepted"
        ]
        assert _read(service, 8453)["status"] == "ok"
        gate.seen.clear()
        await service.sync_all()
        assert gate.hits(BASE_HOST) > 0

    async def test_the_pause_grows_to_a_cap(
        self, funded_service: TradingService, gate: Gate, clock: list[float]
    ) -> None:
        service = funded_service
        await service.sync_all()
        gate.refuse[BASE_HOST] = 403
        await service.sync_all()
        await service.sync_all()  # paused for a minute
        tried = []
        for step in (59, 2, 119, 2, 299, 2, 899, 2, 899, 2):
            clock[0] += step
            gate.seen.clear()
            await service.sync_all()
            tried.append(gate.hits(BASE_HOST))
        # 1, 2, 5, then 15 minutes, and 15 minutes from then on.
        assert tried == [0, 1, 0, 1, 0, 1, 0, 1, 0, 1]

    async def test_a_success_after_the_pause_resets_its_growth(
        self, funded_service: TradingService, gate: Gate, clock: list[float]
    ) -> None:
        service = funded_service
        await service.sync_all()
        gate.refuse[BASE_HOST] = 403
        for _ in range(3):
            await service.sync_all()
            clock[0] += 61  # paused for 1, then 2 minutes
        del gate.refuse[BASE_HOST]
        clock[0] += 120
        await service.sync_all()
        assert _read(service, 8453)["status"] == "ok"
        gate.refuse[BASE_HOST] = 403
        await service.sync_all()
        await service.sync_all()  # paused again, from the first step
        clock[0] += 61
        gate.seen.clear()
        await service.sync_all()
        assert gate.hits(BASE_HOST) == 1

    @pytest.mark.parametrize("rule", [500, 502, 429, "timeout"])
    async def test_a_server_error_or_a_timeout_never_pauses(
        self, funded_service: TradingService, gate: Gate, rule: int | str
    ) -> None:
        service = funded_service
        await service.sync_all()
        gate.refuse[BASE_HOST] = rule
        for _ in range(4):
            gate.seen.clear()
            await service.sync_all()
            assert gate.hits(BASE_HOST) == 1
        assert _read(service, 8453)["status"] == "failed"

    async def test_with_many_wallets_one_pass_is_enough_and_the_rest_are_not_asked(
        self, funded_service: TradingService, gate: Gate
    ) -> None:
        service = funded_service
        await service.import_wallet("Second", private_key="0x" + "22" * 32)
        await service.import_wallet("Third", private_key="0x" + "33" * 32)
        await service.sync_all()
        gate.refuse[BASE_HOST] = 403
        gate.seen.clear()
        await service.sync_all()
        assert gate.hits(BASE_HOST) == 2  # two refusals in a row; the third is spared
        reads = [r for r in service.chain_reads() if r["chainId"] == 8453]
        assert len(reads) == 3 and {r["status"] for r in reads} == {"failed"}
        # The one not asked still says why its figures are not fresh.
        assert sorted(r["reason"].startswith("paused, key refused") for r in reads) == [
            False,
            False,
            True,
        ]
        gate.seen.clear()
        await service.sync_all()
        assert gate.hits(BASE_HOST) == 0

    async def test_a_new_url_is_tried_at_once(
        self, funded_service: TradingService, gate: Gate
    ) -> None:
        service = funded_service
        await service.sync_all()
        gate.refuse[BASE_HOST] = 401
        await service.sync_all()
        await service.sync_all()
        service.config.rpc_urls = {"8453": CUSTOM_BASE}
        gate.seen.clear()
        await service.sync_all()
        assert gate.hits(CUSTOM_HOST) > 0
        assert _read(service, 8453)["status"] == "ok"

    async def test_housekeeping_is_never_held_by_the_pause(
        self, funded_service: TradingService, gate: Gate, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = funded_service
        await service.sync_all()
        gate.refuse[BASE_HOST] = 403
        await service.sync_all()
        await service.sync_all()
        ran: list[str] = []

        async def dca() -> None:
            ran.append("dca")

        async def triggers() -> None:
            ran.append("triggers")

        monkeypatch.setattr(service, "dca_run_due", dca)
        monkeypatch.setattr(service, "trigger_check", triggers)
        await service.housekeep()
        assert ran == ["dca", "triggers"]
