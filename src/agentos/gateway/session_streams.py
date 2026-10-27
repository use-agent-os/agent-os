"""In-memory session stream replay buffers for gateway events."""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from agentos.gateway.terminal_events import TERMINAL_STREAM_EVENTS, terminal_state
from agentos.util.bounded_registry import BoundedRegistry

#: Frames kept in a session's replay buffer besides ``session.event.*``: a run's
#: ``task.*`` lifecycle and the session's ``sessions.changed`` are what tell a
#: client that reconnects late whether the run it was showing has ended.
_REPLAYABLE_EVENTS = frozenset({"sessions.changed"})
_REPLAYABLE_PREFIXES = ("session.event.", "task.")

_TERMINAL_TASK_EVENTS = frozenset(
    {"task.succeeded", "task.failed", "task.cancelled", "task.timeout", "task.abandoned"}
)

#: Byte budget of an ``in_flight_run`` snapshot (its JSON encoding).
IN_FLIGHT_BUDGET_BYTES = 256 * 1024
#: Text a live run keeps in memory; older text is dropped from the front.
_LIVE_TEXT_MAX_CHARS = 1_000_000
#: Progress events a live run keeps (newest win).
_LIVE_EVENTS_MAX = 64
#: Tail of the partial answer carried on an ``aborted`` terminal frame.
_PARTIAL_TEXT_MAX_CHARS = 64 * 1024


def is_replayable_event(event_name: str) -> bool:
    """True for frames the per-session replay buffer keeps."""
    return event_name in _REPLAYABLE_EVENTS or event_name.startswith(_REPLAYABLE_PREFIXES)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode())


@dataclass(frozen=True)
class BufferedSessionEvent:
    event_name: str
    payload: dict[str, Any]
    stream_seq: int


@dataclass(frozen=True)
class ReplayResult:
    current_stream_seq: int
    replay_complete: bool
    events: list[BufferedSessionEvent]
    gap_reason: str | None = None


@dataclass
class _LiveRun:
    """What a running turn has streamed so far, folded from its own frames."""

    run_id: str
    task_id: str
    started_at: int
    through_stream_seq: int
    text: str = ""
    text_truncated: bool = False
    plan: Any = None
    events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=_LIVE_EVENTS_MAX))
    # Text since the newest message steered into the run (``None``: none
    # was). The snapshot shows only this part: the part before it is listed
    # by ``chat.history`` from the run's in-progress row, above the steered
    # message.
    text_since_steer: str | None = None

    def append_text(self, chunk: str) -> None:
        if not chunk:
            return
        self.text += chunk
        if len(self.text) > _LIVE_TEXT_MAX_CHARS:
            self.text = self.text[-_LIVE_TEXT_MAX_CHARS:]
            self.text_truncated = True
        if self.text_since_steer is not None:
            self.text_since_steer = (self.text_since_steer + chunk)[-_LIVE_TEXT_MAX_CHARS:]

    def steered(self) -> None:
        self.text_since_steer = ""


def _progress_event(event_name: str, payload: dict[str, Any], stream_seq: int) -> dict[str, Any]:
    """A compact record of one progress frame: identity, never bodies."""
    kind = event_name[len("session.event.") :]
    entry: dict[str, Any] = {"kind": kind, "stream_seq": stream_seq}
    for key in ("tool_use_id", "tool_name", "is_error", "name", "mime", "id", "to_state"):
        value = payload.get(key)
        if value is None or value == "":
            continue
        enum_value = getattr(value, "value", None)
        entry[key] = enum_value if enum_value is not None else value
    return entry


def _plan_from_tool_result(payload: dict[str, Any]) -> Any:
    raw = payload.get("result")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    if isinstance(parsed, dict):
        return parsed.get("plan", parsed)
    return None


def project_live_run(
    live: _LiveRun,
    *,
    budget_bytes: int = IN_FLIGHT_BUDGET_BYTES,
    status: str = "running",
) -> dict[str, Any]:
    """Project a live run into a byte-bounded ``in_flight_run`` snapshot.

    Filled in priority order -- run identity, ``started_at``, the most recent
    progress events, the plan, then the text -- so a run whose text is huge
    still reports what it is and what it is doing. Text that does not fit is
    cut from the front (the newest text is what a viewer is reading) and the
    snapshot says so with ``text_truncated``.
    """

    snapshot: dict[str, Any] = {
        "run_id": live.run_id,
        "task_id": live.task_id,
        "status": status,
        "started_at": live.started_at,
        "through_stream_seq": live.through_stream_seq,
        "text": "",
    }
    # Reserve room for the flags added at the end.
    remaining = budget_bytes - _json_size(snapshot) - len(',"text_truncated":true')
    events: list[dict[str, Any]] = []
    for event in reversed(live.events):
        size = _json_size(event) + 1
        if size > remaining:
            break
        events.insert(0, event)
        remaining -= size
    if events:
        snapshot["events"] = events
        remaining -= len(',"events":[]')
    if live.plan is not None:
        size = _json_size(live.plan) + len(',"plan":')
        if size <= remaining:
            snapshot["plan"] = live.plan
            remaining -= size
    text = live.text if live.text_since_steer is None else live.text_since_steer
    truncated = live.text_truncated if live.text_since_steer is None else False
    # Shrink from the front until the encoded text fits; JSON escaping makes
    # the encoded size larger than the raw text, so re-measure each round.
    while text and _json_size(text) > max(0, remaining):
        overshoot = _json_size(text) - max(0, remaining)
        cut = max(overshoot, len(text) // 8, 1)
        text = text[cut:]
        truncated = True
    snapshot["text"] = text
    if truncated:
        snapshot["text_truncated"] = True
    return snapshot


class SessionStreamRegistry:
    """Small in-memory replay buffer keyed by session.

    The WebSocket frame ``seq`` is per connection. ``stream_seq`` is per
    session and survives reconnects long enough to replay recent run events.
    The registry also folds each session's live run (``task.running`` up to
    its terminal) into a snapshot a reconnecting client can adopt.
    """

    def __init__(self, *, max_events_per_session: int = 500) -> None:
        self._max_events_per_session = max_events_per_session
        # All session-scoped: a gateway that serves many short sessions would
        # otherwise keep one counter and one replay buffer per session for the
        # life of the process. They outlive a session *finishing*, though: a
        # later turn on it (a follow-up to a child) must continue the same
        # ``stream_seq``, or a client that saw the first turn drops the second
        # one's frames as duplicates. They go when the session is deleted.
        self._seq_by_session: BoundedRegistry[str, int] = BoundedRegistry(
            name="SessionStreamRegistry._seq_by_session",
            session_of=lambda key, _value: key,
            keep_until_deleted=True,
        )
        self._events_by_session: BoundedRegistry[str, deque[BufferedSessionEvent]] = (
            BoundedRegistry(
                name="SessionStreamRegistry._events_by_session",
                session_of=lambda key, _value: key,
                keep_until_deleted=True,
            )
        )
        self._live_runs: BoundedRegistry[str, _LiveRun] = BoundedRegistry(
            name="SessionStreamRegistry._live_runs",
            session_of=lambda key, _value: key,
            keep_until_deleted=True,
        )

    @staticmethod
    def _is_replay_lossy(event_name: str) -> bool:
        return event_name in {
            "session.event.text_delta",
            "session.event.thinking",
            "session.event.run_heartbeat",
        }

    def _trim_session_events(self, events: deque[BufferedSessionEvent]) -> None:
        while len(events) > self._max_events_per_session:
            for index, event in enumerate(events):
                if self._is_replay_lossy(event.event_name):
                    del events[index]
                    break
            else:
                events.popleft()

    def current_seq(self, session_key: str) -> int:
        return self._seq_by_session.get(session_key, 0)

    def record(
        self,
        session_key: str,
        event_name: str,
        payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        stream_seq = self.current_seq(session_key) + 1
        self._seq_by_session[session_key] = stream_seq

        enriched = dict(payload or {})
        enriched["session_key"] = session_key
        enriched["stream_seq"] = stream_seq
        self._fold_live_run(session_key, event_name, enriched, stream_seq)

        event = BufferedSessionEvent(event_name=event_name, payload=enriched, stream_seq=stream_seq)
        events = self._events_by_session.setdefault(session_key, deque())
        events.append(event)
        self._trim_session_events(events)
        return enriched

    def record_if_replayable(
        self,
        session_key: str,
        event_name: str,
        payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """:meth:`record` a replayable frame; pass any other payload through."""
        if is_replayable_event(event_name):
            return self.record(session_key, event_name, payload)
        return dict(payload or {})

    # -- live run ---------------------------------------------------------

    def _fold_live_run(
        self,
        session_key: str,
        event_name: str,
        payload: dict[str, Any],
        stream_seq: int,
    ) -> None:
        if event_name == "task.running":
            task_id = payload.get("task_id")
            if isinstance(task_id, str) and task_id:
                self._live_runs[session_key] = _LiveRun(
                    run_id=task_id,
                    task_id=task_id,
                    started_at=_now_ms(),
                    through_stream_seq=stream_seq,
                )
            return
        live = self._live_runs.get(session_key)
        if live is None:
            return
        if event_name in _TERMINAL_TASK_EVENTS:
            if payload.get("task_id") in (None, live.task_id):
                self._live_runs.pop(session_key, None)
            return
        if event_name in TERMINAL_STREAM_EVENTS:
            if payload.get("task_id") not in (None, live.task_id):
                return
            if terminal_state(event_name, payload) == "aborted" and "partial_text" not in payload:
                partial = live.text[-_PARTIAL_TEXT_MAX_CHARS:]
                payload["partial_text"] = partial
                if live.text_truncated or len(partial) < len(live.text):
                    payload["partial_text_truncated"] = True
            self._live_runs.pop(session_key, None)
            return
        if not event_name.startswith("session.event."):
            return
        live.through_stream_seq = stream_seq
        if event_name == "session.event.text_delta":
            text = payload.get("text")
            if isinstance(text, str):
                live.append_text(text)
        elif event_name == "session.event.steering":
            live.steered()
            live.events.append(_progress_event(event_name, payload, stream_seq))
        elif event_name in (
            "session.event.tool_use_start",
            "session.event.tool_result",
            "session.event.artifact",
            "session.event.state_change",
            "session.event.compaction",
        ):
            live.events.append(_progress_event(event_name, payload, stream_seq))
            if (
                event_name == "session.event.tool_result"
                and payload.get("tool_name") == "exit_plan_mode"
            ):
                plan = _plan_from_tool_result(payload)
                if plan is not None:
                    live.plan = plan

    def live_run_task_id(self, session_key: str) -> str | None:
        live = self._live_runs.get(session_key)
        return live.task_id if live is not None else None

    def drop_live_run(self, session_key: str) -> None:
        self._live_runs.pop(session_key, None)

    def in_flight_snapshot(
        self,
        session_key: str,
        *,
        active_task: dict[str, Any] | None,
        budget_bytes: int = IN_FLIGHT_BUDGET_BYTES,
    ) -> dict[str, Any] | None:
        """``in_flight_run`` for a session, checked against its task ledger.

        ``active_task`` is the session's queued/running task summary (or
        ``None``). The ledger is the authority on *whether* something runs;
        the live buffer only says what it has streamed. ``None`` therefore
        means "nothing is running". A buffer left behind is not dropped here:
        the run's own terminal frames clear it, and the ``aborted`` frame
        still needs its text for ``partial_text``.
        """
        if not isinstance(active_task, dict):
            return None
        task_id = active_task.get("task_id")
        status = str(active_task.get("status") or "running")
        live = self._live_runs.get(session_key)
        if live is not None and isinstance(task_id, str) and live.task_id == task_id:
            # Every frame recorded so far was folded in (the others carry no
            # text), so the snapshot covers the session's whole stream.
            live.through_stream_seq = self.current_seq(session_key)
            return project_live_run(live, budget_bytes=budget_bytes, status=status)
        started_at = active_task.get("started_at") or active_task.get("created_at")
        return {
            "run_id": task_id,
            "task_id": task_id,
            "status": status,
            "started_at": started_at,
            "through_stream_seq": self.current_seq(session_key),
            "text": "",
        }

    # -- replay -----------------------------------------------------------

    def replay(self, session_key: str, since_stream_seq: int | None) -> ReplayResult:
        current = self.current_seq(session_key)
        if since_stream_seq is None:
            return ReplayResult(current_stream_seq=current, replay_complete=True, events=[])

        events = list(self._events_by_session.get(session_key, ()))
        if current == 0:
            return ReplayResult(
                current_stream_seq=0,
                replay_complete=since_stream_seq == 0,
                events=[],
                gap_reason=None if since_stream_seq == 0 else "stream_buffer_reset",
            )

        if since_stream_seq > current:
            return ReplayResult(
                current_stream_seq=current,
                replay_complete=False,
                events=[],
                gap_reason="cursor_ahead_of_stream",
            )

        if since_stream_seq == current:
            return ReplayResult(current_stream_seq=current, replay_complete=True, events=[])

        if not events:
            return ReplayResult(
                current_stream_seq=current,
                replay_complete=False,
                events=[],
                gap_reason="stream_buffer_empty",
            )

        first_seq = events[0].stream_seq
        replay_complete = since_stream_seq >= first_seq - 1
        replay_events = [event for event in events if event.stream_seq > since_stream_seq]
        return ReplayResult(
            current_stream_seq=current,
            replay_complete=replay_complete,
            events=replay_events,
            gap_reason=None if replay_complete else "buffer_window_missed",
        )


_session_streams = SessionStreamRegistry()


def get_session_streams() -> SessionStreamRegistry:
    return _session_streams
