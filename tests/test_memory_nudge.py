"""Periodic memory-review nudge.

Curated memory only fills up when something asks the agent to write to it, so
the nudge is what turns "the agent *can* save memories" into "the agent
*does*". These tests pin the counter's arithmetic and, more importantly, the
cases where it must stay quiet: machine traffic, self-directed writes, and
reviews reviewing themselves.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentos.engine.runtime import TurnRunner
from agentos.gateway.config import MemoryNudgeConfig


class _Cfg:
    """Minimal stand-in for the memory config block."""

    def __init__(self, nudge: MemoryNudgeConfig | None) -> None:
        self.nudge = nudge


class _Runner:
    """TurnRunner counter surface, unbound from the full runtime.

    The nudge methods only touch `_config` and `_memory_nudge_counters`, so
    binding them onto a bare object exercises the real implementations
    without standing up a gateway.
    """

    def __init__(self, nudge: MemoryNudgeConfig | None) -> None:
        self._config = type("C", (), {"memory": _Cfg(nudge)})()
        self._memory_nudge_counters: dict[tuple[str, str], int] = {}

    _memory_nudge_config = TurnRunner._memory_nudge_config
    _memory_nudge_allowed = TurnRunner._memory_nudge_allowed
    _turn_used_memory_tool = staticmethod(TurnRunner._turn_used_memory_tool)
    _capture_filter_matches = staticmethod(TurnRunner._capture_filter_matches)
    _hydrate_nudge_counter = staticmethod(TurnRunner._hydrate_nudge_counter)
    _evict_nudge_counters_if_needed = TurnRunner._evict_nudge_counters_if_needed
    _note_turn_for_memory_nudge = TurnRunner._note_turn_for_memory_nudge
    forget_memory_nudge_counter = TurnRunner.forget_memory_nudge_counter


def _note(runner: _Runner, **overrides: Any) -> bool:
    kwargs: dict[str, Any] = {
        "agent_id": "main",
        "session_key": "s1",
        "input_mode": "user",
        "run_kind": "default",
        "no_memory_capture": False,
        "turn_segments": [],
        "prior_user_turns": None,
    }
    kwargs.update(overrides)
    return runner._note_turn_for_memory_nudge(**kwargs)


# -- counter arithmetic ----------------------------------------------------


def test_fires_on_the_interval_turn_and_not_before():
    r = _Runner(MemoryNudgeConfig(interval=3))
    assert [_note(r) for _ in range(3)] == [False, False, True]


def test_counter_restarts_after_firing():
    r = _Runner(MemoryNudgeConfig(interval=3))
    fired = [_note(r) for _ in range(9)]
    assert fired == [False, False, True] * 3


def test_interval_one_fires_every_turn():
    r = _Runner(MemoryNudgeConfig(interval=1))
    assert all(_note(r) for _ in range(4))


def test_sessions_count_independently():
    r = _Runner(MemoryNudgeConfig(interval=2))
    assert _note(r, session_key="a") is False
    assert _note(r, session_key="b") is False
    assert _note(r, session_key="a") is True
    assert _note(r, session_key="b") is True


def test_agents_count_independently():
    r = _Runner(MemoryNudgeConfig(interval=2))
    assert _note(r, agent_id="main") is False
    assert _note(r, agent_id="ops") is False
    assert _note(r, agent_id="main") is True


# -- disabled paths --------------------------------------------------------


def test_disabled_never_fires():
    r = _Runner(MemoryNudgeConfig(enabled=False, interval=1))
    assert not any(_note(r) for _ in range(5))


def test_zero_interval_disables():
    r = _Runner(MemoryNudgeConfig(interval=0))
    assert not any(_note(r) for _ in range(5))


def test_missing_config_never_fires():
    r = _Runner(None)
    assert not any(_note(r) for _ in range(5))


# -- traffic that must not advance the counter -----------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"input_mode": "system_event"}, id="system_event"),
        pytest.param({"no_memory_capture": True}, id="no_memory_capture"),
        pytest.param({"run_kind": "memory_nudge"}, id="the_review_itself"),
        pytest.param({"run_kind": "cron_turn"}, id="cron"),
        pytest.param({"run_kind": "heartbeat"}, id="heartbeat"),
        pytest.param({"run_kind": "subagent"}, id="subagent"),
    ],
)
def test_machine_traffic_never_fires(overrides: dict[str, Any]):
    """Machine turns say nothing durable about the user.

    Reviewing them would write the harness itself into memory.
    """
    r = _Runner(MemoryNudgeConfig(interval=1))
    assert not any(_note(r, **overrides) for _ in range(5))


def test_excluded_traffic_does_not_even_advance_the_counter():
    """Skipping must not merely suppress the fire -- it must not count.

    Otherwise a burst of cron turns silently consumes the user's interval and
    the next real turn nudges early.
    """
    r = _Runner(MemoryNudgeConfig(interval=3))
    for _ in range(10):
        _note(r, run_kind="cron_turn")
    assert [_note(r) for _ in range(3)] == [False, False, True]


def test_review_cannot_trigger_another_review():
    """The nudge's own run_kind is excluded, so reviews cannot recurse."""
    r = _Runner(MemoryNudgeConfig(interval=1))
    assert _note(r, run_kind="memory_nudge") is False
    assert r._memory_nudge_counters == {}


# -- self-directed writes hold the counter ---------------------------------


@pytest.mark.parametrize("tool_name", ["memory", "memory_save"])
def test_agent_writing_memory_itself_defers_the_review(tool_name: str):
    """A self-directed write delays the review; it must not cancel it.

    Clearing the counter meant a diligent model -- one that saves on most
    turns -- never reached the interval, so the review never ran. A per-turn
    save and the review's whole-conversation sweep are different jobs, and
    only the latter consolidates.
    """
    r = _Runner(MemoryNudgeConfig(interval=3))
    _note(r)
    _note(r)
    # The interval turn itself wrote memory, so the review waits...
    assert _note(r, turn_segments=[{"type": "tool_result", "name": tool_name}]) is False
    # ...but the counter kept advancing, so the next turn fires.
    assert _note(r) is True


@pytest.mark.parametrize("tool_name", ["memory", "memory_save"])
def test_saving_on_every_turn_still_reaches_a_review(tool_name: str):
    """The regression the benchmark caught: 0 reviews across 10 trials.

    Deferral is bounded, so an unbroken run of self-directed writes is
    reviewed at 2x the interval rather than never.
    """
    r = _Runner(MemoryNudgeConfig(interval=3))
    saves = [{"type": "tool_result", "name": tool_name}]

    fired = [_note(r, turn_segments=saves) for _ in range(6)]

    assert fired == [False] * 5 + [True]


def test_unrelated_tool_calls_do_not_reset():
    r = _Runner(MemoryNudgeConfig(interval=2))
    _note(r)
    assert _note(r, turn_segments=[{"type": "tool_result", "name": "shell"}]) is True


def test_memory_search_is_not_a_write():
    """Reading memory is not curating it, so it must not reset the counter."""
    r = _Runner(MemoryNudgeConfig(interval=2))
    _note(r)
    assert _note(r, turn_segments=[{"type": "tool_result", "name": "memory_search"}]) is True


def test_malformed_segments_are_ignored():
    r = _Runner(MemoryNudgeConfig(interval=2))
    _note(r)
    assert _note(r, turn_segments=["not a dict", None, {}, {"name": None}]) is True


# -- eviction --------------------------------------------------------------


def test_forgetting_a_session_drops_its_counter():
    r = _Runner(MemoryNudgeConfig(interval=3))
    _note(r, session_key="a")
    _note(r, session_key="b")
    r.forget_memory_nudge_counter("a")
    assert [k[1] for k in r._memory_nudge_counters] == ["b"]


def test_forgetting_resets_progress_for_that_session():
    r = _Runner(MemoryNudgeConfig(interval=2))
    _note(r, session_key="a")
    r.forget_memory_nudge_counter("a")
    assert _note(r, session_key="a") is False


def test_counter_dict_stays_bounded():
    """Nothing tells the runner a session ended, so the dict must self-bound.

    A gateway running for weeks would otherwise retain one entry per session
    it ever saw.
    """
    from agentos.engine.runtime import _MAX_NUDGE_COUNTERS

    r = _Runner(MemoryNudgeConfig(interval=1000))
    for i in range(_MAX_NUDGE_COUNTERS + 100):
        _note(r, session_key=f"s{i}")
    assert len(r._memory_nudge_counters) <= _MAX_NUDGE_COUNTERS


def test_eviction_keeps_the_most_recent_sessions():
    """Evicting the oldest half costs a re-seed, never a wrong count."""
    from agentos.engine.runtime import _MAX_NUDGE_COUNTERS

    r = _Runner(MemoryNudgeConfig(interval=1000))
    for i in range(_MAX_NUDGE_COUNTERS + 1):
        _note(r, session_key=f"s{i}")
    newest = f"s{_MAX_NUDGE_COUNTERS}"
    assert ("main", newest) in r._memory_nudge_counters


# -- hydration across processes --------------------------------------------


def test_counter_seeds_from_persisted_turns():
    """A rebuilt runner must not restart the session's count from zero.

    Every CLI invocation builds a fresh TurnRunner and the gateway evicts
    idle agents, so without seeding, a session spread across processes would
    never reach the interval and the nudge would never fire at all.
    """
    r = _Runner(MemoryNudgeConfig(interval=3))
    # 5 stored turns: 4 completed before this one, so phase is 4 % 3 == 1.
    # This turn makes 2 -> one more to go, instead of restarting at 1.
    assert _note(r, prior_user_turns=5) is False
    assert _note(r) is True


def test_hydration_can_fire_immediately_when_the_session_is_already_due():
    r = _Runner(MemoryNudgeConfig(interval=3))
    # 3 prior turns means 2 complete cycles' worth of phase -> due now.
    assert _note(r, prior_user_turns=3) is True


def test_hydration_only_seeds_once():
    """After seeding, the live counter owns the session."""
    r = _Runner(MemoryNudgeConfig(interval=5))
    _note(r, prior_user_turns=4)  # seed 3, this turn -> 4
    # A later turn reporting a wildly different count must not re-seed.
    assert _note(r, prior_user_turns=999) is True  # 5 == interval
    assert r._memory_nudge_counters[("main", "s1")] == 0


def test_unknown_prior_turns_starts_from_zero():
    r = _Runner(MemoryNudgeConfig(interval=2))
    assert _note(r, prior_user_turns=None) is False
    assert _note(r) is True


def test_hydration_ignores_nonsense_counts():
    r = _Runner(MemoryNudgeConfig(interval=2))
    assert _note(r, prior_user_turns=0) is False
    r.forget_memory_nudge_counter("s1")
    assert _note(r, prior_user_turns=-5) is False


# -- the review must not pollute the conversation --------------------------


def test_review_turns_are_classified_internal():
    """The review's reply is addressed to the harness, not the user.

    Persisting it would leave stray "Nothing to save." lines in the
    transcript and then replay them as context on the next real turn.
    """
    from agentos.engine.turn_runner.turn_finalizer_stage import _is_internal_turn

    assert _is_internal_turn("memory_nudge") is True


@pytest.mark.parametrize("run_kind", ["default", "cron_turn", "subagent", "heartbeat", ""])
def test_ordinary_turns_are_still_persisted(run_kind: str):
    """Only the nudge is internal -- everything else keeps its transcript."""
    from agentos.engine.turn_runner.turn_finalizer_stage import _is_internal_turn

    assert _is_internal_turn(run_kind) is False


async def test_nudge_counts_even_when_the_transcript_is_not_persisted():
    """`append_message` returns False wherever no session manager is wired.

    Nesting the nudge under that result meant the counter never advanced and
    the review silently never fired in those deployments -- which is exactly
    what CLI runs do.
    """
    from agentos.engine.turn_runner.turn_finalizer_stage import (
        TurnFinalizerStage,
        TurnFinalizerStageInput,
    )

    noted: list[str] = []

    class _NoTranscript:
        async def append_message(self, *_a: Any, **_k: Any) -> bool:
            return False  # no session manager wired

    class _Capture:
        async def capture_turn(self, **_k: Any) -> None:
            raise AssertionError("capture must stay paired with the transcript")

    class _Nudge:
        def note_turn(self, *, run_kind: str, **_k: Any) -> None:
            noted.append(run_kind)

    class _Noop:
        async def persist_error(self, **_k: Any) -> None: ...
        async def roll_up(self, **_k: Any) -> Any:
            return None

    stage = TurnFinalizerStage(
        transcript_append=_NoTranscript(),
        turn_memory_capture=_Capture(),
        session_totals=_Noop(),
        turn_error_persist=_Noop(),
        memory_nudge=_Nudge(),
    )
    await stage.run(
        TurnFinalizerStageInput(
            final_text_parts=["hello"],
            turn_segments=[],
            turn_artifacts=[],
            error_message=None,
            pending_error_event=None,
            done_event=None,
            runtime_message="hi",
            input_mode="user",
            input_provenance=None,
            resolved_model="m",
            agent_id="main",
            session_key="s1",
            tool_context=None,
            run_kind="default",
            heartbeat_ack_max_chars=300,
            no_memory_capture=False,
        )
    )
    assert noted == ["default"]


def test_review_prompt_is_not_persisted_as_user_input():
    """persist_input=False keeps the harness prompt out of the transcript."""
    import inspect

    src = inspect.getsource(TurnRunner._run_memory_nudge_review)
    assert "persist_input=False" in src


# -- prompt ----------------------------------------------------------------


def test_review_prompt_offers_an_explicit_way_to_decline():
    """Without a stated opt-out a review invents an entry to justify itself."""
    from agentos.engine.runtime import _MEMORY_REVIEW_PROMPT

    assert "Nothing to save." in _MEMORY_REVIEW_PROMPT
    assert "memory tool" in _MEMORY_REVIEW_PROMPT


# -- the review reports what it wrote --------------------------------------


def test_review_records_its_writes_instead_of_discarding_them():
    """A silent review is indistinguishable from one that never ran.

    That is not hypothetical: the counter bug above went unnoticed precisely
    because the review's outcome was drained and thrown away.
    """
    import inspect

    src = inspect.getsource(TurnRunner._run_memory_nudge_review)
    assert "memory_nudge.review_done" in src
    assert "_MEMORY_WRITE_TOOL_NAMES" in src


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"target": "user", "action": "add", "content": "Prefers Go"}, "user/add: Prefers Go"),
        ({"action": "add", "content": "no target"}, "memory/add: no target"),
        ({"target": "memory", "action": "remove", "old_text": "stale"}, "memory/remove: stale"),
        ({"target": "memory", "action": "add"}, "memory/add"),
        (None, "write"),
    ],
)
def test_write_summary_renders_target_action_and_content(arguments, expected):
    from agentos.engine.runtime import _summarize_memory_write

    assert _summarize_memory_write(arguments) == expected


def test_write_summary_collapses_a_batch_to_its_operation_count():
    """Naming every op would push one log line past anything readable."""
    from agentos.engine.runtime import _summarize_memory_write

    summary = _summarize_memory_write(
        {
            "target": "memory",
            "operations": [
                {"action": "remove", "old_text": "a"},
                {"action": "remove", "old_text": "b"},
                {"action": "add", "content": "merged"},
            ],
        }
    )

    assert summary == "memory/batch[3 ops: add,remove]"


def test_write_summary_truncates_a_long_entry():
    from agentos.engine.runtime import _summarize_memory_write

    summary = _summarize_memory_write({"action": "add", "content": "x" * 400})

    assert summary.endswith("…")
    assert len(summary) < 200


# -- the review cannot reach past the memory tools ---------------------------


class _ReviewRunner:
    """Just enough of TurnRunner to drive one review turn and capture its context."""

    def __init__(self) -> None:
        self._config = type("C", (), {"memory": _Cfg(MemoryNudgeConfig(enabled=True))})()
        self.contexts: list[Any] = []

    _memory_nudge_config = TurnRunner._memory_nudge_config
    _run_memory_nudge_review = TurnRunner._run_memory_nudge_review

    def _resolve_memory_source_dir(self, agent_id: str) -> str:
        return f"/memory/{agent_id}"

    async def run(self, message: str, session_key: str, **kwargs: Any) -> Any:
        self.contexts.append(kwargs["tool_context"])
        return
        yield  # pragma: no cover - makes this an async generator


async def test_review_turn_is_unattended_and_memory_only():
    """A background review must never be able to raise an approval prompt.

    It inherited the full tool surface, so a model that chose ``apply_patch``
    over the memory tool tripped the out-of-workspace gate and blocked the
    user with an approval for a write they never asked for.
    """
    from agentos.engine.runtime import _MEMORY_REVIEW_TOOL_NAMES
    from agentos.tools.types import InteractionMode

    runner = _ReviewRunner()
    await runner._run_memory_nudge_review(agent_id="main", session_key="s1")

    (ctx,) = runner.contexts
    assert ctx.interaction_mode is InteractionMode.UNATTENDED
    assert ctx.allowed_tools == set(_MEMORY_REVIEW_TOOL_NAMES)
    assert "apply_patch" not in ctx.allowed_tools
    assert "write_file" not in ctx.allowed_tools
    assert ctx.workspace_dir == "/memory/main"
