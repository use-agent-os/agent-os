"""OAuth token lifetime across the Streamable HTTP client's whole life.

The gateway signs in with one client (the one that holds the browser redirect)
and then serves tools from a second client that only reads the saved tokens.
These tests drive that second client against an in-memory MCP server whose
access tokens expire, the way a short-lived provider token (ten minutes) does.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx
import pytest

from agentos.mcp import streamable_http as streamable_http_module
from agentos.mcp.streamable_http import (
    FileOAuthStorage,
    MCPAuthorizationRequiredError,
    MCPStreamableHTTPClient,
)
from agentos.mcp.types import MCPServerConfig

SERVER_URL = "https://mcp.example.test/mcp"
TOKEN_URL = "https://mcp.example.test/token"
TOKEN_LIFETIME = 600


class FakeOAuthMCPServer:
    """A Streamable HTTP MCP server behind short-lived bearer tokens."""

    def __init__(self) -> None:
        self.valid_access_tokens: set[str] = set()
        self.valid_refresh_tokens: set[str] = {"refresh-1"}
        self.refreshes = 0
        self.tool_calls: list[str] = []
        self.requests: list[str] = []

    def issue(self, access: str) -> None:
        self.valid_access_tokens.add(access)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(f"{request.method} {request.url.path}")
        if str(request.url) == TOKEN_URL:
            return self._token(request)
        if request.url.path.startswith("/.well-known/"):
            return httpx.Response(404)
        if str(request.url) != SERVER_URL:
            return httpx.Response(404)
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        if bearer not in self.valid_access_tokens:
            return httpx.Response(401, headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})
        if request.method == "GET":
            return httpx.Response(405)
        if request.method == "DELETE":
            return httpx.Response(204)
        return self._rpc(json.loads(request.content))

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = dict(httpx.QueryParams(request.content.decode()))
        if form.get("grant_type") != "refresh_token":
            return httpx.Response(400, json={"error": "unsupported_grant_type"})
        if form.get("refresh_token") not in self.valid_refresh_tokens:
            return httpx.Response(400, json={"error": "invalid_grant"})
        self.refreshes += 1
        access = f"access-refreshed-{self.refreshes}"
        refresh = f"refresh-{self.refreshes + 1}"
        self.valid_refresh_tokens = {refresh}
        self.issue(access)
        return httpx.Response(
            200,
            json={
                "access_token": access,
                "token_type": "Bearer",
                "expires_in": TOKEN_LIFETIME,
                "refresh_token": refresh,
            },
        )

    def _rpc(self, message: dict[str, Any]) -> httpx.Response:
        method = message.get("method")
        if "id" not in message:
            return httpx.Response(202)
        if method == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "1"},
            }
        elif method == "tools/call":
            self.tool_calls.append(message["params"]["name"])
            result = {"content": [{"type": "text", "text": "ok"}], "isError": False}
        elif method == "tools/list":
            result = {
                "tools": [
                    {"name": name, "inputSchema": {"type": "object"}}
                    for name in ("get_me", "list_social_sets")
                ]
            }
        else:
            result = {}
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": message["id"], "result": result},
            headers={"content-type": "application/json", "mcp-session-id": "s-1"},
        )


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> FakeOAuthMCPServer:
    fake = FakeOAuthMCPServer()

    def fake_mcp_http_client(_url: str, **kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: fake.handle(request)), **kwargs
        )

    monkeypatch.setattr(streamable_http_module, "mcp_http_client", fake_mcp_http_client)
    return fake


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Wall-clock offset in seconds; token expiry is wall-clock based."""
    offset = [0.0]
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + offset[0])
    return offset


def _config(tmp_path: Any, timeout: float = 5.0) -> MCPServerConfig:
    return MCPServerConfig(
        name="Typefully",
        transport="streamable_http",
        url=SERVER_URL,
        oauth=True,
        state_dir=str(tmp_path),
        tool_timeout_seconds=timeout,
    )


async def _sign_in(tmp_path: Any, server: FakeOAuthMCPServer, access: str = "access-0") -> None:
    """Save tokens the way the sign-in client does after the browser redirect."""
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

    storage = FileOAuthStorage("Typefully", SERVER_URL, str(tmp_path))
    await storage.set_client_info(
        OAuthClientInformationFull.model_validate(
            {
                "redirect_uris": ["http://127.0.0.1/control/mcp/oauth/callback"],
                "client_id": "client-id",
                "token_endpoint_auth_method": "none",
            }
        )
    )
    await storage.set_tokens(
        OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=TOKEN_LIFETIME,
            refresh_token="refresh-1",
        )
    )
    server.issue(access)


def _expire(server: FakeOAuthMCPServer, clock: list[float]) -> None:
    clock[0] += TOKEN_LIFETIME + 60
    server.valid_access_tokens.clear()


@pytest.mark.asyncio
async def test_a_connected_client_refreshes_a_token_that_expires_while_it_runs(
    tmp_path: Any, server: FakeOAuthMCPServer, clock: list[float]
) -> None:
    """The production failure: tools died ten minutes after sign-in."""
    await _sign_in(tmp_path, server)
    client = MCPStreamableHTTPClient(_config(tmp_path))
    await client.connect()
    try:
        assert (await client.call_tool("get_me", {})).content == "ok"

        _expire(server, clock)

        assert (await client.call_tool("get_me", {})).content == "ok"
        assert server.refreshes == 1
        assert server.tool_calls == ["get_me", "get_me"]
    finally:
        await client.close()

    saved = await FileOAuthStorage("Typefully", SERVER_URL, str(tmp_path)).get_tokens()
    assert saved.access_token == "access-refreshed-1"
    assert saved.refresh_token == "refresh-2"


@pytest.mark.asyncio
async def test_connecting_with_an_expired_saved_token_refreshes_it_first(
    tmp_path: Any, server: FakeOAuthMCPServer, clock: list[float]
) -> None:
    """A gateway restart after the token lapsed reconnects without a browser."""
    await _sign_in(tmp_path, server)
    _expire(server, clock)

    client = MCPStreamableHTTPClient(_config(tmp_path))
    await client.connect()
    try:
        assert (await client.call_tool("list_social_sets", {})).content == "ok"
    finally:
        await client.close()
    assert server.refreshes == 1


@pytest.mark.asyncio
async def test_a_token_file_without_an_expiry_stamp_ages_from_its_write_time(
    tmp_path: Any, server: FakeOAuthMCPServer, clock: list[float]
) -> None:
    """Files saved before the stamp existed still get refreshed once they lapse."""
    await _sign_in(tmp_path, server)
    path = FileOAuthStorage("Typefully", SERVER_URL, str(tmp_path)).path
    payload = json.loads(path.read_text())
    payload.pop("expires_at", None)
    path.write_text(json.dumps(payload))
    written = time.time()
    os.utime(path, (written, written))
    _expire(server, clock)

    client = MCPStreamableHTTPClient(_config(tmp_path))
    await client.connect()
    try:
        assert (await client.call_tool("get_me", {})).content == "ok"
    finally:
        await client.close()
    assert server.refreshes == 1


@pytest.mark.asyncio
async def test_a_revoked_refresh_token_asks_for_sign_in_instead_of_a_redirect_error(
    tmp_path: Any, server: FakeOAuthMCPServer, clock: list[float]
) -> None:
    """A runtime client cannot open a browser; it must say the server needs sign-in."""
    await _sign_in(tmp_path, server)
    _expire(server, clock)
    server.valid_refresh_tokens.clear()

    client = MCPStreamableHTTPClient(_config(tmp_path))
    with pytest.raises(MCPAuthorizationRequiredError, match="Typefully"):
        await client.connect()
    assert await client.oauth_storage().is_authenticated() is False


@pytest.mark.asyncio
async def test_a_dead_transport_reconnects_on_the_next_call(
    tmp_path: Any, server: FakeOAuthMCPServer
) -> None:
    """One failed request tears the SDK transport down; the client must not stay dead."""
    await _sign_in(tmp_path, server)
    # The in-flight request only fails once the SDK's read timeout passes.
    client = MCPStreamableHTTPClient(_config(tmp_path, timeout=0.5))
    await client.connect()
    try:
        original = server.handle
        failed = False

        def fail_once(request: httpx.Request) -> httpx.Response:
            nonlocal failed
            if (
                not failed
                and request.method == "POST"
                and str(request.url) == SERVER_URL
                and json.loads(request.content).get("method") == "tools/call"
            ):
                failed = True
                return httpx.Response(500)
            return original(request)

        server.handle = fail_once  # type: ignore[method-assign]
        # The request reached the server, so it is reported, not resent.
        with pytest.raises(Exception):  # noqa: B017 - the SDK's own failure shape
            await client.call_tool("get_me", {})
        assert client.is_alive is False
        assert server.tool_calls == []

        assert (await client.call_tool("get_me", {})).content == "ok"
        assert client.is_alive is True
        assert server.tool_calls == ["get_me"]
    finally:
        await client.close()
