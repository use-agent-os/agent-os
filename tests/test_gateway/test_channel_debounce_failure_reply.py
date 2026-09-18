"""A debounced batch that fails to start must tell the user something (#1206).

``_dispatch_combined_message_after_debounce`` replies on the channel only when
the enqueue raised ``TaskQueueFullError``. Every other failure — a session
store that is down, a provider that blew up while the turn was being started —
logged and returned, so the coalesced messages the user sent vanished with no
reply at all: accepted, then silence.

The reply itself has to be best-effort. It is sent from inside the ``except``
block that handles the failure, and a channel that just failed a turn is
exactly the condition where the notice send fails too; a raise there would
replace the original failure with a second one and skip the caller's cleanup.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
import structlog

from agentos.channels.types import IncomingMessage, OutgoingMessage
from agentos.gateway._debounce import _DefaultDebounceCoordinator
from agentos.gateway.channel_dispatch import (
    _ChannelInFlightSet,
    _dispatch_combined_message_after_debounce,
)
from agentos.gateway.task_runtime import TaskQueueFullError

_SESSION_KEY = "agent:main:telegram:direct:u1"
_SECRET = "my bank pin is 4242"


class _FakeChannel:
    """Records what reached the user; optionally fails every send."""

    def __init__(self, *, send_raises: bool = False) -> None:
        self.sent: list[OutgoingMessage] = []
        self._send_raises = send_raises

    async def send(self, message: OutgoingMessage) -> None:
        self.sent.append(message)
        if self._send_raises:
            raise RuntimeError("channel transport is down")


class _FakeSessionManager:
    async def get_or_create(self, key: str, **kwargs: Any) -> tuple[Any, bool]:
        return SimpleNamespace(session_key=key, **kwargs), True

    async def update(self, key: str, **kwargs: Any) -> None:
        return None

    async def append_message(self, key: str, role: str, content: str) -> Any:
        return SimpleNamespace(content=content)

    async def read_transcript(self, key: str) -> list[Any]:
        return []


class _FakeTurnRunner:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    def _get_session_lock(self, key: str) -> asyncio.Lock:
        return self._lock


class _FailingTaskRuntime:
    """Raises from ``enqueue`` the way a broken backend does mid-startup."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def enqueue(self, envelope: Any, message: str, **kwargs: Any) -> Any:
        raise self._exc


def _message(content: str = _SECRET) -> IncomingMessage:
    return IncomingMessage(sender_id="u1", channel_id="c1", content=content)


def _combined(content: str = _SECRET) -> SimpleNamespace:
    msg = _message(content)
    return SimpleNamespace(
        content=content,
        attachments=[],
        message=msg,
        raw_content=content,
        coalesced_count=3,
    )


async def _dispatch(
    channel: _FakeChannel,
    exc: BaseException,
    *,
    in_flight: _ChannelInFlightSet | None = None,
) -> None:
    await _dispatch_combined_message_after_debounce(
        channel,
        _combined(),
        _FakeTurnRunner(),
        _FakeSessionManager(),
        _SESSION_KEY,
        "telegram",
        _FailingTaskRuntime(exc),
        None,
        None,
        in_flight,
    )


def _texts(channel: _FakeChannel) -> list[str]:
    return [str(getattr(m, "content", "")) for m in channel.sent]


# ── The reported half: an unexpected failure said nothing at all ─────────────


@pytest.mark.asyncio
async def test_unexpected_failure_replies_on_the_channel() -> None:
    """Fails without the fix: the non-queue-full branch sent nothing."""
    channel = _FakeChannel()

    await _dispatch(channel, RuntimeError("session store unavailable"))

    assert len(channel.sent) == 1
    assert "couldn't be processed" in _texts(channel)[0]


@pytest.mark.asyncio
async def test_unexpected_failure_reply_does_not_echo_the_user_text() -> None:
    """The notice is generic: a failing turn must not bounce content back."""
    channel = _FakeChannel()

    await _dispatch(channel, RuntimeError("session store unavailable"))

    assert _SECRET not in _texts(channel)[0]


@pytest.mark.asyncio
async def test_unexpected_failure_logs_the_session_key() -> None:
    """Fails without the fix: the log carried no session_key and no error."""
    channel = _FakeChannel()

    with structlog.testing.capture_logs() as captured:
        await _dispatch(channel, RuntimeError("session store unavailable"))

    failures = [e for e in captured if e["event"] == "channel_dispatch.debounce_enqueue_failed"]
    assert len(failures) == 1
    assert failures[0]["session_key"] == _SESSION_KEY
    assert failures[0]["reason"] == "unexpected"
    assert "session store unavailable" in failures[0]["error"]


@pytest.mark.asyncio
async def test_failure_log_does_not_carry_the_message_content() -> None:
    """Structured telemetry names the session, never what was said in it."""
    channel = _FakeChannel()

    with structlog.testing.capture_logs() as captured:
        await _dispatch(channel, RuntimeError("session store unavailable"))

    assert not any(_SECRET in str(value) for event in captured for value in event.values())


# ── The best-effort half: the notice must not raise out of the handler ──────


@pytest.mark.asyncio
async def test_a_failing_reply_send_does_not_escape_the_handler() -> None:
    """Fails without the fix: the unwrapped send raised out of ``except``."""
    channel = _FakeChannel(send_raises=True)

    await _dispatch(channel, RuntimeError("session store unavailable"))

    assert len(channel.sent) == 1


@pytest.mark.asyncio
async def test_a_failing_reply_send_is_logged() -> None:
    channel = _FakeChannel(send_raises=True)

    with structlog.testing.capture_logs() as captured:
        await _dispatch(channel, RuntimeError("session store unavailable"))

    replies = [e for e in captured if e["event"] == "channel_dispatch.debounce_error_reply_failed"]
    assert len(replies) == 1
    assert replies[0]["session_key"] == _SESSION_KEY
    assert replies[0]["reason"] == "unexpected"


@pytest.mark.asyncio
async def test_a_failing_queue_full_reply_does_not_escape_the_handler() -> None:
    """The queue-full branch had the same unwrapped send (#1206).

    Its notice reached the user before the fix, but only when the channel was
    healthy; a channel that was refusing sends turned the handled queue-full
    case into an unhandled exception out of the dispatch coroutine.
    """
    channel = _FakeChannel(send_raises=True)

    with structlog.testing.capture_logs() as captured:
        await _dispatch(channel, TaskQueueFullError(session_key=_SESSION_KEY, max_pending=1))

    replies = [e for e in captured if e["event"] == "channel_dispatch.debounce_error_reply_failed"]
    assert len(replies) == 1
    assert replies[0]["reason"] == "queue_full"


@pytest.mark.asyncio
async def test_queue_full_still_replies() -> None:
    """Guard: passes either way by design — the queue-full notice predates
    this fix and must survive being routed through the best-effort helper."""
    channel = _FakeChannel()

    await _dispatch(channel, TaskQueueFullError(session_key=_SESSION_KEY, max_pending=1))

    assert "queue is full" in _texts(channel)[0]


@pytest.mark.asyncio
async def test_reservation_is_released_even_when_the_reply_send_fails() -> None:
    """A notice that raised past the handler would strand the slot it held."""
    channel = _FakeChannel(send_raises=True)
    in_flight = _ChannelInFlightSet(cap=1)

    await _dispatch(channel, RuntimeError("session store unavailable"), in_flight=in_flight)

    assert in_flight.full() is False


# ── The coordinator's own swallow-all handler ───────────────────────────────


@pytest.mark.asyncio
async def test_deliver_names_the_session_whose_batch_was_lost() -> None:
    """Fails without the fix: ``_deliver`` logged ``reason`` and nothing else."""
    coordinator = _DefaultDebounceCoordinator()

    async def _on_fire(combined: Any) -> None:
        raise RuntimeError("dispatch exploded")

    with structlog.testing.capture_logs() as captured:
        await coordinator._deliver(_SESSION_KEY, [_message()], _on_fire)

    failures = [e for e in captured if e["event"] == "channel_dispatch.debounce_enqueue_failed"]
    assert len(failures) == 1
    assert failures[0]["session_key"] == _SESSION_KEY
    assert "dispatch exploded" in failures[0]["error"]


@pytest.mark.asyncio
async def test_deliver_still_swallows_the_failure() -> None:
    """Guard: passes either way by design — a failed batch must not take the
    coordinator down with it, so the added telemetry must stay inside the
    existing ``except``."""
    coordinator = _DefaultDebounceCoordinator()

    async def _on_fire(combined: Any) -> None:
        raise RuntimeError("dispatch exploded")

    await coordinator._deliver(_SESSION_KEY, [_message()], _on_fire)
