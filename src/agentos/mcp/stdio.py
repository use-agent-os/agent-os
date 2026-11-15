"""MCP stdio transport client."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from typing import Any, cast

from agentos import __version__
from agentos.env_policy import SKILL_SIGNING_KEY_ENV
from agentos.mcp.client import MCPClient
from agentos.mcp.types import MCPServerConfig, MCPToolDef, MCPToolResult


def _path_of(env: dict[str, str] | None) -> str | None:
    """Return the ``PATH`` *env* gives the child, or ``None`` for this process's.

    Windows environment names are case-insensitive, so a server config is free
    to spell the variable ``Path``. The merged environment is built as
    ``{**os.environ, **config.env}``, which leaves both spellings in the dict
    with the config's own last — so the scan runs backwards and the override
    the operator wrote is the one that wins.
    """
    if env is None:
        return None
    for name, value in reversed(list(env.items())):
        if name.upper() == "PATH":
            return value
    return None


class MCPStdioClient(MCPClient):
    """MCP client using stdio transport (subprocess + newline-delimited JSON-RPC).

    The MCP stdio transport frames messages by newline: each JSON-RPC message is
    written as a single line on stdin and read back as a single line from
    stdout. ``Content-Length`` headers are the Language Server Protocol
    convention, not this one, and a spec-compliant server never sends them.
    """

    _CLOSE_TIMEOUT_SECONDS = 2.0
    # A message is read until its delimiter, so a server that never sends one
    # would otherwise grow the buffer without bound.
    _MAX_MESSAGE_BYTES = 16 * 1024 * 1024

    def __init__(self, config: MCPServerConfig) -> None:
        super().__init__(config)
        self._process: asyncio.subprocess.Process | None = None
        self._request_id = 0
        self._lock = asyncio.Lock()

    @staticmethod
    def _encode_message(message: dict[str, Any]) -> bytes:
        """Encode one JSON-RPC message as a single newline-terminated line.

        ``json.dumps`` escapes newlines inside strings, so the payload cannot
        contain the delimiter no matter what a tool argument holds. Compact
        separators keep the frame to what the transport needs.
        """
        return json.dumps(message, separators=(",", ":")).encode() + b"\n"

    @staticmethod
    def _decode_response(data: bytes) -> dict[str, Any]:
        """Decode one newline-delimited JSON-RPC message."""
        try:
            decoded = json.loads(data.decode().strip())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"MCP server sent a line that is not valid JSON: {exc}") from exc
        if not isinstance(decoded, dict):
            raise ValueError(
                f"MCP server response is not a JSON-RPC object: {type(decoded).__name__}"
            )
        return cast(dict[str, Any], decoded)

    @classmethod
    async def _read_line(cls, stream: asyncio.StreamReader) -> bytes:
        """Read one newline-terminated line of any length.

        ``StreamReader.readline`` raises *and drops the data* once a line
        exceeds the stream buffer limit — 64 KiB for an asyncio subprocess
        pipe, which a real ``tools/list`` or tool result passes easily. Draining
        ``readuntil``'s ``LimitOverrunError`` instead keeps the frame whole.

        Returns a non-empty, newline-terminated line, or raises ``ValueError``:
        EOF before the delimiter is a short read, reported as one rather than
        handed to ``json.loads`` as a partial line, and a server that keeps
        sending without ever delimiting is cut off at ``_MAX_MESSAGE_BYTES``.
        """
        chunks: list[bytes] = []
        size = 0
        while True:
            try:
                chunks.append(await stream.readuntil(b"\n"))
            except asyncio.LimitOverrunError as exc:
                # No delimiter within the buffer yet: take what is buffered and
                # keep looking. ``exc.consumed`` never exceeds what is buffered,
                # so this cannot block.
                chunks.append(await stream.readexactly(exc.consumed))
                size += len(chunks[-1])
                if size > cls._MAX_MESSAGE_BYTES:
                    raise ValueError(
                        f"MCP server message exceeds {cls._MAX_MESSAGE_BYTES} bytes "
                        "with no newline delimiter"
                    ) from exc
                continue
            except asyncio.IncompleteReadError as exc:
                partial = b"".join([*chunks, exc.partial])
                if not partial:
                    raise ValueError("MCP server closed stdout without sending a response") from exc
                raise ValueError(
                    f"Truncated message: EOF after {len(partial)} bytes with no newline delimiter"
                ) from exc
            return b"".join(chunks)

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _launchable_command(self, env: dict[str, str] | None) -> str:
        """Return the command to hand ``create_subprocess_exec``.

        On Windows the bare name is not enough. ``create_subprocess_exec``
        goes to ``CreateProcessW``, which appends ``.exe`` to an extensionless
        name and stops there — it never walks ``PATHEXT`` the way a shell
        does. ``npx``, ``npm``, ``uvx`` and ``pipx`` all ship as ``.cmd``
        wrappers, which is what essentially every published MCP server config
        names, so every one of them fails with ``WinError 2`` before a byte of
        MCP traffic. ``shutil.which`` performs the ``PATHEXT`` search and
        hands back the real ``npx.cmd``; CPython's own launcher knows how to
        pass a resolved ``.cmd`` to ``cmd.exe`` safely, so no extra shell
        wrapping belongs here.

        The lookup uses the ``PATH`` the child will actually get, so a server
        that pins its own ``PATH`` in ``env`` is resolved against that and not
        against this process's.

        POSIX is left alone: ``exec`` already searches ``PATH`` for a bare
        command, and substituting the resolved path would replace a program's
        own ``argv[0]`` with it for no gain.
        """
        command = self.config.command
        assert command is not None
        if sys.platform != "win32":
            return command
        resolved = shutil.which(command, path=_path_of(env))
        if resolved is None:
            raise FileNotFoundError(
                f"MCP server {self.config.name!r}: command {command!r} was not "
                f"found on PATH (searched with PATHEXT). Install it, or give "
                f"the server config an absolute path."
            )
        return resolved

    async def connect(self) -> None:
        """Spawn the subprocess and perform MCP initialization handshake."""
        assert self.config.command is not None, "stdio transport requires command"

        # Never the skills' hot-wallet keys: an MCP server is code the gateway
        # runs for the agent, and those keys reach only their own gated entry
        # point (tools/skill_signing.py).
        env: dict[str, str] = {
            key: value for key, value in os.environ.items() if key not in SKILL_SIGNING_KEY_ENV
        }
        if self.config.env:
            env = {**env, **self.config.env}

        self._process = await asyncio.create_subprocess_exec(
            # Unconfigured, the child's PATH is this process's: let which() read it.
            self._launchable_command(env if self.config.env else None),
            *self.config.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            env=env,
        )

        # MCP initialize handshake
        await self._send_request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "agentos", "version": __version__},
            },
        )
        # Send initialized notification
        await self._send_notification("notifications/initialized")

    async def close(self) -> None:
        """Terminate the subprocess."""
        process = self._process
        self._process = None
        if process is None:
            return
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=self._CLOSE_TIMEOUT_SECONDS)
        except TimeoutError:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()

    async def _send_request(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and read the response."""
        assert self._process is not None
        assert self._process.stdin is not None
        assert self._process.stdout is not None

        async with self._lock:
            req_id = self._next_id()
            request: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
            if params is not None:
                request["params"] = params

            self._process.stdin.write(self._encode_message(request))
            await self._process.stdin.drain()

            return await self._read_response(req_id)

    async def _send_notification(self, method: str) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        assert self._process is not None
        assert self._process.stdin is not None

        async with self._lock:
            notification = {"jsonrpc": "2.0", "method": method}
            self._process.stdin.write(self._encode_message(notification))
            await self._process.stdin.drain()

    async def _read_response(self, req_id: int) -> dict[str, Any]:
        """Read newline-delimited messages until the reply to ``req_id`` arrives.

        A server may interleave its own traffic with the reply — a
        ``notifications/message`` log line, ``notifications/tools/list_changed``
        after a catalog change, or a request of its own. Returning whatever
        arrived first would hand a notification back as the result and leave
        every later read one message behind, so replies are matched on their
        JSON-RPC id the way the SSE client does. Each iteration consumes at
        least one byte and EOF raises, so this cannot spin.
        """
        assert self._process is not None
        assert self._process.stdout is not None

        while True:
            line = await self._read_line(self._process.stdout)
            # Servers occasionally flush a bare newline between messages.
            if not line.strip():
                continue
            message = self._decode_response(line)
            # A server-originated request carries ``method`` next to an id from
            # the server's own id space; only a reply can match ours.
            if "method" not in message and message.get("id") == req_id:
                return message

    async def list_tools(self) -> list[MCPToolDef]:
        """List tools from the MCP server."""
        response = await self._send_request("tools/list")
        tools_data = response.get("result", {}).get("tools", [])
        return [
            MCPToolDef(
                name=t["name"],
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}),
            )
            for t in tools_data
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPToolResult:
        """Call a tool on the MCP server."""
        response = await self._send_request("tools/call", {"name": name, "arguments": arguments})

        if "error" in response:
            return MCPToolResult(
                content=response["error"].get("message", "Unknown error"),
                is_error=True,
            )

        result = response.get("result", {})

        try:
            from mcp.types import CallToolResult

            call_result = CallToolResult.model_validate(result)
        except Exception:
            # The server completed the call; only the pinned SDK's model of
            # what a result looks like fell short of it -- a structured-only
            # result with no ``content`` key, or a content block newer than the
            # installed ``mcp``. That is the server's answer, rendered less
            # richly, not a tool failure: ``is_error`` is the signal the model
            # retries on, and a validation dump is not the tool's output.
            return self._render_raw_result(result)

        chunks: list[str] = []
        for block in call_result.content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                chunks.append(text)
                continue
            if hasattr(block, "model_dump_json"):
                chunks.append(block.model_dump_json())

        structured = getattr(call_result, "structuredContent", None)
        if not chunks and structured is not None:
            chunks.append(json.dumps(structured, ensure_ascii=False))

        return MCPToolResult(
            content="\n".join(chunks),
            is_error=bool(getattr(call_result, "isError", False)),
        )

    @staticmethod
    def _render_raw_result(result: Any) -> MCPToolResult:
        """Render a ``tools/call`` result straight from its JSON.

        Mirrors the SDK-backed path block for block: a text block contributes
        its text, any other block its JSON, and ``structuredContent`` stands in
        when there is nothing else -- so the two spellings of a structured-only
        result come out the same.
        """
        if not isinstance(result, dict):
            return MCPToolResult(
                content=f"Unexpected tools/call result: {json.dumps(result, ensure_ascii=False)}",
                is_error=True,
            )
        chunks: list[str] = []
        content = result.get("content")
        for block in content if isinstance(content, list) else []:
            text = block.get("text") if isinstance(block, dict) else None
            if isinstance(text, str):
                chunks.append(text)
            else:
                chunks.append(json.dumps(block, ensure_ascii=False))
        structured = result.get("structuredContent")
        if not chunks and structured is not None:
            chunks.append(json.dumps(structured, ensure_ascii=False))
        return MCPToolResult(
            content="\n".join(chunks),
            is_error=bool(result.get("isError", False)),
        )
