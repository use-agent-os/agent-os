"""Native chat-platform command menus derived from the unified registry."""

from __future__ import annotations

from typing import Any

from agentos.engine.commands import DEFAULT_REGISTRY, Surface

# Platform constraints:
# https://core.telegram.org/bots/api#setmycommands
# https://discord.com/developers/docs/interactions/application-commands
# https://api.slack.com/reference/manifests
TELEGRAM_COMMAND_LIMIT = 100
TELEGRAM_DESCRIPTION_LIMIT = 256
DISCORD_DESCRIPTION_LIMIT = 100
SLACK_DESCRIPTION_LIMIT = 48


def _channel_commands() -> tuple[tuple[str, str], ...]:
    """Return canonical CHANNEL commands as ``(name, description)`` pairs."""
    return tuple(
        (command.name.lstrip("/"), command.description)
        for command in DEFAULT_REGISTRY.for_surface(Surface.CHANNEL)
    )


def telegram_bot_commands() -> list[dict[str, str]]:
    """Build the payload accepted by Telegram's ``setMyCommands`` API."""
    return [
        {"command": name, "description": description[:TELEGRAM_DESCRIPTION_LIMIT]}
        for name, description in _channel_commands()
    ][:TELEGRAM_COMMAND_LIMIT]


def discord_application_commands() -> list[dict[str, Any]]:
    """Build Discord CHAT_INPUT application-command registrations."""
    return [
        {"name": name, "description": description[:DISCORD_DESCRIPTION_LIMIT], "type": 1}
        for name, description in _channel_commands()
    ]


def slack_command_manifest(request_url: str) -> dict[str, Any]:
    """Build the Slack app-manifest fragment for native slash commands.

    Slack slash commands are configured in an app manifest instead of through a
    bot-token API. ``request_url`` must point at this channel's Slack webhook
    route, which accepts the resulting form submissions.
    """
    if not request_url.strip():
        raise ValueError("Slack slash-command manifest requires a request URL")
    return {
        "features": {
            "slash_commands": [
                {
                    "command": f"/{name}",
                    "description": description[:SLACK_DESCRIPTION_LIMIT],
                    "should_escape": False,
                    "url": request_url,
                }
                for name, description in _channel_commands()
            ]
        }
    }
