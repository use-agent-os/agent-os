"""Issue #2472: a timezone-scheduled cron job mis-fires across a DST boundary.

``_next_run`` matched a cron expression by walking UTC minute by minute and
converting each candidate to wall time in ``job.tz``. Every UTC minute is
distinct, but a **wall-clock time is not**:

* **Fall back** — the hour repeats, so two different UTC minutes both render as
  the scheduled local time and a *daily* job fired **twice**.
* **Spring forward** — the hour is skipped, so no UTC minute renders as the
  scheduled local time and a daily job was **silently skipped** for that day.

Both were silent; nothing logged, the run count just came out wrong. It matters
more here than for an ordinary cron daemon because the payload is an agent
turn: a duplicate fire sends the message twice, spends tokens twice and writes
to the session twice, and a skipped fire means a digest or watcher never ran.

The resolutions are the conventional ones. An ambiguous local time fires on its
**first** occurrence only (``fold == 0``). A local time that does not exist
fires once at the **first instant after the gap**.

UTC-scheduled jobs are untouched: UTC has no transitions, so that path keeps
the original single comparison.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agentos.scheduler.jobs import _next_run, _skipped_wall_minutes
from agentos.scheduler.types import CronJob, ScheduleKind

NY = ZoneInfo("America/New_York")
BERLIN = ZoneInfo("Europe/Berlin")
SYDNEY = ZoneInfo("Australia/Sydney")


def job(expr: str, tz: str = "") -> CronJob:
    return CronJob(
        id="j",
        name="n",
        cron_expr=expr,
        tz=tz,
        schedule_kind=ScheduleKind.CRON,
        jitter_seconds=0,
    )


def fires_between(j: CronJob, start: datetime, end: datetime, limit: int = 60) -> list[datetime]:
    """Every fire time in ``[start, end)``, driven the way the timer drives it."""
    out: list[datetime] = []
    cursor = start
    while len(out) < limit:
        nxt = _next_run(j, cursor)
        if nxt >= end:
            return out
        out.append(nxt)
        cursor = nxt
    raise AssertionError("runaway schedule")


def locals_of(fires: list[datetime], zone: ZoneInfo) -> list[str]:
    return [f.astimezone(zone).strftime("%Y-%m-%d %H:%M") for f in fires]


# ── fall back: the ambiguous hour ───────────────────────────────────────────


def test_a_daily_job_fires_once_on_the_fall_back_night() -> None:
    """2026-11-01 America/New_York: 02:00 EDT becomes 01:00 EST, so 01:30
    happens twice. The job used to fire on both."""
    fires = fires_between(
        job("30 1 * * *", "America/New_York"),
        datetime(2026, 10, 31, 12, 0, tzinfo=UTC),
        datetime(2026, 11, 2, 12, 0, tzinfo=UTC),
    )

    assert locals_of(fires, NY) == ["2026-11-01 01:30", "2026-11-02 01:30"]


def test_the_surviving_fall_back_fire_is_the_first_occurrence() -> None:
    """Of the two instants that render as 01:30, the earlier one is kept —
    a job scheduled for 01:30 should run when 01:30 first arrives."""
    fires = fires_between(
        job("30 1 * * *", "America/New_York"),
        datetime(2026, 11, 1, 3, 0, tzinfo=UTC),
        datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
    )

    assert fires == [datetime(2026, 11, 1, 5, 30, tzinfo=UTC)]
    assert fires[0].astimezone(NY).fold == 0


def test_an_hourly_job_does_not_repeat_the_ambiguous_hour() -> None:
    """``0 * * * *`` legitimately fires every hour, and the repeated 01:00 is
    the one that must not be doubled."""
    fires = fires_between(
        job("0 * * * *", "America/New_York"),
        datetime(2026, 11, 1, 3, 30, tzinfo=UTC),
        datetime(2026, 11, 1, 9, 0, tzinfo=UTC),
    )

    stamps = locals_of(fires, NY)
    assert len(stamps) == len(set(stamps)), f"a local hour fired twice: {stamps}"


def test_berlin_fall_back_is_handled_too() -> None:
    """Not a US-only rule: Europe transitions on a different date."""
    fires = fires_between(
        job("30 2 * * *", "Europe/Berlin"),
        datetime(2026, 10, 24, 12, 0, tzinfo=UTC),
        datetime(2026, 10, 26, 12, 0, tzinfo=UTC),
    )

    assert locals_of(fires, BERLIN) == ["2026-10-25 02:30", "2026-10-26 02:30"]


def test_southern_hemisphere_fall_back_is_handled() -> None:
    """Australia moves the other way round the calendar."""
    fires = fires_between(
        job("30 2 * * *", "Australia/Sydney"),
        datetime(2026, 4, 3, 0, 0, tzinfo=UTC),
        datetime(2026, 4, 6, 0, 0, tzinfo=UTC),
    )

    stamps = locals_of(fires, SYDNEY)
    assert len(stamps) == len(set(stamps)), f"a local time fired twice: {stamps}"


# ── spring forward: the hour that never happens ─────────────────────────────


def test_a_daily_job_still_runs_on_the_spring_forward_day() -> None:
    """2027-03-14 America/New_York: 02:00 jumps to 03:00, so 02:30 never
    exists. The job used to skip the day entirely."""
    fires = fires_between(
        job("30 2 * * *", "America/New_York"),
        datetime(2027, 3, 13, 12, 0, tzinfo=UTC),
        datetime(2027, 3, 16, 12, 0, tzinfo=UTC),
    )

    assert locals_of(fires, NY) == [
        "2027-03-14 03:00",
        "2027-03-15 02:30",
        "2027-03-16 02:30",
    ]


def test_the_shifted_fire_lands_at_the_first_instant_after_the_gap() -> None:
    fires = fires_between(
        job("30 2 * * *", "America/New_York"),
        datetime(2027, 3, 14, 0, 0, tzinfo=UTC),
        datetime(2027, 3, 14, 23, 0, tzinfo=UTC),
    )

    assert fires == [datetime(2027, 3, 14, 7, 0, tzinfo=UTC)]


def test_a_schedule_outside_the_gap_is_not_shifted() -> None:
    """01:30 exists on the spring-forward morning, so it must fire at 01:30 —
    the gap handling must not drag unrelated schedules forward."""
    fires = fires_between(
        job("30 1 * * *", "America/New_York"),
        datetime(2027, 3, 13, 12, 0, tzinfo=UTC),
        datetime(2027, 3, 15, 12, 0, tzinfo=UTC),
    )

    assert locals_of(fires, NY) == ["2027-03-14 01:30", "2027-03-15 01:30"]


def test_the_gap_fires_only_once_even_with_several_matches_inside_it() -> None:
    """``*/10 2 * * *`` names six minutes inside the skipped hour. The day owes
    one run, not six."""
    fires = fires_between(
        job("*/10 2 * * *", "America/New_York"),
        datetime(2027, 3, 14, 0, 0, tzinfo=UTC),
        datetime(2027, 3, 14, 23, 0, tzinfo=UTC),
    )

    assert len(fires) == 1
    assert fires[0] == datetime(2027, 3, 14, 7, 0, tzinfo=UTC)


def test_berlin_spring_forward_is_handled_too() -> None:
    fires = fires_between(
        job("30 2 * * *", "Europe/Berlin"),
        datetime(2027, 3, 27, 12, 0, tzinfo=UTC),
        datetime(2027, 3, 29, 12, 0, tzinfo=UTC),
    )

    assert len(fires) == 2, locals_of(fires, BERLIN)


# ── UTC and untimed schedules are untouched ─────────────────────────────────


def test_a_utc_job_is_unaffected() -> None:
    fires = fires_between(
        job("30 1 * * *"),
        datetime(2026, 10, 31, 12, 0, tzinfo=UTC),
        datetime(2026, 11, 3, 12, 0, tzinfo=UTC),
    )

    assert fires == [
        datetime(2026, 11, 1, 1, 30, tzinfo=UTC),
        datetime(2026, 11, 2, 1, 30, tzinfo=UTC),
        datetime(2026, 11, 3, 1, 30, tzinfo=UTC),
    ]


def test_a_utc_job_on_a_transition_date_still_fires_once() -> None:
    """The transition is only a transition somewhere; UTC has none."""
    fires = fires_between(
        job("0 * * * *"),
        datetime(2026, 11, 1, 0, 30, tzinfo=UTC),
        datetime(2026, 11, 1, 6, 0, tzinfo=UTC),
    )

    assert len(fires) == 5


@pytest.mark.parametrize(
    ("expr", "tz"),
    [
        ("*/5 * * * *", "America/New_York"),
        ("0 9 * * 1-5", "Europe/Berlin"),
        ("0 0 1 * *", "Australia/Sydney"),
        ("15 3 * * 0", ""),
    ],
)
def test_ordinary_schedules_still_advance(expr: str, tz: str) -> None:
    """Every fire strictly after the last, on an ordinary week."""
    j = job(expr, tz)
    cursor = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    for _ in range(5):
        nxt = _next_run(j, cursor)
        assert nxt > cursor
        cursor = nxt


def test_jitter_is_still_applied() -> None:
    j = job("30 1 * * *", "America/New_York")
    j.jitter_seconds = 17

    nxt = _next_run(j, datetime(2026, 6, 1, 0, 0, tzinfo=UTC))

    assert nxt.second == 17


# ── the gap helper itself ───────────────────────────────────────────────────


def test_no_gap_between_consecutive_minutes() -> None:
    a = datetime(2026, 6, 1, 1, 0, tzinfo=NY)
    assert _skipped_wall_minutes(a, a + timedelta(minutes=1)) == []


def test_no_gap_when_the_clock_goes_backwards() -> None:
    """A fall-back moves local time backwards; that is not a gap, and treating
    it as one would fire every schedule in the repeated hour."""
    later = datetime(2026, 11, 1, 1, 30, tzinfo=NY)
    earlier = datetime(2026, 11, 1, 1, 0, tzinfo=NY)

    assert _skipped_wall_minutes(later, earlier) == []


def test_the_gap_lists_the_minutes_that_never_happened() -> None:
    previous = datetime(2027, 3, 14, 1, 59, tzinfo=NY)
    current = datetime(2027, 3, 14, 3, 0, tzinfo=NY)

    missing = _skipped_wall_minutes(previous, current)

    assert missing[0] == datetime(2027, 3, 14, 2, 0)
    assert missing[-1] == datetime(2027, 3, 14, 2, 59)
    assert len(missing) == 60


def test_the_gap_is_bounded() -> None:
    """A malformed zone must not turn one tick into an unbounded inner loop."""
    previous = datetime(2026, 1, 1, 0, 0, tzinfo=NY)
    current = datetime(2030, 1, 1, 0, 0, tzinfo=NY)

    assert len(_skipped_wall_minutes(previous, current)) == 24 * 60


def test_no_previous_wall_is_not_a_gap() -> None:
    assert _skipped_wall_minutes(None, datetime(2026, 6, 1, 0, 0, tzinfo=NY)) == []
