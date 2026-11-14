"""Approvals domain RPC handlers backed by ApprovalQueue."""

from __future__ import annotations

from typing import Any

from agentos.application.approval_events import approval_display_item
from agentos.application.approval_queue import get_approval_queue
from agentos.application.approval_rpc import (
    approval_forget_rpc_payload,
    approval_pending_rpc_payload,
    approval_request_rpc_payload,
    approval_resolve_rpc_payload,
    approval_settings_rpc_payload,
    approval_snapshot_rpc_payload,
    approval_wait_decision_rpc_payload,
)
from agentos.gateway.agent_surface import recognized_agent
from agentos.gateway.idempotency import run_idempotent
from agentos.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher

_d = get_dispatcher()


def require_human(ctx: RpcContext, action: str) -> None:
    """Refuse a connection the gateway recognizes as an agent's.

    Approving a prompt, changing how prompts are answered and forgetting
    remembered answers are the user's decisions. A connection that carries an
    agent token, comes from a process the gateway spawned, or opened while an
    agent shell was running is refused, as ``tools.invoke`` refuses it.
    ``unbound`` is not: the user's own web console on loopback is unbound.
    """
    binding = recognized_agent(ctx)
    if binding is not None:
        raise RpcHandlerError(
            "approvals.operator_required",
            f"{action} is the user's decision; an agent cannot make it. "
            "Ask the user to answer it in the app (an agent's shell, or a "
            "console opened while one was running, is refused).",
            details=binding.to_dict(),
        )


@_d.method("exec.approvals.get")
async def _handle_exec_approvals_get(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    queue = get_approval_queue()
    return approval_settings_rpc_payload(queue.get_settings())


@_d.method("exec.approvals.set")
async def _handle_exec_approvals_set(params: dict | None, ctx: RpcContext) -> None:
    require_human(ctx, "Changing the approval mode")
    if not isinstance(params, dict) or "mode" not in params:
        raise ValueError("params.mode is required")
    queue = get_approval_queue()
    queue.set_settings(
        mode=params["mode"],
        allow_patterns=params.get("allowPatterns"),
        deny_patterns=params.get("denyPatterns"),
    )
    return None


@_d.method("exec.approvals.node.get")
async def _handle_exec_approvals_node_get(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict) or "nodeId" not in params:
        raise ValueError("params.nodeId is required")
    queue = get_approval_queue()
    node_id = params["nodeId"]
    return approval_settings_rpc_payload(
        queue.get_settings(node_id=node_id),
        node_id=node_id,
        inherited=not queue.has_node_settings(node_id),
    )


@_d.method("exec.approvals.node.set")
async def _handle_exec_approvals_node_set(params: dict | None, ctx: RpcContext) -> None:
    require_human(ctx, "Changing the approval mode")
    if not isinstance(params, dict) or "nodeId" not in params:
        raise ValueError("params.nodeId is required")
    if "mode" not in params:
        raise ValueError("params.mode is required")
    queue = get_approval_queue()
    queue.set_settings(
        mode=params["mode"],
        allow_patterns=params.get("allowPatterns"),
        deny_patterns=params.get("denyPatterns"),
        node_id=params["nodeId"],
    )
    return None


@_d.method("exec.approval.request")
async def _handle_exec_approval_request(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("params required: toolName, args, sessionKey")
    for field in ("toolName", "args", "sessionKey"):
        if field not in params:
            raise ValueError(f"params.{field} is required")
    return approval_request_rpc_payload(
        get_approval_queue(),
        namespace="exec",
        params=params,
        node_id=params.get("nodeId"),
    )


@_d.method("exec.approval.waitDecision")
async def _handle_exec_approval_wait_decision(
    params: dict | None, ctx: RpcContext
) -> dict[str, Any]:
    if not isinstance(params, dict) or "id" not in params:
        raise ValueError("params.id is required")
    queue = get_approval_queue()
    return await approval_wait_decision_rpc_payload(
        queue,
        params["id"],
        timeout_seconds=params.get("timeoutSeconds"),
    )


@_d.method("exec.approval.snapshot")
async def _handle_exec_approval_snapshot(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Return a diagnostic snapshot: current mode + cached intent count."""
    from agentos.application.intent_cache import get_intent_cache

    queue = get_approval_queue()
    cache = get_intent_cache()
    return approval_snapshot_rpc_payload(queue, cache)


@_d.method("exec.approval.forget")
async def _handle_exec_approval_forget(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Drop cached intent approvals.

    ``params.target`` (optional) — clear entries matching a single command/path.
    Omit to wipe the whole intent cache.
    """
    from agentos.application.intent_cache import get_intent_cache

    require_human(ctx, "Forgetting remembered approvals")
    cache = get_intent_cache()
    if isinstance(params, dict):
        target = params.get("target")
    else:
        target = None
    return approval_forget_rpc_payload(cache, target)


@_d.method("exec.approval.resolve")
async def _handle_exec_approval_resolve(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    require_human(ctx, "Answering an approval")
    return await run_idempotent(
        "exec.approval.resolve", params, lambda: _exec_approval_resolve(params)
    )


async def _exec_approval_resolve(params: dict | None) -> dict[str, Any]:
    if not isinstance(params, dict) or "id" not in params:
        raise ValueError("params.id is required")
    if "approved" not in params:
        raise ValueError("params.approved is required")
    allow_always = bool(params.get("allowAlways", False))
    remember_intent = bool(params.get("rememberIntent", False))
    elevated_mode = params.get("elevatedMode")
    if elevated_mode not in ("on", "bypass", "full"):
        elevated_mode = None
    queue = get_approval_queue()
    return approval_resolve_rpc_payload(
        queue,
        params["id"],
        bool(params["approved"]),
        allow_always=allow_always,
        remember_intent=remember_intent,
        elevated_mode=elevated_mode,
        payload=params.get("payload"),
    )


@_d.method("approval.list")
async def _handle_approval_list(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Pending approvals across namespaces (exec, plugin, acp, acp_elicitation, flow).

    ``params.namespace`` (optional) narrows the list to one namespace.
    ``params.display: true`` answers exactly as ``GET /api/approvals`` does
    (display items plus the allow/deny patterns), for a console that reaches
    this gateway only over its WebSocket.
    """
    namespace = params.get("namespace") if isinstance(params, dict) else None
    if namespace is not None and not isinstance(namespace, str):
        raise ValueError("params.namespace must be a string")
    queue = get_approval_queue()
    if isinstance(params, dict) and params.get("display") is True:
        settings = approval_settings_rpc_payload(queue.get_settings())
        mode = settings.get("mode", "prompt")
        return {
            "pending": [
                approval_display_item(p, mode=mode) for p in queue.list_pending(namespace or None)
            ],
            "mode": mode,
            "allowPatterns": settings.get("allowPatterns", []),
            "denyPatterns": settings.get("denyPatterns", []),
        }
    return approval_pending_rpc_payload(queue, namespace)


@_d.method("plugin.approval.request")
async def _handle_plugin_approval_request(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("params required: pluginId, version, permissions")
    for field in ("pluginId", "version", "permissions"):
        if field not in params:
            raise ValueError(f"params.{field} is required")
    return approval_request_rpc_payload(
        get_approval_queue(),
        namespace="plugin",
        params=params,
    )


@_d.method("plugin.approval.waitDecision")
async def _handle_plugin_approval_wait_decision(
    params: dict | None, ctx: RpcContext
) -> dict[str, Any]:
    if not isinstance(params, dict) or "id" not in params:
        raise ValueError("params.id is required")
    queue = get_approval_queue()
    return await approval_wait_decision_rpc_payload(
        queue,
        params["id"],
        timeout_seconds=params.get("timeoutSeconds"),
    )


@_d.method("plugin.approval.resolve")
async def _handle_plugin_approval_resolve(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    require_human(ctx, "Answering an approval")
    return await run_idempotent(
        "plugin.approval.resolve", params, lambda: _plugin_approval_resolve(params)
    )


async def _plugin_approval_resolve(params: dict | None) -> dict[str, Any]:
    if not isinstance(params, dict) or "id" not in params:
        raise ValueError("params.id is required")
    if "approved" not in params:
        raise ValueError("params.approved is required")
    queue = get_approval_queue()
    return approval_resolve_rpc_payload(queue, params["id"], bool(params["approved"]))
