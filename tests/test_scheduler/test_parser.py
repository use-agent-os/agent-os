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


def test_parse_cron_dow_ranges_ending_in_named_sunday() -> None:
    # Sunday at the end of a named day-of-week range represents 7 (POSIX),
    # resolving into the full range without inverting into start > 0.
    assert parse_cron("0 0 * * WED-SUN").day_of_week.values == frozenset({0, 3, 4, 5, 6})
    assert parse_cron("0 0 * * SAT-SUN").day_of_week.values == frozenset({0, 6})
    assert parse_cron("0 0 * * Sat-Sun").day_of_week.values == frozenset({0, 6})
    assert parse_cron("0 0 * * MON-SUN").day_of_week.values == frozenset({0, 1, 2, 3, 4, 5, 6})
    assert parse_cron("0 0 * * FRI-SUN/2").day_of_week.values == frozenset({0, 5})
    assert parse_cron("0 0 * * SUN-SUN").day_of_week.values == frozenset({0, 1, 2, 3, 4, 5, 6})


def test_parse_cron_dow_7_dedups_with_0_and_names() -> None:
    assert parse_cron("0 0 * * 0,7").day_of_week.values == frozenset({0})
    assert parse_cron("0 0 * * MON,7").day_of_week.values == frozenset({0, 1})


def test_parse_cron_dow_7_matches_sunday_not_monday() -> None:
    expr = parse_cron("0 0 * * 7")
    sunday = datetime(2026, 8, 30, 0, 0)  # a Sunday
    monday = datetime(2026, 8, 31, 0, 0)  # the next Monday
    assert expr.matches(sunday)
    assert not expr.matches(monday)


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


# --- POSIX day-of-month / day-of-week OR rule ----------------------------


def test_dom_and_dow_both_restricted_fire_on_either() -> None:
    # "0 0 1,15 * 5" means the 1st, the 15th, OR any Friday. ANDing the two
    # fields restricted it to a 1st/15th that also happened to be a Friday,
    # which silently killed the schedule for virtually the whole month.
    expr = parse_cron("0 0 1,15 * 5")
    assert expr.matches(datetime(2026, 8, 7, 0, 0))  # Friday, neither 1st nor 15th
    assert expr.matches(datetime(2026, 8, 1, 0, 0))  # 1st, a Saturday
    assert expr.matches(datetime(2026, 8, 15, 0, 0))  # 15th, a Saturday
    assert expr.matches(datetime(2026, 8, 14, 0, 0))  # Friday
    assert not expr.matches(datetime(2026, 8, 6, 0, 0))  # Thursday, neither day


def test_dom_and_dow_or_rule_still_honours_minute_hour_month() -> None:
    # OR applies to the two day fields only — the other three still AND.
    expr = parse_cron("30 9 1,15 8 5")
    assert expr.matches(datetime(2026, 8, 7, 9, 30))
    assert not expr.matches(datetime(2026, 8, 7, 9, 31))  # wrong minute
    assert not expr.matches(datetime(2026, 8, 7, 10, 30))  # wrong hour
    assert not expr.matches(datetime(2026, 9, 4, 9, 30))  # Friday, wrong month


def test_dow_wildcard_keeps_and_semantics() -> None:
    # Only one field restricted: no OR, or "0 0 1 * *" would fire every day.
    expr = parse_cron("0 0 1 * *")
    assert expr.matches(datetime(2026, 8, 1, 0, 0))
    assert not expr.matches(datetime(2026, 8, 7, 0, 0))


def test_dom_wildcard_keeps_and_semantics() -> None:
    expr = parse_cron("0 0 * * 5")
    assert expr.matches(datetime(2026, 8, 7, 0, 0))  # Friday
    assert not expr.matches(datetime(2026, 8, 6, 0, 0))  # Thursday


def test_both_wildcards_match_every_day() -> None:
    expr = parse_cron("0 0 * * *")
    assert expr.matches(datetime(2026, 8, 6, 0, 0))
    assert expr.matches(datetime(2026, 8, 7, 0, 0))


def test_step_over_star_counts_as_restricted() -> None:
    # Only a bare "*" is a wildcard: "*/2" names a specific set of days, so the
    # OR rule applies — same call croniter makes.
    expr = parse_cron("0 0 */2 * 5")  # day-of-month 1,3,5,...,31
    assert expr.matches(datetime(2026, 8, 5, 0, 0))  # Wednesday the 5th, via day-of-month
    assert expr.matches(datetime(2026, 8, 14, 0, 0))  # Friday the 14th, via day-of-week
    assert not expr.matches(datetime(2026, 8, 6, 0, 0))  # Thursday the 6th, neither


def test_wildcard_flag_is_recorded_per_field() -> None:
    expr = parse_cron("0 0 1,15 * 5")
    assert not expr.day_of_month.is_wildcard
    assert expr.month.is_wildcard
    assert not expr.day_of_week.is_wildcard
    assert parse_cron("0 0 * * *").day_of_month.is_wildcard
    assert not parse_cron("0 0 1-31 * *").day_of_month.is_wildcard


def test_weekly_preset_is_unaffected_by_the_or_rule() -> None:
    # "@weekly" expands to "0 0 * * 0" — day-of-month is a wildcard, so it
    # stays a Sunday-only schedule rather than firing daily.
    expr = parse_cron("@weekly")
    assert expr.matches(datetime(2026, 8, 30, 0, 0))  # Sunday
    assert not expr.matches(datetime(2026, 8, 31, 0, 0))  # Monday


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
