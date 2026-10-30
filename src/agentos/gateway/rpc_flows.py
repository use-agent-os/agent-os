"""``flows.*`` RPC: the engine side of the Flows editor.

Thin handlers: check params, call :class:`agentos.flows.service.FlowService`,
map its coded errors onto :class:`RpcHandlerError`. Control surface only (the
dispatcher's default audience). Reads are open to every control connection;
anything that writes or spends (create, save, delete, run, stop, upload,
import) is refused on an agent-bound connection with
``flows.operator_required``: an agent runs flows through its ``flow_run`` tool,
which applies the cost gate and the approval flow.

Events (``flows.run``, ``flows.node``, ``flows.changed``) go to control
connections only, through the WebSocket registry's control broadcast.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import Any

from agentos.flows import FlowError, FlowService
from agentos.gateway.agent_surface import agent_binding
from agentos.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher

_d = get_dispatcher()

Handler = Callable[[dict | None, RpcContext], Awaitable[dict[str, Any]]]


async def broadcast_flow_event(event: str, payload: dict[str, Any]) -> None:
    """Fan an engine event out to admitted control connections."""
    from agentos.gateway.websocket import get_registry

    try:
        await get_registry().broadcast_control(event, payload)
    except Exception:  # pragma: no cover - best effort
        pass


def _service(ctx: RpcContext) -> FlowService:
    from agentos.gateway.flows_wiring import flow_service

    return flow_service(
        ctx.config,
        broadcast=broadcast_flow_event,
        usage_tracker=getattr(ctx, "usage_tracker", None),
    )


def _to_rpc_error(exc: FlowError) -> RpcHandlerError:
    details = dict(exc.details or {})
    if exc.code == "flows.invalid":
        details.setdefault("node_errors", {})
    return RpcHandlerError(exc.code, exc.message, details=details or None)


def _handler(fn: Handler) -> Handler:
    """Map ``FlowError`` and bad params onto the gateway error shape."""

    @functools.wraps(fn)
    async def wrapper(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
        try:
            return await fn(params, ctx)
        except FlowError as exc:
            raise _to_rpc_error(exc) from exc
        except (TypeError, ValueError) as exc:
            raise RpcHandlerError("flows.invalid", str(exc), details={"node_errors": {}}) from exc

    return wrapper


READ_ONLY_REASON = (
    "This window is connected as an agent, so it can't change or run flows. "
    "Open Flows in the AgentOS app that manages this gateway."
)


def _access(ctx: RpcContext) -> dict[str, Any]:
    """``writable`` / ``read_only_reason`` for this connection, known before any write."""
    if agent_binding(ctx) is not None:
        return {"writable": False, "read_only_reason": READ_ONLY_REASON}
    return {"writable": True, "read_only_reason": None}


def _operator_only(fn: Handler) -> Handler:
    @functools.wraps(fn)
    async def wrapper(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
        binding = agent_binding(ctx)
        if binding is not None:
            raise RpcHandlerError(
                "flows.operator_required", READ_ONLY_REASON, details=binding.to_dict()
            )
        return await fn(params, ctx)

    return wrapper


def _params(params: dict | None) -> dict[str, Any]:
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    return params


def _str(p: dict[str, Any], key: str, *, required: bool = False) -> str | None:
    value = p.get(key)
    if value is None or value == "":
        if required:
            raise ValueError(f"params.{key} is required")
        return None
    if not isinstance(value, str):
        raise ValueError(f"params.{key} must be a string")
    return value.strip()


def _req(p: dict[str, Any], key: str) -> str:
    value = _str(p, key, required=True)
    assert value is not None
    return value


def _obj(p: dict[str, Any], key: str, *, required: bool = False) -> dict[str, Any] | None:
    value = p.get(key)
    if value is None:
        if required:
            raise ValueError(f"params.{key} is required")
        return None
    if not isinstance(value, dict):
        raise ValueError(f"params.{key} must be an object")
    return value


def _int(p: dict[str, Any], key: str, *, default: int | None = None) -> int | None:
    value = p.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"params.{key} must be an integer")
    return int(value)


# ── schema ────────────────────────────────────────────────────────────────


@_d.method("flows.nodes")
@_handler
async def _flows_nodes(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    return _service(ctx).node_schemas()


@_d.method("flows.models")
@_handler
async def _flows_models(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    return _service(ctx).models_wire()


# ── documents ─────────────────────────────────────────────────────────────


@_d.method("flows.list")
@_handler
async def _flows_list(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    return {**_service(ctx).list_flows(), **_access(ctx)}


@_d.method("flows.create")
@_handler
@_operator_only
async def _flows_create(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    flow = await _service(ctx).create_flow(_req(p, "name"), _str(p, "template"))
    return {"flow": flow}


@_d.method("flows.get")
@_handler
async def _flows_get(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    return {"flow": _service(ctx).get_flow(_req(p, "id")), **_access(ctx)}


@_d.method("flows.save")
@_handler
@_operator_only
async def _flows_save(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    flow = _obj(p, "flow", required=True)
    base_rev = _int(p, "base_rev")
    if base_rev is None:
        raise ValueError("params.base_rev is required")
    return {"flow": await _service(ctx).save_flow(flow, base_rev)}


@_d.method("flows.delete")
@_handler
@_operator_only
async def _flows_delete(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    await _service(ctx).delete_flow(_req(p, "id"))
    return {"ok": True}


# ── analysis ──────────────────────────────────────────────────────────────


@_d.method("flows.validate")
@_handler
async def _flows_validate(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    return _service(ctx).validate(_obj(p, "flow", required=True), _str(p, "to_node"))


@_d.method("flows.estimate")
@_handler
async def _flows_estimate(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    return _service(ctx).estimate(
        _obj(p, "flow", required=True), _str(p, "to_node"), _obj(p, "inputs")
    )


# ── runs ──────────────────────────────────────────────────────────────────


@_d.method("flows.run")
@_handler
@_operator_only
async def _flows_run(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    origin = _str(p, "origin") or "canvas"
    if origin == "agent":
        raise ValueError("params.origin must be 'canvas' or 'cli'")
    return await _service(ctx).submit_run(
        _req(p, "flow_id"),
        flow=_obj(p, "flow"),
        inputs=_obj(p, "inputs"),
        to_node=_str(p, "to_node"),
        origin=origin,
    )


@_d.method("flows.stop")
@_handler
@_operator_only
async def _flows_stop(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    await _service(ctx).stop_run(_req(p, "run_id"))
    return {"ok": True}


@_d.method("flows.runs")
@_handler
async def _flows_runs(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    limit = _int(p, "limit", default=30) or 30
    return {"runs": _service(ctx).list_runs(_str(p, "flow_id"), limit)}


@_d.method("flows.run.get")
@_handler
async def _flows_run_get(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    return {"run": _service(ctx).get_run(_req(p, "run_id"))}


# ── media ─────────────────────────────────────────────────────────────────


@_d.method("flows.media")
@_handler
async def _flows_media(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    return _service(ctx).media_wire(_req(p, "sha256").lower(), _int(p, "max_side"))


@_d.method("flows.upload")
@_handler
@_operator_only
async def _flows_upload(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    image = _service(ctx).upload(_str(p, "name"), _str(p, "mime"), p.get("data_b64"))
    return {"image": image.to_wire()}


@_d.method("flows.import_png")
@_handler
@_operator_only
async def _flows_import_png(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    p = _params(params)
    return _service(ctx).import_png(p.get("data_b64"))
