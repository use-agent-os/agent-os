"""Diagnostics-mode RPC handlers."""

from __future__ import annotations

from typing import Any

from agentos.gateway.diagnostics import (
    DiagnosticsState,
    diagnostics_status_payload,
)
from agentos.gateway.rpc import RpcContext, get_dispatcher

_d = get_dispatcher()


def _state(ctx: RpcContext) -> DiagnosticsState:
    state = getattr(ctx, "diagnostics_state", None)
    if isinstance(state, DiagnosticsState):
        return state
    state = DiagnosticsState.from_config(getattr(ctx, "config", None))
    ctx.diagnostics_state = state
    return state


@_d.method("diagnostics.status")
async def _handle_diagnostics_status(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    return diagnostics_status_payload(_state(ctx), getattr(ctx, "config", None))


@_d.method("diagnostics.set")
async def _handle_diagnostics_set(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    state = _state(ctx)
    if params.get("reset"):
        state.reset_runtime()
        return diagnostics_status_payload(state, getattr(ctx, "config", None))
    if "enabled" not in params:
        raise ValueError("params.enabled is required")
    enabled = bool(params.get("enabled"))
    raw = bool(params.get("raw", False))
    state.set_runtime(enabled=enabled, raw=raw)
    return diagnostics_status_payload(state, getattr(ctx, "config", None))
