"""The bridge module on its own: routes, Relay quote reading, step validation."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from agentos.trading import bridge
from agentos.trading.chains import BASE, NATIVE_ADDRESS, ROBINHOOD

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "relay"
WALLET = "0x1111111111111111111111111111111111111111"
SOLVER = "0x4cd00e387622c35bddb9b4c962c136462338bc31"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
ETH_RAW = 10_000_000_000_000_000
USDC_RAW = 10_000_000


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


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


def test_normalize_quote_reads_the_eth_quote() -> None:
    route = bridge.resolve_route(BASE, ROBINHOOD, "ETH")
    quote = bridge.normalize_quote(
        fixture("quote_eth_base_to_rh"), route=route, wallet=WALLET, amount_raw=ETH_RAW
    )

    assert quote.request_id and quote.request_id.startswith("0x")
    assert quote.solver == SOLVER
    assert quote.expected_out_raw == 9_988_625_576_077_740
    assert quote.rate == pytest.approx(0.998862557607774)
    assert quote.time_estimate_s == 1
    assert quote.amount_in_usd == pytest.approx(22.950588)
    # gas + relayer + app, never relayerGas/relayerService (inside relayer) nor subsidized.
    assert quote.fees_usd == pytest.approx(0.000483 + 0.026090 + 0, abs=1e-3)
    assert set(quote.fees) == {"gasUsd", "relayerUsd", "appUsd"}
    assert quote.to_dict()["expectedOutRaw"] == "9988625576077740"


def test_normalize_quote_sums_fees_from_the_top_level_buckets_only() -> None:
    body = fixture("quote_usdc_base_to_rh")
    route = bridge.resolve_route(BASE, ROBINHOOD, "USDC")
    quote = bridge.normalize_quote(body, route=route, wallet=WALLET, amount_raw=USDC_RAW)

    fees = body["fees"]
    expected = sum(float(fees[k]["amountUsd"]) for k in ("gas", "relayer", "app"))
    assert quote.fees_usd == pytest.approx(expected, abs=1e-6)
    assert quote.expected_out_raw == 9_972_681


@pytest.mark.parametrize(
    ("mutate", "fragment"),
    [
        (lambda b: b["details"]["currencyIn"].__setitem__("amount", "1"), "base units"),
        (lambda b: b["details"]["currencyOut"]["currency"].__setitem__("chainId", 1), "chain 1"),
        (
            lambda b: b["details"]["currencyOut"]["currency"].__setitem__("address", USDC),
            "not ETH",
        ),
        (
            lambda b: b["details"].__setitem__(
                "recipient", "0x2222222222222222222222222222222222222222"
            ),
            "not the wallet",
        ),
    ],
)
def test_normalize_quote_refuses_a_quote_for_another_order(mutate: Any, fragment: str) -> None:
    body = fixture("quote_eth_base_to_rh")
    mutate(body)
    route = bridge.resolve_route(BASE, ROBINHOOD, "ETH")

    with pytest.raises(bridge.BridgeError) as info:
        bridge.normalize_quote(body, route=route, wallet=WALLET, amount_raw=ETH_RAW)

    assert info.value.code == "trading.bridge.provider"
    assert fragment in str(info.value)


def test_extract_request_id_falls_back_through_the_shapes() -> None:
    assert bridge._extract_request_id({"requestId": "0xtop"}) == "0xtop"
    assert bridge._extract_request_id({"steps": [{"requestId": "0xstep"}]}) == "0xstep"
    assert (
        bridge._extract_request_id({"steps": [{"items": [{"data": {"requestId": "0xitem"}}]}]})
        == "0xitem"
    )
    assert bridge._extract_request_id({"steps": [{}]}) is None


# ── steps ───────────────────────────────────────────────────────────────


def quote_for(name: str, origin: Any, destination: Any, token: str, raw: int) -> Any:
    route = bridge.resolve_route(origin, destination, token)
    return bridge.normalize_quote(fixture(name), route=route, wallet=WALLET, amount_raw=raw)


def test_validate_steps_native_is_one_deposit_with_the_value_and_calldata() -> None:
    quote = quote_for("quote_eth_base_to_rh", BASE, ROBINHOOD, "ETH", ETH_RAW)

    approve, deposit = bridge.validate_steps(
        quote, route=quote.route, wallet=WALLET, amount_raw=ETH_RAW
    )

    assert approve is None
    assert deposit["to"] == SOLVER
    assert deposit["value"] == str(ETH_RAW)
    assert deposit["data"].startswith("0x49290c1c")
    assert deposit["chainId"] == 8453 and deposit["from"] == WALLET
    assert deposit["gasLimit"] == 32713


def test_validate_steps_erc20_is_approve_to_the_solver_then_deposit_without_value() -> None:
    quote = quote_for("quote_usdg_rh_to_base", ROBINHOOD, BASE, "USDG", USDC_RAW)

    approve, deposit = bridge.validate_steps(
        quote, route=quote.route, wallet=WALLET, amount_raw=USDC_RAW
    )

    assert approve is not None
    assert approve["to"] == bridge.USDG_ROBINHOOD
    assert approve["data"].startswith("0x095ea7b3")
    assert "0x" + approve["data"][10 + 24 : 10 + 64] == SOLVER
    assert approve["value"] == "0" and approve["chainId"] == 4663
    assert deposit["to"] == SOLVER and deposit["value"] == "0"
    assert deposit["data"].startswith("0xe8017952")


def test_validate_steps_skips_items_relay_marks_complete() -> None:
    body = fixture("quote_usdc_base_to_rh")
    body["steps"][0]["items"][0]["status"] = "complete"
    route = bridge.resolve_route(BASE, ROBINHOOD, "USDC")
    quote = bridge.normalize_quote(body, route=route, wallet=WALLET, amount_raw=USDC_RAW)

    approve, deposit = bridge.validate_steps(quote, route=route, wallet=WALLET, amount_raw=USDC_RAW)

    assert approve is None and deposit["to"] == SOLVER


def _steps(name: str) -> list[dict[str, Any]]:
    return copy.deepcopy(fixture(name)["steps"])


@pytest.mark.parametrize(
    ("name", "token", "raw", "mutate", "code", "fragment"),
    [
        (
            "quote_eth_base_to_rh",
            "ETH",
            ETH_RAW,
            lambda s: s[0]["items"][0]["data"].__setitem__(
                "to", "0x2222222222222222222222222222222222222222"
            ),
            "trading.tx_failed",
            "not a Relay solver",
        ),
        (
            "quote_eth_base_to_rh",
            "ETH",
            ETH_RAW,
            lambda s: s[0]["items"][0]["data"].__setitem__("value", str(ETH_RAW - 1)),
            "trading.tx_failed",
            "not the order's",
        ),
        (
            "quote_eth_base_to_rh",
            "ETH",
            ETH_RAW,
            lambda s: s[0]["items"][0]["data"].__setitem__("data", "0x"),
            "trading.bridge.unsupported_step",
            "no calldata",
        ),
        (
            "quote_eth_base_to_rh",
            "ETH",
            ETH_RAW,
            lambda s: s[0]["items"][0]["data"].__setitem__("chainId", 4663),
            "trading.bridge.unsupported_step",
            "chain 4663",
        ),
        (
            "quote_eth_base_to_rh",
            "ETH",
            ETH_RAW,
            lambda s: s[0]["items"][0]["data"].__setitem__(
                "from", "0x2222222222222222222222222222222222222222"
            ),
            "trading.bridge.unsupported_step",
            "not the wallet",
        ),
        (
            "quote_eth_base_to_rh",
            "ETH",
            ETH_RAW,
            lambda s: s[0].__setitem__("kind", "signature"),
            "trading.bridge.unsupported_step",
            '"signature" step',
        ),
        (
            "quote_eth_base_to_rh",
            "ETH",
            ETH_RAW,
            lambda s: s[0].__setitem__("id", "swap"),
            "trading.bridge.unsupported_step",
            "unknown step",
        ),
        (
            "quote_eth_base_to_rh",
            "ETH",
            ETH_RAW,
            lambda s: s.append(copy.deepcopy(s[0])),
            "trading.bridge.unsupported_step",
            "two deposits",
        ),
        (
            "quote_eth_base_to_rh",
            "ETH",
            ETH_RAW,
            lambda s: s.clear(),
            "trading.bridge.unsupported_step",
            "no deposit",
        ),
        (
            "quote_usdc_base_to_rh",
            "USDC",
            USDC_RAW,
            lambda s: s[1]["items"][0]["data"].__setitem__("value", "1"),
            "trading.tx_failed",
            "carries native value",
        ),
        (
            "quote_usdc_base_to_rh",
            "USDC",
            USDC_RAW,
            lambda s: s[0]["items"][0]["data"].__setitem__(
                "to", "0x4200000000000000000000000000000000000006"
            ),
            "trading.tx_failed",
            "not on the token",
        ),
        (
            "quote_usdc_base_to_rh",
            "USDC",
            USDC_RAW,
            # approve(OTHER, amount): a spender that is not the deposit target.
            lambda s: s[0]["items"][0]["data"].__setitem__(
                "data",
                "0x095ea7b3"
                + "2222222222222222222222222222222222222222".rjust(64, "0")
                + format(USDC_RAW, "x").rjust(64, "0"),
            ),
            "trading.tx_failed",
            "not the deposit target",
        ),
        (
            "quote_usdc_base_to_rh",
            "USDC",
            USDC_RAW,
            lambda s: s[0]["items"][0]["data"].__setitem__(
                "data",
                "0x095ea7b3" + SOLVER[2:].rjust(64, "0") + format(USDC_RAW + 1, "x").rjust(64, "0"),
            ),
            "trading.tx_failed",
            "exceeds the order",
        ),
        (
            "quote_usdc_base_to_rh",
            "USDC",
            USDC_RAW,
            lambda s: s[0]["items"][0]["data"].__setitem__("data", "0xdeadbeef"),
            "trading.tx_failed",
            "not approve(address,uint256)",
        ),
        (
            "quote_usdc_base_to_rh",
            "USDC",
            USDC_RAW,
            lambda s: s.insert(0, copy.deepcopy(s[0])),
            "trading.bridge.unsupported_step",
            "two approvals",
        ),
    ],
)
def test_validate_steps_refuses_anything_off_pattern(
    name: str, token: str, raw: int, mutate: Any, code: str, fragment: str
) -> None:
    body = fixture(name)
    mutate(body["steps"])
    route = bridge.resolve_route(BASE, ROBINHOOD, token)
    quote = bridge.normalize_quote(body, route=route, wallet=WALLET, amount_raw=raw)

    with pytest.raises(bridge.BridgeError) as info:
        bridge.validate_steps(quote, route=route, wallet=WALLET, amount_raw=raw)

    assert info.value.code == code
    assert fragment in str(info.value)


def test_validate_steps_refuses_an_approval_on_a_native_bridge() -> None:
    body = fixture("quote_eth_base_to_rh")
    body["steps"].insert(0, copy.deepcopy(fixture("quote_usdc_base_to_rh")["steps"][0]))
    route = bridge.resolve_route(BASE, ROBINHOOD, "ETH")
    quote = bridge.normalize_quote(body, route=route, wallet=WALLET, amount_raw=ETH_RAW)

    with pytest.raises(bridge.BridgeError, match="approval on a native bridge"):
        bridge.validate_steps(quote, route=route, wallet=WALLET, amount_raw=ETH_RAW)


# ── fill status ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("body", "status", "origin", "destination"),
    [
        ({"status": "waiting"}, "waiting", None, None),
        ({"status": "pending", "inTxHashes": ["0xAA"]}, "pending", "0xaa", None),
        (
            {"status": "success", "inTxHashes": ["0xAA"], "txHashes": ["0xBB"]},
            "success",
            "0xaa",
            "0xbb",
        ),
        ({"status": "failure"}, "failure", None, None),
        ({"status": "refund", "txHashes": []}, "refund", None, None),
        ({"status": "delayed"}, "unknown", None, None),
        ("not json", "unknown", None, None),
    ],
)
def test_fill_status_from_relay(
    body: Any, status: str, origin: str | None, destination: str | None
) -> None:
    fill = bridge.fill_status_from(body)

    assert fill["status"] == status
    assert fill["originTxHash"] == origin
    assert fill["destinationTxHash"] == destination


# ── client errors ───────────────────────────────────────────────────────


def test_relay_error_maps_route_refusals_and_outages() -> None:
    route = bridge.RelayClient._error({"message": "No routes found for this request"}, 400)
    assert route.code == "trading.bridge.unsupported_route"
    assert route.details["supportedRoutes"]

    outage = bridge.RelayClient._error({"message": "upstream"}, 502)
    assert outage.code == "trading.bridge.provider" and outage.retryable

    bad = bridge.RelayClient._error({"message": "amount too small", "errorCode": "AMOUNT"}, 400)
    assert bad.code == "trading.bridge.provider" and not bad.retryable
    assert bad.details["relayCode"] == "AMOUNT"
