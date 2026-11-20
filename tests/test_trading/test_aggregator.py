"""The AgentOS DEX provider: its own contract, and one swap end to end.

The shared swap machinery is covered in ``test_service.py``, which now runs
on this provider because it is the default. What lives here is what is
specific to the aggregator: the shape of its single GET, the refusals it is
allowed to make, and the two things this side checks before signing —
that an approval names the swap target and nothing else, and that lapsed
calldata is re-fetched rather than broadcast.
"""

from __future__ import annotations

import json

import pytest

import agentos.trading.service as service_module
from agentos.trading.aggregator import (
    AGENTOS_ROUTER,
    ALLOWANCE_HOLDER,
    MAX_SLIPPAGE_BPS,
    NATIVE_SENTINEL,
    TRUSTED_SPENDERS,
    AggregatorClient,
    AggregatorProvider,
    slippage_to_bps,
)
from agentos.trading.chains import BASE, NATIVE_ADDRESS, ROBINHOOD
from agentos.trading.providers import (
    DEFAULT_PROVIDER_ID,
    PROVIDER_IDS,
    ProviderError,
    provider_label,
)
from agentos.trading.service import TradingService
from tests.test_trading.fakes import (
    ROUTER,
    USDC,
    WALLET,
    WETH,
    FakeAggregator,
    FakeChain,
    approve_calldata,
    decode_fake_raw,
)
from tests.test_trading.test_service import _wire_swap_effects


def _provider(http) -> AggregatorProvider:
    return AggregatorProvider(AggregatorClient(http=http))


#: A contract the operator trusts through ``trading.aggregator_trusted_spenders``.
CONFIGURED = "0x9999999999999999999999999999999999999999"


def _quote_body(*, spender: str, target: str, approve_amount: int, amount_needed: int) -> dict:
    """A minimal ``/v1/quote`` body with an approval, for the parser alone."""
    return {
        "chainId": 8453,
        "sellToken": {"address": USDC, "symbol": "USDC", "decimals": 6},
        "buyToken": {"address": WETH, "symbol": "WETH", "decimals": 18},
        "sellAmount": {"raw": "1000000"},
        "buyAmount": {"raw": "5000000000000000"},
        "minBuyAmount": {"raw": "4975000000000000"},
        "liquidityAvailable": True,
        "approval": {
            "required": True,
            "token": USDC,
            "spender": spender,
            "amountNeeded": str(amount_needed),
            "transaction": {
                "to": USDC,
                "data": approve_calldata(spender, approve_amount),
                "value": "0",
            },
        },
        "transaction": {"to": target, "data": "0x2213bc0b" + "22" * 32, "value": "0"},
    }


class TestRegistry:
    def test_aggregator_is_the_default_and_comes_first(self) -> None:
        assert DEFAULT_PROVIDER_ID == "aggregator"
        # The AgentOS DEX is the only swap provider.
        assert PROVIDER_IDS == ("aggregator",)
        assert provider_label("aggregator") == "AgentOS DEX"
        # Rows the removed Uniswap provider filled keep a readable label.
        assert provider_label("uniswap") == "Uniswap"

    def test_the_provider_is_not_a_setting(self) -> None:
        from agentos.gateway.config import TradingConfig

        assert "provider" not in TradingConfig.model_fields
        assert "uniswap_api_key" not in TradingConfig.model_fields

    def test_slippage_is_clamped_to_what_the_api_accepts(self) -> None:
        assert slippage_to_bps(None) == 50
        assert slippage_to_bps(0.5) == 50
        assert slippage_to_bps(1.25) == 125
        # The API refuses anything outside 1..500 bps, so we never send it.
        assert slippage_to_bps(0.0) == 1
        assert slippage_to_bps(40.0) == MAX_SLIPPAGE_BPS


class TestRequestShape:
    async def test_one_get_carries_the_whole_order(
        self, http, fake_aggregator: FakeAggregator
    ) -> None:
        quote = await _provider(http).quote(
            chain=BASE,
            swapper="0x1111111111111111111111111111111111111111",
            token_in=USDC,
            token_out=NATIVE_ADDRESS,
            amount_raw=10 * 10**6,
            slippage_pct=1.0,
            decision_origin="human_mediated",
        )
        request = fake_aggregator.requests[-1]
        assert request.method == "GET" and request.url.path == "/v1/quote"
        params = dict(request.url.params)
        assert params["chain"] == "8453"
        assert params["sell"] == USDC
        # Our own native address is the zero address, which the API rejects;
        # it must go out as the aggregator's sentinel instead.
        assert params["buy"] == NATIVE_SENTINEL
        assert params["sellAmountRaw"] == str(10 * 10**6)
        assert params["slippageBps"] == "100"
        assert params["taker"] == "0x1111111111111111111111111111111111111111"
        # Nothing secret travels with it: no key, no auth header, no body.
        assert "authorization" not in request.headers
        assert not request.content
        assert quote.provider == "aggregator"
        assert quote.token_out == NATIVE_ADDRESS
        assert quote.routing == "Uniswap_V4"
        assert quote.slippage_pct == 1.0
        # 0x is the default source and is asked by naming none.
        assert "source" not in params and quote.raw.source is None

    @pytest.mark.parametrize(
        ("source", "sent"), [("uniswap", "uniswap"), (" Uniswap ", "uniswap"), ("0x", None)]
    )
    async def test_a_named_source_travels_as_the_source_parameter(
        self, http, fake_aggregator: FakeAggregator, source: str, sent: str | None
    ) -> None:
        quote = await _provider(http).quote(
            chain=ROBINHOOD,
            swapper="0x1111111111111111111111111111111111111111",
            token_in="0xaaaa000000000000000000000000000000000001",
            token_out=NATIVE_ADDRESS,
            amount_raw=10**18,
            slippage_pct=None,
            decision_origin="human_mediated",
            source=source,
        )
        params = dict(fake_aggregator.requests[-1].url.params)
        assert params.get("source") == sent
        assert quote.raw.source == sent
        # Whichever source answered, it is one provider with one set of pins.
        assert quote.provider == "aggregator"

    async def test_an_unknown_source_never_reaches_the_network(
        self, http, fake_aggregator: FakeAggregator
    ) -> None:
        with pytest.raises(ProviderError) as info:
            await _provider(http).quote(
                chain=BASE,
                swapper="0x1111111111111111111111111111111111111111",
                token_in=USDC,
                token_out=WETH,
                amount_raw=10**6,
                slippage_pct=None,
                decision_origin="human_mediated",
                source="kyber",
            )
        assert info.value.code == "trading.invalid"
        assert fake_aggregator.requests == []

    async def test_an_out_of_range_slippage_is_clamped_and_said_so(self, http) -> None:
        quote = await _provider(http).quote(
            chain=BASE,
            swapper="0x1111111111111111111111111111111111111111",
            token_in=USDC,
            token_out=WETH,
            amount_raw=10**6,
            slippage_pct=40.0,
            decision_origin="human_mediated",
        )
        assert quote.slippage_pct == 5.0
        assert any("clamped" in w for w in quote.warnings)

    async def test_an_unserved_chain_never_reaches_the_network(
        self, http, fake_aggregator: FakeAggregator
    ) -> None:
        bad_chain = type(BASE)(**{**BASE.__dict__, "chain_id": 1, "name": "Ethereum"})
        with pytest.raises(ProviderError) as info:
            await _provider(http).quote(
                chain=bad_chain,
                swapper="0x1111111111111111111111111111111111111111",
                token_in=USDC,
                token_out=WETH,
                amount_raw=10**6,
                slippage_pct=None,
                decision_origin="human_mediated",
            )
        assert info.value.code == "trading.unsupported_chain"
        assert fake_aggregator.requests == []


class TestRefusals:
    @pytest.mark.parametrize(
        ("upstream", "status", "code", "retryable"),
        [
            ("UNKNOWN_TOKEN", 400, "trading.unknown_token", False),
            ("TOKEN_NOT_TRADEABLE", 400, "trading.token_not_tradeable", False),
            ("UNSUPPORTED_CHAIN", 400, "trading.unsupported_chain", False),
            ("INVALID_REQUEST", 400, "trading.invalid", False),
            ("UPSTREAM_REJECTED", 400, "trading.provider", False),
            ("UPSTREAM_ERROR", 502, "trading.provider", True),
            ("MISCONFIGURED", 503, "trading.provider", False),
            ("SOURCE_UNAVAILABLE", 503, "trading.provider", False),
        ],
    )
    async def test_every_error_code_maps_to_one_of_ours(
        self,
        http,
        fake_aggregator: FakeAggregator,
        upstream: str,
        status: int,
        code: str,
        retryable: bool,
    ) -> None:
        fake_aggregator.quote_error = (
            status,
            {"error": {"code": upstream, "message": f"{upstream} happened"}},
        )
        with pytest.raises(ProviderError) as info:
            await _provider(http).quote(
                chain=BASE,
                swapper="0x1111111111111111111111111111111111111111",
                token_in=USDC,
                token_out=WETH,
                amount_raw=10**6,
                slippage_pct=None,
                decision_origin="human_mediated",
            )
        assert info.value.code == code
        assert info.value.retryable is retryable
        assert f"{upstream} happened" in str(info.value)
        assert (info.value.details or {})["aggregatorCode"] == upstream

    async def test_a_legal_refusal_is_not_retried(
        self, http, fake_aggregator: FakeAggregator
    ) -> None:
        """``TOKEN_NOT_TRADEABLE`` is permanent: one request, no back-off."""
        fake_aggregator.quote_error = (400, {"error": {"code": "TOKEN_NOT_TRADEABLE"}})
        with pytest.raises(ProviderError):
            await _provider(http).quote(
                chain=ROBINHOOD,
                swapper="0x1111111111111111111111111111111111111111",
                token_in="0xaaaa000000000000000000000000000000000001",
                token_out=WETH,
                amount_raw=10**18,
                slippage_pct=None,
                decision_origin="autonomous",
            )
        assert fake_aggregator.quotes == 1

    async def test_no_liquidity_is_a_200_and_still_an_error_here(
        self, http, fake_aggregator: FakeAggregator
    ) -> None:
        fake_aggregator.liquidity = False
        with pytest.raises(ProviderError) as info:
            await _provider(http).quote(
                chain=BASE,
                swapper="0x1111111111111111111111111111111111111111",
                token_in=USDC,
                token_out=WETH,
                amount_raw=10**6,
                slippage_pct=None,
                decision_origin="human_mediated",
            )
        assert info.value.code == "trading.no_route"


class TestApprovalIntegrity:
    async def test_an_approval_that_names_another_spender_is_refused(
        self, http, fake_aggregator: FakeAggregator
    ) -> None:
        """The one relation the calldata cannot hide: spender is the target.

        A tampered (or merely wrong) response that approves some other
        contract is rejected while parsing — before any of it can be signed.
        """
        fake_aggregator.approval_needed = True
        fake_aggregator.tx_to = ALLOWANCE_HOLDER
        fake_aggregator.approval_spender = "0x000000000000000000000000000000000000dEaD"
        with pytest.raises(ProviderError) as info:
            await _provider(http).quote(
                chain=BASE,
                swapper="0x1111111111111111111111111111111111111111",
                token_in=USDC,
                token_out=WETH,
                amount_raw=10**6,
                slippage_pct=None,
                decision_origin="human_mediated",
            )
        assert info.value.code == "trading.tx_failed"
        assert "refusing to sign" in str(info.value)

    async def test_a_response_that_agrees_with_itself_is_not_thereby_trusted(
        self, http, fake_aggregator: FakeAggregator
    ) -> None:
        """Spender == target is necessary, not sufficient.

        The trusted set used to be read off the response, so a response
        approving any contract and sending the swap to that same contract
        passed every check. The set is pinned now: a consistent response
        naming a contract this desk does not know is refused all the same.
        """
        foreign = "0x000000000000000000000000000000000000dEaD"
        fake_aggregator.approval_needed = True
        fake_aggregator.tx_to = foreign
        fake_aggregator.approval_spender = foreign
        provider = _provider(http)
        with pytest.raises(ProviderError) as info:
            await provider.quote(
                chain=BASE,
                swapper="0x1111111111111111111111111111111111111111",
                token_in=USDC,
                token_out=WETH,
                amount_raw=10**6,
                slippage_pct=None,
                decision_origin="human_mediated",
            )
        assert info.value.code == "trading.tx_failed"
        assert "not a contract this desk trusts" in str(info.value)
        assert TRUSTED_SPENDERS == frozenset({ALLOWANCE_HOLDER, AGENTOS_ROUTER})
        assert foreign.lower() not in TRUSTED_SPENDERS

    @pytest.mark.parametrize("contract", [ALLOWANCE_HOLDER, AGENTOS_ROUTER])
    def test_each_trusted_contract_passes_as_spender_and_target(self, http, contract: str) -> None:
        """Direct 0x quotes name AllowanceHolder; contract mode names the AgentOS Router."""
        client = AggregatorClient(http=http)
        # The gateway sends checksummed addresses; the pin compares lowercase.
        checksummed = "0x4Cf93bac8920b1EFc92B0d2C84dcbE56802E530c"
        if contract == AGENTOS_ROUTER:
            assert checksummed.lower() == AGENTOS_ROUTER
            contract = checksummed
        body = _quote_body(
            spender=contract, target=contract, approve_amount=10**6, amount_needed=10**6
        )
        parsed = client._parse_quote(body, chain=BASE, taker=WALLET, amount_raw=10**6)
        assert parsed.approval is not None and parsed.spender == contract.lower()

    def test_a_configured_spender_is_trusted_as_spender_and_target(self, http) -> None:
        """``trading.aggregator_trusted_spenders`` adds to the pins, for both roles."""
        body = _quote_body(
            spender=CONFIGURED, target=CONFIGURED, approve_amount=10**6, amount_needed=10**6
        )
        # Not trusted until the operator says so — and the refusal names the key.
        with pytest.raises(ProviderError) as info:
            AggregatorClient(http=http)._parse_quote(
                body, chain=BASE, taker=WALLET, amount_raw=10**6
            )
        assert "not a contract this desk trusts" in str(info.value)
        assert "trading.aggregator_trusted_spenders" in str(info.value)
        client = AggregatorClient(
            http=http, extra_spenders=[CONFIGURED.upper().replace("0X", "0x")]
        )
        parsed = client._parse_quote(body, chain=BASE, taker=WALLET, amount_raw=10**6)
        assert parsed.spender == CONFIGURED
        assert client.trusted_spenders == TRUSTED_SPENDERS | {CONFIGURED}
        provider = AggregatorProvider(client)
        assert provider.trusted_spenders(BASE, None) == client.trusted_spenders  # type: ignore[arg-type]
        assert provider.trusted_targets(BASE, None) == client.trusted_spenders  # type: ignore[arg-type]
        # The module's pins are untouched: config adds per client, never globally.
        assert CONFIGURED not in TRUSTED_SPENDERS

    def test_a_configured_spender_still_must_be_the_swap_target(self, http) -> None:
        client = AggregatorClient(http=http, extra_spenders=[CONFIGURED])
        for spender, target in ((CONFIGURED, AGENTOS_ROUTER), (AGENTOS_ROUTER, CONFIGURED)):
            body = _quote_body(
                spender=spender, target=target, approve_amount=10**6, amount_needed=10**6
            )
            with pytest.raises(ProviderError) as info:
                client._parse_quote(body, chain=BASE, taker=WALLET, amount_raw=10**6)
            assert "but the swap is sent to" in str(info.value)

    @pytest.mark.parametrize(
        ("spender", "target"),
        [(AGENTOS_ROUTER, ALLOWANCE_HOLDER), (ALLOWANCE_HOLDER, AGENTOS_ROUTER)],
    )
    def test_two_trusted_contracts_still_must_be_the_same_one(
        self, http, spender: str, target: str
    ) -> None:
        """Approving one trusted contract and calling the other is refused."""
        client = AggregatorClient(http=http)
        body = _quote_body(
            spender=spender, target=target, approve_amount=10**6, amount_needed=10**6
        )
        with pytest.raises(ProviderError) as info:
            client._parse_quote(body, chain=BASE, taker=WALLET, amount_raw=10**6)
        assert "but the swap is sent to" in str(info.value)

    def test_a_trusted_spender_that_is_not_the_swap_target_is_refused(self, http) -> None:
        client = AggregatorClient(http=http)
        body = _quote_body(
            spender=ALLOWANCE_HOLDER, target=ROUTER, approve_amount=10**6, amount_needed=10**6
        )
        with pytest.raises(ProviderError) as info:
            client._parse_quote(body, chain=BASE, taker=WALLET, amount_raw=10**6)
        assert "but the swap is sent to" in str(info.value)

    @pytest.mark.parametrize("unlimited", [2**256 - 1, 2**255, 2**96 - 1])
    def test_an_unlimited_approval_is_refused(self, http, unlimited: int) -> None:
        """The API approves what the order needs; anything unlimited is not it."""
        client = AggregatorClient(http=http)
        # Either field may carry the bad number: the calldata is what gets signed,
        # amountNeeded is what the API claims.
        for amount_needed, approve_amount in ((unlimited, 10**6), (10**6, unlimited)):
            body = _quote_body(
                spender=ALLOWANCE_HOLDER,
                target=ALLOWANCE_HOLDER,
                approve_amount=approve_amount,
                amount_needed=amount_needed,
            )
            with pytest.raises(ProviderError) as info:
                client._parse_quote(body, chain=BASE, taker=WALLET, amount_raw=10**6)
            assert info.value.code == "trading.tx_failed"
            assert "unlimited" in str(info.value)
        # And exactly the order's amount passes.
        body = _quote_body(
            spender=ALLOWANCE_HOLDER,
            target=ALLOWANCE_HOLDER,
            approve_amount=10**6,
            amount_needed=10**6,
        )
        parsed = client._parse_quote(body, chain=BASE, taker=WALLET, amount_raw=10**6)
        assert parsed.approval is not None and parsed.spender == ALLOWANCE_HOLDER

    async def test_the_approval_comes_from_the_quote_not_a_second_call(
        self, http, fake_aggregator: FakeAggregator
    ) -> None:
        fake_aggregator.approval_needed = True
        fake_aggregator.tx_to = ALLOWANCE_HOLDER
        provider = _provider(http)
        quote = await provider.quote(
            chain=BASE,
            swapper="0x1111111111111111111111111111111111111111",
            token_in=USDC,
            token_out=WETH,
            amount_raw=10**6,
            slippage_pct=None,
            decision_origin="human_mediated",
        )
        before = len(fake_aggregator.requests)
        approval = await provider.approval_tx(quote, evm=None, decision_origin="human_mediated")
        assert len(fake_aggregator.requests) == before  # no round trip
        assert approval is not None
        assert approval["to"] == USDC and approval["value"] == "0"
        # The set the service checks against is the module's constant, not
        # whatever the response named.
        assert provider.trusted_spenders(BASE, quote) == TRUSTED_SPENDERS
        assert provider.trusted_spenders(BASE, quote) == frozenset(
            {ALLOWANCE_HOLDER, AGENTOS_ROUTER}
        )
        spender = "0x" + approval["data"][10 + 24 : 10 + 64]
        assert spender.lower() in provider.trusted_spenders(BASE, quote)

    async def test_selling_the_gas_coin_needs_no_approval(self, http) -> None:
        provider = _provider(http)
        quote = await provider.quote(
            chain=BASE,
            swapper="0x1111111111111111111111111111111111111111",
            token_in=NATIVE_ADDRESS,
            token_out=USDC,
            amount_raw=10**15,
            slippage_pct=None,
            decision_origin="human_mediated",
        )
        assert await provider.approval_tx(quote, evm=None, decision_origin="human_mediated") is None


class TestFreshness:
    async def test_a_quote_is_stale_before_the_api_says_it_expires(self, http) -> None:
        """We stop trusting calldata ahead of its own ``expiresAt``."""
        quote = await _provider(http).quote(
            chain=BASE,
            swapper="0x1111111111111111111111111111111111111111",
            token_in=USDC,
            token_out=WETH,
            amount_raw=10**6,
            slippage_pct=None,
            decision_origin="human_mediated",
        )
        assert quote.fresh is True
        assert quote.fresh_for_s <= 30.0
        quote.fetched_at -= quote.fresh_for_s + 1
        assert quote.fresh is False

    async def test_a_short_expiry_upstream_shortens_ours(self, http, fake_aggregator) -> None:
        fake_aggregator.expires_in_s = 3.0
        quote = await _provider(http).quote(
            chain=BASE,
            swapper="0x1111111111111111111111111111111111111111",
            token_in=USDC,
            token_out=WETH,
            amount_raw=10**6,
            slippage_pct=None,
            decision_origin="human_mediated",
        )
        assert quote.fresh_for_s < 4.0

    async def test_build_refetches_lapsed_calldata(self, http, fake_aggregator) -> None:
        provider = _provider(http)
        quote = await provider.quote(
            chain=BASE,
            swapper="0x1111111111111111111111111111111111111111",
            token_in=USDC,
            token_out=WETH,
            amount_raw=10**6,
            slippage_pct=None,
            decision_origin="human_mediated",
        )
        assert fake_aggregator.quotes == 1
        # A fresh quote is built from what it already has.
        tx = await provider.build(quote, deadline=0, decision_origin="autonomous")
        assert fake_aggregator.quotes == 1
        assert tx["to"] == ALLOWANCE_HOLDER and tx["data"].startswith("0x2213bc0b")
        assert tx["gasLimit"] == str(int(288079 * 1.2))
        # A lapsed one is fetched again rather than broadcast.
        quote.fetched_at -= 120
        fake_aggregator.amount_out = 4_000_000_000_000_000
        await provider.build(quote, deadline=0, decision_origin="autonomous")
        assert fake_aggregator.quotes == 2
        assert quote.amount_out_raw == 4_000_000_000_000_000
        assert quote.min_out_raw == 4_000_000_000_000_000 * 995 // 1000
        assert fake_aggregator.sources == ["0x", "0x"]

    async def test_build_refetches_from_the_source_that_priced_the_order(
        self, http, fake_aggregator
    ) -> None:
        provider = _provider(http)
        quote = await provider.quote(
            chain=BASE,
            swapper="0x1111111111111111111111111111111111111111",
            token_in=USDC,
            token_out=WETH,
            amount_raw=10**6,
            slippage_pct=None,
            decision_origin="human_mediated",
            source="uniswap",
        )
        quote.fetched_at -= 120
        await provider.build(quote, deadline=0, decision_origin="autonomous")
        # A lapsed Uniswap quote re-fetched from 0x would be refused for a stock.
        assert fake_aggregator.sources == ["uniswap", "uniswap"]
        assert quote.raw.source == "uniswap"


class TestThroughTheService:
    async def test_a_swap_routes_through_the_aggregator_end_to_end(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_aggregator: FakeAggregator,
    ) -> None:
        from tests.test_trading.test_service import _wire_swap_effects

        service = funded_service
        assert service.provider_id() == "aggregator"
        wallet = service.test_wallet  # type: ignore[attr-defined]
        fake_aggregator.approval_needed = True
        fake_aggregator.tx_to = ALLOWANCE_HOLDER  # what the live API sends the swap to
        _wire_swap_effects(base_chain, wallet, out_token=WETH, out_amount=5 * 10**15)
        orders = await service.swap(
            chain=BASE,
            wallets=None,
            token_in="USDC",
            token_out="WETH",
            amount_in="10",
            amount_pct=None,
            slippage_pct=0.5,
            initiator="manual",
            session_key=None,
            note=None,
            wait=True,
        )
        order = orders[0]
        assert order["status"] == "confirmed", order
        assert order["provider"] == "aggregator"
        assert order["providerLabel"] == "AgentOS DEX"
        assert order["approvalTxHash"] and order["txHash"]
        # Approval first, then the swap — two transactions, in that order.
        assert len(base_chain.sent) == 2
        approval_tx = decode_fake_raw(base_chain.sent[0])
        swap_tx = decode_fake_raw(base_chain.sent[1])
        assert approval_tx["to"].lower() == USDC
        assert swap_tx["to"].lower() == ALLOWANCE_HOLDER and swap_tx["value"] == 0
        # Uniswap was never consulted.
        # And the calldata went out byte for byte as the aggregator sent it.
        assert swap_tx["data"] == fake_aggregator.tx_data

    async def test_the_engine_prices_gas_the_aggregator_quotes_in_eth(
        self, funded_service: TradingService, fake_aggregator: FakeAggregator
    ) -> None:
        fake_aggregator.gas_cost_native = "0.0004"  # ETH; the fake prices it at $2,000
        quote = await funded_service.quote(
            chain=BASE, wallet=None, token_in="USDC", token_out="WETH", amount_in="10"
        )
        assert quote["gasUsd"] == pytest.approx(0.8)

    async def test_quote_carries_the_agentos_dex_fee_in_the_sell_token(
        self, funded_service: TradingService
    ) -> None:
        quote = await funded_service.quote(
            chain=BASE, wallet=None, token_in="USDC", token_out="WETH", amount_in="10"
        )
        # 20 bps of 10 USDC, written in USDC's own six decimals.
        assert quote["swapFee"] == {
            "bps": 20,
            "pct": 0.2,
            "symbol": "USDC",
            "amount": "0.02",
            "amountRaw": "20000",
        }

    async def test_an_unmatched_fee_token_keeps_the_aggregators_own_amount(
        self, funded_service: TradingService, fake_aggregator: FakeAggregator
    ) -> None:
        fake_aggregator.fee_symbol = "XYZ"
        fake_aggregator.fee_formatted = "0.02"
        quote = await funded_service.quote(
            chain=BASE, wallet=None, token_in="USDC", token_out="WETH", amount_in="10"
        )
        assert quote["swapFee"]["symbol"] == "XYZ"
        assert quote["swapFee"]["amount"] == "0.02"

    async def test_no_fee_block_means_no_swap_fee(
        self, funded_service: TradingService, fake_aggregator: FakeAggregator
    ) -> None:
        fake_aggregator.fee_bps = None
        quote = await funded_service.quote(
            chain=BASE, wallet=None, token_in="USDC", token_out="WETH", amount_in="10"
        )
        assert quote["swapFee"] is None

    async def test_status_lists_the_one_provider(self, funded_service: TradingService) -> None:
        status = await funded_service.status(check_rpc=True)
        assert status["provider"] == "aggregator"
        rows = status["providers"]
        assert [r["id"] for r in rows] == ["aggregator"]
        assert rows[0]["active"] is True and rows[0]["needsKey"] is False
        assert rows[0]["healthy"] is True
        assert "apiKeyConfigured" not in status
        # The geo-block flag went with KyberSwap; nothing reports one now.
        assert "blocked" not in rows[0]
        assert json.dumps(status)  # the whole payload stays JSON-serialisable


class TestSigning:
    async def test_a_lowercase_target_is_checksummed_before_it_is_signed(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_aggregator: FakeAggregator,
    ) -> None:
        """eth-account will not sign a ``to`` that is not EIP-55 checksummed.

        The aggregator answers in lowercase (0x's AllowanceHolder comes back
        as ``0x0000000000001ff3…``), and a live swap failed at the signer with
        "Transaction had invalid fields" until the engine re-checksummed it.
        Both legs are covered: the approval's ``to`` is one of our own
        normalised — therefore lowercase — token addresses.
        """
        from eth_utils import is_checksum_address

        from tests.test_trading.test_service import _wire_swap_effects

        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        fake_aggregator.approval_needed = True
        fake_aggregator.tx_to = "0x0000000000001ff3684f28c67538d4d072c22734"
        assert not is_checksum_address(fake_aggregator.tx_to)
        _wire_swap_effects(base_chain, wallet, out_token=WETH, out_amount=5 * 10**15)
        orders = await service.swap(
            chain=BASE,
            wallets=None,
            token_in="USDC",
            token_out="WETH",
            amount_in="10",
            amount_pct=None,
            slippage_pct=None,
            initiator="manual",
            session_key=None,
            note=None,
            wait=True,
        )
        assert orders[0]["status"] == "confirmed", orders[0]
        assert len(base_chain.sent) == 2
        for raw in base_chain.sent:
            assert is_checksum_address(decode_fake_raw(raw)["to"])

    async def test_a_swap_into_eth_reads_the_balance_at_the_receipt_block(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_aggregator: FakeAggregator,
    ) -> None:
        """A node one block behind must not book the ETH leg as ~nothing.

        A swap into the gas coin has no Transfer log to fall back on, so a
        stale ``latest`` balance would be booked verbatim. Measured live on
        Base on 2026-09-19: a confirmed swap reported ``receivedOut`` short
        by exactly the preceding approval's gas, because the balance came
        from the block before. Here "latest" is frozen at the pre-swap
        number and only a block-pinned read sees the ETH arrive.
        """
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        received = 20_000_000_000_000  # 0.00002 ETH
        fake_aggregator.amount_out = received
        _wire_swap_effects(base_chain, wallet, out_token=NATIVE_ADDRESS, out_amount=received)

        frozen = base_chain.native.get(wallet.lower(), 0)
        asked: list[str] = []

        def balance_at(address: str, block: str) -> int:
            asked.append(block)
            live = base_chain.native.get(address, 0)
            if address != wallet.lower():
                return live
            return frozen if block == "latest" else live

        base_chain.balance_at = balance_at
        orders = await service.swap(
            chain=BASE,
            wallets=None,
            token_in="USDC",
            token_out="ETH",
            amount_in="10",
            amount_pct=None,
            slippage_pct=None,
            initiator="manual",
            session_key=None,
            note=None,
            wait=True,
        )
        assert orders[0]["status"] == "confirmed", orders[0]
        assert any(block != "latest" for block in asked), asked
        assert orders[0]["receivedOut"] == "0.00002"

    async def test_a_balance_equal_to_the_pre_swap_one_is_retried_not_believed(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_aggregator: FakeAggregator,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A load balancer answers the stale read without erroring.

        Measured on Base: the block-pinned read came back with the previous
        block's state and no error, so the ETH leg booked as the gas cost
        alone. The sender of a mined transaction has paid for its gas, so a
        balance identical to the pre-swap one cannot be the truth — it is
        retried until the node catches up.
        """
        monkeypatch.setattr(service_module, "CHAIN_CATCHUP_POLL_S", 0.0)
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        received = 20_000_000_000_000
        fake_aggregator.amount_out = received
        _wire_swap_effects(base_chain, wallet, out_token=NATIVE_ADDRESS, out_amount=received)

        frozen = base_chain.native.get(wallet.lower(), 0)
        stale_reads: list[str] = []

        def balance_at(address: str, block: str) -> int:
            live = base_chain.native.get(address, 0)
            if address != wallet.lower() or not base_chain.sent:
                return live  # nothing has been broadcast yet: nothing to lag behind
            if len(stale_reads) < 3:
                stale_reads.append(block)
                return frozen  # a node that has not seen the swap's block
            return live

        base_chain.balance_at = balance_at
        orders = await service.swap(
            chain=BASE,
            wallets=None,
            token_in="USDC",
            token_out="ETH",
            amount_in="10",
            amount_pct=None,
            slippage_pct=None,
            initiator="manual",
            session_key=None,
            note=None,
            wait=True,
        )
        assert orders[0]["status"] == "confirmed", orders[0]
        assert orders[0]["receivedOut"] == "0.00002"
        # It insisted rather than believing the first stale answer.
        assert len(stale_reads) == 3, stale_reads

    async def test_the_swap_waits_for_the_allowance_to_become_visible(
        self,
        funded_service: TradingService,
        base_chain: FakeChain,
        fake_aggregator: FakeAggregator,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An approval's receipt does not mean the next eth_call can see it.

        Measured on Base: the swap simulated as "ERC20: transfer amount
        exceeds allowance" against an allowance the same endpoint reported
        as present moments later, and the order failed for a reason the user
        could do nothing about. The engine now waits for the read side to
        catch up before it simulates.
        """
        monkeypatch.setattr(service_module, "CHAIN_CATCHUP_POLL_S", 0.0)
        service = funded_service
        wallet = service.test_wallet  # type: ignore[attr-defined]
        fake_aggregator.approval_needed = True
        fake_aggregator.tx_to = ALLOWANCE_HOLDER
        _wire_swap_effects(base_chain, wallet, out_token=WETH, out_amount=5 * 10**15)

        lagging = {"reads": 0}

        class _Lagging(dict):
            """A node that has not applied the approval for its first reads."""

            def get(self, key, default=None):  # type: ignore[override]
                value = super().get(key, default)
                if value:
                    lagging["reads"] += 1
                    if lagging["reads"] <= 3:
                        return 0
                return value

        base_chain.allowances = _Lagging(base_chain.allowances)
        orders = await service.swap(
            chain=BASE,
            wallets=None,
            token_in="USDC",
            token_out="WETH",
            amount_in="10",
            amount_pct=None,
            slippage_pct=None,
            initiator="manual",
            session_key=None,
            note=None,
            wait=True,
        )
        assert orders[0]["status"] == "confirmed", orders[0]
        assert lagging["reads"] > 3, lagging


class TestTrustedSpendersConfig:
    def test_default_is_empty(self) -> None:
        from agentos.gateway.config import TradingConfig

        assert TradingConfig().aggregator_trusted_spenders == []

    def test_addresses_are_lowercased_and_deduplicated(self) -> None:
        from agentos.gateway.config import TradingConfig

        checksummed = "0x4Cf93bac8920b1EFc92B0d2C84dcbE56802E530c"
        cfg = TradingConfig(
            aggregator_trusted_spenders=[f"  {checksummed} ", checksummed.lower(), CONFIGURED]
        )
        assert cfg.aggregator_trusted_spenders == [AGENTOS_ROUTER, CONFIGURED]

    @pytest.mark.parametrize(
        "bad",
        [
            "0x4Cf93bac8920b1EFc92B0d2C84dcbE56802E530C",  # one letter's case flipped
            "0x1234",
            "not-an-address",
            "0x" + "zz" * 20,
            "0x0000000000000000000000000000000000000000",
            "",
            "eth",
        ],
    )
    def test_a_bad_address_is_refused(self, bad: str) -> None:
        from agentos.gateway.config import TradingConfig

        with pytest.raises(ValueError, match="aggregator_trusted_spenders"):
            TradingConfig(aggregator_trusted_spenders=[bad])

    def test_validate_assignment_applies(self) -> None:
        from agentos.gateway.config import TradingConfig

        cfg = TradingConfig()
        with pytest.raises(ValueError):
            cfg.aggregator_trusted_spenders = ["0x1234"]
        assert cfg.aggregator_trusted_spenders == []
