"""The wallet sync's catch-up after downtime (#3976).

A gateway that was off for days owes millions of blocks. The incremental pass
used to walk that gap in 2,000-block windows and write its cursor once, at the
end, so a single transport error threw away everything read so far and the
next pass started over. The gap is now read in the chain's measured sweep span
(``ChainSpec.transfer_log_span``), each window is booked before the next is
read, and the cursor is saved after each; a steady pass still sends exactly
what it always did.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

import httpx
import pytest

from agentos.trading.chains import BASE, MULTICALL3, NATIVE_ADDRESS, ROBINHOOD, ChainSpec
from agentos.trading.evm import TRANSFER_TOPIC, EvmClient, EvmTransportError, pad_address
from agentos.trading.ledger import Ledger
from agentos.trading.prices import PriceService, TokenMeta, native_token
from agentos.trading.service import TradingService
from agentos.trading.sync import SYNC_OVERLAP_BLOCKS, WalletSyncer, sweep_span
from agentos.trading.vault import WalletRecord
from tests.test_trading.fakes import AAPL, OTHER, USDC, WALLET, WETH, FakeChain, FakePrices

RPC_HOST = "mainnet.base.org"
NODE_SPAN = 2_000  # EvmClient's default max_log_span, as the service builds it

# The Base scenario: a first pass at block 10_000, then the gateway is off
# until block 360_000. The catch-up starts SYNC_OVERLAP_BLOCKS below the cursor.
SETTLED_AT = 10_000
HEAD = 360_000
CATCH_UP_FROM = SETTLED_AT + 1 - SYNC_OVERLAP_BLOCKS
SPAN = BASE.transfer_log_span
W1 = (CATCH_UP_FROM, CATCH_UP_FROM + SPAN - 1)
W2 = (W1[1] + 1, W1[1] + SPAN)
W3 = (W2[1] + 1, W2[1] + SPAN)
W4 = (W3[1] + 1, HEAD)

Gate = Callable[[dict[str, Any]], httpx.Response | None]


def _range(req: dict[str, Any]) -> tuple[int, int]:
    flt = req["params"][0]
    return int(flt["fromBlock"], 16), int(flt["toBlock"], 16)


def refuse_wider_than(blocks: int) -> Gate:
    """A paid load-balanced gateway: a wider eth_getLogs is an HTTP 500 and a sentence."""

    def gate(req: dict[str, Any]) -> httpx.Response | None:
        if req.get("method") != "eth_getLogs":
            return None
        lo, hi = _range(req)
        if hi - lo + 1 <= blocks:
            return None
        message = f"limited to {blocks} blocks and this request needs {hi - lo + 1}"
        return httpx.Response(500, json={"error": {"code": -32000, "message": message}})

    return gate


def outage_from(lo: int, hi: int) -> Gate:
    """The node fails every eth_getLogs that starts inside ``lo..hi``, at any span."""

    def gate(req: dict[str, Any]) -> httpx.Response | None:
        if req.get("method") == "eth_getLogs" and lo <= _range(req)[0] <= hi:
            return httpx.Response(502, text="bad gateway")
        return None

    return gate


@dataclass
class World:
    """One wallet syncer over one fake node, with a gate in front of the node."""

    chain: FakeChain
    ledger: Ledger
    syncer: WalletSyncer
    evm: EvmClient
    spec: ChainSpec
    clock: dict[str, float]
    gate: Gate | None = None
    #: Every eth_getLogs range asked for, the refused ones included.
    attempts: list[tuple[int, int]] = field(default_factory=list)
    #: Tokens whose metadata lookup fails the way a node outage does.
    meta_down: set[str] = field(default_factory=set)

    def answered_logs(self) -> list[tuple[int, int]]:
        """The eth_getLogs ranges the node itself answered."""
        return [_range(c) for c in self.chain.calls if c["method"] == "eth_getLogs"]

    def methods(self) -> Counter[str]:
        return Counter(str(c["method"]) for c in self.chain.calls)

    def reset_counts(self) -> None:
        self.chain.calls.clear()
        self.attempts.clear()


MakeWorld = Callable[..., Awaitable[World]]


@pytest.fixture
async def make_world() -> AsyncIterator[MakeWorld]:
    clients: list[httpx.AsyncClient] = []
    ledgers: list[Ledger] = []

    async def make(
        spec: ChainSpec = BASE, *, block: int = SETTLED_AT, multicall: bool = False
    ) -> World:
        chain = FakeChain(chain_id=spec.chain_id, block=block)
        chain.tokens[USDC] = ("USDC", "USD Coin", 6)
        chain.tokens[WETH] = ("WETH", "Wrapped Ether", 18)
        prices_fake = FakePrices()
        prices_fake.spot[("base", USDC)] = 1.0
        prices_fake.spot[("base", WETH)] = 2000.0
        prices_fake.history[USDC] = 1.0
        prices_fake.history[WETH] = 1500.0
        prices_fake.lists["base"] = [
            {"chainId": 8453, "address": t, "symbol": s, "name": n, "decimals": d}
            for t, (s, n, d) in chain.tokens.items()
        ]
        holder: dict[str, World] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host != RPC_HOST:
                return prices_fake.handle(request)
            world = holder["world"]
            payload = json.loads(request.content or b"null")
            if isinstance(payload, dict):
                if payload.get("method") == "eth_getLogs":
                    world.attempts.append(_range(payload))
                if world.gate is not None:
                    answer = world.gate(payload)
                    if answer is not None:
                        return answer
            return chain.handle(request)

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(http)
        ledger = Ledger(":memory:")
        ledgers.append(ledger)
        clock = {"now": 1_700_000_000.0 + block * 2 + 100}
        prices = PriceService(http=http, ttl_s=0, now=lambda: clock["now"])
        evm = EvmClient(
            f"https://{RPC_HOST}", http=http, multicall3=MULTICALL3 if multicall else None
        )

        async def token_meta(chain_spec: ChainSpec, address: str) -> TokenMeta:
            if address in holder["world"].meta_down:
                raise EvmTransportError("HTTP 502")
            if address == NATIVE_ADDRESS:
                return native_token(chain_spec)
            known = await prices.known_token(chain_spec, address)
            if known:
                ledger.upsert_token(
                    chain_spec.chain_id,
                    address,
                    symbol=known.symbol,
                    name=known.name,
                    decimals=known.decimals,
                    verified=True,
                )
                return known
            return TokenMeta(chain_spec.chain_id, address, "", "", 18)

        async def watch(chain_spec: ChainSpec) -> list[str]:
            return [USDC, WETH] if chain_spec.chain_id == BASE.chain_id else []

        syncer = WalletSyncer(
            ledger,
            prices,
            evm_for=lambda chain_spec: evm,
            token_meta=token_meta,
            watch_tokens=watch,
            now=lambda: clock["now"],
        )
        world = World(chain=chain, ledger=ledger, syncer=syncer, evm=evm, spec=spec, clock=clock)
        holder["world"] = world
        return world

    yield make
    for http in clients:
        await http.aclose()
    for ledger in ledgers:
        ledger.close()


def _wallet(spec: ChainSpec = BASE, created_block: int | None = 9_000) -> WalletRecord:
    record = WalletRecord(address=WALLET, label="W", created_at=1.0, imported=created_block is None)
    if created_block is not None:
        record.created_block[str(spec.chain_id)] = created_block
    return record


async def settled_then_offline(world: World) -> None:
    """A wallet synced at block 10_000, then 350k blocks of activity while the gateway is off."""
    chain = world.chain
    chain.add_transfer(token=USDC, sender=OTHER, recipient=WALLET, amount=100 * 10**6, block=9_100)
    chain.set_erc20(USDC, WALLET, 100 * 10**6)
    chain.set_native(WALLET, 10**18)
    await world.syncer.sync(_wallet(), world.spec)
    assert world.ledger.sync_state(8453, WALLET)["last_block"] == SETTLED_AT

    chain.block = HEAD
    world.clock["now"] = 1_700_000_000.0 + HEAD * 2 + 100
    # Window 1: a deposit, then an external swap (30 USDC out, 0.01 WETH in).
    chain.add_transfer(token=USDC, sender=OTHER, recipient=WALLET, amount=50 * 10**6, block=20_000)
    swap = chain.add_transfer(
        token=USDC, sender=WALLET, recipient=OTHER, amount=30 * 10**6, block=100_000
    )
    chain.add_transfer(
        token=WETH,
        sender=OTHER,
        recipient=WALLET,
        amount=10**16,
        block=100_000,
        tx_hash=swap,
        log_index=1,
    )
    # Window 2: a withdrawal, and a deposit inside the overlap a resume re-reads.
    chain.add_transfer(token=USDC, sender=WALLET, recipient=OTHER, amount=20 * 10**6, block=150_000)
    chain.add_transfer(
        token=USDC, sender=OTHER, recipient=WALLET, amount=5 * 10**6, block=W2[1] - 5
    )
    # Window 3: WETH in and partly out again.
    chain.add_transfer(token=WETH, sender=OTHER, recipient=WALLET, amount=2 * 10**16, block=250_000)
    chain.add_transfer(token=WETH, sender=WALLET, recipient=OTHER, amount=5 * 10**15, block=300_000)
    # Window 4: one more deposit.
    chain.add_transfer(token=USDC, sender=OTHER, recipient=WALLET, amount=10 * 10**6, block=350_000)
    # 5 USDC more than the sweep explains (an opening), and 0.2 ETH gone.
    chain.set_erc20(USDC, WALLET, 120 * 10**6)
    chain.set_erc20(WETH, WALLET, 25 * 10**15)
    chain.set_native(WALLET, 8 * 10**17)


def ledger_view(ledger: Ledger) -> dict[str, Any]:
    """Everything a pass books, without row ids."""
    columns = (
        "ts",
        "kind",
        "tx_hash",
        "log_index",
        "token_in",
        "amount_in_raw",
        "token_out",
        "amount_out_raw",
        "value_usd",
        "cost_basis_source",
        "note",
    )
    entries = sorted(
        (tuple(e[c] for c in columns) for e in ledger.list_entries(wallet=WALLET, limit=1000)),
        key=repr,
    )
    state = ledger.sync_state(8453, WALLET) or {}
    return {
        "entries": entries,
        "balances": {str(r["token"]): str(r["raw"]) for r in ledger.balances(WALLET, 8453)},
        "positions": sorted(
            (p.token, p.amount_raw, round(p.cost_usd, 9)) for p in ledger.positions(WALLET)
        ),
        "realized": {
            k: round(v, 9) for k, v in sorted(ledger.realized_by_position(WALLET).items())
        },
        "cursor": (state.get("last_block"), state.get("oldest_block")),
    }


def _booked(ledger: Ledger) -> list[tuple[str, int]]:
    return [
        (str(e["tx_hash"]), int(e["log_index"]))
        for e in ledger.list_entries(wallet=WALLET, limit=1000)
        if e["tx_hash"]
    ]


class TestWideWindows:
    def test_sweep_spans_stay_within_what_was_measured(self) -> None:
        """Measured 2026-11-22: Base took up to 1M blocks, Robinhood's public node caps at 30k."""
        assert NODE_SPAN < BASE.transfer_log_span <= 1_000_000
        assert NODE_SPAN < ROBINHOOD.transfer_log_span <= 30_000
        # A chain nobody measured sweeps at the node span, as before.
        assert ChainSpec.__dataclass_fields__["transfer_log_span"].default == NODE_SPAN

    @pytest.mark.parametrize("spec", [BASE, ROBINHOOD], ids=["base", "robinhood"])
    async def test_a_long_gap_is_swept_in_wide_windows(
        self, make_world: MakeWorld, spec: ChainSpec
    ) -> None:
        world = await make_world(spec)
        await world.syncer.sync(_wallet(spec), spec)
        gap = 1_234_567
        world.chain.block = SETTLED_AT + gap
        world.reset_counts()

        await world.syncer.sync(_wallet(spec), spec)

        window = sweep_span(spec, world.evm)
        blocks = world.chain.block - CATCH_UP_FROM + 1
        windows = -(-blocks // window)
        assert world.methods()["eth_getLogs"] == 2 * windows
        # At least ten times fewer than 2,000-block windows.
        assert windows * 10 <= -(-blocks // NODE_SPAN)
        ranges = world.answered_logs()[::2]
        assert ranges[0] == (CATCH_UP_FROM, CATCH_UP_FROM + window - 1)
        assert all(hi - lo + 1 == window for lo, hi in ranges[:-1])
        assert ranges[-1][1] == world.chain.block
        assert all(b[0] == a[1] + 1 for a, b in pairwise(ranges))
        assert world.ledger.sync_state(spec.chain_id, WALLET)["last_block"] == world.chain.block

    async def test_a_short_gap_is_one_wide_window_too(self, make_world: MakeWorld) -> None:
        """Ten minutes off on Base (3,000 blocks) is 2 calls, not 2 per 2,000 blocks."""
        world = await make_world()
        await world.syncer.sync(_wallet(), BASE)
        world.chain.block = SETTLED_AT + 3_000
        world.reset_counts()
        await world.syncer.sync(_wallet(), BASE)
        assert world.answered_logs() == [(CATCH_UP_FROM, SETTLED_AT + 3_000)] * 2

    async def test_a_rebuild_sweeps_in_wide_windows(self, make_world: MakeWorld) -> None:
        world = await make_world(block=300_000)
        wallet = _wallet(created_block=None)
        world.syncer.initial_lookback = 100
        world.syncer.full_lookback = 250_000
        world.chain.add_transfer(token=USDC, sender=OTHER, recipient=WALLET, amount=7, block=60_000)
        world.chain.add_transfer(
            token=USDC, sender=OTHER, recipient=WALLET, amount=5, block=299_990
        )
        world.chain.set_erc20(USDC, WALLET, 12)
        await world.syncer.sync(wallet, BASE)
        world.reset_counts()

        await world.syncer.sync(wallet, BASE, full=True)

        # 250_001 blocks in 100k windows: 3 windows of 2 calls (it was 20 per 20k window).
        assert world.answered_logs()[::2] == [
            (200_001, 300_000),
            (100_001, 200_000),
            (50_000, 100_000),
        ]
        deposits = world.ledger.list_entries(wallet=WALLET, kind="deposit")
        assert sorted((e["amount_out_raw"], e["note"]) for e in deposits) == [
            ("5", None),
            ("7", None),
        ]


class TestRefusedWindows:
    """``transfer_logs`` halves a window the node refuses; the sweep still completes."""

    async def _catch_up(self, world: World, spec: ChainSpec) -> None:
        await world.syncer.sync(_wallet(spec), spec)
        for block in (50_000, 150_000, 249_999):
            world.chain.add_transfer(
                token=USDC, sender=OTHER, recipient=WALLET, amount=block, block=block
            )
        world.chain.block = 250_000
        world.reset_counts()
        await world.syncer.sync(_wallet(spec), spec)
        assert world.ledger.sync_state(spec.chain_id, WALLET)["last_block"] == 250_000
        amounts = sorted(
            int(e["amount_out_raw"])
            for e in world.ledger.list_entries(wallet=WALLET, kind="deposit")
            if e["tx_hash"]
        )
        assert amounts == [50_000, 150_000, 249_999]

    async def test_a_paid_gateway_refusal_http_500_halves(self, make_world: MakeWorld) -> None:
        """Robinhood's dRPC answered 50k blocks with an HTTP 500: a 100k window halves to 25k."""
        world = await make_world()
        world.gate = refuse_wider_than(40_000)
        await self._catch_up(world, BASE)
        assert any(hi - lo + 1 > 40_000 for lo, hi in world.attempts)  # asked wide, refused
        assert all(hi - lo + 1 <= 40_000 for lo, hi in world.answered_logs())

    async def test_a_public_node_rpc_error_halves(self, make_world: MakeWorld) -> None:
        """Robinhood's public node caps at 30k with a JSON-RPC error on HTTP 200."""
        world = await make_world()
        world.chain.max_log_span = 30_000
        await self._catch_up(world, BASE)
        assert any(hi - lo + 1 > 30_000 for lo, hi in world.attempts)  # asked wide, refused

    @pytest.mark.parametrize("public", [False, True], ids=["paid", "public"])
    async def test_robinhoods_own_span_is_refused_by_neither(
        self, make_world: MakeWorld, public: bool
    ) -> None:
        world = await make_world(ROBINHOOD)
        if public:
            world.chain.max_log_span = 30_000
        else:
            world.gate = refuse_wider_than(40_000)
        await self._catch_up(world, ROBINHOOD)
        blocks = 250_000 - CATCH_UP_FROM + 1
        assert len(world.attempts) == 2 * -(-blocks // ROBINHOOD.transfer_log_span)


class TestResumableCatchUp:
    async def test_a_failure_keeps_the_windows_before_it_and_the_next_pass_resumes(
        self, make_world: MakeWorld
    ) -> None:
        control = await make_world()
        await settled_then_offline(control)
        await control.syncer.sync(_wallet(), BASE)
        expected = ledger_view(control.ledger)
        assert expected["cursor"] == (HEAD, 9_000)

        world = await make_world()
        await settled_then_offline(world)
        before = ledger_view(world.ledger)
        world.gate = outage_from(*W3)
        with pytest.raises(EvmTransportError):
            await world.syncer.sync(_wallet(), BASE)

        # Windows 1 and 2 are booked and the cursor sits at the end of window 2.
        assert world.ledger.sync_state(8453, WALLET)["last_block"] == W2[1]
        booked = {
            (e["kind"], e["amount_in_raw"] or e["amount_out_raw"])
            for e in world.ledger.list_entries(wallet=WALLET, limit=1000)
            if e["tx_hash"]
        }
        assert booked == {
            ("deposit", str(100 * 10**6)),
            ("deposit", str(50 * 10**6)),
            ("swap", str(30 * 10**6)),
            ("withdraw", str(20 * 10**6)),
            ("deposit", str(5 * 10**6)),
        }
        # Nothing that compares the chain with the sweep ran: balances are
        # last-good, no opening, no native delta, no price snapshot.
        partial = ledger_view(world.ledger)
        assert partial["balances"] == before["balances"]
        assert world.ledger.opening_entry(8453, WALLET, USDC) is None
        natives = [
            e
            for e in world.ledger.list_entries(wallet=WALLET, limit=1000)
            if NATIVE_ADDRESS in (e["token_in"], e["token_out"])
        ]
        assert [e["note"] for e in natives] == ["opening balance"]
        assert world.ledger.snapshots(8453, USDC, since=world.clock["now"]) == []
        # The outage cost a bounded number of tries: the window halved to the node span.
        tried = [hi - lo + 1 for lo, hi in world.attempts if lo == W3[0]]
        assert tried == [100_000, 50_000, 25_000, 12_500, 6_250, 3_125, NODE_SPAN]

        world.gate = None
        world.reset_counts()
        assert await world.syncer.sync(_wallet(), BASE) is True
        assert world.answered_logs()[0][0] == W2[1] + 1 - SYNC_OVERLAP_BLOCKS
        # Windows 1 and 2 are not read again; the resume is two windows wide.
        assert world.methods()["eth_getLogs"] == 4
        assert ledger_view(world.ledger) == expected
        assert sorted(_booked(world.ledger)) == sorted(set(_booked(world.ledger)))

    async def test_a_window_that_failed_half_booked_books_the_rest_once(
        self, make_world: MakeWorld
    ) -> None:
        """A failure while booking (a metadata read) leaves part of a window in the ledger."""
        control = await make_world()
        await settled_then_offline(control)
        control.chain.add_transfer(
            token=AAPL, sender=OTHER, recipient=WALLET, amount=9, block=260_000
        )
        await control.syncer.sync(_wallet(), BASE)
        expected = ledger_view(control.ledger)

        world = await make_world()
        await settled_then_offline(world)
        world.chain.add_transfer(
            token=AAPL, sender=OTHER, recipient=WALLET, amount=9, block=260_000
        )
        world.meta_down.add(AAPL)
        with pytest.raises(EvmTransportError):
            await world.syncer.sync(_wallet(), BASE)
        # Window 3's WETH deposit (block 250_000) went in before the AAPL one failed.
        assert world.ledger.sync_state(8453, WALLET)["last_block"] == W2[1]
        deposits = world.ledger.list_entries(wallet=WALLET, kind="deposit")
        assert any(e["token_out"] == WETH for e in deposits)
        assert not any(e["token_out"] == AAPL for e in deposits)

        world.meta_down.clear()
        await world.syncer.sync(_wallet(), BASE)
        assert ledger_view(world.ledger) == expected
        booked = _booked(world.ledger)
        assert len(booked) == len(set(booked))

    async def test_a_restart_mid_catch_up_resumes_from_the_saved_cursor(
        self, make_world: MakeWorld
    ) -> None:
        """A new syncer over the same ledger (a gateway restart) picks up the saved cursor."""
        world = await make_world()
        await settled_then_offline(world)
        world.gate = outage_from(*W4)
        with pytest.raises(EvmTransportError):
            await world.syncer.sync(_wallet(), BASE)
        assert world.ledger.sync_state(8453, WALLET)["last_block"] == W3[1]
        world.gate = None
        world.reset_counts()
        fresh = WalletSyncer(
            world.ledger,
            world.syncer.prices,
            evm_for=lambda spec: world.evm,
            token_meta=world.syncer._token_meta,
            watch_tokens=world.syncer._watch_tokens,
            now=lambda: world.clock["now"],
        )
        await fresh.sync(_wallet(), BASE)
        assert world.answered_logs() == [(W3[1] + 1 - SYNC_OVERLAP_BLOCKS, HEAD)] * 2
        assert world.ledger.sync_state(8453, WALLET)["last_block"] == HEAD

    async def test_a_first_sweep_writes_no_cursor_until_it_reaches_the_head(
        self, make_world: MakeWorld
    ) -> None:
        """The cursor marks a wallet/chain swept: openings and ``initial`` reporting wait for it."""
        spec = ROBINHOOD
        world = await make_world(spec, block=100_000)
        wallet = _wallet(spec, created_block=None)  # imported: the first sweep is the lookback
        world.chain.add_transfer(token=USDC, sender=OTHER, recipient=WALLET, amount=3, block=60_000)
        world.chain.add_transfer(token=USDC, sender=OTHER, recipient=WALLET, amount=4, block=95_000)
        world.chain.set_erc20(USDC, WALLET, 10)
        # 50_000 blocks at 30k: two windows; the second fails.
        world.gate = outage_from(80_000, 100_000)
        with pytest.raises(EvmTransportError):
            await world.syncer.sync(wallet, spec)
        assert world.ledger.sync_state(spec.chain_id, WALLET) is None
        assert [e["amount_out_raw"] for e in world.ledger.list_entries(wallet=WALLET)] == ["3"]
        assert await world.syncer.ensure_opening(wallet, spec, USDC, world.evm) is False
        assert world.ledger.opening_entry(spec.chain_id, WALLET, USDC) is None

        world.gate = None
        await world.syncer.sync(wallet, spec)
        state = world.ledger.sync_state(spec.chain_id, WALLET)
        assert (state["last_block"], state["oldest_block"]) == (100_000, 50_000)
        deposits = world.ledger.list_entries(wallet=WALLET, kind="deposit")
        assert sorted((e["amount_out_raw"], e["note"] or "") for e in deposits) == [
            ("3", ""),
            ("3", "opening balance"),
            ("4", ""),
        ]
        booked = _booked(world.ledger)
        assert len(booked) == len(set(booked))


class TestSteadyState:
    """A pass with nothing to catch up sends exactly what it always has."""

    @pytest.fixture
    async def settled(self, make_world: MakeWorld) -> World:
        world = await make_world(multicall=True)
        world.chain.set_erc20(USDC, WALLET, 5)
        world.chain.set_native(WALLET, 10**18)
        await world.syncer.sync(_wallet(), BASE)
        world.chain.block += 150  # 30 s of Base blocks
        world.reset_counts()
        return world

    async def test_a_steady_pass_sends_the_same_calls(
        self, settled: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        writes: list[int] = []
        original = settled.ledger.set_sync_state

        def spy(*args: Any, **kwargs: Any) -> None:
            writes.append(int(kwargs["last_block"]))
            original(*args, **kwargs)

        monkeypatch.setattr(settled.ledger, "set_sync_state", spy)
        await settled.syncer.sync(_wallet(), BASE)

        assert settled.methods() == Counter(
            {"eth_blockNumber": 1, "eth_getLogs": 2, "eth_getBalance": 1, "eth_call": 1}
        )
        start, head = SETTLED_AT + 1 - SYNC_OVERLAP_BLOCKS, SETTLED_AT + 150
        wallet_topic = "0x" + pad_address(WALLET)
        filters = [c["params"][0] for c in settled.chain.calls if c["method"] == "eth_getLogs"]
        span = {"fromBlock": hex(start), "toBlock": hex(head)}
        assert filters == [
            {**span, "topics": [TRANSFER_TOPIC, None, wallet_topic]},
            {**span, "topics": [TRANSFER_TOPIC, wallet_topic]},
        ]
        assert writes == [head]

    async def test_a_steady_pass_with_a_transfer_books_it_as_before(self, settled: World) -> None:
        settled.chain.add_transfer(
            token=USDC, sender=OTHER, recipient=WALLET, amount=7, block=SETTLED_AT + 100
        )
        settled.chain.set_erc20(USDC, WALLET, 12)
        assert await settled.syncer.sync(_wallet(), BASE) is True
        assert settled.methods() == Counter(
            {
                "eth_blockNumber": 1,
                "eth_getLogs": 2,
                "eth_getBlockByNumber": 1,
                "eth_getBalance": 1,
                "eth_call": 1,
            }
        )
        assert {p.token: p.amount_raw for p in settled.ledger.positions(WALLET)}[USDC] == 12

    async def test_an_outage_fails_a_steady_pass_at_once_as_before(self, settled: World) -> None:
        """Within the node's span there is no halving to try: one request, then the error."""
        settled.gate = outage_from(0, 10**9)
        with pytest.raises(EvmTransportError):
            await settled.syncer.sync(_wallet(), BASE)
        assert len(settled.attempts) == 1
        assert settled.ledger.sync_state(8453, WALLET)["last_block"] == SETTLED_AT


class TestServiceReporting:
    """How ``sync_all`` reports a catch-up that stopped short, and the one that finishes it."""

    async def test_a_stopped_catch_up_is_a_failed_read_and_the_next_pass_finishes(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        service = funded_service
        address = service.test_wallet  # type: ignore[attr-defined]
        key = address.lower()
        await service.sync_all()
        state = service.ledger.sync_state(8453, key)
        settled = int(state["last_block"])
        start = max(int(state["oldest_block"]), settled + 1 - SYNC_OVERLAP_BLOCKS)
        window = BASE.transfer_log_span
        base_chain.block = settled + 300_000
        for k, amount in enumerate((11, 22, 33)):
            base_chain.add_transfer(
                token=USDC,
                sender=OTHER,
                recipient=address,
                amount=amount,
                block=start + k * window + 1_000,
            )
        down = {"lo": start + 2 * window}
        kinds: set[str | None] = set()
        handle = base_chain.handle

        def flaky(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content or b"null")
            if isinstance(payload, dict) and payload.get("method") == "eth_getLogs":
                kinds.add(service.sync_kind)
                if int(payload["params"][0]["fromBlock"], 16) >= down["lo"]:
                    return httpx.Response(502, text="bad gateway")
            return handle(request)

        monkeypatch.setattr(base_chain, "handle", flaky)
        service.events.clear()  # type: ignore[attr-defined]
        await service.sync_all()

        read = service.chain_reads(address, 8453)[0]
        assert read["status"] == "failed" and "502" in str(read["reason"])
        assert service.ledger.sync_state(8453, key)["last_block"] == start + 2 * window - 1
        amounts = sorted(
            int(e["amount_out_raw"])
            for e in service.ledger.list_entries(wallet=key, kind="deposit")
            if e["tx_hash"]
        )
        assert amounts == [11, 22]
        # Not a "changed" ledger to announce: the pass failed, and says so.
        assert not [e for e in service.events if e[0] == "trading.changed"]  # type: ignore[attr-defined]

        down["lo"] = 10**12
        service.events.clear()  # type: ignore[attr-defined]
        assert await service.sync_all() >= 1
        assert service.chain_reads(address, 8453)[0]["status"] == "ok"
        assert service.ledger.sync_state(8453, key)["last_block"] == base_chain.block
        amounts = sorted(
            int(e["amount_out_raw"])
            for e in service.ledger.list_entries(wallet=key, kind="deposit")
            if e["tx_hash"]
        )
        assert amounts == [11, 22, 33]
        changed = [p for e, p in service.events if e == "trading.changed"]  # type: ignore[attr-defined]
        assert {"schema_version": 1, "reason": "sync", "wallet": address} in changed
        # A catch-up of a wallet that has a cursor is the engine's routine pass, not a first sweep.
        assert kinds == {"background"}
