"""AOS-06: an ``Upgrade: websocket`` header never exempts an HTTP request.

``AuthMiddleware`` is a ``BaseHTTPMiddleware``, so real WebSocket handshakes
(websocket scopes) never reach it — they authenticate in ``websocket.py``. A
plain HTTP request that merely carries the header used to skip every
admission branch, exposing status, metrics, uploads and paid transcription.
"""

from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient

from agentos.gateway.app import create_gateway_app
from agentos.gateway.config import AuthConfig, GatewayConfig

_TOKEN = "dummy-token-aos06-abcdefghijklmnop"
_PROXY = "10.0.0.5"
_REMOTE = ("198.51.100.7", 40000)
_UPGRADE_HEADERS = (
    {"Upgrade": "websocket"},
    {"Upgrade": "WebSocket"},
    {"Upgrade": "websocket", "Connection": "keep-alive"},
)
_TEXT_FILE = {"file": ("x.txt", b"dummy upload body\n", "text/plain")}
_AUDIO_FILE = {"file": ("a.webm", b"audio-bytes", "audio/webm")}


def _token_client(peer: tuple[str, int] = _REMOTE) -> TestClient:
    config = GatewayConfig(host="0.0.0.0", auth=AuthConfig(mode="token", token=_TOKEN))
    return TestClient(create_gateway_app(config), client=peer, base_url="http://127.0.0.1:18791")


def _proxy_client(peer: tuple[str, int] = _REMOTE) -> TestClient:
    config = GatewayConfig(
        host="0.0.0.0", auth=AuthConfig(mode="trusted-proxy", trusted_proxy=_PROXY)
    )
    config.audio.enabled = True
    return TestClient(create_gateway_app(config), client=peer, base_url="http://127.0.0.1:18791")


@pytest.mark.parametrize("headers", _UPGRADE_HEADERS)
@pytest.mark.parametrize("path", ["/api/system/status", "/metrics"])
def test_token_mode_upgrade_header_does_not_skip_auth(path: str, headers: dict[str, str]) -> None:
    client = _token_client()
    assert client.get(path, headers=headers).status_code == 401
    authed = {**headers, "Authorization": f"Bearer {_TOKEN}"}
    assert client.get(path, headers=authed).status_code == 200


@pytest.mark.parametrize("headers", _UPGRADE_HEADERS)
def test_trusted_proxy_mode_upgrade_header_does_not_skip_auth(headers: dict[str, str]) -> None:
    client = _proxy_client()
    assert client.get("/api/system/status", headers=headers).status_code == 401
    assert client.get("/metrics", headers=headers).status_code == 401
    upload = client.post("/api/v1/files/upload", files=_TEXT_FILE, headers=headers)
    assert upload.status_code == 401
    assert "file_uuid" not in upload.text
    transcribe = client.post("/api/audio/transcribe", files=_AUDIO_FILE, headers=headers)
    assert transcribe.status_code == 401


def test_trusted_proxy_upload_and_transcribe_self_check_admission() -> None:
    """The handlers refuse a non-proxy peer even with the middleware bypassed."""

    from starlette.applications import Starlette

    from agentos.gateway.audio_transcription import register_audio_transcription_routes
    from agentos.gateway.uploads import UploadStore, register_upload_routes

    calls: list[Any] = []

    class _Provider:
        async def transcribe(self, *args: Any, **kwargs: Any) -> Any:
            calls.append((args, kwargs))
            raise AssertionError("provider must not be called for an unadmitted peer")

    config = GatewayConfig(
        host="0.0.0.0", auth=AuthConfig(mode="trusted-proxy", trusted_proxy=_PROXY)
    )
    config.audio.enabled = True
    app = Starlette()
    register_upload_routes(
        app,
        config=config,
        store=UploadStore(marker_dir=None, ttl_seconds=600, max_file_bytes=1024),
    )
    register_audio_transcription_routes(
        app, config=config, provider_factory=lambda _cfg: _Provider()
    )

    direct = TestClient(app, client=_REMOTE)
    assert direct.post("/api/v1/files/upload", files=_TEXT_FILE).status_code == 401
    assert direct.post("/api/audio/transcribe", files=_AUDIO_FILE).status_code == 401
    assert calls == []

    via_proxy = TestClient(app, client=(_PROXY, 40000))
    resp = via_proxy.post("/api/v1/files/upload", files=_TEXT_FILE)
    assert resp.status_code == 200
    assert "file_uuid" in resp.json()


def test_real_websocket_handshake_still_authenticates_itself() -> None:
    client = _token_client()

    def connect(token: str | None) -> dict[str, Any]:
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["event"] == "connect.challenge"
            params: dict[str, Any] = {"minProtocol": 3, "maxProtocol": 3}
            if token is not None:
                params["auth"] = {"token": token}
            ws.send_json({"type": "req", "id": "c", "method": "connect", "params": params})
            result: dict[str, Any] = ws.receive_json()
            return result

    assert connect(None)["error"]["code"] == "UNAUTHORIZED"
    assert connect(_TOKEN)["type"] == "hello-ok"
