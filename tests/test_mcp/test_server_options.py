"""Per-server MCP options: ``enabled``, ``cwd``, ``tool_filter``, ``connect_timeout_seconds``."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agentos.gateway.boot import _discover_configured_mcp_servers
from agentos.gateway.config import GatewayConfig, MCPServerEntry
from agentos.mcp import discovery
from agentos.mcp.client import MCPClient
from agentos.mcp.types import MCPServerConfig, MCPToolDef, MCPToolFilter, MCPToolResult
from agentos.tools.registry import ToolRegistry

DEMO_SERVER = Path(__file__).resolve().parents[1] / "fixtures" / "mcp" / "fastmcp_demo_server.py"


def _tool(name: str) -> MCPToolDef:
    return MCPToolDef(name=name, description=name, input_schema={"type": "object"})


class _FakeClient(MCPClient):
    def __init__(self, config: MCPServerConfig, tools: list[str]) -> None:
        super().__init__(config)
        self._tools = tools

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def list_tools(self) -> list[MCPToolDef]:
        return [_tool(name) for name in self._tools]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPToolResult:
        return MCPToolResult(content=name)


@pytest.fixture(autouse=True)
async def _no_leaked_clients():
    yield
    from agentos.mcp.supervisor import shutdown_supervisor

    await shutdown_supervisor()
    await discovery.close_active_clients()


# ── config parsing / back-compat ─────────────────────────────────────


def test_existing_server_entry_keeps_its_defaults() -> None:
    entry = MCPServerEntry.model_validate({"name": "fs", "command": "npx", "args": ["-y", "x"]})

    assert entry.enabled is True
    assert entry.cwd is None
    assert entry.connect_timeout_seconds is None
    assert entry.tool_filter.include == []
    assert entry.tool_filter.exclude == []


def test_new_server_fields_parse_from_toml_shaped_payload() -> None:
    cfg = GatewayConfig.model_validate(
        {
            "mcp": {
                "enabled": True,
                "connect_timeout_seconds": 5.0,
                "servers": [
                    {
                        "name": "fs",
                        "command": "npx",
                        "enabled": False,
                        "cwd": "~/tools",
                        "connect_timeout_seconds": 12.5,
                        "tool_filter": {"include": ["read_*"], "exclude": ["read_secret"]},
                    }
                ],
            }
        }
    )
    (entry,) = cfg.mcp.servers
    runtime = MCPServerConfig.from_entry(entry, state_dir="/state")

    assert runtime.enabled is False
    assert runtime.cwd == "~/tools"
    assert runtime.connect_timeout_seconds == 12.5
    assert runtime.effective_connect_timeout(5.0) == 12.5
    assert runtime.tool_filter == MCPToolFilter(include=("read_*",), exclude=("read_secret",))
    assert runtime.state_dir == "/state"


def test_runtime_config_defers_to_global_connect_timeout() -> None:
    runtime = MCPServerConfig.from_entry(MCPServerEntry(name="fs", command="x"))
    assert runtime.effective_connect_timeout(7.0) == 7.0


def test_tool_filter_rejects_unknown_keys_and_non_positive_timeout() -> None:
    with pytest.raises(ValueError):
        MCPServerEntry.model_validate({"name": "fs", "tool_filter": {"includes": ["x"]}})
    with pytest.raises(ValueError):
        MCPServerEntry.model_validate({"name": "fs", "connect_timeout_seconds": 0})


def test_toml_round_trip_keeps_new_fields() -> None:
    cfg = GatewayConfig.model_validate(
        {"mcp": {"servers": [{"name": "fs", "command": "x", "tool_filter": {"exclude": ["rm"]}}]}}
    )
    again = GatewayConfig.model_validate(cfg.to_toml_dict())
    assert again.mcp.servers[0].tool_filter.exclude == ["rm"]
    assert again.mcp.servers[0].enabled is True


# ── tool filter ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("include", "exclude", "expected"),
    [
        ((), (), {"search", "read_file", "read_dir", "delete_all"}),
        (("read_*",), (), {"read_file", "read_dir"}),
        ((), ("delete_*",), {"search", "read_file", "read_dir"}),
        (("read_*", "search"), ("read_dir",), {"search", "read_file"}),
        (("READ_*",), (), set()),  # case-sensitive
    ],
)
def test_tool_filter_globs(include, exclude, expected) -> None:
    tool_filter = MCPToolFilter.of(include, exclude)
    names = ["search", "read_file", "read_dir", "delete_all"]
    assert {name for name in names if tool_filter.allows(name)} == expected


@pytest.mark.asyncio
async def test_discover_and_register_skips_filtered_tools(monkeypatch) -> None:
    config = MCPServerConfig(
        name="demo",
        transport="stdio",
        command="unused",
        tool_filter=MCPToolFilter.of(["read_*", "search"], ["read_dir"]),
    )
    monkeypatch.setattr(
        discovery,
        "create_client",
        lambda cfg: _FakeClient(cfg, ["search", "read_file", "read_dir", "delete_all"]),
    )
    registry = ToolRegistry()

    names = await discovery.discover_and_register(config, registry)

    assert names == ["mcp_search", "mcp_read_file"]
    assert registry.get("mcp_delete_all") is None
    assert registry.get("mcp_read_dir") is None


# ── boot: enabled + per-server timeout ───────────────────────────────


def _boot_config(tmp_path, servers: list[dict[str, Any]]) -> GatewayConfig:
    return GatewayConfig(
        state_dir=str(tmp_path),
        mcp={"enabled": True, "connect_timeout_seconds": 5.0, "servers": servers},
    )


@pytest.mark.asyncio
async def test_boot_skips_disabled_server(tmp_path, monkeypatch) -> None:
    discover = AsyncMock(side_effect=ConnectionError("unreachable in tests"))
    monkeypatch.setattr(discovery, "connect_and_list", discover)

    await _discover_configured_mcp_servers(
        _boot_config(
            tmp_path,
            [
                {"name": "off", "command": "x", "enabled": False},
                {"name": "on", "command": "y"},
            ],
        ),
        ToolRegistry(),
    )

    assert [call.args[0].name for call in discover.await_args_list] == ["on"]


@pytest.mark.asyncio
async def test_boot_uses_per_server_connect_timeout(tmp_path, monkeypatch) -> None:
    import agentos.gateway.boot as boot

    seen: list[float] = []
    real_wait_for = boot.asyncio.wait_for

    async def wait_for(aw, timeout):
        seen.append(timeout)
        return await real_wait_for(aw, timeout)

    monkeypatch.setattr(
        discovery, "connect_and_list", AsyncMock(side_effect=ConnectionError("unreachable"))
    )
    monkeypatch.setattr(boot.asyncio, "wait_for", wait_for)

    await _discover_configured_mcp_servers(
        _boot_config(
            tmp_path,
            [
                {"name": "slow", "command": "x", "connect_timeout_seconds": 42.0},
                {"name": "default", "command": "y"},
            ],
        ),
        ToolRegistry(),
    )

    assert seen[:2] == [42.0, 5.0]


# ── rpc ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rpc_connect_refuses_disabled_server(tmp_path) -> None:
    from agentos.gateway.rpc import RpcContext, get_dispatcher

    ctx = RpcContext(
        conn_id="t",
        config=_boot_config(tmp_path, [{"name": "off", "command": "x", "enabled": False}]),
        tool_registry=ToolRegistry(),
    )
    result = await get_dispatcher().dispatch("r1", "mcp.connect", {"name": "off"}, ctx)

    assert result.error is not None
    assert "disabled" in str(result.error)


# ── stdio cwd against a real FastMCP server ──────────────────────────


@pytest.mark.asyncio
async def test_stdio_server_runs_in_configured_cwd(tmp_path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    config = MCPServerConfig(
        name="demo",
        transport="stdio",
        command=sys.executable,
        args=[str(DEMO_SERVER)],
        cwd=str(workdir),
        tool_filter=MCPToolFilter.of(["where"]),
    )
    registry = ToolRegistry()

    names = await discovery.discover_and_register(config, registry)
    assert names == ["mcp_where"]
    tool = registry.get("mcp_where")
    assert tool is not None
    output = await tool.handler()

    assert str(workdir.resolve()) in output


@pytest.mark.asyncio
async def test_stdio_missing_cwd_is_reported_by_name(tmp_path) -> None:
    config = MCPServerConfig(
        name="demo",
        transport="stdio",
        command=sys.executable,
        args=[str(DEMO_SERVER)],
        cwd=str(tmp_path / "nope"),
    )
    with pytest.raises(FileNotFoundError, match="working directory"):
        await discovery.discover_and_register(config, ToolRegistry())
