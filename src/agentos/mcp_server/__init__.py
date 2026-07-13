"""Inbound MCP server bridge for AgentOS.

This package exposes AgentOS sessions to external MCP clients. It is
intentionally separate from :mod:`agentos.mcp`, which is the outbound MCP
client integration used to import tools from external MCP servers.
"""

from agentos.mcp_server.bridge import AgentOSMCPBridge
from agentos.mcp_server.server import create_mcp_server

__all__ = ["AgentOSMCPBridge", "create_mcp_server"]
