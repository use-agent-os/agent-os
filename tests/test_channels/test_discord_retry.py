"""Transient-HTTP retries on the Discord adapter's outbound calls.

``retry_request`` (``agentos.channels._util``) backs Discord outbound calls;
these tests pin that transient 429/5xx errors are retried, and that file upload
retries reopen the file handle instead of sending an exhausted body (#1164).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from agentos.channels.contract import ChannelCapabilities, ChannelSendStatus
from agentos.channels.discord import DiscordChannel, DiscordChannelConfig
from agentos.channels.types import OutgoingMessage

_REQUEST = httpx.Request("POST", "https://discord.test/api")


def _resp(
    status_code: int = 200,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=body if body is not None else {"id": "msg-123"},
        headers=headers,
        request=_REQUEST,
    )


def _channel() -> DiscordChannel:
    return DiscordChannel(
        DiscordChannelConfig(
            token="test-token",
            default_channel_id="ch-default",
        )
    )


@pytest.fixture
def no_sleep():
    """Collapse ``retry_request``'s backoff so the assertions stay fast."""
    with patch("agentos.channels._util.asyncio.sleep", new=AsyncMock()) as sleep:
        yield sleep


async def test_send_file_retries_and_reopens_the_upload_body(tmp_path: Path, no_sleep) -> None:
    """A retried upload must re-read the file, not send an exhausted handle (#1164)."""
    sample = tmp_path / "note.txt"
    sample.write_bytes(b"hello world")

    channel = _channel()
    bodies: list[bytes] = []
    calls: list[str] = []

    async def _post(url: str, **kwargs: Any) -> httpx.Response:
        calls.append(url)
        assert url == "/channels/ch-target/messages"
        file_tuple = kwargs["files"]["file"]
        assert file_tuple[0] == "note.txt"
        bodies.append(file_tuple[1].read())
        if len(bodies) == 1:
            return _resp(503)
        return _resp(200, {"id": "msg-file-1"})

    client = AsyncMock()
    client.post = AsyncMock(side_effect=_post)
    channel._client = client

    result = await channel.send_file("ch-target", str(sample), content="here you go")

    assert result.status == ChannelSendStatus.SENT
    assert result.capability == ChannelCapabilities.NATIVE_FILE_UPLOAD
    assert result.provider_message_id == "msg-file-1"
    # Both upload attempts carried the full body — the handle was reopened.
    assert bodies == [b"hello world", b"hello world"]
    assert len(calls) == 2


async def test_send_retries_server_error_then_succeeds(no_sleep) -> None:
    channel = _channel()
    client = AsyncMock()
    client.post = AsyncMock(side_effect=[_resp(503), _resp(200, {"id": "msg-ok"})])
    channel._client = client

    result = await channel.send(OutgoingMessage(content="hi", reply_to="ch-1"))

    assert result.provider_message_id == "msg-ok"
    assert client.post.await_count == 2


async def test_edit_retries_transient_error(no_sleep) -> None:
    channel = _channel()
    client = AsyncMock()
    client.patch = AsyncMock(side_effect=[_resp(500), _resp(200, {"id": "msg-edit-1"})])
    channel._client = client

    result = await channel.edit("msg-edit-1", "updated")

    assert result.provider_message_id == "msg-edit-1"
    assert client.patch.await_count == 2
