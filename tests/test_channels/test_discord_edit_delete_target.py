"""Discord ``edit``/``delete`` resolve the channel a message lives in (#1883).

``DiscordChannel.edit`` and ``delete`` looked the channel up in the in-memory
``_sent_messages`` cache and fell back to ``default_channel_id`` -- which
defaults to ``""``. Any message the process did not send itself (a user's
message, or one sent before a restart) therefore went to
``/channels//messages/<id>`` and Discord answered 404. Telegram and Slack
carry the conversation inside the id (``<chat_id>|<message_id>``); Discord
now accepts the same ``<channel_id>|<message_id>`` shape, and the ``message``
tool builds it from ``target`` instead of dropping it.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from agentos.channels.discord import DiscordChannel, DiscordChannelConfig
from agentos.tools.builtin import messaging

_REQUEST = httpx.Request("DELETE", "https://discord.test/api")


def _resp() -> httpx.Response:
    return httpx.Response(200, json={"id": "678"}, request=_REQUEST)


def _channel(default: str = "") -> tuple[DiscordChannel, AsyncMock]:
    channel = DiscordChannel(DiscordChannelConfig(token="fake-token", default_channel_id=default))
    client = AsyncMock()
    client.delete = AsyncMock(return_value=_resp())
    client.patch = AsyncMock(return_value=_resp())
    channel._client = client
    return channel, client


def _url(call: AsyncMock) -> str:
    assert call.await_count == 1
    return str(call.await_args.args[0])


# --- adapter ---------------------------------------------------------------


async def test_delete_uses_channel_from_composite_id() -> None:
    channel, client = _channel()

    result = await channel.delete("111222333|678")

    assert _url(client.delete) == "/channels/111222333/messages/678"
    assert result.target_id == "111222333"
    assert result.provider_message_id == "678"


async def test_edit_uses_channel_from_composite_id() -> None:
    channel, client = _channel()

    result = await channel.edit("111222333|678", "updated")

    assert _url(client.patch) == "/channels/111222333/messages/678"
    assert client.patch.await_args.kwargs["json"] == {"content": "updated"}
    assert result.target_id == "111222333"
    assert result.provider_message_id == "678"


async def test_delete_prefers_the_channel_a_sent_message_was_tracked_in() -> None:
    """Stream edits and cleanup use bare ids the adapter itself handed out."""
    channel, client = _channel(default="chan_default")
    channel._sent_messages["678"] = "chan_sent"

    await channel.delete("678")

    assert _url(client.delete) == "/channels/chan_sent/messages/678"
    assert "678" not in channel._sent_messages


async def test_edit_bare_id_falls_back_to_default_channel() -> None:
    channel, client = _channel(default="chan_default")

    await channel.edit("678", "updated")

    assert _url(client.patch) == "/channels/chan_default/messages/678"


async def test_composite_id_without_channel_part_uses_default() -> None:
    """``|678`` (empty channel) must not produce ``/channels//messages/678``."""
    channel, client = _channel(default="chan_default")

    await channel.delete("|678")

    assert _url(client.delete) == "/channels/chan_default/messages/678"


async def test_delete_without_any_channel_raises_before_calling_discord() -> None:
    channel, client = _channel(default="")

    with pytest.raises(ValueError, match=r"<channel_id>\|<message_id>"):
        await channel.delete("678")

    assert client.delete.await_count == 0


async def test_edit_without_any_channel_raises_before_calling_discord() -> None:
    channel, client = _channel(default="")

    with pytest.raises(ValueError, match=r"<channel_id>\|<message_id>"):
        await channel.edit("678", "updated")

    assert client.patch.await_count == 0


# --- message tool ----------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "message_id", "expected"),
    [
        ("111222333", "678", "111222333|678"),
        ("111222333", "444555|678", "444555|678"),
        ("", "678", "678"),
    ],
)
def test_delete_message_id_encodes_target_for_discord(
    target: str, message_id: str, expected: str
) -> None:
    assert messaging._delete_message_id("discord", target, message_id) == expected


async def test_message_tool_delete_routes_discord_target(monkeypatch: pytest.MonkeyPatch) -> None:
    channel, client = _channel()
    monkeypatch.setattr(messaging, "_channels", {"discord": channel})

    out: dict[str, Any] = json.loads(
        await messaging.message(
            channel="discord",
            target="111222333",
            message_id="678",
            action="delete",
        )
    )

    assert out == {
        "status": "deleted",
        "channel": "discord",
        "target": "111222333",
        "message_id": "678",
    }
    assert _url(client.delete) == "/channels/111222333/messages/678"
