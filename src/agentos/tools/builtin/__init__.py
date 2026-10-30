"""Register all built-in tools by importing each submodule."""

from __future__ import annotations

from importlib import import_module

import structlog

_FATAL_MODULES = frozenset({"shell", "patch", "filesystem"})
_NAMES = [
    "acp_tools",
    "agents",
    "artifacts",
    "ask",
    "browser",
    "code_exec",
    "control",
    "env_tools",
    "file_authoring",
    "filesystem",
    "flows",
    "git",
    "media",
    "messaging",
    "patch",
    "plan",
    "projects",
    "router_control",
    "sessions",
    "session_search",
    "shell",
    "tool_search",
    "web",
    "web_fetch",
    "x_search",
]

log = structlog.get_logger(__name__)

for _name in _NAMES:
    try:
        globals()[_name] = import_module(f"{__name__}.{_name}")
    except Exception as exc:
        if _name in _FATAL_MODULES:
            raise
        log.warning("builtin_tool.import_failed", module=_name, error=str(exc))
        continue

__all__ = _NAMES
