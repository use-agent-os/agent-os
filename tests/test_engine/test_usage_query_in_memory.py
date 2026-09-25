"""``UsageTracker._query_in_memory`` -- the no-database query path.

Two defects, both in that one function:

* A session whose ``_per_model`` was never populated produced no rows at all, so its
  tokens and cost were reported as zero even though ``SessionUsage`` prices exactly that
  case from ``model_id`` / ``provider_id`` (#3031).
* ``tool_name``, ``start_date`` and ``end_date`` were accepted and then ignored, so a
  filtered query returned every record instead of the matching ones (#3034).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agentos.engine.usage import SessionUsage, UsageTracker


def _day(offset_days: int) -> str:
    return (datetime.now(UTC) + timedelta(days=offset_days)).strftime("%Y-%m-%d")


@pytest.fixture
def tracker() -> UsageTracker:
    return UsageTracker(db_path="")


def _recorded(tracker: UsageTracker) -> None:
    """A session recorded the ordinary way, so it carries per-model detail."""
    tracker.add("session-rec", 100, 50, "gpt-4o", provider_id="openai")


def _with_session_totals(tracker: UsageTracker) -> None:
    tracker._sessions["session-123"] = SessionUsage(
        input_tokens=1500,
        output_tokens=300,
        model_id="gpt-4o",
        provider_id="openai",
    )


def test_a_session_without_per_model_detail_is_still_reported(tracker: UsageTracker) -> None:
    _with_session_totals(tracker)

    rows = tracker.query_usage(session_key="session-123")

    assert len(rows) == 1
    row = rows[0]
    assert row["inputTokens"] == 1500
    assert row["outputTokens"] == 300
    assert row["model"] == "gpt-4o"
    assert row["provider"] == "openai"
    assert row["sessionKey"] == "session-123"


def test_the_reported_cost_matches_the_session(tracker: UsageTracker) -> None:
    _with_session_totals(tracker)

    row = tracker.query_usage(session_key="session-123")[0]

    assert row["costUsd"] == tracker._sessions["session-123"].cost


def test_per_model_sessions_still_produce_one_row_per_model(tracker: UsageTracker) -> None:
    tracker.add("session-abc", 100, 50, "gpt-4o", provider_id="openai")
    tracker.add("session-abc", 10, 5, "claude-opus-5", provider_id="anthropic")

    rows = tracker.query_usage(session_key="session-abc")

    assert sorted(r["model"] for r in rows) == ["claude-opus-5", "gpt-4o"]


def test_a_tool_name_filter_matches_nothing_rather_than_everything(tracker: UsageTracker) -> None:
    """In-memory rows are per-model turn totals; none carries a tool name."""
    _recorded(tracker)

    assert tracker.query_usage() != []
    assert tracker.query_usage(tool_name="bash") == []


def test_a_start_date_in_the_future_excludes_current_records(tracker: UsageTracker) -> None:
    _recorded(tracker)

    assert tracker.query_usage(start_date=_day(1)) == []


def test_an_end_date_in_the_past_excludes_current_records(tracker: UsageTracker) -> None:
    _recorded(tracker)

    assert tracker.query_usage(end_date=_day(-1)) == []


def test_a_date_range_covering_today_keeps_the_records(tracker: UsageTracker) -> None:
    _recorded(tracker)

    rows = tracker.query_usage(start_date=_day(-1), end_date=_day(1))

    assert len(rows) == 1


def test_an_unparsable_date_is_ignored_as_in_the_sql_path(tracker: UsageTracker) -> None:
    _recorded(tracker)

    assert len(tracker.query_usage(start_date="not-a-date")) == 1


def test_other_filters_still_apply(tracker: UsageTracker) -> None:
    _with_session_totals(tracker)

    assert tracker.query_usage(session_key="other") == []
    assert len(tracker.query_usage(session_key="session-123")) == 1
