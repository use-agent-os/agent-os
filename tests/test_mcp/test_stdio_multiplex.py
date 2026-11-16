"""The stdio client multiplexes JSON-RPC: requests overlap, and die cleanly."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from agentos.mcp import discovery
from agentos.mcp.stdio import MCPConnectionLost, MCPStdioClient
from agentos.mcp.types import MCPServerConfig
from agentos.tools.registry import ToolRegistry

DEMO_SERVER = Path(__file__).resolve().parents[1] / "fixtures" / "mcp" / "fastmcp_demo_server.py"


def _demo_config(name: str = "demo", **overrides: Any) -> MCPServerConfig:
    return MCPServerConfig(
        name=name,
        transport="stdio",
        command=sys.executable,
        args=[str(DEMO_SERVER), "--extras"],
        **overrides,
    )


@pytest.fixture(autouse=True)
async def _no_leaked_clients():
    yield
    await discovery.close_active_clients()


class _Stdin:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def write(self, data: bytes) -> None:
        self.messages.extend(json.loads(line) for line in data.splitlines() if line.strip())

    async def drain(self) -> None:
        return None


class _Process:
    """A server that says only what the test feeds into ``stdout``."""

    def __init__(self) -> None:
        self.stdin = _Stdin()
        self.stdout = asyncio.StreamReader()
        self.returncode: int | None = None

    def feed(self, message: dict[str, Any]) -> None:
        self.stdout.feed_data(json.dumps(message).encode() + b"\n")

    def terminate(self) -> None:
        self.returncode = 0

    async def wait(self) -> int:
        return 0


def _scripted() -> tuple[MCPStdioClient, _Process]:
    client = MCPStdioClient(MCPServerConfig(name="demo", transport="stdio", command="demo"))
    process = _Process()
    client._process = process  # type: ignore[assignment]
    return client, process


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


# ── overlap ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_fast_call_returns_while_a_slow_call_is_in_flight() -> None:
    client = MCPStdioClient(_demo_config())
    await client.connect()
    try:
        finished: list[str] = []

        async def call(name: str, arguments: dict[str, Any]) -> str:
            result = await client.call_tool(name, arguments)
            finished.append(name)
            return result.content

        slow = asyncio.create_task(call("sleep", {"seconds": 1.5}))
        await asyncio.sleep(0.1)  # the slow request is on the wire
        started = time.perf_counter()
        fast = await call("search", {"query": "q"})
        elapsed = time.perf_counter() - started

        assert fast == "results for q"
        assert not slow.done()
        assert elapsed < 1.0
        assert await slow == "slept 1.5"
        assert finished == ["search", "sleep"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_replies_are_matched_by_id_in_any_order() -> None:
    client, process = _scripted()
    first = asyncio.create_task(client._send_request("tools/call", {"name": "a"}))
    second = asyncio.create_task(client._send_request("tools/call", {"name": "b"}))
    await _settle()
    ids = [message["id"] for message in process.stdin.messages]

    process.feed({"jsonrpc": "2.0", "id": ids[1], "result": {"which": "b"}})
    process.feed({"jsonrpc": "2.0", "id": ids[0], "result": {"which": "a"}})

    assert (await second)["result"] == {"which": "b"}
    assert (await first)["result"] == {"which": "a"}
    assert client._pending == {}
    await client.close()


# ── cancellation and timeout ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_timed_out_request_is_forgotten_and_the_server_is_told() -> None:
    client, process = _scripted()

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(client._send_request("tools/call", {"name": "slow"}), 0.05)
    await _settle()

    request, cancelled = process.stdin.messages
    assert client._pending == {}
    assert cancelled == {
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {"requestId": request["id"], "reason": "client cancelled the request"},
    }

    # A reply that arrives after the caller gave up is dropped, and the
    # connection is still good for the next request.
    process.feed({"jsonrpc": "2.0", "id": request["id"], "result": {"late": True}})
    follow_up = asyncio.create_task(client._send_request("tools/list"))
    await _settle()
    process.feed({"jsonrpc": "2.0", "id": process.stdin.messages[-1]["id"], "result": {"ok": 1}})
    assert (await follow_up)["result"] == {"ok": 1}
    await client.close()


@pytest.mark.asyncio
async def test_initialize_is_never_cancelled_by_notification() -> None:
    """The spec forbids cancelling ``initialize``."""
    client, process = _scripted()

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(client._send_request("initialize", {}), 0.05)
    await _settle()

    assert [message["method"] for message in process.stdin.messages] == ["initialize"]
    await client.close()


@pytest.mark.asyncio
async def test_tool_timeout_tells_the_agent_which_server_and_what_to_do() -> None:
    registry = ToolRegistry()
    await discovery.discover_and_register(_demo_config(tool_timeout_seconds=0.3), registry)
    sleep = registry.get("mcp_sleep")
    search = registry.get("mcp_search")
    assert sleep is not None and search is not None

    with pytest.raises(discovery.MCPToolError) as excinfo:
        await sleep.handler(seconds=5)

    message = excinfo.value.user_message
    assert "'sleep' on server 'demo' timed out after 0.3s" in message
    assert "retry once" in message and "slow or busy" in message
    # The timeout cost one request, not the connection.
    assert "results for after" in await search.handler(query="after")


# ── server death ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_server_death_fails_every_pending_request_at_once() -> None:
    client = MCPStdioClient(_demo_config())
    await client.connect()
    try:
        calls = [
            asyncio.create_task(client.call_tool("sleep", {"seconds": 30, "label": str(i)}))
            for i in range(3)
        ]
        await asyncio.sleep(0.2)
        exited = asyncio.create_task(client.wait_for_exit())
        started = time.perf_counter()
        assert client._process is not None
        client._process.kill()

        results = await asyncio.wait_for(asyncio.gather(*calls, return_exceptions=True), 5)
        assert time.perf_counter() - started < 5
        assert all(isinstance(result, MCPConnectionLost) for result in results)
        assert client._pending == {}
        # The supervisor's crash watcher still sees the exit code.
        assert await asyncio.wait_for(exited, 5) not in (None, 0)
        # Nothing is written into the dead pipe afterwards.
        with pytest.raises(MCPConnectionLost):
            await client.call_tool("search", {"query": "q"})
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_call_cut_off_by_server_death_reads_as_a_disconnect() -> None:
    registry = ToolRegistry()
    await discovery.discover_and_register(_demo_config(), registry)
    entry = discovery.active_clients_snapshot()[0]
    sleep = registry.get("mcp_sleep")
    assert sleep is not None

    call = asyncio.create_task(sleep.handler(seconds=30))
    await asyncio.sleep(0.2)
    entry.client._process.kill()  # type: ignore[attr-defined]

    with pytest.raises(discovery.MCPToolError, match="'demo' disconnected while running"):
        await asyncio.wait_for(call, 5)


@pytest.mark.asyncio
async def test_close_fails_pending_requests() -> None:
    client, _process = _scripted()
    pending = asyncio.create_task(client._send_request("tools/call", {"name": "x"}))
    await _settle()

    await client.close()

    with pytest.raises(MCPConnectionLost, match="was disconnected"):
        await pending


# ── traffic the server starts ────────────────────────────────────────


@pytest.mark.asyncio
async def test_server_requests_are_answered_instead_of_left_hanging() -> None:
    client, process = _scripted()
    listing = asyncio.create_task(client._send_request("tools/list"))
    await _settle()

    process.feed({"jsonrpc": "2.0", "id": "srv-1", "method": "ping"})
    process.feed({"jsonrpc": "2.0", "id": "srv-2", "method": "roots/list"})
    process.feed({"jsonrpc": "2.0", "id": process.stdin.messages[0]["id"], "result": {}})
    await listing
    await _settle()

    replies = {message["id"]: message for message in process.stdin.messages[1:]}
    assert replies["srv-1"]["result"] == {}
    assert replies["srv-2"]["error"]["code"] == -32601
    await client.close()


@pytest.mark.asyncio
async def test_a_bad_line_fails_what_is_in_flight_but_keeps_the_connection() -> None:
    client, process = _scripted()
    first = asyncio.create_task(client._send_request("tools/list"))
    await _settle()
    process.stdout.feed_data(b"Content-Length: 12\r\n")

    with pytest.raises(ValueError, match="not valid JSON"):
        await first

    second = asyncio.create_task(client._send_request("tools/list"))
    await _settle()
    process.feed({"jsonrpc": "2.0", "id": process.stdin.messages[-1]["id"], "result": {"ok": 1}})
    assert (await second)["result"] == {"ok": 1}
    await client.close()
