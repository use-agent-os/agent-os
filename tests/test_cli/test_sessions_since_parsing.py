"""Issue #2132: `sessions list --since` reading a digit-only date as an epoch.

`raw.isdigit()` routed every all-digit value to `fromtimestamp`, so a date typed
without separators became a 1970 timestamp and the filter matched *everything* —
exit 0, a full table, no warning. The reverse of #1913, which silently
under-reported; this silently over-reported.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import typer

from agentos.cli import sessions_cmd


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
def test_since_accepts_the_forms_a_user_actually_types(raw: str, expected: datetime) -> None:
    """`20260101` used to parse as 1970-08-23, so the filter matched everything."""
    assert sessions_cmd._parse_since(raw) == expected


@pytest.mark.parametrize("raw", ["2026", "0", "123", "20261332", "not-a-date", "2026-13-01"])
def test_since_rejects_what_it_cannot_read(raw: str) -> None:
    """Rejecting is the point: silently meaning a different date is the bug."""
    with pytest.raises(typer.BadParameter, match="ISO date"):
        sessions_cmd._parse_since(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "99999999999999",  # ~year 5138 after the millis divide
        "999999999999999999",  # far past anything any platform accepts
        "9999999999999999999999",
    ],
)
def test_since_rejects_an_out_of_range_timestamp_without_a_traceback(raw: str) -> None:
    """`fromtimestamp` raises OSError/OverflowError, not ValueError, so this
    escaped the `except ValueError` and reached the user as a traceback.

    The range is checked explicitly rather than left to the platform: Linux
    accepts timestamps up to year 9999 while the Windows CRT stops near year
    3000, so `--since 99999999999999` used to raise on Windows and quietly
    become a year-5138 filter on Linux. Same input, same answer, either way.
    """
    with pytest.raises(typer.BadParameter):
        sessions_cmd._parse_since(raw)


def test_a_far_future_row_timestamp_is_skipped_rather_than_crashing() -> None:
    """The row path shares the bound and must still degrade quietly."""
    assert sessions_cmd._row_datetime({"updated_at": 99999999999999}) is None


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_since_treats_absence_as_no_filter(raw: str | None) -> None:
    assert sessions_cmd._parse_since(raw) is None


@pytest.mark.parametrize(
    "value",
    [1757808000, 1757808000000, "1757808000", "2026-09-14T00:00:00Z", "20260914"],
)
def test_row_datetime_reads_the_timestamps_a_gateway_sends(value: object) -> None:
    assert sessions_cmd._row_datetime({"updated_at": value}) is not None


@pytest.mark.parametrize("value", ["123", "garbage", 99999999999999999999, "", None, {}])
def test_row_datetime_skips_a_bad_row_instead_of_aborting_the_listing(value: object) -> None:
    """`_row_datetime` shares the parser with `--since`, but a row is not a flag:
    one unusable timestamp must be skipped, never raise BadParameter and take
    the whole `sessions list` down with it."""
    assert sessions_cmd._row_datetime({"updated_at": value}) is None
