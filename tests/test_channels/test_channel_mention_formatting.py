from __future__ import annotations

from agentos.channels.discord import DiscordChannel
from agentos.channels.slack import SlackChannel


def test_slack_mention_formatting() -> None:
    assert SlackChannel.format_mention("U12345") == "<@U12345>"
    assert SlackChannel.format_channel_mention("C67890") == "<#C67890>"


def test_discord_mention_formatting() -> None:
    assert DiscordChannel.format_mention("123456789") == "<@123456789>"
    assert DiscordChannel.format_channel_mention("987654321") == "<#987654321>"
    assert DiscordChannel.format_role_mention("555666777") == "<@&555666777>"
