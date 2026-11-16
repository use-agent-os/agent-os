from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from agentos.mcp import stdio
from agentos.mcp.stdio import MCPConnectionLost, MCPStdioClient
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


async def _read_response(client: MCPStdioClient, req_id: int) -> dict[str, Any]:
    """Wait for the reply to *req_id* the way ``_send_request`` does, minus the write."""
    future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    client._pending[req_id] = future
    client._ensure_reader()
    return await future


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

    assert await _read_response(client, 1) == {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}


@pytest.mark.asyncio
async def test_read_response_rejects_content_length_framing() -> None:
    """LSP-style framing is not an MCP stdio message and must not be accepted."""
    body = b'{"jsonrpc":"2.0","id":1,"result":{}}'
    client = _client_reading(b"Content-Length: %d\r\n\r\n" % len(body) + body + b"\n")

    with pytest.raises(ValueError, match="not valid JSON"):
        await _read_response(client, 1)


@pytest.mark.asyncio
async def test_read_response_stops_at_the_first_delimiter() -> None:
    """A second buffered message stays in the stream for the next read."""
    client = _client_reading(
        b'{"jsonrpc":"2.0","id":1,"result":{"a":1}}\n{"jsonrpc":"2.0","id":2,"result":{"b":2}}\n'
    )

    first, second = await asyncio.gather(_read_response(client, 1), _read_response(client, 2))

    assert first["result"] == {"a": 1}
    assert second["result"] == {"b": 2}


@pytest.mark.asyncio
async def test_read_response_skips_blank_lines() -> None:
    client = _client_reading(b"\n\r\n" + b'{"jsonrpc":"2.0","id":1,"result":{}}\n')

    assert (await _read_response(client, 1))["id"] == 1


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

    assert await _read_response(client, 7) == {"jsonrpc": "2.0", "id": 7, "result": {"tools": []}}


@pytest.mark.asyncio
async def test_read_response_skips_a_reply_to_another_request() -> None:
    client = _client_reading(
        b'{"jsonrpc":"2.0","id":6,"result":{"stale":true}}\n'
        b'{"jsonrpc":"2.0","id":7,"result":{"fresh":true}}\n'
    )

    assert (await _read_response(client, 7))["result"] == {"fresh": True}


@pytest.mark.asyncio
async def test_read_response_skips_a_server_request_reusing_the_id() -> None:
    """A server request has its own id space; ``method`` tells it from a reply."""
    client = _client_reading(
        b'{"jsonrpc":"2.0","id":7,"method":"sampling/createMessage","params":{}}\n'
        b'{"jsonrpc":"2.0","id":7,"result":{"ok":true}}\n'
    )

    assert (await _read_response(client, 7))["result"] == {"ok": True}


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
    response = await _read_response(client, 1)
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

    assert await _read_response(client, 1) == payload


@pytest.mark.asyncio
async def test_read_response_gives_up_on_a_message_with_no_delimiter_in_sight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading to a delimiter must not buffer without bound."""
    monkeypatch.setattr(MCPStdioClient, "_MAX_MESSAGE_BYTES", 256)
    # Four times the cap, and EOF after it, so the cap is what has to fire:
    # without it the read ends in the short-read error instead.
    client = _client_reading(b"x" * 1024, limit=8)

    with pytest.raises(MCPConnectionLost, match="exceeds 256 bytes"):
        await _read_response(client, 1)


@pytest.mark.asyncio
async def test_read_response_raises_when_eof_arrives_before_the_delimiter() -> None:
    """Premature EOF must surface as a clear ValueError, not a JSON error."""
    client = _client_reading(b'{"jsonrpc":"2.0","id":1,"result":{}')  # no newline delimiter

    with pytest.raises(MCPConnectionLost, match="Truncated message"):
        await _read_response(client, 1)


@pytest.mark.asyncio
async def test_read_response_raises_when_partial_message_exceeds_stream_limit() -> None:
    """The short-read guard still fires for a partial message past the limit."""
    client = _client_reading(b'{"jsonrpc":"2.0","id":1,"result":"' + b"x" * 4096, limit=64)

    with pytest.raises(MCPConnectionLost, match="Truncated message"):
        await _read_response(client, 1)


@pytest.mark.asyncio
async def test_read_response_raises_when_server_closes_stdout() -> None:
    client = _client_reading(b"")

    with pytest.raises(MCPConnectionLost, match="closed stdout"):
        await _read_response(client, 1)


@pytest.mark.asyncio
async def test_read_response_reports_undecodable_bytes_as_a_bad_line() -> None:
    """Invalid UTF-8 must give the same framing diagnostic as invalid JSON."""
    client = _client_reading(b"\xff\xfe{}\n")

    with pytest.raises(ValueError, match="not valid JSON"):
        await _read_response(client, 1)


@pytest.mark.asyncio
async def test_read_response_rejects_non_object_message() -> None:
    client = _client_reading(b"[1, 2, 3]\n")

    with pytest.raises(ValueError, match="not a JSON-RPC object"):
        await _read_response(client, 1)


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
        name = message["params"]["name"]
        if name == "fail":
            result = {
                "isError": True,
                "content": [{"type": "text", "text": "failed execution"}],
            }
        else:
            result = {
                "content": [{"type": "text", "text": message["params"]["arguments"]["text"]}]
            }
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


@pytest.mark.asyncio
async def test_tool_call_reports_is_error_flag(tmp_path: Path) -> None:
    """MCPStdioClient propagates isError flag from server tool result."""
    server = tmp_path / "server.py"
    server.write_text(_SPEC_COMPLIANT_SERVER)
    client = MCPStdioClient(
        MCPServerConfig(name="demo", transport="stdio", command=sys.executable, args=[str(server)])
    )

    try:
        await client.connect()
        result = await client.call_tool("fail", {})
    finally:
        await client.close()

    assert result.is_error is True
    assert result.content == "failed execution"


@pytest.mark.asyncio
async def test_concurrent_tool_calls_each_get_their_own_reply(tmp_path: Path) -> None:
    """Concurrent tool calls on the same stdio client must not cross replies."""
    server = tmp_path / "server.py"
    server.write_text(_SPEC_COMPLIANT_SERVER)
    client = MCPStdioClient(
        MCPServerConfig(name="demo", transport="stdio", command=sys.executable, args=[str(server)])
    )

    try:
        await client.connect()
        # Fire 10 concurrent tool calls
        results = await asyncio.gather(
            *[client.call_tool("echo", {"text": f"msg-{i}"}) for i in range(10)]
        )
    finally:
        await client.close()

    assert len(results) == 10
    assert [r.content for r in results] == [f"msg-{i}" for i in range(10)]
    assert all(not r.is_error for r in results)


@pytest.mark.asyncio
async def test_stdio_and_session_clients_parse_payloads_identically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp.types import CallToolResult

    from agentos.mcp.client import MCPSessionClient
    from agentos.mcp.types import MCPServerConfig

    server_result_mixed = {
        "content": [
            {"type": "text", "text": "abc"},
            {"type": "image", "data": "base64...", "mimeType": "image/png"},
        ],
        "isError": True,
        "structuredContent": {"foo": "bar"},
    }

    server_result_struct_only = {
        "content": [],
        "isError": False,
        "structuredContent": {"foo": "bar"},
    }

    server_result_resource = {
        "content": [
            {
                "type": "resource",
                "resource": {
                    "uri": "memo://notes/1",
                    "text": "note content",
                },
            }
        ],
        "isError": False,
    }

    for payload in [server_result_mixed, server_result_struct_only, server_result_resource]:
        # 1. MCPStdioClient
        stdio_client = MCPStdioClient(
            MCPServerConfig(name="demo", transport="stdio", command="demo")
        )

        async def mock_send_request(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"result": payload}

        monkeypatch.setattr(stdio_client, "_send_request", mock_send_request)
        stdio_result = await stdio_client.call_tool("test", {})

        # 2. MCPSessionClient
        class TestSessionClient(MCPSessionClient):
            async def _open_session(self, stack: Any) -> Any:
                pass

            async def connect(self) -> None:
                pass

            async def close(self) -> None:
                pass

        session_client = TestSessionClient(
            MCPServerConfig(name="demo", transport="stdio", command="demo")
        )

        class MockSession:
            async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
                return CallToolResult.model_validate(payload)

        monkeypatch.setattr(session_client, "_require_session", lambda: MockSession())
        session_result = await session_client.call_tool("test", {})

        assert stdio_result == session_result


# A ``tools/call`` payload the pinned SDK cannot model is still the server's
# answer, not a tool failure (#2020). It is rendered from the raw JSON instead
# of being replaced by a pydantic validation dump with ``is_error=True``.


def _stdio_client_returning(monkeypatch: pytest.MonkeyPatch, result: Any) -> MCPStdioClient:
    client = MCPStdioClient(MCPServerConfig(name="demo", transport="stdio", command="demo"))

    async def _send_request(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": 1, "result": result}

    monkeypatch.setattr(client, "_send_request", _send_request)
    return client


@pytest.mark.asyncio
async def test_structured_only_result_without_content_key_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    without = await _stdio_client_returning(
        monkeypatch, {"structuredContent": {"rows": 3}}
    ).call_tool("run_report", {})
    with_empty = await _stdio_client_returning(
        monkeypatch, {"content": [], "structuredContent": {"rows": 3}}
    ).call_tool("run_report", {})

    assert without.is_error is False
    assert without.content == '{"rows": 3}'
    # Two spellings of one result must not produce opposite outcomes.
    assert without == with_empty


@pytest.mark.asyncio
async def test_unmodellable_content_block_is_kept_as_raw_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    block = {"type": "widget", "spec": {"k": 1}}
    result = await _stdio_client_returning(monkeypatch, {"content": [block]}).call_tool("t", {})

    assert result.is_error is False
    assert json.loads(result.content) == block
    assert "validation error" not in result.content


@pytest.mark.asyncio
async def test_unmodellable_block_keeps_the_text_blocks_around_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "content": [
            {"type": "text", "text": "42 rows"},
            {"type": "widget", "spec": {"k": 1}},
            {"type": "text", "text": "done"},
        ]
    }
    result = await _stdio_client_returning(monkeypatch, payload).call_tool("t", {})

    assert result.is_error is False
    lines = result.content.split("\n")
    assert lines[0] == "42 rows"
    assert json.loads(lines[1]) == payload["content"][1]
    assert lines[2] == "done"


@pytest.mark.asyncio
async def test_unmodellable_payload_still_honours_the_servers_is_error_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"content": [{"type": "widget", "spec": {}}], "isError": True}
    result = await _stdio_client_returning(monkeypatch, payload).call_tool("t", {})

    assert result.is_error is True
    assert json.loads(result.content) == payload["content"][0]


@pytest.mark.asyncio
async def test_a_result_that_is_not_an_object_is_reported_as_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _stdio_client_returning(monkeypatch, None).call_tool("t", {})

    assert result.is_error is True
    assert result.content


# --- Windows command resolution (#2726) -------------------------------------
#
# ``create_subprocess_exec`` reaches ``CreateProcessW``, which appends ``.exe``
# to an extensionless name and stops. It never walks ``PATHEXT``. ``npx``,
# ``npm``, ``uvx`` and ``pipx`` are ``.cmd`` wrappers, so every published MCP
# server config failed with ``WinError 2`` on Windows before a byte of MCP
# traffic. The platform is simulated rather than skipped, so these run on both
# CI jobs.


def _spawn_recorder(monkeypatch: pytest.MonkeyPatch, calls: list[tuple[Any, ...]]) -> None:
    """Capture what ``connect()`` hands the subprocess layer."""

    async def fake_exec(*args: Any, **kwargs: Any) -> _PipeProcess:
        calls.append(args)
        return _PipeProcess(_reader(data=b'{"jsonrpc":"2.0","id":1,"result":{}}\n'))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)


def _windows(monkeypatch: pytest.MonkeyPatch, table: dict[str, str]) -> list[str]:
    """Pretend to be Windows with *table* as the PATHEXT lookup's answers."""
    monkeypatch.setattr(stdio.sys, "platform", "win32")
    searched: list[str] = []

    def fake_which(command: str, path: str | None = None) -> str | None:
        searched.append(path if path is not None else "<process PATH>")
        return table.get(command)

    monkeypatch.setattr(stdio.shutil, "which", fake_which)
    return searched


@pytest.mark.parametrize(
    ("command", "resolved"),
    [
        ("npx", r"C:\Program Files\nodejs\npx.cmd"),
        ("npm", r"C:\Program Files\nodejs\npm.cmd"),
        ("uvx", r"C:\tools\uv\uvx.exe"),
        ("pipx", r"C:\Python312\Scripts\pipx.exe"),
    ],
)
async def test_a_wrapper_command_is_spawned_by_its_resolved_path_on_windows(
    monkeypatch: pytest.MonkeyPatch, command: str, resolved: str
) -> None:
    """All four commands the issue enumerates, each as its own case."""
    _windows(monkeypatch, {command: resolved})
    calls: list[tuple[Any, ...]] = []
    _spawn_recorder(monkeypatch, calls)

    client = MCPStdioClient(
        MCPServerConfig(
            name="filesystem",
            transport="stdio",
            command=command,
            args=["-y", "@modelcontextprotocol/server-filesystem", r"C:\workspace"],
        )
    )
    await client.connect()

    assert calls[0] == (
        resolved,
        "-y",
        "@modelcontextprotocol/server-filesystem",
        r"C:\workspace",
    )


async def test_an_unresolvable_command_names_itself_and_its_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure has to say which server and which command.

    ``WinError 2`` names neither, and an operator with several servers
    configured cannot tell from it which one failed to start.
    """
    _windows(monkeypatch, {})
    calls: list[tuple[Any, ...]] = []
    _spawn_recorder(monkeypatch, calls)

    client = MCPStdioClient(
        MCPServerConfig(name="filesystem", transport="stdio", command="npx", args=["-y", "srv"])
    )

    with pytest.raises(FileNotFoundError) as excinfo:
        await client.connect()

    message = str(excinfo.value)
    assert "npx" in message
    assert "filesystem" in message
    assert "PATHEXT" in message
    # The spawn must not be attempted at all, or the clear message is buried
    # under the ``WinError 2`` it was written to replace.
    assert calls == []


async def test_the_error_stays_a_filenotfounderror(monkeypatch: pytest.MonkeyPatch) -> None:
    """``WinError 2`` is a ``FileNotFoundError``, and callers catch ``OSError``.

    Raising the same type keeps every existing handler working; only the
    message improves.
    """
    _windows(monkeypatch, {})
    _spawn_recorder(monkeypatch, [])

    client = MCPStdioClient(MCPServerConfig(name="demo", transport="stdio", command="nope"))

    with pytest.raises(OSError):
        await client.connect()


async def test_resolution_uses_the_path_the_child_will_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server that pins its own ``PATH`` must be resolved against that one.

    Searching this process's ``PATH`` instead would find a different binary
    than the one the operator configured, or none at all.
    """
    searched = _windows(monkeypatch, {"uvx": r"C:\pinned\uvx.exe"})
    calls: list[tuple[Any, ...]] = []
    _spawn_recorder(monkeypatch, calls)

    client = MCPStdioClient(
        MCPServerConfig(
            name="pinned",
            transport="stdio",
            command="uvx",
            env={"PATH": r"C:\pinned"},
        )
    )
    await client.connect()

    assert searched == [r"C:\pinned"]
    assert calls[0] == (r"C:\pinned\uvx.exe",)


async def test_a_lowercase_path_override_still_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows environment names are case-insensitive, so ``Path`` is legal.

    The merged environment is ``{**os.environ, **config.env}``, which leaves
    the inherited ``PATH`` *and* the config's ``Path`` in the dict. Picking the
    first match would search the inherited one and silently ignore the
    override the operator wrote.
    """
    monkeypatch.setenv("PATH", r"C:\inherited")
    searched = _windows(monkeypatch, {"npx": r"C:\override\npx.cmd"})
    _spawn_recorder(monkeypatch, [])

    client = MCPStdioClient(
        MCPServerConfig(
            name="pinned",
            transport="stdio",
            command="npx",
            env={"Path": r"C:\override"},
        )
    )
    await client.connect()

    assert searched == [r"C:\override"]


async def test_no_configured_env_searches_this_process_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no ``env`` in the config the child inherits ours, so ``which``
    must be left to read ``os.environ`` itself rather than be handed a
    half-built ``PATH``."""
    searched = _windows(monkeypatch, {"npx": r"C:\nodejs\npx.cmd"})
    _spawn_recorder(monkeypatch, [])

    client = MCPStdioClient(MCPServerConfig(name="demo", transport="stdio", command="npx"))
    await client.connect()

    assert searched == ["<process PATH>"]


# --- POSIX is deliberately untouched ---------------------------------------


async def test_posix_spawns_the_command_as_written(monkeypatch: pytest.MonkeyPatch) -> None:
    """The boundary this fix deliberately does not cross.

    POSIX ``exec`` already searches ``PATH`` for a bare command, so there is
    nothing to fix; substituting the resolved path would hand the program a
    different ``argv[0]`` than it has today for no gain. Passes either way by
    design -- it is the guard that keeps the Windows fix from leaking.
    """
    monkeypatch.setattr(stdio.sys, "platform", "linux")
    monkeypatch.setattr(stdio.shutil, "which", lambda cmd, path=None: "/usr/local/bin/npx")
    calls: list[tuple[Any, ...]] = []
    _spawn_recorder(monkeypatch, calls)

    client = MCPStdioClient(
        MCPServerConfig(name="demo", transport="stdio", command="npx", args=["-y", "srv"])
    )
    await client.connect()

    assert calls[0] == ("npx", "-y", "srv")


async def test_posix_does_not_raise_early_for_an_unresolvable_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``which`` returning nothing on POSIX must not pre-empt the spawn.

    ``os.access(X_OK)`` can disagree with what ``exec`` will accept, and the
    POSIX ``ENOENT`` already names the file. Failing early here would turn a
    working server into a refused one. Passes either way by design.
    """
    monkeypatch.setattr(stdio.sys, "platform", "linux")
    monkeypatch.setattr(stdio.shutil, "which", lambda cmd, path=None: None)
    calls: list[tuple[Any, ...]] = []
    _spawn_recorder(monkeypatch, calls)

    client = MCPStdioClient(MCPServerConfig(name="demo", transport="stdio", command="npx"))
    await client.connect()

    assert calls[0] == ("npx",)
