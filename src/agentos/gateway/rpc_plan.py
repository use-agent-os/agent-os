"""RPC handlers for session plan mode.

Backs the /plan slash command, the chat composer's plan pill, and the plan
card's Approve button. The store is process memory (``PlanModeStore``), so
``plan.mode.get`` exists for reconnecting clients to read the flag back
rather than guessing.
"""

from __future__ import annotations

from typing import Any

from agentos.gateway.access import CONTROL_AND_CHANNEL, CONTROL_ONLY
from agentos.gateway.agent_surface import agent_binding
from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.gateway.rpc.registry import RpcHandlerError
from agentos.plan_mode import get_plan_mode_store
from agentos.session.keys import canonicalize_session_key

_d = get_dispatcher()

#: Bindings the gateway positively identifies as an agent's: a shell or cron
#: script presenting its minted agent token, or a connection opened while an
#: agent shell runs. ``unbound`` is not refused here: the local web console
#: has no operator secret and is bound that way, and its plan card is how a
#: plan is approved.
_AGENT_VIAS = frozenset({"token", "window"})


def _require_key(params: dict | None) -> str:
    if not isinstance(params, dict) or "key" not in params:
        raise ValueError("params.key is required")
    key = params["key"]
    if not isinstance(key, str) or not key.strip():
        raise ValueError("params.key must be a non-empty string")
    return canonicalize_session_key(key)


def _payload(key: str) -> dict[str, Any]:
    return {"key": key, "planMode": get_plan_mode_store().is_enabled(key)}


@_d.method("plan.mode.set", CONTROL_AND_CHANNEL)
async def _handle_plan_mode_set(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Turn plan mode on or off for a session.

    ``mode`` accepts "on"/"off". Turning it off is also how a plan gets
    approved: the plan card's Approve button and ``/plan off`` both land
    here — approval is out of band by design, never an in-band model call,
    so a connection bound to an agent may turn it on but never off.
    """
    key = _require_key(params)
    mode = str((params or {}).get("mode") or "").strip().lower()
    if mode not in {"on", "off"}:
        raise ValueError("params.mode must be 'on' or 'off'")
    binding = agent_binding(ctx)
    if mode == "off" and binding is not None and binding.via in _AGENT_VIAS:
        raise RpcHandlerError(
            "plan.operator_required",
            "Plan mode can only be turned off by the operator, not by an agent.",
            details=binding.to_dict(),
        )
    store = get_plan_mode_store()
    if mode == "on":
        store.enable(key)
    else:
        store.disable(key)
    return _payload(key)


@_d.method("plan.mode.get", CONTROL_ONLY)
async def _handle_plan_mode_get(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    return _payload(_require_key(params))
