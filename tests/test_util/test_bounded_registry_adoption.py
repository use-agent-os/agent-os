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
from agentos.gateway.channel_dispatch import ChannelSessionPointers
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
        (ChannelSessionPointers, ["_map"]),
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


def test_a_running_background_shell_session_outlives_the_cache_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``background_process`` exists for dev servers and long builds, so a
    session that is still running must stay reachable past the TTL — the
    subprocess would otherwise keep running with nothing able to poll, log or
    kill it, and its cleanup callbacks would never fire."""
    registry = shell._bg_sessions
    ttl = registry.ttl_seconds
    assert ttl is not None
    now = [1000.0]
    monkeypatch.setattr(registry, "_now", lambda: now[0])

    running = shell._BgSession(session_id="bg-run", command="sleep", process=object())  # type: ignore[arg-type]
    finished = shell._BgSession(
        session_id="bg-done",
        command="true",
        process=object(),
        done=True,  # type: ignore[arg-type]
    )
    registry["bg-run"] = running
    registry["bg-done"] = finished
    try:
        now[0] += ttl + 1

        assert registry.get("bg-run") is running
        assert registry.get("bg-done") is None

        running.done = True
        assert registry.get("bg-run") is None
    finally:
        registry.pop("bg-run", None)
        registry.pop("bg-done", None)


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


def test_channel_session_pointers_are_dropped_on_teardown() -> None:
    """#1561: ``_map`` grew one entry per chat that ever ran ``/new``, forever."""
    pointers = ChannelSessionPointers()
    pointers.set("doomed", "doomed-new")
    pointers.set("kept", "kept-new")

    drop_session_state("doomed")

    assert pointers.resolve("doomed") == "doomed"
    assert pointers.resolve("kept") == "kept-new"


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


def test_discord_channel_context_sites_are_bounded() -> None:
    from agentos.channels.discord import DiscordChannel, DiscordChannelConfig

    channel = DiscordChannel(DiscordChannelConfig(token="token"))
    _field(channel, "_channel_types")
    _field(channel, "_thread_parent_channels")
