"""agentos.engine — Agent core state machine.

The public surface is **lazy** for everything except ``.types``:
``from agentos.engine import Agent`` continues to work (PEP 562
``__getattr__`` resolves the symbol on first access), but
``import agentos.engine.types`` does NOT transitively drag in the
``agent`` / ``context`` / ``subagent`` modules — keeping tooling that
only needs the type stubs (mypy probes, IDE inspection, packaging
gates, the public-tool-surface lint at ``test_public_tool_surface.py``)
fast and dependency-free.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .types import (
    THINKING_BUDGETS,
    AgentConfig,
    AgentEvent,
    AgentState,
    ArtifactEvent,
    DoneEvent,
    ErrorEvent,
    RouterControlReplayEvent,
    RunHeartbeatEvent,
    StateChangeEvent,
    TextDeltaEvent,
    ThinkingEvent,
    ThinkingLevel,
    ToolCall,
    ToolResult,
    ToolResultEvent,
    ToolUseStartEvent,
    WarningEvent,
)

if TYPE_CHECKING:
    from .agent import Agent, ToolHandler
    from .context import ContextAssembly, ContextFiles
    from .subagent import (
        SubagentHandle,
        SubagentManager,
        SubagentRegistry,
        SubagentSpec,
    )


# Map of lazy attribute name → (module_path, attribute_name). Loaded on
# first access via __getattr__; the imports themselves cascade tools/
# / channels/ / provider/ stacks that the type-stub consumers do not
# need.
_LAZY_MAP: dict[str, tuple[str, str]] = {
    "Agent": ("agentos.engine.agent", "Agent"),
    "ToolHandler": ("agentos.engine.agent", "ToolHandler"),
    "ContextAssembly": ("agentos.engine.context", "ContextAssembly"),
    "ContextFiles": ("agentos.engine.context", "ContextFiles"),
    "SubagentHandle": ("agentos.engine.subagent", "SubagentHandle"),
    "SubagentManager": ("agentos.engine.subagent", "SubagentManager"),
    "SubagentRegistry": ("agentos.engine.subagent", "SubagentRegistry"),
    "SubagentSpec": ("agentos.engine.subagent", "SubagentSpec"),
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_MAP:
        import importlib

        mod_path, attr = _LAZY_MAP[name]
        return getattr(importlib.import_module(mod_path), attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "THINKING_BUDGETS",
    # Types (eager)
    "AgentConfig",
    "AgentEvent",
    "AgentState",
    "ArtifactEvent",
    "ContextAssembly",
    "ContextFiles",
    "DoneEvent",
    "ErrorEvent",
    "RouterControlReplayEvent",
    "RunHeartbeatEvent",
    "StateChangeEvent",
    "SubagentHandle",
    "SubagentManager",
    "SubagentRegistry",
    "SubagentSpec",
    "TextDeltaEvent",
    "ThinkingEvent",
    "ThinkingLevel",
    "ToolCall",
    "ToolHandler",
    "ToolResult",
    "ToolResultEvent",
    "ToolUseStartEvent",
    "WarningEvent",
    "Agent",
]
