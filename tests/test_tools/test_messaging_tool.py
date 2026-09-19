"""Unit tests for the built-in message tool and metadata routing."""

from __future__ import annotations

import json

import pytest

from agentos.channels.types import OutgoingMessage
from agentos.tools.builtin.messaging import (
    _outgoing_metadata,
    _reply_to_target,
    message,
    register_channel,
    unregister_channel,
)


class DummyChannelAdapter:
    def __init__(self) -> None:
        self.sent: list[OutgoingMessage] = []

    async def send(self, msg: OutgoingMessage) -> None:
        self.sent.append(msg)


def test_outgoing_metadata_carries_slack_channel_and_thread() -> None:
    """_outgoing_metadata preserves target channel in metadata for Slack."""
    meta = _outgoing_metadata("slack", "C012345678", "1712345.678")
    assert meta.get("channel") == "C012345678"
    assert meta.get("thread_ts") == "1712345.678"

    meta_no_thread = _outgoing_metadata("slack", "C012345678", None)
    assert meta_no_thread.get("channel") == "C012345678"
    assert "thread_ts" not in meta_no_thread


def test_reply_to_target_resolution() -> None:
    assert _reply_to_target("slack", "C012345678", "1712345.678") in ("1712345.678", "C012345678")
    assert _reply_to_target("telegram", "123456", "999") == "123456"
    assert _reply_to_target("discord", "chan_1", "thread_2") == "thread_2"


@pytest.mark.asyncio
async def test_message_tool_delivers_slack_target_in_metadata() -> None:
    adapter = DummyChannelAdapter()
    register_channel("slack", adapter)
    try:
        res_raw = await message(
            channel="slack",
            target="C987654321",
            text="Hello Slack team",
            thread_id="1712345.678",
        )
        res = json.loads(res_raw)
        assert res["status"] == "sent"
        assert res["channel"] == "slack"
        assert res["target"] == "C987654321"

        assert len(adapter.sent) == 1
        sent_msg = adapter.sent[0]
        assert sent_msg.content == "Hello Slack team"
        assert sent_msg.metadata.get("channel") == "C987654321"
        assert sent_msg.metadata.get("thread_ts") == "1712345.678"
    finally:
        unregister_channel("slack")
