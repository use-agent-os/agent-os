from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from agentos.channels.discord import DiscordChannel, DiscordChannelConfig
from agentos.channels.slack import SlackChannel
from agentos.channels.telegram import TelegramChannel, TelegramChannelConfig
from agentos.channels.types import IncomingMessage, OutgoingMessage
from agentos.gateway.approval_queue import get_approval_queue, reset_approval_queue
from agentos.gateway.channel_dispatch import (
    _send_channel_approval_prompt,
)


@pytest.fixture(autouse=True)
def clean_approval_queue():
    reset_approval_queue()
    yield
    reset_approval_queue()


@pytest.mark.asyncio
async def test_telegram_callback_query_parsing_and_resolution() -> None:
    queue = get_approval_queue()
    approval_id = queue.request("exec", {"argv": ["rm", "-rf"], "action_kind": "exec"})

    channel = TelegramChannel(TelegramChannelConfig(token="test-token"))
    calls = []

    async def fake_api(method: str, payload: dict | None = None) -> Any:
        calls.append((method, payload or {}))
        return True

    channel._api = fake_api

    # Simulate callback query update for "approve"
    update = {
        "update_id": 100,
        "callback_query": {
            "id": "cb123",
            "from": {"id": 12345, "username": "bob"},
            "message": {
                "message_id": 999,
                "chat": {"id": 12345, "type": "private"},
                "text": "Do you want to run this command?",
            },
            "data": f"approve:{approval_id}",
        },
    }

    inbound = channel.parse_incoming(update)
    assert inbound.content == "Approve"
    assert inbound.sender_id == "12345"

    # Wait for the background task _handle_telegram_callback to complete
    await asyncio.sleep(0.1)

    # Verify ApprovalQueue is resolved
    entry = queue.get(approval_id)
    assert entry.resolved is True
    assert entry.approved is True

    # Verify Telegram API edits the message and clears buttons
    assert len(calls) == 2
    assert calls[0][0] == "answerCallbackQuery"
    assert calls[0][1]["callback_query_id"] == "cb123"
    assert calls[1][0] == "editMessageText"
    assert calls[1][1]["message_id"] == 999
    assert "Approved ✅" in calls[1][1]["text"]
    assert calls[1][1]["reply_markup"] is None


@pytest.mark.asyncio
async def test_slack_interactive_payload_handling() -> None:
    queue = get_approval_queue()
    approval_id = queue.request("exec", {"argv": ["rm", "-rf"], "action_kind": "exec"})

    channel = SlackChannel(token="xoxb-test", slack_channel_id="C12345")

    # Mock parse_event
    channel.parse_event = lambda ev: IncomingMessage(
        sender_id=ev["user"],
        channel_id=ev["channel"],
        content=ev["text"],
    )

    payload = {
        "type": "block_actions",
        "user": {"id": "U12345"},
        "channel": {"id": "C12345"},
        "response_url": "https://hooks.slack.com/actions/test",
        "message": {
            "text": "Approve this execution?",
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "Approve this execution?"},
                },
                {
                    "type": "actions",
                    "block_id": "approval_actions",
                    "elements": [{"type": "button", "value": f"approve:{approval_id}"}],
                },
            ],
        },
        "actions": [{"value": f"approve:{approval_id}"}],
    }

    post_calls = []

    # Patch httpx.AsyncClient.post
    class FakeResponse:
        def raise_for_status(self):
            pass

    async def fake_post(url, json=None, **kwargs):
        post_calls.append((url, json))
        return FakeResponse()

    with patch("httpx.AsyncClient.post", side_effect=fake_post):
        await channel._handle_slack_interactive(payload)

    # Verify ApprovalQueue is resolved
    entry = queue.get(approval_id)
    assert entry.resolved is True
    assert entry.approved is True

    # Verify Slack message is updated using response_url
    assert len(post_calls) == 1
    assert post_calls[0][0] == "https://hooks.slack.com/actions/test"
    sent_json = post_calls[0][1]
    assert sent_json["replace_original"] is True
    # Verify approval_actions block is removed and decision is appended
    assert len(sent_json["blocks"]) == 2
    assert sent_json["blocks"][1]["text"]["text"] == "*Approved ✅*"

    # Verify a virtual "Approve" message is enqueued
    msg = await channel.receive()
    assert msg.content == "Approve"
    assert msg.sender_id == "U12345"


@pytest.mark.asyncio
async def test_discord_component_interaction_handling() -> None:
    queue = get_approval_queue()
    approval_id = queue.request("exec", {"argv": ["rm", "-rf"], "action_kind": "exec"})

    channel = DiscordChannel(DiscordChannelConfig(token="test-token"))

    post_calls = []

    async def fake_post(path, json=None, headers=None, **kwargs):
        post_calls.append((path, json))

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"id": "999"}

        return FakeResp()

    channel._get_client = lambda: AsyncMock(post=fake_post)

    data = {
        "type": 3,  # Component interaction
        "id": "int123",
        "token": "token123",
        "channel_id": "chan123",
        "user": {"id": "usr123"},
        "message": {
            "content": "Approval requested",
        },
        "data": {
            "custom_id": f"deny:{approval_id}",
        },
    }

    await channel._handle_discord_component_interaction(data)

    # Verify ApprovalQueue is resolved
    entry = queue.get(approval_id)
    assert entry.resolved is True
    assert entry.approved is False

    # Verify Discord API UPDATE_MESSAGE callback
    assert len(post_calls) == 1
    assert post_calls[0][0] == "/interactions/int123/token123/callback"
    payload = post_calls[0][1]
    assert payload["type"] == 7  # UPDATE_MESSAGE
    assert "Denied ❌" in payload["data"]["content"]
    assert payload["data"]["components"] == []

    # Verify a virtual "Deny" message is enqueued
    msg = await channel.receive()
    assert msg.content == "Deny"
    assert msg.sender_id == "usr123"


@pytest.mark.asyncio
async def test_send_channel_approval_prompt() -> None:
    class DummyChannel:
        def transport_name(self):
            return "telegram"

        async def send(self, msg: OutgoingMessage):
            self.sent_msg = msg

    channel = DummyChannel()
    inbound = IncomingMessage(sender_id="usr1", channel_id="chan1", content="hi")
    pending = {
        "approval_id": "app-123",
        "command": "ls -l",
        "tool_name": "shell",
        "status": "approval_required",
    }

    await _send_channel_approval_prompt(channel, inbound, pending)

    assert channel.sent_msg is not None
    assert "shell" in channel.sent_msg.content
    assert "ls -l" in channel.sent_msg.content
    assert "reply_markup" in channel.sent_msg.metadata
    assert channel.sent_msg.metadata["reply_markup"]["inline_keyboard"][0][0]["text"] == "Approve"
