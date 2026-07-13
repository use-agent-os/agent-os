"""Tests for the /c0-/c3 tier-hold slash commands and router.hold.* RPC."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from agentos.engine.commands import DEFAULT_REGISTRY, Surface
from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.router_control import RouterControlHoldStore

_TIERS = ("c0", "c1", "c2", "c3")


def _router_cfg(enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        enabled=enabled,
        tiers={
            "c0": {"model": "gemini-flash-lite", "provider": "openrouter"},
            "c3": {"model": "claude-opus-4-8", "provider": "openrouter"},
        },
    )


def _ctx(cfg: SimpleNamespace | None = None, store: RouterControlHoldStore | None = None):
    runner = SimpleNamespace(
        router_control_hold_store=store if store is not None else RouterControlHoldStore(),
        router_control_config=cfg if cfg is not None else _router_cfg(),
    )
    return RpcContext(conn_id="test", turn_runner=runner)


async def _dispatch(method: str, params: dict, ctx: RpcContext):
    return await get_dispatcher().dispatch("r1", method, params, ctx)


# ---------------------------------------------------------------------------
# Registry: /c0-/c3 and /auto must be visible on web + channel surfaces
# ---------------------------------------------------------------------------


def test_tier_commands_registered_for_web_chat() -> None:
    names = {cmd.name for cmd in DEFAULT_REGISTRY.for_surface(Surface.WEB_CHAT)}
    for tier in _TIERS:
        assert f"/{tier}" in names, f"/{tier} missing from web_chat catalog"
    assert "/auto" in names


def test_tier_commands_registered_for_channel() -> None:
    names = {cmd.name for cmd in DEFAULT_REGISTRY.for_surface(Surface.CHANNEL)}
    for tier in _TIERS:
        assert f"/{tier}" in names, f"/{tier} missing from channel catalog"
    assert "/auto" in names


def test_tier_command_execution_maps_to_router_hold_rpc() -> None:
    cmd = DEFAULT_REGISTRY.find("/c3")
    assert cmd is not None
    execution = cmd.execution_for(Surface.WEB_CHAT)
    assert execution is not None
    assert execution.rpc_method == "router.hold.set"

    channel_exec = cmd.execution_for(Surface.CHANNEL)
    assert channel_exec is not None
    assert channel_exec.rpc_params is not None
    params = channel_exec.rpc_params(SimpleNamespace(session_key="agent:main:main"))
    assert params == {"key": "agent:main:main", "tier": "c3"}


def test_auto_command_execution_maps_to_router_hold_clear() -> None:
    cmd = DEFAULT_REGISTRY.find("/auto")
    assert cmd is not None
    execution = cmd.execution_for(Surface.WEB_CHAT)
    assert execution is not None
    assert execution.rpc_method == "router.hold.clear"


# ---------------------------------------------------------------------------
# RPC: router.hold.set
# ---------------------------------------------------------------------------


def test_router_hold_set_pins_tier() -> None:
    store = RouterControlHoldStore()
    ctx = _ctx(store=store)

    params = {"key": "agent:main:main", "tier": "c3"}
    result = asyncio.run(_dispatch("router.hold.set", params, ctx))

    assert result.error is None, result.error
    assert result.payload is not None
    assert result.payload["tier"] == "c3"
    assert result.payload["model"] == "claude-opus-4-8"

    hold = store.get_valid("agent:main:main")
    assert hold is not None
    assert hold.tier == "c3"
    assert hold.target_id == "tier:c3"


def test_router_hold_set_rejects_unconfigured_tier() -> None:
    ctx = _ctx()

    params = {"key": "agent:main:main", "tier": "c9"}
    result = asyncio.run(_dispatch("router.hold.set", params, ctx))

    assert result.error is not None


def test_router_hold_set_rejects_disabled_router() -> None:
    ctx = _ctx(cfg=_router_cfg(enabled=False))

    params = {"key": "agent:main:main", "tier": "c3"}
    result = asyncio.run(_dispatch("router.hold.set", params, ctx))

    assert result.error is not None


def test_router_hold_set_requires_tier_param() -> None:
    ctx = _ctx()

    result = asyncio.run(_dispatch("router.hold.set", {"key": "agent:main:main"}, ctx))

    assert result.error is not None


# ---------------------------------------------------------------------------
# RPC: router.hold.clear
# ---------------------------------------------------------------------------


def test_router_hold_clear_removes_hold() -> None:
    store = RouterControlHoldStore()
    ctx = _ctx(store=store)
    asyncio.run(_dispatch("router.hold.set", {"key": "agent:main:main", "tier": "c3"}, ctx))
    assert store.get_valid("agent:main:main") is not None

    result = asyncio.run(_dispatch("router.hold.clear", {"key": "agent:main:main"}, ctx))

    assert result.error is None, result.error
    assert result.payload is not None
    assert result.payload["cleared"] is True
    assert store.get_valid("agent:main:main") is None


def test_router_hold_clear_without_hold_reports_not_cleared() -> None:
    ctx = _ctx()

    result = asyncio.run(_dispatch("router.hold.clear", {"key": "agent:main:main"}, ctx))

    assert result.error is None, result.error
    assert result.payload is not None
    assert result.payload["cleared"] is False
