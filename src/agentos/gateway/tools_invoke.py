"""``POST /api/v1/tools/invoke`` -- run one tool without an LLM turn.

Scripts, CI and the desktop can call "search the web" or "read this memory"
directly. The call goes through the same chain as an agent's tool call:

1. default deny: only tools whose registration declares them read-only run,
   on top of a name floor no configuration widens -- shell and code
   execution, file and memory writes, session spawning and sending,
   scheduling, gateway control, outbound messages, paid media generation,
   browser and routing control, trading -- except tool by tool through
   ``[tools.invoke] allow``; a denied or unknown tool answers ``404`` and the
   two are indistinguishable. Arguments that write a file (``output_path``
   and friends) are refused for every tool;
2. the configured policy (``[tools]`` profile / allow / deny, the agent's own
   entry) and the runtime capability surface, resolved exactly as for a turn;
3. the dispatch preflight (injection guard, policy chain) -- a refusal is
   ``403``;
4. the tool itself, inside an unattended, sandboxed tool context. A tool that
   needs a human approval does what it does on any unattended run: it
   refuses, and the endpoint answers ``403`` with ``details.code =
   APPROVAL_REQUIRED`` (and the ``approval_id`` when the tool queued one).

``dry_run`` stops after step 3 and returns the resolved policy.
``idempotency_key`` replays the first response for the same request for ten
minutes and answers ``409`` with ``details.code = REQUEST_CONFLICT`` when the
key is reused for a different one.

Errors have the gateway's typed shape: ``code`` is a category
(:class:`~agentos.gateway.protocol.ErrorCode`) and ``details.code`` the
specific reason (:class:`~agentos.gateway.protocol.ErrorDetailCode`).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import structlog
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agentos.gateway.config import GatewayConfig
from agentos.gateway.protocol import ErrorCode, ErrorDetailCode

log = structlog.get_logger(__name__)

INVOKE_PATH = "/api/v1/tools/invoke"
IDEMPOTENCY_TTL_SECONDS = 600.0
IDEMPOTENCY_MAX_ENTRIES = 1024
DEFAULT_INVOKE_TIMEOUT_SECONDS = 120.0

#: Tools the endpoint never runs unless ``[tools.invoke] allow`` names them.
HARD_DENY_TOOLS: frozenset[str] = frozenset(
    {
        # Shell, terminal and code execution.
        "exec",
        "exec_command",
        "bash",
        "shell",
        "terminal",
        "process",
        "background_process",
        "execute_code",
        # File writes, deletes and moves.
        "write_file",
        "edit_file",
        "apply_patch",
        "delete_file",
        "move_file",
        "git_commit",
        "memory_save",
        "memory_delete",
        "skill_create",
        "skill_edit",
        "skill_delete",
        "skill_install_community",
        "install_skill_deps",
        "create_csv",
        "create_pdf_report",
        "create_pptx",
        "create_xlsx",
        "publish_artifact",
        "env_set",
        # Sessions, agents and outbound delivery.
        "sessions_spawn",
        "sessions_send",
        "sessions_yield",
        "subagents",
        "acp",
        "message",
        # Scheduling and gateway control.
        "cron",
        "scheduler",
        "gateway",
        # Writes that do not look like file tools: the unified memory tool
        # (MEMORY.md feeds every system prompt), project and session state,
        # files written by a download, routing holds, and a browser.
        "memory",
        "http_request",
        "projects_create",
        "projects_update",
        "projects_move_session",
        "session_rename",
        "router_control",
        "browser",
        "dubbing_download",
        # Paid media generation.
        "image_generate",
        "music_generate",
        "song_generate",
        "tts",
        "voice_clone",
        "voice_convert",
        "dubbing_generate",
        # Turn control that needs a live conversation.
        "ask_user",
        "exit_plan_mode",
    }
)
#: Name prefixes that are always hard-denied (trading writes, any future
#: scheduler variant).
HARD_DENY_PREFIXES: tuple[str, ...] = ("trade", "trading", "cron_", "scheduler_")

#: Arguments that make a tool write a file, refused whatever the tool.
FILE_WRITE_ARGUMENTS: frozenset[str] = frozenset(
    {
        "output_path",
        "output_file",
        "out_path",
        "save_path",
        "save_to",
        "download_path",
        "dest_path",
        "destination",
    }
)

_APPROVAL_STATUSES = frozenset({"approval_pending", "approval_required", "pending_approval"})
#: How an unattended refusal comes back: the dispatcher's notice that replaced
#: a queued approval, or a tool (the shell) refusing before it queues one.
_UNATTENDED_REFUSALS = frozenset({"UnsupportedSurface", "UnsupportedSurfaceError"})


def is_hard_denied(tool: str, allow: list[str] | tuple[str, ...] = ()) -> bool:
    """The name floor: tools never run here unless ``allow`` names them."""
    if tool in allow:
        return False
    return tool in HARD_DENY_TOOLS or tool.startswith(HARD_DENY_PREFIXES)


def invocable(tool: str, registered: Any, allow: list[str] | tuple[str, ...] = ()) -> bool:
    """Whether the endpoint may run ``tool`` at all.

    Default deny: besides the name floor, a tool runs only when its
    registration declares it read-only (``ToolSpec.read_only``). A tool that
    may write, send, spend or change state -- including every MCP tool and
    anything registered later without the flag -- needs ``[tools.invoke]
    allow``.
    """
    if registered is None:
        return False
    if tool in allow:
        return True
    if is_hard_denied(tool):
        return False
    return bool(getattr(getattr(registered, "spec", None), "read_only", False))


def _invoke_allow(config: Any) -> list[str]:
    tools = getattr(config, "tools", None)
    invoke = getattr(tools, "invoke", None)
    return [str(name) for name in (getattr(invoke, "allow", None) or [])]


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@dataclass
class _IdempotencyEntry:
    fingerprint: str
    created_at: float
    future: asyncio.Future[tuple[int, dict[str, Any]]] = field(repr=False)


class IdempotencyCache:
    def __init__(
        self,
        *,
        ttl_seconds: float = IDEMPOTENCY_TTL_SECONDS,
        max_entries: int = IDEMPOTENCY_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        self._entries: OrderedDict[str, _IdempotencyEntry] = OrderedDict()

    def _sweep(self) -> None:
        now = self._clock()
        for key in [k for k, e in self._entries.items() if now - e.created_at >= self._ttl]:
            self._entries.pop(key, None)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    def claim(
        self, key: str, fingerprint: str
    ) -> tuple[str, asyncio.Future[tuple[int, dict[str, Any]]]]:
        """``("new", future)`` to run and fill, ``("replay", future)``, or ``("conflict", ...)``."""
        self._sweep()
        entry = self._entries.get(key)
        if entry is not None:
            if entry.fingerprint != fingerprint:
                return "conflict", entry.future
            return "replay", entry.future
        future: asyncio.Future[tuple[int, dict[str, Any]]] = (
            asyncio.get_running_loop().create_future()
        )
        self._entries[key] = _IdempotencyEntry(fingerprint, self._clock(), future)
        return "new", future

    def forget(self, key: str) -> None:
        self._entries.pop(key, None)


def request_fingerprint(body: dict[str, Any]) -> str:
    material = {
        "tool": body.get("tool"),
        "action": body.get("action"),
        "args": body.get("args"),
        "session_key": body.get("session_key") or body.get("sessionKey"),
        "dry_run": bool(body.get("dry_run") or body.get("dryRun")),
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Tool context and policy
# ---------------------------------------------------------------------------


def _error(
    status: int,
    code: ErrorCode,
    message: str,
    detail: ErrorDetailCode | None = None,
    **details: Any,
) -> tuple[int, dict[str, Any]]:
    """``(status, body)`` in the typed shape: a category in ``code``, the
    reason and its data in ``details``."""
    body: dict[str, Any] = {"ok": False, "error": message, "code": code.value}
    extra = {key: value for key, value in details.items() if value is not None}
    if detail is not None:
        extra = {"code": detail.value, **extra}
    if extra:
        body["details"] = extra
    return status, body


def _respond(
    status: int, code: ErrorCode, message: str, detail: ErrorDetailCode | None = None
) -> JSONResponse:
    status, body = _error(status, code, message, detail)
    return JSONResponse(body, status_code=status)


def _not_found() -> tuple[int, dict[str, Any]]:
    return _error(404, ErrorCode.NOT_FOUND, "Tool not found")


def build_invoke_tool_context(
    config: GatewayConfig,
    registry: Any,
    *,
    session_key: str | None,
    runtime: dict[str, Any],
) -> Any:
    """The tool context a turn would get for this caller, resolved the same way."""
    from agentos.agents.scope import resolve_agent_workspace_dir
    from agentos.session.keys import parse_agent_id
    from agentos.tools.policy import apply_tool_policy_from_config
    from agentos.tools.policy_runtime import (
        resolve_runtime_tool_surface,
        tool_surface_capabilities_from_runtime,
    )
    from agentos.tools.types import CallerKind, InteractionMode, ToolContext

    agent_id = parse_agent_id(session_key) if session_key else "main"
    workspace_dir = resolve_agent_workspace_dir(agent_id, config)
    workspace_strict = getattr(config, "workspace_strict", None)
    if not isinstance(workspace_strict, bool):
        workspace_strict = bool(workspace_dir)
    ctx = ToolContext(
        caller_kind=CallerKind.CLI,
        # Nobody is watching an HTTP call: approval-gated tools refuse
        # instead of parking a prompt the caller cannot answer.
        interaction_mode=InteractionMode.UNATTENDED,
        agent_id=agent_id,
        workspace_dir=str(workspace_dir),
        workspace_strict=workspace_strict,
        session_key=session_key,
        channel_kind="http",
        channel_id="tools.invoke",
        sender_id="tools-invoke",
        source_kind="http",
        source_name="tools.invoke",
    )
    ctx = apply_tool_policy_from_config(ctx, available_tools=registry.list_names(), config=config)
    capabilities = tool_surface_capabilities_from_runtime(
        session_manager=runtime.get("session_manager"),
        task_runtime=runtime.get("task_runtime"),
        scheduler=runtime.get("cron_scheduler"),
        gateway_config=config,
        channel_manager=runtime.get("channel_manager"),
    )
    return resolve_runtime_tool_surface(ctx, capabilities=capabilities)


def _policy_summary(ctx: Any, *, tool: str, override: bool, spec: Any) -> dict[str, Any]:
    from agentos.tools.registry import resolve_profile

    return {
        "tool": tool,
        "readOnly": bool(getattr(spec, "read_only", False)),
        "mayRequireApproval": bool(getattr(spec, "requires_approval", False)),
        "callerKind": str(ctx.caller_kind),
        "interactionMode": str(ctx.interaction_mode),
        "agentId": ctx.agent_id,
        "sessionKey": ctx.session_key,
        "profile": resolve_profile(ctx).value,
        "sandboxed": ctx.elevated is None,
        "invokeAllowOverride": override,
    }


def _envelope(result: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(getattr(result, "content", "") or "")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _result_value(content: str) -> Any:
    try:
        return json.loads(content)
    except (TypeError, ValueError):
        return content


async def invoke_tool(
    body: dict[str, Any],
    *,
    config: GatewayConfig,
    registry: Any,
    runtime: dict[str, Any],
    timeout: float | None = None,
) -> tuple[int, dict[str, Any]]:
    """Run (or dry-run) one tool call; returns ``(status, json body)``."""
    from agentos.tool_boundary import ToolCall
    from agentos.tools.argument_admission import REJECTED_ERROR_CLASS
    from agentos.tools.dispatch import build_tool_handler, preflight_tool_call

    tool = body.get("tool")
    if not isinstance(tool, str) or not tool.strip():
        return _error(400, ErrorCode.INVALID_REQUEST, "tool is required")
    tool = tool.strip()
    args = body.get("args", {})
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return _error(400, ErrorCode.INVALID_REQUEST, "args must be an object")
    action = body.get("action")
    if action is not None:
        if not isinstance(action, str) or not action.strip():
            return _error(400, ErrorCode.INVALID_REQUEST, "action must be a non-empty string")
        args = {**args, "action": action.strip()} if "action" not in args else args
    session_key = body.get("session_key", body.get("sessionKey"))
    if session_key is not None and (not isinstance(session_key, str) or not session_key.strip()):
        return _error(400, ErrorCode.INVALID_REQUEST, "session_key must be a non-empty string")
    session_key = session_key.strip() if isinstance(session_key, str) else None
    dry_run = bool(body.get("dry_run", body.get("dryRun", False)))

    allow = _invoke_allow(config)
    if not invocable(tool, registry.get(tool), allow):
        log.info("tools_invoke.not_found", tool=tool)
        return _not_found()
    writes = sorted(FILE_WRITE_ARGUMENTS & set(args))
    if writes:
        return _error(
            400,
            ErrorCode.INVALID_REQUEST,
            f"argument {writes[0]!r} writes a file, which this endpoint does not do",
        )

    ctx = build_invoke_tool_context(config, registry, session_key=session_key, runtime=runtime)
    call = ToolCall(tool_use_id=f"http-{uuid.uuid4().hex[:12]}", tool_name=tool, arguments=args)
    denial = await preflight_tool_call(registry=registry, ctx=ctx, tool_call=call)
    registered = registry.get(tool)
    policy = _policy_summary(
        ctx, tool=tool, override=tool in allow, spec=getattr(registered, "spec", None)
    )
    if dry_run:
        envelope = _envelope(denial) if denial is not None else {}
        return 200, {
            "ok": True,
            "dryRun": True,
            "allowed": denial is None,
            "reason": envelope.get("user_message") if denial is not None else None,
            "errorClass": envelope.get("error_class") if denial is not None else None,
            "policy": policy,
        }
    if denial is not None:
        envelope = _envelope(denial)
        if envelope.get("error_class") == REJECTED_ERROR_CLASS:
            # The arguments do not match the tool's schema: a bad request,
            # not a policy refusal.
            return _error(
                400,
                ErrorCode.INVALID_REQUEST,
                str(envelope.get("user_message") or "Invalid tool arguments."),
                error_class=REJECTED_ERROR_CLASS,
            )
        return _error(
            403,
            ErrorCode.FORBIDDEN,
            str(envelope.get("user_message") or "Tool not available in this context."),
            ErrorDetailCode.POLICY_DENIED,
            error_class=envelope.get("error_class"),
        )

    capture = _ApprovalCapture()
    handler = build_tool_handler(registry, ctx, tool_hooks=[capture])
    limit = timeout if timeout is not None else _tool_timeout(config)
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(handler(call), timeout=limit)
    except TimeoutError:
        return _error(
            504,
            ErrorCode.UNAVAILABLE,
            f"tool {tool!r} did not finish within {limit:.0f}s",
            ErrorDetailCode.TOOL_TIMEOUT,
        )
    duration_ms = int((time.monotonic() - started) * 1000)
    value = _result_value(result.content)
    approval = _approval_gate(result, value, capture.approval_id)
    if approval is not None:
        return approval
    log.info("tools_invoke.ran", tool=tool, is_error=result.is_error, duration_ms=duration_ms)
    payload: dict[str, Any] = {
        "ok": not result.is_error,
        "tool": tool,
        "result": value,
        "isError": result.is_error,
        "durationMs": duration_ms,
    }
    if result.artifacts:
        payload["artifacts"] = result.artifacts
    return 200, payload


class _ApprovalCapture:
    """Tool hook that keeps the approval id a tool returned.

    On an unattended surface the dispatcher replaces a queued-approval result
    with an ``UnsupportedSurface`` notice (the engine's own path) and drops
    the id; the raw result seen here still has it.
    """

    name = "tools_invoke_approval_capture"

    def __init__(self) -> None:
        self.approval_id: str | None = None

    def before_tool(self, call: Any) -> None:
        return None

    def after_tool(self, call: Any, outcome: Any) -> None:
        raw = getattr(outcome, "result", None)
        value = _result_value(raw) if isinstance(raw, str) else raw
        if isinstance(value, dict) and str(value.get("status") or "") in _APPROVAL_STATUSES:
            approval_id = value.get("approval_id") or value.get("approvalId")
            if approval_id:
                self.approval_id = str(approval_id)


def _approval_gate(
    result: Any, value: Any, captured_id: str | None
) -> tuple[int, dict[str, Any]] | None:
    """A tool that stopped for a human approval: ``403``, ``APPROVAL_REQUIRED``."""
    status = getattr(result, "execution_status", None) or {}
    pending = status.get("reason") == "approval_pending" if isinstance(status, dict) else False
    message = "This call needs a human approval."
    if isinstance(value, dict):
        message = str(value.get("user_message") or value.get("message") or message)
        if str(value.get("status") or "") in _APPROVAL_STATUSES:
            pending = True
            captured_id = captured_id or value.get("approval_id") or value.get("approvalId")
        elif result.is_error and value.get("error_class") in _UNATTENDED_REFUSALS:
            pending = pending or "approval" in message.lower()
    if not pending:
        return None
    return _error(
        403,
        ErrorCode.FORBIDDEN,
        message,
        ErrorDetailCode.APPROVAL_REQUIRED,
        approval_id=str(captured_id) if captured_id else None,
    )


def _tool_timeout(config: Any) -> float:
    value = getattr(config, "agent_tool_timeout_seconds", None)
    if isinstance(value, int | float) and value > 0:
        return float(value)
    return DEFAULT_INVOKE_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


def register_tools_invoke_route(
    app: Starlette,
    *,
    config: GatewayConfig,
    make_ctx: Callable[[Request], Any],
    tool_registry: Any = None,
    runtime: dict[str, Any] | None = None,
    cache: IdempotencyCache | None = None,
) -> None:
    """Register ``POST /api/v1/tools/invoke`` (Control surface, bearer auth)."""
    idempotency = cache or IdempotencyCache()
    runtime_objects = dict(runtime or {})

    async def handler(request: Request) -> JSONResponse:
        from agentos.gateway.access import ConnectionSurface
        from agentos.gateway.agent_surface import agent_binding

        rpc_ctx = make_ctx(request)
        access = rpc_ctx.access
        if not access.admitted or access.surface is not ConnectionSurface.CONTROL:
            return _respond(401, ErrorCode.UNAUTHORIZED, "Unauthorized")
        binding = agent_binding(rpc_ctx)
        if binding is not None and binding.via != "unbound":
            # A connection the gateway recognizes as an agent's (its token, an
            # open exec window) already has its tools under its own turn's
            # policy; a side door that resolves a fresh context is not one.
            # An unbound caller -- a script holding the gateway token -- is
            # who this endpoint is for.
            return _respond(403, ErrorCode.FORBIDDEN, "Not available to an agent")
        try:
            body = await request.json()
        except Exception:
            return _respond(400, ErrorCode.INVALID_REQUEST, "Invalid JSON body")
        if not isinstance(body, dict):
            return _respond(400, ErrorCode.INVALID_REQUEST, "body must be an object")
        from agentos.tools.registry import get_default_registry

        registry = tool_registry or get_default_registry()
        key = body.get("idempotency_key", body.get("idempotencyKey"))
        if key is not None and (not isinstance(key, str) or not key.strip() or len(key) > 200):
            return _respond(400, ErrorCode.INVALID_REQUEST, "invalid idempotency_key")

        async def run() -> tuple[int, dict[str, Any]]:
            return await invoke_tool(
                body, config=config, registry=registry, runtime=runtime_objects
            )

        if not key:
            status, payload = await run()
            return JSONResponse(payload, status_code=status)

        verdict, future = idempotency.claim(key.strip(), request_fingerprint(body))
        if verdict == "conflict":
            return _respond(
                409,
                ErrorCode.INVALID_REQUEST,
                "idempotency_key was already used for a different request",
                ErrorDetailCode.REQUEST_CONFLICT,
            )
        if verdict == "replay":
            status, payload = await asyncio.shield(future)
            return JSONResponse({**payload, "idempotentReplay": True}, status_code=status)
        try:
            status, payload = await run()
        except BaseException as exc:
            idempotency.forget(key.strip())
            if not future.done():
                future.set_exception(exc)
                future.exception()  # mark retrieved; waiters re-raise their own copy
            raise
        if not future.done():
            future.set_result((status, payload))
        return JSONResponse(payload, status_code=status)

    async def contained(request: Request) -> JSONResponse:
        # Failures are logged by type and redacted message: the default error
        # path prints traceback locals, the gateway config among them.
        try:
            return await handler(request)
        except Exception as exc:  # noqa: BLE001 - see comment
            from agentos.redact import redact_sensitive_text

            log.warning(
                "tools_invoke.failed",
                error_type=type(exc).__name__,
                error=(redact_sensitive_text(str(exc)) or "")[:300],
            )
            return _respond(500, ErrorCode.INTERNAL_ERROR, "The tool call could not be handled")

    app.router.routes.append(Route(INVOKE_PATH, contained, methods=["POST"]))


__all__ = [
    "HARD_DENY_PREFIXES",
    "HARD_DENY_TOOLS",
    "INVOKE_PATH",
    "IdempotencyCache",
    "build_invoke_tool_context",
    "invoke_tool",
    "FILE_WRITE_ARGUMENTS",
    "invocable",
    "is_hard_denied",
    "register_tools_invoke_route",
    "request_fingerprint",
]
