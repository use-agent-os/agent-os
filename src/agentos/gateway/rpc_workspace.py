"""RPC handlers for the operator's read-only workspace file browser.

``workspace.files.list`` lists one directory of an agent's workspace,
``workspace.files.read`` returns a text file, ``workspace.files.download``
mints a ticketed URL (``gateway.download_grants``) for an image or a PDF, and
``workspace.files.resolve`` maps a path a tool call carried to the browser's
relative form. There is no write method. The filesystem rules are in
``agentos.gateway.workspace_files``.

All four are Control-surface methods and are refused to a connection the
gateway recognizes as an agent's: an agent reads files through its file tool,
under that tool's policy and approvals, not through the operator's browser.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio

from agentos.agents.scope import resolve_agent_workspace_dir
from agentos.gateway.access import CONTROL_ONLY
from agentos.gateway.protocol import ErrorCode
from agentos.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from agentos.gateway.workspace_files import (
    MAX_DIR_ENTRIES,
    MAX_IMAGE_BYTES,
    MAX_PDF_BYTES,
    MAX_TEXT_BYTES,
    WorkspaceFileError,
    list_directory,
    parse_relative_path,
    read_text,
    relative_location,
    stat_download,
)
from agentos.session.keys import normalize_agent_id

_d = get_dispatcher()

#: Connection ids that are not a WebSocket: a download grant bound to one
#: would never be revoked by a disconnect.
_NON_SOCKET_CONN_IDS = frozenset({"", "http", "_http", "channel", "system"})


def _refuse_agent_caller(ctx: RpcContext) -> None:
    from agentos.gateway.agent_surface import recognized_agent

    binding = recognized_agent(ctx)
    if binding is not None:
        raise RpcHandlerError(
            "agent.operator_required",
            "The workspace file browser is the user's; an agent reads files with its file tools.",
            details=binding.to_dict(),
        )


def _agent_id(params: dict[str, Any]) -> str:
    raw = params.get("agentId", params.get("agent_id"))
    if raw is None or raw == "":
        return "main"
    if not isinstance(raw, str):
        raise ValueError("params.agentId must be a string")
    return normalize_agent_id(raw)


def workspace_root(config: Any, agent_id: str) -> Path:
    """The directory the browser is confined to for ``agent_id``."""
    return resolve_agent_workspace_dir(agent_id, config).expanduser()


def _params(params: Any) -> dict[str, Any]:
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    return params


def to_rpc_error(exc: WorkspaceFileError) -> RpcHandlerError:
    """One error shape for every refusal: ``details.code`` is what the UI reads."""
    code = ErrorCode.NOT_FOUND if exc.code == "not_found" else ErrorCode.INVALID_REQUEST
    return RpcHandlerError(code, exc.message, details={"code": f"workspace.{exc.code}"})


@_d.method("workspace.files.list", CONTROL_ONLY)
async def _handle_workspace_files_list(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """List one directory. Params: ``path`` (relative, ``""`` for the root), ``agentId``."""
    _refuse_agent_caller(ctx)
    body = _params(params)
    agent_id = _agent_id(body)
    root = workspace_root(ctx.config, agent_id)
    try:
        parts = parse_relative_path(body.get("path"))
        listing = await anyio.to_thread.run_sync(list_directory, root, parts)
    except WorkspaceFileError as exc:
        raise to_rpc_error(exc) from exc
    return {
        **listing,
        "agentId": agent_id,
        "limits": {
            "entries": MAX_DIR_ENTRIES,
            "textBytes": MAX_TEXT_BYTES,
            "imageBytes": MAX_IMAGE_BYTES,
            "pdfBytes": MAX_PDF_BYTES,
        },
    }


@_d.method("workspace.files.read", CONTROL_ONLY)
async def _handle_workspace_files_read(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Return a text file's content. Params: ``path`` (relative), ``agentId``."""
    _refuse_agent_caller(ctx)
    body = _params(params)
    agent_id = _agent_id(body)
    root = workspace_root(ctx.config, agent_id)
    try:
        parts = parse_relative_path(body.get("path"))
        result = await anyio.to_thread.run_sync(read_text, root, parts)
    except WorkspaceFileError as exc:
        raise to_rpc_error(exc) from exc
    return {**result, "agentId": agent_id}


@_d.method("workspace.files.resolve", CONTROL_ONLY)
async def _handle_workspace_files_resolve(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Map a path a tool call carried to the browser's relative form.

    Params: ``path`` (relative, absolute, ``~/...`` or a ``/workspace/...``
    alias, as the file tools accept it), ``agentId``. Answers ``path: null``
    with a ``reason`` (``outside``, ``denied``, ``invalid_path``) instead of an
    error: a transcript names files the browser cannot show, and that is not
    a failure. Purely lexical; it reads nothing in the workspace.
    """
    _refuse_agent_caller(ctx)
    body = _params(params)
    agent_id = _agent_id(body)
    root = workspace_root(ctx.config, agent_id)
    try:
        relative: str | None = relative_location(body.get("path"), root)
        reason = None
    except WorkspaceFileError as exc:
        relative, reason = None, exc.code
    payload: dict[str, Any] = {"agentId": agent_id, "path": relative}
    if reason is not None:
        payload["reason"] = reason
    return payload


@_d.method("workspace.files.download", CONTROL_ONLY)
async def _handle_workspace_files_download(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Mint a ticketed URL for an image or a PDF. Params: ``path``, ``agentId``.

    The URL is bound to this WebSocket connection, expires with the ticket
    and is re-checked against the workspace rules on every request.
    """
    from agentos.gateway.download_grants import get_download_grants

    _refuse_agent_caller(ctx)
    body = _params(params)
    conn_id = str(getattr(ctx, "conn_id", "") or "")
    if conn_id in _NON_SOCKET_CONN_IDS:
        raise ValueError(
            "workspace.files.download needs a WebSocket connection: the URL is bound to it"
        )
    agent_id = _agent_id(body)
    root = workspace_root(ctx.config, agent_id)
    try:
        parts = parse_relative_path(body.get("path"))
        info = await anyio.to_thread.run_sync(stat_download, root, parts)
    except WorkspaceFileError as exc:
        raise to_rpc_error(exc) from exc
    grants = get_download_grants()
    # ``session_key`` carries the agent id for this kind: the route resolves
    # the workspace root from it again.
    grant = grants.mint(
        conn_id, "workspace", "/".join(parts), agent_id, name=info.name, mime=info.mime
    )
    return {
        "url": grant.path,
        "kind": info.kind,
        "name": info.name,
        "mime": info.mime,
        "size": info.size,
        "expiresAt": int(grant.expires_at * 1000),
        "ttlSeconds": int(grants.ttl_seconds),
    }
