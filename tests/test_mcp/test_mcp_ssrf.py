"""Regression tests for MCP server URL validation.

The MCP HTTP/SSE/streamable-HTTP transports connect to operator-configured
``config.url`` values. Both transports must reject cloud-metadata endpoints
and any URL whose scheme is not http(s) — and, critically, must validate the
address the socket actually dials rather than just the URL text once
upfront, or a short-TTL DNS-rebinding domain bypasses the check entirely.
"""

from __future__ import annotations

import pytest

from agentos.mcp.sse import MCPSSEClient
from agentos.mcp.streamable_http import MCPStreamableHTTPClient
from agentos.mcp.types import MCPServerConfig
from agentos.tools.ssrf_client import ValidatingNetworkBackend


def _contains(exc: BaseException, cls: type[BaseException]) -> bool:
    """True if *exc* is *cls*, or an ExceptionGroup containing one.

    The streamable-HTTP transport runs inside an anyio task group (part of
    the upstream MCP SDK), so an error raised deep in a background task
    surfaces wrapped in an ExceptionGroup rather than directly — the block
    still happened, it just isn't the top-level exception type.
    """
    if isinstance(exc, cls):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_contains(sub, cls) for sub in exc.exceptions)
    return False


def _sse(url: str) -> MCPSSEClient:
    return MCPSSEClient(MCPServerConfig(name="mcp", transport="sse", url=url))


def _http(url: str) -> MCPStreamableHTTPClient:
    return MCPStreamableHTTPClient(
        MCPServerConfig(name="mcp", transport="streamable_http", url=url)
    )


CLOUD_METADATA_URLS = [
    "http://169.254.169.254/metadata/instance",
    "http://169.254.169.254/computeMetadata/v1/",
    "http://[fd00:ec2::254]/latest/meta-data/",
    "http://metadata.google.internal/computeMetadata/v1/",
    "http://metadata.azure.com/metadata/instance",
    "http://100.100.100.200/latest/meta-data/",
    "http://192.0.0.192/latest/meta-data/",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", CLOUD_METADATA_URLS)
@pytest.mark.parametrize("factory", [_sse, _http])
async def test_mcp_transports_reject_metadata_endpoints(url: str, factory) -> None:
    client = factory(url)
    with pytest.raises((ValueError, BaseExceptionGroup)) as exc_info:
        await client.connect()
    assert _contains(exc_info.value, ValueError), (
        f"connect() raised {type(exc_info.value).__name__} but it wasn't (or "
        "didn't contain) a ValueError/SSRFBlockedError"
    )


BAD_SCHEMES = [
    "file:///etc/passwd",
    "gopher://169.254.169.254/metadata",
    "ftp://example.com/secret",
    "javascript:alert(1)",
    "data:text/plain,hello",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", BAD_SCHEMES)
@pytest.mark.parametrize("factory", [_sse, _http])
async def test_mcp_transports_reject_non_http_schemes(url: str, factory) -> None:
    client = factory(url)
    with pytest.raises((ValueError, BaseExceptionGroup)) as exc_info:
        await client.connect()
    assert _contains(exc_info.value, ValueError)


@pytest.mark.asyncio
async def test_mcp_sse_uses_connect_time_ssrf_guard() -> None:
    """The client must validate the address it actually dials, not just the
    URL text once upfront — otherwise a DNS-rebinding domain (safe answer for
    the pre-check, metadata IP for the real connect) bypasses the guard.

    This is a structural check: it proves ssrf_guarded_client's
    ValidatingNetworkBackend is actually installed on the transport, rather
    than trying to simulate live DNS rebinding.
    """
    client = _sse("http://127.0.0.1:1")  # loopback: allowed by policy, port closed
    try:
        await client.connect()
    except Exception:
        pass  # connecting to a closed local port is expected to fail
    assert client._client is not None, "connect() must set self._client before the handshake"
    backend = client._client._transport._pool._network_backend
    assert isinstance(backend, ValidatingNetworkBackend), (
        "MCPSSEClient is not using ssrf_guarded_client — it will re-resolve the "
        "hostname a second time at connect, outside any SSRF check, which a "
        "DNS-rebinding domain can exploit."
    )
    await client.close()
