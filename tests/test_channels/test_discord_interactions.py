from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from agentos.channels.contract import ChannelSendStatus
from agentos.channels.discord import DiscordChannel, DiscordChannelConfig
from agentos.channels.types import OutgoingMessage
from agentos.gateway.channel_dispatch import (
    _build_command_reply_message,
    _should_skip_unmentioned,
)


class _FakeResponse:
    def __init__(self, *, status_code: int, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self._payload = payload or {}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeDiscordClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def post(self, path: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append(("POST", path, kwargs))
        return _FakeResponse(status_code=204)

    async def patch(self, path: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append(("PATCH", path, kwargs))
        return _FakeResponse(status_code=200, payload={"id": "response-message-1"})


def _application_command(
    *,
    interaction_id: str = "interaction-1",
    interaction_type: int = 2,
) -> dict[str, Any]:
    return {
        "id": interaction_id,
        "application_id": "event-app-id",
        "type": interaction_type,
        "token": "interaction-token",
        "guild_id": "guild-1",
        "channel_id": "channel-1",
        "channel_type": 0,
        "member": {"user": {"id": "user-1"}},
        "data": {
            "type": 1,
            "name": "status",
            "options": [],
        },
    }


async def test_discord_application_command_is_deferred_before_group_dispatch() -> None:
    client = _FakeDiscordClient()
    channel = DiscordChannel(
        DiscordChannelConfig(
            token="bot-token", application_id="configured-app-id", allowed_user_ids=["user-1"]
        )
    )
    channel._client = client

    await channel._handle_dispatch("INTERACTION_CREATE", _application_command())

    assert client.calls == [
        (
            "POST",
            "/interactions/interaction-1/interaction-token/callback",
            {"json": {"type": 5}},
        )
    ]
    message = await channel.receive()
    assert message.content == "/status"
    assert message.metadata["interaction_type"] == "slash_command"
    assert message.metadata["interaction_application_id"] == "event-app-id"
    assert message.metadata["is_group"] is True
    assert channel.is_group_mentioned(message) is True
    assert (
        _should_skip_unmentioned(
            channel,
            message,
            "agent:main:discord:group:channel-1",
        )
        is False
    )


async def test_discord_slash_command_preserves_falsy_option_values() -> None:
    """Issue #1229 — option values 0 and False are falsy but real: the old
    truthiness filter dropped them from the reconstructed command string."""
    client = _FakeDiscordClient()
    channel = DiscordChannel(
        DiscordChannelConfig(token="bot-token", application_id="configured-app-id")
    )
    channel._client = client
    payload = _application_command()
    payload["data"] = {
        "type": 1,
        "name": "temperature",
        "options": [
            {"name": "temp", "type": 4, "value": 0},
            {"name": "verbose", "type": 5, "value": False},
            {"name": "query", "type": 3, "value": "test"},
        ],
    }

    await channel._handle_dispatch("INTERACTION_CREATE", payload)

    message = await channel.receive()
    assert message.content == "/temperature 0 False test"


async def test_discord_slash_command_reconstructs_subcommand() -> None:
    """Issue #1229 — a type-1 SUB_COMMAND extends the command path."""
    client = _FakeDiscordClient()
    channel = DiscordChannel(
        DiscordChannelConfig(token="bot-token", application_id="configured-app-id")
    )
    channel._client = client
    payload = _application_command()
    payload["data"] = {
        "type": 1,
        "name": "agentos",
        "options": [
            {
                "name": "configure",
                "type": 1,
                "options": [{"name": "provider", "type": 3, "value": "openrouter"}],
            }
        ],
    }

    await channel._handle_dispatch("INTERACTION_CREATE", payload)

    message = await channel.receive()
    assert message.content == "/agentos configure openrouter"


async def test_discord_slash_command_reconstructs_subcommand_group() -> None:
    """Issue #1229 — a type-2 SUBCOMMAND_GROUP descends into its subcommand
    and keeps falsy leaf values (0)."""
    client = _FakeDiscordClient()
    channel = DiscordChannel(
        DiscordChannelConfig(token="bot-token", application_id="configured-app-id")
    )
    channel._client = client
    payload = _application_command()
    payload["data"] = {
        "type": 1,
        "name": "config",
        "options": [
            {
                "name": "set",
                "type": 2,
                "options": [
                    {"name": "key", "type": 3, "value": "temperature"},
                    {"name": "value", "type": 4, "value": 0},
                ],
            }
        ],
    }

    await channel._handle_dispatch("INTERACTION_CREATE", payload)

    message = await channel.receive()
    assert message.content == "/config set temperature 0"


async def test_discord_command_reply_completes_original_interaction_response() -> None:
    client = _FakeDiscordClient()
    channel = DiscordChannel(
        DiscordChannelConfig(token="bot-token", application_id="configured-app-id")
    )
    channel._client = client
    await channel._handle_dispatch("INTERACTION_CREATE", _application_command())
    inbound = await channel.receive()
    command_reply = OutgoingMessage(
        content="All systems operational.",
        reply_to=inbound.channel_id,
        metadata={"command": "status", "method": "status"},
    )

    routed_reply = _build_command_reply_message(channel, command_reply, inbound)
    result = await channel.send(routed_reply)

    assert routed_reply.metadata["interaction_token"] == "interaction-token"
    assert routed_reply.metadata["command"] == "status"
    assert client.calls[-1] == (
        "PATCH",
        "/webhooks/event-app-id/interaction-token/messages/@original",
        {"json": {"content": "All systems operational."}},
    )
    assert not any("/channels/" in path for _method, path, _kwargs in client.calls)
    assert result.status is ChannelSendStatus.SENT
    assert result.provider_message_id == "response-message-1"


async def test_discord_streaming_reply_completes_original_interaction_response() -> None:
    client = _FakeDiscordClient()
    channel = DiscordChannel(
        DiscordChannelConfig(token="bot-token", application_id="configured-app-id")
    )
    channel._client = client
    await channel._handle_dispatch("INTERACTION_CREATE", _application_command())
    inbound = await channel.receive()
    client.calls.clear()

    async def _chunks() -> AsyncIterator[str]:
        yield "Streaming response."

    message_id = await channel.send_streaming(
        _chunks(),
        **channel.streaming_reply_kwargs(inbound),
    )

    assert client.calls
    assert all(
        method == "PATCH" and path == "/webhooks/event-app-id/interaction-token/messages/@original"
        for method, path, _kwargs in client.calls
    )
    assert client.calls[-1][2] == {"json": {"content": "Streaming response."}}
    assert not any("/channels/" in path for _method, path, _kwargs in client.calls)
    assert message_id == "response-message-1"


async def test_discord_duplicate_interaction_id_is_acknowledged_and_enqueued_once() -> None:
    client = _FakeDiscordClient()
    channel = DiscordChannel(DiscordChannelConfig(token="bot-token"))
    channel._client = client
    payload = _application_command()

    await channel._handle_dispatch("INTERACTION_CREATE", payload)
    await channel._handle_dispatch("INTERACTION_CREATE", payload)

    assert channel._queue.qsize() == 1
    assert [method for method, _path, _kwargs in client.calls] == ["POST"]


@pytest.mark.parametrize("interaction_type", [1, 3, 4, 5])
async def test_discord_non_command_interaction_types_are_ignored(
    interaction_type: int,
) -> None:
    client = _FakeDiscordClient()
    channel = DiscordChannel(DiscordChannelConfig(token="bot-token"))
    channel._client = client

    await channel._handle_dispatch(
        "INTERACTION_CREATE",
        _application_command(interaction_type=interaction_type),
    )

    assert channel._queue.empty()
    assert client.calls == []
