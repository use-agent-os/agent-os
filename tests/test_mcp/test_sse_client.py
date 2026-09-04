from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import httpx
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
    assert "sse_read_timeout" not in captured
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


class _EventStream(httpx.AsyncByteStream):
    """An in-memory server stream; the real SDK parses and consumes its events."""

    def __init__(self, endpoint: str) -> None:
        self.messages: asyncio.Queue[bytes] = asyncio.Queue()
        self.messages.put_nowait(f"event: endpoint\ndata: {endpoint}\n\n".encode())
        self.closed = False
        self.reader_stopped = False

    def respond(self, request_id: int, result: dict[str, Any]) -> None:
        payload = {"jsonrpc": "2.0", "id": request_id, "result": result}
        self.messages.put_nowait(f"event: message\ndata: {json.dumps(payload)}\n\n".encode())

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            while True:
                yield await self.messages.get()
        finally:
            self.reader_stopped = True

    async def aclose(self) -> None:
        self.closed = True


class _SSEServer:
    def __init__(self, endpoint: str) -> None:
        self.stream = _EventStream(endpoint)
        self.requests: list[httpx.Request] = []
        self.calls: list[dict[str, Any]] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=self.stream
            )
        payload = json.loads(request.content)
        if payload["method"] == "initialize":
            self.stream.respond(
                payload["id"],
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "test", "version": "1"},
                },
            )
        elif payload["method"] == "tools/list":
            self.stream.respond(
                payload["id"],
                {
                    "tools": [
                        {"name": name, "inputSchema": {"type": "object"}}
                        for name in ("first", "second")
                    ]
                },
            )
        elif payload["method"] == "tools/call":
            self.calls.append(payload)
            if len(self.calls) == 2:
                # Reverse arrival order: responses must match ids, not readers.
                for call in reversed(self.calls):
                    self.stream.respond(
                        call["id"],
                        {"content": [{"type": "text", "text": call["params"]["name"]}]},
                    )
        return httpx.Response(202)


def _install_sse_server(monkeypatch: pytest.MonkeyPatch, server: _SSEServer) -> None:
    from agentos.mcp import sse as client_module

    def client_factory(_url: str, **kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(server.handle), **kwargs)

    # Mock only the HTTP boundary; keep sse_client and ClientSession real.
    monkeypatch.setattr(client_module, "mcp_http_client", client_factory)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint",
    ["../messages?session_id=test", "https://example.test/messages?session_id=test"],
)
async def test_real_sdk_uses_advertised_endpoint_and_correlates_responses(
    monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    server = _SSEServer(endpoint)
    _install_sse_server(monkeypatch, server)
    config = _config()
    config.url = "https://example.test/legacy/sse"
    config.message_endpoint = "/ignored-legacy-endpoint"
    client = MCPSSEClient(config)

    async with asyncio.timeout(3):
        try:
            await client.connect()
            assert len(await client.list_tools()) == 2
            first, second = await asyncio.gather(
                client.call_tool("first", {}), client.call_tool("second", {})
            )
            assert first.content == "first"
            assert second.content == "second"
            assert not server.stream.closed
            assert [r.method for r in server.requests] == ["GET"] + ["POST"] * 5
            assert all(
                str(r.url) == "https://example.test/messages?session_id=test"
                for r in server.requests[1:]
            )
            assert json.loads(server.requests[1].content)["method"] == "initialize"
            assert json.loads(server.requests[2].content)["method"] == "notifications/initialized"
        finally:
            await client.close()

    assert server.stream.closed
    assert server.stream.reader_stopped
    assert client._session is None
    assert client._stack is None


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_timeout", [30.0, 600.0])
async def test_real_sdk_separates_stream_idle_timeout_from_tool_timeout(
    monkeypatch: pytest.MonkeyPatch, tool_timeout: float
) -> None:
    server = _SSEServer("/messages")
    _install_sse_server(monkeypatch, server)
    config = _config()
    config.tool_timeout_seconds = tool_timeout
    client = MCPSSEClient(config)

    async with asyncio.timeout(3):
        try:
            await client.connect()
            assert len(await client.list_tools()) == 2
            # Inspect the real SDK's HTTP request, not a fake transport's kwargs.
            stream_request = server.requests[0]
            assert stream_request.method == "GET"
            assert stream_request.extensions["timeout"] == {
                "connect": tool_timeout,
                "read": 300.0,
                "write": tool_timeout,
                "pool": tool_timeout,
            }
            assert not server.stream.closed
        finally:
            await client.close()

    assert server.stream.closed
    assert server.stream.reader_stopped


@pytest.mark.asyncio
async def test_real_sdk_refuses_an_endpoint_on_another_origin(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    server = _SSEServer("https://other.test/messages")
    _install_sse_server(monkeypatch, server)
    config = _config()
    config.tool_timeout_seconds = 1.0
    client = MCPSSEClient(config)

    async with asyncio.timeout(3):
        try:
            # The SDK rejects the origin before POSTing, but can wait on its
            # error stream before yielding. Our handshake deadline bounds it.
            with pytest.raises(TimeoutError, match="MCP SSE handshake timed out"):
                await client.connect()
        finally:
            await client.close()

    assert [r.method for r in server.requests] == ["GET"]
    assert "Endpoint origin does not match connection origin" in caplog.text
    assert server.stream.closed
    assert client._session is None
    assert client._stack is None
