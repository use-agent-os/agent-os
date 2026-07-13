from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agentos.artifacts import ArtifactStore


class _FakeSessionManager:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id

    async def get_session(self, session_key: str) -> object | None:
        if session_key == "agent:main:webchat:ok":
            return SimpleNamespace(session_id=self.session_id)
        return None


def _app(tmp_path: Path):
    pytest.importorskip("starlette.testclient")
    from starlette.applications import Starlette

    from agentos.gateway.artifacts import register_artifact_routes
    from agentos.gateway.config import AttachmentsConfig, AuthConfig, GatewayConfig
    from agentos.gateway.middleware import AuthMiddleware

    config = GatewayConfig(
        auth=AuthConfig(mode="token", token="secret"),
        attachments=AttachmentsConfig(media_root=str(tmp_path)),
    )
    app = Starlette(debug=False)
    register_artifact_routes(
        app,
        config=config,
        session_manager=_FakeSessionManager("session-1"),
    )
    app.add_middleware(AuthMiddleware, config=config)
    return app


def _publish(tmp_path: Path):
    return ArtifactStore(tmp_path).publish_bytes(
        b"hello artifact",
        session_id="session-1",
        session_key="agent:main:webchat:ok",
        name="report final.txt",
        mime="text/plain",
        source="publish_artifact",
    )


def test_artifact_download_requires_auth_and_session_scope(tmp_path: Path) -> None:
    pytest.importorskip("starlette.testclient")
    from starlette.testclient import TestClient

    ref = _publish(tmp_path)

    with TestClient(_app(tmp_path)) as client:
        unauthenticated = client.get(
            f"/api/v1/artifacts/{ref.id}?sessionKey=agent:main:webchat:ok"
        )
        wrong_session = client.get(
            f"/api/v1/artifacts/{ref.id}?sessionKey=agent:main:webchat:other",
            headers={"Authorization": "Bearer secret"},
        )
        missing_session = client.get(
            f"/api/v1/artifacts/{ref.id}",
            headers={"Authorization": "Bearer secret"},
        )

    assert unauthenticated.status_code == 401
    assert wrong_session.status_code == 404
    assert missing_session.status_code == 404


def test_artifact_download_serves_file_response_headers_and_ranges(tmp_path: Path) -> None:
    pytest.importorskip("starlette.testclient")
    from starlette.testclient import TestClient

    ref = _publish(tmp_path)

    with TestClient(_app(tmp_path)) as client:
        response = client.get(
            f"/api/v1/artifacts/{ref.id}?sessionKey=agent:main:webchat:ok",
            headers={"Authorization": "Bearer secret"},
        )
        ranged = client.get(
            f"/api/v1/artifacts/{ref.id}?sessionKey=agent:main:webchat:ok",
            headers={"Authorization": "Bearer secret", "Range": "bytes=0-4"},
        )

    assert response.status_code == 200
    assert response.content == b"hello artifact"
    assert response.headers["content-type"].startswith("text/plain")
    assert "attachment" in response.headers["content-disposition"]
    assert "report%20final.txt" in response.headers["content-disposition"]
    assert ranged.status_code == 206
    assert ranged.content == b"hello"


def test_artifact_download_reports_not_found_and_integrity_errors(tmp_path: Path) -> None:
    pytest.importorskip("starlette.testclient")
    from starlette.testclient import TestClient

    ref = _publish(tmp_path)
    ArtifactStore(tmp_path).path_for(ref).write_bytes(b"tampered")

    with TestClient(_app(tmp_path)) as client:
        missing = client.get(
            "/api/v1/artifacts/missing?sessionKey=agent:main:webchat:ok",
            headers={"Authorization": "Bearer secret"},
        )
        mismatch = client.get(
            f"/api/v1/artifacts/{ref.id}?sessionKey=agent:main:webchat:ok",
            headers={"Authorization": "Bearer secret"},
        )

    assert missing.status_code == 404
    assert mismatch.status_code == 409
