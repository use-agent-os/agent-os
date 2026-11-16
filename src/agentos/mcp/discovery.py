"""MCP tool discovery and registration into AgentOS ToolRegistry."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

import structlog

from agentos.mcp.client import MCPClient
from agentos.mcp.types import MCPServerConfig, MCPToolDef
from agentos.safety.injection_guard import wrap_untrusted_boundary
from agentos.tools.registry import ToolRegistry
from agentos.tools.schema_sanitize import sanitize_input_schema
from agentos.tools.types import SafeToolError, ToolHandler, ToolSpec

log = structlog.get_logger(__name__)

# The server's error text is quoted back to the model inside the failure
# envelope. That envelope caps its own message, but slicing here first keeps a
# server that returns a megabyte of stack trace from being scanned and escaped
# in full only to be thrown away.
_MCP_ERROR_TEXT_MAX_CHARS = 400

_SERVER_SLUG_RE = re.compile(r"[^a-z0-9_]+")


class MCPToolError(SafeToolError):
    """An MCP tool call the server reported as failed (``isError``) or that timed out.

    The message is shown to the model. For ``isError`` it quotes the server's
    own error text inside an untrusted envelope; AgentOS adds nothing it holds.
    """


def server_slug(server_name: str) -> str:
    """Reduce an MCP server id to ``[a-z0-9_]`` for use inside a tool name."""
    slug = _SERVER_SLUG_RE.sub("_", server_name.lower()).strip("_")
    return slug or "server"


def _wrap_untrusted(content: str, server_name: str, tool_name: str) -> str:
    """Quote server-authored text in the envelope web_fetch uses."""
    return wrap_untrusted_boundary(content, f"mcp:{server_name}/{tool_name}")


@dataclass(frozen=True)
class MCPToolRegistration:
    """One registry entry owned by an MCP client."""

    spec: ToolSpec
    handler: ToolHandler
    # The name the MCP server itself uses for the tool. ``spec.name`` is the
    # registered AgentOS name, which carries a server component when another
    # server already held the short one.
    server_tool_name: str = ""

    @property
    def name(self) -> str:
        return self.spec.name


@dataclass(frozen=True)
class ActiveMCPClient:
    """Tracked MCP client with the owner that controls its lifecycle."""

    owner: str
    server_name: str
    transport: str
    client: MCPClient
    registered_tools: tuple[str, ...] = ()
    tool_registrations: tuple[MCPToolRegistration, ...] = ()

    async def close(self) -> None:
        await self.client.close()

    def tool_map(self) -> dict[str, str]:
        """Registered AgentOS tool name -> the tool's name on the MCP server."""
        return {
            registration.name: registration.server_tool_name or registration.name
            for registration in self.tool_registrations
        }


# Module-level registry to keep clients alive for tool handlers.
_active_clients: list[ActiveMCPClient] = []


def active_clients_snapshot() -> tuple[ActiveMCPClient, ...]:
    """Return active MCP clients without exposing mutable runtime state."""
    return tuple(_active_clients)


async def close_active_clients(owner: str | None = None) -> int:
    """Close active MCP clients, optionally scoped to one owner/server name."""
    remaining: list[ActiveMCPClient] = []
    closing: list[ActiveMCPClient] = []
    for entry in _active_clients:
        if owner is None or entry.owner == owner or entry.server_name == owner:
            closing.append(entry)
        else:
            remaining.append(entry)
    _active_clients[:] = remaining

    closed = 0
    for entry in closing:
        try:
            await entry.close()
            closed += 1
        except Exception:
            pass
    return closed


async def disconnect_and_unregister(owner: str, registry: ToolRegistry) -> int:
    """Close one MCP server and remove the tools registered by that server."""
    entries = [
        entry
        for entry in active_clients_snapshot()
        if entry.owner == owner or entry.server_name == owner
    ]
    closing_ids = {id(entry) for entry in entries}
    remaining = [entry for entry in _active_clients if id(entry) not in closing_ids]
    affected_names = {
        registration.name for entry in entries for registration in entry.tool_registrations
    }
    for name in affected_names:
        current = registry.get(name)
        if current is None:
            continue
        current_is_closing = any(
            registration.name == name and registration.handler is current.handler
            for entry in entries
            for registration in entry.tool_registrations
        )
        if not current_is_closing:
            continue
        replacement = next(
            (
                registration
                for entry in reversed(remaining)
                for registration in reversed(entry.tool_registrations)
                if registration.name == name
            ),
            None,
        )
        registry.unregister(name)
        if replacement is not None:
            registry.register(replacement.spec, replacement.handler)
    return await close_active_clients(owner)


def create_client(config: MCPServerConfig) -> MCPClient:
    """Factory: create the appropriate MCPClient for the given transport."""
    if config.transport == "stdio":
        from agentos.mcp.stdio import MCPStdioClient

        return MCPStdioClient(config)
    elif config.transport == "sse":
        from agentos.mcp.sse import MCPSSEClient

        return MCPSSEClient(config)
    elif config.transport == "streamable_http":
        from agentos.mcp.streamable_http import MCPStreamableHTTPClient

        return MCPStreamableHTTPClient(config)
    else:
        raise ValueError(f"Unknown MCP transport: {config.transport!r}")


def _name_taken(name: str, registry: ToolRegistry, server_name: str) -> bool:
    """Whether ``name`` is held by something other than ``server_name`` itself.

    A server re-registering its own tools (discovery run again without a
    disconnect) keeps the names it already had rather than being pushed onto
    the long form by its own earlier registration.
    """
    if registry.get(name) is None:
        return False
    for entry in _active_clients:
        if entry.server_name != server_name:
            continue
        if any(registration.name == name for registration in entry.tool_registrations):
            return False
    return True


def resolve_tool_name(tool_name: str, server_name: str, registry: ToolRegistry) -> str:
    """Pick the AgentOS name for ``tool_name`` from ``server_name``.

    The short ``mcp_{tool}`` form is kept whenever it is free, so existing
    allowlists naming ``mcp_{tool}`` keep working. When another server already
    registered that name, this one gets ``mcp_{server}_{tool}`` (server id
    reduced to ``[a-z0-9_]``) instead of silently replacing it. Servers are
    discovered in config order, so the first configured server keeps the short
    name. Should the long form be taken too (two server ids that reduce to the
    same slug), a numeric suffix keeps it unique.
    """
    short = f"mcp_{tool_name}"
    if not _name_taken(short, registry, server_name):
        return short
    long_name = f"mcp_{server_slug(server_name)}_{tool_name}"
    candidate = long_name
    suffix = 2
    while _name_taken(candidate, registry, server_name):
        candidate = f"{long_name}_{suffix}"
        suffix += 1
    log.warning(
        "mcp.tool_name_collision",
        tool=tool_name,
        server=server_name,
        taken=short,
        registered_as=candidate,
    )
    return candidate


def filter_tools(config: MCPServerConfig, tools: list[MCPToolDef]) -> list[MCPToolDef]:
    """Drop the tools ``config.tool_filter`` rules out, before any is registered.

    Filtering on the server's own tool name, not the ``mcp_`` one, keeps a
    filter stable when a name collision moves a tool onto its long form.
    """
    tool_filter = config.tool_filter
    if not tool_filter.active:
        return tools
    kept = [tool for tool in tools if tool_filter.allows(tool.name)]
    if len(kept) != len(tools):
        log.info(
            "mcp.tools_filtered",
            server=config.name,
            kept=len(kept),
            dropped=sorted(tool.name for tool in tools if not tool_filter.allows(tool.name)),
        )
    return kept


def _mcp_error_message(server_name: str, tool_name: str, text: str) -> str:
    detail = text.strip()
    if len(detail) > _MCP_ERROR_TEXT_MAX_CHARS:
        detail = detail[:_MCP_ERROR_TEXT_MAX_CHARS] + "...[truncated]"
    prefix = f"MCP server {server_name!r} reported an error from tool {tool_name!r}"
    if not detail:
        return f"{prefix}."
    quoted = _wrap_untrusted(detail, server_name, tool_name)
    return f"{prefix}: {quoted}"


def _make_tool_handler(
    client: MCPClient,
    tool_name: str,
    tool_def: MCPToolDef,
    registry: ToolRegistry,
    timeout_seconds: float,
    *,
    server_name: str = "",
) -> MCPToolRegistration:
    """Register a single MCP tool into the registry under an ``mcp_`` name."""
    # The server's schema goes out verbatim in every provider request, so it is
    # normalized once here rather than per turn. A shape one backend tolerates
    # can make another reject the whole call, tools and all.
    schema, fixes = sanitize_input_schema(tool_def.input_schema)
    if fixes:
        log.info("mcp.schema_sanitized", tool=tool_name, fixes=fixes)
    properties: dict[str, Any] = schema.get("properties", {})
    required: list[str] = schema.get("required", [])
    server = server_name or client.config.name

    spec = ToolSpec(
        name=resolve_tool_name(tool_name, server, registry),
        description=tool_def.description,
        parameters=properties,
        required=required,
        # Whatever the server returns was authored outside AgentOS: it gets the
        # external-content budget, like web_fetch.
        result_budget_class="external",
    )

    async def handler(**kwargs: Any) -> str:
        try:
            result = await asyncio.wait_for(
                client.call_tool(tool_name, kwargs),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            raise MCPToolError(
                f"MCP tool {tool_name!r} on server {server!r} timed out after {timeout_seconds}s."
            ) from None
        if result.is_error:
            raise MCPToolError(_mcp_error_message(server, tool_name, result.content))
        if not result.content:
            return result.content
        return _wrap_untrusted(result.content, server, tool_name)

    registry.register(spec, handler)
    return MCPToolRegistration(spec=spec, handler=handler, server_tool_name=tool_name)


async def discover_and_register(
    config: MCPServerConfig,
    registry: ToolRegistry,
    *,
    owner: str | None = None,
) -> list[str]:
    """Connect to MCP server, list tools, register each as a AgentOS tool.

    Returns list of registered tool names.
    The client is kept alive in module-level _active_clients so tool handlers can use it.
    """
    client = create_client(config)
    entry: ActiveMCPClient | None = None

    registered: list[str] = []
    registrations: list[MCPToolRegistration] = []
    try:
        await client.connect()
        tools = filter_tools(config, await client.list_tools())
        for t in tools:
            registration = _make_tool_handler(
                client,
                t.name,
                t,
                registry,
                timeout_seconds=config.tool_timeout_seconds,
                server_name=config.name,
            )
            registered.append(registration.name)
            registrations.append(registration)
        entry = ActiveMCPClient(
            owner=owner or config.name,
            server_name=config.name,
            transport=config.transport,
            client=client,
            registered_tools=tuple(registered),
            tool_registrations=tuple(registrations),
        )
        _active_clients.append(entry)
    except BaseException:
        if entry is not None:
            try:
                _active_clients.remove(entry)
            except ValueError:
                pass
        await client.close()
        raise
    return registered
