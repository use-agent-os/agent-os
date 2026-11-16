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


class MCPConnectionLost(ConnectionError):  # noqa: N818 - reads as an event, like the stdlib's.
    """The server process is gone, or was disconnected, with requests unanswered."""


class MCPRequestError(RuntimeError):
    """The server answered a request with a JSON-RPC error."""


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
        self._write_lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[None]] = set()
        # Why the server can no longer answer, once its stdout has ended.
        self._dead: BaseException | None = None

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

    def _working_directory(self) -> str | None:
        """Return the configured ``cwd`` expanded, or ``None`` for the gateway's own.

        A missing directory is reported by name here: left to the spawn, it
        surfaces as a ``FileNotFoundError`` that reads as if the *command* were
        missing, which sends the operator looking in the wrong place.
        """
        if not self.config.cwd:
            return None
        cwd = os.path.expanduser(self.config.cwd)
        if not os.path.isdir(cwd):
            raise FileNotFoundError(
                f"MCP server {self.config.name!r}: working directory {cwd!r} does not exist."
            )
        return cwd

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

        cwd = self._working_directory()
        self._process = await asyncio.create_subprocess_exec(
            # Unconfigured, the child's PATH is this process's: let which() read it.
            self._launchable_command(env if self.config.env else None),
            *self.config.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
        )
        self._dead = None

        # MCP initialize handshake
        response = await self._send_request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "agentos", "version": __version__},
            },
        )
        self._capture_initialize(response.get("result"))
        # Send initialized notification
        await self._send_notification("notifications/initialized")

    async def wait_for_exit(self) -> int | None:
        """Wait for the server process to end; its exit code if it died on its own.

        Returns ``None`` when ``close()`` ended it (or it was never started), so
        a watcher can tell a crash from a disconnect it should leave alone:
        ``close()`` forgets the process before terminating it.
        """
        process = self._process
        if process is None:
            return None
        code = await process.wait()
        return None if self._process is not process else code

    async def close(self) -> None:
        """Terminate the subprocess."""
        process = self._process
        self._process = None
        reader, self._reader = self._reader, None
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
        self._fail_pending(MCPConnectionLost(f"MCP server {self.config.name!r} was disconnected"))
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

    # ── JSON-RPC multiplexing ────────────────────────────────────────
    #
    # One reader task owns stdout and hands each reply to the future waiting
    # on its id, so a slow ``tools/call`` no longer holds up every other
    # request to the same server. Only the write of a frame is serialized.

    def _ensure_reader(self) -> None:
        if self._reader is None or self._reader.done():
            self._reader = asyncio.get_running_loop().create_task(
                self._read_loop(), name=f"mcp-stdio-reader-{self.config.name}"
            )

    def _fail_pending(self, exc: BaseException) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(exc)

    async def _read_loop(self) -> None:
        """Dispatch every message the server sends until its stdout ends."""
        process = self._process
        assert process is not None
        assert process.stdout is not None
        try:
            while True:
                line = await self._read_line(process.stdout)
                # Servers occasionally flush a bare newline between messages.
                if not line.strip():
                    continue
                try:
                    message = self._decode_response(line)
                except ValueError as exc:
                    # Whose reply this was meant to be cannot be known, and a
                    # server that frames with ``Content-Length`` would otherwise
                    # leave the handshake waiting for its timeout: everything
                    # in flight fails with the reason, the stream stays open.
                    self._fail_pending(exc)
                    continue
                await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - whatever ended the stream ends the requests.
            # Stdout is gone (the process died, or sent a frame that cannot be
            # delimited): nothing in flight will ever be answered. Later
            # requests fail at once instead of writing into a dead pipe.
            lost = MCPConnectionLost(f"MCP server {self.config.name!r}: {exc}")
            lost.__cause__ = exc
            if self._process is process:
                self._dead = lost
            self._fail_pending(lost)

    async def _dispatch(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if isinstance(method, str):
            if "id" not in message:
                if method == "notifications/tools/list_changed":
                    self._tools_changed()
                return
            # A request from the server (``ping``, ``roots/list``, sampling).
            # Its id comes from the server's own id space, so it can never
            # resolve one of ours; unanswered, some servers stall on it.
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"]}
            if method == "ping":
                reply["result"] = {}
            else:
                reply["error"] = {"code": -32601, "message": f"Method not found: {method}"}
            try:
                await self._write(reply)
            except Exception:  # noqa: BLE001 - a dead stdin shows up on the read side.
                pass
            return
        future = self._pending.pop(message.get("id"), None)  # type: ignore[arg-type]
        if future is not None and not future.done():
            future.set_result(message)

    async def _write(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise MCPConnectionLost(f"MCP server {self.config.name!r} is not connected")
        async with self._write_lock:
            process.stdin.write(self._encode_message(message))
            await process.stdin.drain()

    async def _send_request(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and wait for the reply carrying its id."""
        if self._dead is not None:
            raise MCPConnectionLost(str(self._dead)) from self._dead
        if self._process is None:
            raise MCPConnectionLost(f"MCP server {self.config.name!r} is not connected")
        self._ensure_reader()

        req_id = self._next_id()
        request: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            request["params"] = params
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        try:
            try:
                await self._write(request)
            except (BrokenPipeError, ConnectionError) as exc:
                if isinstance(exc, MCPConnectionLost):
                    raise
                raise MCPConnectionLost(
                    f"MCP server {self.config.name!r} closed its stdin: {exc}"
                ) from exc
            return await future
        except asyncio.CancelledError:
            # The caller gave up (a timeout, a cancelled turn). Tell the server
            # so it can stop working on a result nobody will read.
            if method != "initialize" and self._pending.get(req_id) is future:
                self._send_cancelled(req_id)
            raise
        finally:
            self._pending.pop(req_id, None)

    def _send_cancelled(self, req_id: int) -> None:
        """Send ``notifications/cancelled`` for *req_id* without holding up the caller."""
        if self._process is None or self._dead is not None:
            return

        async def _notify() -> None:
            try:
                await self._send_notification(
                    "notifications/cancelled",
                    {"requestId": req_id, "reason": "client cancelled the request"},
                )
            except Exception:  # noqa: BLE001 - best effort; the request is abandoned either way.
                pass

        task = asyncio.get_running_loop().create_task(_notify())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        notification: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            notification["params"] = params
        await self._write(notification)

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = await self._send_request(method, params)
        error = response.get("error")
        if error is not None:
            message = error.get("message") if isinstance(error, dict) else None
            raise MCPRequestError(str(message or error))
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    async def list_tools(self) -> list[MCPToolDef]:
        """List tools from the MCP server."""
        return [MCPToolDef.from_wire(tool) for tool in await self._list_all("tools/list", "tools")]

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
