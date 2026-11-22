"""The bridge module on its own: routes, reading an AgentOS DEX quote, what may be signed."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from agentos.trading import bridge
from agentos.trading.chains import BASE, NATIVE_ADDRESS, ROBINHOOD
from tests.test_trading.fakes import (
    AGENTOS_BRIDGE,
    BRIDGE_NATIVE,
    FakeDexBridge,
    bridge_calldata,
    bridge_fixture,
)

#: The taker the fixtures were recorded for (the gateway docs' placeholder).
WALLET = "0x7a3f5bc1d2e4968a0b1c2d3e4f5061728394a5b6"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
ETH_RAW = 10_000_000_000_000_000
USD_RAW = 25_000_000
ETH_ROUTE = bridge.resolve_route(BASE, ROBINHOOD, "ETH")
USDC_ROUTE = bridge.resolve_route(BASE, ROBINHOOD, "USDC")
USDG_ROUTE = bridge.resolve_route(ROBINHOOD, BASE, "USDG")


def fixture(name: str) -> dict[str, Any]:
    return copy.deepcopy(bridge_fixture(name))


def _parse(name: str, route: bridge.BridgeRoute, amount: int) -> bridge.BridgeQuote:
    return bridge.parse_quote(fixture(name), route=route, wallet=WALLET, amount_raw=amount)


def _now(quote: bridge.BridgeQuote) -> float:
    """A moment the recorded quote was still good at."""
    assert quote.expires_at is not None
    return quote.expires_at - 300.0


def _sign(quote: bridge.BridgeQuote, route: bridge.BridgeRoute, amount: int) -> dict[str, Any]:
    return bridge.bridge_transaction(
        quote, route=route, wallet=WALLET, amount_raw=amount, now=_now(quote)
    )


def _recode(quote: bridge.BridgeQuote, route: bridge.BridgeRoute, **change: Any) -> None:
    """Re-encode the quote's calldata with one field of ``BridgeParams`` changed."""
    assert quote.transaction is not None and quote.order_id and quote.expires_at
    fields: dict[str, Any] = {
        "source": quote.source,
        "token": bridge.token_param(route.token_in.address),
        "amount": quote.amount_raw,
        "net": quote.net_raw,
        "order_id": quote.order_id,
        "destination": route.destination.chain_id,
        "recipient": WALLET,
        "deadline": quote.expires_at,
    }
    fields.update(change)
    quote.transaction["data"] = bridge_calldata(**fields)


# ── routes ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("origin", "destination", "token", "symbol_in", "symbol_out"),
    [
        (BASE, ROBINHOOD, "ETH", "ETH", "ETH"),
        (BASE, ROBINHOOD, "USDC", "USDC", "USDG"),
        (BASE, ROBINHOOD, USDC, "USDC", "USDG"),
        (ROBINHOOD, BASE, "eth", "ETH", "ETH"),
        (ROBINHOOD, BASE, "USDG", "USDG", "USDC"),
        (ROBINHOOD, BASE, bridge.USDG_ROBINHOOD, "USDG", "USDC"),
        # Naming the token by what it is called on the other side works too.
        (ROBINHOOD, BASE, "USDC", "USDG", "USDC"),
        (BASE, ROBINHOOD, "USDG", "USDC", "USDG"),
        (BASE, ROBINHOOD, NATIVE_ADDRESS, "ETH", "ETH"),
    ],
)
def test_resolve_route_pairs_assets_across_the_two_chains(
    origin: Any, destination: Any, token: str, symbol_in: str, symbol_out: str
) -> None:
    route = bridge.resolve_route(origin, destination, token)

    assert route.origin is origin and route.destination is destination
    assert (route.token_in.symbol, route.token_out.symbol) == (symbol_in, symbol_out)
    assert route.token_in.asset == route.token_out.asset
    assert route.to_dict()["tokenOut"]["decimals"] == route.token_out.decimals


@pytest.mark.parametrize(
    ("origin", "destination", "token"),
    [
        (BASE, BASE, "ETH"),
        (BASE, ROBINHOOD, "WETH"),
        (BASE, ROBINHOOD, "0x4200000000000000000000000000000000000006"),
        (ROBINHOOD, BASE, "AAPL"),
    ],
)
def test_resolve_route_refuses_anything_off_the_registry(
    origin: Any, destination: Any, token: str
) -> None:
    with pytest.raises(bridge.BridgeError) as info:
        bridge.resolve_route(origin, destination, token)

    assert info.value.code == "trading.bridge.unsupported_route"
    routes = info.value.details["supportedRoutes"]
    assert {(r["from"], r["to"], r["tokenIn"], r["tokenOut"]) for r in routes} == {
        ("base", "robinhood", "ETH", "ETH"),
        ("base", "robinhood", "USDC", "USDG"),
        ("robinhood", "base", "ETH", "ETH"),
        ("robinhood", "base", "USDG", "USDC"),
    }


# ── quotes ──────────────────────────────────────────────────────────────


def test_parse_quote_reads_a_firm_eth_quote() -> None:
    quote = _parse("quote_eth_base_to_rh", ETH_ROUTE, ETH_RAW)

    assert quote.firm and quote.source == "relay"
    assert quote.amount_raw == ETH_RAW
    # 5 bps off the top; the rest is what the bridge was quoted for.
    assert (quote.fee_bps, quote.fee_raw, quote.net_raw) == (5, 5 * 10**12, ETH_RAW - 5 * 10**12)
    assert quote.min_buy_raw < quote.expected_out_raw < quote.net_raw
    assert quote.rate == pytest.approx(quote.expected_out_raw / ETH_RAW)
    assert quote.refund_guaranteed is True and quote.time_estimate_s == 1.0
    assert quote.bridge == AGENTOS_BRIDGE and quote.allowance_target is None
    assert quote.request_id == quote.tracking_id and len(quote.tracking_id or "") == 66
    assert quote.order_id != quote.tracking_id
    stored = quote.to_dict()
    assert stored["requestId"] == quote.tracking_id and stored["source"] == "relay"
    assert stored["fee"] == {"bps": 5, "amountRaw": str(5 * 10**12)}
    assert stored["sourceLabel"] == "Relay" and stored["bridge"] == AGENTOS_BRIDGE
    # Both bridges are in the comparison, the chosen one marked.
    assert [(r["source"], r["selected"]) for r in stored["routes"]] == [
        ("relay", True),
        ("across", False),
    ]
    assert all(r["available"] and r["expectedOutRaw"] for r in stored["routes"])


def test_parse_quote_reads_a_firm_stable_quote_both_ways() -> None:
    out = _parse("quote_usdc_base_to_rh", USDC_ROUTE, USD_RAW)
    back = _parse("quote_usdg_rh_to_base", USDG_ROUTE, USD_RAW)

    for quote in (out, back):
        assert (quote.fee_raw, quote.net_raw) == (12_500, 24_987_500)
        assert quote.allowance_target == AGENTOS_BRIDGE == quote.bridge
        assert 24_000_000 < quote.min_buy_raw < quote.expected_out_raw < 25_000_000
    # The gateway says when a bridge does not promise the refund.
    across = next(r for r in out.to_dict()["routes"] if r["source"] == "across")
    assert across["refundGuaranteed"] is False and across["label"] == "Across"


def test_an_indicative_quote_has_amounts_and_nothing_to_sign() -> None:
    quote = bridge.parse_quote(
        fixture("quote_eth_base_to_rh_indicative"),
        route=ETH_ROUTE,
        wallet=None,
        amount_raw=ETH_RAW,
    )

    assert not quote.firm and quote.transaction is None
    assert quote.request_id is None and quote.order_id is None and quote.bridge is None
    assert quote.expected_out_raw > 0 and quote.wallet is None
    with pytest.raises(bridge.BridgeError) as info:
        bridge.bridge_transaction(quote, route=ETH_ROUTE, wallet=WALLET, amount_raw=ETH_RAW)
    assert info.value.code == "trading.bridge.unsupported_step"


def test_a_firm_request_answered_without_a_transaction_is_refused() -> None:
    with pytest.raises(bridge.BridgeError, match="no transaction to sign"):
        bridge.parse_quote(
            fixture("quote_eth_base_to_rh_indicative"),
            route=ETH_ROUTE,
            wallet=WALLET,
            amount_raw=ETH_RAW,
        )


def _set(path: tuple[str, ...], value: Any) -> Callable[[dict[str, Any]], None]:
    def mutate(body: dict[str, Any]) -> None:
        node = body
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value

    return mutate


@pytest.mark.parametrize(
    ("mutate", "fragment"),
    [
        (_set(("originChainId",), 4663), "leaves chain 4663"),
        (_set(("destinationChainId",), 42161), "arrives on chain 42161"),
        (_set(("sellToken", "address"), "0x" + "22" * 20), "the quote sells"),
        (_set(("buyToken", "address"), "0x4200000000000000000000000000000000000006"), "delivers"),
        (_set(("sellAmount",), "24999999"), "the order is 25000000"),
        (_set(("source",), "wormhole"), "unknown bridge"),
        (_set(("fee", "bps"), 150), "above the 100 bps cap"),
        (_set(("fee", "amount"), "500000"), "are not 5 bps"),
        (_set(("netAmount",), "25000000"), "are not 5 bps"),
        (_set(("fee",), None), "does not say what the bridge fee is"),
        (_set(("buyAmount",), "0"), "no destination amount"),
        (_set(("minBuyAmount",), "20000000"), "more than 5% short"),
        (_set(("refund", "address"), "0x" + "33" * 20), "not the wallet"),
        (_set(("trackingId",), "0x1234"), "no order id"),
        (_set(("orderId",), None), "no order id"),
    ],
)
def test_parse_quote_refuses_a_quote_for_another_order(
    mutate: Callable[[dict[str, Any]], None], fragment: str
) -> None:
    body = fixture("quote_usdc_base_to_rh")
    mutate(body)

    with pytest.raises(bridge.BridgeError, match=fragment) as info:
        bridge.parse_quote(body, route=USDC_ROUTE, wallet=WALLET, amount_raw=USD_RAW)
    assert info.value.code == "trading.bridge.provider"


def test_a_floor_above_the_expected_amount_is_read_as_the_expected_amount() -> None:
    body = fixture("quote_usdc_base_to_rh")
    body["minBuyAmount"] = str(int(body["buyAmount"]) + 5)

    quote = bridge.parse_quote(body, route=USDC_ROUTE, wallet=WALLET, amount_raw=USD_RAW)

    assert quote.min_buy_raw == quote.expected_out_raw


# ── what is signed ──────────────────────────────────────────────────────


def test_a_native_bridge_is_one_call_with_the_value_and_the_decoded_order() -> None:
    quote = _parse("quote_eth_base_to_rh", ETH_ROUTE, ETH_RAW)

    tx = _sign(quote, ETH_ROUTE, ETH_RAW)

    assert tx["to"] == AGENTOS_BRIDGE and tx["value"] == str(ETH_RAW)
    assert tx["chainId"] == 8453 and tx["from"] == WALLET
    assert tx["data"].startswith(bridge.BRIDGE_SELECTOR) and len(tx["data"]) == 10 + 8 * 64
    # The engine sets the gas limit itself.
    assert "gasLimit" not in tx and "gas" not in tx


def test_an_erc20_bridge_carries_no_value_and_names_the_bridge_as_spender() -> None:
    for name, route in (
        ("quote_usdc_base_to_rh", USDC_ROUTE),
        ("quote_usdg_rh_to_base", USDG_ROUTE),
    ):
        quote = _parse(name, route, USD_RAW)
        tx = _sign(quote, route, USD_RAW)
        assert tx["to"] == AGENTOS_BRIDGE and tx["value"] == "0"
        assert tx["chainId"] == route.origin.chain_id
        # No approval comes from the gateway: the engine encodes its own.
        assert set(tx) == {"from", "to", "data", "value", "chainId"}


def test_an_operator_trusted_bridge_contract_is_accepted_and_no_other() -> None:
    quote = _parse("quote_eth_base_to_rh", ETH_ROUTE, ETH_RAW)
    moved = "0x" + "ab" * 20
    assert quote.transaction is not None
    quote.transaction["to"] = moved

    with pytest.raises(bridge.BridgeError, match="not an AgentOS bridge contract") as info:
        _sign(quote, ETH_ROUTE, ETH_RAW)
    assert info.value.code == "trading.tx_failed"

    tx = bridge.bridge_transaction(
        quote,
        route=ETH_ROUTE,
        wallet=WALLET,
        amount_raw=ETH_RAW,
        trusted=bridge.TRUSTED_BRIDGES | {moved},
        now=_now(quote),
    )
    assert tx["to"] == moved


@pytest.mark.parametrize(
    ("change", "code", "fragment"),
    [
        ({"source": "across"}, "trading.tx_failed", "does not deposit into relay"),
        ({"token": "0x" + "22" * 20}, "trading.tx_failed", "another token"),
        ({"amount": ETH_RAW + 1}, "trading.tx_failed", "the order is"),
        ({"net": ETH_RAW - 10**15}, "trading.tx_failed", "the quote said"),
        ({"order_id": "0x" + "9" * 64}, "trading.tx_failed", "another order id"),
        ({"order_id": "0x" + "0" * 64}, "trading.tx_failed", "another order id"),
        ({"destination": 42161}, "trading.tx_failed", "delivers on chain 42161"),
        ({"recipient": "0x" + "33" * 20}, "trading.tx_failed", "another address"),
    ],
)
def test_calldata_for_any_other_order_is_refused(
    change: dict[str, Any], code: str, fragment: str
) -> None:
    quote = _parse("quote_eth_base_to_rh", ETH_ROUTE, ETH_RAW)
    _recode(quote, ETH_ROUTE, **change)

    with pytest.raises(bridge.BridgeError, match=fragment) as info:
        _sign(quote, ETH_ROUTE, ETH_RAW)
    assert info.value.code == code


def test_the_deadline_must_be_ahead_and_not_far_ahead() -> None:
    quote = _parse("quote_eth_base_to_rh", ETH_ROUTE, ETH_RAW)
    assert quote.expires_at is not None

    with pytest.raises(bridge.BridgeError, match="expired") as late:
        bridge.bridge_transaction(
            quote, route=ETH_ROUTE, wallet=WALLET, amount_raw=ETH_RAW, now=quote.expires_at + 1.0
        )
    assert late.value.code == "trading.bridge.provider" and late.value.retryable

    with pytest.raises(bridge.BridgeError, match="too far ahead") as far:
        bridge.bridge_transaction(
            quote, route=ETH_ROUTE, wallet=WALLET, amount_raw=ETH_RAW, now=quote.expires_at - 7200.0
        )
    assert far.value.code == "trading.tx_failed"


def _tx(key: str, value: Any) -> Callable[[bridge.BridgeQuote], None]:
    def mutate(quote: bridge.BridgeQuote) -> None:
        assert quote.transaction is not None
        quote.transaction[key] = value

    return mutate


def _attr(key: str, value: Any) -> Callable[[bridge.BridgeQuote], None]:
    return lambda quote: setattr(quote, key, value)


@pytest.mark.parametrize(
    ("route_name", "mutate", "code", "fragment"),
    [
        ("eth", _tx("chainId", 4663), "trading.bridge.unsupported_step", "runs on chain 4663"),
        ("eth", _tx("to", "0x1234"), "trading.bridge.unsupported_step", "names no contract"),
        ("eth", _tx("to", ""), "trading.tx_failed", "not an AgentOS bridge contract"),
        ("eth", _tx("value", "1"), "trading.tx_failed", "is not the order's"),
        ("eth", _tx("data", "0x"), "trading.bridge.unsupported_step", "not bridge"),
        (
            "eth",
            _tx("data", "0x095ea7b3" + "0" * 512),
            "trading.bridge.unsupported_step",
            "not bridge",
        ),
        (
            "eth",
            _tx("data", bridge.BRIDGE_SELECTOR + "zz" * 256),
            "trading.bridge.unsupported_step",
            "not hex",
        ),
        (
            "eth",
            _attr("allowance_target", AGENTOS_BRIDGE),
            "trading.bridge.unsupported_step",
            "approval on a native bridge",
        ),
        ("usdc", _tx("value", str(USD_RAW)), "trading.tx_failed", "carries native value"),
        (
            "usdc",
            _attr("allowance_target", "0x" + "44" * 20),
            "trading.tx_failed",
            "is not the bridge contract",
        ),
        ("usdc", _attr("allowance_target", None), "trading.tx_failed", "is not the bridge"),
    ],
)
def test_a_transaction_off_pattern_is_refused(
    route_name: str, mutate: Callable[[bridge.BridgeQuote], None], code: str, fragment: str
) -> None:
    route, name, amount = (
        (ETH_ROUTE, "quote_eth_base_to_rh", ETH_RAW)
        if route_name == "eth"
        else (USDC_ROUTE, "quote_usdc_base_to_rh", USD_RAW)
    )
    quote = _parse(name, route, amount)
    mutate(quote)

    with pytest.raises(bridge.BridgeError, match=fragment) as info:
        _sign(quote, route, amount)
    assert info.value.code == code


def test_dirty_high_bytes_in_an_address_word_are_refused() -> None:
    quote = _parse("quote_usdc_base_to_rh", USDC_ROUTE, USD_RAW)
    assert quote.transaction is not None
    data = str(quote.transaction["data"])
    # The recipient word, with a byte set above the 20 the address occupies.
    start = 10 + 6 * 64
    quote.transaction["data"] = data[:start] + "01" + data[start + 2 :]

    with pytest.raises(bridge.BridgeError, match="another address"):
        _sign(quote, USDC_ROUTE, USD_RAW)


# ── status ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("said", "status"),
    [
        ("pending", "pending"),
        ("filled", "success"),
        ("refunded", "refund"),
        ("failed", "failure"),
        ("unknown", "unknown"),
        ("FILLED", "success"),
        ("something-new", "unknown"),
        (None, "unknown"),
    ],
)
def test_fill_status_from_the_gateways_five_words(said: Any, status: str) -> None:
    assert bridge.fill_status_from({"status": said})["status"] == status


def test_fill_status_reads_the_recorded_answer() -> None:
    fill = bridge.fill_status_from(fixture("status_filled"))

    assert fill["status"] == "success"
    assert fill["originTxHash"].startswith("0xf417") and len(fill["destinationTxHash"]) == 66
    assert bridge.fill_status_from(None) == {
        "status": "unknown",
        "originTxHash": None,
        "destinationTxHash": None,
    }


# ── the client ──────────────────────────────────────────────────────────


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[bridge.DexBridgeClient, list[float]]:
    naps: list[float] = []

    async def nap(seconds: float) -> None:
        naps.append(seconds)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return bridge.DexBridgeClient(http=http, sleep=nap), naps


async def test_quote_asks_the_gateway_for_exactly_the_route() -> None:
    fake = FakeDexBridge()
    client, _ = _client(fake.handle)

    preview = await client.quote(route=USDC_ROUTE, amount_raw=USD_RAW)
    firm = await client.quote(route=ETH_ROUTE, amount_raw=ETH_RAW, wallet=WALLET, source="across")

    asked = fake.calls[0]["params"]
    assert asked == {
        "originChain": "8453",
        "destinationChain": "4663",
        "sellToken": USDC,
        "buyToken": bridge.USDG_ROBINHOOD,
        "amount": str(USD_RAW),
    }
    assert not preview.firm
    pinned = fake.calls[1]["params"]
    # The coin goes by the gateway's own sentinel, never the zero address.
    assert pinned["sellToken"] == pinned["buyToken"] == BRIDGE_NATIVE
    assert pinned["taker"] == WALLET and pinned["source"] == "across"
    assert firm.firm and firm.source == "across"
    assert bridge.bridge_transaction(firm, route=ETH_ROUTE, wallet=WALLET, amount_raw=ETH_RAW)


async def test_status_names_the_bridge_and_the_tracking_id() -> None:
    fake = FakeDexBridge(default_statuses=[{"status": "filled", "destinationTxHash": "0xD00D"}])
    client, _ = _client(fake.handle)

    fill = await client.status("0x" + "ab" * 32, source="across")

    assert fake.calls[-1]["params"] == {"source": "across", "trackingId": "0x" + "ab" * 32}
    assert fill["status"] == "success" and fill["destinationTxHash"] == "0xd00d"
    with pytest.raises(bridge.BridgeError, match="requestId is required"):
        await client.status("")


async def test_a_refused_pair_is_an_unsupported_route_with_the_list() -> None:
    client, naps = _client(lambda _r: httpx.Response(400, json=bridge_fixture("error_pair")))

    with pytest.raises(bridge.BridgeError) as info:
        await client.quote(route=ETH_ROUTE, amount_raw=ETH_RAW)

    assert info.value.code == "trading.bridge.unsupported_route" and not info.value.retryable
    assert info.value.details["aggregatorCode"] == "UNSUPPORTED_BRIDGE_PAIR"
    assert len(info.value.details["supportedRoutes"]) == 4
    assert naps == []


async def test_no_route_says_why_for_each_bridge() -> None:
    client, naps = _client(lambda _r: httpx.Response(400, json=bridge_fixture("error_no_route")))

    with pytest.raises(bridge.BridgeError) as info:
        await client.quote(route=ETH_ROUTE, amount_raw=1000)

    assert info.value.code == "trading.bridge.no_route" and not info.value.retryable
    assert "Relay: NO_ROUTE, Across: NO_ROUTE" in str(info.value)
    assert "a larger amount often works" in str(info.value)
    assert [r["reason"] for r in info.value.details["routes"]] == ["NO_ROUTE", "NO_ROUTE"]
    assert naps == []


async def test_no_route_because_a_bridge_was_down_is_retried() -> None:
    body = bridge_fixture("error_no_route")
    body["error"]["details"]["routes"][1]["reason"] = "UPSTREAM_ERROR"
    hits = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal hits
        hits += 1
        return httpx.Response(400, json=body)

    client, naps = _client(handler)

    with pytest.raises(bridge.BridgeError) as info:
        await client.quote(route=ETH_ROUTE, amount_raw=1000)

    assert info.value.code == "trading.bridge.no_route" and info.value.retryable
    assert hits == 3 and len(naps) == 2


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (503, "BRIDGE_DISABLED", False),
        (503, "MISCONFIGURED", False),
        (503, "SOURCE_UNAVAILABLE", False),
        (429, "RATE_LIMITED", True),
        (502, "UPSTREAM_ERROR", True),
        (500, "INTERNAL_ERROR", True),
        (418, "SOMETHING_NEW", False),
        (500, "SOMETHING_NEW", True),
    ],
)
async def test_gateway_failures_are_provider_errors(
    status: int, code: str, retryable: bool
) -> None:
    hits = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal hits
        hits += 1
        return httpx.Response(status, json={"error": {"code": code, "message": "nope"}})

    client, _ = _client(handler)

    with pytest.raises(bridge.BridgeError) as info:
        await client.quote(route=ETH_ROUTE, amount_raw=ETH_RAW)

    assert info.value.code == "trading.bridge.provider"
    assert info.value.retryable is retryable
    assert info.value.details["aggregatorCode"] == code
    assert hits == (3 if retryable else 1)


async def test_an_invalid_request_is_the_callers_to_fix() -> None:
    client, _ = _client(
        lambda _r: httpx.Response(
            400, json={"error": {"code": "INVALID_REQUEST", "message": "amount must be > 0"}}
        )
    )

    with pytest.raises(bridge.BridgeError, match="amount must be > 0") as info:
        await client.quote(route=ETH_ROUTE, amount_raw=ETH_RAW)
    assert info.value.code == "trading.invalid"


async def test_an_unreachable_gateway_is_retried_then_a_provider_error() -> None:
    hits = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal hits
        hits += 1
        raise httpx.ConnectError("refused", request=request)

    client, naps = _client(handler)

    with pytest.raises(bridge.BridgeError, match="AgentOS DEX unreachable") as info:
        await client.status("0x" + "ab" * 32)

    assert info.value.code == "trading.bridge.provider" and info.value.retryable
    assert hits == 3 and naps == [0.5, 1.0]


async def test_a_rate_limit_waits_as_long_as_the_gateway_says() -> None:
    answers = [
        httpx.Response(
            429,
            json={"error": {"code": "RATE_LIMITED", "message": "slow", "retryAfterSeconds": 2}},
        ),
        httpx.Response(200, json=bridge_fixture("status_filled")),
    ]
    client, naps = _client(lambda _r: answers.pop(0))

    fill = await client.status("0x" + "ab" * 32)

    assert fill["status"] == "success" and naps == [2]


async def test_a_body_that_is_not_json_is_a_provider_error() -> None:
    client, _ = _client(lambda _r: httpx.Response(200, text="<html>"))

    with pytest.raises(bridge.BridgeError, match="returned no JSON"):
        await client.status("0x" + "ab" * 32)


def test_source_labels() -> None:
    assert bridge.source_label("relay") == "Relay"
    assert bridge.source_label("ACROSS") == "Across"
    assert bridge.source_label("stargate") == "Stargate"
    assert bridge.source_label(None) == "Relay"
