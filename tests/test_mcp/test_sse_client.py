from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.mcp.discovery import create_client
from agentos.mcp.sse import MCPSSEClient
from agentos.mcp.types import MCPServerConfig


def _config() -> MCPServerConfig:
    return MCPServerConfig(
        name="legacy-sse",
        transport="sse",
        url="https://example.test/sse",
        headers={"Authorization": "Bearer test"},
        tool_timeout_seconds=17.0,
    )


def test_factory_builds_sse_client() -> None:
    assert isinstance(create_client(_config()), MCPSSEClient)


@pytest.mark.asyncio
async def test_connect_keeps_one_sse_transport_open_until_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp.client import session as session_module
    from mcp.client import sse as transport_module

    events: list[str] = []
    captured: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_transport(url: str, **kwargs: Any):
        captured["url"] = url
        captured.update(kwargs)
        events.append("transport_enter")
        try:
            yield object(), object()
        finally:
            events.append("transport_exit")

    class FakeSession:
        async def __aenter__(self) -> FakeSession:
            events.append("session_enter")
            return self

        async def __aexit__(self, *_args: Any) -> None:
            events.append("session_exit")

        async def initialize(self) -> None:
            events.append("initialize")

    monkeypatch.setattr(transport_module, "sse_client", fake_transport)
    monkeypatch.setattr(session_module, "ClientSession", lambda *_args, **_kwargs: FakeSession())
    client = MCPSSEClient(_config())

    await client.connect()

    assert events == ["transport_enter", "session_enter", "initialize"]
    assert captured["url"] == "https://example.test/sse"
    assert captured["headers"] == {"Authorization": "Bearer test"}
    assert captured["timeout"] == 17.0
    assert captured["sse_read_timeout"] == 17.0
    assert callable(captured["httpx_client_factory"])

    await client.close()

    assert events == [
        "transport_enter",
        "session_enter",
        "initialize",
        "session_exit",
        "transport_exit",
    ]


@pytest.mark.asyncio
async def test_cancelled_connect_closes_partial_sse_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp.client import session as session_module
    from mcp.client import sse as transport_module

    closed: list[str] = []

    @asynccontextmanager
    async def fake_transport(_url: str, **_kwargs: Any):
        try:
            yield object(), object()
        finally:
            closed.append("transport")

    class FakeSession:
        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            closed.append("session")

        async def initialize(self) -> None:
            raise asyncio.CancelledError

    monkeypatch.setattr(transport_module, "sse_client", fake_transport)
    monkeypatch.setattr(session_module, "ClientSession", lambda *_args, **_kwargs: FakeSession())
    client = MCPSSEClient(_config())

    with pytest.raises(asyncio.CancelledError):
        await client.connect()

    assert closed == ["session", "transport"]


@pytest.mark.asyncio
async def test_tools_use_the_connected_mcp_session() -> None:
    class FakeSession:
        async def list_tools(self) -> Any:
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="lookup",
                        description=None,
                        inputSchema={"type": "object"},
                    )
                ]
            )

        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            assert name == "lookup"
            assert arguments == {"query": "agentos"}
            return SimpleNamespace(
                content=[SimpleNamespace(text="found")],
                structuredContent=None,
                isError=False,
            )

    client = MCPSSEClient(_config())
    client._session = FakeSession()

    tools = await client.list_tools()
    result = await client.call_tool("lookup", {"query": "agentos"})

    assert [(tool.name, tool.description, tool.input_schema) for tool in tools] == [
        ("lookup", "", {"type": "object"})
    ]
    assert result.content == "found"
    assert result.is_error is False
