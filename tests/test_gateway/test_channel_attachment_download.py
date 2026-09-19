"""An attachment sent over a channel downloads from the Control UI.

``chat.send`` stages attachment bytes under ``transcripts/<session_id>/``, but a
channel turn stages them under the session key's last segment (``main`` for a DM
on the default ``dm_scope``, the chat id for a group). The download route looked
only under ``session_id``, so the link ``chat.history`` hands the Control UI for
every photo or file a user sent over Telegram, Discord or Slack returned 404.

These run the real ``SessionManager`` over in-memory storage, stage the file the
way ``channel_dispatch`` does, and follow the ``download_url`` ``chat.history``
builds.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from starlette.applications import Starlette

from agentos.attachment_refs import write_transcript_material
from agentos.gateway.attachment_ingest import ingest_attachments
from agentos.gateway.attachments import register_attachment_routes
from agentos.gateway.channel_dispatch import _append_channel_user_message
from agentos.gateway.config import AttachmentsConfig, GatewayConfig
from agentos.gateway.rpc_chat import _annotate_transcript_attachment_downloads
from agentos.session.manager import SessionManager
from agentos.session.storage import SessionStorage

_PNG = b"\x89PNG\r\n\x1a\n" + b"logo-bytes" * 8


@pytest_asyncio.fixture
async def manager():
    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        yield SessionManager(storage, inject_time_prefix=False)
    finally:
        await storage.close()


@pytest.fixture
def config(tmp_path: Path) -> GatewayConfig:
    return GatewayConfig(attachments=AttachmentsConfig(media_root=str(tmp_path)))


@pytest_asyncio.fixture
async def client(manager, config):
    app = Starlette()
    register_attachment_routes(app, config=config, session_manager=manager)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as http:
        yield http


async def _send_over_channel(manager, config, session_key: str, payload: bytes) -> str:
    """Stage a photo the way channel_dispatch does and return its download_url."""
    await manager.get_or_create(session_key, agent_id="main")
    ingested = await ingest_attachments(
        "here is the logo",
        [{"type": "image/png", "name": "logo.png", "data": payload}],
        failure_mode="mark",
        mark_bytes_as_staged=True,
    )
    await _append_channel_user_message(
        session_manager=manager,
        session_key=session_key,
        text=ingested.text,
        attachments=ingested.attachments,
        config=config,
    )
    entry = (await manager.get_transcript(session_key))[-1]
    message = {"attachments": json.loads(entry.content)["attachments"]}
    annotated = _annotate_transcript_attachment_downloads([message], session_key=session_key)
    return annotated[0]["attachments"][0]["download_url"]


# ── channel turns ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session_key",
    [
        "agent:main:main",  # a DM on the default dm_scope
        "agent:main:telegram:group:-1001234567",
        "agent:main:discord:channel:98765",
        "agent:main:telegram:group:-1001234567:topic:5",
    ],
)
async def test_a_photo_sent_over_a_channel_downloads(manager, config, client, session_key):
    payload = _PNG + session_key.encode()

    url = await _send_over_channel(manager, config, session_key, payload)
    response = await client.get(url)

    assert response.status_code == 200
    assert response.content == payload


@pytest.mark.asyncio
async def test_a_channel_attachment_is_still_integrity_checked(manager, config, client, tmp_path):
    url = await _send_over_channel(manager, config, "agent:main:main", _PNG)
    for blob in (tmp_path / "transcripts").rglob("*"):
        if blob.is_file():
            blob.write_bytes(b"tampered")

    response = await client.get(url)

    assert response.status_code == 409


# ── unchanged ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_material_under_the_session_id_still_downloads(manager, client, tmp_path):
    """Positive control: the chat.send layout is served exactly as before."""
    await manager.get_or_create("agent:main:webchat:abc", agent_id="main")
    session = await manager.get_session("agent:main:webchat:abc")
    sha, _path, _wrote = write_transcript_material(
        media_root=tmp_path, session_id=session.session_id, payload=b"web upload"
    )

    response = await client.get(f"/api/v1/attachments/{sha}?sessionKey=agent:main:webchat:abc")

    assert response.status_code == 200
    assert response.content == b"web upload"


@pytest.mark.asyncio
async def test_a_key_that_names_no_session_finds_nothing(client, tmp_path):
    """The key's own segment is only a second place to look, never a way in."""
    sha, _path, _wrote = write_transcript_material(
        media_root=tmp_path, session_id="98765", payload=b"orphaned"
    )

    response = await client.get(
        f"/api/v1/attachments/{sha}?sessionKey=agent:main:discord:channel:98765"
    )

    assert response.status_code == 404
