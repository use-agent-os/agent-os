from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from agentos.channels.telegram import TelegramChannel, TelegramChannelConfig


class _TelegramResponse:
    def __init__(self, message_id: int = 1) -> None:
        self._message_id = message_id

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {
            "ok": True,
            "result": {
                "message_id": self._message_id,
                "document": {"file_id": "file_123"},
            },
        }


@pytest.mark.asyncio
async def test_telegram_send_file_caption_exceeding_1024_chars_splits_safely(
    tmp_path,
) -> None:
    file_path = tmp_path / "test.txt"
    file_path.write_text("dummy content")

    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    client = AsyncMock()

    post_calls: list[dict[str, Any]] = []

    async def _post(url: str, **kwargs: Any) -> _TelegramResponse:
        post_calls.append(
            {"url": url, "data": kwargs.get("data", {}), "json": kwargs.get("json", {})}
        )
        return _TelegramResponse(len(post_calls))

    client.post = _post
    channel._client = client
    channel._owns_client = False

    # 1500 chars content which renders to > 1024 chars HTML caption
    long_caption = "A" * 1500

    res = await channel.send_file("12345", str(file_path), content=long_caption)

    assert res.status == "sent"
    assert len(post_calls) == 2

    # The first call is sendDocument with caption <= 1024 chars
    doc_call = post_calls[0]
    assert "/sendDocument" in doc_call["url"]
    assert "caption" in doc_call["data"]
    assert len(doc_call["data"]["caption"]) <= 1024

    # The second call is sendMessage delivering the remainder
    msg_call = post_calls[1]
    assert "/sendMessage" in msg_call["url"] or "text" in msg_call["json"]


@pytest.mark.asyncio
async def test_telegram_send_file_short_caption_single_call(
    tmp_path,
) -> None:
    file_path = tmp_path / "short.txt"
    file_path.write_text("dummy content")

    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    client = AsyncMock()

    post_calls: list[dict[str, Any]] = []

    async def _post(url: str, **kwargs: Any) -> _TelegramResponse:
        post_calls.append(
            {"url": url, "data": kwargs.get("data", {}), "json": kwargs.get("json", {})}
        )
        return _TelegramResponse(len(post_calls))

    client.post = _post
    channel._client = client
    channel._owns_client = False

    res = await channel.send_file("12345", str(file_path), content="short caption")

    assert res.status == "sent"
    assert len(post_calls) == 1
    assert post_calls[0]["data"]["caption"] == "short caption"
