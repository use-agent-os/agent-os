"""Control UI route exemptions cannot swallow protected API routes."""

from __future__ import annotations

import asyncio

import pytest
from starlette.testclient import TestClient

import agentos.gateway.rpc_config  # noqa: F401  ensure registration
from agentos.gateway.app import create_gateway_app
from agentos.gateway.auth import Principal
from agentos.gateway.config import GatewayConfig
from agentos.gateway.rpc import RpcContext, get_dispatcher


def _config(tmp_path, *, base_path: str, max_requests: int = 120) -> GatewayConfig:
    return GatewayConfig(
        config_path=str(tmp_path / "config.toml"),
        auth={"mode": "token", "token": "test-token"},
        control_ui={"base_path": base_path},
        rate_limit={
            "enabled": True,
            "max_requests": max_requests,
            "window_seconds": 60,
        },
    )


def _patch_base_path(config: GatewayConfig, base_path: str) -> None:
    context = RpcContext(
        conn_id="ui-exemption-test",
        config=config,
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.admin"}),
            is_owner=True,
            authenticated=True,
        ),
    )
    result = asyncio.run(
        get_dispatcher().dispatch(
            "r1",
            "config.patch",
            {"patches": {"control_ui.base_path": base_path}},
            context,
        )
    )
    assert result.error is None, result.error
    assert result.payload["restartRequired"] is True


@pytest.mark.parametrize("base_path", ["/", "/api", "/api/v1"])
def test_runtime_ui_base_path_change_cannot_disable_auth(tmp_path, base_path: str) -> None:
    config = _config(tmp_path, base_path="/control")
    with TestClient(create_gateway_app(config), base_url="http://localhost") as client:
        authorized = client.get(
            "/api/config",
            headers={"authorization": "Bearer test-token"},
        )
        assert authorized.status_code != 401

        _patch_base_path(config, base_path)
        unauthorized = client.get("/api/config")
        assert unauthorized.status_code == 401


@pytest.mark.parametrize("base_path", ["/", "/api", "/api/v1"])
def test_runtime_ui_base_path_change_cannot_disable_rate_limit(
    tmp_path, base_path: str
) -> None:
    config = _config(tmp_path, base_path="/control", max_requests=2)
    headers = {"authorization": "Bearer test-token"}
    with TestClient(create_gateway_app(config), base_url="http://localhost") as client:
        first = client.get("/api/config", headers=headers)
        assert first.status_code != 429

        _patch_base_path(config, base_path)
        second = client.get("/api/config", headers=headers)
        third = client.get("/api/config", headers=headers)
        assert second.status_code != 429
        assert third.status_code == 429


def test_http_token_auth_without_configured_token_fails_closed(tmp_path) -> None:
    config = GatewayConfig(
        config_path=str(tmp_path / "config.toml"),
        auth={"mode": "token", "token": None},
    )

    with TestClient(create_gateway_app(config), base_url="http://localhost") as client:
        response = client.get("/api/config")

    assert response.status_code == 401
