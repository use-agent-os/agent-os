"""``POST /api/v1/rpc``: an allowlisted slice of the RPC surface over plain HTTP.

Scripts and the desktop's ``fetch()`` calls can reach a gateway method
without a WebSocket. The request is ``{id?, method, params?}`` and the
answer ``{id, ok, payload}`` or ``{id, ok: false, error}`` — the WebSocket
``res`` frame without its ``type``. It runs behind the normal bearer auth
(``AuthMiddleware``) and the cross-origin and rate-limit guards, and goes
through the same dispatcher as a WebSocket request on the Control surface,
so a handler cannot tell the two transports apart.

Only :data:`HTTP_RPC_METHODS` are reachable. The list is deliberately short:
reads, plus mutations that create, rename or send without deleting,
reconfiguring or spending from a wallet. ``chat.send`` / ``sessions.send``
start an agent turn exactly as over the WebSocket (and as ``POST
/api/chat`` already did), elevated execution modes included. Configuration
writes, secrets and environment variables, trading and wallets, restarts,
deletes and approval decisions stay on the WebSocket, where a client has
held one authenticated connection since the handshake. Anything else is
``403 FORBIDDEN``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from pydantic_core import to_jsonable_python
from starlette.requests import Request
from starlette.responses import JSONResponse

from agentos.gateway.protocol import (
    MAX_PAYLOAD_BYTES,
    ErrorCode,
    ErrorDetailCode,
    ErrorShape,
    ResFrame,
)

#: The methods ``POST /api/v1/rpc`` dispatches. Additions must be reads or
#: mutations that delete and reconfigure nothing; ``tests/test_gateway/test_http_rpc.py`` guards
#: the excluded families.
HTTP_RPC_METHODS: frozenset[str] = frozenset(
    {
        # Gateway and health
        "health",
        "status",
        "gateway.identity.get",
        "last-heartbeat",
        "doctor.status",
        "doctor.memory.status",
        "diagnostics.status",
        "logs.status",
        "updates.status",
        "auth.status",
        # Configuration (read-only, redacted)
        "config.get",
        "config.schema",
        "config.schema.lookup",
        # Sessions and chat
        "sessions.list",
        "sessions.get",
        "sessions.preview",
        "sessions.resolve",
        "sessions.create",
        "sessions.rename",
        "sessions.pin",
        "sessions.send",
        "sessions.abort",
        "chat.history",
        "chat.send",
        "chat.abort",
        "plan.mode.get",
        "plan.mode.set",
        "router.hold.get",
        # Agents, projects, skills, tools, models
        "agents.list",
        "agent.identity.get",
        "projects.list",
        "projects.get",
        "projects.create",
        "projects.update",
        "skills.list",
        "skills.get",
        "skills.status",
        "tools.catalog",
        "tools.effective",
        "models.list",
        "providers.status",
        "commands.list_for_surface",
        # Usage, scheduling, channels, integrations
        "usage.status",
        "usage.cost",
        "cron.list",
        "cron.status",
        "cron.runs",
        "channels.status",
        "acp.agents",
        "acp.sessions",
        "acp.status",
        "mcp.status",
        "search.status",
        "memory.search",
        "approval.list",
        "exec.approvals.get",
        "onboarding.status",
    }
)


def _error_body(req_id: Any, error: ErrorShape) -> dict[str, Any]:
    return {"id": req_id, "ok": False, "error": error.model_dump(mode="json")}


def _refuse(
    req_id: Any,
    status: int,
    code: ErrorCode,
    message: str,
    detail: ErrorDetailCode | None = None,
    **extra: Any,
) -> JSONResponse:
    details: dict[str, Any] | None = None
    if detail is not None:
        details = {"code": detail.value, **extra}
    shape = ErrorShape(code=code.value, message=message, details=details, retryable=False)
    return JSONResponse(_error_body(req_id, shape), status_code=status)


async def handle_http_rpc(
    request: Request,
    *,
    dispatcher: Any,
    make_ctx: Callable[[Request], Any],
    status_for: Callable[[ResFrame, int], int],
) -> JSONResponse:
    """Validate one ``POST /api/v1/rpc`` body and dispatch it."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_PAYLOAD_BYTES:
        return _refuse(
            None,
            413,
            ErrorCode.INVALID_REQUEST,
            "Request body exceeds the payload limit",
            ErrorDetailCode.PAYLOAD_TOO_LARGE,
            limit_bytes=MAX_PAYLOAD_BYTES,
        )
    raw = await request.body()
    if len(raw) > MAX_PAYLOAD_BYTES:
        return _refuse(
            None,
            413,
            ErrorCode.INVALID_REQUEST,
            "Request body exceeds the payload limit",
            ErrorDetailCode.PAYLOAD_TOO_LARGE,
            limit_bytes=MAX_PAYLOAD_BYTES,
        )
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _refuse(
            None, 400, ErrorCode.INVALID_REQUEST, "Invalid JSON body", ErrorDetailCode.INVALID_FRAME
        )
    if not isinstance(body, dict):
        return _refuse(
            None,
            400,
            ErrorCode.INVALID_REQUEST,
            "Body must be an object {id?, method, params?}",
            ErrorDetailCode.INVALID_FRAME,
        )

    req_id = body.get("id")
    if req_id is not None and (isinstance(req_id, bool) or not isinstance(req_id, str | int)):
        return _refuse(
            None,
            400,
            ErrorCode.INVALID_REQUEST,
            "id must be a string or an integer",
            ErrorDetailCode.INVALID_FRAME,
        )
    method = body.get("method")
    if not isinstance(method, str) or not method:
        return _refuse(
            req_id,
            400,
            ErrorCode.INVALID_REQUEST,
            "method must be a non-empty string",
            ErrorDetailCode.INVALID_FRAME,
        )
    params = body.get("params")
    if params is not None and not isinstance(params, dict):
        return _refuse(
            req_id,
            400,
            ErrorCode.INVALID_REQUEST,
            "params must be an object",
            ErrorDetailCode.INVALID_FRAME,
        )
    if method not in HTTP_RPC_METHODS:
        return _refuse(
            req_id,
            403,
            ErrorCode.FORBIDDEN,
            f"Method {method} is not available over HTTP; use the WebSocket",
            ErrorDetailCode.METHOD_NOT_ALLOWED_OVER_HTTP,
            method=method,
        )

    ctx = make_ctx(request)
    result: ResFrame = await dispatcher.dispatch(
        "http" if req_id is None else str(req_id), method, params, ctx
    )
    if result.ok:
        payload = to_jsonable_python(result.payload, fallback=str)
        return JSONResponse({"id": req_id, "ok": True, "payload": payload})
    error = result.error or ErrorShape(code=ErrorCode.INTERNAL_ERROR.value, message="error")
    return JSONResponse(_error_body(req_id, error), status_code=status_for(result, 500))


__all__ = ["HTTP_RPC_METHODS", "handle_http_rpc"]
