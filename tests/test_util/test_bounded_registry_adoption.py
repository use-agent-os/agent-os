"""Every site the leak reports named is behind the shared primitive.

The point of #1131 is that these twenty dicts stop having twenty different
lifetime rules, so the check is structural: each named field is a
``BoundedRegistry``, and a session's terminal event reaches every one that is
session-scoped.
"""

from __future__ import annotations

import asyncio

import pytest

from agentos.application.approval_queue import ApprovalQueue
from agentos.application.intent_cache import IntentApprovalCache
from agentos.engine.cache_break_monitor import CacheBreakMonitor
from agentos.engine.progress_watchdog import ProgressWatchdog
from agentos.engine.subagent import SubagentRegistry
from agentos.gateway.session_streams import SessionStreamRegistry
from agentos.plan_mode import PlanModeStore
from agentos.sandbox.governance import DenialLedger
from agentos.sandbox.stale_output_cache import StaleOutputCache
from agentos.tools.builtin import shell
from agentos.util.bounded_registry import BoundedRegistry, drop_session_state


def _field(owner: object, name: str) -> BoundedRegistry:
    value = getattr(owner, name)
    assert isinstance(value, BoundedRegistry), f"{type(owner).__name__}.{name} is {type(value)}"
    return value


@pytest.mark.parametrize(
    "factory,fields",
    [
        (SessionStreamRegistry, ["_seq_by_session", "_events_by_session"]),
        (StaleOutputCache, ["_entries"]),
        (SubagentRegistry, ["_archived"]),
        (PlanModeStore, ["_sessions"]),
        (DenialLedger, ["_sessions"]),
        (ProgressWatchdog, ["_repeat_counts", "_repeat_results"]),
        (CacheBreakMonitor, ["_baselines"]),
        (IntentApprovalCache, ["_entries"]),
    ],
)
def test_named_sites_are_bounded(factory, fields: list[str]) -> None:
    owner = factory()
    for name in fields:
        _field(owner, name)


def test_approval_queue_sites_are_bounded(tmp_path) -> None:
    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.db"))
    try:
        _field(queue, "_node_settings")
        _field(queue, "_session_elevated_modes")
    finally:
        queue.close()


def test_module_level_shell_session_store_is_bounded() -> None:
    assert isinstance(shell._bg_sessions, BoundedRegistry)


def test_a_running_background_session_survives_the_ttl_sweep() -> None:
    """A still-running process is never dropped by the TTL (review of #1131).

    ``_bg_sessions`` is cache-shaped with ``evictable=lambda session:
    session.done``. Before the fix, ``_expire`` ignored the veto, so a dev
    server or long build was evicted from the registry after the TTL while its
    OS process kept running — ``process poll``/``log``/``kill`` then reported
    the session gone. The TTL clock starts at spawn and ``get()`` does not
    refresh it, so a 15-minute process is the normal case, not the edge.
    """
    store: BoundedRegistry = shell._bg_sessions
    original_now = store._now
    monkey_now = {"t": 1000.0}
    store._now = lambda: monkey_now["t"]  # type: ignore[method-assign]

    running = shell._BgSession(  # type: ignore[attr-defined]
        session_id="sess-running",
        command="sleep 9999",
        process=None,  # type: ignore[arg-type]
        session_key="agent:main:one",
        done=False,
    )
    finished = shell._BgSession(  # type: ignore[attr-defined]
        session_id="sess-done",
        command="true",
        process=None,  # type: ignore[arg-type]
        session_key="agent:main:one",
        done=True,
    )
    try:
        store[running.session_id] = running
        store[finished.session_id] = finished

        ttl = store.ttl_seconds or 900.0
        monkey_now["t"] = 1000.0 + ttl + 1
        # Trigger the sweep, then read both keys back.
        assert store.get("sess-running") is running
        assert store.get("sess-done") is None
    finally:
        store.discard("sess-running")
        store.discard("sess-done")
        store._now = original_now  # type: ignore[method-assign]


def test_turn_runner_snapshot_fields_are_bounded() -> None:
    from agentos.engine import runtime

    source = runtime.__file__
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    for field in ("_memory_snapshots", "_bootstrap_snapshots"):
        assert f"self.{field}: BoundedRegistry" in text or f"self.{field}: dict" not in text


def test_usage_tracker_fields_are_bounded() -> None:
    from agentos.engine import usage

    with open(usage.__file__, encoding="utf-8") as handle:
        text = handle.read()
    assert "self._scopes: dict" not in text
    assert "self._session_metadata: dict" not in text


def test_task_runtime_lock_fields_are_bounded() -> None:
    from agentos.gateway import task_runtime

    with open(task_runtime.__file__, encoding="utf-8") as handle:
        text = handle.read()
    assert "self._session_locks: dict" not in text
    assert "self._session_execution_locks: dict" not in text


# ── the terminal event actually reaches them ─────────────────────────


def test_evict_session_runtime_state_drops_bounded_registry_entries() -> None:
    from agentos.session.runtime_state import evict_session_runtime_state

    streams = SessionStreamRegistry()
    plan = PlanModeStore()
    monitor = CacheBreakMonitor()

    streams.record("doomed", "session.event.run_start", {})
    streams.record("kept", "session.event.run_start", {})
    plan.enable("doomed")
    plan.enable("kept")
    monitor._baselines["doomed"] = object()  # type: ignore[assignment]

    evict_session_runtime_state("doomed")

    assert streams.current_seq("doomed") == 0
    assert streams.current_seq("kept") == 1
    assert plan.is_enabled("doomed") is False
    assert plan.is_enabled("kept") is True
    assert "doomed" not in monitor._baselines


def test_denial_ledger_session_state_is_dropped_on_teardown() -> None:
    ledger = DenialLedger()

    async def scenario() -> int:
        await ledger.record_denial("doomed", "fp", "policy")  # type: ignore[arg-type]
        await ledger.record_denial("kept", "fp", "policy")  # type: ignore[arg-type]
        drop_session_state("doomed")
        return await ledger.count_session("kept")

    assert asyncio.run(scenario()) == 1

    async def check() -> int:
        return await ledger.count_session("doomed")

    assert asyncio.run(check()) == 0
