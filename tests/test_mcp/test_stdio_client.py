from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

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


def _client_with_process(process: _FakeProcess | _PipeProcess) -> MCPStdioClient:
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


class _FakeStdin:
    """Collects everything the client writes to the subprocess."""

    def __init__(self) -> None:
        self.written = bytearray()

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    async def drain(self) -> None:
        return None


class _PipeProcess:
    """Process stub with a collecting stdin and a readable stdout."""

    def __init__(self, stdout: asyncio.StreamReader) -> None:
        self.stdin = _FakeStdin()
        self.stdout = stdout


def _reader(
    *, data: bytes = b"", eof: bool = True, limit: int | None = None
) -> asyncio.StreamReader:
    reader = asyncio.StreamReader() if limit is None else asyncio.StreamReader(limit=limit)
    if data:
        reader.feed_data(data)
    if eof:
        reader.feed_eof()
    return reader


def _client_reading(data: bytes, *, limit: int | None = None) -> MCPStdioClient:
    return _client_with_process(_PipeProcess(_reader(data=data, limit=limit)))


# --- request framing -------------------------------------------------------


def test_encode_message_emits_one_compact_json_line() -> None:
    """The MCP stdio transport frames messages by newline, not Content-Length."""
    encoded = MCPStdioClient._encode_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    assert b"Content-Length" not in encoded
    assert encoded.endswith(b"\n")
    assert encoded.count(b"\n") == 1
    assert encoded == b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'


def test_encode_message_escapes_newlines_in_payload() -> None:
    """A newline inside an argument must not split the frame."""
    encoded = MCPStdioClient._encode_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"body": "a\nb"}}
    )

    assert encoded.count(b"\n") == 1
    assert json.loads(encoded)["params"]["body"] == "a\nb"


@pytest.mark.asyncio
async def test_send_request_writes_newline_framed_request() -> None:
    """End to end: one line out, one line in, no headers on either side."""
    client = _client_reading(b'{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}\n')

    response = await client._send_request("tools/list")

    written = bytes(client._process.stdin.written)  # type: ignore[union-attr]
    assert written == b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'
    assert response == {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}


@pytest.mark.asyncio
async def test_send_notification_writes_newline_framed_message() -> None:
    client = _client_reading(b"")

    await client._send_notification("notifications/initialized")

    written = bytes(client._process.stdin.written)  # type: ignore[union-attr]
    assert written == b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'


# --- response reading ------------------------------------------------------


@pytest.mark.asyncio
async def test_read_response_accepts_newline_delimited_message() -> None:
    """The exact frame a spec-compliant MCP stdio server writes (issue #894)."""
    client = _client_reading(b'{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}\n')

    assert await client._read_response(1) == {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}


@pytest.mark.asyncio
async def test_read_response_rejects_content_length_framing() -> None:
    """LSP-style framing is not an MCP stdio message and must not be accepted."""
    body = b'{"jsonrpc":"2.0","id":1,"result":{}}'
    client = _client_reading(b"Content-Length: %d\r\n\r\n" % len(body) + body + b"\n")

    with pytest.raises(ValueError, match="not valid JSON"):
        await client._read_response(1)


@pytest.mark.asyncio
async def test_read_response_stops_at_the_first_delimiter() -> None:
    """A second buffered message stays in the stream for the next read."""
    client = _client_reading(
        b'{"jsonrpc":"2.0","id":1,"result":{"a":1}}\n{"jsonrpc":"2.0","id":2,"result":{"b":2}}\n'
    )

    first = await client._read_response(1)
    second = await client._read_response(2)

    assert first["result"] == {"a": 1}
    assert second["result"] == {"b": 2}


@pytest.mark.asyncio
async def test_read_response_skips_blank_lines() -> None:
    client = _client_reading(b"\n\r\n" + b'{"jsonrpc":"2.0","id":1,"result":{}}\n')

    assert (await client._read_response(1))["id"] == 1


@pytest.mark.asyncio
async def test_read_response_skips_notifications_before_the_reply() -> None:
    """A notification is not the answer to a request (issue #894 follow-on).

    Returning the first line read would report the notification as the result
    and leave every later read one message behind.
    """
    client = _client_reading(
        b'{"jsonrpc":"2.0","method":"notifications/tools/list_changed"}\n'
        b'{"jsonrpc":"2.0","method":"notifications/message","params":{"level":"info"}}\n'
        b'{"jsonrpc":"2.0","id":7,"result":{"tools":[]}}\n'
    )

    assert await client._read_response(7) == {"jsonrpc": "2.0", "id": 7, "result": {"tools": []}}


@pytest.mark.asyncio
async def test_read_response_skips_a_reply_to_another_request() -> None:
    client = _client_reading(
        b'{"jsonrpc":"2.0","id":6,"result":{"stale":true}}\n'
        b'{"jsonrpc":"2.0","id":7,"result":{"fresh":true}}\n'
    )

    assert (await client._read_response(7))["result"] == {"fresh": True}


@pytest.mark.asyncio
async def test_read_response_skips_a_server_request_reusing_the_id() -> None:
    """A server request has its own id space; ``method`` tells it from a reply."""
    client = _client_reading(
        b'{"jsonrpc":"2.0","id":7,"method":"sampling/createMessage","params":{}}\n'
        b'{"jsonrpc":"2.0","id":7,"result":{"ok":true}}\n'
    )

    assert (await client._read_response(7))["result"] == {"ok": True}


@pytest.mark.asyncio
async def test_read_response_handles_message_split_across_reads() -> None:
    """A message delivered in many pipe writes must not be truncated.

    ``StreamReader.read(n)`` returns as soon as *any* data is buffered, so a
    reader that does not wait for the delimiter truncates messages that do not
    arrive in a single read and then fails ``json.loads``. The stream limit is
    shrunk well below the message so the over-limit recovery has to run and
    join repeatedly, which is what a >64 KiB result does on a real pipe.
    """
    frame = (
        json.dumps(
            {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "x"} for _ in range(50)]}}
        ).encode()
        + b"\n"
    )
    stdout = _reader(eof=False, limit=64)
    client = _client_with_process(_PipeProcess(stdout))

    async def feed() -> None:
        for start in range(0, len(frame), 7):  # many chunks, each under the limit
            stdout.feed_data(frame[start : start + 7])
            await asyncio.sleep(0)  # force a scheduler yield between chunks
        stdout.feed_eof()

    feeder = asyncio.create_task(feed())
    response = await client._read_response(1)
    await feeder

    assert response["id"] == 1
    assert len(response["result"]["tools"]) == 50


@pytest.mark.asyncio
async def test_read_response_reads_message_longer_than_the_stream_limit() -> None:
    """``readline`` raises and *discards* past the buffer limit; we must not.

    ``asyncio`` subprocess pipes cap a buffered line at 64 KiB, which a real
    ``tools/list`` or tool result exceeds. The limit is shrunk here so the test
    stays fast and deterministic.
    """
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "result": {"text": "x" * 4096}}
    client = _client_reading(json.dumps(payload).encode() + b"\n", limit=64)

    assert await client._read_response(1) == payload


@pytest.mark.asyncio
async def test_read_response_gives_up_on_a_message_with_no_delimiter_in_sight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading to a delimiter must not buffer without bound."""
    monkeypatch.setattr(MCPStdioClient, "_MAX_MESSAGE_BYTES", 256)
    # Four times the cap, and EOF after it, so the cap is what has to fire:
    # without it the read ends in the short-read error instead.
    client = _client_reading(b"x" * 1024, limit=8)

    with pytest.raises(ValueError, match="exceeds 256 bytes"):
        await client._read_response(1)


@pytest.mark.asyncio
async def test_read_response_raises_when_eof_arrives_before_the_delimiter() -> None:
    """Premature EOF must surface as a clear ValueError, not a JSON error."""
    client = _client_reading(b'{"jsonrpc":"2.0","id":1,"result":{}')  # no newline delimiter

    with pytest.raises(ValueError, match="Truncated message"):
        await client._read_response(1)


@pytest.mark.asyncio
async def test_read_response_raises_when_partial_message_exceeds_stream_limit() -> None:
    """The short-read guard still fires for a partial message past the limit."""
    client = _client_reading(b'{"jsonrpc":"2.0","id":1,"result":"' + b"x" * 4096, limit=64)

    with pytest.raises(ValueError, match="Truncated message"):
        await client._read_response(1)


@pytest.mark.asyncio
async def test_read_response_raises_when_server_closes_stdout() -> None:
    client = _client_reading(b"")

    with pytest.raises(ValueError, match="closed stdout"):
        await client._read_response(1)


@pytest.mark.asyncio
async def test_read_response_reports_undecodable_bytes_as_a_bad_line() -> None:
    """Invalid UTF-8 must give the same framing diagnostic as invalid JSON."""
    client = _client_reading(b"\xff\xfe{}\n")

    with pytest.raises(ValueError, match="not valid JSON"):
        await client._read_response(1)


@pytest.mark.asyncio
async def test_read_response_rejects_non_object_message() -> None:
    client = _client_reading(b"[1, 2, 3]\n")

    with pytest.raises(ValueError, match="not a JSON-RPC object"):
        await client._read_response(1)


# --- end to end against a real subprocess ----------------------------------


_SPEC_COMPLIANT_SERVER = """
import json
import sys

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    message = json.loads(line)
    if "id" not in message:  # notification: no response
        continue
    method = message["method"]
    if method == "tools/list":
        # A real server interleaves its own traffic with replies.
        sys.stdout.write(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}) + "\\n"
        )
        sys.stdout.flush()
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {}}
    elif method == "tools/list":
        result = {
            "tools": [
                {"name": "echo", "description": "Echo text", "inputSchema": {"type": "object"}}
            ]
        }
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": message["params"]["arguments"]["text"]}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\\n")
    sys.stdout.flush()
"""


@pytest.mark.asyncio
async def test_connects_to_a_spec_compliant_newline_delimited_server(tmp_path: Path) -> None:
    """Issue #894's repro: a server that speaks only newline-delimited JSON-RPC.

    The server parses each stdin line as JSON, so ``Content-Length`` headers
    make it raise before it ever answers.
    """
    server = tmp_path / "server.py"
    server.write_text(_SPEC_COMPLIANT_SERVER)
    client = MCPStdioClient(
        MCPServerConfig(name="demo", transport="stdio", command=sys.executable, args=[str(server)])
    )

    try:
        await client.connect()
        tools = await client.list_tools()
        result = await client.call_tool("echo", {"text": "multi\nline"})
    finally:
        await client.close()

    assert [t.name for t in tools] == ["echo"]
    assert result.content == "multi\nline"
    assert result.is_error is False
