"""DiscordChannel edit/delete resolve composite targets and reject unknown channels.

Regression for #1883: edit/delete built "/channels//messages/<id>" with an
empty channel id for any message not in the in-memory sent cache, and the
messaging tool discarded the target argument for Discord entirely.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from agentos.channels.contract import ChannelSendStatus
from agentos.channels.discord import DiscordChannel, DiscordChannelConfig
from agentos.tools.builtin.messaging import _delete_message_id


class _Resp:
    status_code = 200
    headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        return None


def _channel(**config: Any) -> tuple[DiscordChannel, dict[str, list[str]]]:
    channel = DiscordChannel(DiscordChannelConfig(token="bot-test", **config))
    client = AsyncMock()
    calls: dict[str, list[str]] = {"patch": [], "delete": []}

    async def _patch(url: str, **kwargs: Any) -> _Resp:
        calls["patch"].append(url)
        return _Resp()

    async def _delete(url: str, **kwargs: Any) -> _Resp:
        calls["delete"].append(url)
        return _Resp()

    client.patch = _patch
    client.delete = _delete
    channel._client = client
    return channel, calls


@pytest.mark.asyncio
async def test_delete_resolves_composite_channel_id() -> None:
    channel, calls = _channel(default_channel_id="")
    await channel.delete("C111|msg123")
    assert calls["delete"] == ["/channels/C111/messages/msg123"]


@pytest.mark.asyncio
async def test_edit_resolves_composite_channel_id() -> None:
    channel, calls = _channel(default_channel_id="")
    await channel.edit("C111|msg123", "new text")
    assert calls["patch"] == ["/channels/C111/messages/msg123"]


@pytest.mark.asyncio
async def test_delete_falls_back_to_sent_messages_cache() -> None:
    channel, calls = _channel(default_channel_id="")
    channel._sent_messages["msg123"] = "C222"
    await channel.delete("msg123")
    assert calls["delete"] == ["/channels/C222/messages/msg123"]


@pytest.mark.asyncio
async def test_edit_falls_back_to_default_channel() -> None:
    channel, calls = _channel(default_channel_id="C999")
    await channel.edit("msg123", "new text")
    assert calls["patch"] == ["/channels/C999/messages/msg123"]


@pytest.mark.asyncio
async def test_delete_unknown_channel_rejects_without_http_call() -> None:
    channel, calls = _channel(default_channel_id="")
    result = await channel.delete("msg_untracked")
    assert result.status == ChannelSendStatus.FAILED
    assert "channel" in result.reason.lower()
    assert calls["delete"] == []


@pytest.mark.asyncio
async def test_edit_unknown_channel_rejects_without_http_call() -> None:
    channel, calls = _channel(default_channel_id="")
    result = await channel.edit("msg_untracked", "new text")
    assert result.status == ChannelSendStatus.FAILED
    assert "channel" in result.reason.lower()
    assert calls["patch"] == []


def test_delete_message_id_encodes_discord_target() -> None:
    assert _delete_message_id("discord", "C111", "msg123") == "C111|msg123"
    assert _delete_message_id("telegram", "T1", "msg1") == "T1|msg1"
    assert _delete_message_id("slack", "S1", "msg1") == "msg1"
    # an already-composite id is passed through unchanged
    assert _delete_message_id("discord", "C111", "C222|msg9") == "C222|msg9"
