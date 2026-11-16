"""RPC surface for MCP server status, live connection, and OAuth authorization."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from agentos.gateway.agent_surface import recognized_agent
from agentos.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher

# The module, not its names: ``agentos.mcp.discovery`` imports the tool
# registry, whose package import loads the builtin tools, and two of those
# (``agents``, ``sessions``) import the gateway -- which imports this module.
# When discovery is the first thing a process imports, it is still partially
# initialized here, so a ``from ... import name`` would fail and drop those
# builtin tools. Attribute lookups at call time see the finished module.
from agentos.mcp import discovery as mcp_discovery
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


def _require_operator(ctx: RpcContext, action: str) -> None:
    """Refuse a connection the gateway recognizes as an agent's.

    Connecting a stdio server makes the gateway fork the configured command,
    with the gateway's own environment, outside the sandbox and the approval
    queue -- the same property that makes ``hooks.lifecycle`` operator-only.
    ``mcp`` is an operator-only config root, so an agent cannot register a
    server; it cannot launch, stop or authorize one either. ``unbound`` is not
    refused: the user's own web console on loopback is unbound.
    """
    binding = recognized_agent(ctx)
    if binding is not None:
        raise RpcHandlerError(
            "mcp.operator_required",
            f"{action} is the user's action; an agent cannot do it. "
            "Ask the user to do it in the app.",
            details=binding.to_dict(),
        )


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
    return MCPServerConfig.from_entry(
        entry,
        state_dir=ctx.config.state_dir,
        oauth_redirect_uri=redirect_uri,
        defaults=ctx.config.mcp,
    )


def _forget_supervision(name: str) -> None:
    """The operator is taking *name* over: stop any background retry or watch."""
    from agentos.mcp.supervisor import get_supervisor

    supervisor = get_supervisor()
    if supervisor is not None:
        supervisor.forget(name)


async def _activate(ctx: RpcContext, config: MCPServerConfig) -> list[str]:
    _require_mcp_enabled(ctx)
    if ctx.tool_registry is None:
        raise ValueError("Tool registry unavailable")
    if not config.enabled:
        raise ValueError(f"MCP server is disabled: {config.name}")
    _forget_supervision(config.name)
    await mcp_discovery.disconnect_and_unregister(config.name, ctx.tool_registry)
    timeout = config.effective_connect_timeout(float(ctx.config.mcp.connect_timeout_seconds))
    tools = await asyncio.wait_for(
        mcp_discovery.discover_and_register(config, ctx.tool_registry, owner=config.name),
        timeout=max(timeout, config.tool_timeout_seconds),
    )
    from agentos.mcp.supervisor import get_supervisor

    supervisor = get_supervisor()
    if supervisor is not None:
        supervisor.watch(config)
    return tools


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


@_d.method("mcp.status")
async def _handle_mcp_status(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    del params
    if ctx.config is None:
        return {"enabled": False, "servers": []}
    from agentos.mcp.supervisor import get_supervisor

    supervisor = get_supervisor()
    active = {entry.server_name: entry for entry in mcp_discovery.active_clients_snapshot()}
    servers = []
    for entry in ctx.config.mcp.servers:
        runtime = _runtime_config(ctx, entry)
        connected = entry.name in active
        retry = supervisor.retry_state(entry.name) if supervisor is not None else None
        servers.append(
            {
                "name": entry.name,
                "enabled": runtime.enabled,
                "transport": entry.transport,
                "url": entry.url,
                "oauth": entry.oauth,
                "authenticated": await _authenticated(runtime),
                "connected": connected,
                "tools": list(active[entry.name].registered_tools) if connected else [],
                # Registered AgentOS name -> the server's own tool name. They
                # differ when another server already held the short name.
                "toolMap": active[entry.name].tool_map() if connected else {},
                "toolFilter": {
                    "include": list(runtime.tool_filter.include),
                    "exclude": list(runtime.tool_filter.exclude),
                },
                # Whether the server's tools wait behind ``tool_search``, and
                # what else the agent was given for it.
                "deferLoading": entry.defer_loading,
                "deferred": active[entry.name].deferred if connected else False,
                "hasInstructions": bool(active[entry.name].instructions) if connected else False,
                "resources": active[entry.name].has_resources if connected else False,
                "prompts": active[entry.name].has_prompts if connected else False,
                # Set while the server waits to reconnect after a failed
                # connect or a crash: attempts, last error, next retry time.
                "retry": retry.to_dict() if retry is not None else None,
            }
        )
    return {"enabled": ctx.config.mcp.enabled, "servers": servers}


@_d.method("mcp.connect")
async def _handle_mcp_connect(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    _require_operator(ctx, "mcp.connect")
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


@_d.method("mcp.disconnect")
async def _handle_mcp_disconnect(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    _require_operator(ctx, "mcp.disconnect")
    name = str((params or {}).get("name") or "").strip()
    if not name:
        raise ValueError("params.name is required")
    if ctx.tool_registry is None:
        raise ValueError("Tool registry unavailable")
    _forget_supervision(name)
    closed = await mcp_discovery.disconnect_and_unregister(name, ctx.tool_registry)
    return {"disconnected": bool(closed)}


@_d.method("mcp.oauth.start")
async def _handle_mcp_oauth_start(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    _require_operator(ctx, "mcp.oauth.start")
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


@_d.method("mcp.oauth.complete")
async def _handle_mcp_oauth_complete(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    _require_operator(ctx, "mcp.oauth.complete")
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


@_d.method("mcp.oauth.clear")
async def _handle_mcp_oauth_clear(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    _require_operator(ctx, "mcp.oauth.clear")
    name = str((params or {}).get("name") or "").strip()
    if not name:
        raise ValueError("params.name is required")
    entry = _server_entry(ctx, name)
    config = _runtime_config(ctx, entry)
    if config.url:
        FileOAuthStorage(config.name, config.url, config.state_dir).clear()
    _forget_supervision(name)
    if ctx.tool_registry is not None:
        await mcp_discovery.disconnect_and_unregister(name, ctx.tool_registry)
    return {"cleared": True}
