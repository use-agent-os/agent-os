"""Both MCP HTTP transports dial through the connect-time SSRF guard (issue #662).

The transports used to build a bare ``httpx.AsyncClient`` from
``MCPServerConfig.url`` with no validation, so a server entry pointed at the
cloud metadata endpoint reached instance credentials.

The tests that matter here check the *transport's own client*, not a helper:
validating the URL text once and then handing it to an unguarded client passes
every literal-address test while leaving the DNS-rebinding window wide open, so
``test_*_installs_the_connect_time_guard`` is the assertion that separates the
two approaches.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
from contextlib import asynccontextmanager
from typing import Any

import pytest

from agentos.mcp.http import assert_supported_mcp_url, mcp_http_client
from agentos.mcp.sse import MCPSSEClient
from agentos.mcp.streamable_http import MCPStreamableHTTPClient
from agentos.mcp.types import MCPServerConfig
from agentos.tools.ssrf_client import (
    ValidatingNetworkBackend,
    validate_metadata_only_address,
)
from agentos.tools.types import SSRFBlockedError, UnsupportedURLSchemeError

METADATA_IP = "169.254.169.254"


def _sse_config(url: str | None) -> MCPServerConfig:
    return MCPServerConfig(name="sse-server", transport="sse", url=url)


def _streamable_config(url: str) -> MCPServerConfig:
    return MCPServerConfig(name="remote", transport="streamable_http", url=url)


def _installed_backend(client: Any) -> Any:
    return client._transport._pool._network_backend


def _resolver(mapping: dict[str, str]):
    """getaddrinfo stand-in: names answer from *mapping*, literals themselves."""
    calls: list[str] = []

    def resolve(host, port=None, *args, **kwargs):
        calls.append(host)
        try:
            ipaddress.ip_address(str(host).strip("[]"))
            answer = str(host)
        except ValueError:
            answer = mapping[str(host)]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answer, port or 80))]

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


# --- the guard is installed on the transport's own client ----------------


@pytest.mark.asyncio
async def test_sse_client_installs_the_connect_time_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK dials through the factory it is handed, so the guard rides both channels.

    ``sse_client`` builds no client of its own: the same instance carries the
    long-lived ``GET`` stream and the POST to the server-advertised endpoint, so
    checking the factory's product covers the endpoint the server picks as well
    as the URL the operator configured.
    """
    from mcp.client import session as session_module
    from mcp.client import sse as transport_module

    captured: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_transport(url: str, *, httpx_client_factory: Any, **_kwargs: Any):
        captured["url"] = url
        captured["http_client"] = httpx_client_factory()
        try:
            yield object(), object()
        finally:
            await captured["http_client"].aclose()

    class FakeSession:
        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def initialize(self) -> None:
            return None

    monkeypatch.setattr(transport_module, "sse_client", fake_transport)
    monkeypatch.setattr(session_module, "ClientSession", lambda *_a, **_k: FakeSession())

    client = MCPSSEClient(_sse_config("http://localhost:9999/sse"))
    await client.connect()
    try:
        assert captured["url"] == "http://localhost:9999/sse"
        backend = _installed_backend(captured["http_client"])
        assert isinstance(backend, ValidatingNetworkBackend)
        assert backend._validator is validate_metadata_only_address
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_streamable_http_client_installs_the_connect_time_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp.client import session as session_module
    from mcp.client import streamable_http as transport_module

    captured: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_transport(url: str, *, http_client: Any = None, **_kwargs: Any):
        captured["url"] = url
        captured["http_client"] = http_client
        yield object(), object(), None

    class FakeSession:
        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def initialize(self) -> None:
            return None

    monkeypatch.setattr(transport_module, "streamable_http_client", fake_transport)
    monkeypatch.setattr(session_module, "ClientSession", lambda *_a, **_k: FakeSession())

    client = MCPStreamableHTTPClient(_streamable_config("https://example.test/mcp"))
    await client.connect()
    try:
        assert captured["url"] == "https://example.test/mcp"
        backend = _installed_backend(captured["http_client"])
        assert isinstance(backend, ValidatingNetworkBackend)
        assert backend._validator is validate_metadata_only_address
    finally:
        await client.close()


# --- what the guard actually blocks --------------------------------------


@pytest.mark.asyncio
async def test_sse_connect_is_blocked_from_the_metadata_address() -> None:
    client = MCPSSEClient(_sse_config(f"http://{METADATA_IP}/sse"))
    try:
        with pytest.raises(SSRFBlockedError) as excinfo:
            await client.connect()
    finally:
        await client.close()

    assert METADATA_IP in str(excinfo.value)


@pytest.mark.asyncio
async def test_sse_connect_is_blocked_when_the_hostname_resolves_to_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The URL text is unremarkable; the address it dials is the credential endpoint."""
    monkeypatch.setattr(socket, "getaddrinfo", _resolver({"mcp.example": METADATA_IP}))

    client = MCPSSEClient(_sse_config("http://mcp.example/sse"))
    try:
        with pytest.raises(SSRFBlockedError) as excinfo:
            await client.connect()
    finally:
        await client.close()

    assert METADATA_IP in str(excinfo.value)


@pytest.mark.asyncio
async def test_metadata_hostnames_are_blocked_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _resolver({"metadata.google.internal": "1.2.3.4"}))

    client = MCPSSEClient(_sse_config("http://metadata.google.internal/sse"))
    try:
        with pytest.raises(SSRFBlockedError):
            await client.connect()
    finally:
        await client.close()


# --- what the guard must keep allowing -----------------------------------


class _LocalServer:
    """A minimal HTTP/1.1 responder on an ephemeral loopback port."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        try:
            conn, _ = self._sock.accept()
            with conn:
                conn.recv(65536)
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                    b"Content-Length: 2\r\nConnection: close\r\n\r\nok"
                )
                conn.shutdown(socket.SHUT_WR)
        except OSError:  # pragma: no cover - the socket closed under us
            pass

    def close(self) -> None:
        self._sock.close()


@pytest.mark.asyncio
async def test_localhost_mcp_servers_are_still_reachable() -> None:
    """The metadata-only policy is deliberate: localhost/LAN MCP is the normal setup.

    The full ``validate_http_url_for_fetch`` policy rejects loopback and private
    ranges, which would break the majority of real MCP configurations.
    """
    server = _LocalServer()
    try:
        async with mcp_http_client(f"http://127.0.0.1:{server.port}/mcp") as client:
            response = await client.get(f"http://127.0.0.1:{server.port}/mcp")
    finally:
        server.close()

    assert response.status_code == 200
    assert response.text == "ok"


# --- scheme and URL validation -------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "gopher://example.test/", "ftp://example.test/mcp", "/mcp", ""],
)
def test_non_http_urls_are_rejected(url: str) -> None:
    with pytest.raises(UnsupportedURLSchemeError):
        assert_supported_mcp_url(url)


def test_http_and_https_urls_are_accepted() -> None:
    assert_supported_mcp_url("http://localhost:3000/mcp")
    assert_supported_mcp_url("https://example.test/mcp")


@pytest.mark.asyncio
async def test_sse_connect_rejects_a_non_http_url() -> None:
    client = MCPSSEClient(_sse_config("file:///etc/passwd"))
    with pytest.raises(UnsupportedURLSchemeError):
        await client.connect()


@pytest.mark.asyncio
async def test_streamable_http_connect_rejects_a_non_http_url() -> None:
    client = MCPStreamableHTTPClient(_streamable_config("file:///etc/passwd"))
    with pytest.raises(UnsupportedURLSchemeError):
        await client.connect()


@pytest.mark.asyncio
async def test_sse_connect_requires_a_url() -> None:
    client = MCPSSEClient(_sse_config(None))
    with pytest.raises(ValueError, match="requires a URL"):
        await client.connect()
