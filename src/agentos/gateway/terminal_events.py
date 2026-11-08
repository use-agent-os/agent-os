"""Exactly-once publication of a run's terminal stream frame.

A run can end along several paths that race: the turn stream reports
``done`` / ``error``, the task runtime marks the task terminal after a cancel
or a crash, and ``sessions.abort`` stops it from outside. Each of them used to
publish (or fail to publish) its own terminal frame, so a client saw two, or --
after a cancel -- none and kept showing the run as active.

Every path now goes through one registry per task runtime, keyed by run id
(the task id):

* the turn stream calls :meth:`TerminalPublications.try_publish` before it
  emits ``session.event.done|error``; only the first claim may emit;
* ``sessions.abort`` calls :meth:`TerminalPublications.hold` before it cancels,
  so a frame the stream produces while the cancel lands is parked instead of
  emitted;
* the task runtime calls :meth:`TerminalPublications.take` once the terminal
  status is persisted and publishes whatever is still owed: the parked frame, or
  one built from the persisted status.

The terminal frame always carries ``state``: ``done``, ``error`` or ``aborted``.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Literal

TERMINAL_STREAM_EVENTS = frozenset({"session.event.done", "session.event.error"})

TerminalState = Literal["done", "error", "aborted"]

#: Entries live from a run's first claim until the runtime settles it, so the
#: registry holds roughly the in-flight runs; the cap is a backstop for entries
#: nothing settles (a run driven without a runtime).
_DEFAULT_MAX_RUNS = 10_000


def mark_aborted_done(payload: dict[str, Any]) -> dict[str, Any]:
    """Shape the ``done`` of a turn that stopped on a cooperative abort.

    Such a turn ends on its own with a DoneEvent whose ``outcome`` is
    ``interrupted`` / ``cancelled``. Its frame reports ``reason: "aborted"``
    and ``partial_text``, the shape the runtime publishes for an aborted run,
    so :func:`terminal_state` gives it state ``aborted``.
    """

    outcome = payload.get("outcome")
    if (
        isinstance(outcome, dict)
        and outcome.get("kind") == "interrupted"
        and outcome.get("reason") == "cancelled"
    ):
        payload["reason"] = "aborted"
        text = payload.get("text")
        payload.setdefault("partial_text", text if isinstance(text, str) else "")
    return payload


def terminal_state(event_name: str, payload: dict[str, Any] | None) -> TerminalState:
    """The ``state`` a terminal stream frame reports."""

    data = payload or {}
    explicit = data.get("state")
    if explicit == "done":
        return "done"
    if explicit == "error":
        return "error"
    if explicit == "aborted":
        return "aborted"
    if event_name == "session.event.done":
        return "aborted" if data.get("reason") == "aborted" else "done"
    code = str(data.get("code") or "").lower()
    reason = str(data.get("terminal_reason") or "").lower()
    if "cancel" in code or reason == "cancelled":
        return "aborted"
    return "error"


@dataclass
class _Publication:
    published: bool = False
    held_by: str | None = None
    parked: tuple[str, dict[str, Any]] | None = None


class TerminalPublications:
    """Per-run terminal reservation shared by every path that ends a run."""

    def __init__(self, *, max_runs: int = _DEFAULT_MAX_RUNS) -> None:
        self._max_runs = max(1, int(max_runs))
        self._runs: OrderedDict[str, _Publication] = OrderedDict()

    def _entry(self, run_id: str) -> _Publication:
        entry = self._runs.get(run_id)
        if entry is None:
            entry = _Publication()
            self._runs[run_id] = entry
            while len(self._runs) > self._max_runs:
                self._runs.popitem(last=False)
        return entry

    def try_publish(self, run_id: str, event_name: str, payload: dict[str, Any]) -> bool:
        """Claim the right to emit ``event_name`` as ``run_id``'s terminal.

        Returns ``False`` when a terminal was already published, or when an
        abort holds the run; in the second case the frame is parked so the
        runtime can still publish it if the run turns out to have finished on
        its own.
        """

        entry = self._entry(run_id)
        if entry.published:
            return False
        if entry.held_by is not None:
            if entry.parked is None:
                entry.parked = (event_name, dict(payload))
            return False
        entry.published = True
        return True

    def hold(self, run_id: str, *, by: str) -> bool:
        """Reserve ``run_id``'s terminal for the runtime to publish.

        Returns ``False`` when the run already published its terminal (it ended
        before the caller got to it).
        """

        entry = self._entry(run_id)
        if entry.published:
            return False
        if entry.held_by is None:
            entry.held_by = by
        return True

    def take(self, run_id: str) -> tuple[bool, tuple[str, dict[str, Any]] | None, str | None]:
        """Settle ``run_id`` once its terminal status is persisted.

        Returns ``(owed, parked, held_by)``: whether a terminal frame still has
        to be published, the frame the stream parked while the run was held (if
        any) and who held it.
        """

        entry = self._runs.pop(run_id, None)
        if entry is None:
            return True, None, None
        if entry.published:
            return False, None, entry.held_by
        return True, entry.parked, entry.held_by

    def is_published(self, run_id: str) -> bool:
        entry = self._runs.get(run_id)
        return bool(entry is not None and entry.published)

    def held_by(self, run_id: str) -> str | None:
        entry = self._runs.get(run_id)
        return entry.held_by if entry is not None else None

    def clear(self) -> None:
        self._runs.clear()


def runtime_terminal_frame(
    status: str,
    *,
    task_id: str,
    terminal_reason: str,
    terminal_message: str | None = None,
    error_class: str | None = None,
    parked: tuple[str, dict[str, Any]] | None = None,
) -> tuple[str, dict[str, Any]]:
    """The terminal stream frame a run owes once its task status is final.

    A frame the stream parked while an abort held the run is preferred when it
    agrees with the outcome (a run that finished on its own keeps its ``done``
    payload, usage and final text included). A cancelled run always ends with
    ``done`` / ``state: aborted`` -- the frame every client already treats as
    "stopped", rather than an error.
    """

    identity = {"run_id": task_id, "task_id": task_id, "terminal_reason": terminal_reason}
    if parked is not None:
        event_name, payload = parked
        state = terminal_state(event_name, payload)
        agrees = (
            status == "succeeded" and event_name == "session.event.done" and state == "done"
        ) or (status in ("failed", "timeout", "abandoned") and state == "error")
        if agrees:
            return event_name, {**payload, **identity, "state": state}
    if status == "succeeded":
        return "session.event.done", {**identity, "state": "done"}
    if status == "cancelled":
        return "session.event.done", {**identity, "state": "aborted", "reason": "aborted"}
    code = str(error_class or ("timeout" if status == "timeout" else status) or "agent_error")
    message = terminal_message or "Agent error"
    return "session.event.error", {
        **identity,
        "state": "error",
        "code": code,
        "message": message,
        "terminal_message": message,
    }
