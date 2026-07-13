"""RPC handlers for user-directed AgentOS Router tier holds.

Backs the /c0-/c3 and /auto slash commands: a session-scoped, short-lived
tier hold set through the same ``RouterControlHoldStore`` the LLM-facing
``router_control`` tool uses, so both paths share expiry and precedence
semantics inside the router step.
"""

from __future__ import annotations

from typing import Any

from agentos.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from agentos.router_control import (
    RouterControlHoldStore,
    RouterControlValidationError,
    resolve_router_control_target,
)
from agentos.session.keys import canonicalize_session_key

_d = get_dispatcher()


def _require_key(params: dict | None) -> str:
    if not isinstance(params, dict) or "key" not in params:
        raise ValueError("params.key is required")
    key = params["key"]
    if not isinstance(key, str):
        raise ValueError("params.key must be a string")
    return canonicalize_session_key(key)


def _router_state(ctx: RpcContext) -> tuple[Any, RouterControlHoldStore]:
    runner = ctx.turn_runner
    store = getattr(runner, "router_control_hold_store", None)
    if not isinstance(store, RouterControlHoldStore):
        raise RpcHandlerError(
            "router.unavailable",
            "Router hold store is unavailable on this gateway",
        )
    cfg = getattr(runner, "router_control_config", None)
    if cfg is None or not getattr(cfg, "enabled", False):
        raise RpcHandlerError(
            "router.disabled",
            "AgentOS Router is disabled or unavailable",
        )
    return cfg, store


@_d.method("router.hold.set", scope="operator.write")
async def _handle_router_hold_set(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    key = _require_key(params)
    tier = str((params or {}).get("tier") or "").strip().lower()
    if not tier:
        raise ValueError("params.tier is required")

    cfg, store = _router_state(ctx)
    try:
        target = resolve_router_control_target(cfg, f"tier:{tier}")
    except RouterControlValidationError as exc:
        raise RpcHandlerError(
            "router.unknown_tier",
            f"Tier '{tier}' is not configured on the AgentOS Router",
            details={"tier": tier},
        ) from exc

    hold = store.set_hold(key, target, evidence=f"slash command /{tier}")
    return {
        "tier": hold.tier,
        "model": hold.model,
        "provider": hold.provider,
        "targetId": hold.target_id,
        "ttlSeconds": hold.ttl_seconds,
    }


@_d.method("router.hold.clear", scope="operator.write")
async def _handle_router_hold_clear(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    key = _require_key(params)
    _cfg, store = _router_state(ctx)
    cleared = store.clear(key)
    return {"cleared": cleared is not None}
