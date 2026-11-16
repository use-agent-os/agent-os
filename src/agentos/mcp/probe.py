"""Check an MCP server definition, and open a one-off connection to list its tools.

Used by ``agentos mcp doctor`` / ``agentos mcp probe``. Nothing here touches the
tool registry or the gateway's active clients: a probe connects, lists, and
closes, so it works with no gateway running and leaves nothing behind.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse

from agentos.mcp.types import MCPServerConfig, MCPToolDef

CheckLevel = Literal["ok", "warn", "error"]


@dataclass(frozen=True)
class MCPCheck:
    level: CheckLevel
    check: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"level": self.level, "check": self.check, "message": self.message}


@dataclass
class MCPProbeResult:
    name: str
    transport: str
    ok: bool
    elapsed_ms: int
    tools: list[MCPToolDef] = field(default_factory=list)
    registered: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        registered = set(self.registered)
        return {
            "name": self.name,
            "transport": self.transport,
            "ok": self.ok,
            "elapsedMs": self.elapsed_ms,
            "error": self.error,
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "registered": tool.name in registered,
                }
                for tool in self.tools
            ],
        }


def _path_of(env: dict[str, str]) -> str | None:
    for name, value in reversed(list(env.items())):
        if name.upper() == "PATH":
            return value
    return None


def validate_server(config: MCPServerConfig, *, runtime_enabled: bool = True) -> list[MCPCheck]:
    """Static checks on a definition: nothing is spawned and nothing is dialled."""
    checks: list[MCPCheck] = []

    def add(level: CheckLevel, check: str, message: str) -> None:
        checks.append(MCPCheck(level, check, message))

    if not config.name.strip():
        add("error", "name", "The server has no name.")
    if not runtime_enabled:
        add("warn", "runtime", "mcp.enabled is false: no MCP server is connected at boot.")
    if not config.enabled:
        add("warn", "enabled", "The server is disabled: it is kept but never connected.")

    if config.transport == "stdio":
        if not config.command:
            add("error", "command", "A stdio server needs a command.")
        else:
            path = _path_of(config.env) if config.env else None
            resolved = shutil.which(config.command, path=path)
            if resolved is None:
                add(
                    "error",
                    "command",
                    f"Command {config.command!r} was not found on PATH. Install it, "
                    "or give an absolute path.",
                )
            else:
                add("ok", "command", f"Command resolves to {resolved}.")
        if config.cwd:
            cwd = os.path.expanduser(config.cwd)
            if os.path.isdir(cwd):
                add("ok", "cwd", f"Working directory {cwd} exists.")
            else:
                add("error", "cwd", f"Working directory {cwd} does not exist.")
        if config.url:
            add("warn", "url", "url is ignored for a stdio server.")
        if config.headers:
            add("warn", "headers", "headers are ignored for a stdio server.")
        if config.oauth:
            add("error", "oauth", "OAuth is only available for Streamable HTTP servers.")
    elif config.transport in {"sse", "streamable_http"}:
        if not config.url:
            add("error", "url", f"A {config.transport} server needs a url.")
        else:
            parsed = urlparse(config.url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                add("error", "url", f"url {config.url!r} is not an absolute http(s) URL.")
            else:
                add("ok", "url", f"url {config.url} is well formed.")
        if config.command or config.args:
            add("warn", "command", "command/args are ignored for an HTTP server.")
        if config.cwd:
            add("warn", "cwd", "cwd is ignored for an HTTP server.")
        if config.env:
            add("warn", "env", "env is ignored for an HTTP server.")
        if config.oauth and config.transport != "streamable_http":
            add("error", "oauth", "OAuth is only available for Streamable HTTP servers.")
    else:
        add("error", "transport", f"Unknown transport {config.transport!r}.")

    if config.tool_timeout_seconds <= 0:
        add("error", "tool_timeout_seconds", "tool_timeout_seconds must be positive.")
    return checks


def filter_checks(config: MCPServerConfig, tools: list[MCPToolDef]) -> list[MCPCheck]:
    """What the tool filter does to a live tool list: patterns that match nothing, an empty set."""
    tool_filter = config.tool_filter
    if not tool_filter.active:
        return []
    from fnmatch import fnmatchcase

    checks: list[MCPCheck] = []
    names = [tool.name for tool in tools]
    for kind, patterns in (("include", tool_filter.include), ("exclude", tool_filter.exclude)):
        for pattern in patterns:
            if not any(fnmatchcase(name, pattern) for name in names):
                checks.append(
                    MCPCheck(
                        "warn",
                        f"tool_filter.{kind}",
                        f"Pattern {pattern!r} matches none of the server's tools.",
                    )
                )
    if names and not any(tool_filter.allows(name) for name in names):
        checks.append(MCPCheck("warn", "tool_filter", "The filter leaves no tool to register."))
    return checks


async def probe_server(config: MCPServerConfig, *, timeout: float) -> MCPProbeResult:
    """Connect, list tools, close. Never raises for a server-side failure."""
    from agentos.mcp.discovery import create_client

    started = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    if config.oauth and config.url:
        from agentos.mcp.streamable_http import FileOAuthStorage

        storage = FileOAuthStorage(config.name, config.url, config.state_dir)
        if not await storage.is_authenticated():
            return MCPProbeResult(
                name=config.name,
                transport=config.transport,
                ok=False,
                elapsed_ms=elapsed(),
                error="OAuth authorization required: sign in from Settings › MCP first.",
            )

    client = create_client(config)
    try:

        async def _connect_and_list() -> list[MCPToolDef]:
            await client.connect()
            return await client.list_tools()

        tools = await asyncio.wait_for(_connect_and_list(), timeout=timeout)
    except TimeoutError:
        return MCPProbeResult(
            name=config.name,
            transport=config.transport,
            ok=False,
            elapsed_ms=elapsed(),
            error=f"Timed out after {timeout:g}s connecting and listing tools.",
        )
    except Exception as exc:  # noqa: BLE001 - reported to the operator, not raised.
        return MCPProbeResult(
            name=config.name,
            transport=config.transport,
            ok=False,
            elapsed_ms=elapsed(),
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001 - a failed close must not hide the probe result.
            pass
    return MCPProbeResult(
        name=config.name,
        transport=config.transport,
        ok=True,
        elapsed_ms=elapsed(),
        tools=tools,
        registered=[tool.name for tool in tools if config.tool_filter.allows(tool.name)],
    )
