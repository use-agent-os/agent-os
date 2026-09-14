"""Issue #2132: `sessions list --since` reads a digit-only date as a 1970 epoch.

`raw.isdigit()` routed every all-digit value straight to `fromtimestamp` with
no plausibility check, so a date typed without separators (`20260101`) or a
bare year (`2026`) landed in 1970 and the filter matched *everything* --
exit 0, a full table, no warning. The opposite of #1913 (which silently
under-reported); this silently over-reports.

A second, related defect lived in the same function: an out-of-range digit
string (`99999999999999`) reached `datetime.fromtimestamp`, which delegates
range checking to the platform's C library -- Windows raises `OSError` for
it, but the same value is happily accepted on Linux (it resolves to a
valid, if absurd, ~5138 AD). Relying on that platform behavior is not a fix;
the digit-count allowlist here rejects it outright, before any timestamp
conversion runs, so the accept/reject outcome does not depend on the OS.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import typer

from agentos.cli import sessions_cmd
from agentos.cli.sessions_cmd import (
    _datetime_from_text,
    _epoch_seconds_to_datetime,
    _parse_since,
    _row_datetime,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("20260101", datetime(2026, 1, 1, tzinfo=UTC)),
        ("20260914", datetime(2026, 9, 14, tzinfo=UTC)),
        ("2026-09-14", datetime(2026, 9, 14, tzinfo=UTC)),
        ("2026-09-14T08:30:00Z", datetime(2026, 9, 14, 8, 30, tzinfo=UTC)),
        ("1757808000", datetime(2025, 9, 14, tzinfo=UTC)),
        ("1757808000000", datetime(2025, 9, 14, tzinfo=UTC)),
    ],
)
def test_since_accepts_the_forms_a_user_or_gateway_actually_sends(
    raw: str, expected: datetime
) -> None:
    """`20260101` used to parse as 1970-08-23; the filter matched everything."""
    assert _parse_since(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "2026",  # a bare year -- 4 digits, not a plausible epoch
        "0",
        "123",
        "20261332",  # 8 digits but not a real calendar date (month 13)
        "not-a-date",
        "2026-13-01",  # invalid ISO month
        "99999999999999",  # 14 digits: the platform-dependent crash case
    ],
)
def test_since_rejects_what_it_cannot_read(raw: str) -> None:
    """Rejecting is the point: silently meaning a different date is the bug
    this issue reports, so an implausible value must raise, not guess."""
    with pytest.raises(typer.BadParameter, match="ISO date"):
        _parse_since(raw)


def test_since_rejection_message_echoes_what_was_typed() -> None:
    with pytest.raises(typer.BadParameter, match="got '2026'"):
        _parse_since("2026")


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_since_treats_absence_as_no_filter(raw: str | None) -> None:
    assert _parse_since(raw) is None


def test_epoch_seconds_to_datetime_rejects_an_overflowing_value_portably() -> None:
    """The conversion must raise the same way on every platform: pure
    ``timedelta`` arithmetic, not a call into the platform's C library.

    This is what makes the fix portable -- avoiding ``datetime.fromtimestamp``
    for the conversion means there is no OS-specific range to disagree on.
    """
    with pytest.raises(ValueError, match="out of range"):
        _epoch_seconds_to_datetime(10**30)


def test_datetime_from_text_rejects_an_out_of_range_digit_string_by_length_alone() -> None:
    """A 14-digit string is rejected by the digit-count allowlist before any
    timestamp conversion is attempted -- so the accept/reject outcome for
    Issue #2132's own repro never depends on what the platform's
    ``fromtimestamp`` would have done with the resulting magnitude."""
    with pytest.raises(ValueError):
        _datetime_from_text("99999999999999")


@pytest.mark.parametrize(
    "value",
    [1757808000, 1757808000000, "1757808000", "2026-09-14T00:00:00Z", "20260914"],
)
def test_row_datetime_reads_the_timestamps_a_gateway_sends(value: object) -> None:
    assert _row_datetime({"updated_at": value}) is not None


@pytest.mark.parametrize(
    "value",
    [
        "123",
        "garbage",
        99999999999999999999,  # int, far beyond any plausible epoch
        1e300,  # float, would overflow timedelta on conversion
        -99999999999999999999,
        "",
        None,
        {},
    ],
)
def test_row_datetime_skips_a_bad_row_instead_of_aborting_the_listing(value: object) -> None:
    """`_row_datetime` shares the parser with `--since`, but a row is not a
    flag: one unusable timestamp must be skipped, never raise and take the
    whole `sessions list` down with it."""
    assert _row_datetime({"updated_at": value}) is None


def test_row_datetime_prefers_updated_at_over_camel_case_alias() -> None:
    row = {"updated_at": "2026-01-01", "updatedAt": "2020-01-01"}
    assert _row_datetime(row) == datetime(2026, 1, 1, tzinfo=UTC)


def test_fromtimestamp_is_not_called_for_epoch_conversion() -> None:
    """Regression guard for the portability fix itself: if a future edit
    reintroduces a ``datetime.fromtimestamp`` call for the conversion path,
    the platform-dependent range bug comes back with it. The docstring
    names ``fromtimestamp`` to explain why it's avoided, so this checks for
    an actual call, not just the word."""
    import inspect

    source = inspect.getsource(sessions_cmd._epoch_seconds_to_datetime)
    assert ".fromtimestamp(" not in source
