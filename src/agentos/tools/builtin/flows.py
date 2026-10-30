"""Image Flows agent tools: ``flow_list`` and ``flow_run``.

An Image Flow is a node graph the user builds in the desktop Flows editor
(``docs/flows.md``). These tools let an agent find one and run it in-process
through :class:`agentos.flows.FlowService`, never through the ``flows.*`` RPC
(which refuses writes on an agent-bound connection).

Spending is gated: ``flow_run`` asks the engine for an estimate first and
queues the run directly only when the estimate is known and at or under
``[flows].agent_cost_limit_usd``. Anything above that, or an unknown estimate,
goes through the approval queue (namespace ``exec``, the same prompt the
operator answers for a shell command or a patch); the engine waits for the
decision and retries the call with the ``approval_id``, which binds the
approval to this flow, its revision and these inputs.

Saved PNGs are published into the current session as artifacts while the run
executes, and a ``application/vnd.agentos.flow-run+json`` card marker is
published first in the call's artifact list, so the chat draws the run card
above the images.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import structlog

from agentos.tools.registry import tool
from agentos.tools.types import (
    CallerKind,
    InteractionMode,
    ToolContext,
    ToolError,
    UnsupportedSurfaceError,
    current_tool_context,
)

log = structlog.get_logger(__name__)

FLOW_RUN_MIME = "application/vnd.agentos.flow-run+json"
CARD_VERSION = 1
FLOW_TOOL_NAMES: frozenset[str] = frozenset({"flow_list", "flow_run"})

#: Spend approvals have their own namespace: the queue never turns a resolve in
#: it into a session elevation, "always allow" is not remembered for it, and a
#: session-wide bypass (which releases pending ``exec`` approvals) never
#: approves a spend. The approval surfaces offer only allow-once and deny.
_APPROVAL_NAMESPACE = "flow"
_APPROVAL_TOOL = "flow_run"
#: How long a declined run keeps every later ``flow_run`` of that flow in that
#: session behind a fresh approval, whatever its inputs or cost.
DENIAL_WINDOW_S = 30 * 60
_MAX_DENIALS = 256
_DEFAULT_WAIT_SECONDS = 300
_MAX_WAIT_SECONDS = 900
_TERMINAL = frozenset({"done", "failed", "cancelled"})
_MAX_INPUT_CHARS = 8_000


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def _gateway_config() -> Any | None:
    try:
        from agentos.tools.builtin import control

        return control._gateway_config
    except Exception:  # noqa: BLE001 - a missing config means "defaults"
        return None


def _flows_enabled(config: Any | None) -> bool:
    flows_cfg = getattr(config, "flows", None)
    if flows_cfg is None:
        return True
    return bool(getattr(flows_cfg, "enabled", True))


def _live_service() -> Any | None:
    """The gateway's FlowService when one is running in this process (built at boot)."""
    from agentos.flows import peek_flow_service

    return peek_flow_service()


def _build_service(config: Any | None) -> Any:
    from agentos.gateway.flows_wiring import flow_service

    return flow_service(config)


def flows_available(config: Any | None = None) -> bool:
    """Whether the flow tools can do anything: flows on and one image model has a key.

    Reads the live service only (the gateway builds it at boot and keeps its
    config current), so a turn's tool-surface check never creates flow state
    or touches the network.
    """
    service = _live_service()
    if service is None:
        return False
    cfg = config if config is not None else service.config
    if not _flows_enabled(cfg):
        return False
    try:
        return service.models.default_id() is not None
    except Exception:  # noqa: BLE001 - capability checks must be non-fatal
        return False


def _unavailable_reason(service: Any) -> str | None:
    """Why the tools cannot run, phrased for the model, or ``None`` when they can."""
    registry = service.models
    if registry.default_id() is not None:
        return None
    reasons = sorted(
        {
            str(model.get("unavailable_reason") or "")
            for model in registry.to_wire().get("models") or []
            if model.get("unavailable_reason")
        }
    )
    detail = f" ({'; '.join(reasons)})" if reasons else ""
    return (
        f"No image model is available for Image Flows{detail}. Ask the user to add an "
        "image provider key (see `agentos flows models`), then try again."
    )


def _service() -> Any:
    """The live FlowService (built on first use outside the gateway).

    A live service is used as it is: its config is the one the gateway keeps
    current, and pushing this module's boot-time copy into it could undo a
    reload.
    """
    service = _live_service()
    if service is None:
        service = _build_service(_gateway_config())
    if not _flows_enabled(service.config):
        raise ToolError("Image Flows are disabled ([flows].enabled = false).")
    reason = _unavailable_reason(service)
    if reason is not None:
        raise ToolError(reason)
    return service


def _flow_error(exc: Exception) -> ToolError:
    code = str(getattr(exc, "code", "") or "")
    message = str(getattr(exc, "message", "") or exc)
    details = getattr(exc, "details", None) or {}
    node_errors = details.get("node_errors") if isinstance(details, dict) else None
    if code == "flows.invalid" and node_errors:
        lines = [
            f"{node_id}: {err.get('message') or err.get('code')}"
            + (f" (input {err['input']})" if err.get("input") else "")
            for node_id, errs in node_errors.items()
            for err in errs
            if isinstance(err, dict)
        ]
        message = f"{message}: " + "; ".join(lines)
    return ToolError(f"{code}: {message}" if code else message)


# ---------------------------------------------------------------------------
# Flow documents
# ---------------------------------------------------------------------------


def _public_inputs(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """``[{name, target, value}]``: what ``inputs`` may override, with current values."""
    import agentos.flows.nodes  # noqa: F401 - registers the built-in nodes
    from agentos.flows.schema import get_node_type

    by_id = {str(node.get("id")): node for node in doc.get("nodes") or []}
    out: list[dict[str, Any]] = []
    for name, target in (doc.get("inputs") or {}).items():
        node_id, _, input_name = str(target).partition(".")
        node = by_id.get(node_id) or {}
        values = node.get("values") or {}
        value = values.get(input_name)
        if value is None:
            cls = get_node_type(str(node.get("type") or ""))
            spec = cls.schema.input(input_name) if cls is not None else None
            value = spec.default if spec is not None else None
        out.append({"name": name, "target": str(target), "value": value})
    return out


def _clean_inputs(inputs: Any) -> dict[str, Any]:
    if inputs is None or inputs == "":
        return {}
    if isinstance(inputs, str):
        try:
            inputs = json.loads(inputs)
        except ValueError as exc:
            raise ToolError("inputs must be an object of public input names to values") from exc
    if not isinstance(inputs, dict):
        raise ToolError("inputs must be an object of public input names to values")
    cleaned: dict[str, Any] = {}
    for key, value in inputs.items():
        if not isinstance(key, str) or not key.strip():
            raise ToolError("inputs keys must be public input names")
        if not isinstance(value, (str, int, float, bool)):
            raise ToolError(f"inputs.{key} must be a string, number or boolean")
        if isinstance(value, str) and len(value) > _MAX_INPUT_CHARS:
            raise ToolError(f"inputs.{key} is longer than {_MAX_INPUT_CHARS} characters")
        cleaned[key.strip()] = value
    return cleaned


# ---------------------------------------------------------------------------
# Run summaries (the tool result and the card share them)
# ---------------------------------------------------------------------------


def _node_types(record: dict[str, Any]) -> dict[str, str]:
    graph = record.get("graph") or {}
    return {str(node.get("id")): str(node.get("type")) for node in graph.get("nodes") or []}


def run_images(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Saved images (with their files), else generated ones, in graph order."""
    types = _node_types(record)
    outputs = record.get("outputs") or {}
    order = [str(node.get("id")) for node in (record.get("graph") or {}).get("nodes") or []]
    saved: list[dict[str, Any]] = []
    generated: list[dict[str, Any]] = []
    for node_id in order:
        out = outputs.get(node_id)
        if not isinstance(out, dict):
            continue
        refs = [ref for ref in out.get("images") or [] if isinstance(ref, dict)]
        if types.get(node_id) == "save":
            files = [str(path) for path in out.get("files") or []]
            for index, ref in enumerate(refs):
                path = files[index] if index < len(files) else None
                saved.append(
                    {
                        "sha256": ref.get("sha256"),
                        "mime": ref.get("mime") or "image/png",
                        "width": ref.get("width"),
                        "height": ref.get("height"),
                        "name": Path(path).name if path else None,
                        "path": path,
                        "node_id": node_id,
                    }
                )
        elif types.get(node_id) == "image":
            for ref in refs:
                generated.append(
                    {
                        "sha256": ref.get("sha256"),
                        "mime": ref.get("mime") or "image/png",
                        "width": ref.get("width"),
                        "height": ref.get("height"),
                        "name": None,
                        "path": None,
                        "node_id": node_id,
                    }
                )
    return saved or generated


def run_progress(record: dict[str, Any]) -> dict[str, int]:
    states = [
        str(state.get("state"))
        for state in (record.get("nodes") or {}).values()
        if isinstance(state, dict)
    ]
    return {
        "done": sum(1 for s in states if s in ("done", "cached")),
        "total": len(states),
        "cached": sum(1 for s in states if s == "cached"),
        "failed": sum(1 for s in states if s == "error"),
    }


def card_payload(record: dict[str, Any], flow_name: str) -> dict[str, Any]:
    """The ``application/vnd.agentos.flow-run+json`` body (docs/flows.md)."""
    from agentos.flows.types import utc_now_iso

    progress = run_progress(record)
    return {
        "version": CARD_VERSION,
        "kind": "flow_run",
        "run_id": record.get("run_id"),
        "flow_id": record.get("flow_id"),
        "flow_name": flow_name,
        "status": record.get("status"),
        "origin": record.get("origin"),
        "progress": {"done": progress["done"], "total": progress["total"]},
        "cached": progress["cached"],
        "est_usd": record.get("est_usd"),
        "cost_usd": record.get("cost_usd"),
        "images": [
            {key: image.get(key) for key in ("sha256", "mime", "width", "height", "name")}
            for image in run_images(record)
        ],
        "error": record.get("error"),
        "created": record.get("created"),
        "ended": record.get("ended"),
        "fetchedAt": utc_now_iso(),
    }


# ---------------------------------------------------------------------------
# Session artifacts
# ---------------------------------------------------------------------------


def _artifact_ctx(ctx: ToolContext | None) -> ToolContext | None:
    if (
        ctx is None
        or ctx.caller_kind is CallerKind.SUBAGENT
        or not ctx.artifact_media_root
        or not ctx.artifact_session_id
        or not ctx.session_key
    ):
        return None
    return ctx


def _store_kwargs(ctx: ToolContext) -> dict[str, Any]:
    from agentos.artifacts import DEFAULT_ARTIFACT_DISK_BUDGET_BYTES, DEFAULT_ARTIFACT_MAX_BYTES

    return {
        "session_id": ctx.artifact_session_id,
        "session_key": ctx.session_key,
        "source": "flow_run",
        "max_bytes": ctx.artifact_max_bytes
        if ctx.artifact_max_bytes is not None
        else DEFAULT_ARTIFACT_MAX_BYTES,
        "disk_budget_bytes": ctx.artifact_disk_budget_bytes
        if ctx.artifact_disk_budget_bytes is not None
        else DEFAULT_ARTIFACT_DISK_BUDGET_BYTES,
    }


def _artifact_sink(ctx: ToolContext | None) -> Any:
    """``(path, mime) -> artifact | None`` publishing each saved PNG into the session.

    Bound to the calling context (the run executes in its own task). Never
    raises: a full artifact budget must not fail the Save node, whose file is
    already on disk.
    """
    target = _artifact_ctx(ctx)
    if target is None:
        return None

    def sink(path: Path, mime: str) -> dict[str, Any] | None:
        from agentos.artifacts import ArtifactStore, artifact_payload

        try:
            ref = ArtifactStore(str(target.artifact_media_root)).publish_file(
                path, name=Path(path).name, mime=mime or "image/png", **_store_kwargs(target)
            )
        except Exception as exc:  # noqa: BLE001 - see docstring
            log.warning("flow_run.artifact_publish_failed", path=str(path), error=str(exc))
            return None
        payload = artifact_payload(ref)
        target.published_artifacts.append(payload)
        return payload

    return sink


def _publish_card(
    ctx: ToolContext | None, card: dict[str, Any], *, position: int
) -> dict[str, Any] | None:
    """Publish the card marker ahead of this call's image artifacts."""
    target = _artifact_ctx(ctx)
    if target is None:
        return None
    from agentos.artifacts import ArtifactStore, artifact_payload

    body = json.dumps(card, ensure_ascii=False).encode("utf-8")
    try:
        ref = ArtifactStore(str(target.artifact_media_root)).publish_bytes(
            body,
            name=f"flow-run-{card.get('run_id')}.json",
            mime=FLOW_RUN_MIME,
            **_store_kwargs(target),
        )
    except Exception as exc:  # noqa: BLE001 - the run already happened; the card is a bonus
        log.warning("flow_run.card_publish_failed", error=str(exc))
        return None
    payload = artifact_payload(ref)
    position = max(0, min(position, len(target.published_artifacts)))
    target.published_artifacts.insert(position, payload)
    return payload


def _public_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in artifact.items() if key != "download_url"}


# ---------------------------------------------------------------------------
# Approval (cost gate)
# ---------------------------------------------------------------------------

#: ``(session_key, flow_id) -> monotonic time of the last declined run``.
#: In-process on purpose: it guards one conversation against an agent that
#: answers a "no" with a cheaper variant, and a gateway restart ends that
#: conversation's turn anyway.
_denials: dict[tuple[str, str], float] = {}


def _denial_key(ctx: ToolContext | None, flow_id: str) -> tuple[str, str]:
    return (str(ctx.session_key or "") if ctx is not None else "", flow_id)


def _record_denial(ctx: ToolContext | None, flow_id: str) -> None:
    now = time.monotonic()
    for key, at in list(_denials.items()):
        if now - at > DENIAL_WINDOW_S:
            _denials.pop(key, None)
    if len(_denials) >= _MAX_DENIALS:
        _denials.pop(min(_denials, key=lambda k: _denials[k]), None)
    _denials[_denial_key(ctx, flow_id)] = now


def _denied_minutes_ago(ctx: ToolContext | None, flow_id: str) -> int | None:
    """Minutes since this session declined a run of this flow, within the window."""
    at = _denials.get(_denial_key(ctx, flow_id))
    if at is None:
        return None
    age = time.monotonic() - at
    if age > DENIAL_WINDOW_S:
        _denials.pop(_denial_key(ctx, flow_id), None)
        return None
    return int(age // 60)


def _clear_denial(ctx: ToolContext | None, flow_id: str) -> None:
    _denials.pop(_denial_key(ctx, flow_id), None)


#: Approval ids this session asked for, per flow, until their answer is seen.
#: The engine stops waiting for an answer after a while; a "no" given after
#: that must still count when the agent calls again.
_outstanding: dict[tuple[str, str], list[str]] = {}


def _remember_request(ctx: ToolContext | None, flow_id: str, approval_id: str) -> None:
    ids = _outstanding.setdefault(_denial_key(ctx, flow_id), [])
    ids.append(approval_id)
    del ids[:-8]
    if len(_outstanding) > _MAX_DENIALS:
        _outstanding.pop(next(iter(_outstanding)), None)


def _forget_request(ctx: ToolContext | None, flow_id: str, approval_id: str) -> None:
    key = _denial_key(ctx, flow_id)
    ids = [i for i in _outstanding.get(key, []) if i != approval_id]
    if ids:
        _outstanding[key] = ids
    else:
        _outstanding.pop(key, None)


def _note_request(ctx: ToolContext | None, flow_id: str, envelope: str) -> None:
    """Remember a prompt this call raised; a policy denial counts as a "no" now."""
    try:
        status = json.loads(envelope)
    except ValueError:
        return
    if status.get("status") == "approval_required" and status.get("approval_id"):
        _remember_request(ctx, flow_id, str(status["approval_id"]))
    elif status.get("status") == "approval_denied":
        _record_denial(ctx, flow_id)


def _sync_outstanding(ctx: ToolContext | None, flow_id: str) -> None:
    """Record a denial for any earlier prompt of this flow the user has since declined."""
    key = _denial_key(ctx, flow_id)
    ids = _outstanding.get(key)
    if not ids:
        return
    from agentos.gateway.approval_queue import get_approval_queue

    queue = get_approval_queue()
    keep: list[str] = []
    for approval_id in ids:
        try:
            entry = queue.get(approval_id)
        except KeyError:
            continue
        if not entry.resolved:
            keep.append(approval_id)
        elif not entry.approved:
            _record_denial(ctx, flow_id)
    if keep:
        _outstanding[key] = keep
    else:
        _outstanding.pop(key, None)


_DECLINED_MESSAGE = (
    "The user declined this run; nothing was queued and nothing was spent. Do not "
    "run this flow again, and do not work around the refusal (fewer images, other "
    "inputs, a cheaper model, another flow, or image_generate) unless the user asks "
    "you to in a new message. Tell the user the run was not started."
)


def _usd(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value == 0 or value >= 0.1:
        return f"${value:.2f}"
    return f"${value:.3f}" if value >= 0.01 else f"${value:.4f}"


def _approval_args(doc: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    return {"flow_id": doc["id"], "rev": int(doc.get("rev") or 0), "inputs": inputs}


def price_text(price: Any) -> str:
    """A Price as the desktop writes it: ``free``, ``~$0.067``, ``~$0.0034–$0.10``, ``unknown``."""
    if not isinstance(price, dict):
        return "unknown"
    mark = "~" if price.get("approximate") else ""
    kind = price.get("kind")
    if kind == "free":
        return "free"
    if kind == "usd" and isinstance(price.get("usd"), (int, float)):
        return f"{mark}{_usd(price['usd'])}"
    low, high = price.get("min"), price.get("max")
    if kind == "range" and isinstance(low, (int, float)) and isinstance(high, (int, float)):
        return f"{mark}{_usd(low)}" if low == high else f"{mark}{_usd(low)}–{_usd(high)}"
    return "unknown"


def _approval_params(
    doc: dict[str, Any],
    inputs: dict[str, Any],
    total: dict[str, Any],
    limit_usd: float,
    ctx: ToolContext | None,
    *,
    declined_minutes_ago: int | None = None,
) -> dict[str, Any]:
    """The approval row. ``estimateUsd`` is the upper bound the run is capped at."""
    from agentos.flows.types import price_upper_usd

    name = str(doc.get("name") or doc["id"])
    estimate_usd = price_upper_usd(total) if total else None
    shown = price_text(total) if estimate_usd is not None else "unknown"
    if declined_minutes_ago is not None:
        when = "just now" if declined_minutes_ago < 1 else f"{declined_minutes_ago} min ago"
        cost = "cost unknown" if estimate_usd is None else f"estimated {shown}"
        warning = (
            f"You declined a run of the Image Flow {name!r} {when}. The agent asks to "
            f"run it again ({cost}). Approve only if you want it run now."
        )
    elif estimate_usd is None:
        warning = (
            f"Runs the Image Flow {name!r}. Its cost cannot be estimated; agents may "
            f"spend up to {_usd(limit_usd)} per run without asking."
        )
    else:
        warning = (
            f"Runs the Image Flow {name!r} for an estimated {shown}, above the "
            f"{_usd(limit_usd)} agents may spend per run without asking."
        )
    return {
        "toolName": _APPROVAL_TOOL,
        "command": f"flow_run {name}",
        "args": _approval_args(doc, inputs),
        "estimateUsd": estimate_usd,
        "limitUsd": limit_usd,
        "warning": warning,
        "sessionKey": ctx.session_key if ctx is not None and ctx.session_key else "",
        "agent": ctx.agent_id if ctx is not None else "",
        "mode": "flow",
    }


def _envelope(status: str, approval_id: str, params: dict[str, Any], message: str) -> str:
    return json.dumps(
        {
            "status": status,
            "approval_id": approval_id,
            "command": params.get("command"),
            "estimate_usd": params.get("estimateUsd"),
            "limit_usd": params.get("limitUsd"),
            "warning": params.get("warning"),
            "message": message,
        }
    )


def _check_approval(
    approval_id: str, doc: dict[str, Any], inputs: dict[str, Any], ctx: ToolContext | None
) -> tuple[str | None, float | None]:
    """``(envelope, approved_limit)``: an envelope to return, or the amount approved."""
    from agentos.gateway.approval_queue import get_approval_queue

    queue = get_approval_queue()
    try:
        entry = queue.get(approval_id)
    except KeyError as exc:
        raise ToolError(str(exc)) from exc
    if entry.namespace != _APPROVAL_NAMESPACE or entry.params.get("toolName") != _APPROVAL_TOOL:
        raise ToolError(f"Approval does not belong to flow_run: {approval_id}")
    # Bound to the flow, its revision and these inputs: an approval for one
    # cheap run cannot be replayed for another flow or an edited graph.
    if entry.params.get("args") != _approval_args(doc, inputs):
        raise ToolError(
            "Approval does not match this run (the flow or its inputs changed); "
            "call flow_run again without approval_id"
        )
    if entry.consumed:
        raise ToolError(f"Approval already consumed: {approval_id}")
    if entry.resolved:
        # Its answer is handled here; a later sync must not count it again.
        _forget_request(ctx, doc["id"], approval_id)
    if not entry.resolved:
        return (
            _envelope("approval_pending", approval_id, entry.params, "Approval is still pending."),
            None,
        )
    if not entry.approved:
        # A "no" sticks: every later run of this flow in this session, at any
        # price, asks again for DENIAL_WINDOW_S (an expired prompt counts too).
        _record_denial(ctx, doc["id"])
        return _envelope("approval_denied", approval_id, entry.params, _DECLINED_MESSAGE), None
    try:
        queue.consume(approval_id)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    _clear_denial(ctx, doc["id"])
    approved = entry.params.get("estimateUsd")
    return None, float(approved) if isinstance(approved, (int, float)) else None


def _request_approval(params: dict[str, Any], ctx: ToolContext | None) -> tuple[str | None, bool]:
    """``(envelope, approved_now)``. ``approved_now`` means a policy approved it."""
    from agentos.gateway.approval_queue import get_approval_queue

    queue = get_approval_queue()
    settings = queue.get_settings()
    if (
        settings.mode not in ("auto-approve", "auto-deny")
        and ctx is not None
        and ctx.interaction_mode is InteractionMode.UNATTENDED
    ):
        raise UnsupportedSurfaceError(
            f"flow_run needs approval here ({params.get('warning')}), but this run is "
            "unattended. Ask the user to run the flow from the Flows editor or the CLI, "
            "or to raise [flows].agent_cost_limit_usd."
        )
    approval_id = queue.request(namespace=_APPROVAL_NAMESPACE, params=params)
    if settings.mode == "auto-approve":
        queue.resolve(approval_id, True, resolved_by="policy")
        queue.consume(approval_id)
        return None, True
    if settings.mode == "auto-deny":
        queue.resolve(approval_id, False, resolved_by="policy")
        return (
            _envelope(
                "approval_denied",
                approval_id,
                params,
                "Denied by the active approval policy; nothing was queued. Do not retry "
                "or work around it; tell the user.",
            ),
            False,
        )
    return (
        _envelope(
            "approval_required",
            approval_id,
            params,
            "The estimated cost needs the user's approval. Wait for it; the call is "
            "retried with the approval_id.",
        ),
        False,
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool(
    name="flow_list",
    concurrency="parallel",
    read_only=True,
    defer_loading=True,
    description=(
        "List the user's saved Image Flows, which generate images (pictures, photos, "
        "illustrations, drawings). Returns ids, names and public inputs (the values "
        "flow_run may override). Call it before flow_run to pick a flow for an image "
        "request or to find the one the user names."
    ),
    params={},
)
async def flow_list() -> str:
    service = _service()
    rows = []
    for item in service.list_flows()["flows"]:
        try:
            doc = service.get_flow(item["id"])
        except Exception:  # noqa: BLE001 - one unreadable file must not hide the rest
            continue
        rows.append(
            {
                "id": item["id"],
                "name": item.get("name") or "",
                "updated": item.get("updated"),
                "node_count": item.get("node_count"),
                "inputs": _public_inputs(doc),
            }
        )
    return json.dumps(
        {
            "count": len(rows),
            "flows": rows,
            "default_model": service.models.default_id(),
            "agent_cost_limit_usd": service.agent_cost_limit_usd,
        },
        ensure_ascii=False,
        default=str,
    )


@tool(
    name="flow_run",
    defer_loading=True,
    requires_approval=True,
    description=(
        "Generate images (draw a picture, photo, illustration) with a saved Image Flow. "
        "Runs one of the user's flows by id or exact name, optionally overriding its public "
        "inputs such as the prompt (see flow_list), and waits for the images. Use it when "
        "the user asks to run or reuse a flow they built, and for any image request when "
        "image_generate is not available; when image_generate is available, a one-off "
        "picture from a prompt is image_generate. If no flow fits, tell the user to "
        "create one in the desktop Flows editor. A run whose estimate is above the agent "
        "spending limit, or unknown, waits for the user's approval; if the user declines, "
        "do not retry or try a cheaper variant. The saved images and a run card are "
        "delivered to the chat by this tool; do not publish them again."
    ),
    params={
        "flow": {
            "type": "string",
            "description": "Flow id (flw_...) or its exact name.",
        },
        "inputs": {
            "type": "object",
            "description": (
                'Public input overrides, e.g. {"prompt": "a red fox", "count": 2}. '
                "Only names flow_list reports are accepted."
            ),
        },
        "wait_seconds": {
            "type": "integer",
            "description": (
                f"How long to wait for the run (0-{_MAX_WAIT_SECONDS}, default "
                f"{_DEFAULT_WAIT_SECONDS}). A run still going then keeps running; its "
                "card in the chat updates when it finishes."
            ),
        },
        "approval_id": {
            "type": "string",
            "description": "Set by the runtime after the user approved the cost; never invent one.",
        },
    },
    required=["flow"],
    execution_timeout_seconds=_DEFAULT_WAIT_SECONDS + 30,
    execution_timeout_argument="wait_seconds",
    execution_timeout_padding=30.0,
)
async def flow_run(
    flow: str,
    inputs: dict[str, Any] | None = None,
    wait_seconds: int = _DEFAULT_WAIT_SECONDS,
    approval_id: str | None = None,
) -> str:
    from agentos.flows import FlowError

    if not isinstance(flow, str) or not flow.strip():
        raise ToolError("flow must be a flow id or name (see flow_list)")
    try:
        wait = float(wait_seconds)
    except (TypeError, ValueError) as exc:
        raise ToolError("wait_seconds must be a number of seconds") from exc
    wait = max(0.0, min(float(_MAX_WAIT_SECONDS), wait))
    cleaned = _clean_inputs(inputs)
    service = _service()
    ctx = current_tool_context.get()
    try:
        doc = service.resolve_flow(flow.strip())
    except FlowError as exc:
        raise _flow_error(exc) from exc

    limit: float | None = service.agent_cost_limit_usd
    if approval_id:
        envelope, limit = _check_approval(approval_id, doc, cleaned, ctx)
        if envelope is not None:
            return envelope
    else:
        _sync_outstanding(ctx, doc["id"])
        declined = _denied_minutes_ago(ctx, doc["id"])
        if declined is not None:
            # After a "no", nothing runs without a fresh yes, even under the limit.
            try:
                total = service.estimate(doc, None, cleaned or None).get("total") or {}
            except FlowError as exc:
                raise _flow_error(exc) from exc
            params = _approval_params(
                doc,
                cleaned,
                total if isinstance(total, dict) else {},
                service.agent_cost_limit_usd,
                ctx,
                declined_minutes_ago=declined,
            )
            upper = params["estimateUsd"]
            envelope, approved_now = _request_approval(params, ctx)
            if not approved_now:
                assert envelope is not None
                _note_request(ctx, doc["id"], envelope)
                return envelope
            _clear_denial(ctx, doc["id"])
            limit = upper
    artifact_start = len(ctx.published_artifacts) if ctx is not None else 0
    submit = {
        "inputs": cleaned or None,
        "origin": "agent",
        "session_key": ctx.session_key if ctx is not None else None,
        "artifact_sink": _artifact_sink(ctx),
    }
    try:
        queued = await service.submit_run(doc["id"], max_cost_usd=limit, **submit)
    except FlowError as exc:
        if exc.code != "flows.cost_gate":
            raise _flow_error(exc) from exc
        if approval_id:
            details = exc.details or {}
            return json.dumps(
                {
                    "status": "cost_changed",
                    "message": (
                        f"The estimate rose after approval ({exc.message}); nothing was "
                        "queued. Call flow_run again to ask for approval at the new price."
                    ),
                    "estimate": details.get("estimate"),
                }
            )
        details = exc.details or {}
        estimate = details.get("estimate") or {}
        total = estimate.get("total") if isinstance(estimate, dict) else None
        params = _approval_params(
            doc,
            cleaned,
            total if isinstance(total, dict) else {},
            service.agent_cost_limit_usd,
            ctx,
        )
        upper = params["estimateUsd"]
        envelope, approved_now = _request_approval(params, ctx)
        if not approved_now:
            assert envelope is not None
            _note_request(ctx, doc["id"], envelope)
            return envelope
        try:
            queued = await service.submit_run(doc["id"], max_cost_usd=upper, **submit)
        except FlowError as again:
            raise _flow_error(again) from again

    run_id = str(queued["run_id"])
    try:
        record = await service.wait_run(run_id, timeout=wait)
    except asyncio.CancelledError:
        # The turn was stopped: stop spending on its behalf too.
        try:
            await asyncio.shield(service.stop_run(run_id))
        except Exception:  # noqa: BLE001 - best effort while unwinding
            log.debug("flow_run.stop_on_cancel_failed", run_id=run_id, exc_info=True)
        raise
    return _result(record, doc, ctx, artifact_start)


def _result(
    record: dict[str, Any], doc: dict[str, Any], ctx: ToolContext | None, artifact_start: int
) -> str:
    flow_name = str(doc.get("name") or doc["id"])
    status = str(record.get("status") or "")
    progress = run_progress(record)
    images = run_images(record)
    published = list(ctx.published_artifacts[artifact_start:]) if ctx is not None else []
    card = _publish_card(ctx, card_payload(record, flow_name), position=artifact_start)
    by_name = {str(a.get("name")): a for a in published if a.get("name")}
    rows = []
    for image in images:
        row = {key: image.get(key) for key in ("path", "sha256", "width", "height")}
        artifact = by_name.get(str(image.get("name"))) if image.get("name") else None
        if artifact is not None:
            row["artifact_id"] = artifact.get("id")
        rows.append(row)
    node_errors = {
        node_id: state.get("error")
        for node_id, state in (record.get("nodes") or {}).items()
        if isinstance(state, dict) and state.get("state") == "error"
    }
    payload: dict[str, Any] = {
        "status": status if status in _TERMINAL else "running",
        "run_id": record.get("run_id"),
        "flow": {"id": doc["id"], "name": flow_name},
        "images": rows,
        "image_count": len(rows),
        "cost_usd": record.get("cost_usd"),
        "est_usd": record.get("est_usd"),
        "cached": progress["cached"],
        "nodes": {"done": progress["done"], "total": progress["total"]},
        "error": record.get("error"),
    }
    if node_errors:
        payload["node_errors"] = node_errors
    if status not in _TERMINAL:
        payload["note"] = (
            "The run is still going and keeps running in the background; its card in the "
            "chat updates when it finishes. Do not start it again."
        )
    if not any(image.get("path") for image in images) and images:
        payload["note_unsaved"] = (
            "This flow has no Save node, so the images are only in the Flows media store."
        )
    artifacts = [card] if card is not None else []
    artifacts.extend(published)
    if artifacts:
        payload["artifacts"] = [_public_artifact(a) for a in artifacts]
        payload["note_delivery"] = (
            "The run card and the saved images are already in this chat. Do not call "
            "publish_artifact for them."
        )
    return json.dumps(payload, ensure_ascii=False, default=str)
