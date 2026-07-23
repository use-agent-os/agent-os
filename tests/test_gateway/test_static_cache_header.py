"""Cache policy tests for the built React Control UI."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from agentos.gateway import control_ui
from agentos.gateway.config import GatewayConfig


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv("AGENTOS_STATIC_NO_CACHE", raising=False)
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text(
        (
            "<!doctype html><html><head>"
            '<base data-agentos-control-base href="./">'
            '</head><body><div id="root"></div></body></html>'
        ),
        encoding="utf-8",
    )
    (assets / "app-a1b2c3.js").write_text("export {};\n", encoding="utf-8")
    (dist / "theme-bootstrap.js").write_text(
        "document.documentElement.dataset.theme = 'light';\n",
        encoding="utf-8",
    )
    (dist / "THIRD_PARTY_LICENSES.txt").write_text(
        "Generated dependency licenses\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(control_ui, "_DIST_DIR", dist)
    app = Starlette(routes=control_ui.create_control_ui_routes(GatewayConfig()))
    return TestClient(app)


def test_built_asset_carries_immutable_cache_control(client: TestClient) -> None:
    response = client.get("/control/static/dist/assets/app-a1b2c3.js")

    assert response.status_code == 200, response.text
    cache = response.headers.get("cache-control", "")
    assert "public" in cache
    assert "max-age=31536000" in cache
    assert "immutable" in cache


@pytest.mark.parametrize(
    "path",
    ("assets/app-a1b2c3.js", r"assets\app-a1b2c3.js"),
)
def test_fingerprinted_asset_classification_is_cross_platform(path: str) -> None:
    assert control_ui._is_fingerprinted_asset_path(path) is True


def test_spa_shell_is_not_cached(client: TestClient) -> None:
    response = client.get("/control/")

    assert response.status_code == 200
    assert "no-store" in response.headers.get("cache-control", "")


def test_direct_built_index_is_not_cached(client: TestClient) -> None:
    response = client.get("/control/static/dist/index.html")

    assert response.status_code == 200
    assert "no-store" in response.headers.get("cache-control", "")
    assert "immutable" not in response.headers.get("cache-control", "")


def test_generated_license_ledger_is_not_cached(client: TestClient) -> None:
    response = client.get("/control/static/dist/THIRD_PARTY_LICENSES.txt")

    assert response.status_code == 200
    assert "no-store" in response.headers.get("cache-control", "")
    assert "immutable" not in response.headers.get("cache-control", "")


def test_stable_theme_bootstrap_is_not_cached(client: TestClient) -> None:
    response = client.get("/control/static/dist/theme-bootstrap.js")

    assert response.status_code == 200
    assert "no-store" in response.headers.get("cache-control", "")
    assert "immutable" not in response.headers.get("cache-control", "")


def test_env_rollback_disables_asset_cache_control(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTOS_STATIC_NO_CACHE", "1")

    response = client.get("/control/static/dist/assets/app-a1b2c3.js")

    assert response.status_code == 200
    cache = response.headers.get("cache-control", "")
    assert "max-age=31536000" not in cache
    assert "immutable" not in cache


def test_missing_asset_does_not_add_cache_header(client: TestClient) -> None:
    response = client.get("/control/static/dist/assets/does-not-exist.js")

    assert response.status_code == 404
    cache = response.headers.get("cache-control", "")
    assert "no-store" in cache
    assert "max-age=31536000" not in cache
    assert "immutable" not in cache
