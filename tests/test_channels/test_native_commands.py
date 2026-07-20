from __future__ import annotations

from typing import Any

import pytest

from agentos.channels.discord import DiscordChannel, DiscordChannelConfig
from agentos.channels.slack import SlackChannel
from agentos.channels.telegram import TelegramChannel, TelegramChannelConfig
from agentos.engine.native_commands import (
    discord_application_commands,
    slack_command_manifest,
    telegram_bot_commands,
)


def test_native_command_payloads_are_derived_from_channel_registry() -> None:
    telegram = telegram_bot_commands()
    discord = discord_application_commands()

    assert {item["command"] for item in telegram} == {item["name"] for item in discord}
    assert {item["command"] for item in telegram} >= {"help", "new", "status"}
    assert all("/" not in item["command"] for item in telegram)
    assert all(item["type"] == 1 for item in discord)


def test_slack_manifest_uses_same_commands_and_request_url() -> None:
    manifest = slack_command_manifest("https://example.test/slack/events")
    commands = manifest["features"]["slash_commands"]

    assert {item["command"].removeprefix("/") for item in commands} == {
        item["command"] for item in telegram_bot_commands()
    }
    assert all(item["url"] == "https://example.test/slack/events" for item in commands)
    assert all(item["should_escape"] is False for item in commands)


def test_slack_manifest_requires_a_request_url() -> None:
    with pytest.raises(ValueError, match="request URL"):
        slack_command_manifest("")


@pytest.mark.asyncio
async def test_telegram_registration_pushes_the_unified_command_menu() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    calls: list[tuple[str, dict[str, Any] | None]] = []

    async def fake_api(method: str, payload: dict[str, Any] | None = None) -> None:
        calls.append((method, payload))

    channel._api = fake_api  # type: ignore[method-assign]  # noqa: SLF001
    await channel.register_slash_commands()

    assert calls == [("setMyCommands", {"commands": telegram_bot_commands()})]


@pytest.mark.asyncio
async def test_discord_registration_pushes_the_unified_command_menu() -> None:
    channel = DiscordChannel(DiscordChannelConfig(token="token", application_id="app-id"))
    calls: list[list[dict[str, Any]]] = []

    async def capture(commands: list[dict[str, Any]]) -> None:
        calls.append(commands)

    channel.register_slash_commands = capture  # type: ignore[method-assign]
    await channel.register_native_slash_commands()

    assert calls == [discord_application_commands()]


class _SlashRequest:
    headers = {"content-type": "application/x-www-form-urlencoded"}

    async def body(self) -> bytes:
        return b"command=%2Fstatus"

    async def form(self) -> dict[str, str]:
        return {
            "command": "/status",
            "text": "",
            "user_id": "U1",
            "channel_id": "C1",
            "channel_name": "general",
        }


@pytest.mark.asyncio
async def test_slack_slash_command_form_is_enqueued_for_channel_dispatch() -> None:
    channel = SlackChannel(token="token", slack_channel_id="C1")

    response = await channel._handle_webhook(_SlashRequest())  # noqa: SLF001

    assert response.status_code == 200
    message = await channel.receive()
    assert message.content == "/status"
    assert message.sender_id == "U1"
    assert message.channel_id == "C1"
