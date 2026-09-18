"""The tracker's per-session state is bounded, each field by the rule that fits it.

#1131 migrated `_scopes` and `_session_metadata`; `_sessions`, `_session_spend`
and `_session_active_skill` stayed plain dicts. `UsageTracker` is a process
global, so a gateway serving many short sessions grew three entries per session
key it had ever seen, and `drop_session_state()` -- which the gateway calls on
every terminal event -- dropped half the tracker's state and kept the rest.

The two halves need different rules, so this pins both:

* `_session_spend` and `_session_active_skill` describe a session while it is
  live, so the terminal event drops them.
* `_sessions` must **survive** the terminal event. `rpc_usage` reads it to give
  a session its per-model breakdown, which disk persistence does not record, so
  dropping it at session end is the regression `_append_tracker_only_rows`
  exists to prevent. It is bounded by TTL and a ceiling instead.
"""

from __future__ import annotations

import pytest

from agentos.engine.usage import UsageTracker
from agentos.util.bounded_registry import BoundedRegistry, drop_session_state

KEY = "agent:main:main"


@pytest.fixture
def tracker() -> UsageTracker:
    return UsageTracker()


def _spend(tracker: UsageTracker, session_key: str, cost: float = 0.25) -> None:
    tracker.add(
        session_key,
        1000,
        500,
        model_id="claude-opus-5",
        provider_id="anthropic",
        billed_cost=cost,
    )


def test_a_terminal_event_drops_the_session_spend_mirror(tracker) -> None:
    _spend(tracker, KEY)
    assert KEY in tracker._session_spend

    drop_session_state(KEY)

    assert KEY not in tracker._session_spend


def test_a_terminal_event_drops_the_active_skill(tracker) -> None:
    """Written from ``skill_tools`` while a skill runs; meaningless after."""
    _spend(tracker, KEY)
    tracker._session_active_skill[KEY] = "pdf-toolkit"

    drop_session_state(KEY)

    assert KEY not in tracker._session_active_skill


def test_three_hundred_ended_sessions_leave_no_live_session_state(tracker) -> None:
    """The leak, for the two fields the terminal event owns."""
    keys = [f"agent:main:s{i}" for i in range(300)]
    for key in keys:
        _spend(tracker, key)
        tracker._session_active_skill[key] = "xlsx"
    assert len(tracker._session_spend) == 300

    for key in keys:
        drop_session_state(key)

    assert len(tracker._session_spend) == 0
    assert len(tracker._session_active_skill) == 0


def test_the_usage_rows_survive_the_terminal_event(tracker) -> None:
    """Deliberate: dropping this at session end is the regression to avoid.

    ``_append_tracker_only_rows`` merges these in to give a session its
    per-model breakdown, which disk persistence does not record.
    """
    _spend(tracker, KEY)

    drop_session_state(KEY)

    assert KEY in tracker._sessions
    assert KEY in tracker.all_sessions()


def test_the_usage_rows_are_bounded_by_a_ceiling_instead(tracker) -> None:
    """Which is what keeps the surviving map from growing without limit."""
    ceiling = tracker._sessions.max_entries

    for i in range(ceiling + 200):
        _spend(tracker, f"agent:main:s{i}")

    assert len(tracker._sessions) <= ceiling
    assert tracker._sessions.ttl_seconds is not None


def test_the_already_migrated_fields_are_still_evicted(tracker) -> None:
    """Guard on the #1131 behaviour this change builds on."""
    _spend(tracker, KEY)
    assert KEY in tracker._session_metadata

    drop_session_state(KEY)

    assert KEY not in tracker._session_metadata


def test_every_per_session_field_is_the_shared_primitive(tracker) -> None:
    for name in (
        "_sessions",
        "_scopes",
        "_session_metadata",
        "_session_spend",
        "_session_active_skill",
    ):
        assert isinstance(getattr(tracker, name), BoundedRegistry), name


def test_recording_and_reading_usage_still_works(tracker) -> None:
    """Guard: the accounting surface is unchanged by the container swap."""
    tracker.add(KEY, 1000, 500, model_id="claude-opus-5", provider_id="anthropic", billed_cost=0.25)
    tracker.add(KEY, 200, 100, model_id="claude-opus-5", provider_id="anthropic", billed_cost=0.10)

    usage = tracker.all_sessions()[KEY]

    assert isinstance(tracker.all_sessions(), dict)
    assert usage.input_tokens == 1200
    assert usage.output_tokens == 600
    assert tracker.get_effective_session_cost(KEY) == pytest.approx(0.35)


def test_the_effective_cost_falls_back_once_the_session_has_ended(tracker) -> None:
    """Guard: the spend mirror going away must not raise or report nonsense.

    ``get_effective_session_cost`` already reads the ledger when the mirror has
    no entry -- that is the path a session resumed after a restart takes.
    """
    _spend(tracker, KEY, cost=0.25)
    assert tracker.get_effective_session_cost(KEY) == pytest.approx(0.25)

    drop_session_state(KEY)

    assert tracker.get_effective_session_cost(KEY) >= 0.0
