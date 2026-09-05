"""MCP legacy HTTP+SSE transport client (spec revision 2024-11-05).

``HTTP with SSE`` is a two-channel protocol: the client opens a long-lived
``GET`` stream *first*, the server answers with an ``endpoint`` event naming the
URI to post to, and every JSON-RPC response comes back on the stream that is
already open. This module used to do the opposite — it posted to a guessed
``/message`` path and then opened a fresh ``GET`` per request — so a compliant
server either rejected the initialization or emitted the response into a window
where nothing was listening (issue #922).

Rather than re-derive that lifecycle, the client now drives ``mcp.client.sse``,
the sibling of the SDK transport ``streamable_http.py`` already uses. The SDK
owns the ordering, the ``endpoint`` resolution (``urljoin`` against the
configured URL, with a scheme/netloc mismatch refused before any POST is made),
the single correlated receive stream, and the reader-task cancellation on close.

The SSRF guard from #662 survives the swap because the SDK builds no HTTP client
of its own: ``httpx_client_factory`` is the hook it dials through, and the
factory installed here returns :func:`mcp_http_client`. Both channels — the
``GET`` stream and the POST to the server-advertised endpoint — therefore go
through the same connect-time, address-pinning guard. That matters more under
this transport than under Streamable HTTP, because the POST target is chosen by
the server rather than by the operator's configuration.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any

import httpx

from agentos.mcp.client import MCPSessionClient
from agentos.mcp.http import assert_supported_mcp_url, mcp_http_client
from agentos.mcp.streamable_http import MCPDependencyError

# How long the receive stream may sit idle before the transport gives up. This
# is a keep-alive ceiling for a connection that is expected to stay open between
# tool calls, not a per-request deadline — that one is ``tool_timeout_seconds``.
SSE_READ_TIMEOUT_SECONDS = 300.0


def _unwrap_single_exception(exc: BaseException) -> BaseException:
    """Peel single-member groups off an SDK failure.

    The SDK runs the transport inside an anyio task group, which repackages
    whatever the connection raised as an ``ExceptionGroup``. Callers of this
    client — and the SSRF tests — expect the same ``SSRFBlockedError`` or
    ``HTTPError`` the transport used to raise directly, so a group carrying one
    exception is unwrapped back to it. Genuine multi-error groups are left
    alone.
    """
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    return exc


class MCPSSEClient(MCPSessionClient):
    """MCP SDK-backed client for the legacy HTTP+SSE transport."""

    transport_label = "MCP SSE"

    def _http_client_factory(self, url: str) -> Any:
        """Return the SDK client factory, bound to the guarded constructor."""

        def factory(
            headers: dict[str, str] | None = None,
            timeout: httpx.Timeout | None = None,
            auth: httpx.Auth | None = None,
        ) -> httpx.AsyncClient:
            return mcp_http_client(url, headers=headers, timeout=timeout, auth=auth)

        return factory

    async def _open_session(self, stack: AsyncExitStack) -> Any:
        """Open the SSE stream, adopt the advertised endpoint, then initialize."""
        if not self.config.url:
            raise ValueError("SSE MCP server requires a URL")
        # Checked here as well as inside ``mcp_http_client`` so an unusable URL
        # fails before the SDK starts a task group around it.
        assert_supported_mcp_url(self.config.url)

        try:
            from mcp.client.session import ClientSession
            from mcp.client.sse import sse_client
        except ImportError as exc:
            raise MCPDependencyError(
                "SSE support is unavailable because the MCP SDK is missing. "
                "Reinstall or upgrade AgentOS."
            ) from exc

        timeout = self.config.tool_timeout_seconds
        try:
            # The whole handshake is bounded, not just the HTTP calls inside it.
            # A server that opens the stream and then never emits an ``endpoint``
            # event leaves the SDK waiting on an internal stream with no deadline
            # of its own, and a rejected endpoint reports the same way, so an
            # unbounded connect would hang the caller instead of failing it.
            async with asyncio.timeout(timeout):
                read_stream, write_stream = await stack.enter_async_context(
                    sse_client(
                        self.config.url,
                        headers=dict(self.config.headers),
                        timeout=timeout,
                        sse_read_timeout=SSE_READ_TIMEOUT_SECONDS,
                        httpx_client_factory=self._http_client_factory(self.config.url),
                    )
                )
                session = await stack.enter_async_context(
                    ClientSession(
                        read_stream,
                        write_stream,
                        read_timeout_seconds=timedelta(seconds=timeout),
                    )
                )
                await session.initialize()
        except BaseException as exc:
            unwrapped = _unwrap_single_exception(exc)
            if isinstance(unwrapped, TimeoutError):
                raise TimeoutError(
                    f"MCP SSE handshake with {self.config.url} did not complete within "
                    f"{timeout}s: the server never advertised a usable endpoint, or "
                    f"never answered initialize"
                ) from exc
            if unwrapped is not exc:
                raise unwrapped
            raise
        return session
