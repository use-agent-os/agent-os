"""AOS-07: uvicorn must not rewrite the transport peer from ``X-Forwarded-For``.

uvicorn defaults to ``proxy_headers=True`` with ``forwarded_allow_ips`` of
``127.0.0.1``, which wraps the app in ``ProxyHeadersMiddleware``: any loopback
process could then send ``X-Forwarded-For: <trusted proxy>`` and be admitted
in trusted-proxy mode. The app is served here through ``uvicorn.Config`` with
the exact kwargs ``gateway run`` uses.
"""

from __future__ import annotations

from typing import Any

import pytest
import uvicorn
from starlette.testclient import TestClient

from agentos.gateway.app import create_gateway_app
from agentos.gateway.boot import uvicorn_config_kwargs
from agentos.gateway.config import AuthConfig, GatewayConfig

_PROXY = "10.0.0.5"
_SPOOF = {"X-Forwarded-For": _PROXY}


def _served(config: GatewayConfig) -> Any:
    uv_config = uvicorn.Config(**uvicorn_config_kwargs(create_gateway_app(config), config))
    uv_config.load()
    return uv_config.loaded_app


@pytest.fixture
def proxy_config() -> GatewayConfig:
    return GatewayConfig(
        host="0.0.0.0", auth=AuthConfig(mode="trusted-proxy", trusted_proxy=_PROXY)
    )


def test_gateway_run_disables_uvicorn_proxy_headers(
    proxy_config: GatewayConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Even an operator-set FORWARDED_ALLOW_IPS must not turn the rewrite on.
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    kwargs = uvicorn_config_kwargs(object(), proxy_config)
    uv_config = uvicorn.Config(**kwargs)
    assert uv_config.proxy_headers is False


@pytest.mark.parametrize("path", ["/api/sessions", "/api/system/status"])
def test_loopback_peer_cannot_spoof_trusted_proxy_via_xff(
    proxy_config: GatewayConfig, path: str
) -> None:
    client = TestClient(
        _served(proxy_config), client=("127.0.0.1", 40000), base_url="http://127.0.0.1:18791"
    )
    assert client.get(path).status_code == 401
    assert client.get(path, headers=_SPOOF).status_code == 401


def test_loopback_peer_cannot_spoof_trusted_proxy_for_rpc(proxy_config: GatewayConfig) -> None:
    client = TestClient(
        _served(proxy_config), client=("127.0.0.1", 40000), base_url="http://127.0.0.1:18791"
    )
    resp = client.post("/api/v1/rpc", json={"method": "config.get", "params": {}}, headers=_SPOOF)
    assert resp.status_code == 401


def test_real_trusted_proxy_peer_is_still_admitted(proxy_config: GatewayConfig) -> None:
    client = TestClient(
        _served(proxy_config), client=(_PROXY, 40000), base_url="http://127.0.0.1:18791"
    )
    headers = {"X-Forwarded-For": "203.0.113.9"}
    assert client.get("/api/system/status", headers=headers).status_code == 200
    assert client.get("/api/sessions", headers=headers).status_code == 200
