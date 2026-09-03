"""Telegram polling acknowledges updates only after local handling succeeds."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agentos.channels.telegram import TelegramChannel, TelegramChannelConfig


def _message_update(update_id: int) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 7},
            "text": "after callback",
        },
    }


async def test_failed_callback_is_retried_before_later_batch_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="test-token", poll_idle_sleep_s=0.25))
    callback_update = {"update_id": 10, "callback_query": {"id": "callback-10"}}
    later_update = _message_update(11)
    payloads: list[dict[str, Any]] = []
    queue_sizes_before_poll: list[int] = []

    async def api(method: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        assert method == "getUpdates"
        payloads.append(dict(payload))
        queue_sizes_before_poll.append(channel._queue.qsize())
        if len(payloads) <= 2:
            return [callback_update, later_update]
        raise asyncio.CancelledError

    callback_attempts = 0

    async def handle_callback(payload: dict[str, Any]) -> None:
        nonlocal callback_attempts
        assert payload == callback_update["callback_query"]
        callback_attempts += 1
        if callback_attempts == 1:
            raise RuntimeError("transient callback dependency failure")

    sleep = AsyncMock()
    monkeypatch.setattr("agentos.channels.telegram.asyncio.sleep", sleep)
    channel._api = api  # type: ignore[method-assign]
    channel._handle_telegram_callback = handle_callback  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await channel._poll_loop()

    assert callback_attempts == 2
    assert "offset" not in payloads[1]
    assert payloads[2]["offset"] == 12
    assert queue_sizes_before_poll == [0, 0, 1]
    message = channel._queue.get_nowait()
    assert message.content == "after callback"
    assert channel._queue.empty()
    sleep.assert_awaited_once_with(0.25)


async def test_unsupported_update_is_acknowledged(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="test-token"))
    payloads: list[dict[str, Any]] = []

    async def api(method: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        assert method == "getUpdates"
        payloads.append(dict(payload))
        if len(payloads) == 1:
            return [{"update_id": 20, "poll": {"id": "unsupported"}}]
        raise asyncio.CancelledError

    channel._api = api  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await channel._poll_loop()

    assert payloads[1]["offset"] == 21
    assert channel._queue.empty()
