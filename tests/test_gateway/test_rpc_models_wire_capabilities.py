"""``models.list`` wire ``capabilities`` must reflect every ModelInfo flag.

The CLI exposes a free-form ``--capability`` filter, so a flag the wire mapper
forgets is a filter that can never match: ``agentos models list -c reasoning``
returned ``[]`` while the catalog carried ``supports_reasoning=True`` (#1707).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.gateway.rpc_models import _model_info_to_wire
from agentos.provider.model_catalog import ModelInfo


def _wire(**flags: bool) -> list[str]:
    row = {"model_id": "m", "display_name": "m", "provider": "p", **flags}
    return _model_info_to_wire(row)["capabilities"]


def test_reasoning_flag_maps_to_the_reasoning_capability() -> None:
    assert _wire(supports_reasoning=True) == ["chat", "reasoning"]
    assert _wire(supports_reasoning=False) == ["chat"]
    assert _wire() == ["chat"]


def test_capability_order_is_stable_across_all_flags() -> None:
    assert _wire(supports_tools=True, supports_vision=True, supports_reasoning=True) == [
        "chat",
        "tools",
        "vision",
        "reasoning",
    ]


def test_model_info_dump_round_trips_reasoning() -> None:
    """The real ``ModelInfo.model_dump()`` shape, not a hand-written dict."""
    info = ModelInfo(
        model_id="deepseek-r1",
        provider="deepseek",
        supports_reasoning=True,
        supports_tools=True,
    )

    assert _model_info_to_wire(info.model_dump())["capabilities"] == ["chat", "tools", "reasoning"]


def _ctx(rows: list[dict]) -> RpcContext:
    ctx = RpcContext(conn_id="test")
    ctx.config = SimpleNamespace(llm=SimpleNamespace(provider="deepseek"))
    ctx.model_catalog = SimpleNamespace(
        list_models=lambda: [SimpleNamespace(model_dump=lambda row=row: row) for row in rows]
    )
    return ctx


def test_models_list_filter_by_reasoning_finds_the_reasoning_model() -> None:
    ctx = _ctx(
        [
            {"model_id": "deepseek-chat", "provider": "deepseek", "supports_reasoning": False},
            {"model_id": "deepseek-r1", "provider": "deepseek", "supports_reasoning": True},
        ]
    )

    result = asyncio.run(
        get_dispatcher().dispatch("r1", "models.list", {"capabilities": ["reasoning"]}, ctx)
    )

    assert result.error is None, result.error
    assert [m["id"] for m in result.payload] == ["deepseek-r1"]
    assert result.payload[0]["capabilities"] == ["chat", "reasoning"]
