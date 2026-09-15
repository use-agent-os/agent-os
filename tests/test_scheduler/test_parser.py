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


def test_parse_cron_dow_ranges_may_end_at_sun_by_name() -> None:
    # POSIX spells Sunday 0 or 7, and at the *upper* bound of a range it is 7 —
    # "SAT-SUN" is 6-7. Substituting the name with 0 unconditionally made it
    # 6-0, which _parse_field rejects as reversed, so every weekend-ish
    # schedule a user would write by name died with CronParseError while its
    # numeric spelling parsed fine.
    assert parse_cron("0 0 * * SAT-SUN").day_of_week.values == frozenset({0, 6})
    assert parse_cron("0 0 * * WED-SUN").day_of_week.values == frozenset({0, 3, 4, 5, 6})
    assert parse_cron("0 0 * * MON-SUN").day_of_week.values == frozenset({0, 1, 2, 3, 4, 5, 6})
    # Named and numeric spellings of the same range must agree.
    assert parse_cron("0 0 * * WED-SUN").day_of_week.values == (
        parse_cron("0 0 * * WED-7").day_of_week.values
    )
    assert parse_cron("0 0 * * sat-sun").day_of_week.values == frozenset({0, 6})


def test_parse_cron_dow_sun_at_range_start_is_still_zero() -> None:
    # Only the upper bound flips to 7 for a *distinct*-endpoint range. A
    # range that starts at Sunday and ends somewhere else keeps 0, so
    # "SUN-WED" stays four days.
    assert parse_cron("0 0 * * SUN-WED").day_of_week.values == frozenset({0, 1, 2, 3})
    # SUN-SUN and 0-SUN have the *same* endpoint on both sides -- per
    # POSIX/croniter that's the whole-field rule (see
    # test_parse_cron_same_endpoint_range_spans_the_whole_field), not a
    # single day. This test originally asserted {0} here; that was the bug
    # fixed by the same-endpoint-range change.
    assert parse_cron("0 0 * * SUN-SUN").day_of_week.values == frozenset({0, 1, 2, 3, 4, 5, 6})
    assert parse_cron("0 0 * * 0-SUN").day_of_week.values == frozenset({0, 1, 2, 3, 4, 5, 6})


def test_parse_cron_dow_sun_range_with_step() -> None:
    # The step branch parses the same range, so it must see 6-7 too.
    assert parse_cron("0 0 * * SAT-SUN/2").day_of_week.values == frozenset({6})
    assert parse_cron("0 0 * * MON-SUN/2").day_of_week.values == frozenset({0, 1, 3, 5})


def test_parse_cron_sat_sun_matches_the_weekend() -> None:
    expr = parse_cron("0 0 * * SAT-SUN")
    assert expr.matches(datetime(2026, 8, 29, 0, 0))  # a Saturday
    assert expr.matches(datetime(2026, 8, 30, 0, 0))  # the Sunday after it
    assert not expr.matches(datetime(2026, 8, 31, 0, 0))  # the Monday after that


def test_parse_cron_dow_7_dedups_with_0_and_names() -> None:
    assert parse_cron("0 0 * * 0,7").day_of_week.values == frozenset({0})
    assert parse_cron("0 0 * * MON,7").day_of_week.values == frozenset({0, 1})


def test_parse_cron_dow_bare_step_matches_croniter_for_every_start() -> None:
    # Issue #1501: a bare day-of-week step "N/M" (no explicit "-" range)
    # used `range(N, hi + 1, M)` with hi = 7, the alias-inclusive field
    # bound. That's only correct when N == 0. croniter treats day-of-week
    # as 7 true values (0-6), with 7 purely an input alias for 0 (its own
    # RANGES[DOW] == (0, 6)), and internally rewrites a bare "N/M" to
    # "N-6/M" -- so if the (alias-resolved) start lands exactly on 6, the
    # whole field is stepped, not just that one value. This table is the
    # full exhaustive cross-check: every valid day-of-week start value
    # (0-7) against a representative step, verified independently against
    # croniter directly (not just against this parser's own prior output).
    expectations = {
        (0, 1): {0, 1, 2, 3, 4, 5, 6},
        (0, 2): {0, 2, 4, 6},
        (0, 7): {0},
        (1, 3): {1, 4},
        (2, 3): {2, 5},
        (5, 4): {5},
        (6, 1): {0, 1, 2, 3, 4, 5, 6},
        (6, 2): {0, 2, 4, 6},
        (6, 3): {0, 3, 6},
        (7, 1): {0, 1, 2, 3, 4, 5, 6},
        (7, 2): {0, 2, 4, 6},
        (7, 3): {0, 3, 6},
        (7, 7): {0},
    }
    for (start, step), expected in expectations.items():
        got = parse_cron(f"0 0 * * {start}/{step}").day_of_week.values
        msg = f"{start}/{step}: got {sorted(got)}, want {sorted(expected)}"
        assert got == frozenset(expected), msg


def test_parse_cron_dow_bare_step_is_identical_across_sunday_spellings() -> None:
    # 0, 7, and the name SUN all denote Sunday; a bare step expression must
    # give byte-for-byte the same set regardless of which spelling is used.
    for step in (1, 2, 3, 4, 7):
        by_zero = parse_cron(f"0 0 * * 0/{step}").day_of_week.values
        by_seven = parse_cron(f"0 0 * * 7/{step}").day_of_week.values
        by_name = parse_cron(f"0 0 * * SUN/{step}").day_of_week.values
        assert by_zero == by_seven == by_name


def test_parse_cron_dow_bare_step_fix_does_not_affect_explicit_ranges() -> None:
    # The fix is scoped to the bare-value ("N/M", no dash) branch only.
    # Explicit two-sided ranges, with or without a step, must be untouched
    # -- these already pass on main via #1344's fix for #1063.
    assert parse_cron("0 0 * * WED-7").day_of_week.values == frozenset({0, 3, 4, 5, 6})
    assert parse_cron("0 0 * * SAT-SUN").day_of_week.values == frozenset({0, 6})
    assert parse_cron("0 0 * * SUN-FRI").day_of_week.values == frozenset({0, 1, 2, 3, 4, 5})


def test_parse_cron_dow_bare_step_fix_does_not_affect_other_fields() -> None:
    # Only day_of_week has a 0/max alias; every other field's bare "N/M"
    # step must keep using the field's real declared max, unchanged.
    assert parse_cron("0 0 1/6 * *").day_of_month.values == frozenset({1, 7, 13, 19, 25, 31})
    assert parse_cron("0 0 * 12/6 *").month.values == frozenset({12})
    assert parse_cron("0 22/1 * * *").hour.values == frozenset({22, 23})


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


def test_parse_cron_same_endpoint_range_spans_the_whole_field() -> None:
    # POSIX/croniter: a range whose two endpoints resolve to the same field
    # position means the entire field, not a single value -- croniter's own
    # source has this as an explicit `elif low == high: whole cycle` branch,
    # confirmed empirically to apply to every field, with or without a step.
    # #1344 (merged) partially compensated for this within day_of_week's
    # named-Sunday handling, but only for the literal text "sun" as the
    # *upper* bound, and excluded exactly the cases that need it most:
    assert parse_cron("0 0 * * SUN-SUN").day_of_week.values == frozenset({0, 1, 2, 3, 4, 5, 6})
    assert parse_cron("0 0 * * 0-SUN").day_of_week.values == frozenset({0, 1, 2, 3, 4, 5, 6})
    assert parse_cron("0 0 * * SUN-0").day_of_week.values == frozenset({0, 1, 2, 3, 4, 5, 6})
    assert parse_cron("0 0 * * sUn-SuN").day_of_week.values == frozenset({0, 1, 2, 3, 4, 5, 6})
    # Not Sunday-specific at all -- any same-endpoint day-of-week range:
    assert parse_cron("0 0 * * 3-3").day_of_week.values == frozenset({0, 1, 2, 3, 4, 5, 6})
    assert parse_cron("0 0 * * 6-6").day_of_week.values == frozenset({0, 1, 2, 3, 4, 5, 6})
    # With a step:
    assert parse_cron("0 0 * * 6-6/2").day_of_week.values == frozenset({0, 2, 4, 6})
    assert parse_cron("0 0 * * SUN-SUN/2").day_of_week.values == frozenset({0, 2, 4, 6})
    # Not day-of-week-specific either -- every field has this rule:
    assert parse_cron("0 5-5 * * *").hour.values == frozenset(range(24))
    assert parse_cron("0 0 1 3-3 *").month.values == frozenset(range(1, 13))
    assert parse_cron("30-30 * * * *").minute.values == frozenset(range(60))
    assert parse_cron("0 0 15-15 * *").day_of_month.values == frozenset(range(1, 32))


def test_parse_cron_same_endpoint_range_fix_does_not_affect_distinct_endpoints() -> None:
    # Regression guard: only start == end triggers the whole-field expansion.
    # Every already-correct #1344/#1501 case must be unaffected.
    assert parse_cron("0 0 * * SAT-SUN").day_of_week.values == frozenset({0, 6})
    assert parse_cron("0 0 * * WED-SUN").day_of_week.values == frozenset({0, 3, 4, 5, 6})
    assert parse_cron("0 0 * * MON-SUN").day_of_week.values == frozenset({0, 1, 2, 3, 4, 5, 6})
    assert parse_cron("0 0 * * FRI-SUN").day_of_week.values == frozenset({0, 5, 6})
    assert parse_cron("0 0 * * SUN-FRI").day_of_week.values == frozenset({0, 1, 2, 3, 4, 5})
    assert parse_cron("0 0 * * MON-FRI").day_of_week.values == frozenset({1, 2, 3, 4, 5})
    assert parse_cron("0 0 * * FRI-SUN/2").day_of_week.values == frozenset({0, 5})
    assert parse_cron("0 0 1 3-6 *").month.values == frozenset({3, 4, 5, 6})
    assert parse_cron("0 9-17 * * *").hour.values == frozenset(range(9, 18))
    # Reversed ranges must still raise, not silently become the whole field.
    with pytest.raises(CronParseError, match="Range start > end"):
        parse_cron("0 0 * * 5-3")
    with pytest.raises(CronParseError, match="Range start > end"):
        parse_cron("0 0 * * FRI-MON")


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
