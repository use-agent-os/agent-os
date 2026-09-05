"""Telegram polling acknowledges updates only after local handling succeeds."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from structlog.testing import capture_logs

from agentos.channel_pairing import ChannelAdmission
from agentos.channels.telegram import TelegramChannel, TelegramChannelConfig
from agentos.gateway.approval_queue import ApprovalQueue


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


async def test_permanent_callback_failure_is_bounded_and_next_update_gets_its_own_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="test-token", poll_idle_sleep_s=0.25))
    channel._update_offset = 10
    updates = [
        {"update_id": 10, "callback_query": {"id": "bad"}},
        {"update_id": 11, "callback_query": {"id": "recoverable"}},
        _message_update(12),
    ]
    offsets: list[int] = []
    attempts = {"bad": 0, "recoverable": 0}

    async def api(method: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        assert method == "getUpdates"
        offsets.append(payload["offset"])
        if len(offsets) > 5:
            raise asyncio.CancelledError
        return [update for update in updates if update["update_id"] >= payload["offset"]]

    async def handle_callback(callback: dict[str, Any]) -> None:
        key = callback["id"]
        attempts[key] += 1
        if key == "bad" or attempts[key] < 3:
            raise RuntimeError("callback dependency failed")

    sleep = AsyncMock()
    monkeypatch.setattr("agentos.channels.telegram.asyncio.sleep", sleep)
    monkeypatch.setattr(channel, "_api", api)
    monkeypatch.setattr(channel, "_handle_telegram_callback", handle_callback)

    with capture_logs() as logs, pytest.raises(asyncio.CancelledError):
        await channel._poll_loop()

    assert attempts == {"bad": 3, "recoverable": 3}
    assert offsets == [10, 10, 10, 11, 11, 13]
    assert sleep.await_count == 4
    assert channel._queue.get_nowait().content == "after callback"
    assert channel._queue.empty()
    errors = [entry for entry in logs if entry["log_level"] == "error"]
    assert len(errors) == 1
    assert errors[0]["update_id"] == 10
    assert errors[0]["attempts"] == 3


@pytest.mark.parametrize("fail_first", [False, True])
async def test_repeated_callback_resolves_and_delivers_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fail_first: bool
) -> None:
    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))
    approval_id = queue.request("exec", {"sessionKey": "agent:main:telegram:dm:7"})
    channel = TelegramChannel(TelegramChannelConfig(token="test-token"))
    monkeypatch.setattr("agentos.gateway.approval_queue.get_approval_queue", lambda: queue)
    monkeypatch.setattr(
        channel.pairing_store,
        "admission",
        lambda *_a: ChannelAdmission("telegram", "7", "grant", 1),
    )
    resolve = Mock(wraps=queue.resolve)
    monkeypatch.setattr(queue, "resolve", resolve)
    callback = {
        "id": "callback-10",
        "from": {"id": 7},
        "message": {"message_id": 42, "chat": {"id": 7, "type": "private"}, "text": "Allow?"},
        "data": f"approve:{approval_id}",
    }
    update = {"update_id": 10, "callback_query": callback}
    batches = [[update], [update, {"update_id": 11, "callback_query": callback}]]
    if fail_first:
        batches.insert(0, [update])
    poll_payloads: list[dict[str, Any]] = []
    callback_api_calls: list[str] = []

    async def api(method: str, payload: dict[str, Any]) -> Any:
        if method == "getUpdates":
            poll_payloads.append(dict(payload))
            if not batches:
                raise asyncio.CancelledError
            return batches.pop(0)
        callback_api_calls.append(method)
        return True

    get = queue.get
    get_attempts = 0

    def transient_get(key: str) -> Any:
        nonlocal get_attempts
        get_attempts += 1
        if fail_first and get_attempts == 1:
            raise RuntimeError("temporary approval storage failure")
        return get(key)

    monkeypatch.setattr(queue, "get", transient_get)
    monkeypatch.setattr(channel, "_api", api)
    monkeypatch.setattr("agentos.channels.telegram.asyncio.sleep", AsyncMock())
    try:
        with pytest.raises(asyncio.CancelledError):
            await channel._poll_loop()

        resolve.assert_called_once_with(approval_id, True)
        assert callback_api_calls == ["answerCallbackQuery", "editMessageText"]
        assert queue.get(approval_id).approved is True
        assert channel._queue.get_nowait().content == "Approve"
        assert channel._queue.empty()
        assert poll_payloads[-1]["offset"] == 12
        if fail_first:
            assert "offset" not in poll_payloads[1]
    finally:
        queue._conn.close()
