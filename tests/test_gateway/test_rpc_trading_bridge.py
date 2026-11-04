"""``trading.bridge.quote|create|status|routes``: an agent may quote and create, never approve.

Runs through the real dispatcher and service with Relay answered from the
recorded quotes (``tests/test_trading/fakes.py``, ``FakeRelay``).
"""

from __future__ import annotations

from typing import Any

from agentos.gateway.rpc import RpcContext, get_dispatcher
from tests.test_gateway.test_rpc_trading import _agent_ctx, call, ctx, stack  # noqa: F401
from tests.test_trading.conftest import PASSWORD
from tests.test_trading.fakes import RELAY_SOLVER, USDC, USDG

METHODS = (
    "trading.bridge.quote",
    "trading.bridge.create",
    "trading.bridge.status",
    "trading.bridge.routes",
)


async def _ready(ctx: RpcContext, stack: dict[str, Any]) -> str:  # noqa: F811
    await call("wallet.setup", {"password": PASSWORD}, ctx)
    address = (await call("wallet.create", {"label": "Main"}, ctx)).payload["wallet"]["address"]
    wallet = address.lower()
    stack["base"].set_native(wallet, 10**18)
    stack["base"].set_erc20(USDC, wallet, 1_000 * 10**6)
    stack["robinhood"].tokens[USDG] = ("USDG", "Global Dollar", 6)
    return wallet


def test_bridge_methods_are_registered() -> None:
    assert set(METHODS) <= set(get_dispatcher().methods())


async def test_routes_lists_both_directions(ctx: RpcContext) -> None:  # noqa: F811
    res = await call("trading.bridge.routes", {}, ctx)
    assert res.ok, res.error
    routes = {(r["from"], r["to"], r["tokenIn"], r["tokenOut"]) for r in res.payload["routes"]}
    assert routes == {
        ("base", "robinhood", "ETH", "ETH"),
        ("base", "robinhood", "USDC", "USDG"),
        ("robinhood", "base", "ETH", "ETH"),
        ("robinhood", "base", "USDG", "USDC"),
    }


async def test_quote_takes_chain_names_and_one_size(
    ctx: RpcContext,  # noqa: F811
    stack: dict[str, Any],  # noqa: F811
) -> None:
    await _ready(ctx, stack)
    res = await call(
        "trading.bridge.quote",
        {"chain": "base", "toChain": "robinhood", "token": "USDC", "amount": "25"},
        ctx,
    )
    assert res.ok, res.error
    quote = res.payload["quote"]
    assert quote["tokenOut"]["symbol"] == "USDG" and quote["amountIn"] == "25"
    assert quote["guard"]["decision"] == "allow"
    sent = stack["relay"].calls[-1]["body"]
    assert sent["originCurrency"] == USDC and sent["destinationCurrency"] == USDG

    by_usd = await call(
        "trading.bridge.quote",
        {"chainId": 8453, "toChainId": 4663, "token": "ETH", "amountUsd": 20},
        ctx,
    )
    assert by_usd.ok, by_usd.error
    assert by_usd.payload["quote"]["amountIn"] == "0.01"

    by_pct = await call(
        "trading.bridge.quote",
        {"chainId": 8453, "toChainId": 4663, "token": "USDC", "amountPct": 10},
        ctx,
    )
    assert by_pct.ok, by_pct.error
    assert by_pct.payload["quote"]["amountIn"] == "100"


async def test_param_validation(
    ctx: RpcContext,  # noqa: F811
    stack: dict[str, Any],  # noqa: F811
) -> None:
    await _ready(ctx, stack)
    missing = await call("trading.bridge.quote", {"chainId": 8453, "token": "ETH"}, ctx)
    assert missing.ok is False and "toChainId" in missing.error.message
    two = await call(
        "trading.bridge.quote",
        {"chainId": 8453, "toChainId": 4663, "token": "ETH", "amount": "1", "amountUsd": 5},
        ctx,
    )
    assert two.ok is False and "exactly one" in two.error.message
    none = await call(
        "trading.bridge.quote", {"chainId": 8453, "toChainId": 4663, "token": "ETH"}, ctx
    )
    assert none.ok is False and "exactly one" in none.error.message
    same = await call(
        "trading.bridge.quote",
        {"chainId": 8453, "toChainId": 8453, "token": "ETH", "amount": "0.01"},
        ctx,
    )
    assert same.ok is False and same.error.code == "trading.bridge.unsupported_route"
    assert same.error.details["supportedRoutes"]
    off = await call(
        "trading.bridge.quote",
        {"chainId": 8453, "toChainId": 4663, "token": "WETH", "amount": "1"},
        ctx,
    )
    assert off.ok is False and off.error.code == "trading.bridge.unsupported_route"
    bad_chain = await call(
        "trading.bridge.quote",
        {"chainId": 8453, "toChainId": 1, "token": "ETH", "amount": "0.01"},
        ctx,
    )
    assert bad_chain.ok is False and bad_chain.error.code == "trading.unsupported_chain"
    status = await call("trading.bridge.status", {}, ctx)
    assert status.ok is False and "orderId or params.requestId" in status.error.message


async def test_agent_creates_but_only_the_operator_approves(
    ctx: RpcContext,  # noqa: F811
    stack: dict[str, Any],  # noqa: F811
) -> None:
    await _ready(ctx, stack)
    agent = _agent_ctx(stack, "agent:trading:desk:1")
    res = await call(
        "trading.bridge.create",
        {
            "chainId": 8453,
            "toChainId": 4663,
            "token": "ETH",
            "amount": "0.01",
            "note": "user: bridge",
            "clientOrderId": "br-1",
        },
        agent,
    )
    assert res.ok, res.error
    order = res.payload["order"]
    assert order["kind"] == "bridge" and order["status"] == "awaiting_approval"
    assert order["initiator"] == "agent" and order["sessionKey"] == "agent:trading:desk:1"
    assert order["clientOrderId"] == "br-1" and order["note"] == "user: bridge"
    assert order["recipient"].lower() == RELAY_SOLVER
    assert order["bridge"]["destinationChainId"] == 4663 and order["bridge"]["requestId"]

    denied = await call("trading.orders.approve", {"orderId": order["orderId"]}, agent)
    assert denied.ok is False and denied.error.code == "trading.operator_required"

    status = await call("trading.bridge.status", {"requestId": order["bridge"]["requestId"]}, agent)
    assert status.ok, status.error
    assert status.payload["order"]["orderId"] == order["orderId"]
    assert status.payload["bridge"]["fillStatus"] is None

    listed = await call("trading.orders.list", {"kind": "bridge"}, ctx)
    assert listed.ok and [o["orderId"] for o in listed.payload["orders"]] == [order["orderId"]]

    again = await call(
        "trading.bridge.create",
        {
            "chainId": 8453,
            "toChainId": 4663,
            "token": "ETH",
            "amount": "0.01",
            "clientOrderId": "br-1",
        },
        agent,
    )
    assert again.ok and again.payload["order"]["orderId"] == order["orderId"]

    unknown = await call("trading.bridge.status", {"orderId": "ord_nope"}, ctx)
    assert unknown.ok is False and unknown.error.code == "trading.bridge.not_found"
