"""Bulk ``balanceOf`` reads: one Multicall3 ``eth_call``, on the public node when one is set.

The wallet sync reads many tokens for many wallets on every pass, and a metered
RPC bills each item of a JSON-RPC batch. A custom RPC is kept for what must be fresh
(trades, nonces, receipts); the bulk read goes to the chain's public node and falls
back to the custom one, so a public outage costs requests and never a stale ledger.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest

from agentos.gateway.config import TradingConfig
from agentos.trading.chains import BASE, MULTICALL3, ROBINHOOD
from agentos.trading.evm import (
    SEL_AGGREGATE3,
    SEL_BALANCE_OF,
    EvmClient,
    decode_aggregate3,
    encode_aggregate3,
    encode_call,
    pad_address,
)
from agentos.trading.service import TradingService
from tests.test_trading.fakes import AAPL, USDC, WALLET, WETH, FakeChain

CUSTOM = "https://custom.rpc/key"
PUBLIC = "https://public.rpc/"
BALANCE_OF = encode_call(SEL_BALANCE_OF, pad_address(WALLET))


def _node(chain_id: int = 8453) -> FakeChain:
    chain = FakeChain(chain_id=chain_id)
    chain.set_erc20(USDC, WALLET, 42)
    chain.set_erc20(AAPL, WALLET, 7)
    return chain


def _balance_calls(chain: FakeChain) -> int:
    """JSON-RPC items that read a balance: a direct ``balanceOf`` or a multicall."""
    return sum(
        1
        for c in chain.calls
        if c["method"] == "eth_call"
        and c["params"][0]["data"][:10] in {SEL_BALANCE_OF, SEL_AGGREGATE3}
    )


def _multicalls(chain: FakeChain) -> int:
    return sum(
        1
        for c in chain.calls
        if c["method"] == "eth_call" and c["params"][0]["to"].lower() == MULTICALL3.lower()
    )


class TestAbi:
    def test_encoding_is_what_eth_abi_reads(self) -> None:
        from eth_abi import decode

        data = encode_aggregate3([(USDC, BALANCE_OF), (WETH, BALANCE_OF)])
        assert data.startswith("0x82ad56cb")
        calls = decode(["(address,bool,bytes)[]"], bytes.fromhex(data[10:]))[0]
        assert [c[0].lower() for c in calls] == [USDC, WETH]
        assert all(c[1] is True for c in calls)
        assert all("0x" + c[2].hex() == BALANCE_OF for c in calls)

    def test_decoding_reads_what_eth_abi_writes(self) -> None:
        from eth_abi import encode

        results = [(True, (5).to_bytes(32, "big")), (False, b"\x01\x02"), (True, b"")]
        raw = "0x" + encode(["(bool,bytes)[]"], [results]).hex()
        assert decode_aggregate3(raw) == results

    @pytest.mark.parametrize(
        "answer",
        ["0x", "0xzz", "0x" + "00" * 31, "0x" + format(32, "064x") + format(5, "064x")],
    )
    def test_a_malformed_answer_is_refused(self, answer: str) -> None:
        with pytest.raises(ValueError):
            decode_aggregate3(answer)


class TestMulticallRead:
    @pytest.fixture
    async def setup(self):
        chain = _node()
        async with httpx.AsyncClient(transport=httpx.MockTransport(chain.handle)) as http:
            yield chain, EvmClient(CUSTOM, http=http, multicall3=MULTICALL3)

    async def test_many_tokens_are_one_eth_call(self, setup: Any) -> None:
        chain, client = setup
        assert await client.erc20_balances(WALLET, [USDC, WETH, AAPL]) == {
            USDC: 42,
            WETH: 0,
            AAPL: 7,
        }
        assert len(chain.calls) == 1
        assert _multicalls(chain) == 1

    async def test_a_big_read_is_split(self, setup: Any) -> None:
        chain, client = setup
        tokens = [f"0x{i:040x}" for i in range(1, 251)]
        balances = await client.erc20_balances(WALLET, tokens)
        assert set(balances) == set(tokens)
        assert _multicalls(chain) == 3

    async def test_a_failed_token_is_none_and_retried_alone(self, setup: Any) -> None:
        """A token that reverts is unknown, never zero, and costs its neighbours nothing."""
        chain, client = setup
        chain.fail_balance_of.add(USDC)
        assert await client.erc20_balances(WALLET, [USDC, WETH, AAPL]) == {
            USDC: None,
            WETH: 0,
            AAPL: 7,
        }
        assert _multicalls(chain) == 1
        direct = [c for c in chain.calls if c["params"][0]["to"].lower() != MULTICALL3.lower()]
        assert [c["params"][0]["to"] for c in direct] == [USDC]

    async def test_an_address_with_no_code_reads_as_the_batch_reads_it(self, setup: Any) -> None:
        """A call to no contract succeeds with no data: the batch path decides what that is."""
        chain, client = setup
        chain.answers_empty = {AAPL}
        assert (await client.erc20_balances(WALLET, [USDC, AAPL]))[AAPL] == 0
        direct = [c for c in chain.calls if c["params"][0]["to"].lower() != MULTICALL3.lower()]
        assert [c["params"][0]["to"] for c in direct] == [AAPL]

    async def test_no_multicall_contract_falls_back_to_the_batch(self, setup: Any) -> None:
        chain, client = setup
        chain.multicall_deployed = False
        assert await client.erc20_balances(WALLET, [USDC, AAPL]) == {USDC: 42, AAPL: 7}

    async def test_a_client_without_multicall_batches_as_before(self) -> None:
        chain = _node()
        async with httpx.AsyncClient(transport=httpx.MockTransport(chain.handle)) as http:
            client = EvmClient(CUSTOM, http=http)
            assert await client.erc20_balances(WALLET, [USDC, AAPL]) == {USDC: 42, AAPL: 7}
        assert _multicalls(chain) == 0
        assert len(chain.calls) == 2


class TestPublicLane:
    @pytest.fixture
    async def nodes(self):
        custom, public = _node(), _node()
        public.down = False  # type: ignore[attr-defined]

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "custom.rpc":
                return custom.handle(request)
            if public.down:  # type: ignore[attr-defined]
                return httpx.Response(429, text="slow down")
            return public.handle(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = EvmClient(CUSTOM, http=http, multicall3=MULTICALL3, read_url=PUBLIC)
            yield custom, public, client

    async def test_the_bulk_read_never_touches_the_custom_rpc(self, nodes: Any) -> None:
        custom, public, client = nodes
        assert await client.erc20_balances(WALLET, [USDC, AAPL]) == {USDC: 42, AAPL: 7}
        assert _balance_calls(custom) == 0
        assert _multicalls(public) == 1

    async def test_a_single_read_stays_on_the_custom_rpc(self, nodes: Any) -> None:
        """One-token reads sit next to trades, where the freshest answer matters."""
        custom, public, client = nodes
        assert await client.erc20_balance_of(USDC, WALLET) == 42
        assert _balance_calls(custom) == 1
        assert _balance_calls(public) == 0

    async def test_everything_else_stays_on_the_custom_rpc(self, nodes: Any) -> None:
        custom, public, client = nodes
        await client.block_number()
        await client.get_balance(WALLET)
        await client.transfer_logs(WALLET, from_block=1, to_block=2)
        assert public.calls == []
        assert {c["method"] for c in custom.calls} == {
            "eth_blockNumber",
            "eth_getBalance",
            "eth_getLogs",
        }

    async def test_a_public_outage_falls_back_to_the_custom_rpc(self, nodes: Any) -> None:
        custom, public, client = nodes
        public.down = True
        assert await client.erc20_balances(WALLET, [USDC, AAPL]) == {USDC: 42, AAPL: 7}
        assert _multicalls(custom) == 1

    async def test_a_public_outage_is_not_asked_again_at_once(self, nodes: Any) -> None:
        custom, public, client = nodes
        public.down = True
        await client.erc20_balances(WALLET, [USDC, AAPL])
        public.down = False
        await client.erc20_balances(WALLET, [USDC, AAPL])
        assert _multicalls(public) == 0
        assert _multicalls(custom) == 2

    async def test_the_public_node_is_tried_again_after_the_cooldown(
        self, nodes: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        custom, public, client = nodes
        public.down = True
        await client.erc20_balances(WALLET, [USDC, AAPL])
        public.down = False
        clock = time.monotonic() + 3600
        monkeypatch.setattr("agentos.trading.evm.time.monotonic", lambda: clock)
        await client.erc20_balances(WALLET, [USDC, AAPL])
        assert _multicalls(public) == 1


def test_the_sync_interval_defaults_to_a_minute() -> None:
    assert TradingConfig.model_fields["sync_interval_seconds"].default == 60


class TestServiceWiring:
    @pytest.mark.parametrize("spec", [BASE, ROBINHOOD], ids=["base", "robinhood"])
    def test_both_chains_name_the_verified_contract(self, spec: Any) -> None:
        assert spec.multicall3 == MULTICALL3

    def _client(
        self, service: TradingService, monkeypatch: pytest.MonkeyPatch, urls: dict[str, str]
    ) -> EvmClient:
        monkeypatch.delenv("RPC_BASE_URL", raising=False)
        monkeypatch.setattr(service, "_evm_factory", None)
        monkeypatch.setattr(service.config, "rpc_urls", urls, raising=False)
        service._evm.clear()
        return service.evm(BASE)

    def test_a_custom_rpc_reads_in_bulk_from_the_public_node(
        self, service: TradingService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._client(service, monkeypatch, {"8453": CUSTOM})
        assert client.url == CUSTOM
        assert client.read_url == BASE.rpc_url
        assert client.multicall3 == MULTICALL3

    def test_with_no_custom_rpc_there_is_one_node(
        self, service: TradingService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._client(service, monkeypatch, {})
        assert client.url == BASE.rpc_url
        assert client.read_url is None
