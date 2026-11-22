"""How the fixes for #3976 behave together.

Each change has its own tests: the wide, resumable catch-up
(``test_sync_catchup``), the usage counter, URL follow and refused-key pause
(``test_rpc_hygiene``), the cheaper read paths (``test_rpc_read_paths``).
These cover what only shows where they meet: a steady pass on a metered
endpoint counted both by the engine and by the node, a catch-up whose
endpoint starts refusing the key half-way, and an RPC URL changed between two
passes of a catch-up. Also the rule that joins the first two: a refused key or
a rate limit is not a range problem, so the log readers do not halve it.
"""

from __future__ import annotations

import dataclasses
import json
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from agentos.trading.chains import BASE
from agentos.trading.evm import NO_HALVING_STATUSES, EvmClient, EvmTransportError
from agentos.trading.service import AUTH_PAUSE_S, TradingService
from agentos.trading.sync import SYNC_OVERLAP_BLOCKS
from tests.test_trading.fakes import (
    OTHER,
    USDC,
    WALLET,
    FakeAggregator,
    FakeChain,
    FakeDexBridge,
    FakeIndexer,
    FakePrices,
    make_transport,
)

BASE_HOST = "mainnet.base.org"
RH_HOST = "rpc.mainnet.chain.robinhood.com"
PAID_BASE_HOST = "base.paid.rpc.test"
PAID_RH_HOST = "robinhood.paid.rpc.test"
PAID_BASE = f"https://{PAID_BASE_HOST}/v2/dummy-key-0123456789"
PAID_RH = f"https://{PAID_RH_HOST}/v2/dummy-key-0123456789"
NODE_SPAN = 2_000  # EvmClient's default max_log_span, as the service builds it
WINDOW = BASE.transfer_log_span
# 100k halved down to the node span: what a refusal that is about the range costs.
HALVINGS = [100_000, 50_000, 25_000, 12_500, 6_250, 3_125, NODE_SPAN]
# A listed token whose ``balanceOf`` reverts for every holder (an airdropped honeypot).
JUNK = "0x9999000000000000000000000000000000000009"
SECOND_KEY = "0x" + "22" * 32

Rule = Callable[[str, dict[str, Any]], httpx.Response | None]


def _refusal(status: int | str) -> httpx.Response:
    if status == "timeout":
        raise httpx.ReadTimeout("timed out")
    assert isinstance(status, int)
    return httpx.Response(status, json={"error": {"code": -32000, "message": "refused"}})


def _from_block(item: dict[str, Any]) -> int:
    return int(item["params"][0]["fromBlock"], 16)


# ── the log readers: a refused key or a rate limit is not halved ───────────


@dataclass
class Node:
    """An EvmClient over a fake node that can refuse every eth_getLogs."""

    client: EvmClient
    answer: int | str | None = None
    #: The span of every eth_getLogs asked, refused ones included.
    spans: list[int] = field(default_factory=list)


@pytest.fixture
async def node() -> AsyncIterator[Node]:
    chain = FakeChain(chain_id=8453, block=200_000)
    holder: dict[str, Node] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        item = json.loads(request.content or b"null")
        if isinstance(item, dict) and item.get("method") == "eth_getLogs":
            flt = item["params"][0]
            holder["node"].spans.append(int(flt["toBlock"], 16) - int(flt["fromBlock"], 16) + 1)
            if holder["node"].answer is not None:
                return _refusal(holder["node"].answer)
        return chain.handle(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        holder["node"] = Node(EvmClient(f"https://{BASE_HOST}", http=http))
        yield holder["node"]


async def _read_logs(node: Node, kind: str) -> None:
    reader = node.client.transfer_logs if kind == "transfer" else node.client.approval_logs
    await reader(WALLET, from_block=1, to_block=100_000, max_span=100_000)


@pytest.mark.parametrize("kind", ["transfer", "approval"])
class TestNoHalvingOnRefusal:
    @pytest.mark.parametrize("status", [401, 403, 429])
    async def test_a_refused_key_or_a_rate_limit_is_raised_at_once(
        self, node: Node, kind: str, status: int
    ) -> None:
        """A narrower range is refused the same way; the old halving cost seven requests."""
        assert status in NO_HALVING_STATUSES
        node.answer = status
        with pytest.raises(EvmTransportError) as refused:
            await _read_logs(node, kind)
        assert refused.value.status == status
        assert node.spans == [100_000]

    @pytest.mark.parametrize("status", [400, 500, 502, "timeout"])
    async def test_a_range_refusal_or_a_timeout_still_halves_to_the_node_span(
        self, node: Node, kind: str, status: int | str
    ) -> None:
        """A load-balanced gateway says "range too large" with a 400/500."""
        node.answer = status
        with pytest.raises(EvmTransportError) as failed:
            await _read_logs(node, kind)
        assert failed.value.status == (None if status == "timeout" else status)
        assert node.spans == HALVINGS


# ── the service: every chain host behind one wire ──────────────────────────


@dataclass
class Wire:
    """The chain hosts the engine can reach, what was sent to each, and a rule in front."""

    hosts: dict[str, FakeChain]
    #: (host, JSON-RPC methods) per HTTP request, the refused ones included.
    sent: list[tuple[str, list[str]]] = field(default_factory=list)
    #: (host, method) of each request the rule answered instead of the node.
    refused: list[tuple[str, str]] = field(default_factory=list)
    #: (host, filter) of every eth_getLogs.
    filters: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    rule: Rule | None = None

    def requests(self, host: str) -> int:
        return sum(1 for sent_to, _ in self.sent if sent_to == host)

    def methods(self, host: str) -> Counter[str]:
        return Counter(m for sent_to, ms in self.sent if sent_to == host for m in ms)

    def ranges(self, host: str, wallet: str) -> list[tuple[int, int]]:
        """The incoming-transfer ranges asked of ``host`` for ``wallet`` (one per window)."""
        topic = "0x" + wallet.lower().removeprefix("0x").rjust(64, "0")
        return [
            (int(f["fromBlock"], 16), int(f["toBlock"], 16))
            for sent_to, f in self.filters
            if sent_to == host and len(f["topics"]) == 3 and f["topics"][2] == topic
        ]

    def clear(self) -> None:
        self.sent.clear()
        self.refused.clear()
        self.filters.clear()


@pytest.fixture
def wire(base_chain: FakeChain, robinhood_chain: FakeChain) -> Wire:
    return Wire(
        hosts={
            BASE_HOST: base_chain,
            RH_HOST: robinhood_chain,
            PAID_BASE_HOST: base_chain,
            PAID_RH_HOST: robinhood_chain,
        }
    )


@pytest.fixture
def transport(
    wire: Wire,
    fake_aggregator: FakeAggregator,
    fake_prices: FakePrices,
    fake_indexer: FakeIndexer,
    fake_bridge: FakeDexBridge,
) -> httpx.MockTransport:
    rest = make_transport(
        chains={},
        aggregator=fake_aggregator,
        prices=fake_prices,
        indexer=fake_indexer,
        bridge=fake_bridge,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        chain = wire.hosts.get(host)
        if chain is None:
            return rest.handle_request(request)
        payload = json.loads(request.content or b"null")
        items = [i for i in (payload if isinstance(payload, list) else [payload]) if i]
        wire.sent.append((host, [str(i.get("method")) for i in items]))
        for item in items:
            if item.get("method") == "eth_getLogs":
                wire.filters.append((host, item["params"][0]))
        if wire.rule is not None and len(items) == 1:
            answer = wire.rule(host, items[0])
            if answer is not None:
                wire.refused.append((host, str(items[0].get("method"))))
                return answer
        return chain.handle(request)

    return httpx.MockTransport(handler)


@pytest.fixture
def clock(funded_service: TradingService, monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [time.time()]
    monkeypatch.setattr(funded_service, "_now", lambda: now[0])
    return now


async def _usage(service: TradingService) -> dict[int, dict[str, Any]]:
    """``trading.status``'s ``rpcUsage`` per chain id."""
    return {c["chainId"]: c["rpcUsage"] for c in (await service.status())["chains"]}


def _sent(before: dict[str, Any], after: dict[str, Any]) -> Counter[str]:
    """The calls one lane was sent between two ``rpcUsage`` reads, by method."""
    sent = Counter(after["methods"])
    sent.subtract(before["methods"])
    assert after["calls"] - before["calls"] == sum(sent.values())
    return +sent


def _deposits(service: TradingService, key: str) -> list[int]:
    return sorted(
        int(e["amount_out_raw"])
        for e in service.ledger.list_entries(wallet=key, kind="deposit", limit=1000)
        if e["tx_hash"]
    )


async def _two_wallets(service: TradingService, base_chain: FakeChain) -> list[str]:
    """The funded wallet and a second, imported one holding 5 USDC on Base; vault order."""
    second = await service.import_wallet("Second", private_key=SECOND_KEY)
    base_chain.set_erc20(USDC, second["address"], 5 * 10**6)
    return [record.key for record in service.vault.list()]


class TestSteadyPassOnAMeteredEndpoint:
    async def test_four_calls_per_wallet_and_chain_counted_by_the_engine_and_the_node(
        self,
        funded_service: TradingService,
        wire: Wire,
        base_chain: FakeChain,
        robinhood_chain: FakeChain,
        fake_prices: FakePrices,
    ) -> None:
        """Measured 2026-11-22: 18 billed calls a pass for 2 wallets x 2 chains, 2 of them
        one token's ``balanceOf``. The steady pass is 4 per wallet x chain, 16."""
        service = funded_service
        keys = await _two_wallets(service, base_chain)
        # A metered RPC on both chains; each chain's public node is the read lane, a
        # node of its own here (same state, own call log) so the two lanes are told apart.
        service.config.rpc_urls = {"8453": PAID_BASE, "4663": PAID_RH}
        public = {
            8453: dataclasses.replace(base_chain, calls=[]),
            4663: dataclasses.replace(robinhood_chain, calls=[]),
        }
        wire.hosts[BASE_HOST], wire.hosts[RH_HOST] = public[8453], public[4663]
        paid = {8453: base_chain, 4663: robinhood_chain}
        base_chain.fail_balance_of.add(JUNK)  # the set is shared with the public node
        fake_prices.lists["base"].append(
            {"chainId": 8453, "address": JUNK, "symbol": "JUNK", "name": "Junk", "decimals": 18}
        )
        fake_prices.spot[("base", JUNK)] = 1.0
        await service.token_meta(BASE, JUNK)  # met once: every wallet is scanned for it

        await service.sync_all()  # each wallet's first pass reads every token
        assert JUNK not in service.ledger.hidden_tokens(8453)
        # It was asked: in the multicall on the read lane, then alone on the paid RPC.
        assert base_chain.balance_reads().count(JUNK) == len(keys)
        assert public[8453].balance_reads().count(JUNK) == len(keys)

        base_chain.block += 150  # 30 s of blocks on each chain
        robinhood_chain.block += 250
        for chain in (*paid.values(), *public.values()):
            chain.calls.clear()
        before = await _usage(service)
        await service.sync_all()
        after = await _usage(service)

        n = len(keys)
        steady = Counter({"eth_blockNumber": n, "eth_getLogs": 2 * n, "eth_getBalance": n})
        for chain_id in (8453, 4663):
            counted = _sent(before[chain_id]["rpc"], after[chain_id]["rpc"])
            logged = Counter(str(c["method"]) for c in paid[chain_id].calls)
            assert counted == logged == steady, chain_id
            assert paid[chain_id].balance_reads() == []  # not one single balanceOf
            assert after[chain_id]["rpc"]["requests"] - before[chain_id]["rpc"]["requests"] == 8
            assert after[chain_id]["rpc"]["failed"] == before[chain_id]["rpc"]["failed"]
            # The read lane agrees with its node too: the balance multicalls, nothing else.
            read = _sent(before[chain_id]["read"], after[chain_id]["read"])
            assert read == Counter(str(c["method"]) for c in public[chain_id].calls)
            assert set(read) <= {"eth_call"}
        assert JUNK not in public[8453].balance_reads()
        # The read still says it is incomplete: JUNK's balance is unknown, not zero.
        base_reads = [r for r in service.chain_reads() if r["chainId"] == 8453]
        assert [r["status"] for r in base_reads] == ["partial", "partial"]
        assert {r["status"] for r in service.chain_reads() if r["chainId"] == 4663} == {"ok"}


class TestCatchUpIntoARefusedKey:
    async def test_one_refused_request_per_wallet_then_the_pause_then_the_resume(
        self,
        funded_service: TradingService,
        wire: Wire,
        base_chain: FakeChain,
        clock: list[float],
    ) -> None:
        service = funded_service
        keys = await _two_wallets(service, base_chain)
        await service.sync_all()
        settled = {k: int(service.ledger.sync_state(8453, k)["last_block"]) for k in keys}
        start = {
            k: max(
                int(service.ledger.sync_state(8453, k)["oldest_block"]),
                settled[k] + 1 - SYNC_OVERLAP_BLOCKS,
            )
            for k in keys
        }
        # The gateway is off for 350k blocks; each wallet gets a deposit per window.
        head = base_chain.block = base_chain.block + 350_000
        amounts: dict[str, list[int]] = {k: [] for k in keys}
        for n, k in enumerate(keys):
            for i in range(4):
                amount = 1_000 * (n + 1) + i
                block = min(head, start[k] + i * WINDOW + 1_000)
                base_chain.add_transfer(
                    token=USDC, sender=OTHER, recipient=k, amount=amount, block=block
                )
                amounts[k].append(amount)
            base_chain.set_erc20(USDC, k, base_chain.get_erc20(USDC, k) + sum(amounts[k]))

        # The first wallet's third window is where the key stops being accepted; from
        # then on the endpoint refuses everything.
        first, second = keys
        cut = start[first] + 2 * WINDOW
        refusing = {"on": False}

        def rule(host: str, item: dict[str, Any]) -> httpx.Response | None:
            if host != BASE_HOST:
                return None
            if item.get("method") == "eth_getLogs" and _from_block(item) >= cut:
                refusing["on"] = True
            return _refusal(403) if refusing["on"] else None

        wire.rule = rule
        wire.clear()
        before = await _usage(service)
        with capture_logs() as logs:
            await service.sync_all()
        after = await _usage(service)

        # One refused request per wallet: the window the key died in (not halved),
        # then the other wallet's eth_blockNumber. The engine counted the same two.
        assert wire.refused == [(BASE_HOST, "eth_getLogs"), (BASE_HOST, "eth_blockNumber")]
        assert after[8453]["rpc"]["failed"] - before[8453]["rpc"]["failed"] == 2
        assert wire.ranges(BASE_HOST, first)[-1] == (cut, cut + WINDOW - 1)
        # The windows already booked are kept, and the cursor says so.
        assert service.ledger.sync_state(8453, first)["last_block"] == cut - 1
        assert _deposits(service, first) == sorted(amounts[first][:2])
        assert service.ledger.sync_state(8453, second)["last_block"] == settled[second]
        # Two refusals in a row: the chain is paused, once, and the reads say why.
        assert [e["event"] for e in logs if e["event"].startswith("trading.rpc_key")] == [
            "trading.rpc_key_refused"
        ]
        reads = [r for r in service.chain_reads() if r["chainId"] == 8453]
        assert all(r["status"] == "failed" and "HTTP 403" in r["reason"] for r in reads)
        assert {r["status"] for r in service.chain_reads() if r["chainId"] == 4663} == {"ok"}

        # While paused the background sync asks the chain nothing.
        clock[0] += AUTH_PAUSE_S[0] / 2
        wire.clear()
        await service.sync_all()
        assert wire.requests(BASE_HOST) == 0
        assert all(
            r["reason"].startswith("paused, key refused")
            for r in service.chain_reads()
            if r["chainId"] == 8453
        )

        # The key works again and the pause is over: both catch-ups complete.
        wire.rule = None
        clock[0] += AUTH_PAUSE_S[0]
        wire.clear()
        with capture_logs() as logs:
            await service.sync_all()
        assert [e["event"] for e in logs if e["event"].startswith("trading.rpc_key")] == [
            "trading.rpc_key_accepted"
        ]
        resumed = wire.ranges(BASE_HOST, first)
        assert resumed[0][0] == cut - SYNC_OVERLAP_BLOCKS  # from its cursor, not from the gap
        assert len(resumed) == -(-(head - resumed[0][0] + 1) // WINDOW) == 2
        assert wire.ranges(BASE_HOST, second)[0][0] == start[second]
        for k in keys:
            assert service.ledger.sync_state(8453, k)["last_block"] == head
            assert _deposits(service, k) == sorted(amounts[k])  # each booked once
        assert {r["status"] for r in service.chain_reads()} == {"ok"}
        assert 8453 not in service._auth_pauses


class TestRpcUrlChangedMidCatchUp:
    async def test_the_next_pass_resumes_on_the_new_url_and_the_count_carries_over(
        self, funded_service: TradingService, wire: Wire, base_chain: FakeChain
    ) -> None:
        service = funded_service
        key = service.test_wallet.lower()  # type: ignore[attr-defined]
        await service.sync_all()
        old = service.evm(BASE)
        assert old.url == BASE.rpc_url and old.read_url is None
        state = service.ledger.sync_state(8453, key)
        start = max(int(state["oldest_block"]), int(state["last_block"]) + 1 - SYNC_OVERLAP_BLOCKS)
        head = base_chain.block = base_chain.block + 250_000
        for i, amount in enumerate((11, 22, 33)):
            base_chain.add_transfer(
                token=USDC,
                sender=OTHER,
                recipient=key,
                amount=amount,
                block=start + i * WINDOW + 500,
            )

        # The default endpoint fails every window past the first: a server error,
        # which halves (it may be about the range) and never pauses.
        def rule(host: str, item: dict[str, Any]) -> httpx.Response | None:
            if host == BASE_HOST and item.get("method") == "eth_getLogs":
                if _from_block(item) >= start + WINDOW:
                    return _refusal(502)
            return None

        wire.rule = rule
        await service.sync_all()
        assert service.ledger.sync_state(8453, key)["last_block"] == start + WINDOW - 1
        assert len(wire.refused) == len(HALVINGS)
        assert service._auth_pauses == {}
        before = (await _usage(service))[8453]
        assert before["rpc"]["failed"] == len(HALVINGS)

        # The operator points Base at another endpoint; no restart.
        service.config.rpc_urls = {"8453": PAID_BASE}
        wire.clear()
        await service.sync_all()
        fresh = service.evm(BASE)
        assert fresh is not old
        assert (fresh.url, fresh.read_url) == (PAID_BASE, BASE.rpc_url)
        # The catch-up resumed from its cursor on the new endpoint and finished there;
        # the old one only served the balance multicall, as the read lane.
        assert wire.ranges(PAID_BASE_HOST, key)[0][0] == start + WINDOW - SYNC_OVERLAP_BLOCKS
        assert wire.methods(BASE_HOST) == Counter({"eth_call": 1})
        assert service.ledger.sync_state(8453, key)["last_block"] == head
        assert _deposits(service, key) == [11, 22, 33]
        assert service.chain_reads(key, 8453)[0]["status"] == "ok"
        # One count for the chain since start, whichever URL served it.
        after = (await _usage(service))[8453]
        assert after["since"] == before["since"]
        assert after["rpc"]["requests"] == before["rpc"]["requests"] + wire.requests(PAID_BASE_HOST)
        assert _sent(before["rpc"], after["rpc"]) == wire.methods(PAID_BASE_HOST)
        assert after["rpc"]["failed"] == before["rpc"]["failed"]
        assert after["read"]["requests"] == before["read"]["requests"] + 1
