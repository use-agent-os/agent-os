from __future__ import annotations

import pytest

from agentos.channels._attachment_io import (
    attachment_limit_for_mime,
    ensure_declared_size_within_limit,
    preferred_attachment_mime,
)
from agentos.channels.discord import DiscordChannel, DiscordChannelConfig
from agentos.channels.telegram import TelegramChannel, TelegramChannelConfig
from agentos.channels.types import Attachment
from agentos.gateway.attachment_ingest import (
    IMAGE_ATTACHMENT_BYTES,
    MAX_ATTACHMENT_BYTES,
    MAX_STAGED_PDF_BYTES,
    TEXT_ATTACHMENT_BYTES,
)


def test_generic_download_content_type_preserves_declared_allowed_mime() -> None:
    assert preferred_attachment_mime("application/octet-stream", "text/plain") == "text/plain"
    assert preferred_attachment_mime("text/plain", "application/pdf") == "text/plain"


def test_channel_attachment_limit_uses_declared_mime_policy() -> None:
    assert attachment_limit_for_mime("text/plain") == TEXT_ATTACHMENT_BYTES
    assert attachment_limit_for_mime("image/png") == IMAGE_ATTACHMENT_BYTES
    assert attachment_limit_for_mime("application/pdf") == MAX_STAGED_PDF_BYTES
    assert attachment_limit_for_mime(None) == MAX_ATTACHMENT_BYTES

    ensure_declared_size_within_limit(
        6 * 1024 * 1024,
        name="report.pdf",
        limit=attachment_limit_for_mime("application/pdf"),
    )
    with pytest.raises(ValueError, match="exceeds"):
        ensure_declared_size_within_limit(
            TEXT_ATTACHMENT_BYTES + 1,
            name="large.txt",
            limit=attachment_limit_for_mime("text/plain"),
        )


def test_telegram_document_maps_to_attachment_metadata() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="t"))

    msg = channel.parse_incoming(
        {
            "message": {
                "message_id": 1,
                "chat": {"id": 123, "type": "private"},
                "from": {"id": 456},
                "caption": "read",
                "document": {
                    "file_id": "file-1",
                    "file_name": "report.pdf",
                    "mime_type": "application/pdf",
                    "file_size": 12,
                },
            }
        }
    )

    assert len(msg.attachments) == 1
    att = msg.attachments[0]
    assert att.name == "report.pdf"
    assert att.mime_type == "application/pdf"
    assert att.size == 12
    assert att.metadata["telegram_file_id"] == "file-1"


def test_telegram_photo_uses_largest_photo_file_id() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="t"))

    msg = channel.parse_incoming(
        {
            "message": {
                "message_id": 1,
                "chat": {"id": 123, "type": "private"},
                "from": {"id": 456},
                "photo": [
                    {"file_id": "small", "file_unique_id": "s", "width": 10, "height": 10},
                    {"file_id": "large", "file_unique_id": "l", "width": 100, "height": 100},
                ],
            }
        }
    )

    assert msg.content == "[photo]"
    assert len(msg.attachments) == 1
    att = msg.attachments[0]
    assert att.mime_type == "image/jpeg"
    assert att.metadata["telegram_file_id"] == "large"


@pytest.mark.asyncio
async def test_discord_resolve_inbound_attachment_fetches_url_bytes() -> None:
    class FakeResponse:
        headers = {"content-type": "text/plain; charset=utf-8"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        async def aiter_bytes(self):
            yield b"hello"

    class FakeClient:
        def stream(self, method: str, url: str):
            assert method == "GET"
            assert url == "https://cdn.discordapp.test/a.txt"
            return FakeResponse()

    channel = DiscordChannel(DiscordChannelConfig(token="t"))
    channel._client = FakeClient()

    resolved = await channel.resolve_inbound_attachment(
        Attachment(
            name="a.txt",
            mime_type=None,
            url="https://cdn.discordapp.test/a.txt",
            size=5,
        )
    )

    assert resolved.data == b"hello"
    assert resolved.mime_type == "text/plain"
    assert resolved.metadata["source_url"] == "https://cdn.discordapp.test/a.txt"


@pytest.mark.asyncio
async def test_discord_oversize_content_length_is_rejected_before_body_read() -> None:
    class FakeResponse:
        headers = {"content-length": str(MAX_ATTACHMENT_BYTES + 1)}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        async def aiter_bytes(self):
            raise AssertionError("oversize response body should not be read")
            yield b""

    class FakeClient:
        def stream(self, method: str, url: str):
            assert method == "GET"
            return FakeResponse()

    channel = DiscordChannel(DiscordChannelConfig(token="t"))
    channel._client = FakeClient()

    with pytest.raises(ValueError, match="exceeds"):
        await channel.resolve_inbound_attachment(
            Attachment(name="huge.bin", url="https://cdn.discordapp.test/huge.bin")
        )


@pytest.mark.asyncio
async def test_telegram_oversize_declared_attachment_skips_get_file() -> None:
    class NoApiTelegram(TelegramChannel):
        async def _api(self, method: str, payload=None):
            raise AssertionError("oversize Telegram attachment should not call getFile")

    channel = NoApiTelegram(TelegramChannelConfig(token="t"))

    with pytest.raises(ValueError, match="exceeds"):
        await channel.resolve_inbound_attachment(
            Attachment(
                name="huge.txt",
                mime_type="text/plain",
                size=TEXT_ATTACHMENT_BYTES + 1,
                metadata={"telegram_file_id": "file-1"},
            )
        )
