from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from starlette.requests import Request

from agentos.channel_pairing import ChannelAdmission
from agentos.channels.discord import DiscordChannel, DiscordChannelConfig
from agentos.channels.slack import SlackChannel
from agentos.channels.telegram import TelegramChannel, TelegramChannelConfig
from agentos.channels.types import IncomingMessage, OutgoingMessage
from agentos.engine.types import DoneEvent, TextDeltaEvent, ToolResultEvent
from agentos.gateway.approval_queue import get_approval_queue, reset_approval_queue
from agentos.gateway.channel_dispatch import (
    _run_turn_batch_path,
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
    callback_query = {
        "id": "cb123",
        "from": {"id": 12345, "username": "bob"},
        "message": {
            "message_id": 999,
            "chat": {"id": 12345, "type": "private"},
            "text": "Do you want to run this command?",
        },
        "data": f"approve:{approval_id}",
    }

    # Verify that an unpaired user gets rejected (admission check fails)
    await channel._handle_telegram_callback(callback_query)
    # Verify ApprovalQueue is NOT resolved
    entry = queue.get(approval_id)
    assert entry.resolved is False
    assert len(calls) == 1
    assert calls[0][0] == "answerCallbackQuery"
    assert "Only paired users" in calls[0][1]["text"]
    calls.clear()

    # Pair the user and verify they can resolve it
    admission_mock = ChannelAdmission("telegram", "12345", "grant1", 1)
    with patch.object(channel.pairing_store, "admission", return_value=admission_mock):
        await channel._handle_telegram_callback(callback_query)

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

    # Verify a virtual "Approve" message is enqueued
    msg = await channel.receive()
    assert msg.content == "Approve"
    assert msg.sender_id == "12345"


@pytest.mark.asyncio
async def test_telegram_callback_query_session_mismatch() -> None:
    queue = get_approval_queue()
    # Approval is bound to a specific session key
    approval_id = queue.request(
        "exec",
        {
            "argv": ["rm", "-rf"],
            "action_kind": "exec",
            "sessionKey": "agent:main:telegram:direct:99999",
        },
    )

    channel = TelegramChannel(TelegramChannelConfig(token="test-token"))
    calls = []

    async def fake_api(method: str, payload: dict | None = None) -> Any:
        calls.append((method, payload or {}))
        return True

    channel._api = fake_api

    callback_query = {
        "id": "cb123",
        "from": {"id": 12345, "username": "bob"},
        "message": {
            "message_id": 999,
            "chat": {"id": 12345, "type": "private"},
            "text": "Do you want to run this command?",
        },
        "data": f"approve:{approval_id}",
    }

    # Pair the user but click from a mismatched chat context (session key direct:99999 vs 12345)
    admission_mock = ChannelAdmission("telegram", "12345", "grant1", 1)
    with patch.object(channel.pairing_store, "admission", return_value=admission_mock):
        await channel._handle_telegram_callback(callback_query)

    # Verify ApprovalQueue is NOT resolved
    entry = queue.get(approval_id)
    assert entry.resolved is False
    assert len(calls) == 1
    assert calls[0][0] == "answerCallbackQuery"
    assert "does not belong to this chat" in calls[0][1]["text"]


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

    class FakeResponse:
        def raise_for_status(self):
            pass

    async def fake_post(url, json=None, **kwargs):
        post_calls.append((url, json))
        return FakeResponse()

    # Reject if policy does not admit
    from dataclasses import replace

    channel.policy = replace(
        channel.policy, allowlist=frozenset({"U99999"}), allowlist_enabled=True
    )

    with patch("httpx.AsyncClient.post", side_effect=fake_post):
        await channel._handle_slack_interactive(payload)

    # Verify ApprovalQueue is NOT resolved since clicker was rejected
    entry = queue.get(approval_id)
    assert entry.resolved is False
    assert len(post_calls) == 0

    # Admit by opening allowlist or disabling it
    channel.policy = replace(channel.policy, allowlist=frozenset(), allowlist_enabled=False)

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
    assert len(sent_json["blocks"]) == 2
    assert sent_json["blocks"][1]["text"]["text"] == "*Approved ✅*"

    # Verify a virtual "Approve" message is enqueued
    msg = await channel.receive()
    assert msg.content == "Approve"
    assert msg.sender_id == "U12345"


@pytest.mark.asyncio
async def test_slack_interactive_payload_unsigned_rejected() -> None:
    channel = SlackChannel(token="xoxb-test", slack_channel_id="C12345", signing_secret=None)

    # Mock fastapi request
    scope = {
        "type": "http",
        "method": "POST",
        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
    }
    req = Request(scope)

    async def mock_body():
        return b"payload=%7B%22type%22%3A%22block_actions%22%7D"

    req.body = mock_body

    resp = await channel._handle_webhook(req)
    assert resp.status_code == 401


def _unsigned_json_request(payload: dict[str, Any]) -> Request:
    body = json.dumps(payload).encode()
    request = Request({"type": "http", "method": "POST", "headers": []})

    async def _body() -> bytes:
        return body

    request.body = _body  # type: ignore[method-assign]
    return request


@pytest.mark.asyncio
async def test_slack_event_callback_unsigned_rejected() -> None:
    """No signing secret means no way to attribute the POST to Slack, so the
    event must not be ingested (GH #674)."""
    channel = SlackChannel(token="xoxb-test", slack_channel_id="C12345", signing_secret=None)

    resp = await channel._handle_webhook(  # noqa: SLF001
        _unsigned_json_request(
            {
                "type": "event_callback",
                "event": {
                    "type": "message",
                    "user": "U1",
                    "channel": "C12345",
                    "text": "pwned",
                    "ts": "1710000000.000100",
                    "channel_type": "im",
                },
            }
        )
    )

    assert resp.status_code == 401
    assert channel._queue.empty()  # noqa: SLF001


@pytest.mark.asyncio
async def test_slack_slash_command_unsigned_rejected() -> None:
    """Slash commands ride the same unauthenticated path as event callbacks."""
    channel = SlackChannel(token="xoxb-test", slack_channel_id="C12345", signing_secret=None)
    scope = {
        "type": "http",
        "method": "POST",
        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
    }
    request = Request(scope)

    async def _body() -> bytes:
        return b"command=%2Fstatus&user_id=U1&channel_id=C12345"

    request.body = _body  # type: ignore[method-assign]

    resp = await channel._handle_webhook(request)  # noqa: SLF001

    assert resp.status_code == 401
    assert channel._queue.empty()  # noqa: SLF001


@pytest.mark.asyncio
async def test_slack_event_callback_rejected_when_signing_secret_is_blank() -> None:
    """An empty HMAC key verifies anything an attacker signs with it, so a
    blank secret counts as no secret."""
    channel = SlackChannel(token="xoxb-test", slack_channel_id="C12345", signing_secret="")

    resp = await channel._handle_webhook(  # noqa: SLF001
        _unsigned_json_request({"type": "event_callback", "event": {"type": "message"}})
    )

    assert resp.status_code == 401
    assert channel._queue.empty()  # noqa: SLF001


@pytest.mark.asyncio
async def test_slack_url_verification_still_answered_unsigned() -> None:
    """The handshake has no side effects, so it stays open — an operator can
    pass Slack's endpoint check before the signing secret is wired up."""
    channel = SlackChannel(token="xoxb-test", slack_channel_id="C12345", signing_secret=None)

    resp = await channel._handle_webhook(  # noqa: SLF001
        _unsigned_json_request({"type": "url_verification", "challenge": "abc123"})
    )

    assert resp.status_code == 200
    assert json.loads(bytes(resp.body)) == {"challenge": "abc123"}


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

    # Reject if policy does not admit
    from dataclasses import replace

    channel.policy = replace(
        channel.policy, allowlist=frozenset({"usr999"}), allowlist_enabled=True
    )

    await channel._handle_discord_component_interaction(data)
    entry = queue.get(approval_id)
    assert entry.resolved is False
    assert len(post_calls) == 1
    assert "Only paired users" in post_calls[0][1]["data"]["content"]
    post_calls.clear()

    # Admit
    channel.policy = replace(channel.policy, allowlist=frozenset(), allowlist_enabled=False)
    channel._dedupe._seen.clear()

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

    # Verify a virtual "Denied" message is enqueued
    msg = await channel.receive()
    assert msg.content == "Deny"
    assert msg.sender_id == "usr123"


_PENDING_APPROVAL: dict[str, Any] = {
    "approval_id": "app-123",
    "command": "ls -l",
    "tool_name": "shell",
    "status": "approval_required",
}


def _capture_sends(channel: Any) -> list[OutgoingMessage]:
    """Stub the adapter's outbound send and collect the messages it emits."""
    sent: list[OutgoingMessage] = []

    async def fake_send(message: OutgoingMessage) -> None:
        sent.append(message)

    channel.send = fake_send
    return sent


def _inbound() -> IncomingMessage:
    return IncomingMessage(sender_id="usr1", channel_id="chan1", content="hi")


@pytest.mark.asyncio
async def test_send_channel_approval_prompt_telegram() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="test-token"))
    sent = _capture_sends(channel)

    await _send_channel_approval_prompt(channel, _inbound(), dict(_PENDING_APPROVAL))

    assert len(sent) == 1
    assert "shell" in sent[0].content
    assert "ls -l" in sent[0].content
    row = sent[0].metadata["reply_markup"]["inline_keyboard"][0]
    assert [button["text"] for button in row] == ["Approve", "Deny"]
    assert [button["callback_data"] for button in row] == ["approve:app-123", "deny:app-123"]


@pytest.mark.asyncio
async def test_send_channel_approval_prompt_slack() -> None:
    channel = SlackChannel(token="xoxb-test", slack_channel_id="C12345")
    sent = _capture_sends(channel)

    await _send_channel_approval_prompt(channel, _inbound(), dict(_PENDING_APPROVAL))

    assert len(sent) == 1
    elements = sent[0].metadata["blocks"][1]["elements"]
    assert [element["text"]["text"] for element in elements] == ["Approve", "Deny"]
    assert [element["value"] for element in elements] == ["approve:app-123", "deny:app-123"]


@pytest.mark.asyncio
async def test_send_channel_approval_prompt_discord() -> None:
    channel = DiscordChannel(DiscordChannelConfig(token="test-token"))
    sent = _capture_sends(channel)

    await _send_channel_approval_prompt(channel, _inbound(), dict(_PENDING_APPROVAL))

    assert len(sent) == 1
    components = sent[0].metadata["components"][0]["components"]
    assert [component["label"] for component in components] == ["Approve", "Deny"]
    assert [component["custom_id"] for component in components] == [
        "approve:app-123",
        "deny:app-123",
    ]


class _ApprovalTurnRunner:
    """Yield a gated tool result followed by the assistant's closing text."""

    async def run(self, message: str, session_key: str, **kwargs: Any) -> Any:
        yield ToolResultEvent(
            tool_use_id="call-1",
            tool_name="shell",
            result=json.dumps(_PENDING_APPROVAL),
        )
        yield TextDeltaEvent(text="waiting for approval")
        yield DoneEvent()


def _batch_turn_kwargs() -> dict[str, Any]:
    return {
        "turn_runner": _ApprovalTurnRunner(),
        "msg": _inbound(),
        "session_key": "agent:main:telegram:direct:chan1",
        "tool_ctx": SimpleNamespace(agent_id="main"),
        "event_bridge": None,
        "semantic_message": None,
        "config": SimpleNamespace(
            agent_stream_heartbeat_interval_seconds=60.0,
            agent_stream_idle_timeout_seconds=5.0,
        ),
    }


@pytest.mark.asyncio
async def test_channel_turn_delivers_approval_prompt_and_completes() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="test-token"))
    sent = _capture_sends(channel)

    await _run_turn_batch_path(channel, **_batch_turn_kwargs())

    keyboard = sent[0].metadata["reply_markup"]["inline_keyboard"][0]
    assert [button["callback_data"] for button in keyboard] == [
        "approve:app-123",
        "deny:app-123",
    ]
    assert "waiting for approval" in sent[-1].content


@pytest.mark.asyncio
async def test_channel_turn_survives_approval_prompt_rendering_failure() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="test-token"))
    sent = _capture_sends(channel)

    async def broken_prompt(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("prompt rendering blew up")

    with patch(
        "agentos.gateway.channel_dispatch._send_channel_approval_prompt",
        broken_prompt,
    ):
        await _run_turn_batch_path(channel, **_batch_turn_kwargs())

    assert "shell" in sent[0].content
    assert "ls -l" in sent[0].content
    assert "reply_markup" not in sent[0].metadata
    assert "waiting for approval" in sent[-1].content


@pytest.mark.asyncio
async def test_slack_interactive_task_retained_and_cleaned_up() -> None:
    channel = SlackChannel(token="xoxb-test", slack_channel_id="C123")
    proceed = asyncio.Event()

    async def _mock_interactive(_payload: dict[str, Any]) -> None:
        await proceed.wait()

    channel._handle_slack_interactive = _mock_interactive  # type: ignore[method-assign]
    payload = {"type": "interactive", "id": 1}
    task = channel._spawn_interactive_task(payload)

    assert task in channel._interactive_tasks
    assert len(channel._interactive_tasks) == 1

    proceed.set()
    await task

    assert task not in channel._interactive_tasks
    assert len(channel._interactive_tasks) == 0


@pytest.mark.asyncio
async def test_slack_channel_stop_drains_interactive_tasks() -> None:
    channel = SlackChannel(token="xoxb-test", slack_channel_id="C123")

    async def _mock_interactive(_payload: dict[str, Any]) -> None:
        await asyncio.Event().wait()

    channel._handle_slack_interactive = _mock_interactive  # type: ignore[method-assign]
    task = channel._spawn_interactive_task({"type": "interactive"})

    assert task in channel._interactive_tasks
    assert not task.done()

    await channel.stop()

    assert task.done()
    assert task.cancelled()
    assert len(channel._interactive_tasks) == 0

