from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agentos.channels.discord import DiscordChannel, DiscordChannelConfig
from agentos.tools.builtin.messaging import _channels, _delete_message_id, message


def test_delete_message_id_encodes_target_for_discord() -> None:
    res = _delete_message_id("discord", "111222333", "999888")
    assert res == "111222333|999888"

    # Preserves existing channel|msg
    res_existing = _delete_message_id("discord", "111222333", "444555|999888")
    assert res_existing == "444555|999888"

    # Preserves bare id when target is empty
    res_empty_target = _delete_message_id("discord", "", "999888")
    assert res_empty_target == "999888"


def test_discord_split_message_ref() -> None:
    cfg = DiscordChannelConfig(token="fake_token", default_channel_id="chan_def")
    channel = DiscordChannel(cfg)

    # 1. Composite channel_id|message_id
    assert channel._split_message_ref("chan_custom|msg_1") == ("chan_custom", "msg_1")

    # 2. Tracked sent message
    channel._sent_messages["msg_tracked"] = "chan_sent"
    assert channel._split_message_ref("msg_tracked") == ("chan_sent", "msg_tracked")

    # 3. Fallback to default_channel_id
    assert channel._split_message_ref("msg_unknown") == ("chan_def", "msg_unknown")

    # 4. Error when neither is available
    channel_no_default = DiscordChannel(DiscordChannelConfig(token="fake_token"))
    with pytest.raises(
        ValueError, match="discord edit/delete requires '<channel_id>\\|<message_id>'"
    ):
        channel_no_default._split_message_ref("msg_untracked")


@pytest.mark.asyncio
async def test_discord_edit_with_composite_channel_ref() -> None:
    channel = DiscordChannel(DiscordChannelConfig(token="fake_token"))

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.patch = AsyncMock(return_value=mock_resp)
    channel._client = mock_client

    # Edit using composite channel_id|message_id
    result = await channel.edit("target_chan|msg_42", "updated content")

    mock_client.patch.assert_awaited_with(
        "/channels/target_chan/messages/msg_42",
        json={"content": "updated content"},
        headers=channel._auth_headers(),
    )
    assert result.target_id == "target_chan"
    assert result.provider_message_id == "msg_42"


@pytest.mark.asyncio
async def test_discord_delete_with_composite_channel_ref() -> None:
    channel = DiscordChannel(DiscordChannelConfig(token="fake_token"))

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.delete = AsyncMock(return_value=mock_resp)
    channel._client = mock_client

    channel._sent_messages["msg_42"] = "target_chan"

    # Delete using composite channel_id|message_id
    result = await channel.delete("target_chan|msg_42")

    mock_client.delete.assert_awaited_with(
        "/channels/target_chan/messages/msg_42",
        headers=channel._auth_headers(),
    )
    assert result.target_id == "target_chan"
    assert result.provider_message_id == "msg_42"
    assert "msg_42" not in channel._sent_messages


@pytest.mark.asyncio
async def test_messaging_tool_delete_discord_honors_target() -> None:
    channel = DiscordChannel(DiscordChannelConfig(token="fake_token"))

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.delete = AsyncMock(return_value=mock_resp)
    channel._client = mock_client

    _channels["discord"] = channel
    try:
        await message(
            channel="discord",
            target="target_guild_channel",
            action="delete",
            message_id="msg_999",
        )
        mock_client.delete.assert_awaited_with(
            "/channels/target_guild_channel/messages/msg_999",
            headers=channel._auth_headers(),
        )
    finally:
        _channels.pop("discord", None)
