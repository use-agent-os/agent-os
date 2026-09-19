from __future__ import annotations

from agentos.channels.slack import SlackChannel
from agentos.channels.types import IncomingMessage


def test_extract_mentions_supports_enterprise_grid_and_bot_ids() -> None:
    """Issue #3050: Slack mentions can be standard users (U...), Enterprise Grid
    users (W...), or bots (B...), with or without display names."""
    text = "Hello <@U12345678> and <@W87654321|enterprise_user> and bot <@B99999999>!"
    mentions = SlackChannel.extract_mentions(text)
    assert mentions == ["U12345678", "W87654321", "B99999999"]


def test_is_mentioned_enterprise_grid_user() -> None:
    """Issue #3050: Ensure is_mentioned recognises enterprise grid bot ID."""
    channel = SlackChannel.__new__(SlackChannel)
    channel.bot_user_id = "W99887766"

    text = "Hey <@W99887766|mybot> what is the status?"
    assert channel.is_mentioned(text) is True

    msg = IncomingMessage(
        id="1",
        channel_name="slack",
        channel_id="C123",
        sender_id="U1111",
        content=text,
    )
    assert channel.is_group_mentioned(msg) is True


def test_is_mentioned_bot_user_id() -> None:
    """Issue #3050: Ensure is_mentioned recognises bot user ID starting with B."""
    channel = SlackChannel.__new__(SlackChannel)
    channel.bot_user_id = "B12345678"

    assert channel.is_mentioned("<@B12345678> please help") is True
    assert channel.is_mentioned("<@U12345678> please help") is False
