from __future__ import annotations

from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from agentos.gateway import control_ui
from agentos.gateway.config import ControlUiConfig, GatewayConfig

_INDEX = """\
<!doctype html>
<html>
  <head>
    <base data-agentos-control-base href="./">
    <script type="module" src="./assets/app-a1b2c3.js"></script>
  </head>
  <body><div id="root"></div></body>
</html>
"""


@pytest.fixture
def dist_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text(_INDEX, encoding="utf-8")
    (assets / "app-a1b2c3.js").write_text("export {};\n", encoding="utf-8")
    monkeypatch.setattr(control_ui, "_DIST_DIR", dist)
    return dist


def _client(config: GatewayConfig | None = None) -> TestClient:
    resolved = config or GatewayConfig()
    app = Starlette(routes=control_ui.create_control_ui_routes(resolved))
    return TestClient(app)


def test_bootstrap_returns_json_context(dist_dir: Path) -> None:
    response = _client().get("/control/api/bootstrap")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert "no-store" in response.headers["cache-control"]
    data = response.json()
    assert set(data) == {
        "version",
        "ws_url",
        "auth_mode",
        "base_path",
        "config_path",
        "features",
    }
    assert data["base_path"] == "/control"
    assert data["ws_url"].startswith("ws")
    assert "diagnostics" in data["features"]


def test_spa_deep_link_serves_uncached_shell_with_runtime_base(dist_dir: Path) -> None:
    response = _client().get("/control/health")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "no-store" in response.headers["cache-control"]
    assert (
        '<base data-agentos-control-base="/control" href="/control/static/dist/">'
        in response.text
    )
    assert response.text.count("data-agentos-control-base") == 1


def test_custom_base_path_serves_deep_link_and_assets(dist_dir: Path) -> None:
    config = GatewayConfig()
    config.control_ui.base_path = "/console"
    client = _client(config)

    shell = client.get("/console/mcp/oauth/callback")
    asset = client.get("/console/static/dist/assets/app-a1b2c3.js")

    assert shell.status_code == 200
    assert (
        '<base data-agentos-control-base="/console" href="/console/static/dist/">'
        in shell.text
    )
    assert asset.status_code == 200


@pytest.mark.parametrize(
    "base_path",
    (
        "",
        "/",
        "console",
        "//evil.example",
        "/api",
        "/api/v1",
        "/ws",
        "/ws/admin",
        "/console?debug=1",
        "/console#fragment",
        "/a/../b",
        "/a//b",
    ),
)
def test_unsafe_or_conflicting_base_paths_fail_fast(base_path: str) -> None:
    with pytest.raises(ValueError, match="control_ui.base_path"):
        ControlUiConfig(base_path=base_path)


def test_missing_dist_returns_actionable_uncached_503(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(control_ui, "_DIST_DIR", tmp_path / "missing-dist")

    response = _client().get("/control/chat")

    assert response.status_code == 503
    assert "no-store" in response.headers["cache-control"]
    assert "python scripts/build_control_ui.py build" in response.text
    assert str(tmp_path) not in response.text


def test_bundle_without_runtime_base_marker_returns_503(
    dist_dir: Path,
) -> None:
    (dist_dir / "index.html").write_text(
        '<!doctype html><html><body><div id="root"></div></body></html>',
        encoding="utf-8",
    )

    response = _client().get("/control/")

    assert response.status_code == 503
    assert "no-store" in response.headers["cache-control"]


def test_runtime_base_injection_escapes_attribute_content(dist_dir: Path) -> None:
    shell = control_ui._render_spa_shell('/console"><script>alert(1)</script>')

    assert (
        'data-agentos-control-base="/console&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;"'
        in shell
    )
    assert 'href="/console&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;/static/dist/"' in shell
    assert "<script>alert(1)</script>" not in shell


def test_bootstrap_includes_config_path(dist_dir: Path, tmp_path: Path) -> None:
    config = GatewayConfig()
    config.config_path = str(tmp_path / "AgentOS Config.toml")

    response = _client(config).get("/control/api/bootstrap")

    assert response.status_code == 200
    assert response.json()["config_path"] == str(config.config_path)


def test_bootstrap_ws_url_uses_client_reachable_wildcard_host(dist_dir: Path) -> None:
    config = GatewayConfig()
    config.host = "0.0.0.0"
    config.port = 20002

    response = _client(config).get("/control/api/bootstrap")

    assert response.status_code == 200
    assert response.json()["ws_url"] == "ws://127.0.0.1:20002/ws"
