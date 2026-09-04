"""MCP stdio transport client."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, cast

from agentos import __version__
from agentos.mcp.client import MCPClient
from agentos.mcp.types import MCPServerConfig, MCPToolDef, MCPToolResult


class MCPStdioClient(MCPClient):
    """MCP client using newline-delimited JSON-RPC over a subprocess."""

    _CLOSE_TIMEOUT_SECONDS = 2.0

    def __init__(self, config: MCPServerConfig) -> None:
        super().__init__(config)
        self._process: asyncio.subprocess.Process | None = None
        self._request_id = 0

    @staticmethod
    def _encode_request(request: dict[str, Any]) -> bytes:
        """Encode one compact newline-delimited JSON-RPC message."""
        return json.dumps(request, separators=(",", ":")).encode() + b"\n"

    @staticmethod
    def _decode_response(data: bytes) -> dict[str, Any]:
        """Decode one newline-delimited JSON-RPC response."""
        if not data.endswith(b"\n"):
            raise ValueError("Truncated response: missing newline delimiter")

        body = data[:-1]
        if b"\n" in body:
            raise ValueError("Response contains an embedded newline")
        return cast(dict[str, Any], json.loads(body.decode()))

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def connect(self) -> None:
        """Spawn the subprocess and perform MCP initialization handshake."""
        assert self.config.command is not None, "stdio transport requires command"

        env: dict[str, str] | None = None
        if self.config.env:
            env = {**os.environ, **self.config.env}

        self._process = await asyncio.create_subprocess_exec(
            self.config.command,
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

        req_id = self._next_id()
        request: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            request["params"] = params

        encoded = self._encode_request(request)
        self._process.stdin.write(encoded)
        await self._process.stdin.drain()

        return await self._read_response()

    async def _send_notification(self, method: str) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        assert self._process is not None
        assert self._process.stdin is not None

        notification = {"jsonrpc": "2.0", "method": method}
        encoded = self._encode_request(notification)
        self._process.stdin.write(encoded)
        await self._process.stdin.drain()

    async def _read_response(self) -> dict[str, Any]:
        """Read one response even when it exceeds the stream's buffer limit."""
        assert self._process is not None
        assert self._process.stdout is not None

        reader = self._process.stdout
        line = bytearray()
        while True:
            try:
                line.extend(await reader.readuntil(b"\n"))
                break
            except asyncio.LimitOverrunError as exc:
                # Consume only bytes before the delimiter, leaving later
                # responses buffered for the next request.
                line.extend(await reader.readexactly(exc.consumed))
            except asyncio.IncompleteReadError as exc:
                if line or exc.partial:
                    raise ValueError("Truncated response: missing newline delimiter") from exc
                raise ValueError("Unexpected EOF while reading response") from exc
        return self._decode_response(bytes(line))

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
        content_list = result.get("content", [])
        text = "\n".join(c.get("text", "") for c in content_list if c.get("type") == "text")
        return MCPToolResult(content=text)
