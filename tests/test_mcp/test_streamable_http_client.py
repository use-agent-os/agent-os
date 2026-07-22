from __future__ import annotations

import os
import stat

import pytest

from agentos.mcp.discovery import create_client
from agentos.mcp.streamable_http import FileOAuthStorage, MCPStreamableHTTPClient
from agentos.mcp.types import MCPServerConfig


def test_factory_builds_streamable_http_client() -> None:
    config = MCPServerConfig(
        name="remote",
        transport="streamable_http",
        url="https://example.test/mcp",
    )

    assert isinstance(create_client(config), MCPStreamableHTTPClient)


@pytest.mark.asyncio
async def test_oauth_storage_round_trips_privately(tmp_path) -> None:
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

    storage = FileOAuthStorage(
        "Robinhood Trading",
        "https://agent.robinhood.com/mcp/trading",
        str(tmp_path),
    )
    tokens = OAuthToken(access_token="access-secret", refresh_token="refresh-secret")
    client_info = OAuthClientInformationFull.model_validate(
        {
            "redirect_uris": ["http://127.0.0.1/control/mcp/oauth/callback"],
            "client_id": "client-id",
            "token_endpoint_auth_method": "none",
        }
    )

    await storage.set_tokens(tokens)
    await storage.set_client_info(client_info)

    restored = FileOAuthStorage(
        "Robinhood Trading",
        "https://agent.robinhood.com/mcp/trading",
        str(tmp_path),
    )
    assert (await restored.get_tokens()).access_token == "access-secret"
    assert (await restored.get_client_info()).client_id == "client-id"
    # Windows does not expose POSIX owner-only mode bits through stat/chmod;
    # credential files inherit the current user's state-directory ACL instead.
    if os.name != "nt":
        assert stat.S_IMODE(restored.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(restored.path.parent.stat().st_mode) == 0o700
    assert await restored.is_authenticated() is True

    restored.clear()
    assert not restored.path.exists()
