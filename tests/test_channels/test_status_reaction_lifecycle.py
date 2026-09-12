"""Status-reaction lifecycle on the failure path.

Regression coverage for #1560: ``failed()`` appended its token through
``_add_state`` and nothing ever reclaimed it, because ``completed()`` was the
only method that popped ``_active``. On the ``TaskQueueFullError`` path --
``received(msg)`` then ``failed(msg)``, then return -- the message was left
carrying a contradictory ✅/❌ pair forever, and the ``_active`` entry leaked
once per rejected message for the adapter's lifetime.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agentos.channels._reactions import SlackStatusReactor, _BaseStatusReactor
from agentos.channels.types import IncomingMessage


class _RecordingReactor(_BaseStatusReactor):
    def __init__(self) -> None:
        super().__init__("test", _SilentLog())
        self.added: list[str] = []
        self.removed: list[str] = []

    async def _add(self, message: IncomingMessage, state: str) -> Any:
        self.added.append(state)
        return state

    async def _remove(self, token: Any) -> None:
        self.removed.append(str(token))


class _SilentLog:
    def warning(self, *args: Any, **kwargs: Any) -> None:
        return None


def _message(ts: str = "1700000000.000100") -> IncomingMessage:
    return IncomingMessage(
        sender_id="U1",
        channel_id="C1",
        content="hi",
        metadata={"ts": ts},
    )


@pytest.mark.asyncio
async def test_failed_clears_the_progress_marks_and_keeps_the_failure_mark() -> None:
    reactor = _RecordingReactor()
    message = _message()

    await reactor.received(message)
    await reactor.running(message)
    await reactor.failed(message)

    assert reactor.added == ["received", "running", "failed"]
    # The ✅/👀 pair is cleared; ❌ stays as the outcome marker.
    assert reactor.removed == ["received", "running"]


@pytest.mark.asyncio
async def test_failed_does_not_leave_a_tracked_entry_behind() -> None:
    reactor = _RecordingReactor()
    message = _message()

    await reactor.received(message)
    await reactor.failed(message)

    assert not reactor._active


@pytest.mark.asyncio
async def test_repeated_rejections_do_not_accumulate_entries() -> None:
    reactor = _RecordingReactor()

    for index in range(5):
        message = _message(ts=f"1700000000.00{index:04d}")
        await reactor.received(message)
        await reactor.failed(message)

    assert not reactor._active


@pytest.mark.asyncio
async def test_completed_after_failed_does_not_remove_the_failure_mark() -> None:
    reactor = _RecordingReactor()
    message = _message()

    await reactor.received(message)
    await reactor.failed(message)
    await reactor.completed(message)

    assert reactor.removed == ["received"]


@pytest.mark.asyncio
async def test_completed_still_clears_every_tracked_mark() -> None:
    reactor = _RecordingReactor()
    message = _message()

    await reactor.received(message)
    await reactor.running(message)
    await reactor.completed(message)

    assert reactor.removed == ["received", "running"]
    assert not reactor._active


@pytest.mark.asyncio
async def test_failed_is_skipped_once_reactions_are_disabled() -> None:
    reactor = _RecordingReactor()
    message = _message()
    await reactor.received(message)
    reactor._disable("test")

    await reactor.failed(message)

    # The tracked mark is still reclaimed, but no new reaction is attempted.
    assert reactor.removed == ["received"]
    assert reactor.added == ["received"]


@pytest.mark.asyncio
async def test_slack_status_reactor_ignores_already_reacted_and_no_reaction() -> None:
    mock_client = MagicMock()
    mock_channel = MagicMock()
    mock_channel._get_client.return_value = mock_client

    # 1. already_reacted on /reactions.add
    resp_add = MagicMock()
    resp_add.status_code = 200
    resp_add.raise_for_status.return_value = None
    resp_add.json.return_value = {"ok": False, "error": "already_reacted"}

    # 2. no_reaction on /reactions.remove
    resp_remove = MagicMock()
    resp_remove.status_code = 200
    resp_remove.raise_for_status.return_value = None
    resp_remove.json.return_value = {"ok": False, "error": "no_reaction"}

    mock_client.post = AsyncMock(side_effect=[resp_add, resp_remove])

    reactor = SlackStatusReactor(mock_channel, _SilentLog())
    message = _message()

    # received() adds reaction; already_reacted should succeed cleanly without disabling
    await reactor.received(message)
    assert not reactor._disabled
    assert len(reactor._active[reactor._message_key(message)]) == 1

    # completed() removes reaction; no_reaction should succeed cleanly without disabling
    await reactor.completed(message)
    assert not reactor._disabled
    assert not reactor._active.get(reactor._message_key(message))


@pytest.mark.asyncio
async def test_slack_status_reactor_disables_on_fatal_api_error() -> None:
    mock_client = MagicMock()
    mock_channel = MagicMock()
    mock_channel._get_client.return_value = mock_client

    resp_error = MagicMock()
    resp_error.status_code = 200
    resp_error.raise_for_status.return_value = None
    resp_error.json.return_value = {"ok": False, "error": "fatal_error"}

    mock_client.post = AsyncMock(return_value=resp_error)

    reactor = SlackStatusReactor(mock_channel, _SilentLog())
    message = _message()

    await reactor.received(message)
    assert reactor._disabled

