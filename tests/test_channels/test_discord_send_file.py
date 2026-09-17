"""Regression tests for DiscordChannel.send_file target resolution and caption chunking."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from agentos.channels.discord import DiscordChannel, DiscordChannelConfig

_REQUEST = httpx.Request("POST", "https://discord.test/api")


def _resp(status_code: int = 200, body: dict[str, Any] | None = None) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=body if body is not None else {"id": "101"},
        request=_REQUEST,
    )


@pytest.mark.asyncio
async def test_send_file_caption_exceeding_2000_chars_splits_safely(tmp_path: Path) -> None:
    sample = tmp_path / "data.csv"
    sample.write_bytes(b"col1,col2\n1,2")

    channel = DiscordChannel(config=DiscordChannelConfig(token="test-token"))
    post_calls: list[dict[str, Any]] = []

    async def _post(url: str, **kwargs: Any) -> httpx.Response:
        post_calls.append({"url": url, **kwargs})
        return _resp(200, {"id": f"msg-{len(post_calls)}"})

    client = AsyncMock()
    client.post = AsyncMock(side_effect=_post)
    channel._client = client

    long_caption = "Line of text.\n" * 200  # ~2800 characters, exceeds 2000 limit

    result = await channel.send_file("C100", str(sample), content=long_caption)

    assert result.status.value == "sent"
    # First call is file upload with caption <= 2000 chars; second is follow-up text
    assert len(post_calls) >= 2
    first_call = post_calls[0]
    assert first_call["url"] == "/channels/C100/messages"
    assert "content" in first_call["data"]
    assert len(first_call["data"]["content"]) <= 2000
    assert first_call["files"]["file"][0] == "data.csv"

    second_call = post_calls[1]
    assert second_call["url"] == "/channels/C100/messages"
    assert "content" in second_call["json"]


@pytest.mark.asyncio
async def test_send_file_short_caption_single_call(tmp_path: Path) -> None:
    sample = tmp_path / "short.txt"
    sample.write_bytes(b"hello")

    channel = DiscordChannel(config=DiscordChannelConfig(token="test-token"))
    post_calls: list[dict[str, Any]] = []

    async def _post(url: str, **kwargs: Any) -> httpx.Response:
        post_calls.append({"url": url, **kwargs})
        return _resp(200, {"id": "msg-1"})

    client = AsyncMock()
    client.post = AsyncMock(side_effect=_post)
    channel._client = client

    result = await channel.send_file("C200", str(sample), content="short caption")

    assert result.status.value == "sent"
    assert len(post_calls) == 1
    assert post_calls[0]["url"] == "/channels/C200/messages"
    assert post_calls[0]["data"]["content"] == "short caption"


@pytest.mark.asyncio
async def test_send_file_composite_target_resolves_channel(tmp_path: Path) -> None:
    sample = tmp_path / "report.pdf"
    sample.write_bytes(b"%PDF-1.4")

    channel = DiscordChannel(config=DiscordChannelConfig(token="test-token"))
    post_calls: list[dict[str, Any]] = []

    async def _post(url: str, **kwargs: Any) -> httpx.Response:
        post_calls.append({"url": url, **kwargs})
        return _resp(200, {"id": "msg-1"})

    client = AsyncMock()
    client.post = AsyncMock(side_effect=_post)
    channel._client = client

    # Target specified with pipe-delimited message reference: <channel_id>|<message_id>
    result = await channel.send_file("C300|msg999", str(sample))

    assert result.status.value == "sent"
    assert post_calls[0]["url"] == "/channels/C300/messages"
    assert result.target_id == "C300"


@pytest.mark.asyncio
async def test_send_file_empty_target_falls_back_to_default_channel(tmp_path: Path) -> None:
    sample = tmp_path / "log.txt"
    sample.write_bytes(b"log data")

    channel = DiscordChannel(
        config=DiscordChannelConfig(token="test-token", default_channel_id="C_DEFAULT")
    )
    post_calls: list[dict[str, Any]] = []

    async def _post(url: str, **kwargs: Any) -> httpx.Response:
        post_calls.append({"url": url, **kwargs})
        return _resp(200, {"id": "msg-1"})

    client = AsyncMock()
    client.post = AsyncMock(side_effect=_post)
    channel._client = client

    result = await channel.send_file("", str(sample))

    assert result.status.value == "sent"
    assert post_calls[0]["url"] == "/channels/C_DEFAULT/messages"
    assert result.target_id == "C_DEFAULT"


@pytest.mark.asyncio
async def test_send_file_missing_target_raises_value_error(tmp_path: Path) -> None:
    sample = tmp_path / "sample.txt"
    sample.write_bytes(b"text")

    channel = DiscordChannel(config=DiscordChannelConfig(token="test-token"))

    with pytest.raises(
        ValueError, match="discord.send_file requires channel_id or default_channel_id"
    ):
        await channel.send_file("", str(sample))
