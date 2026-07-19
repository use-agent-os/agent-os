from starlette.applications import Starlette
from starlette.testclient import TestClient

from agentos.gateway.config import GatewayConfig
from agentos.gateway.control_ui import create_control_ui_routes


def _client() -> TestClient:
    config = GatewayConfig()
    app = Starlette(routes=create_control_ui_routes(config))
    return TestClient(app)


def test_bootstrap_returns_json_context():
    client = _client()
    resp = client.get("/control/api/bootstrap")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    data = resp.json()
    assert set(data) == {"version", "ws_url", "auth_mode", "base_path", "config_path", "features"}
    assert data["base_path"] == "/control"
    assert data["ws_url"].startswith("ws")
    assert "diagnostics" in data["features"]


def test_bootstrap_not_cached():
    client = _client()
    resp = client.get("/control/api/bootstrap")
    assert "no-store" in resp.headers.get("cache-control", "")


def test_spa_fallback_still_serves_html():
    client = _client()
    resp = client.get("/control/health")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
