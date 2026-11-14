"""A reaction in a guild channel/thread carries no text, so the generic
``is_mentioned(msg.content)`` check is always ``False`` there -- the
group-mention gate (``_should_skip_unmentioned``) would otherwise drop
*every* reaction in *every* guild channel or thread, even one added to a
message the bot itself just sent. Reacting to the bot's own message is
already an unambiguous, directed response to it, equivalent to being
mentioned, and must not be gated out.
"""

from __future__ import annotations

from agentos.channels.discord import DiscordChannel, DiscordChannelConfig
from agentos.gateway.channel_dispatch import _should_skip_unmentioned


async def _channel_with_sent_message() -> tuple[DiscordChannel, str]:
    """``_sent_messages`` is normally populated by send()/send_file() as a
    side effect of actually posting a message via the REST API -- set it
    directly here rather than exercising that unrelated path."""
    channel = DiscordChannel(DiscordChannelConfig(token="token", allowed_user_ids=["user-1"]))
    channel._sent_messages["bot-msg-1"] = "guild-channel-1"
    return channel, "bot-msg-1"


async def test_reaction_on_the_bots_own_message_is_treated_as_mentioned() -> None:
    channel, bot_message_id = await _channel_with_sent_message()

    await channel._handle_dispatch(
        "MESSAGE_REACTION_ADD",
        {
            "message_id": bot_message_id,
            "channel_id": "guild-channel-1",
            "guild_id": "guild-1",
            "user_id": "user-1",
            "emoji": {"name": "thumbsup"},
        },
    )
    msg = await channel.receive()

    assert msg.metadata["is_group"] is True
    assert channel.is_group_mentioned(msg) is True
    assert (
        _should_skip_unmentioned(channel, msg, "agent:main:discord:group:guild-channel-1") is False
    )


async def test_reaction_on_someone_elses_message_is_still_gated() -> None:
    channel = DiscordChannel(DiscordChannelConfig(token="token", allowed_user_ids=["user-1"]))

    await channel._handle_dispatch(
        "MESSAGE_REACTION_ADD",
        {
            "message_id": "someone-elses-msg",
            "channel_id": "guild-channel-1",
            "guild_id": "guild-1",
            "user_id": "user-1",
            "emoji": {"name": "thumbsup"},
        },
    )
    msg = await channel.receive()

    assert msg.metadata["is_group"] is True
    assert channel.is_group_mentioned(msg) is False
    assert (
        _should_skip_unmentioned(channel, msg, "agent:main:discord:group:guild-channel-1") is True
    )
