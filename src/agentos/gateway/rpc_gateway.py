"""``gateway.restart``: restart the running gateway in place.

The handler returns as soon as the restart is scheduled so the caller can
acknowledge; the work itself happens in :mod:`agentos.gateway.restart`.

Who may call it: a Control client (the web UI, the desktop app, the CLI)
always — it already holds ``updates.apply`` and ``config.patch``, which are
strictly more powerful. A channel sender, or a connection bound to an agent,
only when ``[gateway_control] chat_restart`` is on: those are the callers a
paired chat identity or the model itself can be.
"""

from __future__ import annotations

from typing import Any

from agentos import __version__
from agentos.gateway.access import CONTROL_AND_CHANNEL, ConnectionSurface
from agentos.gateway.restart import (
    RestartOrigin,
    RestartPendingError,
    RestartUnsupportedError,
    drain_timeout_for,
    get_restarter,
    normalize_mode,
)
from agentos.gateway.rpc import RpcContext, RpcHandlerError, RpcUnavailableError, get_dispatcher

_d = get_dispatcher()

_REASON_MAX_CHARS = 200

CHAT_RESTART_DISABLED_MESSAGE = (
    "Gateway restart from chat is disabled (set [gateway_control] chat_restart = true "
    "and restart the gateway once)."
)


def chat_restart_enabled(config: Any) -> bool:
    control = getattr(config, "gateway_control", None)
    return bool(getattr(control, "chat_restart", False))


def is_chat_caller(ctx: RpcContext) -> bool:
    """A paired channel sender, or a connection the gateway bound to an agent."""
    access = ctx.access
    if access.surface is ConnectionSurface.CHANNEL:
        return True
    return getattr(access, "agent", None) is not None


@_d.method("gateway.restart", audiences=CONTROL_AND_CHANNEL)
async def _handle_gateway_restart(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Schedule an in-place restart of this gateway.

    Params:
        mode: ``safe`` (default; drain active turns) or ``force`` (cancel them).
        reason: short free text recorded in the log and the request file.
        key: the caller's session key, so the confirmation after boot lands
            in that web chat. Channel callers are located from their envelope.

    Returns ``accepted: true`` with the restart id, or ``accepted: false,
    alreadyPending: true`` when one is underway. ``RESTART_UNSUPPORTED`` on
    Windows; ``UNAUTHORIZED`` for a chat caller while the toggle is off.
    """
    raw = params or {}
    if not isinstance(raw, dict):
        raise ValueError("params must be an object")
    mode = normalize_mode(raw.get("mode"))
    reason = str(raw.get("reason") or "rpc").strip()[:_REASON_MAX_CHARS] or "rpc"
    key = raw.get("key")
    if key is not None and not isinstance(key, str):
        raise ValueError("key must be a string")

    if is_chat_caller(ctx) and not chat_restart_enabled(ctx.config):
        raise RpcHandlerError("UNAUTHORIZED", CHAT_RESTART_DISABLED_MESSAGE)

    restarter = get_restarter()
    if restarter is None:
        raise RpcUnavailableError("gateway restart is not wired in this runtime")

    envelope = ctx.originating_envelope
    if envelope is not None:
        origin = RestartOrigin.from_envelope(envelope)
    else:
        origin = RestartOrigin.web(key, ctx.conn_id)

    try:
        req = restarter.request(reason=reason, mode=mode, origin=origin)
    except RestartPendingError:
        pending = restarter.pending
        return {
            "accepted": False,
            "alreadyPending": True,
            "restartId": pending.restart_id if pending else None,
            "mode": pending.mode if pending else None,
        }
    except RestartUnsupportedError as exc:
        raise RpcHandlerError("RESTART_UNSUPPORTED", str(exc)) from exc

    return {
        "accepted": True,
        "restartId": req.restart_id,
        "mode": req.mode,
        "drainTimeoutSeconds": drain_timeout_for(req.mode),
        "pid": req.pid,
        "version": __version__,
    }


@_d.method("gateway.status")
async def _handle_gateway_status(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """The supervisor contract's live numbers: state, drain budget, active turns.

    The same ``state``/``failing``/``safe_mode`` as ``GET /ready``, plus what
    only an authenticated caller should see: how many turns are running
    right now (what a stop would have to drain).
    """
    import os

    from agentos.gateway.readiness import drain_budget_ms, get_gateway_readiness

    readiness = get_gateway_readiness()
    restarter = get_restarter()
    pending = restarter.pending if restarter is not None else None
    if readiness is None:
        return {
            "state": "ready",
            "ready": True,
            "failing": [],
            "safe_mode": {"active": False},
            "drain_budget_ms": drain_budget_ms(),
            "active_turns": _active_turns(ctx),
            "pid": os.getpid(),
            "version": __version__,
            "restart_pending": pending is not None,
        }
    payload = readiness.snapshot(gateway_ready=False if pending is not None else None)
    active = readiness.active_turns()
    payload.update(
        {
            "active_turns": active if active is not None else _active_turns(ctx),
            "pid": os.getpid(),
            "version": __version__,
            "restart_pending": pending is not None,
            "restart_expected_ms": readiness.restart_expected_ms(),
        }
    )
    return payload


def _active_turns(ctx: RpcContext) -> int | None:
    runtime = getattr(ctx, "task_runtime", None)
    value = getattr(runtime, "active_turns", None)
    return int(value) if isinstance(value, int) else None
