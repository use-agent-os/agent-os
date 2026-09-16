"""Issue #2399: TurnRunner's two compaction dicts were never bounded.

``_memory_snapshots`` and ``_bootstrap_snapshots``, declared in the same block,
are ``BoundedRegistry`` precisely because "nothing notifies this runner when a
session ends" — the phrase ``_evict_nudge_counters_if_needed`` uses about
itself. ``_compaction_failures`` and ``_emergency_compaction_overrides`` were
plain ``dict``, and each has exactly one removal path that a dropped session
never reaches:

* ``_compaction_failures`` is popped only by ``_record_compaction_success`` —
  never on failure, which is the only way an entry gets created.
* ``_emergency_compaction_overrides`` is popped only by the *next* turn's
  history load — never if there is no next turn.

So a session that fails compaction once, or takes emergency compaction once,
and is then abandoned (one-off chat, cron or subagent session, client
disconnect) held its entry for the life of the process. The override is the
worse of the two: it carries ``kept_entries``, a full kept-transcript slice, so
that leak scales with conversation size and not merely session count.

Both are now ``BoundedRegistry`` with ``session_of``, which caps them and — the
part a plain size cap would not give — makes them visible to
``drop_session_state``, the process-wide reaper that already clears the sibling
registries when a session is known to be over.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agentos.engine import runtime as runtime_module
from agentos.engine.runtime import TurnRunner
from agentos.util.bounded_registry import (
    BoundedRegistry,
    drop_session_state,
    registry_limits,
    registry_stats,
)

_COMPACTION_FAILURE_LIMIT = runtime_module._COMPACTION_FAILURE_LIMIT
_COOLDOWN = runtime_module._COMPACTION_CIRCUIT_COOLDOWN_SECONDS


@pytest.fixture
def runner() -> TurnRunner:
    return TurnRunner(provider_selector=MagicMock(), session_manager=None)


def override(text: str = "kept") -> runtime_module._EmergencyCompactionOverride:
    return runtime_module._EmergencyCompactionOverride(
        summary="Emergency request-scoped compaction",
        kept_entries=[{"role": "user", "content": text}],
        reason="context_overflow",
        compaction_id="c-1",
    )


# ── both fields are bounded, like their siblings ────────────────────────────


@pytest.mark.parametrize(
    "field",
    ["_compaction_failures", "_emergency_compaction_overrides"],
)
def test_the_field_is_a_bounded_registry(runner: TurnRunner, field: str) -> None:
    assert isinstance(getattr(runner, field), BoundedRegistry)


@pytest.mark.parametrize(
    "field",
    ["_compaction_failures", "_emergency_compaction_overrides"],
)
def test_the_field_is_named_and_session_scoped(runner: TurnRunner, field: str) -> None:
    """``session_of`` is what ``drop_session_state`` needs; without it a
    registry is capped but cannot answer "which entries are this session's"."""
    registry = getattr(runner, field)

    assert registry.name == f"TurnRunner.{field}"
    assert registry.discard_session("nobody") == 0  # answers, rather than no-ops


def test_both_appear_in_registry_stats(runner: TurnRunner) -> None:
    """Being in the weak registry table is how an operator sees the sizes."""
    names = {row["name"] for row in registry_stats()}

    assert "TurnRunner._compaction_failures" in names
    assert "TurnRunner._emergency_compaction_overrides" in names


# ── the leak itself ─────────────────────────────────────────────────────────


def test_abandoned_failing_sessions_do_not_accumulate(runner: TurnRunner) -> None:
    """Each session fails compaction once and is never seen again."""
    cap = registry_limits().session_max_entries

    for index in range(cap * 4):
        runner._record_compaction_failure(f"user:sess-{index}")

    assert len(runner._compaction_failures) <= cap


def test_abandoned_emergency_overrides_do_not_accumulate(runner: TurnRunner) -> None:
    """The heavier one: every entry holds a transcript slice."""
    cap = registry_limits().session_max_entries

    for index in range(cap * 4):
        runner._emergency_compaction_overrides[f"user:sess-{index}"] = override()

    assert len(runner._emergency_compaction_overrides) <= cap


def test_a_still_active_session_is_not_evicted_by_churn(runner: TurnRunner) -> None:
    """Eviction is least-recently-used, so the session actually being served
    keeps its circuit-breaker state while one-off sessions churn past it.

    This is the property that makes capping safe rather than merely small: if
    eviction were arbitrary, a busy session could lose its failure count and
    keep retrying a compaction that cannot succeed.
    """
    cap = registry_limits().session_max_entries
    live = "user:live"
    runner._record_compaction_failure(live)

    for index in range(cap * 2):
        runner._record_compaction_failure(f"user:throwaway-{index}")
        runner._compaction_failures.get(live)  # the live session keeps being served

    assert live in runner._compaction_failures


@pytest.mark.parametrize(
    "field",
    ["_compaction_failures", "_emergency_compaction_overrides"],
)
def test_drop_session_state_now_reaches_both(runner: TurnRunner, field: str) -> None:
    """The reason ``session_of`` matters. Before this change the process-wide
    reaper cleared the snapshot registries and left these two untouched."""
    runner._record_compaction_failure("user:doomed")
    runner._emergency_compaction_overrides["user:doomed"] = override()
    runner._record_compaction_failure("user:other")
    runner._emergency_compaction_overrides["user:other"] = override()

    drop_session_state("user:doomed")

    registry = getattr(runner, field)
    assert "user:doomed" not in registry
    assert "user:other" in registry, "only the named session's state should go"


# ── the circuit breaker still behaves exactly as before ─────────────────────


def test_failures_accumulate_per_session(runner: TurnRunner) -> None:
    runner._record_compaction_failure("user:a")
    runner._record_compaction_failure("user:a")
    runner._record_compaction_failure("user:b")

    assert runner._compaction_failures["user:a"].count == 2
    assert runner._compaction_failures["user:b"].count == 1


def test_the_circuit_stays_closed_below_the_limit(runner: TurnRunner) -> None:
    for _ in range(_COMPACTION_FAILURE_LIMIT - 1):
        runner._record_compaction_failure("user:a")

    assert runner._compaction_circuit_open("user:a") is False


def test_the_circuit_opens_at_the_limit(runner: TurnRunner) -> None:
    for _ in range(_COMPACTION_FAILURE_LIMIT):
        runner._record_compaction_failure("user:a")

    assert runner._compaction_circuit_open("user:a") is True


def test_the_circuit_half_opens_after_the_cooldown(runner: TurnRunner) -> None:
    for _ in range(_COMPACTION_FAILURE_LIMIT):
        runner._record_compaction_failure("user:a")
    runner._compaction_failures["user:a"].opened_at = (
        runtime_module.time.monotonic() - _COOLDOWN - 1
    )

    assert runner._compaction_circuit_open("user:a") is False


def test_an_unknown_session_has_a_closed_circuit(runner: TurnRunner) -> None:
    assert runner._compaction_circuit_open("user:never-seen") is False


def test_success_clears_the_failure_state(runner: TurnRunner) -> None:
    for _ in range(_COMPACTION_FAILURE_LIMIT):
        runner._record_compaction_failure("user:a")

    runner._record_compaction_success("user:a")

    assert "user:a" not in runner._compaction_failures
    assert runner._compaction_circuit_open("user:a") is False


def test_success_for_an_unseen_session_is_a_no_op(runner: TurnRunner) -> None:
    """``pop`` on a BoundedRegistry raises without a default; the call site
    passes one, and this pins that it still does."""
    runner._record_compaction_success("user:never-seen")  # must not raise


def test_recording_a_failure_does_not_replace_the_registry(runner: TurnRunner) -> None:
    """``_record_compaction_failure`` used to re-create the field as ``{}``
    behind a ``hasattr`` guard. Left in place, that would silently swap the
    bounded registry back out for the dict this change removed.
    """
    runner._record_compaction_failure("user:a")

    assert isinstance(runner._compaction_failures, BoundedRegistry)


# ── the emergency override is still consumed exactly once ───────────────────


def test_an_override_is_consumed_by_the_read(runner: TurnRunner) -> None:
    """It is request-scoped: the next history load takes it and it is gone."""
    runner._emergency_compaction_overrides["user:a"] = override("first")

    taken = runner._emergency_compaction_overrides.pop("user:a", None)
    again = runner._emergency_compaction_overrides.pop("user:a", None)

    assert taken is not None
    assert taken.kept_entries == [{"role": "user", "content": "first"}]
    assert again is None


def test_reading_an_absent_override_returns_none(runner: TurnRunner) -> None:
    assert runner._emergency_compaction_overrides.pop("user:never-seen", None) is None


def test_overrides_are_kept_apart_per_session(runner: TurnRunner) -> None:
    runner._emergency_compaction_overrides["user:a"] = override("a")
    runner._emergency_compaction_overrides["user:b"] = override("b")

    assert runner._emergency_compaction_overrides["user:a"].kept_entries[0]["content"] == "a"
    assert runner._emergency_compaction_overrides["user:b"].kept_entries[0]["content"] == "b"


def test_a_second_override_replaces_the_first_for_a_session(runner: TurnRunner) -> None:
    runner._emergency_compaction_overrides["user:a"] = override("old")
    runner._emergency_compaction_overrides["user:a"] = override("new")

    assert len(runner._emergency_compaction_overrides) == 1
    assert runner._emergency_compaction_overrides["user:a"].kept_entries[0]["content"] == "new"


# ── eviction degrades safely ────────────────────────────────────────────────


def test_losing_a_failure_entry_only_re_arms_the_circuit(runner: TurnRunner) -> None:
    """What an eviction costs, stated as a test.

    The count is a heuristic guard, not correctness state: dropping it means
    the session gets its retries again, which is the same position it was in
    before it ever failed — not a crash and not data loss.
    """
    for _ in range(_COMPACTION_FAILURE_LIMIT):
        runner._record_compaction_failure("user:a")
    assert runner._compaction_circuit_open("user:a") is True

    runner._compaction_failures.discard("user:a")

    assert runner._compaction_circuit_open("user:a") is False
    runner._record_compaction_failure("user:a")
    assert runner._compaction_failures["user:a"].count == 1


def test_losing_an_override_falls_back_to_the_untrimmed_transcript(
    runner: TurnRunner,
) -> None:
    """The override is an optimisation applied to the next turn's history load.
    Without it the caller reads the transcript it already has, which is correct
    if larger — so eviction costs context budget, never content.
    """
    runner._emergency_compaction_overrides["user:a"] = override()

    runner._emergency_compaction_overrides.discard("user:a")

    assert runner._emergency_compaction_overrides.pop("user:a", None) is None
