"""The legacy HTTP+SSE transport follows the 2024-11-05 lifecycle (issue #922).

The client used to POST to a guessed ``/message`` path and only then open a
``GET`` stream, one per request. Against a compliant server that loses the
handshake outright: the server picks its own message URI and announces it in an
``endpoint`` event on a stream the client is required to open *first*.

These tests drive the real client against a real (loopback, in-process) legacy
SSE server rather than a mock, because the ordering is the defect. A stubbed
transport would happily accept the old order.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from typing import Any

import pytest

from agentos.mcp.sse import MCPSSEClient
from agentos.mcp.types import MCPServerConfig

TOOL_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
}


class LegacySSEServer:
    """A minimal, spec-shaped MCP HTTP+SSE server on an ephemeral loopback port.

    It answers ``GET /sse`` with a long-lived ``text/event-stream``, advertises
    its message URI in an ``endpoint`` event, accepts JSON-RPC over ``POST`` at
    that URI only, and returns every response on the already-open stream.
    """

    def __init__(self, *, endpoint: Callable[[int], str] | None = None) -> None:
        self._endpoint_for = endpoint or (lambda _port: "/mcp/messages?sessionId=s1")
        self.log: list[tuple[str, str]] = []
        self.posted: list[dict[str, Any]] = []
        self.stream_ready = asyncio.Event()
        self.stream_closed = asyncio.Event()
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._held: list[str] = []
        self.hold = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        self.endpoint = self._endpoint_for(self.port)

    async def stop(self) -> None:
        self._server.close()
        # ``wait_closed()`` does not return while a connection is still live, so
        # a test that failed before closing its client would hang the run here
        # instead of reporting the assertion.
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(5):
                await self._server.wait_closed()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/sse"

    async def wait_for_posts(self, count: int, timeout: float = 5.0) -> None:
        """Block until *count* POSTs have landed.

        ``connect()`` returns once the SDK has handed the ``initialized``
        notification to its writer task; the POST itself completes a moment
        later, so assertions about it need this instead of a bare read.
        """
        async with asyncio.timeout(timeout):
            while len(self.posted) < count:
                await asyncio.sleep(0.01)

    # --- request dispatch -------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            method, target, _ = request_line.decode().split()
            headers: dict[str, str] = {}
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                key, _, value = line.decode().partition(":")
                headers[key.strip().lower()] = value.strip()

            self.log.append((method, target))
            if method == "GET":
                await self._serve_stream(reader, writer)
            elif method == "POST":
                body = await reader.readexactly(int(headers.get("content-length", "0")))
                await self._serve_post(writer, target, body)
            else:  # pragma: no cover - no other verb is ever sent
                writer.write(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
        except (ConnectionResetError, asyncio.IncompleteReadError):  # pragma: no cover
            pass
        finally:
            writer.close()

    async def _serve_stream(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Cache-Control: no-cache\r\n"
            b"Connection: close\r\n\r\n"
        )
        writer.write(f"event: endpoint\ndata: {self.endpoint}\n\n".encode())
        await writer.drain()
        self.stream_ready.set()

        pump = asyncio.create_task(self._pump(writer))
        eof = asyncio.create_task(reader.read())
        try:
            _, pending = await asyncio.wait({pump, eof}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
        finally:
            self.stream_closed.set()

    async def _pump(self, writer: asyncio.StreamWriter) -> None:
        while True:
            payload = await self._queue.get()
            writer.write(f"event: message\ndata: {payload}\n\n".encode())
            await writer.drain()

    async def _serve_post(self, writer: asyncio.StreamWriter, target: str, body: bytes) -> None:
        message = json.loads(body)
        self.posted.append({"target": target, "message": message})
        response = self._respond(message)
        if response is not None:
            self._held.append(json.dumps(response))
            if len(self._held) > self.hold:
                # Deliberately reversed so a client that assumed responses
                # arrive in request order would fail the correlation tests.
                for payload in reversed(self._held):
                    self._queue.put_nowait(payload)
                self._held.clear()
        writer.write(b"HTTP/1.1 202 Accepted\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()

    def _respond(self, message: dict[str, Any]) -> dict[str, Any] | None:
        if "id" not in message:  # a notification expects no reply
            return None
        method = message["method"]
        if method == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "legacy-sse", "version": "1.0"},
            }
        elif method == "tools/list":
            result = {
                "tools": [{"name": "echo", "description": "Echo back", "inputSchema": TOOL_SCHEMA}]
            }
        elif method == "tools/call":
            text = message["params"]["arguments"]["text"]
            result = {"content": [{"type": "text", "text": f"echo:{text}"}], "isError": False}
        else:  # pragma: no cover - the client sends nothing else
            return {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": f"unknown method {method}"},
            }
        return {"jsonrpc": "2.0", "id": message["id"], "result": result}


@pytest.fixture
async def server() -> Any:
    srv = LegacySSEServer()
    await srv.start()
    try:
        yield srv
    finally:
        await srv.stop()


def _config(url: str, *, timeout: float = 10.0) -> MCPServerConfig:
    return MCPServerConfig(name="legacy", transport="sse", url=url, tool_timeout_seconds=timeout)


@pytest.mark.asyncio
async def test_stream_opens_before_the_first_post(server: LegacySSEServer) -> None:
    """The regression itself: initialization used to be POSTed with no stream open."""
    client = MCPSSEClient(_config(server.url))
    await client.connect()
    try:
        await server.wait_for_posts(2)
        assert server.log[0] == ("GET", "/sse")
        assert [entry[0] for entry in server.log[1:]] == ["POST", "POST"]
        assert [item["message"]["method"] for item in server.posted] == [
            "initialize",
            "notifications/initialized",
        ]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_posts_go_to_the_advertised_endpoint_not_a_guessed_path(
    server: LegacySSEServer,
) -> None:
    client = MCPSSEClient(_config(server.url))
    await client.connect()
    try:
        await client.list_tools()
    finally:
        await client.close()

    targets = {item["target"] for item in server.posted}
    assert targets == {"/mcp/messages?sessionId=s1"}
    assert not any(target == "/message" for _method, target in server.log)


@pytest.mark.asyncio
async def test_notifications_are_sent_to_the_advertised_endpoint(
    server: LegacySSEServer,
) -> None:
    client = MCPSSEClient(_config(server.url))
    await client.connect()
    try:
        await server.wait_for_posts(2)
        notification = next(
            item for item in server.posted if item["message"]["method"].startswith("notifications/")
        )
    finally:
        await client.close()

    assert notification["target"] == "/mcp/messages?sessionId=s1"
    assert "id" not in notification["message"]


@pytest.mark.asyncio
async def test_tools_are_listed_and_called_over_the_open_stream(
    server: LegacySSEServer,
) -> None:
    client = MCPSSEClient(_config(server.url))
    await client.connect()
    try:
        tools = await client.list_tools()
        assert [tool.name for tool in tools] == ["echo"]
        assert tools[0].description == "Echo back"
        assert tools[0].input_schema == TOOL_SCHEMA

        result = await client.call_tool("echo", {"text": "hi"})
        assert result.content == "echo:hi"
        assert result.is_error is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_concurrent_calls_are_correlated_by_request_id(
    server: LegacySSEServer,
) -> None:
    """One receive stream, two in-flight calls, responses emitted in reverse order."""
    client = MCPSSEClient(_config(server.url))
    await client.connect()
    try:
        server.hold = 1  # release the pair only once both responses exist
        first, second = await asyncio.gather(
            client.call_tool("echo", {"text": "one"}),
            client.call_tool("echo", {"text": "two"}),
        )
        assert first.content == "echo:one"
        assert second.content == "echo:two"
        assert server.log.count(("GET", "/sse")) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_close_ends_the_receive_stream(server: LegacySSEServer) -> None:
    client = MCPSSEClient(_config(server.url))
    await client.connect()
    try:
        assert not server.stream_closed.is_set()
    finally:
        await client.close()

    async with asyncio.timeout(5):
        await server.stream_closed.wait()
    assert client._session is None
    assert client._runner is None


@pytest.mark.asyncio
async def test_close_works_from_a_different_task_than_connect(
    server: LegacySSEServer,
) -> None:
    """The gateway never closes an MCP client from the task that opened it.

    ``discover_and_register`` runs during boot or in an RPC handler;
    ``close_active_clients`` runs from shutdown or a later ``mcp.disconnect``.
    Entering the SDK's anyio task groups inline would make this raise
    ``RuntimeError: Attempted to exit cancel scope in a different task``, which
    ``close_active_clients`` swallows — leaking the connection.
    """
    client = MCPSSEClient(_config(server.url))
    await asyncio.create_task(client.connect())
    try:
        assert await asyncio.create_task(client.list_tools())
    finally:
        await asyncio.create_task(client.close())

    async with asyncio.timeout(5):
        await server.stream_closed.wait()


@pytest.mark.asyncio
async def test_connecting_twice_is_refused(server: LegacySSEServer) -> None:
    client = MCPSSEClient(_config(server.url))
    await client.connect()
    try:
        with pytest.raises(RuntimeError, match="already connected"):
            await client.connect()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_failed_connect_leaves_nothing_to_close(server: LegacySSEServer) -> None:
    """After a refused handshake the client is reusable and ``close()`` is a no-op."""
    client = MCPSSEClient(_config("http://127.0.0.1:1/sse", timeout=1.0))
    with pytest.raises(Exception) as excinfo:
        await client.connect()
    assert not isinstance(excinfo.value, BaseExceptionGroup)
    await client.close()

    assert client._runner is None
    assert client._session is None


@pytest.mark.asyncio
async def test_close_is_idempotent(server: LegacySSEServer) -> None:
    client = MCPSSEClient(_config(server.url))
    await client.connect()
    await client.close()
    await client.close()


@pytest.mark.asyncio
async def test_calls_before_connect_are_refused(server: LegacySSEServer) -> None:
    client = MCPSSEClient(_config(server.url))
    with pytest.raises(RuntimeError, match="MCP SSE client is not connected"):
        await client.list_tools()
    with pytest.raises(RuntimeError, match="MCP SSE client is not connected"):
        await client.call_tool("echo", {"text": "hi"})


# --- endpoint resolution --------------------------------------------------


@pytest.mark.asyncio
async def test_absolute_same_origin_endpoint_is_accepted() -> None:
    srv = LegacySSEServer(endpoint=lambda port: f"http://127.0.0.1:{port}/rpc")
    await srv.start()
    try:
        client = MCPSSEClient(_config(srv.url))
        await client.connect()
        try:
            assert await client.list_tools()
        finally:
            await client.close()
        assert {item["target"] for item in srv.posted} == {"/rpc"}
    finally:
        await srv.stop()


@pytest.mark.asyncio
async def test_endpoint_pointing_at_another_origin_never_receives_a_post() -> None:
    """A compromised server must not be able to redirect the POST channel.

    The SDK refuses the mismatched origin inside its reader, which leaves the
    handshake with nothing to complete; the bounded connect turns that into a
    timeout instead of a hang. What matters for security is the second
    assertion: nothing was posted anywhere.
    """
    srv = LegacySSEServer(endpoint=lambda _port: "http://169.254.169.254/latest/meta-data")
    await srv.start()
    try:
        client = MCPSSEClient(_config(srv.url, timeout=1.0))
        with pytest.raises(TimeoutError):
            await client.connect()
        await client.close()

        assert srv.posted == []
        assert [method for method, _target in srv.log] == ["GET"]
    finally:
        await srv.stop()


@pytest.mark.asyncio
async def test_connect_is_bounded_when_no_endpoint_event_arrives() -> None:
    """A server that opens the stream and goes quiet must not hang the caller."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        while (await reader.readline()) not in (b"\r\n", b"\n", b""):
            pass
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        await reader.read()
        writer.close()

    silent = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = silent.sockets[0].getsockname()[1]
    try:
        client = MCPSSEClient(_config(f"http://127.0.0.1:{port}/sse", timeout=1.0))
        with pytest.raises(TimeoutError, match="did not complete"):
            await client.connect()
        await client.close()
    finally:
        silent.close()
        await silent.wait_closed()
