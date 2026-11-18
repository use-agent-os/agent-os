"""The memory review that runs once before a compaction summarises a session.

The review itself is the periodic memory review ("nudge") with a second
trigger: :meth:`agentos.engine.runtime.TurnRunner.review_memory_before_compaction`
runs it as a real, internal turn of the session about to be compacted. This
module holds what the compaction side needs to know about it.

Compaction must come out exactly as it would have without the review, so the
review turn is kept away from every piece of per-session compaction state.
While it runs, :func:`is_pre_compaction_review` is true for its session in its
own context only, and:

* the compaction coordinator declines to compact that session (the review
  cannot rewrite the transcript it is reviewing, whatever its size);
* the proactive scheduler does not treat it as a turn start or a turn end
  (a turn start would cancel the very job that runs the review);
* the runner neither records nor clears the session's compaction anchor.

It is also add-only, enforced in the ``memory`` and ``memory_save`` tool
handlers through :func:`write_refusal`: it runs unattended, right before the
history that would show what it did is summarised away, so it may add entries
and append notes but never replace, remove or overwrite. Once the review's
caller stops waiting for it (deadline or cancellation), it may not begin any
further memory write either.

Every decision is logged once as a ``pre_compaction_review`` event whose
``outcome`` is ``ran``, ``timeout``, ``error``, ``cancelled`` or ``skipped``
(with a ``reason``).
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Final

import structlog

log = structlog.get_logger(__name__)

EVENT: Final = "pre_compaction_review"

#: Provenance trigger of the review turn (its run kind stays ``memory_nudge``).
TRIGGER: Final = "pre_compaction"

# Outcomes.
RAN: Final = "ran"
TIMEOUT: Final = "timeout"
ERROR: Final = "error"
SKIPPED: Final = "skipped"
#: The compaction the review preceded was cancelled (a new user turn cancels a
#: proactive job), and the review with it.
CANCELLED: Final = "cancelled"

# Skip reasons.
#: ``memory.nudge`` (or its ``before_compaction`` switch) is off.
DISABLED: Final = "disabled"
#: Not a private, human conversation: cron, sub-agent, ACP, group, channel.
INELIGIBLE: Final = "ineligible"
#: The compaction was forced by a full context, so a review turn would not fit.
NO_HEADROOM: Final = "no_headroom"
#: A pre-compaction review of the same session is already running.
IN_PROGRESS: Final = "in_progress"
#: A memory review ran for the session within the last
#: :data:`RECENT_REVIEW_USER_TURNS` user turns.
RECENT_REVIEW: Final = "recent_review"
#: The compaction would summarise nothing (within budget, no older history, or
#: not worth a summary), decided before any model call.
NOTHING_TO_COMPACT: Final = "nothing_to_compact"

#: A review this many user turns ago (or fewer) still covers the conversation.
RECENT_REVIEW_USER_TURNS: Final = 2

# Reasons that say the feature does not apply here at all; logged at debug so
# every cron or sub-agent compaction does not leave an info line behind.
_QUIET_REASONS: Final = frozenset({DISABLED, INELIGIBLE})


@dataclass
class ReviewScope:
    """One running pre-compaction review, as its own context sees it.

    The runner keeps a reference and calls :meth:`close_writes` when it stops
    waiting for the review, so a review that keeps running past its deadline
    cannot start another memory write.
    """

    session_key: str
    writes_open: bool = True

    def close_writes(self) -> None:
        self.writes_open = False


_ACTIVE: contextvars.ContextVar[ReviewScope | None] = contextvars.ContextVar(
    "pre_compaction_review", default=None
)


def is_pre_compaction_review(session_key: str | None) -> bool:
    """True inside the pre-compaction review turn of *session_key*."""
    scope = _ACTIVE.get()
    return scope is not None and scope.session_key == session_key


@contextmanager
def pre_compaction_review_scope(scope: ReviewScope | str) -> Iterator[ReviewScope]:
    """Mark the current context as a pre-compaction review.

    Takes the :class:`ReviewScope` the runner holds (or a session key, for a
    scope nobody closes). Its session key must be canonical: the readers
    compare it with the keys the turn pipeline and the coordinator carry.
    Set by the runtime only, in the review's own task; nothing the model
    sends can reach it.
    """
    active = ReviewScope(scope) if isinstance(scope, str) else scope
    token = _ACTIVE.set(active)
    try:
        yield active
    finally:
        _ACTIVE.reset(token)


def write_refusal(*, adds_only: bool, what: str) -> str | None:
    """Why the running pre-compaction review may not make this memory write.

    ``None`` outside a pre-compaction review (every other caller, including
    the periodic review, keeps its operations) and for an add while the
    review's writes are open. *adds_only* says whether the write only adds
    (``memory`` add, or a batch of adds; ``memory_save`` append); *what*
    names the refused operation for the model.
    """
    scope = _ACTIVE.get()
    if scope is None:
        return None
    if not scope.writes_open:
        return (
            "The pre-compaction memory review has ended; it cannot start another "
            "memory write. Nothing was changed."
        )
    if adds_only:
        return None
    return (
        f"The pre-compaction memory review can only add new memory; {what} is not "
        "allowed here. Nothing was changed. Add a new entry (memory action 'add') "
        "or append a note (memory_save mode 'append') instead, or reply that "
        "nothing needs saving."
    )


def log_outcome(
    *,
    session_key: str,
    compaction: str,
    outcome: str,
    reason: str | None = None,
    agent_id: str | None = None,
    **fields: Any,
) -> None:
    """Record one ``pre_compaction_review`` decision.

    *compaction* names the compaction the review preceded (or was skipped
    for), e.g. ``proactive_background``, ``manual`` or ``agent_inline_overflow``.
    """
    payload: dict[str, Any] = {
        "session_key": session_key,
        "compaction": compaction,
        "outcome": outcome,
    }
    if agent_id is not None:
        payload["agent_id"] = agent_id
    if reason is not None:
        payload["reason"] = reason
    payload.update(fields)
    emit = log.debug if reason in _QUIET_REASONS else log.info
    emit(EVENT, **payload)
