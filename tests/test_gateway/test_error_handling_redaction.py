from __future__ import annotations

from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from agentos.gateway.audio_transcription import register_audio_transcription_routes
from agentos.gateway.config import GatewayConfig
from agentos.gateway.middleware import ErrorHandlingMiddleware
from agentos.gateway.rpc.registry import RpcContext


def test_error_handling_middleware_production_returns_generic_error_and_error_id() -> None:
    """In production mode (debug=False), unhandled exceptions return generic error + error_id."""
    fake_secret = "sk-proj-" + "A" * 32
    fake_path = "/" + "home/developer/.agentos/db.sqlite"

    async def _failing_endpoint(request: Request) -> JSONResponse:
        raise RuntimeError(
            f"SQL query failed at {fake_path} with {fake_secret}"
        )

    app = Starlette(
        routes=[Route("/fail", _failing_endpoint, methods=["GET"])],
        middleware=[Middleware(ErrorHandlingMiddleware, debug=False)],
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/fail")

    assert resp.status_code == 500
    data = resp.json()
    assert data["code"] == "INTERNAL_ERROR"
    assert data["error"] == "Internal server error"
    assert "error_id" in data
    assert len(data["error_id"]) > 0

    # Ensure sensitive info is NOT leaked in body
    assert "SQL query failed" not in resp.text
    assert "home/developer" not in resp.text
    assert fake_secret not in resp.text


def test_error_handling_middleware_debug_returns_redacted_exception() -> None:
    """In debug mode (debug=True), unhandled exception text is returned with secrets redacted."""
    fake_secret = "sk-ant-" + "B" * 32

    async def _failing_endpoint(request: Request) -> JSONResponse:
        raise RuntimeError(
            f"Connection failed with token {fake_secret} on internal host"
        )

    app = Starlette(
        routes=[Route("/fail", _failing_endpoint, methods=["GET"])],
        middleware=[Middleware(ErrorHandlingMiddleware, debug=True)],
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/fail")

    assert resp.status_code == 500
    data = resp.json()
    assert data["code"] == "INTERNAL_ERROR"
    assert "error_id" in data
    assert "Connection failed" in data["error"]
    # Secret must be redacted
    assert fake_secret not in resp.text


def test_audio_transcription_route_production_returns_generic_error() -> None:
    """In production mode (debug=False), provider exceptions return generic message + error_id."""
    fake_secret = "sk-proj-" + "C" * 32

    class _FailingProvider:
        async def transcribe_audio(self, request: Any) -> Any:
            raise RuntimeError(
                f"ElevenLabs auth error at /var/run/secret {fake_secret}"
            )

    app = Starlette()
    config = GatewayConfig()
    config.debug = False
    config.auth.mode = "none"
    config.audio.enabled = True

    register_audio_transcription_routes(
        app,
        config=config,
        provider_factory=lambda _cfg: _FailingProvider(),
    )

    with TestClient(app) as client:
        resp = client.post(
            "/api/audio/transcribe",
            files={"file": ("test.webm", b"audio-bytes", "audio/webm")},
        )

    assert resp.status_code == 502
    data = resp.json()
    assert data["code"] == "PROVIDER_ERROR"
    assert data["error"] == "Audio transcription failed"
    assert "error_id" in data
    assert fake_secret not in resp.text
    assert "/var/run/secret" not in resp.text


def test_audio_transcription_route_debug_redacts_provider_exception() -> None:
    """In debug mode (debug=True), provider exceptions are returned but secrets are redacted."""
    fake_secret = "ghp_" + "D" * 36

    class _FailingProvider:
        async def transcribe_audio(self, request: Any) -> Any:
            raise RuntimeError(f"ElevenLabs error with secret {fake_secret} failed")

    app = Starlette()
    config = GatewayConfig()
    config.debug = True
    config.auth.mode = "none"
    config.audio.enabled = True

    register_audio_transcription_routes(
        app,
        config=config,
        provider_factory=lambda _cfg: _FailingProvider(),
    )

    with TestClient(app) as client:
        resp = client.post(
            "/api/audio/transcribe",
            files={"file": ("test.webm", b"audio-bytes", "audio/webm")},
        )

    assert resp.status_code == 502
    data = resp.json()
    assert data["code"] == "PROVIDER_ERROR"
    assert "error_id" in data
    assert "ElevenLabs error" in data["error"]
    assert fake_secret not in resp.text


@pytest.mark.asyncio
async def test_rpc_unhandled_exception_redacts_sensitive_text() -> None:
    """RPC dispatcher redacts sensitive strings from unexpected handler exceptions."""
    from agentos.gateway.access import CONTROL_ONLY, ConnectionSurface
    from agentos.gateway.auth import AccessContext
    from agentos.gateway.rpc.registry import RpcRegistry

    fake_secret = "sk-or-v1-" + "E" * 40
    dispatcher = RpcRegistry()

    async def _failing_handler(params: Any, ctx: RpcContext) -> Any:
        raise RuntimeError(f"Crash on token {fake_secret}")

    dispatcher.register("test.failing_method", _failing_handler, CONTROL_ONLY)

    access = AccessContext(
        surface=ConnectionSurface.CONTROL, admitted=True, credential_verified=True
    )
    ctx = RpcContext(conn_id="test", access=access)
    res = await dispatcher.dispatch("req-1", "test.failing_method", {}, ctx)

    assert res.ok is False
    assert res.error is not None
    assert res.error.code == "INTERNAL_ERROR"
    assert fake_secret not in res.error.message
