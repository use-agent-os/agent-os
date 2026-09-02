"""Spend budgets: ledger accumulation, ceiling evaluation, and turn-loop enforcement."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.engine.types import ErrorEvent, WarningEvent
from agentos.engine.usage import UsageTracker
from agentos.gateway.config import BudgetsConfig

SESSION = "agent:main:telegram:acct:peer"


def _spend(tracker: UsageTracker, session_key: str, cost: float) -> None:
    """Record ``cost`` dollars of provider-billed spend on ``session_key``."""
    tracker.add(
        session_key,
        input_tokens=100,
        output_tokens=10,
        model_id="test-model",
        billed_cost=cost,
        provider_id="test",
    )


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


# ── Ledger ──────────────────────────────────────────────────────────────


def test_daily_ledger_accumulates_without_a_database() -> None:
    tracker = UsageTracker()
    _spend(tracker, SESSION, 1.25)
    _spend(tracker, SESSION, 0.75)

    day = _today()
    assert tracker.get_spend(day, "gateway", "global") == pytest.approx(2.0)
    assert tracker.get_spend(day, "agent", "main") == pytest.approx(2.0)
    assert tracker.get_spend(day, "channel", "telegram") == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_daily_ledger_survives_a_restart(tmp_path: Path) -> None:
    db = str(tmp_path / "spend_ledger.db")
    first = UsageTracker(ledger_db_path=db)
    _spend(first, SESSION, 3.0)

    # A fresh tracker on the same DB stands in for a gateway restart: the
    # whole point of the ledger is that a crash cannot reset a daily ceiling.
    restarted = UsageTracker(ledger_db_path=db)
    assert restarted.get_spend(_today(), "gateway", "global") == pytest.approx(3.0)
    hard_stop, _ = await restarted.check_budget_limits(SESSION, BudgetsConfig(daily_limit=3.0))
    assert hard_stop is True


@pytest.mark.asyncio
async def test_session_ceiling_survives_a_restart(tmp_path: Path) -> None:
    """A crash-and-respawn must not hand a session a fresh allowance."""
    db = str(tmp_path / "spend_ledger.db")
    first = UsageTracker(ledger_db_path=db)
    _spend(first, SESSION, 4.0)

    restarted = UsageTracker(ledger_db_path=db)
    assert restarted.get_effective_session_cost(SESSION) == pytest.approx(4.0)

    config = BudgetsConfig(session_limit=5.0)
    assert await restarted.check_budget_limits(SESSION, config) == (False, None)
    # The post-restart turn's own spend adds to the pre-restart total rather
    # than starting the count over.
    _spend(restarted, SESSION, 1.0)
    assert (await restarted.check_budget_limits(SESSION, config))[0] is True


@pytest.mark.asyncio
async def test_a_dropped_ledger_write_cannot_retire_a_ceiling(tmp_path: Path) -> None:
    """The persisted row can only under-report; reads take the larger value."""
    db = str(tmp_path / "spend_ledger.db")
    tracker = UsageTracker(ledger_db_path=db)
    _spend(tracker, SESSION, 6.0)

    # Simulate a write that never landed (sqlite busy, disk full): the row
    # lags behind what this process has actually spent.
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE spend_ledger SET cost_usd = 1.0")

    assert tracker.get_spend(_today(), "gateway", "global") == pytest.approx(6.0)
    assert (await tracker.check_budget_limits(SESSION, BudgetsConfig(daily_limit=5.0)))[0] is True


def test_ledger_ignores_zero_cost_usage() -> None:
    tracker = UsageTracker()
    tracker.add(SESSION, input_tokens=0, output_tokens=0, model_id="test-model")

    assert tracker.get_spend(_today(), "gateway", "global") == 0.0


# ── Ceiling evaluation ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_config_and_empty_config_enforce_nothing() -> None:
    tracker = UsageTracker()
    _spend(tracker, SESSION, 100.0)

    assert await tracker.check_budget_limits(SESSION, None) == (False, None)
    assert await tracker.check_budget_limits(SESSION, BudgetsConfig()) == (False, None)


@pytest.mark.asyncio
async def test_disabled_switch_suspends_configured_ceilings() -> None:
    tracker = UsageTracker()
    _spend(tracker, SESSION, 10.0)

    config = BudgetsConfig(enabled=False, session_limit=1.0, session_warn=0.5)
    assert await tracker.check_budget_limits(SESSION, config) == (False, None)


@pytest.mark.asyncio
async def test_session_limit_hard_stops_at_the_ceiling() -> None:
    tracker = UsageTracker()
    config = BudgetsConfig(session_limit=2.0)

    _spend(tracker, SESSION, 1.5)
    assert await tracker.check_budget_limits(SESSION, config) == (False, None)

    _spend(tracker, SESSION, 0.5)
    hard_stop, message = await tracker.check_budget_limits(SESSION, config)
    assert hard_stop is True
    assert message is not None
    assert "Session cost" in message
    assert "$2.0000" in message


@pytest.mark.asyncio
async def test_session_warning_fires_once_and_does_not_stop_the_turn() -> None:
    tracker = UsageTracker()
    config = BudgetsConfig(session_warn=1.0, session_limit=10.0)

    _spend(tracker, SESSION, 1.0)
    hard_stop, message = await tracker.check_budget_limits(SESSION, config)
    assert hard_stop is False
    assert message is not None
    assert "warning threshold" in message

    _spend(tracker, SESSION, 1.0)
    assert await tracker.check_budget_limits(SESSION, config) == (False, None)


@pytest.mark.asyncio
async def test_daily_limit_hard_stops_across_sessions() -> None:
    tracker = UsageTracker()
    config = BudgetsConfig(daily_limit=5.0)

    _spend(tracker, "agent:main:telegram:a:1", 3.0)
    _spend(tracker, "agent:main:webchat:b:2", 2.0)

    hard_stop, message = await tracker.check_budget_limits("agent:main:webchat:b:2", config)
    assert hard_stop is True
    assert message is not None
    assert "Daily gateway cost" in message


@pytest.mark.asyncio
async def test_agent_and_channel_daily_ceilings_are_scoped() -> None:
    tracker = UsageTracker()
    _spend(tracker, "agent:trader:telegram:a:1", 4.0)
    _spend(tracker, "agent:writer:webchat:b:2", 1.0)

    config = BudgetsConfig(agent_daily_limit={"trader": 3.0})
    assert (await tracker.check_budget_limits("agent:trader:telegram:a:1", config))[0] is True
    assert await tracker.check_budget_limits("agent:writer:webchat:b:2", config) == (False, None)

    channel_config = BudgetsConfig(channel_daily_limit={"telegram": 3.0})
    trader = await tracker.check_budget_limits("agent:trader:telegram:a:1", channel_config)
    assert trader[0] is True
    writer = await tracker.check_budget_limits("agent:writer:webchat:b:2", channel_config)
    assert writer == (False, None)


@pytest.mark.asyncio
async def test_subagent_spend_is_attributed_to_the_parent_agent() -> None:
    """A fan-out must count against the spawning agent's daily ceiling."""
    tracker = UsageTracker()
    _spend(tracker, "subagent:agent:trader:subagent:child-1", 2.0)
    _spend(tracker, "subagent:agent:trader:subagent:child-2", 2.0)

    config = BudgetsConfig(agent_daily_limit={"trader": 3.0})
    hard_stop, message = await tracker.check_budget_limits("agent:trader:telegram:a:1", config)
    assert hard_stop is True
    assert message is not None
    assert "agent 'trader'" in message


@pytest.mark.asyncio
async def test_hard_stop_wins_over_an_earlier_scope_warning() -> None:
    tracker = UsageTracker()
    _spend(tracker, SESSION, 6.0)

    # Session is only at its warn threshold, but the daily ceiling is breached.
    config = BudgetsConfig(session_warn=5.0, session_limit=20.0, daily_limit=6.0)
    hard_stop, message = await tracker.check_budget_limits(SESSION, config)
    assert hard_stop is True
    assert message is not None
    assert "Daily gateway cost" in message


# ── Config validation ───────────────────────────────────────────────────


def test_warn_above_its_own_limit_is_rejected() -> None:
    with pytest.raises(ValueError, match="session_warn"):
        BudgetsConfig(session_warn=10.0, session_limit=5.0)
    with pytest.raises(ValueError, match="daily_warn"):
        BudgetsConfig(daily_warn=10.0, daily_limit=5.0)
    with pytest.raises(ValueError, match="agent_daily_warn"):
        BudgetsConfig(agent_daily_warn={"main": 10.0}, agent_daily_limit={"main": 5.0})


def test_negative_ceilings_are_rejected() -> None:
    with pytest.raises(ValueError):
        BudgetsConfig(session_limit=-1.0)
    with pytest.raises(ValueError, match="must be >= 0"):
        BudgetsConfig(agent_daily_limit={"main": -1.0})


def test_colliding_scope_keys_are_rejected() -> None:
    """Two spellings of one scope would silently drop one of the numbers."""
    with pytest.raises(ValueError, match="both refer to"):
        BudgetsConfig(agent_daily_limit={"default": 5.0, "main": 100.0})
    with pytest.raises(ValueError, match="both refer to"):
        BudgetsConfig(channel_daily_limit={"Telegram": 5.0, "telegram": 100.0})


def test_scope_keys_are_normalized() -> None:
    config = BudgetsConfig(
        agent_daily_limit={"Default": 5.0},
        channel_daily_limit={"Telegram": 5.0},
    )
    assert config.agent_daily_limit == {"main": 5.0}
    assert config.channel_daily_limit == {"telegram": 5.0}


def test_unknown_budget_keys_are_rejected() -> None:
    with pytest.raises(ValueError):
        BudgetsConfig(sesion_limit=5.0)  # type: ignore[call-arg]


# ── Turn-loop enforcement ───────────────────────────────────────────────


class _RecordingSessionManager:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []

    async def append_message(self, session_key: str, *, role: str, content: str) -> None:
        self.messages.append((session_key, role, content))


def _runner(tracker: Any, budgets: Any, manager: Any) -> Any:
    from agentos.engine.runtime import TurnRunner

    return TurnRunner(
        provider_selector=None,
        session_manager=manager,
        usage_tracker=tracker,
        config=SimpleNamespace(budgets=budgets, context_window_tokens=100_000),
    )


async def _collect(runner: Any) -> list[Any]:
    return [event async for event in runner._run_turn("hi", SESSION, "main", None, [])]


@pytest.mark.asyncio
async def test_turn_is_refused_at_the_hard_limit() -> None:
    tracker = UsageTracker()
    _spend(tracker, SESSION, 5.0)
    manager = _RecordingSessionManager()
    runner = _runner(tracker, BudgetsConfig(session_limit=5.0), manager)

    events = await _collect(runner)

    assert len(events) == 1
    error = events[0]
    assert isinstance(error, ErrorEvent)
    assert error.code == "budget_exceeded"
    assert "budget limit" in error.message
    # The refusal is auditable in the transcript, not only in the log.
    assert manager.messages and manager.messages[0][1] == "system"


@pytest.mark.asyncio
async def test_turn_warns_but_continues_below_the_hard_limit() -> None:
    tracker = UsageTracker()
    _spend(tracker, SESSION, 4.0)
    runner = _runner(tracker, BudgetsConfig(session_warn=4.0, session_limit=10.0), None)

    events = await _collect(runner)

    assert isinstance(events[0], WarningEvent)
    assert events[0].code == "budget_warning"
    # The turn was not refused — it proceeded and failed later on the absent
    # provider, which is the pre-existing behavior for this stub runner.
    assert not any(
        isinstance(event, ErrorEvent) and event.code == "budget_exceeded" for event in events
    )


@pytest.mark.asyncio
async def test_turn_runs_when_no_budgets_are_configured() -> None:
    tracker = UsageTracker()
    _spend(tracker, SESSION, 100.0)
    runner = _runner(tracker, BudgetsConfig(), None)

    events = await _collect(runner)

    assert not any(
        isinstance(event, ErrorEvent) and event.code == "budget_exceeded" for event in events
    )


@pytest.mark.asyncio
async def test_hard_stop_without_a_message_still_refuses_the_turn() -> None:
    """The refusal must hinge on the decision, not on the presentation text."""

    class _TerseTracker:
        async def check_budget_limits(
            self, session_key: str, config: Any
        ) -> tuple[bool, str | None]:
            return True, None

    runner = _runner(_TerseTracker(), BudgetsConfig(session_limit=1.0), None)

    events = await _collect(runner)

    assert len(events) == 1
    assert isinstance(events[0], ErrorEvent)
    assert events[0].code == "budget_exceeded"
    assert events[0].message


@pytest.mark.asyncio
async def test_budget_check_failure_fails_open() -> None:
    """A broken budget check must never be the reason a turn is refused."""

    class _ExplodingTracker:
        async def check_budget_limits(
            self, session_key: str, config: Any
        ) -> tuple[bool, str | None]:
            raise RuntimeError("ledger unavailable")

    runner = _runner(_ExplodingTracker(), BudgetsConfig(session_limit=0.0), None)

    events = await _collect(runner)

    assert not any(
        isinstance(event, ErrorEvent) and event.code == "budget_exceeded" for event in events
    )


# ── Intra-turn enforcement ──────────────────────────────────────────────


class _LoopingToolProvider:
    """A provider that never stops calling a tool — a runaway turn."""

    provider_name = "fake"

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def chat(
        self,
        messages: list[Any],
        tools: list[Any] | None = None,
        config: Any | None = None,
    ) -> Any:
        self.calls.append(messages)
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int) -> Any:
        from agentos.provider import DoneEvent as ProviderDone
        from agentos.provider import ToolUseEndEvent as ProviderToolUseEnd
        from agentos.provider import ToolUseStartEvent as ProviderToolUseStart

        tool_id = f"tool-{call_number}"
        yield ProviderToolUseStart(tool_use_id=tool_id, tool_name="echo")
        yield ProviderToolUseEnd(
            tool_use_id=tool_id, tool_name="echo", arguments={"value": "again"}
        )
        yield ProviderDone(stop_reason="tool_use", input_tokens=1, output_tokens=1)


@pytest.mark.asyncio
async def test_spend_guard_stops_a_runaway_loop_inside_one_turn() -> None:
    """The turn-start gate alone leaves an unbounded tool loop unbounded."""
    from agentos.engine import Agent, AgentConfig, ToolResult
    from agentos.provider import ToolDefinition, ToolInputSchema

    async def _echo(call: Any) -> Any:
        return ToolResult(tool_use_id=call.tool_use_id, tool_name=call.tool_name, content="ok")

    seen: list[str] = []

    async def guard(session_key: str) -> tuple[bool, str | None]:
        seen.append(session_key)
        # Under the ceiling for the first iteration, over it after that.
        return len(seen) > 1, "Daily gateway cost $50.0000 has reached the $50.0000 budget limit."

    provider = _LoopingToolProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=0),  # unbounded, as the shipped default is
        tool_definitions=[
            ToolDefinition(
                name="echo",
                description="Echo.",
                input_schema=ToolInputSchema(
                    properties={"value": {"type": "string"}}, required=["value"]
                ),
            )
        ],
        tool_handler=_echo,
        session_key=SESSION,
        spend_budget_guard=guard,
    )

    events = [event async for event in agent.run_turn("go")]

    assert any(
        getattr(event, "code", "") == "budget_exceeded" and event.kind == "error"
        for event in events
    )
    # It stopped early rather than looping to exhaustion.
    assert len(provider.calls) <= 3
    assert seen == [SESSION] * len(seen)


@pytest.mark.asyncio
async def test_a_broken_guard_does_not_stop_the_turn() -> None:
    from agentos.engine import Agent, AgentConfig

    async def guard(session_key: str) -> tuple[bool, str | None]:
        raise RuntimeError("ledger unavailable")

    provider = _LoopingToolProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=2),
        session_key=SESSION,
        spend_budget_guard=guard,
    )

    events = [event async for event in agent.run_turn("go")]

    assert not any(getattr(event, "code", "") == "budget_exceeded" for event in events)


# ── Concurrent admission (reservation race) ─────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_checks_near_ceiling_do_not_all_pass() -> None:
    """The reservation race: N turns admitted at once must not each clear the
    same pre-reservation ceiling.

    Spend sits just under a session ceiling, then several checks run
    concurrently — the shape of a subagent fan-out, where every child calls
    the budget check before any of them has recorded a cent. Without
    reservations every check reads the same stale spend and all pass; with
    them, only the ones that still fit under the ceiling once siblings'
    reservations are counted are admitted.
    """
    import asyncio

    tracker = UsageTracker()
    # $9.90 spent, $10 ceiling, default $0.50 reservation → only the first
    # admission fits (9.90 + 0.50 = 10.40 > 10 blocks the rest, but the very
    # first sees 9.90 < 10 and is let through).
    _spend(tracker, SESSION, 9.90)
    config = BudgetsConfig(session_limit=10.0)

    results = await asyncio.gather(
        *(tracker.check_budget_limits(SESSION, config) for _ in range(8))
    )
    hard_stops = [hard_stop for hard_stop, _ in results]

    # At least one must be refused — the pre-fix code would pass all eight.
    assert any(hard_stops), "expected at least one concurrent check to be refused"
    # And at least one is admitted (the first, before any reservation lands).
    assert not all(hard_stops), "the first admission under the ceiling should pass"


@pytest.mark.asyncio
async def test_reservation_expires_so_a_stalled_turn_frees_headroom() -> None:
    """A crashed/hung turn's reservation must not permanently eat the budget."""
    import time as _time

    tracker = UsageTracker()
    _spend(tracker, SESSION, 9.90)
    # Tiny TTL so the reservation from the first check has expired by the second.
    config = BudgetsConfig(session_limit=10.0, reservation_ttl_seconds=0.01)

    first_stop, _ = await tracker.check_budget_limits(SESSION, config)
    assert first_stop is False  # admitted, places a reservation

    _time.sleep(0.05)  # let the reservation expire

    # Real spend never landed (turn hung), so a later check sees only the
    # $9.90 real spend again, not a stuck reservation on top of it.
    second_stop, _ = await tracker.check_budget_limits(SESSION, config)
    assert second_stop is False


# ── Reservation release on turn teardown (issue #823, point 2) ──────────


@pytest.mark.asyncio
async def test_reservation_released_frees_headroom_immediately() -> None:
    """A turn that ends WITHOUT recording spend (errored/cancelled before any
    provider call) must free its admission reservation at once — not leave it
    to expire on the TTL and hold headroom a sibling could use.

    This is the release-on-all-paths property: reserve at admission, then
    release explicitly at turn teardown regardless of how the turn ended.
    """
    tracker = UsageTracker()
    _spend(tracker, SESSION, 9.00)
    # Long TTL so ONLY an explicit release (not expiry) can free headroom.
    cfg = BudgetsConfig(session_limit=10.0, reservation_ttl_seconds=10_000.0)

    # A check places its own reservation only AFTER it passes the gate, so it
    # never blocks on itself. Each admission adds $0.50 of reserved headroom
    # that the NEXT check sees: 9.00 -> admit (reserved 0.50) -> admit
    # (reserved 1.00) -> the third sees 9.00 + 1.00 = 10.00 and is blocked.
    assert (await tracker.check_budget_limits(SESSION, cfg))[0] is False
    assert (await tracker.check_budget_limits(SESSION, cfg))[0] is False
    assert (await tracker.check_budget_limits(SESSION, cfg))[0] is True  # ceiling hit

    # Those turns end without recording spend (error/cancel path): release
    # frees the held headroom immediately despite the 10_000s TTL not elapsing.
    tracker.release_session_reservations(SESSION)

    # A fresh admission now sees only the 9.00 real spend again and is let in.
    assert (await tracker.check_budget_limits(SESSION, cfg))[0] is False


@pytest.mark.asyncio
async def test_release_targets_only_own_reservation_not_siblings() -> None:
    """Releasing one session's reservations must never drop another session's,
    even though releases are keyed per session id set."""
    tracker = UsageTracker()
    cfg = BudgetsConfig(session_limit=100.0, reservation_ttl_seconds=10_000.0)

    await tracker.check_budget_limits("agent:a:web:s1:1", cfg)
    await tracker.check_budget_limits("agent:a:web:s2:1", cfg)

    # Release s1 only.
    tracker.release_session_reservations("agent:a:web:s1:1")

    # s2's reservation ids must be untouched (still tracked).
    assert "agent:a:web:s2:1" in tracker._session_reservation_ids
    assert "agent:a:web:s1:1" not in tracker._session_reservation_ids
