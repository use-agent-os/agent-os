"""MCP legacy HTTP+SSE transport client."""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any

import httpx

from agentos.mcp.client import MCPClient
from agentos.mcp.http import assert_supported_mcp_url, mcp_http_client
from agentos.mcp.types import MCPServerConfig, MCPToolDef, MCPToolResult


class MCPSSEClient(MCPClient):
    """MCP SDK-backed client for the legacy HTTP+SSE transport."""

    def __init__(self, config: MCPServerConfig) -> None:
        super().__init__(config)
        self._stack: AsyncExitStack | None = None
        self._session: Any = None

    async def connect(self) -> None:
        """Open the SSE stream and perform the MCP initialization handshake."""
        if not self.config.url:
            raise ValueError("SSE MCP server requires a URL")
        url = self.config.url
        assert_supported_mcp_url(url)

        from mcp.client.session import ClientSession
        from mcp.client.sse import sse_client

        def httpx_client_factory(
            headers: dict[str, str] | None = None,
            timeout: httpx.Timeout | None = None,
            auth: httpx.Auth | None = None,
        ) -> httpx.AsyncClient:
            return mcp_http_client(
                url,
                headers=headers,
                timeout=timeout,
                auth=auth,
                follow_redirects=True,
            )

        stack = AsyncExitStack()
        try:
            # HTTP read timeouts alone do not bound waiting for the SDK's
            # endpoint event or initialization on its internal streams.
            async with asyncio.timeout(self.config.tool_timeout_seconds):
                read_stream, write_stream = await stack.enter_async_context(
                    sse_client(
                        self.config.url,
                        headers=self.config.headers,
                        timeout=self.config.tool_timeout_seconds,
                        sse_read_timeout=self.config.tool_timeout_seconds,
                        httpx_client_factory=httpx_client_factory,
                    )
                )
                session = await stack.enter_async_context(
                    ClientSession(
                        read_stream,
                        write_stream,
                        read_timeout_seconds=timedelta(seconds=self.config.tool_timeout_seconds),
                    )
                )
                await session.initialize()
        except BaseException as exc:
            await stack.aclose()
            failure = exc
            while isinstance(failure, BaseExceptionGroup) and len(failure.exceptions) == 1:
                failure = failure.exceptions[0]
            if isinstance(failure, TimeoutError):
                raise TimeoutError("MCP SSE handshake timed out") from failure
            if failure is exc:
                raise
            raise failure from exc

        self._stack = stack
        self._session = session

    async def close(self) -> None:
        """Close the MCP session and its long-lived SSE stream."""
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._session = None

    async def list_tools(self) -> list[MCPToolDef]:
        """List tools from the MCP server."""
        if self._session is None:
            raise RuntimeError("SSE MCP client is not connected")
        result = await self._session.list_tools()
        return [
            MCPToolDef(
                name=tool.name,
                description=tool.description or "",
                input_schema=tool.inputSchema,
            )
            for tool in result.tools
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPToolResult:
        """Call a tool on the MCP server."""
        if self._session is None:
            raise RuntimeError("SSE MCP client is not connected")
        result = await self._session.call_tool(name, arguments)
        chunks: list[str] = []
        for block in result.content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                chunks.append(text)
                continue
            if hasattr(block, "model_dump_json"):
                chunks.append(block.model_dump_json())
        structured = getattr(result, "structuredContent", None)
        if not chunks and structured is not None:
            chunks.append(json.dumps(structured, ensure_ascii=False))
        return MCPToolResult(
            content="\n".join(chunks),
            is_error=bool(getattr(result, "isError", False)),
        )
