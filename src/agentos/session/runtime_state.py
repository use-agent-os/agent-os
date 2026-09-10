"""Process-global runtime bookkeeping keyed by session.

Several long-lived stores hang off module globals rather than the session
row: spawn-group state in the gateway, routing history in the engine router,
and the per-parent spawn locks in the sessions builtin. None of them are
reachable from storage, so deleting a session row leaves them behind. On a
gateway that runs for weeks, every deleted or pruned session leaks another
entry — an unbounded leak.

:func:`evict_session_runtime_state` is the single choke point every session
removal path calls. Imports are local so this module stays importable from
``agentos.session`` without pulling in engine/gateway packages (and without
creating an import cycle).
"""

from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)


def evict_session_runtime_state(session_key: str) -> None:
    """Drop in-memory per-session bookkeeping: every ``BoundedRegistry`` that
    knows how to identify a session, plus subagent, routing, and spawn-lock
    state.

    This is the single terminal-event hook the bounded registries rely on: the
    size ceiling is the backstop for a session that never reaches here, not the
    mechanism.

    Idempotent and never raises: it runs on both the terminal path
    (``SessionManager.finish``) and every deletion path, and a missing or
    already-evicted entry is the normal case rather than an error. A store
    that fails to import (partial install, trimmed distribution) must not
    block the deletion it is attached to.
    """
    try:
        from agentos.util.bounded_registry import drop_session_state

        drop_session_state(session_key)
    except Exception:
        log.debug("session.runtime_state.bounded_registry_evict_failed", session_key=session_key)
    try:
        from agentos.gateway.subagent_announce import _tracker as _spawn_tracker

        _spawn_tracker.evict(session_key)
    except Exception:
        log.debug("session.runtime_state.spawn_tracker_evict_failed", session_key=session_key)
    try:
        from agentos.engine.steps.agentos_router import (
            _history_store as _routing_store,
        )

        _routing_store.evict(session_key)
    except Exception:
        log.debug("session.runtime_state.routing_history_evict_failed", session_key=session_key)
    try:
        from agentos.tools.builtin.sessions import evict_spawn_lock

        evict_spawn_lock(session_key)
    except Exception:
        log.debug("session.runtime_state.spawn_lock_evict_failed", session_key=session_key)
