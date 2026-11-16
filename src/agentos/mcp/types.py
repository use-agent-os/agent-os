"""MCP client type definitions."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any


@dataclass(frozen=True)
class MCPToolFilter:
    """Glob filter over the names a server gives its tools.

    An empty ``include`` admits everything; a name matching any ``exclude``
    pattern is dropped even when ``include`` matched it. Matching is
    case-sensitive (``fnmatchcase``): MCP tool names are, and a filter that
    meant one tool must not quietly pick up another on a case-folding OS.
    """

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    @classmethod
    def of(cls, include: Iterable[str] = (), exclude: Iterable[str] = ()) -> MCPToolFilter:
        return cls(
            include=tuple(p for p in include if p),
            exclude=tuple(p for p in exclude if p),
        )

    @property
    def active(self) -> bool:
        return bool(self.include or self.exclude)

    def allows(self, tool_name: str) -> bool:
        if self.include and not any(fnmatchcase(tool_name, p) for p in self.include):
            return False
        return not any(fnmatchcase(tool_name, p) for p in self.exclude)


@dataclass
class MCPServerConfig:
    name: str
    transport: str  # "stdio" | "sse" | "streamable_http"
    command: str | None = None  # for stdio
    args: list[str] = field(default_factory=list)  # for stdio
    cwd: str | None = None  # for stdio
    url: str | None = None  # for HTTP transports
    env: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    oauth: bool = False
    oauth_redirect_uri: str | None = None
    state_dir: str | None = None
    tool_timeout_seconds: float = 30.0
    enabled: bool = True
    # ``None`` defers to the global ``mcp.connect_timeout_seconds``.
    connect_timeout_seconds: float | None = None
    tool_filter: MCPToolFilter = field(default_factory=MCPToolFilter)

    @classmethod
    def from_entry(
        cls,
        entry: Any,
        *,
        state_dir: str | None = None,
        oauth_redirect_uri: str | None = None,
    ) -> MCPServerConfig:
        """Build the runtime config from a ``gateway.config.MCPServerEntry``.

        Duck-typed so this package never imports the gateway config. Every
        place that connects a configured server (boot, ``mcp.connect``, the
        CLI probe) goes through here, so a new entry field cannot reach one
        path and miss another.
        """
        tool_filter = getattr(entry, "tool_filter", None)
        return cls(
            name=entry.name,
            transport=entry.transport,
            command=entry.command,
            args=list(entry.args),
            cwd=getattr(entry, "cwd", None),
            url=entry.url,
            env=dict(entry.env),
            headers=dict(entry.headers),
            oauth=entry.oauth,
            oauth_redirect_uri=oauth_redirect_uri,
            state_dir=state_dir,
            tool_timeout_seconds=entry.tool_timeout_seconds,
            enabled=getattr(entry, "enabled", True),
            connect_timeout_seconds=getattr(entry, "connect_timeout_seconds", None),
            tool_filter=MCPToolFilter.of(
                getattr(tool_filter, "include", ()), getattr(tool_filter, "exclude", ())
            ),
        )

    def effective_connect_timeout(self, default: float) -> float:
        return self.connect_timeout_seconds if self.connect_timeout_seconds else default


@dataclass
class MCPToolDef:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class MCPToolResult:
    content: str
    is_error: bool = False
