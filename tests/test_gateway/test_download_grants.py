"""Ticketed, connection-bound download URLs (#3693)."""

from __future__ import annotations

import io
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from agentos.artifacts import ArtifactStore
from agentos.attachment_refs import write_transcript_material
from agentos.gateway.access import ConnectionSurface
from agentos.gateway.auth import AccessContext
from agentos.gateway.config import (
    ArtifactsConfig,
    AttachmentsConfig,
    AuthConfig,
    GatewayConfig,
)
from agentos.gateway.download_grants import (
    DownloadGrantRegistry,
    content_disposition,
    get_download_grants,
    make_thumbnail,
    register_download_routes,
)
from agentos.gateway.middleware import AuthMiddleware
from agentos.gateway.rpc import RpcContext, get_dispatcher

SESSION_KEY = "agent:main:webchat:ok"


class _FakeSessionManager:
    async def get_session(self, session_key: str) -> object | None:
        if session_key == SESSION_KEY:
            return SimpleNamespace(session_id="session-1")
        return None


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def _config(tmp_path: Path, **kwargs: Any) -> GatewayConfig:
    return GatewayConfig(
        auth=AuthConfig(mode="token", token="secret"),
        attachments=AttachmentsConfig(media_root=str(tmp_path)),
        **kwargs,
    )


def _publish(tmp_path: Path, payload: bytes = b"hello artifact", **kwargs: Any):  # noqa: ANN202
    return ArtifactStore(tmp_path).publish_bytes(
        payload,
        session_id="session-1",
        session_key=SESSION_KEY,
        name=kwargs.pop("name", "report final.txt"),
        mime=kwargs.pop("mime", "text/plain"),
        source="publish_artifact",
    )


def _png(width: int, height: int) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(out, format="PNG")
    return out.getvalue()


def _ctx(config: GatewayConfig, conn_id: str = "conn-1") -> RpcContext:
    return RpcContext(
        conn_id=conn_id,
        access=AccessContext(
            surface=ConnectionSurface.CONTROL, admitted=True, credential_verified=True
        ),
        config=config,
        session_manager=_FakeSessionManager(),
    )


async def _mint(config: GatewayConfig, params: dict[str, Any], conn_id: str = "conn-1") -> Any:
    return await get_dispatcher().dispatch(
        "r1", "artifacts.download", params, _ctx(config, conn_id)
    )


def _route_app(
    config: GatewayConfig, *, alive: set[str] | None = None, registry: Any = None
) -> Starlette:
    app = Starlette()
    live = alive if alive is not None else {"conn-1"}
    register_download_routes(
        app,
        config=config,
        session_manager=_FakeSessionManager(),
        registry=registry,
        connection_alive=lambda conn_id: conn_id in live,
    )
    app.add_middleware(AuthMiddleware, config=config)
    return app


@pytest.fixture(autouse=True)
def _clean_grants() -> None:
    get_download_grants().clear()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_ticket_is_32_random_bytes_url_safe(self) -> None:
        grant = DownloadGrantRegistry().mint("c", "artifact", "art-1", SESSION_KEY)
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", grant.ticket)
        assert grant.path == f"/api/v1/artifacts/download/c/{grant.ticket}"

    def test_five_minute_ttl(self) -> None:
        clock = _Clock()
        registry = DownloadGrantRegistry(clock=clock)
        grant = registry.mint("c", "artifact", "art-1", SESSION_KEY)
        assert grant.expires_at == clock.now + 300
        clock.now += 299
        assert registry.lookup("c", grant.ticket) == grant
        clock.now += 1
        assert registry.lookup("c", grant.ticket) is None
        assert registry.live_count() == 0

    def test_bound_to_the_issuing_connection(self) -> None:
        registry = DownloadGrantRegistry()
        grant = registry.mint("c", "artifact", "art-1", SESSION_KEY)
        assert registry.lookup("other", grant.ticket) is None

    def test_at_most_128_per_connection_oldest_evicted(self) -> None:
        registry = DownloadGrantRegistry()
        first = registry.mint("c", "artifact", "art-0", SESSION_KEY)
        for index in range(1, 129):
            registry.mint("c", "artifact", f"art-{index}", SESSION_KEY)
        assert registry.live_count("c") == 128
        assert registry.lookup("c", first.ticket) is None
        registry.mint("other", "artifact", "art-x", SESSION_KEY)
        assert registry.live_count("other") == 1

    def test_revoked_when_the_connection_closes(self) -> None:
        registry = DownloadGrantRegistry()
        grant = registry.mint("c", "artifact", "art-1", SESSION_KEY)
        assert registry.revoke_connection("c") == 1
        assert registry.lookup("c", grant.ticket) is None


# ---------------------------------------------------------------------------
# RPC
# ---------------------------------------------------------------------------


class TestRpc:
    @pytest.mark.asyncio
    async def test_mints_an_artifact_url(self, tmp_path: Path) -> None:
        ref = _publish(tmp_path)
        res = await _mint(_config(tmp_path), {"session_key": SESSION_KEY, "artifact_id": ref.id})
        assert res.ok, res.error
        payload = res.payload
        assert payload["url"].startswith("/api/v1/artifacts/download/conn-1/")
        assert payload["kind"] == "artifact"
        assert payload["name"] == ref.name
        assert payload["mime"] == "text/plain"
        assert payload["size"] == ref.size
        assert payload["ttlSeconds"] == 300

    @pytest.mark.asyncio
    async def test_mints_an_attachment_url_with_camel_case(self, tmp_path: Path) -> None:
        sha, _, _ = write_transcript_material(
            media_root=tmp_path, session_id="session-1", payload=b"attached"
        )
        res = await _mint(
            _config(tmp_path),
            {
                "sessionKey": SESSION_KEY,
                "attachmentSha256": sha,
                "name": "notes.txt",
                "mime": "text/plain",
                "transport": "http",
            },
        )
        assert res.ok, res.error
        assert res.payload["kind"] == "attachment"
        assert res.payload["name"] == "notes.txt"

    @pytest.mark.asyncio
    async def test_unknown_or_foreign_target_is_not_found(self, tmp_path: Path) -> None:
        ref = _publish(tmp_path)
        config = _config(tmp_path)
        other_session = await _mint(
            config, {"session_key": "agent:main:webchat:other", "artifact_id": ref.id}
        )
        assert other_session.error.code == "NOT_FOUND"
        assert other_session.error.message == "artifact not found"  # no stray quotes
        missing = await _mint(config, {"session_key": SESSION_KEY, "artifact_id": "art-nope"})
        assert missing.error.code == "NOT_FOUND"
        assert get_download_grants().live_count() == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "params",
        [
            {"artifact_id": "art-1"},
            {"session_key": SESSION_KEY},
            {"session_key": SESSION_KEY, "artifact_id": "a", "attachment_sha256": "b"},
            {"session_key": SESSION_KEY, "artifact_id": "a", "transport": "ws"},
        ],
    )
    async def test_invalid_params(self, tmp_path: Path, params: dict[str, Any]) -> None:
        res = await _mint(_config(tmp_path), params)
        assert res.error.code == "INVALID_REQUEST"

    @pytest.mark.asyncio
    async def test_refused_off_a_websocket(self, tmp_path: Path) -> None:
        """An HTTP RPC caller has no socket for the grant to die with."""
        ref = _publish(tmp_path)
        res = await _mint(
            _config(tmp_path), {"session_key": SESSION_KEY, "artifact_id": ref.id}, conn_id="http"
        )
        assert res.error.code == "INVALID_REQUEST"


# ---------------------------------------------------------------------------
# HTTP route
# ---------------------------------------------------------------------------


async def _artifact_url(tmp_path: Path, config: GatewayConfig, **publish: Any) -> str:
    ref = _publish(tmp_path, **publish)
    res = await _mint(config, {"session_key": SESSION_KEY, "artifact_id": ref.id})
    assert res.ok, res.error
    return str(res.payload["url"])


class TestRoute:
    @pytest.mark.asyncio
    async def test_serves_without_authorization_with_sandbox_headers(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        url = await _artifact_url(tmp_path, config)
        client = TestClient(_route_app(config))
        resp = client.get(url)
        assert resp.status_code == 200
        assert resp.content == b"hello artifact"
        assert resp.headers["content-security-policy"] == "default-src 'none'; sandbox"
        assert resp.headers["content-disposition"].startswith("attachment;")
        assert "report%20final.txt" in resp.headers["content-disposition"]
        assert resp.headers["cache-control"] == "no-store"
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["content-type"].startswith("text/plain")

    @pytest.mark.asyncio
    async def test_head_and_single_range(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        url = await _artifact_url(tmp_path, config)
        client = TestClient(_route_app(config))
        head = client.head(url)
        assert head.status_code == 200
        assert head.content == b""
        assert head.headers["content-length"] == str(len(b"hello artifact"))
        ranged = client.get(url, headers={"Range": "bytes=0-4"})
        assert ranged.status_code == 206
        assert ranged.content == b"hello"
        assert ranged.headers["content-range"] == "bytes 0-4/14"
        assert ranged.headers["content-security-policy"] == "default-src 'none'; sandbox"

    @pytest.mark.asyncio
    async def test_several_ranges_get_the_whole_file(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        url = await _artifact_url(tmp_path, config)
        resp = TestClient(_route_app(config)).get(url, headers={"Range": "bytes=0-1,4-5"})
        assert resp.status_code == 200
        assert resp.content == b"hello artifact"

    @pytest.mark.asyncio
    async def test_thumbnail_variant(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        url = await _artifact_url(
            tmp_path, config, payload=_png(2400, 600), name="wide.png", mime="image/png"
        )
        resp = TestClient(_route_app(config)).get(url + "?variant=thumbnail")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
        assert resp.headers["content-security-policy"] == "default-src 'none'; sandbox"
        from PIL import Image

        with Image.open(io.BytesIO(resp.content)) as image:
            assert image.size == (1200, 300)

    @pytest.mark.asyncio
    async def test_thumbnail_refuses_non_images(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        url = await _artifact_url(tmp_path, config)
        resp = TestClient(_route_app(config)).get(url + "?variant=thumbnail")
        assert resp.status_code == 415
        assert resp.json()["code"] == "INVALID_REQUEST"
        assert resp.json()["details"] == {"code": "UNSUPPORTED_MEDIA_TYPE"}

    @pytest.mark.asyncio
    async def test_unknown_variant(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        url = await _artifact_url(tmp_path, config)
        assert TestClient(_route_app(config)).get(url + "?variant=raw").status_code == 400

    @pytest.mark.asyncio
    async def test_foreign_unknown_or_dead_connection_is_404(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        url = await _artifact_url(tmp_path, config)
        ticket = url.rsplit("/", 1)[1]
        client = TestClient(_route_app(config))
        assert client.get(f"/api/v1/artifacts/download/conn-2/{ticket}").status_code == 404
        assert client.get("/api/v1/artifacts/download/conn-1/not-a-ticket").status_code == 404
        dead = TestClient(_route_app(config, alive=set()))
        assert dead.get(url).status_code == 404

    @pytest.mark.asyncio
    async def test_revoked_ticket_is_404(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        url = await _artifact_url(tmp_path, config)
        get_download_grants().revoke_connection("conn-1")
        assert TestClient(_route_app(config)).get(url).status_code == 404

    @pytest.mark.asyncio
    async def test_attachment_download(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        sha, _, _ = write_transcript_material(
            media_root=tmp_path, session_id="session-1", payload=b"<script>x</script>"
        )
        res = await _mint(
            config,
            {
                "session_key": SESSION_KEY,
                "attachment_sha256": sha,
                "name": "page.html",
                "mime": "text/html",
            },
        )
        resp = TestClient(_route_app(config)).get(res.payload["url"])
        assert resp.status_code == 200
        assert resp.content == b"<script>x</script>"
        # An HTML attachment opened directly cannot script anything.
        assert resp.headers["content-security-policy"] == "default-src 'none'; sandbox"
        assert resp.headers["content-disposition"].startswith("attachment;")


def test_content_disposition_encodes_unicode_and_quotes() -> None:
    value = content_disposition('rapport "final" é.pdf')
    assert value.startswith("attachment; filename=\"rapport 'final' .pdf\"")
    assert "filename*=UTF-8''rapport%20%22final%22%20%C3%A9.pdf" in value


def test_thumbnail_rejects_animated_images(tmp_path: Path) -> None:
    from PIL import Image

    path = tmp_path / "anim.gif"
    frames = [Image.new("RGB", (10, 10), color) for color in ("red", "blue")]
    frames[0].save(path, save_all=True, append_images=frames[1:])
    assert make_thumbnail(path) is None


# ---------------------------------------------------------------------------
# End to end through the real app and a real WebSocket
# ---------------------------------------------------------------------------


def _connect(ws: Any) -> str:
    assert ws.receive_json()["event"] == "connect.challenge"
    ws.send_json(
        {"type": "req", "id": "c", "method": "connect", "params": {"auth": {"token": "secret"}}}
    )
    hello = ws.receive_json()
    assert hello["type"] == "hello-ok", hello
    assert "artifact-tickets" in hello["features"]["capabilities"]
    return str(hello["server"]["conn_id"])


def test_ticket_over_websocket_then_plain_get_until_the_socket_closes(tmp_path: Path) -> None:
    from agentos.gateway.app import create_gateway_app

    config = _config(tmp_path)
    ref = _publish(tmp_path)
    app = create_gateway_app(config, session_manager=_FakeSessionManager())
    client = TestClient(app, base_url="http://localhost")
    with client.websocket_connect("/ws") as ws:
        conn_id = _connect(ws)
        ws.send_json(
            {
                "type": "req",
                "id": "1",
                "method": "artifacts.download",
                "params": {"session_key": SESSION_KEY, "artifact_id": ref.id, "transport": "http"},
            }
        )
        res = ws.receive_json()
        while res.get("type") != "res":
            res = ws.receive_json()
        assert res["ok"], res
        url = res["payload"]["url"]
        assert f"/download/{conn_id}/" in url
        # No Authorization header: the ticket is the credential.
        live = client.get(url)
        assert live.status_code == 200
        assert live.content == b"hello artifact"
    # The server side of the close runs on the app's own thread; give it a beat.
    deadline = time.monotonic() + 5
    while get_download_grants().live_count(conn_id) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert client.get(url).status_code == 404


@pytest.mark.parametrize("mode", ["none", "token"])
def test_a_transcript_artifact_url_loads_only_through_a_ticket(tmp_path: Path, mode: str) -> None:
    """What the web console relies on to show a card, an image or a file.

    A transcript row names its artifact by a bare ``download_url``. That URL
    resolves nothing on its own (it has no session), with or without auth; the
    socket's ``artifacts.download`` ticket for the same id serves the bytes to
    a request with no header at all.
    """
    from agentos.artifacts import artifact_payload
    from agentos.gateway.app import create_gateway_app

    auth = AuthConfig(mode="token", token="secret") if mode == "token" else AuthConfig()
    config = GatewayConfig(auth=auth, attachments=AttachmentsConfig(media_root=str(tmp_path)))
    ref = _publish(tmp_path, b'{"type":"cards"}', name="cards.json", mime="application/json")
    row = artifact_payload(ref.to_dict())
    assert row["download_url"] == f"/api/v1/artifacts/{ref.id}"
    app = create_gateway_app(config, session_manager=_FakeSessionManager())
    # A loopback peer: auth "none" admits only that.
    client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000))
    bearer = {"Authorization": "Bearer secret"} if mode == "token" else {}
    assert client.get(row["download_url"], headers=bearer).status_code == 404
    with client.websocket_connect("/ws") as ws:
        _connect(ws)
        ws.send_json(
            {
                "type": "req",
                "id": "1",
                "method": "artifacts.download",
                "params": {
                    "session_key": SESSION_KEY,
                    "artifact_id": row["id"],
                    "transport": "http",
                },
            }
        )
        res = ws.receive_json()
        while res.get("type") != "res":
            res = ws.receive_json()
        assert res["ok"], res
        served = client.get(res["payload"]["url"])
        assert served.status_code == 200
        assert served.content == b'{"type":"cards"}'
        assert served.headers["content-type"].startswith("application/json")


def test_legacy_route_can_be_switched_off(tmp_path: Path) -> None:
    from agentos.gateway.artifacts import register_artifact_routes

    ref = _publish(tmp_path)
    for enabled, expected in ((True, 200), (False, 404)):
        config = _config(tmp_path, artifacts=ArtifactsConfig(legacy_session_key_urls=enabled))
        app = Starlette()
        register_artifact_routes(app, config=config, session_manager=_FakeSessionManager())
        resp = TestClient(app).get(f"/api/v1/artifacts/{ref.id}?sessionKey={SESSION_KEY}")
        assert resp.status_code == expected


def test_legacy_route_logs_a_deprecation_once(tmp_path: Path) -> None:
    from structlog.testing import capture_logs

    from agentos.gateway import artifacts as gateway_artifacts

    gateway_artifacts._LEGACY_WARNED.clear()
    ref = _publish(tmp_path)
    app = Starlette()
    gateway_artifacts.register_artifact_routes(
        app, config=_config(tmp_path), session_manager=_FakeSessionManager()
    )
    client = TestClient(app)
    with capture_logs() as logs:
        client.get(f"/api/v1/artifacts/{ref.id}?sessionKey={SESSION_KEY}")
        client.get(f"/api/v1/artifacts/{ref.id}?sessionKey={SESSION_KEY}")
    warnings = [
        entry
        for entry in logs
        if entry["event"] == "gateway.download.legacy_session_key_url"
        and entry["log_level"] == "warning"
    ]
    assert len(warnings) == 1


class TestVerificationIsCached:
    """A ticket's HEAD / ranges / thumbnail must not re-hash the file each time."""

    @pytest.mark.asyncio
    async def test_attachment_is_hashed_once_while_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentos.gateway import download_verify

        download_verify.clear()
        hashed: list[Path] = []
        real = download_verify._sha256

        def counting(path: Path) -> str:
            hashed.append(path)
            return real(path)

        monkeypatch.setattr(download_verify, "_sha256", counting)
        config = _config(tmp_path)
        sha, path, _ = write_transcript_material(
            media_root=tmp_path, session_id="session-1", payload=b"x" * 4096
        )
        res = await _mint(config, {"session_key": SESSION_KEY, "attachment_sha256": sha})
        client = TestClient(_route_app(config))
        for _ in range(3):
            assert client.get(res.payload["url"], headers={"Range": "bytes=0-9"}).status_code == 206
        assert len(hashed) == 1
        # A changed file is verified again (and here fails the integrity check).
        Path(path).write_bytes(b"y" * 4096)
        changed = client.get(res.payload["url"])
        assert changed.status_code == 409
        assert changed.json()["details"] == {"code": "INTEGRITY_ERROR"}
        assert len(hashed) == 2
        # Minting a ticket for the altered file is refused with the same reason.
        again = await _mint(config, {"session_key": SESSION_KEY, "attachment_sha256": sha})
        assert again.error is not None
        assert again.error.code == "INTERNAL_ERROR"
        assert again.error.details == {"code": "INTEGRITY_ERROR"}

    @pytest.mark.asyncio
    async def test_artifact_is_resolved_once_while_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentos.gateway import artifacts as gateway_artifacts

        gateway_artifacts._RESOLVED.clear()
        calls: list[str] = []
        real = ArtifactStore.resolve_for_download

        def counting(self: ArtifactStore, artifact_id: str, *, session_id: str) -> Any:
            calls.append(artifact_id)
            return real(self, artifact_id, session_id=session_id)

        monkeypatch.setattr(ArtifactStore, "resolve_for_download", counting)
        config = _config(tmp_path)
        url = await _artifact_url(tmp_path, config)
        client = TestClient(_route_app(config))
        client.head(url)
        client.get(url)
        client.get(url, headers={"Range": "bytes=0-1"})
        assert len(calls) == 1
