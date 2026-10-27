"""Mid-turn steering: messages the user sends while a turn is running.

A message sent in ``steer`` mode while the session's turn runs does not stop
that turn and does not wait for it to end. It waits in the session's queue
and the running turn takes it at its next checkpoint:

* **Between tool calls of an ordered sequence.** Once a call of the batch has
  started, an ``exclusive`` call (or any call ordered behind one) that has
  not started yet is not launched while a message waits; it gets a synthetic
  "skipped" result instead (:func:`skipped_for_steering`). Calls of a shared
  (``parallel`` / ``keyed``) group that the batch already admitted together
  run to completion.
* **After a tool batch.** The message is appended as a user message after the
  tool results, before the next model call. A turn that would have ended
  there (the tool asked to end it) continues instead.
* **When the model answers without tool calls.** The turn continues with the
  message rather than ending.

One message is taken per checkpoint. Taking it writes the user row to the
transcript, so the row lands at the point the model meets it, inside the
turn, and the turn's structured record names the row
(:attr:`agentos.engine.transcript.TranscriptRecord.steered_entry_ids`) so a
reload does not replay it twice. A message no checkpoint takes before the turn
ends is not lost: it is still a queued task and runs as the next turn.

The engine only sees the :class:`SteeringSource` protocol. The gateway binds
one per running task (:func:`bind_steering`) for the duration of the turn;
the agent of that session's turn picks it up with :func:`steering_for`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Protocol

from agentos.engine.types import SteeringEvent, ToolCall, ToolResult
from agentos.execution_status import runtime_execution_status
from agentos.provider.types import Message
from agentos.session.keys import canonicalize_session_key

#: Content of the synthetic result a skipped tool call gets.
SKIPPED_FOR_STEERING_CONTENT = (
    "Skipped: the user sent a new message before this call started, so it did "
    "not run. Read the new message, then decide whether the call is still needed."
)
#: ``execution_status.reason`` of a skipped tool call.
SKIPPED_FOR_STEERING_REASON = "skipped_for_steering"


@dataclass(frozen=True)
class SteeringMessage:
    """A queued message the running turn took, already in the transcript."""

    text: str
    message_id: str | None = None
    task_id: str | None = None


class SteeringSource(Protocol):
    """The running turn's view of its session's steering queue."""

    @property
    def session_key(self) -> str: ...

    def has_pending(self) -> bool:
        """True when a message is waiting to be taken (cheap, no I/O)."""
        ...

    async def take(self) -> SteeringMessage | None:
        """Take the oldest waiting message and write it to the transcript.

        ``None`` when nothing is waiting any more (withdrawn meanwhile) or the
        write failed; a message whose write failed stays queued in its place.
        """
        ...


_current: ContextVar[SteeringSource | None] = ContextVar("agentos_steering_source", default=None)


@contextmanager
def bind_steering(source: SteeringSource | None) -> Iterator[None]:
    """Make *source* the steering queue of the turn run inside this block."""

    token = _current.set(source)
    try:
        yield
    finally:
        _current.reset(token)


def steering_for(session_key: str | None) -> SteeringSource | None:
    """The bound steering source, if it belongs to *session_key*.

    A turn of another session started from inside this one (a subagent run
    in-process) inherits the context but must not take this session's
    messages. A source with an ``attach()`` method is told that a turn loop
    now checks it, which is when the gateway starts routing ``steer`` sends
    to it.
    """

    source = _current.get()
    if source is None or not session_key:
        return None
    try:
        if canonicalize_session_key(session_key) != canonicalize_session_key(source.session_key):
            return None
    except Exception:  # noqa: BLE001 - a malformed key never steals messages
        return None
    attach = getattr(source, "attach", None)
    if callable(attach):
        attach()
    return source


def steering_pending(source: SteeringSource | None) -> bool:
    if source is None:
        return False
    try:
        return bool(source.has_pending())
    except Exception:  # noqa: BLE001 - a broken probe must not break the tool batch
        return False


async def take_steering(
    source: SteeringSource | None,
    turn_messages: list[Message],
    steered_entry_ids: list[str],
) -> SteeringEvent | None:
    """Checkpoint: append one waiting message to *turn_messages*.

    Returns the event announcing it, or ``None`` when nothing was taken. The
    message's transcript row id is recorded in *steered_entry_ids*.
    """

    if not steering_pending(source):
        return None
    assert source is not None
    taken = await source.take()
    if taken is None or not taken.text:
        return None
    turn_messages.append(Message(role="user", content=taken.text))
    if taken.message_id:
        steered_entry_ids.append(taken.message_id)
    return SteeringEvent(text=taken.text, message_id=taken.message_id, task_id=taken.task_id)


class BatchSteeringCheckpoint:
    """Decide, call by call, whether a tool call may still start.

    Built per tool batch with the batch's per-call ``exclusive`` flags. A call
    is cut (gets :func:`skipped_for_steering`) when a message waits, at least
    one call of the batch has started, and the call is part of an ordered
    sequence: it is ``exclusive``, or it is a shared call behind an exclusive
    one that opens a new shared group (no shared call of that group started
    yet). Shared calls of a group already running are never cut.
    """

    def __init__(
        self,
        pending: Callable[[], bool] | None,
        exclusive: Sequence[bool],
    ) -> None:
        self._pending = pending
        self._exclusive = list(exclusive)
        self._first_exclusive = next(
            (index for index, flag in enumerate(self._exclusive) if flag), None
        )
        self._started = 0
        self._last_started_exclusive = False

    def enter(self, index: int) -> bool:
        """True when call *index* may run (it now counts as started)."""

        if self._cut(index):
            return False
        self._started += 1
        self._last_started_exclusive = self._exclusive[index]
        return True

    def _cut(self, index: int) -> bool:
        if self._pending is None or self._started == 0:
            return False
        if not self._exclusive[index]:
            ordered = (
                self._first_exclusive is not None
                and index > self._first_exclusive
                and self._last_started_exclusive
            )
            if not ordered:
                return False
        try:
            return bool(self._pending())
        except Exception:  # noqa: BLE001 - a broken probe must not lose the call
            return False


def skipped_for_steering(call: ToolCall) -> ToolResult:
    """The synthetic result of a call not started because a message arrived."""

    return ToolResult(
        tool_use_id=call.tool_use_id,
        tool_name=call.tool_name,
        content=SKIPPED_FOR_STEERING_CONTENT,
        is_error=True,
        execution_status=runtime_execution_status("cancelled", reason=SKIPPED_FOR_STEERING_REASON),
    )


def is_skipped_for_steering(result: ToolResult) -> bool:
    status = result.execution_status
    return isinstance(status, dict) and status.get("reason") == SKIPPED_FOR_STEERING_REASON
