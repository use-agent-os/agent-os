from __future__ import annotations

import asyncio
from typing import Any

import pytest
import pytest_asyncio

from agentos.mcp.client import MCPClient
from agentos.mcp.types import MCPServerConfig, MCPToolDef, MCPToolResult
from agentos.tools.registry import ToolRegistry


class FakeMCPClient(MCPClient):
    def __init__(
        self,
        config: MCPServerConfig,
        tools: list[MCPToolDef] | None = None,
        *,
        fail_list: bool = False,
    ) -> None:
        super().__init__(config)
        self.tools = tools or []
        self.fail_list = fail_list
        self.connected = False
        self.closed = False

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed = True

    async def list_tools(self) -> list[MCPToolDef]:
        if self.fail_list:
            raise RuntimeError("list failed")
        return self.tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPToolResult:
        return MCPToolResult(content=f"{name}:{arguments}")


@pytest_asyncio.fixture(autouse=True)
async def _close_mcp_clients():
    from agentos.mcp.discovery import close_active_clients

    await close_active_clients()
    yield
    await close_active_clients()


@pytest.mark.asyncio
async def test_discovered_mcp_clients_have_owner_and_close_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentos.mcp import discovery

    config = MCPServerConfig(name="docs", transport="stdio", command="mock-mcp")
    client = FakeMCPClient(
        config,
        tools=[
            MCPToolDef(
                name="lookup",
                description="Lookup docs",
                input_schema={"properties": {"q": {"type": "string"}}, "required": ["q"]},
            )
        ],
    )
    monkeypatch.setattr(discovery, "create_client", lambda _config: client)

    names = await discovery.discover_and_register(config, ToolRegistry(), owner="gateway")
    snapshot = discovery.active_clients_snapshot()

    assert names == ["mcp_lookup"]
    assert len(snapshot) == 1
    assert snapshot[0].owner == "gateway"
    assert snapshot[0].server_name == "docs"
    assert snapshot[0].transport == "stdio"
    assert snapshot[0].client is client
    assert snapshot[0].registered_tools == ("mcp_lookup",)
    assert await discovery.close_active_clients(owner="docs") == 1
    assert client.closed is True
    assert discovery.active_clients_snapshot() == ()


@pytest.mark.asyncio
async def test_disconnect_unregisters_tools_owned_by_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentos.mcp import discovery

    config = MCPServerConfig(name="docs", transport="stdio", command="mock-mcp")
    client = FakeMCPClient(
        config,
        tools=[
            MCPToolDef(
                name="lookup",
                description="Lookup docs",
                input_schema={"properties": {}, "required": []},
            )
        ],
    )
    monkeypatch.setattr(discovery, "create_client", lambda _config: client)
    registry = ToolRegistry()

    await discovery.discover_and_register(config, registry, owner="docs")
    assert registry.get("mcp_lookup") is not None

    assert await discovery.disconnect_and_unregister("docs", registry) == 1
    assert registry.get("mcp_lookup") is None
    assert client.closed is True


@pytest.mark.asyncio
async def test_disconnect_older_server_preserves_newer_same_named_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentos.mcp import discovery

    older_config = MCPServerConfig(name="older", transport="stdio", command="mock-mcp")
    newer_config = MCPServerConfig(name="newer", transport="stdio", command="mock-mcp")
    tool = MCPToolDef(
        name="lookup",
        description="Lookup",
        input_schema={"properties": {}, "required": []},
    )
    older_client = FakeMCPClient(older_config, tools=[tool])
    newer_client = FakeMCPClient(newer_config, tools=[tool])
    clients = iter((older_client, newer_client))
    monkeypatch.setattr(discovery, "create_client", lambda _config: next(clients))
    registry = ToolRegistry()

    await discovery.discover_and_register(older_config, registry, owner="older")
    older_registration = registry.get("mcp_lookup")
    assert older_registration is not None
    older_handler = older_registration.handler
    await discovery.discover_and_register(newer_config, registry, owner="newer")
    newer_registration = registry.get("mcp_lookup")
    assert newer_registration is not None
    newer_handler = newer_registration.handler

    assert newer_handler is not older_handler
    assert await discovery.disconnect_and_unregister("older", registry) == 1
    registered = registry.get("mcp_lookup")
    assert registered is not None
    assert registered.handler is newer_handler
    assert older_client.closed is True
    assert newer_client.closed is False


@pytest.mark.asyncio
async def test_disconnect_newer_server_restores_older_same_named_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentos.mcp import discovery

    older_config = MCPServerConfig(name="older", transport="stdio", command="mock-mcp")
    newer_config = MCPServerConfig(name="newer", transport="stdio", command="mock-mcp")
    tool = MCPToolDef(
        name="lookup",
        description="Lookup",
        input_schema={"properties": {}, "required": []},
    )
    older_client = FakeMCPClient(older_config, tools=[tool])
    newer_client = FakeMCPClient(newer_config, tools=[tool])
    clients = iter((older_client, newer_client))
    monkeypatch.setattr(discovery, "create_client", lambda _config: next(clients))
    registry = ToolRegistry()

    await discovery.discover_and_register(older_config, registry, owner="older")
    older_registration = registry.get("mcp_lookup")
    assert older_registration is not None
    older_handler = older_registration.handler
    await discovery.discover_and_register(newer_config, registry, owner="newer")

    assert await discovery.disconnect_and_unregister("newer", registry) == 1
    registered = registry.get("mcp_lookup")
    assert registered is not None
    assert registered.handler is older_handler
    assert older_client.closed is False
    assert newer_client.closed is True


@pytest.mark.asyncio
async def test_failed_mcp_discovery_closes_client_without_leaking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentos.mcp import discovery

    config = MCPServerConfig(name="broken", transport="stdio", command="mock-mcp")
    client = FakeMCPClient(config, fail_list=True)
    monkeypatch.setattr(discovery, "create_client", lambda _config: client)

    with pytest.raises(RuntimeError, match="list failed"):
        await discovery.discover_and_register(config, ToolRegistry())

    assert client.closed is True
    assert discovery.active_clients_snapshot() == ()


@pytest.mark.asyncio
async def test_cancelled_mcp_discovery_closes_client_without_leaking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentos.mcp import discovery

    class CancelledMCPClient(FakeMCPClient):
        async def list_tools(self) -> list[MCPToolDef]:
            raise asyncio.CancelledError

    config = MCPServerConfig(name="cancelled", transport="stdio", command="mock-mcp")
    client = CancelledMCPClient(config)
    monkeypatch.setattr(discovery, "create_client", lambda _config: client)

    with pytest.raises(asyncio.CancelledError):
        await discovery.discover_and_register(config, ToolRegistry())

    assert client.closed is True
    assert discovery.active_clients_snapshot() == ()


@pytest.mark.asyncio
async def test_server_schema_is_sanitized_before_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server's raw schema reaches the provider, so it is repaired on the way in."""

    from agentos.mcp import discovery

    config = MCPServerConfig(name="pydantic-ish", transport="stdio", command="mock-mcp")
    client = FakeMCPClient(
        config,
        tools=[
            MCPToolDef(
                name="search",
                description="Search things",
                input_schema={
                    "type": "object",
                    "properties": {
                        # The shape Pydantic emits for every Optional[...] field.
                        "limit": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                        "mode": {"type": ["string", "null"]},
                        "target": {"$ref": "#/$defs/Target"},
                        "broken": "object",
                    },
                    "required": ["limit", "broken"],
                    "$defs": {
                        "Target": {"type": "object", "properties": {"id": {"type": "string"}}}
                    },
                },
            )
        ],
    )
    monkeypatch.setattr(discovery, "create_client", lambda _config: client)

    registry = ToolRegistry()
    await discovery.discover_and_register(config, registry, owner="gateway")

    registered = registry.get("mcp_search")
    assert registered is not None
    parameters = registered.spec.parameters

    assert parameters["limit"] == {"type": "integer"}
    assert parameters["mode"] == {"type": "string"}
    assert parameters["target"] == {"type": "object", "properties": {"id": {"type": "string"}}}
    assert "broken" not in parameters
    # required must not name a property that no longer exists.
    assert registered.spec.required == ["limit"]


@pytest.mark.asyncio
async def test_clean_server_schema_is_registered_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentos.mcp import discovery

    config = MCPServerConfig(name="tidy", transport="stdio", command="mock-mcp")
    client = FakeMCPClient(
        config,
        tools=[
            MCPToolDef(
                name="lookup",
                description="Lookup",
                input_schema={
                    "type": "object",
                    "properties": {"q": {"type": "string", "description": "Query."}},
                    "required": ["q"],
                },
            )
        ],
    )
    monkeypatch.setattr(discovery, "create_client", lambda _config: client)

    registry = ToolRegistry()
    await discovery.discover_and_register(config, registry, owner="gateway")

    registered = registry.get("mcp_lookup")
    assert registered is not None
    assert registered.spec.parameters == {"q": {"type": "string", "description": "Query."}}
    assert registered.spec.required == ["q"]


def test_mcp_tool_result_is_success() -> None:
    ok = MCPToolResult(content="ok", is_error=False)
    err = MCPToolResult(content="fail", is_error=True)
    assert ok.is_success is True
    assert err.is_success is False

