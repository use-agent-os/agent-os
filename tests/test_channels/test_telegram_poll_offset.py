"""Regression tests for Telegram polling offset acknowledgement (issue #1027).

The polling loop used to set ``_update_offset = update_id + 1`` before invoking
the callback handler, so a transient handler failure acknowledged the update to
Telegram anyway and the callback was lost permanently. These tests drive the
real polling loop with synthetic Bot API responses and assert the offset is
only committed after the update is handled or deliberately ignored.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from agentos.channels.telegram import TelegramChannel, TelegramChannelConfig


def _callback_update(update_id: int) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {"id": f"cb{update_id}", "data": "approve:approval-1"},
    }


def _message_update(update_id: int) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 0,
            "chat": {"id": 100, "type": "private"},
            "from": {"id": 7, "username": "tester"},
            "text": "hello",
        },
    }


def _unsupported_update(update_id: int) -> dict[str, Any]:
    return {"update_id": update_id, "my_chat_member": {"status": "kicked"}}


def _install_api(
    channel: TelegramChannel,
    batches: list[list[dict[str, Any]]],
    calls: list[dict[str, Any]],
    queue_sizes: list[int] | None = None,
) -> None:
    """Serve ``batches`` from ``getUpdates``, then stop the loop with CancelledError.

    ``queue_sizes`` records the inbound queue size at the moment each served
    poll started, so assertions can inspect state between polls.
    """

    async def fake_api(method: str, payload: dict[str, Any] | None = None) -> Any:
        calls.append(payload or {})
        if len(calls) > len(batches):
            raise asyncio.CancelledError
        if queue_sizes is not None:
            queue_sizes.append(channel._queue.qsize())
        return batches[len(calls) - 1]

    channel._api = fake_api  # type: ignore[method-assign]


def _install_handler(
    channel: TelegramChannel,
    *,
    failures: int = 0,
    fail_ids: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Fail the first ``failures`` callback invocations and any ``fail_ids`` hits.

    ``failures`` counts across redeliveries, so a callback that fails once
    succeeds when the polling loop retries it with the same callback id.
    """

    async def fake_handler(cb: dict[str, Any]) -> None:
        handled.append(cb)
        if len(handled) <= failures or cb.get("id") in fail_ids:
            raise RuntimeError("transient handler failure")

    handled: list[dict[str, Any]] = []
    channel._handle_telegram_callback = fake_handler  # type: ignore[method-assign]
    return handled


async def _run_poll_loop(channel: TelegramChannel) -> None:
    with patch("agentos.channels.telegram.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(asyncio.CancelledError):
            await channel._poll_loop()


@pytest.mark.asyncio
async def test_failed_callback_is_retried_before_offset_acknowledges_it() -> None:
    """A failing handler keeps the offset on the failed update and retries it."""
    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    batch = [_callback_update(10)]
    calls: list[dict[str, Any]] = []
    _install_api(channel, [batch, batch], calls)
    handled = _install_handler(channel, failures=1)

    await _run_poll_loop(channel)

    assert len(handled) == 2  # failed once, then retried successfully
    assert len(calls) == 3
    assert "offset" not in calls[0]
    # The first retry does not acknowledge past the failed update.
    assert calls[1]["offset"] == 10
    # After the successful retry, offset advancement resumes normally.
    assert calls[2]["offset"] == 11


@pytest.mark.asyncio
async def test_later_update_in_batch_waits_for_failed_callback() -> None:
    """Updates after the failed callback are not processed ahead of its retry."""
    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    batch = [_callback_update(10), _message_update(11)]
    calls: list[dict[str, Any]] = []
    queue_sizes: list[int] = []
    _install_api(channel, [batch, batch], calls, queue_sizes)
    handled = _install_handler(channel, failures=1)

    await _run_poll_loop(channel)

    assert len(handled) == 2
    # First poll stopped at the failed callback: message 11 was not processed.
    assert calls[1]["offset"] == 10
    assert queue_sizes[1] == 0
    # Retry succeeded: the callback and message 11 are processed in order.
    assert calls[2]["offset"] == 12
    assert channel._queue.qsize() == 1
    assert channel._queue.get_nowait().metadata["update_id"] == 11


@pytest.mark.asyncio
async def test_messages_and_unsupported_updates_keep_acknowledgement() -> None:
    """Handled messages and deliberately ignored updates still advance the offset."""
    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    batch = [_message_update(10), _unsupported_update(11)]
    calls: list[dict[str, Any]] = []
    _install_api(channel, [batch], calls)

    await _run_poll_loop(channel)

    assert channel._queue.qsize() == 1
    assert calls[1]["offset"] == 12


@pytest.mark.asyncio
async def test_failed_callback_with_malformed_update_id_does_not_stall() -> None:
    """A callback failure on an update without an int update_id logs and moves on."""
    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    batch = [_callback_update(10), {"callback_query": {"id": "cb_bad", "data": "deny:x"}}]
    calls: list[dict[str, Any]] = []
    _install_api(channel, [batch], calls)
    handled = _install_handler(channel, fail_ids=frozenset({"cb_bad"}))

    await _run_poll_loop(channel)

    assert len(handled) == 2
    # The well-formed update before the failure still advances the offset.
    assert calls[1]["offset"] == 11
