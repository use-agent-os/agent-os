from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agentos.gateway.auth import Principal
from agentos.gateway.config import GatewayConfig, MCPServerEntry
from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.tools.registry import ToolRegistry


def _ctx(tmp_path) -> RpcContext:
    config = GatewayConfig(
        config_path=str(tmp_path / "agentos.toml"),
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
    return RpcContext(
        conn_id="test",
        config=config,
        tool_registry=ToolRegistry(),
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.admin"}),
            is_owner=True,
            authenticated=True,
        ),
    )


@pytest.mark.asyncio
async def test_mcp_status_reports_configured_oauth_server(tmp_path) -> None:
    result = await get_dispatcher().dispatch("r1", "mcp.status", {}, _ctx(tmp_path))

    assert result.error is None
    assert result.payload == {
        "enabled": True,
        "servers": [
            {
                "name": "robinhood-trading",
                "transport": "streamable_http",
                "url": "https://agent.robinhood.com/mcp/trading",
                "oauth": True,
                "authenticated": False,
                "connected": False,
                "tools": [],
            }
        ],
    }


@pytest.mark.asyncio
async def test_mcp_connect_requests_authorization_before_network(tmp_path) -> None:
    result = await get_dispatcher().dispatch(
        "r1",
        "mcp.connect",
        {"name": "robinhood-trading"},
        _ctx(tmp_path),
    )

    assert result.error is None
    assert result.payload["connected"] is False
    assert result.payload["authorizationRequired"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("mcp.connect", {"name": "robinhood-trading"}),
        (
            "mcp.oauth.start",
            {
                "name": "robinhood-trading",
                "redirectUri": "http://127.0.0.1/control/mcp/oauth/callback",
            },
        ),
    ],
)
async def test_mcp_runtime_disabled_rejects_live_connection(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    params: dict[str, str],
) -> None:
    from agentos.gateway import rpc_mcp

    ctx = _ctx(tmp_path)
    ctx.config.mcp.enabled = False
    activate = AsyncMock()
    oauth_client = MagicMock()
    monkeypatch.setattr(rpc_mcp, "_activate", activate)
    monkeypatch.setattr(rpc_mcp, "MCPStreamableHTTPClient", oauth_client)

    result = await get_dispatcher().dispatch("r1", method, params, ctx)

    assert result.error is not None
    assert result.error.code == "INVALID_REQUEST"
    assert result.error.message == "MCP runtime is disabled"
    activate.assert_not_awaited()
    oauth_client.assert_not_called()


@pytest.mark.asyncio
async def test_oauth_rpc_completes_pending_browser_flow(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentos.gateway import rpc_mcp

    class FakeOAuthClient:
        def __init__(self, config, *, redirect_handler, callback_handler) -> None:
            self.redirect_handler = redirect_handler
            self.callback_handler = callback_handler

        async def connect(self) -> None:
            await self.redirect_handler("https://example.test/authorize?state=state-123")
            code, state = await self.callback_handler()
            assert (code, state) == ("code-456", "state-123")

        async def close(self) -> None:
            return None

    activate = AsyncMock(return_value=["mcp_portfolio"])
    monkeypatch.setattr(rpc_mcp, "MCPStreamableHTTPClient", FakeOAuthClient)
    monkeypatch.setattr(rpc_mcp, "_activate", activate)
    ctx = _ctx(tmp_path)

    started = await get_dispatcher().dispatch(
        "r1",
        "mcp.oauth.start",
        {
            "name": "robinhood-trading",
            "redirectUri": "http://127.0.0.1/control/mcp/oauth/callback",
        },
        ctx,
    )
    assert started.error is None
    assert started.payload["authorizationRequired"] is True
    assert started.payload["state"] == "state-123"

    completed = await get_dispatcher().dispatch(
        "r2",
        "mcp.oauth.complete",
        {"code": "code-456", "state": "state-123"},
        ctx,
    )
    assert completed.error is None
    assert completed.payload == {
        "connected": True,
        "authorizationRequired": False,
        "tools": ["mcp_portfolio"],
    }
    activate.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_config_patch_preserves_redacted_headers_after_removal(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    ctx.config.mcp.servers.insert(
        0,
        MCPServerEntry(
            name="plain-server",
            transport="streamable_http",
            url="https://example.test/mcp",
        ),
    )
    ctx.config.mcp.servers[1].headers = {"Authorization": "Bearer secret-token"}
    dispatcher = get_dispatcher()

    public = await dispatcher.dispatch("r1", "config.get", {}, ctx)
    assert public.error is None
    servers = public.payload["mcp"]["servers"]
    assert servers[1]["headers"]["Authorization"] == "[redacted]"

    patched = await dispatcher.dispatch(
        "r2",
        "config.patch",
        {"patches": {"mcp.servers": servers[1:]}},
        ctx,
    )

    assert patched.error is None
    assert ctx.config.mcp.servers[0].name == "robinhood-trading"
    assert ctx.config.mcp.servers[0].headers["Authorization"] == "Bearer secret-token"
    persisted = (tmp_path / "agentos.toml").read_text(encoding="utf-8")
    assert "Bearer secret-token" in persisted
    assert "[redacted]" not in persisted
