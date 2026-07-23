"""MCP client package — connect to external MCP servers and register their tools."""

from __future__ import annotations

from agentos.mcp.client import MCPClient
from agentos.mcp.discovery import (
    ActiveMCPClient,
    active_clients_snapshot,
    close_active_clients,
    disconnect_and_unregister,
    discover_and_register,
)
from agentos.mcp.types import MCPServerConfig, MCPToolDef, MCPToolResult

__all__ = [
    "ActiveMCPClient",
    "MCPClient",
    "MCPServerConfig",
    "MCPToolDef",
    "MCPToolResult",
    "active_clients_snapshot",
    "close_active_clients",
    "disconnect_and_unregister",
    "discover_and_register",
]
