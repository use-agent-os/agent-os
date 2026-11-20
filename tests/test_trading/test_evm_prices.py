from __future__ import annotations

import json

import httpx
import pytest

from agentos.trading import evm as evm_mod
from agentos.trading.chains import BASE, NATIVE_ADDRESS, ROBINHOOD
from agentos.trading.evm import EvmClient, EvmRpcError, EvmTransportError
from agentos.trading.prices import PriceService
from agentos.trading.providers import ProviderError, validate_transaction
from agentos.trading.token_lists import RETRY_DELAYS_S, REVALIDATE_TTL_S
from tests.test_trading.fakes import (
    AAPL,
    OTHER,
    USDC,
    WALLET,
    WETH,
    FakeChain,
    FakePrices,
    transfer_log,
)


class TestAbiHelpers:
    def test_encode_and_decode(self) -> None:
        assert evm_mod.pad_address(WALLET) == "0" * 24 + WALLET[2:]
        assert evm_mod.decode_uint("0x") == 0
        assert evm_mod.decode_uint("0x10") == 16
        with pytest.raises(ValueError):
            evm_mod.pad_uint(-1)
        # dynamic string
        text = "USD Coin"
        raw = (
            (32).to_bytes(32, "big")
            + len(text).to_bytes(32, "big")
            + text.encode().ljust(32, b"\0")
        )
        assert evm_mod.decode_string("0x" + raw.hex()) == text
        # bytes32 style
        assert evm_mod.decode_string("0x" + b"MKR".ljust(32, b"\0").hex()) == "MKR"
        assert evm_mod.decode_string("0x1234") == ""

    def test_parse_transfer_log_rejects_erc721(self) -> None:
        log = transfer_log(
            tx_hash="0xaa",
            log_index=1,
            block=5,
            token=USDC,
            sender=WALLET,
            recipient=OTHER,
            amount=7,
        )
        parsed = evm_mod.parse_transfer_log(log)
        assert parsed is not None
        assert (parsed.sender, parsed.recipient, parsed.amount, parsed.block_number) == (
            WALLET,
            OTHER,
            7,
            5,
        )
        nft = dict(log, topics=[*log["topics"], "0x01"])
        assert evm_mod.parse_transfer_log(nft) is None
        assert evm_mod.parse_transfer_log({"topics": []}) is None

    def test_receipt_helpers(self) -> None:
        receipt = {"status": "0x1", "gasUsed": "0x10", "effectiveGasPrice": "0x2", "logs": []}
        assert evm_mod.receipt_succeeded(receipt)
        assert evm_mod.receipt_gas_wei(receipt) == 32
        # OP-stack receipts add the L1 data fee on top of gasUsed × price.
        assert evm_mod.receipt_gas_wei({**receipt, "l1Fee": "0x10"}) == 48
        assert not evm_mod.receipt_succeeded({"status": "0x0"})
        assert not evm_mod.receipt_succeeded(None)


@pytest.fixture
def chain() -> FakeChain:
    c = FakeChain(chain_id=8453, block=3_000)
    c.tokens[USDC] = ("USDC", "USD Coin", 6)
    c.set_native(WALLET, 5 * 10**18)
    c.set_erc20(USDC, WALLET, 42 * 10**6)
    return c


@pytest.fixture
async def client(chain: FakeChain):
    transport = httpx.MockTransport(chain.handle)
    async with httpx.AsyncClient(transport=transport) as http:
        yield EvmClient("https://mainnet.base.org", http=http, max_log_span=1000)


class TestEvmClient:
    async def test_reads_and_user_agent(self, client: EvmClient, chain: FakeChain) -> None:
        assert await client.chain_id() == 8453
        assert await client.block_number() == 3_000
        assert await client.get_balance(WALLET) == 5 * 10**18
        assert await client.erc20_balance_of(USDC, WALLET) == 42 * 10**6
        assert await client.erc20_metadata(USDC) == ("USDC", "USD Coin", 6)
        assert await client.erc20_metadata(OTHER) == ("", "", 18)
        assert await client.block_timestamp(10) == 1_700_000_000 + 20

    async def test_missing_user_agent_is_403(self, chain: FakeChain) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            request.headers["user-agent"] = "curl"
            return chain.handle(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with pytest.raises(EvmTransportError):
                await EvmClient("https://rpc", http=http).chain_id()

    async def test_batch_balances_and_sequential_fallback(
        self, client: EvmClient, chain: FakeChain
    ) -> None:
        balances = await client.erc20_balances(WALLET, [USDC, WETH])
        assert balances == {USDC: 42 * 10**6, WETH: 0}
        assert len(chain.calls) == 2  # one batch round-trip, two inner calls
        chain.batch_supported = False
        chain.calls.clear()
        assert await client.erc20_balances(WALLET, [USDC, WETH]) == {USDC: 42 * 10**6, WETH: 0}

    async def test_failed_balance_read_is_none_not_zero(
        self, client: EvmClient, chain: FakeChain
    ) -> None:
        """A per-item RPC error is 'unknown'; a zero here would erase a holding."""
        chain.fail_balance_of.add(USDC)
        assert await client.erc20_balances(WALLET, [USDC, WETH]) == {USDC: None, WETH: 0}
        chain.batch_supported = False
        assert await client.erc20_balances(WALLET, [USDC, WETH]) == {USDC: None, WETH: 0}

    async def test_rpc_error_surfaces(self, client: EvmClient) -> None:
        with pytest.raises(EvmRpcError) as info:
            await client.call("eth_nope")
        assert info.value.code == -32601

    async def test_transfer_logs_chunk_and_halve(self, client: EvmClient, chain: FakeChain) -> None:
        chain.max_log_span = 300
        chain.add_transfer(token=USDC, sender=OTHER, recipient=WALLET, amount=1, block=100)
        chain.add_transfer(token=USDC, sender=WALLET, recipient=OTHER, amount=2, block=2_500)
        chain.add_transfer(token=USDC, sender=OTHER, recipient=OTHER, amount=3, block=2_600)
        logs = await client.transfer_logs(WALLET, from_block=0, to_block=3_000)
        assert [(t.block_number, t.amount) for t in logs] == [(100, 1), (2_500, 2)]
        spans = [
            int(c["params"][0]["toBlock"], 16) - int(c["params"][0]["fromBlock"], 16) + 1
            for c in chain.calls
            if c["method"] == "eth_getLogs"
        ]
        # The first attempt at the full span is refused; the client halves until accepted.
        assert spans[0] == 1000 and spans[-1] <= 300 and all(s <= 300 for s in spans[2:])

    async def test_transfer_logs_halve_on_a_gateway_http_500_too(self, chain: FakeChain) -> None:
        """dRPC refuses an oversized range with HTTP 500, not a JSON-RPC error.

        ``approval_logs`` already halved on that; ``transfer_logs`` raised it
        as an outage. Both now halve down to the node's own span and only
        raise below it.
        """
        chain.add_transfer(token=USDC, sender=OTHER, recipient=WALLET, amount=1, block=100)
        chain.add_transfer(token=USDC, sender=WALLET, recipient=OTHER, amount=2, block=2_500)
        gateway = {"max_span": 500}

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            if payload.get("method") == "eth_getLogs":
                flt = payload["params"][0]
                span = int(flt["toBlock"], 16) - int(flt["fromBlock"], 16) + 1
                if span > gateway["max_span"]:
                    return httpx.Response(500, text="query exceeds max block range")
            return chain.handle(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = EvmClient("https://mainnet.base.org", http=http, max_log_span=250)
            logs = await client.transfer_logs(WALLET, from_block=0, to_block=3_000, max_span=4_000)
            assert [(t.block_number, t.amount) for t in logs] == [(100, 1), (2_500, 2)]
            spans = [
                int(c["params"][0]["toBlock"], 16) - int(c["params"][0]["fromBlock"], 16) + 1
                for c in chain.calls
                if c["method"] == "eth_getLogs"
            ]
            assert spans and all(s <= 500 for s in spans)  # only accepted chunks reach the node
            # A 500 at or below the node's own span is a real outage and is raised.
            gateway["max_span"] = 100
            with pytest.raises(EvmTransportError):
                await client.transfer_logs(WALLET, from_block=0, to_block=3_000, max_span=4_000)

    async def test_transfer_logs_dedupes_self_transfer(
        self, client: EvmClient, chain: FakeChain
    ) -> None:
        chain.add_transfer(token=USDC, sender=WALLET, recipient=WALLET, amount=9, block=10)
        logs = await client.transfer_logs(WALLET, from_block=0, to_block=20)
        assert len(logs) == 1

    async def test_fee_data_with_and_without_history(
        self, client: EvmClient, chain: FakeChain
    ) -> None:
        max_fee, tip = await client.fee_data()
        assert max_fee == 2 * 10**8 + 10**6 and tip == 10**6
        chain.fee_history = False
        max_fee, tip = await client.fee_data()
        assert max_fee == 2 * 10**8 + 10**7 and tip == 10**7

    async def test_send_and_receipt(self, client: EvmClient, chain: FakeChain) -> None:
        tx_hash = await client.send_raw_transaction("0xdeadbeef")
        assert chain.sent == ["0xdeadbeef"]
        assert await client.get_transaction_receipt(tx_hash) is None
        assert await client.wait_for_receipt(tx_hash, timeout_s=0.01, interval_s=0.001) is None
        chain.receipt(tx_hash, status=1)
        receipt = await client.wait_for_receipt(tx_hash, timeout_s=1, interval_s=0.001)
        assert receipt is not None and evm_mod.receipt_succeeded(receipt)

    async def test_sealed_receipt_replaces_a_provisional_l1_fee(
        self, client: EvmClient, chain: FakeChain
    ) -> None:
        # Base's flashblock preconfirmation: the first receipt carries l1Fee A,
        # the one in the sealed block l1Fee B.
        tx_hash = "0x" + "ab" * 32
        first = chain.receipt(tx_hash, gas_used=100, gas_price=2)
        first["l1Fee"] = hex(2_957_077_405)
        chain.sealed[tx_hash] = {**first, "l1Fee": hex(5_063_072_155)}
        chain.head_step = 1
        receipt = await client.wait_for_sealed_receipt(tx_hash, timeout_s=1, interval_s=0.001)
        assert receipt is not None
        assert evm_mod.receipt_gas_wei(receipt) == 200 + 5_063_072_155
        # The re-read happened only once the head was past the receipt's block.
        assert chain.block > int(first["blockNumber"], 16)

    async def test_sealed_receipt_keeps_the_first_when_the_head_stalls(
        self, client: EvmClient, chain: FakeChain
    ) -> None:
        from structlog.testing import capture_logs

        tx_hash = "0x" + "cd" * 32
        first = chain.receipt(tx_hash, gas_used=100, gas_price=2)
        first["l1Fee"] = hex(10_543_899_578)
        chain.sealed[tx_hash] = {**first, "l1Fee": hex(5_637_609_152)}
        with capture_logs() as logs:
            receipt = await evm_mod.sealed_receipt(
                client, tx_hash, first, timeout_s=0.02, interval_s=0.001
            )
        assert receipt is first
        assert evm_mod.receipt_gas_wei(receipt) == 200 + 10_543_899_578
        assert [e["event"] for e in logs] == ["trading.receipt_provisional"]
        assert logs[0]["reason"] == "head did not advance"

    async def test_sealed_receipt_skips_chains_without_an_l1_fee(
        self, client: EvmClient, chain: FakeChain
    ) -> None:
        tx_hash = "0x" + "ef" * 32
        first = chain.receipt(tx_hash)
        before = len(chain.calls)
        assert await evm_mod.sealed_receipt(client, tx_hash, first) is first
        assert len(chain.calls) == before  # not even a head read

    async def test_sealed_receipt_survives_a_failing_re_read(
        self, client: EvmClient, chain: FakeChain
    ) -> None:
        from structlog.testing import capture_logs

        tx_hash = "0x" + "12" * 32
        first = chain.receipt(tx_hash)
        first["l1Fee"] = "0x10"
        chain.head_step = 1
        chain.fail_methods.add("eth_getTransactionReceipt")
        with capture_logs() as logs:
            receipt = await evm_mod.sealed_receipt(
                client, tx_hash, first, timeout_s=0.02, interval_s=0.001
            )
        assert receipt is first
        assert logs and "re-read failed" in logs[-1]["reason"]


class TestSwapTransactionValidation:
    def test_a_transaction_that_is_not_safe_to_sign_is_refused(self) -> None:
        good = {"to": "0x" + "11" * 20, "from": "0x" + "22" * 20, "data": "0xabcd", "value": "0"}
        validate_transaction(good)
        for bad in (
            {**good, "data": "0x"},
            {**good, "data": "zz"},
            {**good, "to": "0x1"},
            {**good, "to": "0xnope"},
            {**good, "from": None},
            {k: v for k, v in good.items() if k != "value"},
            {**good, "maxFeePerGas": "1", "gasPrice": "1"},
        ):
            with pytest.raises(ProviderError) as refused:
                validate_transaction(bad)
            assert refused.value.code == "trading.tx_failed"


class TestPriceService:
    @pytest.fixture
    def fake(self) -> FakePrices:
        prices = FakePrices()
        prices.spot[("base", USDC)] = 1.0
        prices.spot[("base", WETH)] = 2000.0
        prices.spot[("robinhood", AAPL)] = 150.0
        prices.lists["base"] = [
            {"chainId": 8453, "address": USDC, "symbol": "USDC", "name": "USD Coin", "decimals": 6},
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
                "address": OTHER,
                "symbol": "AAPL",
                "name": "Apple Fan Coin",
                "decimals": 18,
            },
        ]
        prices.history[USDC] = 0.99
        prices.candles = [[1, 1.0, 2.0, 0.5, 1.5, 10.0], [2, 1.5, 2.5, 1.0, 2.0, 12.0]]
        return prices

    @pytest.fixture
    async def svc(self, fake: FakePrices):
        clock = {"now": 1_000_000.0}
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as http:
            service = PriceService(http=http, ttl_s=20, now=lambda: clock["now"])
            service.clock = clock  # type: ignore[attr-defined]
            yield service

    async def test_spot_prices_cache_and_native(self, svc: PriceService, fake: FakePrices) -> None:
        prices = await svc.prices(BASE, [USDC, NATIVE_ADDRESS])
        assert prices[USDC].price_usd == 1.0
        assert prices[NATIVE_ADDRESS].price_usd == 2000.0  # native priced through WETH
        assert prices[USDC].change_24h_pct == 2.5
        calls = len(fake.requests)
        await svc.prices(BASE, [USDC, NATIVE_ADDRESS])
        assert len(fake.requests) == calls  # served from cache
        svc.clock["now"] += 100  # type: ignore[attr-defined]
        await svc.prices(BASE, [USDC])
        assert len(fake.requests) == calls + 1

    async def test_missing_price_is_none(self, svc: PriceService) -> None:
        assert await svc.price(BASE, OTHER) is None

    async def test_an_unanswered_chunk_is_held_briefly_then_asked_again(
        self, fake: FakePrices, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 429 was never cached, so every ``prices()`` inside the TTL re-sent the
        same chunk: the portfolio poll plus desk turns made it a retry loop."""
        from agentos.trading import prices as prices_mod

        monkeypatch.setattr(prices_mod, "UNAVAILABLE_JITTER_S", 0.0)
        throttled = {"on": True}

        def limited(request: httpx.Request) -> httpx.Response:
            if throttled["on"] and request.url.host == "api.dexscreener.com":
                fake.requests.append(request)
                return httpx.Response(429)
            return fake.handle(request)

        clock = {"now": 1_000_000.0}
        async with httpx.AsyncClient(transport=httpx.MockTransport(limited)) as http:
            svc = PriceService(http=http, ttl_s=20, now=lambda: clock["now"])
            first = (await svc.prices(BASE, [USDC]))[USDC]
            assert first.unavailable and first.price_usd is None
            assert first.retry_in_s == prices_mod.UNAVAILABLE_HOLD_S
            asked = len(fake.requests)
            # Inside the hold: the same miss, and nothing is sent.
            clock["now"] += 1.0
            held = (await svc.prices(BASE, [USDC]))[USDC]
            assert held.unavailable and held.retry_in_s == pytest.approx(2.0)
            assert len(fake.requests) == asked
            # After it: asked again, and the answer is cached for the full TTL.
            throttled["on"] = False
            clock["now"] += 2.5
            again = (await svc.prices(BASE, [USDC]))[USDC]
            assert again.price_usd == 1.0 and not again.unavailable
            assert len(fake.requests) == asked + 1
            clock["now"] += 10
            assert (await svc.prices(BASE, [USDC]))[USDC].price_usd == 1.0
            assert len(fake.requests) == asked + 1

    async def test_the_hold_is_jittered_once_per_chunk(self) -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(503))
        ) as http:
            got = await PriceService(http=http).prices(BASE, [USDC, WETH])
        holds = {info.retry_in_s for info in got.values()}
        # One hold for the chunk (its tokens come back together), within the jitter.
        assert len(holds) == 1 and all(info.unavailable for info in got.values())
        (hold,) = holds
        assert 2.0 <= hold <= 4.0

    async def test_native_of_an_eth_chain_borrows_base_eth(
        self, svc: PriceService, fake: FakePrices
    ) -> None:
        """Robinhood has no WETH pool to price its gas coin; ETH is ETH, so Base's stands in."""
        assert ROBINHOOD.weth is None and ROBINHOOD.native_symbol == "ETH"
        prices = await svc.prices(ROBINHOOD, [NATIVE_ADDRESS, AAPL])
        assert prices[NATIVE_ADDRESS].price_usd == 2000.0
        assert prices[NATIVE_ADDRESS].change_24h_pct == 2.5
        assert prices[AAPL].price_usd == 150.0  # the chain's own feed is untouched
        assert await svc.price(ROBINHOOD, NATIVE_ADDRESS) == 2000.0
        # Borrowed per call, never written into Robinhood's cache: a second
        # call within the TTL reads Base's cached ETH and asks nothing new.
        calls = len(fake.requests)
        assert (await svc.prices(ROBINHOOD, [NATIVE_ADDRESS]))[NATIVE_ADDRESS].price_usd == 2000.0
        assert len(fake.requests) == calls
        assert (4663, NATIVE_ADDRESS) in svc._prices and svc._prices[
            (4663, NATIVE_ADDRESS)
        ].price_usd is None
        # Base's own ETH price never goes through the fallback.
        assert (await svc.prices(BASE, [NATIVE_ADDRESS]))[NATIVE_ADDRESS].pair_address

    async def test_token_list_and_stock_flag(self, svc: PriceService) -> None:
        tokens = await svc.token_list(ROBINHOOD)
        assert tokens[AAPL].stock_token is True and tokens[AAPL].verified is True
        assert tokens[OTHER].stock_token is False
        hits = await svc.find_by_symbol(ROBINHOOD, "aapl")
        assert [h.address for h in hits] == [AAPL, OTHER]
        assert (await svc.find_by_symbol(BASE, "eth"))[0].native is True
        assert await svc.known_token(BASE, USDC) is not None
        assert await svc.known_token(BASE, OTHER) is None

    async def test_failed_token_list_download_is_retried_soon(
        self, svc: PriceService, fake: FakePrices
    ) -> None:
        # A gateway that booted while the list was unreachable answered "Unknown
        # token symbol 'BONER' on Robinhood Chain" for a whole day: the empty
        # list had been cached. A failed download is held only briefly.
        saved = fake.lists.pop("robinhood")
        assert await svc.token_list(ROBINHOOD) == {}
        calls = len(fake.requests)
        assert await svc.token_list(ROBINHOOD) == {}
        assert len(fake.requests) == calls  # held: no hammering inside the hold
        fake.lists["robinhood"] = saved
        svc.clock["now"] += RETRY_DELAYS_S[0] + 1  # type: ignore[attr-defined]
        tokens = await svc.token_list(ROBINHOOD)
        assert AAPL in tokens
        assert len(fake.requests) == calls + 1

    async def test_token_list_merges_the_chain_and_stock_lists(
        self, svc: PriceService, fake: FakePrices
    ) -> None:
        stock = "0xb200000000000000000000000000000000000001"
        fake.stock_lists["base"] = [
            {
                "chainId": 8453,
                "address": stock,
                "symbol": "AAPLc",
                "name": "Apple Inc.",
                "decimals": 8,
                "logoURI": "https://img/aaplc",
                "tags": ["stock"],
            },
            # The chain's own list named USDC first: the stock list cannot
            # rewrite its decimals, only lend it what it lacks.
            {
                "chainId": 8453,
                "address": USDC,
                "symbol": "FAKE",
                "name": "Not USDC",
                "decimals": 18,
                "logoURI": "https://img/usdc",
            },
        ]
        tokens = await svc.token_list(BASE)
        asked = [r.url.path.rsplit("/", 1)[-1] for r in fake.requests]
        assert asked == ["base_tokens.json", "base_stock_tokens.json"]
        assert all(r.url.host == "raw.githubusercontent.com" for r in fake.requests)
        # A Base stock carries no name suffix: the list's tag is what marks it.
        assert tokens[stock].stock_token is True and tokens[stock].verified is True
        assert tokens[stock].decimals == 8 and tokens[stock].logo_url == "https://img/aaplc"
        assert (tokens[USDC].symbol, tokens[USDC].decimals) == ("USDC", 6)
        assert tokens[USDC].logo_url == "https://img/usdc"
        assert tokens[USDC].stock_token is False

    async def test_token_list_picks_up_a_newer_version_without_a_restart(
        self, svc: PriceService, fake: FakePrices
    ) -> None:
        fake.list_versions["base_tokens"] = (1, 0, 0)
        first = await svc.token_list(BASE)
        assert OTHER not in first
        calls = len(fake.requests)
        # Inside the re-check window nothing is asked, and the answer is the same object.
        svc.clock["now"] += REVALIDATE_TTL_S - 1  # type: ignore[attr-defined]
        assert await svc.token_list(BASE) is first
        assert len(fake.requests) == calls

        # Past it, an unchanged list costs a conditional request answered 304.
        svc.clock["now"] += 2  # type: ignore[attr-defined]
        assert await svc.token_list(BASE) is first  # answered at once, checked behind
        await svc.token_lists.settle()
        rechecks = fake.requests[calls:]
        assert len(rechecks) == 2 and all(r.headers.get("if-none-match") for r in rechecks)
        assert await svc.token_list(BASE) is first

        # A new version of the list is published: the next re-check adopts it.
        fake.lists["base"].append(
            {"chainId": 8453, "address": OTHER, "symbol": "NEW", "name": "New", "decimals": 9}
        )
        fake.list_versions["base_tokens"] = (1, 0, 1)
        svc.clock["now"] += REVALIDATE_TTL_S + 1  # type: ignore[attr-defined]
        await svc.token_list(BASE)
        await svc.token_lists.settle()
        latest = await svc.token_list(BASE)
        assert latest[OTHER].symbol == "NEW" and latest[OTHER].decimals == 9
        assert (await svc.known_token(BASE, OTHER)) is latest[OTHER]

    async def test_a_list_that_stops_answering_keeps_serving_its_held_copy(
        self, svc: PriceService, fake: FakePrices
    ) -> None:
        first = await svc.token_list(ROBINHOOD)
        assert AAPL in first
        del fake.lists["robinhood"]
        svc.clock["now"] += REVALIDATE_TTL_S + 1  # type: ignore[attr-defined]
        assert await svc.token_list(ROBINHOOD) is first
        await svc.token_lists.settle()
        assert await svc.token_list(ROBINHOOD) is first

    async def test_search(self, svc: PriceService) -> None:
        rows = await svc.search(ROBINHOOD, "AAPL")
        assert rows[0]["address"] == AAPL and rows[0]["stockToken"] is True
        assert rows[0]["priceUsd"] == 150.0
        by_address = await svc.search(BASE, USDC)
        assert by_address[0]["symbol"] == "USDC" and by_address[0]["priceUsd"] == 1.0
        assert (await svc.search(BASE, "ETH"))[0]["native"] is True
        assert await svc.search(BASE, "   ") == []

    async def test_history_and_fallback(self, svc: PriceService, fake: FakePrices) -> None:
        assert await svc.price_at(BASE, USDC, 900_000) == 0.99
        assert await svc.price_at(BASE, WETH, 900_000) is None
        calls = len(fake.requests)
        await svc.price_at(BASE, USDC, 900_100)  # same hour bucket → cached
        assert len(fake.requests) == calls

    async def test_ohlcv_only_on_base(self, svc: PriceService) -> None:
        candles = await svc.ohlcv(BASE, WETH, timeframe="hour", limit=2)
        assert candles == [
            {"t": 1.0, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5},
            {"t": 2.0, "o": 1.5, "h": 2.5, "l": 1.0, "c": 2.0},
        ]
        assert await svc.ohlcv(ROBINHOOD, AAPL, timeframe="hour", limit=2) is None
