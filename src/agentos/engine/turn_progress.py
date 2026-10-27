"""Durable progress of a running turn: the in-progress transcript row.

A turn's assistant side used to reach the transcript only when the turn
ended, so a crash or kill mid-turn lost every tool call that had already run
(a trade, a sent message, a file write). Now a running turn keeps one
in-progress assistant row up to date:

* after the model issues a round of tool calls, before they run
  (phase :data:`PHASE_TOOLS_RUNNING`), and
* after all of that round's results are in (phase :data:`PHASE_ROUND_COMPLETE`).

Each write replaces the previous one atomically (same ``message_id``), and the
turn's final row replaces it in turn, so a finished turn leaves exactly the
one row it always did. The in-progress row is a regular assistant row:

* ``provider_messages`` holds the turn's structured record so far (the same
  shape :mod:`agentos.engine.transcript` stores for a finished turn) plus a
  :data:`TURN_PROGRESS_KEY` marker: ``{"state": "running", "round": N,
  "phase": ...}``. History loads replay it like any record; a call without a
  result gets an unknown-outcome result (``repair_tool_pairing``).
* ``tool_calls`` holds the flat display segments so far, so ``chat.history``
  shows what ran once the turn is no longer live.
* ``content`` is empty and there is no usage: code that reads "the last
  reply" skips it, and usage is rolled up once, by the final row.

From the transcript alone, :func:`classify_transcript_tail` tells a finished
turn from one interrupted after round N (and which calls of round N + 1 were
issued without a recorded result).
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Final, Literal

import structlog

log = structlog.get_logger(__name__)

TURN_PROGRESS_KEY = "turn_progress"
TURN_PROGRESS_STATE_RUNNING = "running"
TurnProgressPhase = Literal["tools_running", "round_complete"]
PHASE_TOOLS_RUNNING: Final = "tools_running"
PHASE_ROUND_COMPLETE: Final = "round_complete"


@dataclass(frozen=True)
class TurnProgress:
    """One progress report from the agent loop.

    *record* is the encoded structured record of the turn so far
    (``encode_transcript_record``); *round* counts the tool rounds whose
    calls have been issued in this turn.
    """

    record: dict[str, Any]
    round: int
    phase: TurnProgressPhase


TurnProgressSink = Callable[[TurnProgress], Awaitable[None]]


def _decode_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None
    return value


def turn_progress_marker(entry: Any) -> dict[str, Any] | None:
    """The progress marker of an in-progress row, or ``None`` for any other row."""
    if getattr(entry, "role", None) != "assistant":
        return None
    record = _decode_json(getattr(entry, "provider_messages", None))
    if not isinstance(record, dict):
        return None
    marker = record.get(TURN_PROGRESS_KEY)
    if not isinstance(marker, dict):
        return None
    if marker.get("state") != TURN_PROGRESS_STATE_RUNNING:
        return None
    return marker


def is_turn_progress_entry(entry: Any) -> bool:
    return turn_progress_marker(entry) is not None


def _unanswered_tool_use_ids(record: Any) -> list[str]:
    """Tool calls in *record* that have no result after them, in call order."""
    if not isinstance(record, dict):
        return []
    messages = record.get("messages")
    if not isinstance(messages, list):
        return []
    calls: list[str] = []
    answered: set[str] = set()
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                calls.append(block["id"])
            elif block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str):
                answered.add(block["tool_use_id"])
    return [call_id for call_id in dict.fromkeys(calls) if call_id not in answered]


# ---------------------------------------------------------------------------
# Boot-time classification
# ---------------------------------------------------------------------------

TurnTailStatus = Literal["empty", "finished", "unanswered_input", "interrupted"]


@dataclass(frozen=True)
class TurnTail:
    """What the end of a transcript says about its last turn.

    * ``empty`` -- no user or assistant rows.
    * ``finished`` -- the last turn ended: its final assistant row, or a
      system row (an error) written when it stopped, is the newest row.
    * ``unanswered_input`` -- user row(s) after the last finished turn and no
      progress was recorded for them: the turn never got as far as a tool
      round (or never started).
    * ``interrupted`` -- the newest assistant row is an in-progress row: the
      turn stopped after ``rounds_completed`` complete tool rounds. When
      ``pending_tool_use_ids`` is not empty, the calls of the next round were
      issued and their outcome is unknown (they may have run).

    ``trailing_user_entries`` are user rows the deciding row's turn does not
    replay itself (follow-ups queued while the turn ran, or the unanswered
    input itself), oldest first; rows steered into the turn are not among
    them.
    """

    status: TurnTailStatus
    rounds_completed: int = 0
    pending_tool_use_ids: tuple[str, ...] = ()
    progress_entry: Any = None
    input_entry_id: str | None = None
    trailing_user_entries: tuple[Any, ...] = field(default_factory=tuple)


def _record_covered_ids(record: Any) -> set[str]:
    """User rows a turn record replays itself: its input and steered rows."""
    if not isinstance(record, dict):
        return set()
    covered: set[str] = set()
    input_entry_id = record.get("input_entry_id")
    if isinstance(input_entry_id, str) and input_entry_id:
        covered.add(input_entry_id)
    steered = record.get("steered_entry_ids")
    if isinstance(steered, list):
        covered.update(item for item in steered if isinstance(item, str) and item)
    return covered


def classify_transcript_tail(entries: Sequence[Any]) -> TurnTail:
    """Classify the last turn of a transcript (rows oldest first).

    The deciding row is the newest assistant or system row. User rows its
    turn record replays itself (the turn's input, rows steered into it) are
    part of that turn, never trailing input, wherever they sort: each
    progress write replaces the in-progress row at the end of the
    transcript, so a row steered in (or a follow-up queued) mid-turn ends up
    *before* the newest in-progress row. For an interrupted turn, the
    trailing rows are therefore the user rows after its input row the record
    does not name, on either side of the in-progress row. A steered row the
    in-progress row does not name yet (the process died between writing the
    row and the next progress write) is trailing: the model never met it on
    record, and a history load replays it after the turn, where it belongs.
    """
    deciding_index: int | None = None
    for index in range(len(entries) - 1, -1, -1):
        if getattr(entries[index], "role", None) in ("assistant", "system"):
            deciding_index = index
            break
    if deciding_index is None:
        users = [entry for entry in entries if getattr(entry, "role", None) == "user"]
        return _finished_or_unanswered(users, empty_if_none=True)

    deciding = entries[deciding_index]
    record = None
    if getattr(deciding, "role", None) == "assistant":
        record = _decode_json(getattr(deciding, "provider_messages", None))
    covered = _record_covered_ids(record)

    def _trailing(rows: Sequence[Any]) -> list[Any]:
        return [
            entry
            for entry in rows
            if getattr(entry, "role", None) == "user"
            and getattr(entry, "message_id", None) not in covered
        ]

    after = _trailing(entries[deciding_index + 1 :])
    marker = turn_progress_marker(deciding)
    if marker is None:
        return _finished_or_unanswered(after)

    input_entry_id = record.get("input_entry_id") if isinstance(record, dict) else None
    before: list[Any] = []
    if isinstance(input_entry_id, str) and input_entry_id:
        for index in range(deciding_index - 1, -1, -1):
            if getattr(entries[index], "message_id", None) == input_entry_id:
                before = _trailing(entries[index + 1 : deciding_index])
                break
    raw_round = marker.get("round")
    round_index = raw_round if isinstance(raw_round, int) and raw_round > 0 else 0
    pending = tuple(_unanswered_tool_use_ids(record))
    completed = round_index if not pending else max(0, round_index - 1)
    return TurnTail(
        status="interrupted",
        rounds_completed=completed,
        pending_tool_use_ids=pending,
        progress_entry=deciding,
        input_entry_id=input_entry_id if isinstance(input_entry_id, str) else None,
        trailing_user_entries=tuple(before + after),
    )


def _finished_or_unanswered(trailing_users: list[Any], *, empty_if_none: bool = False) -> TurnTail:
    if trailing_users:
        return TurnTail(status="unanswered_input", trailing_user_entries=tuple(trailing_users))
    return TurnTail(status="empty" if empty_if_none else "finished")


# ---------------------------------------------------------------------------
# Why a turn is being stopped
# ---------------------------------------------------------------------------


@dataclass
class TurnStopSignal:
    """Shared by a running turn and whoever runs it: why it is being stopped.

    The task runtime binds one per turn (:func:`bind_turn_stop_signal`) and
    sets :attr:`shutdown` before the gateway's own shutdown cancels the turn.
    A mutable holder in a context variable, so tasks the turn spawns (stream
    drivers) see the flag too.
    """

    shutdown: bool = False


_TURN_STOP_SIGNAL: ContextVar[TurnStopSignal | None] = ContextVar(
    "agentos_turn_stop_signal", default=None
)


def bind_turn_stop_signal(signal: TurnStopSignal) -> None:
    """Bind *signal* to the current task's context (and tasks it creates)."""
    _TURN_STOP_SIGNAL.set(signal)


def stopped_by_shutdown() -> bool:
    """True when the running turn is being cancelled by a gateway shutdown.

    Such a turn keeps its in-progress row, so restart recovery can tell from
    the transcript how far it got.
    """
    signal = _TURN_STOP_SIGNAL.get()
    return signal is not None and signal.shutdown


# ---------------------------------------------------------------------------
# Display filter
# ---------------------------------------------------------------------------


def hide_live_turn_progress(entries: list[Any], in_flight_run: Any) -> list[Any]:
    """Leave out the in-progress row of the turn that is still running.

    While a turn runs, a viewer renders it from the live stream
    (``in_flight_run``); listing its in-progress row as well would show its
    tool calls twice. Rows of an earlier, interrupted turn (written before the
    live run started) stay visible.

    A live row with a message steered into it keeps its part up to the newest
    steer marker (``agentos.engine.steering.STEER_SEGMENT_TYPE``): that work
    is settled, the live stream restarts at the steer, and the steered user
    row shows between the two.
    """
    if not isinstance(in_flight_run, dict) or not entries:
        return entries
    started_at = in_flight_run.get("started_at")
    started = int(started_at) if isinstance(started_at, (int, float)) else None
    kept: list[Any] = []
    changed = False
    for entry in entries:
        if is_turn_progress_entry(entry):
            created_at = getattr(entry, "created_at", None)
            if started is None or not isinstance(created_at, int) or created_at >= started:
                changed = True
                settled = _settled_before_last_steer(entry)
                if settled is not None:
                    kept.append(settled)
                continue
        kept.append(entry)
    return kept if changed else entries


def _settled_before_last_steer(entry: Any) -> Any:
    """A copy of a live in-progress row cut after its newest steer marker."""
    from agentos.engine.steering import is_steer_segment

    segments = getattr(entry, "tool_calls", None)
    if not isinstance(segments, list):
        return None
    last = max((i for i, segment in enumerate(segments) if is_steer_segment(segment)), default=-1)
    copy = getattr(entry, "model_copy", None)
    if last < 0 or not callable(copy):
        return None
    return copy(update={"tool_calls": segments[: last + 1]})


# ---------------------------------------------------------------------------
# Writer (runtime side)
# ---------------------------------------------------------------------------


class TurnProgressWriter:
    """Keeps a running turn's in-progress row current.

    One writer per turn. ``message_id`` names the row once a write was
    attempted; the turn's final (or cancelled) row passes it as
    ``replaces_message_id`` so the in-progress row is swapped out atomically.
    Writes are best-effort: a failure is logged and the turn carries on, as
    it did before rows were written mid-turn.
    """

    def __init__(
        self,
        *,
        session_key: str,
        session_manager: Any,
        segments: list[dict[str, Any]],
        write_context: Callable[[str], AbstractAsyncContextManager[None]],
        tool_use_input: Callable[[str, str, dict[str, Any]], Any] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._session_key = session_key
        self._session_manager = session_manager
        self._segments = segments
        self._write_context = write_context
        self._tool_use_input = tool_use_input
        self._clock = clock
        self.message_id: str | None = None
        self.writes = 0

    @staticmethod
    def supported(session_manager: Any) -> bool:
        """Whether *session_manager* can store and later replace the row."""
        if session_manager is None:
            return False
        if not callable(getattr(session_manager, "write_turn_progress", None)):
            return False
        from agentos.compat.inspect_utils import accepts_keyword_arg

        return accepts_keyword_arg(session_manager.append_message, "replaces_message_id")

    def _display_segments(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        segments = [dict(segment) for segment in self._segments if isinstance(segment, dict)]
        if self._tool_use_input is None:
            return segments
        # A call issued but not yet answered has no input on its segment (it
        # is filled when the result arrives); take it from the record so a
        # reader of an interrupted turn sees what was attempted.
        arguments_by_id: dict[str, tuple[str, dict[str, Any]]] = {}
        for message in record.get("messages") or []:
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and isinstance(block.get("id"), str)
                    and isinstance(block.get("input"), dict)
                ):
                    arguments_by_id[block["id"]] = (str(block.get("name") or ""), block["input"])
        for segment in segments:
            if segment.get("type") != "tool_use" or segment.get("input") not in ("", None):
                continue
            found = arguments_by_id.get(str(segment.get("tool_use_id") or ""))
            if found is None:
                continue
            name, arguments = found
            try:
                segment["input"] = self._tool_use_input(
                    name or str(segment.get("name") or ""),
                    str(segment.get("tool_use_id")),
                    arguments,
                )
            except Exception:  # noqa: BLE001 - display detail only
                continue
        return segments

    async def record(self, progress: TurnProgress) -> None:
        """Agent sink: persist *progress* as the turn's in-progress row."""
        if self.message_id is None:
            self.message_id = f"turn-progress-{uuid.uuid4().hex}"
        provider_messages = {
            **progress.record,
            TURN_PROGRESS_KEY: {
                "state": TURN_PROGRESS_STATE_RUNNING,
                "round": progress.round,
                "phase": progress.phase,
                "updated_at": int(self._clock() * 1000),
            },
        }
        try:
            async with self._write_context(self._session_key):
                await self._session_manager.write_turn_progress(
                    self._session_key,
                    message_id=self.message_id,
                    tool_calls=self._display_segments(progress.record) or None,
                    provider_messages=provider_messages,
                )
            self.writes += 1
        except Exception as exc:  # noqa: BLE001 - progress is best-effort
            log.warning(
                "turn_progress.write_failed",
                session_key=self._session_key,
                round=progress.round,
                phase=progress.phase,
                error=str(exc),
            )

    def replaced(self) -> None:
        """The final row took the in-progress row's place."""
        self.message_id = None

    async def discard(self) -> None:
        """Remove the in-progress row (the turn ends without a final row)."""
        message_id = self.message_id
        if message_id is None:
            return
        self.message_id = None
        remove = getattr(self._session_manager, "remove_message", None)
        if not callable(remove):
            return
        try:
            async with self._write_context(self._session_key):
                await remove(self._session_key, message_id)
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup
            log.warning(
                "turn_progress.discard_failed",
                session_key=self._session_key,
                error=str(exc),
            )
