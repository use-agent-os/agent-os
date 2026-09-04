from __future__ import annotations

import asyncio
import json

import pytest

from agentos.mcp.stdio import MCPStdioClient
from agentos.mcp.types import MCPServerConfig


class _FakeProcess:
    def __init__(self, *, exits_on_terminate: bool = True) -> None:
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.wait_calls = 0
        self.exits_on_terminate = exits_on_terminate

    def terminate(self) -> None:
        self.terminated = True
        if self.exits_on_terminate:
            self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        self.wait_calls += 1
        if self.returncode is None:
            await asyncio.sleep(3600)
        return self.returncode


def _client_with_process(process: _FakeProcess) -> MCPStdioClient:
    client = MCPStdioClient(MCPServerConfig(name="demo", transport="stdio", command="demo"))
    client._process = process  # type: ignore[assignment]
    return client


@pytest.mark.asyncio
async def test_close_waits_for_terminated_stdio_process() -> None:
    process = _FakeProcess(exits_on_terminate=True)

    await _client_with_process(process).close()

    assert process.terminated is True
    assert process.killed is False
    assert process.wait_calls == 1


@pytest.mark.asyncio
async def test_close_kills_stdio_process_when_terminate_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(exits_on_terminate=False)
    client = _client_with_process(process)
    monkeypatch.setattr(client, "_CLOSE_TIMEOUT_SECONDS", 0.001)

    await client.close()

    assert process.terminated is True
    assert process.killed is True
    assert process.wait_calls == 2


class _StdoutOnlyProcess:
    """Minimal process stub exposing only a ``stdout`` StreamReader."""

    def __init__(self, stdout: asyncio.StreamReader) -> None:
        self.stdout = stdout
        self.stdin = None


def _line(payload: bytes) -> bytes:
    return payload + b"\n"


def test_encode_request_uses_newline_delimited_json() -> None:
    encoded = MCPStdioClient._encode_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    assert encoded.endswith(b"\n")
    assert encoded.count(b"\n") == 1
    assert b"Content-Length" not in encoded
    assert json.loads(encoded) == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
    }


@pytest.mark.asyncio
async def test_read_response_handles_line_split_across_reads() -> None:
    """A newline-delimited response may arrive in multiple pipe writes."""
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "x"} for _ in range(50)]}}
    ).encode()
    line = _line(payload)
    split = len(line) - (len(payload) // 2)

    reader = asyncio.StreamReader()
    process = _StdoutOnlyProcess(reader)
    client = MCPStdioClient(MCPServerConfig(name="demo", transport="stdio", command="demo"))
    client._process = process  # type: ignore[assignment]

    async def feed() -> None:
        reader.feed_data(line[:split])
        await asyncio.sleep(0)  # force a scheduler yield between chunks
        reader.feed_data(line[split:])
        reader.feed_eof()

    feeder = asyncio.create_task(feed())
    response = await client._read_response()
    await feeder

    assert response["id"] == 1
    assert len(response["result"]["tools"]) == 50


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_size", [8192, 262144])
async def test_read_large_response_preserves_following_message(chunk_size: int) -> None:
    response = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": "é" * 100_000}]},
    }
    following = {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}
    data = _line(json.dumps(response, ensure_ascii=False).encode()) + _line(
        json.dumps(following).encode()
    )
    reader = asyncio.StreamReader(limit=65536)
    client = MCPStdioClient(MCPServerConfig(name="demo", transport="stdio", command="demo"))
    client._process = _StdoutOnlyProcess(reader)  # type: ignore[assignment]

    async def feed() -> None:
        for start in range(0, len(data), chunk_size):
            reader.feed_data(data[start : start + chunk_size])
            await asyncio.sleep(0)
        reader.feed_eof()

    feeder = asyncio.create_task(feed())
    try:
        assert await client._read_response() == response
        assert await client._read_response() == following
    finally:
        await feeder


@pytest.mark.asyncio
@pytest.mark.parametrize("text_size", [0, 200_000])
async def test_read_response_raises_when_eof_precedes_newline(text_size: int) -> None:
    """EOF before the newline delimiter must surface as a clear error."""
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"text": "x" * text_size}})

    reader = asyncio.StreamReader()
    reader.feed_data(payload.encode())
    reader.feed_eof()
    process = _StdoutOnlyProcess(reader)
    client = MCPStdioClient(MCPServerConfig(name="demo", transport="stdio", command="demo"))
    client._process = process  # type: ignore[assignment]

    with pytest.raises(ValueError, match="missing newline delimiter"):
        await client._read_response()


@pytest.mark.asyncio
async def test_read_response_raises_on_empty_eof() -> None:
    reader = asyncio.StreamReader()
    reader.feed_eof()
    client = MCPStdioClient(MCPServerConfig(name="demo", transport="stdio", command="demo"))
    client._process = _StdoutOnlyProcess(reader)  # type: ignore[assignment]

    with pytest.raises(ValueError, match="Unexpected EOF while reading response"):
        await client._read_response()
