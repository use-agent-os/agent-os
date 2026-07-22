"""RPC surface for MCP server status, live connection, and OAuth authorization."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.mcp.discovery import (
    active_clients_snapshot,
    disconnect_and_unregister,
    discover_and_register,
)
from agentos.mcp.streamable_http import FileOAuthStorage, MCPStreamableHTTPClient
from agentos.mcp.types import MCPServerConfig

_d = get_dispatcher()


@dataclass
class _PendingOAuth:
    server_name: str
    config: MCPServerConfig
    auth_url: asyncio.Future[str]
    callback: asyncio.Future[tuple[str, str | None]]
    task: asyncio.Task[list[str]] | None = None
    state: str | None = None


_pending_by_state: dict[str, _PendingOAuth] = {}
_pending_by_server: dict[str, _PendingOAuth] = {}


def _require_mcp_enabled(ctx: RpcContext) -> None:
    if ctx.config is None:
        raise ValueError("No config available")
    if not ctx.config.mcp.enabled:
        raise ValueError("MCP runtime is disabled")


def _server_entry(ctx: RpcContext, name: str) -> Any:
    if ctx.config is None:
        raise ValueError("No config available")
    for entry in ctx.config.mcp.servers:
        if entry.name == name:
            return entry
    raise ValueError(f"MCP server not found: {name}")


def _runtime_config(
    ctx: RpcContext, entry: Any, redirect_uri: str | None = None
) -> MCPServerConfig:
    return MCPServerConfig(
        name=entry.name,
        transport=entry.transport,
        command=entry.command,
        args=list(entry.args),
        url=entry.url,
        env=dict(entry.env),
        headers=dict(entry.headers),
        oauth=entry.oauth,
        oauth_redirect_uri=redirect_uri,
        state_dir=ctx.config.state_dir,
        tool_timeout_seconds=entry.tool_timeout_seconds,
    )


async def _activate(ctx: RpcContext, config: MCPServerConfig) -> list[str]:
    _require_mcp_enabled(ctx)
    if ctx.tool_registry is None:
        raise ValueError("Tool registry unavailable")
    await disconnect_and_unregister(config.name, ctx.tool_registry)
    timeout = float(ctx.config.mcp.connect_timeout_seconds)
    return await asyncio.wait_for(
        discover_and_register(config, ctx.tool_registry, owner=config.name),
        timeout=max(timeout, config.tool_timeout_seconds),
    )


async def _authenticate_and_activate(ctx: RpcContext, flow: _PendingOAuth) -> list[str]:
    async def redirect_handler(url: str) -> None:
        state = parse_qs(urlparse(url).query).get("state", [""])[0]
        if not state:
            raise ValueError("OAuth provider returned an authorization URL without state")
        flow.state = state
        _pending_by_state[state] = flow
        if not flow.auth_url.done():
            flow.auth_url.set_result(url)

    async def callback_handler() -> tuple[str, str | None]:
        return await flow.callback

    try:
        client = MCPStreamableHTTPClient(
            flow.config,
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
        )
        try:
            await client.connect()
        finally:
            await client.close()
        return await _activate(ctx, flow.config)
    finally:
        if flow.state and _pending_by_state.get(flow.state) is flow:
            _pending_by_state.pop(flow.state, None)
        if _pending_by_server.get(flow.server_name) is flow:
            _pending_by_server.pop(flow.server_name, None)


async def _authenticated(config: MCPServerConfig) -> bool:
    if not config.oauth or not config.url:
        return False
    return await FileOAuthStorage(config.name, config.url, config.state_dir).is_authenticated()


@_d.method("mcp.status", scope="operator.read")
async def _handle_mcp_status(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    del params
    if ctx.config is None:
        return {"enabled": False, "servers": []}
    active = {entry.server_name: entry for entry in active_clients_snapshot()}
    servers = []
    for entry in ctx.config.mcp.servers:
        runtime = _runtime_config(ctx, entry)
        connected = entry.name in active
        servers.append(
            {
                "name": entry.name,
                "transport": entry.transport,
                "url": entry.url,
                "oauth": entry.oauth,
                "authenticated": await _authenticated(runtime),
                "connected": connected,
                "tools": list(active[entry.name].registered_tools) if connected else [],
            }
        )
    return {"enabled": ctx.config.mcp.enabled, "servers": servers}


@_d.method("mcp.connect", scope="operator.admin")
async def _handle_mcp_connect(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    _require_mcp_enabled(ctx)
    name = str((params or {}).get("name") or "").strip()
    if not name:
        raise ValueError("params.name is required")
    entry = _server_entry(ctx, name)
    config = _runtime_config(ctx, entry)
    if config.oauth and not await _authenticated(config):
        return {"connected": False, "authorizationRequired": True, "tools": []}
    tools = await _activate(ctx, config)
    return {"connected": True, "authorizationRequired": False, "tools": tools}


@_d.method("mcp.disconnect", scope="operator.admin")
async def _handle_mcp_disconnect(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    name = str((params or {}).get("name") or "").strip()
    if not name:
        raise ValueError("params.name is required")
    if ctx.tool_registry is None:
        raise ValueError("Tool registry unavailable")
    closed = await disconnect_and_unregister(name, ctx.tool_registry)
    return {"disconnected": bool(closed)}


@_d.method("mcp.oauth.start", scope="operator.admin")
async def _handle_mcp_oauth_start(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    _require_mcp_enabled(ctx)
    values = params or {}
    name = str(values.get("name") or "").strip()
    redirect_uri = str(values.get("redirectUri") or "").strip()
    if not name or not redirect_uri:
        raise ValueError("params.name and params.redirectUri are required")
    parsed = urlparse(redirect_uri)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("redirectUri must be an absolute HTTP(S) URL")

    entry = _server_entry(ctx, name)
    config = _runtime_config(ctx, entry, redirect_uri)
    if config.transport != "streamable_http" or not config.oauth:
        raise ValueError("OAuth is only available for OAuth-enabled Streamable HTTP servers")

    previous = _pending_by_server.pop(name, None)
    if previous and previous.task and not previous.task.done():
        previous.task.cancel()
    loop = asyncio.get_running_loop()
    flow = _PendingOAuth(
        server_name=name,
        config=config,
        auth_url=loop.create_future(),
        callback=loop.create_future(),
    )
    _pending_by_server[name] = flow
    flow.task = asyncio.create_task(_authenticate_and_activate(ctx, flow))

    auth_wait = asyncio.create_task(asyncio.wait_for(asyncio.shield(flow.auth_url), 20.0))
    done, _ = await asyncio.wait({auth_wait, flow.task}, return_when=asyncio.FIRST_COMPLETED)
    if flow.task in done:
        auth_wait.cancel()
        with suppress(asyncio.CancelledError, TimeoutError):
            await auth_wait
        tools = await flow.task
        return {"connected": True, "authorizationRequired": False, "tools": tools}
    try:
        authorization_url = await auth_wait
    except BaseException:
        flow.task.cancel()
        with suppress(asyncio.CancelledError):
            await flow.task
        raise
    return {
        "connected": False,
        "authorizationRequired": True,
        "authorizationUrl": authorization_url,
        "state": flow.state,
    }


@_d.method("mcp.oauth.complete", scope="operator.admin")
async def _handle_mcp_oauth_complete(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    del ctx
    values = params or {}
    code = str(values.get("code") or "").strip()
    state = str(values.get("state") or "").strip()
    if not code or not state:
        raise ValueError("params.code and params.state are required")
    flow = _pending_by_state.get(state)
    if flow is None or flow.task is None:
        raise ValueError("OAuth authorization is no longer pending")
    if not flow.callback.done():
        flow.callback.set_result((code, state))
    try:
        tools = await asyncio.wait_for(flow.task, 60.0)
    finally:
        _pending_by_state.pop(state, None)
        _pending_by_server.pop(flow.server_name, None)
    return {"connected": True, "authorizationRequired": False, "tools": tools}


@_d.method("mcp.oauth.clear", scope="operator.admin")
async def _handle_mcp_oauth_clear(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    name = str((params or {}).get("name") or "").strip()
    if not name:
        raise ValueError("params.name is required")
    entry = _server_entry(ctx, name)
    config = _runtime_config(ctx, entry)
    if config.url:
        FileOAuthStorage(config.name, config.url, config.state_dir).clear()
    if ctx.tool_registry is not None:
        await disconnect_and_unregister(name, ctx.tool_registry)
    return {"cleared": True}
