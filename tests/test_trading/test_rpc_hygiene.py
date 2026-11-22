"""JSON-RPC calls that buy nothing (issue #3976), and seeing what the engine sends.

On a metered endpoint every JSON-RPC call is billed, each item of a batch array
on its own. Around the sync loop's client:

* a token whose ``balanceOf`` never answers is not asked again on every pass;
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from agentos.trading.chains import BASE, MULTICALL3, NATIVE_ADDRESS
from agentos.trading.evm import EvmClient
from agentos.trading.ledger import Ledger
from agentos.trading.prices import PriceService, TokenMeta, native_token
from agentos.trading.sync import WalletSyncer
from agentos.trading.vault import WalletRecord
from tests.test_trading.fakes import OTHER, USDC, WALLET, WETH, FakeChain, FakePrices

# A token whose ``balanceOf`` reverts, as an airdropped honeypot's does.
JUNK = "0x9999000000000000000000000000000000000009"
PAID = "https://paid.rpc.test/dummy-key-0123456789"
PUBLIC = "https://public.rpc.test/"


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
