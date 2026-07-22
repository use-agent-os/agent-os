from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agentos.gateway.boot import _discover_configured_mcp_servers
from agentos.gateway.config import GatewayConfig
from agentos.mcp.streamable_http import FileOAuthStorage
from agentos.tools.registry import ToolRegistry


def _oauth_config(tmp_path) -> GatewayConfig:
    return GatewayConfig(
        state_dir=str(tmp_path),
        mcp={
            "enabled": True,
            "servers": [
                {
                    "name": "robinhood-trading",
                    "transport": "streamable_http",
                    "url": "https://agent.robinhood.com/mcp/trading",
                    "oauth": True,
                }
            ],
        },
    )


@pytest.mark.asyncio
async def test_boot_skips_oauth_server_until_user_authenticates(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentos.mcp import discovery

    discover = AsyncMock(return_value=[])
    monkeypatch.setattr(discovery, "discover_and_register", discover)
    monkeypatch.setattr(FileOAuthStorage, "is_authenticated", AsyncMock(return_value=False))

    await _discover_configured_mcp_servers(_oauth_config(tmp_path), ToolRegistry())

    discover.assert_not_awaited()


@pytest.mark.asyncio
async def test_boot_reconnects_oauth_server_after_authentication(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentos.mcp import discovery

    discover = AsyncMock(return_value=["mcp_portfolio"])
    monkeypatch.setattr(discovery, "discover_and_register", discover)
    monkeypatch.setattr(FileOAuthStorage, "is_authenticated", AsyncMock(return_value=True))

    await _discover_configured_mcp_servers(_oauth_config(tmp_path), ToolRegistry())

    discover.assert_awaited_once()
