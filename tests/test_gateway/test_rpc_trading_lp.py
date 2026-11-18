"""``trading.lp.*``: registered, read-only on the agent surface, params reach the engine."""

from __future__ import annotations

from typing import Any

import pytest

from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.trading import lp
from tests.test_gateway.test_rpc_trading import _agent_ctx, call, ctx, stack  # noqa: F401

METHODS = ("trading.lp.pool", "trading.lp.ranges", "trading.lp.position", "trading.lp.positions")


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake(kind: str):
        async def run(service: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append((kind, kwargs))
            return {"version": 1, "kind": kind}

        return run

    for kind in ("pool", "ranges", "position", "positions"):
        monkeypatch.setattr(lp, f"lp_{kind}", fake(kind))
    return calls


def test_methods_are_registered() -> None:
    assert set(METHODS) <= set(get_dispatcher().methods())


async def test_agent_may_read_every_lp_method(
    stack: dict[str, Any],  # noqa: F811
    seen: list[tuple[str, dict[str, Any]]],
) -> None:
    agent = _agent_ctx(stack)
    for method, params in [
        ("trading.lp.pool", {"chainId": 8453, "target": "PEPE", "quote": "WETH"}),
        ("trading.lp.ranges", {"target": "0x" + "ab" * 32}),
        ("trading.lp.position", {"chainId": "robinhood", "tokenId": "48213"}),
        ("trading.lp.positions", {"wallets": ["0x" + "11" * 20], "all": True}),
    ]:
        res = await call(method, params, agent)
        assert res.ok, (method, res.error)
        assert res.payload["kind"] == method.rsplit(".", 1)[1]
    pool, ranges, position, positions = (kwargs for _, kwargs in seen)
    assert pool["chain"].chain_id == 8453 and pool["target"] == "PEPE" and pool["quote"] == "WETH"
    assert ranges["chain"] is None and ranges["target"] == "0x" + "ab" * 32
    assert position["chain"].chain_id == 4663 and position["token_id"] == 48213
    assert positions == {
        "chains": None,
        "wallets": ["0x" + "11" * 20],
        "include_closed": True,
        "budget_s": None,
    }


async def test_param_validation(
    ctx: RpcContext,  # noqa: F811
    seen: list[tuple[str, dict[str, Any]]],
) -> None:
    for method, params in [
        ("trading.lp.pool", {}),
        ("trading.lp.position", {"tokenId": "1"}),
        ("trading.lp.position", {"chainId": 8453, "tokenId": "abc"}),
        ("trading.lp.position", {"chainId": 8453, "tokenId": 0}),
        ("trading.lp.positions", {"wallets": "0x11"}),
        ("trading.lp.positions", {"all": "yes"}),
        ("trading.lp.positions", {"chainIds": "base"}),
        ("trading.lp.positions", {"chainIds": []}),
        ("trading.lp.positions", {"chainIds": [8453, 1]}),
        ("trading.lp.positions", {"chainId": 8453, "chainIds": [4663]}),
        ("trading.lp.positions", {"budgetSeconds": "fast"}),
        ("trading.lp.ranges", {"chainId": 1, "target": "PEPE"}),
    ]:
        res = await call(method, params, ctx)
        assert res.ok is False, (method, params)
    assert seen == []


async def test_engine_errors_keep_their_code(
    ctx: RpcContext,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentos.trading.service import TradingError

    async def missing(service: Any, **kwargs: Any) -> dict[str, Any]:
        raise TradingError("trading.lp.not_found", "no Uniswap V4 pool holds it")

    monkeypatch.setattr(lp, "lp_pool", missing)
    res = await call("trading.lp.pool", {"chainId": 8453, "target": "0x" + "12" * 20}, ctx)
    assert res.ok is False and res.error.code == "trading.lp.not_found"


async def test_positions_take_several_chains_and_a_budget(
    ctx: RpcContext,  # noqa: F811
    seen: list[tuple[str, dict[str, Any]]],
) -> None:
    res = await call(
        "trading.lp.positions",
        {"chainIds": [8453, "robinhood", 8453], "budgetSeconds": 40},
        ctx,
    )
    assert res.ok, res.error
    ((_, kwargs),) = seen
    assert [c.key for c in kwargs["chains"]] == ["base", "robinhood"]
    assert kwargs["budget_s"] == 40.0


async def test_pool_and_ranges_take_a_fee_tier(
    ctx: RpcContext,  # noqa: F811
    seen: list[tuple[str, dict[str, Any]]],
) -> None:
    res = await call(
        "trading.lp.pool", {"chainId": 8453, "target": "ETH/USDC", "feePct": "0.05"}, ctx
    )
    assert res.ok, res.error
    res = await call(
        "trading.lp.ranges",
        {"chainId": 8453, "target": "ETH", "quote": "USDC", "feePct": 3000},
        ctx,
    )
    assert res.ok, res.error
    (_, pool), (_, ranges) = seen
    assert pool["target"] == "ETH/USDC" and pool["quote"] is None and pool["fee"] == 500
    assert ranges["quote"] == "USDC" and ranges["fee"] == 3000
    # A tier that is not one is refused before the engine is asked.
    res = await call("trading.lp.pool", {"target": "ETH", "feePct": "fast"}, ctx)
    assert res.ok is False and res.error.code == "trading.invalid"
    assert len(seen) == 2


async def test_the_echoed_request_keeps_the_fee(
    ctx: RpcContext,  # noqa: F811
    seen: list[tuple[str, dict[str, Any]]],
) -> None:
    res = await call("trading.lp.pool", {"chainId": 8453, "target": "ETH", "feePct": "500"}, ctx)
    assert res.ok, res.error
    assert res.payload["request"]["params"] == {
        "target": "ETH",
        "chainId": 8453,
        "feePct": "0.05%",
    }


async def test_book_reads_the_cache_and_validates_its_params(
    stack: dict[str, Any],  # noqa: F811
    ctx: RpcContext,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``trading.lp.book``: registered, readable by the agent, params reach the book."""
    from agentos.trading import lp_book

    asked: list[dict[str, Any]] = []

    async def read(self: Any, **kwargs: Any) -> dict[str, Any]:
        asked.append(kwargs)
        return {"version": 1, "kind": "book", "positions": []}

    monkeypatch.setattr(lp_book.LpBook, "read", read)
    assert "trading.lp.book" in set(get_dispatcher().methods())
    res = await call("trading.lp.book", {}, _agent_ctx(stack))
    assert res.ok, res.error
    assert res.payload["kind"] == "book"
    res = await call(
        "trading.lp.book", {"wallet": "Main", "chainId": "robinhood", "refresh": True}, ctx
    )
    assert res.ok, res.error
    assert asked[0] == {"wallet": None, "chain": None, "refresh": False}
    assert asked[1]["wallet"] == "Main" and asked[1]["chain"].chain_id == 4663
    assert asked[1]["refresh"] is True
    for params in ({"refresh": "yes"}, {"chainId": 1}):
        res = await call("trading.lp.book", params, ctx)
        assert res.ok is False, params
    assert len(asked) == 2
