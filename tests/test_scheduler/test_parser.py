"""Cron parser surface: parse_cron acceptance/rejection + parse_iso_at."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agentos.scheduler.parser import CronParseError, parse_cron, parse_iso_at

# --- parse_cron ----------------------------------------------------------


def test_parse_cron_accepts_basic_five_field() -> None:
    assert parse_cron("*/5 * * * *").raw == "*/5 * * * *"


def test_parse_cron_accepts_named_dow_and_month() -> None:
    assert parse_cron("0 9 * * 1-5").raw == "0 9 * * 1-5"
    assert parse_cron("30 8 1 jan *").raw == "30 8 1 jan *"


def test_parse_cron_names_are_case_insensitive() -> None:
    # POSIX: month and day-of-week names are case-insensitive. The parser used
    # to substitute only all-lowercase and all-uppercase spellings, so the
    # common "Mon-Fri" business-hours schedule was rejected outright.
    assert parse_cron("0 9 * * Mon-Fri").day_of_week.values == frozenset({1, 2, 3, 4, 5})
    assert parse_cron("0 9 * * MON-FRI").day_of_week.values == frozenset({1, 2, 3, 4, 5})
    assert parse_cron("0 0 * * Mon,Wed,Fri").day_of_week.values == frozenset({1, 3, 5})
    assert parse_cron("0 9 * Jan *").month.values == frozenset({1})
    assert parse_cron("0 9 * JAN *").month.values == frozenset({1})
    assert parse_cron("0 0 * Jan-Mar *").month.values == frozenset({1, 2, 3})
    assert parse_cron("0 0 * JAN-MAR/2 *").month.values == frozenset({1, 3})


def test_parse_cron_accepts_preset_alias() -> None:
    assert parse_cron("@hourly").raw == "0 * * * *"


def test_parse_cron_rejects_wrong_field_count() -> None:
    with pytest.raises(CronParseError, match="Expected 5 fields"):
        parse_cron("0 9 * *")


def test_parse_cron_rejects_out_of_range_value() -> None:
    with pytest.raises(CronParseError, match="out of range"):
        parse_cron("0 25 * * *")


def test_parse_cron_rejects_garbage() -> None:
    with pytest.raises(CronParseError):
        parse_cron("not-a-cron")


def test_parse_cron_accepts_dow_7_as_sunday() -> None:
    # POSIX permits either 0 or 7 to mean Sunday in the day-of-week field.
    expr = parse_cron("0 0 * * 7")
    assert expr.day_of_week.values == frozenset({0})


def test_parse_cron_dow_ranges_may_end_at_7() -> None:
    # With Sunday spellable as 7, a "WED-SUN" style range is valid and must
    # resolve to the same weekday set as its 0-terminated equivalent.
    expr = parse_cron("0 0 * * WED-7")
    assert expr.day_of_week.values == frozenset({0, 3, 4, 5, 6})


def test_parse_cron_dow_7_dedups_with_0_and_names() -> None:
    assert parse_cron("0 0 * * 0,7").day_of_week.values == frozenset({0})
    assert parse_cron("0 0 * * MON,7").day_of_week.values == frozenset({0, 1})


def test_parse_cron_dow_7_matches_sunday_not_monday() -> None:
    expr = parse_cron("0 0 * * 7")
    sunday = datetime(2026, 8, 30, 0, 0)  # a Sunday
    monday = datetime(2026, 8, 31, 0, 0)  # the next Monday
    assert expr.matches(sunday)
    assert not expr.matches(monday)

def test_parse_cron_dom_and_dow_both_restricted_use_or_not_and() -> None:
    # POSIX: "if both fields are restricted (i.e., aren't *), the command
    # will be run when either field matches the current time." Before this
    # fix, matches() ANDed all five fields unconditionally, so a schedule
    # like "1st of the month OR every Friday" instead required the 1st to
    # *also* be a Friday -- true almost never, silently breaking the
    # schedule for the entire month in most cases.
    expr = parse_cron("0 0 1,15 * 5")

    # A Friday that is neither the 1st nor the 15th: should fire because
    # day-of-week matches, even though day-of-month does not.
    friday_not_1_or_15 = datetime(2026, 8, 7, 0, 0)
    assert friday_not_1_or_15.weekday() == 4  # sanity: is a Friday
    assert expr.matches(friday_not_1_or_15)

    # The 1st of a month that is not a Friday: should fire because
    # day-of-month matches, even though day-of-week does not.
    first_not_friday = datetime(2026, 9, 1, 0, 0)
    assert first_not_friday.weekday() != 4  # sanity: not a Friday
    assert expr.matches(first_not_friday)

    # Neither the 1st/15th nor a Friday: should not fire.
    neither = datetime(2026, 8, 4, 0, 0)  # a Tuesday
    assert neither.weekday() != 4
    assert neither.day not in (1, 15)
    assert not expr.matches(neither)


def test_parse_cron_dom_wildcard_dow_restricted_uses_and() -> None:
    # When only day-of-week is restricted (day-of-month is "*"), the
    # combination is a plain AND: this reduces to "every Friday", not
    # "every day OR every Friday".
    expr = parse_cron("0 0 * * 5")
    friday = datetime(2026, 8, 7, 0, 0)
    saturday = datetime(2026, 8, 8, 0, 0)
    assert expr.matches(friday)
    assert not expr.matches(saturday)


def test_parse_cron_dow_wildcard_dom_restricted_uses_and() -> None:
    # When only day-of-month is restricted (day-of-week is "*"), the
    # combination is a plain AND: this reduces to "the 1st of the month".
    expr = parse_cron("0 0 1 * *")
    first = datetime(2026, 9, 1, 0, 0)
    second = datetime(2026, 9, 2, 0, 0)
    assert expr.matches(first)
    assert not expr.matches(second)


def test_parse_cron_dom_explicit_full_range_is_not_a_wildcard() -> None:
    # POSIX ties the OR-vs-AND rule to a literal "*" in the source text, not
    # to whether the resolved value set happens to span the whole range.
    # An explicit "1-31" is "restricted" even though it matches every day.
    expr = parse_cron("0 0 1-31 * 5")
    assert not expr.day_of_month.is_wildcard
    # OR semantics still apply: any day matches via day_of_month regardless
    # of day_of_week, since 1-31 covers every possible day number.
    tuesday = datetime(2026, 8, 4, 0, 0)
    assert tuesday.weekday() != 4
    assert expr.matches(tuesday)



def test_parse_cron_rejects_unknown_preset() -> None:
    with pytest.raises(CronParseError, match="Unknown preset"):
        parse_cron("@bogus")


def test_parse_cron_rejects_reversed_range_with_step() -> None:
    # A reversed range in the step branch used to parse into an *empty* field
    # set, so the expression validated, stored, and then matched nothing —
    # _next_run would burn through its whole scan window and raise
    # "No valid next run found" at job creation. Reject it up front like the
    # plain-range branch already does.
    with pytest.raises(CronParseError, match="Range start > end"):
        parse_cron("5-3/2 * * * *")
    with pytest.raises(CronParseError, match="Range start > end"):
        parse_cron("0 0 * * FRI-TUE/2")
    with pytest.raises(CronParseError, match="Range start > end"):
        parse_cron("0 0 * dec-feb/2 *")


# --- parse_iso_at --------------------------------------------------------


def test_parse_iso_at_accepts_offset() -> None:
    dt = parse_iso_at("2026-05-15T09:00:00+08:00")
    assert dt.tzinfo is not None
    assert dt.year == 2026 and dt.hour == 9


def test_parse_iso_at_accepts_z_suffix() -> None:
    dt = parse_iso_at("2026-05-15T01:00:00Z")
    assert dt.tzinfo is not None
    assert dt.astimezone(UTC) == datetime(2026, 5, 15, 1, 0, tzinfo=UTC)


def test_parse_iso_at_rejects_naive_datetime() -> None:
    with pytest.raises(CronParseError, match="timezone"):
        parse_iso_at("2026-05-15T09:00:00")


def test_parse_iso_at_rejects_garbage() -> None:
    with pytest.raises(CronParseError, match="Invalid ISO-8601"):
        parse_iso_at("not-a-timestamp")


def test_parse_iso_at_rejects_empty() -> None:
    with pytest.raises(CronParseError, match="must not be empty"):
        parse_iso_at("   ")


def test_parse_iso_at_rejects_non_string() -> None:
    with pytest.raises(CronParseError, match="Expected ISO-8601 string"):
        parse_iso_at(12345)  # type: ignore[arg-type]
