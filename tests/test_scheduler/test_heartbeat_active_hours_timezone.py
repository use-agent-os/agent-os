"""Regression: active_hours must be compared in the system's local time.

The module docstring documents ``active_hours`` as "24-hour local time", but
both call sites always pass a UTC-aware ``datetime`` and the check read
``moment.hour`` directly -- comparing the *UTC* hour against a window a user
set expecting their own clock. For anyone not on UTC, the window is silently
shifted by their offset with no error or warning anywhere.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime

import pytest

from agentos.scheduler.heartbeat import HeartbeatConfig
from agentos.scheduler.heartbeat_loop import HeartbeatLoop


@pytest.fixture
def local_timezone():
    """Pin the process's local timezone to America/Los_Angeles for the test."""
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset() is only available on Unix/POSIX")
    original = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    time.tzset()
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        time.tzset()


def test_config_active_hours_uses_local_time_not_utc(local_timezone) -> None:
    """22:00 UTC is 14:00 or 15:00 in Los Angeles (PDT/PST) -- inside a 9-21
    local window, even though 22 is outside it in UTC terms."""
    config = HeartbeatConfig(active_hours=(9, 21))
    moment_utc = datetime(2026, 1, 15, 22, 0, tzinfo=UTC)  # 14:00 PST locally

    assert config.is_within_active_hours(moment_utc) is True


def test_config_active_hours_excludes_a_moment_local_time_puts_outside_it(
    local_timezone,
) -> None:
    """Guard: a UTC moment that is ALSO outside the window in local time must
    still be excluded -- proves this isn't just always returning True."""
    config = HeartbeatConfig(active_hours=(9, 21))
    moment_utc = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)  # 22:00 PST the day before

    assert config.is_within_active_hours(moment_utc) is False


def test_config_no_active_hours_window_always_matches(local_timezone) -> None:
    """Guard: no window configured must still mean "always active"."""
    config = HeartbeatConfig(active_hours=None)
    moment_utc = datetime(2026, 1, 15, 3, 0, tzinfo=UTC)

    assert config.is_within_active_hours(moment_utc) is True


def test_loop_within_active_hours_uses_local_time_not_utc(local_timezone) -> None:
    moment_utc = datetime(2026, 1, 15, 22, 0, tzinfo=UTC)  # 14:00 PST locally

    assert HeartbeatLoop._within_active_hours((9, 21), moment_utc) is True
