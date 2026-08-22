from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

from agentos.gateway.app import create_gateway_app
from agentos.gateway.config import AuthConfig, GatewayConfig
from agentos.gateway.middleware import AuthMiddleware


def test_auth_middleware_extract_token_rejects_query_param() -> None:
    """AuthMiddleware._extract_token extracts from headers only, not query params."""
    middleware = AuthMiddleware(
        app=MagicMock(),
        config=GatewayConfig(auth=AuthConfig(mode="token", token="my-secret")),
    )

    # 1. Query parameter only -> None
    scope_query: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/api/sessions",
        "query_string": b"token=my-secret",
        "headers": [],
    }
    req_query = Request(scope_query)
    assert middleware._extract_token(req_query) is None

    # 2. Authorization: Bearer header -> extracted
    scope_bearer: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/api/sessions",
        "query_string": b"",
        "headers": [(b"authorization", b"Bearer my-secret")],
    }
    req_bearer = Request(scope_bearer)
    assert middleware._extract_token(req_bearer) == "my-secret"

    # 3. x-agentos-token header -> extracted
    scope_custom: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/api/sessions",
        "query_string": b"",
        "headers": [(b"x-agentos-token", b"my-secret")],
    }
    req_custom = Request(scope_custom)
    assert middleware._extract_token(req_custom) == "my-secret"


def test_gateway_endpoints_reject_query_token() -> None:
    """REST and RPC endpoints reject ?token= query parameter and require headers."""
    config = GatewayConfig(auth=AuthConfig(mode="token", token="secret-123"))
    app = create_gateway_app(config=config)

    with TestClient(app, base_url="http://localhost") as client:
        # 1. Query token is rejected with 401 Unauthorized
        res_query = client.get("/api/sessions?token=secret-123")
        assert res_query.status_code == 401
        assert res_query.json().get("code") == "UNAUTHORIZED"

        res_config_query = client.get("/api/config?token=secret-123")
        assert res_config_query.status_code == 401

        # 2. Bearer header is accepted
        res_bearer = client.get(
            "/api/sessions",
            headers={"Authorization": "Bearer secret-123"},
        )
        assert res_bearer.status_code == 200

        # 3. x-agentos-token header is accepted
        res_header = client.get(
            "/api/sessions",
            headers={"x-agentos-token": "secret-123"},
        )
        assert res_header.status_code == 200


def test_boot_gateway_disables_uvicorn_access_log(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gateway boot sequence configures uvicorn with access_log=False to prevent URL leaks."""
    import asyncio
    from types import SimpleNamespace

    from agentos.gateway import boot

    captured_config: dict[str, Any] = {}

    class FakeTurnRunner:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def set_session_lock_provider(self, _provider: Any) -> None:
            pass

    class FakeUvicornConfig:
        def __init__(self, **kwargs: Any) -> None:
            captured_config.update(kwargs)

    class FakeServer:
        def __init__(self, _config: Any) -> None:
            self.should_exit = False

        async def serve(self) -> None:
            return None

    async def fake_build_services(**kwargs: Any) -> Any:
        config = kwargs["config"]

        async def close() -> None:
            return None

        return SimpleNamespace(
            provider_selector=object(),
            tool_registry=object(),
            session_manager=object(),
            skill_loader=object(),
            usage_tracker=object(),
            config=config,
            memory_sync_managers={},
            model_catalog=None,
            memory_retrievers={},
            turn_capture_services={},
            cron_scheduler=None,
            task_runtime=None,
            agent_registry=None,
            memory_managers={},
            memory_stores={},
            _turn_runner_ref=[],
            close=close,
        )

    def fake_create_background_task(coro: Any) -> Any:
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        return asyncio.create_task(asyncio.sleep(0))

    monkeypatch.setattr("agentos.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr(boot, "build_services", fake_build_services)
    monkeypatch.setattr(boot, "_setup_file_logging", lambda config: None)
    monkeypatch.setattr(boot, "emit_skill_filter_banner", lambda config: None)
    monkeypatch.setattr(boot, "create_background_task", fake_create_background_task)
    monkeypatch.setattr(boot.uvicorn, "Config", FakeUvicornConfig)
    monkeypatch.setattr(boot.uvicorn, "Server", FakeServer)
    monkeypatch.setattr("agentos.gateway.pidlock.GatewayPidLock.acquire", lambda self: None)
    monkeypatch.setattr("agentos.gateway.pidlock.GatewayPidLock.release", lambda self: None)

    config = GatewayConfig(
        state_dir=str(tmp_path / "state"),
        workspace_dir=str(tmp_path / "workspace"),
        control_ui={"enabled": False},
        channels={"channels": []},
    )

    async def run_case() -> None:
        server = await boot.start_gateway_server(config=config, run=True)
        try:
            assert captured_config.get("access_log") is False
        finally:
            await server.close()

    asyncio.run(run_case())
