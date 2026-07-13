"""agentos.tools — Tool Registry + built-in tools."""

from agentos.tools import builtin as _builtin  # noqa: F401 — side-effect: register tools
from agentos.tools.registry import ToolRegistry, get_default_registry, tool
from agentos.tools.types import (
    CallerKind,
    RegisteredTool,
    ToolContext,
    ToolError,
    ToolSpec,
)

__all__ = [
    "ToolRegistry",
    "get_default_registry",
    "tool",
    "CallerKind",
    "ToolContext",
    "ToolSpec",
    "RegisteredTool",
    "ToolError",
]
