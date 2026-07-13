"""Tool boundary re-export for callers that import through agentos.tools."""

from __future__ import annotations

from agentos.tool_boundary import AgentToolHandler, ToolCall, ToolResult

__all__ = ["AgentToolHandler", "ToolCall", "ToolResult"]
