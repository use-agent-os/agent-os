"""What the model catalog offers, and therefore what can be pinned.

OpenCAP's INFERENCE endpoint serves each model twice — under a bare canonical id
and under namespaced ``<upstream>/<model>`` aliases — but the PUBLIC catalog the
gateway reads (``/api/public/models``) publishes only the bare ids. So aliases
never reach ``models.list`` and are not pinnable, and no alias-stripping filter
is needed or wanted: on OpenRouter every id is ``vendor/model``, and one applied
there would empty the list.

These tests pin that contract down, because it is the reason `router.hold.set`
can validate against the catalog at all.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.gateway.rpc_models import _model_info_to_wire
from agentos.router_control import RouterControlHoldStore

_OPENCAP_ROWS = [
    {"id": "claude-opus-5", "provider": "opencap"},
    {"id": "gpt-5.6-terra", "provider": "opencap"},
]


def _ctx(provider: str = "opencap", rows: list[dict] | None = None) -> RpcContext:
    ctx = RpcContext(conn_id="test")
    ctx.config = SimpleNamespace(llm=SimpleNamespace(provider=provider))
    catalog_rows = _OPENCAP_ROWS if rows is None else rows
    ctx.model_catalog = SimpleNamespace(
        list_models=lambda: [
            SimpleNamespace(
                model_dump=lambda row=row: {
                    "model_id": row["id"],
                    "display_name": row["id"],
                    "provider": row["provider"],
                    **{k: v for k, v in row.items() if k not in {"id", "provider"}},
                }
            )
            for row in catalog_rows
        ]
    )
    return ctx


def _list(params: dict, ctx: RpcContext) -> list[dict]:
    result = asyncio.run(get_dispatcher().dispatch("r1", "models.list", params, ctx))
    assert result.error is None, result.error
    return result.payload or []


def _with_router(ctx: RpcContext) -> RpcContext:
    ctx.turn_runner = SimpleNamespace(
        router_control_hold_store=RouterControlHoldStore(),
        router_control_config=SimpleNamespace(
            enabled=True,
            default_tier="c1",
            tiers={"c1": {"model": "gpt-5.6-terra", "provider": "opencap"}},
        ),
    )
    return ctx


def test_models_list_returns_the_catalog_verbatim() -> None:
    """No id-shape filtering: whatever the provider publishes is what is offered."""

    ids = {m["id"] for m in _list({}, _ctx())}

    assert ids == {"claude-opus-5", "gpt-5.6-terra"}


def test_namespaced_ids_survive_for_providers_that_use_them() -> None:
    """On OpenRouter every id is `vendor/model`; stripping them would empty the list."""

    rows = [
        {"id": "anthropic/claude-opus-5", "provider": "openrouter"},
        {"id": "openai/gpt-5.6-luna", "provider": "openrouter"},
    ]
    ids = {m["id"] for m in _list({}, _ctx("openrouter", rows))}

    assert ids == {"anthropic/claude-opus-5", "openai/gpt-5.6-luna"}


def test_a_catalog_model_can_be_pinned() -> None:
    ctx = _with_router(_ctx())

    result = asyncio.run(
        get_dispatcher().dispatch(
            "r2",
            "router.hold.set",
            {"key": "agent:main:main", "model": "claude-opus-5"},
            ctx,
        )
    )

    assert result.error is None, result.error
    assert result.payload is not None
    assert result.payload["model"] == "claude-opus-5"


def test_a_model_outside_the_catalog_cannot_be_pinned() -> None:
    """Covers OpenCAP's inference-only aliases as well as plain typos.

    Verified against the live gateway: `/use openrouter/gpt-5.4` is refused with
    `router.unknown_model` because the public catalog does not list it.
    """

    ctx = _with_router(_ctx())

    result = asyncio.run(
        get_dispatcher().dispatch(
            "r3",
            "router.hold.set",
            {"key": "agent:main:main", "model": "openrouter/gpt-5.4"},
            ctx,
        )
    )

    assert result.error is not None


def test_model_info_to_wire_includes_reasoning() -> None:
    wire = _model_info_to_wire({"supports_reasoning": True, "supports_tools": True})
    assert wire["capabilities"] == ["chat", "tools", "reasoning"]

    wire_no_reasoning = _model_info_to_wire({"supports_tools": True})
    assert wire_no_reasoning["capabilities"] == ["chat", "tools"]


def test_models_list_includes_reasoning_capability_and_filters() -> None:
    rows = [
        {"id": "deepseek/deepseek-r1", "provider": "openrouter", "supports_reasoning": True},
        {"id": "anthropic/claude-3.5-sonnet", "provider": "openrouter", "supports_tools": True},
    ]
    ctx = _ctx("openrouter", rows)
    all_models = _list({}, ctx)
    assert len(all_models) == 2
    r1 = next(m for m in all_models if m["id"] == "deepseek/deepseek-r1")
    assert "reasoning" in r1["capabilities"]
    assert "chat" in r1["capabilities"]

    filtered = _list({"capabilities": ["reasoning"]}, ctx)
    assert len(filtered) == 1
    assert filtered[0]["id"] == "deepseek/deepseek-r1"

