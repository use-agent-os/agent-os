"""Shared cron schedule wire normalizer."""

from __future__ import annotations

import pytest

from agentos.scheduler.schedule_normalizer import coerce_schedule_from_params
from agentos.scheduler.types import ScheduleKind


def test_normalizer_accepts_structured_cron_with_tz() -> None:
    kind, value, tz = coerce_schedule_from_params(
        {"schedule": {"kind": "cron", "expr": "0 9 * * 1-5", "tz": "Asia/Shanghai"}}
    )

    assert kind == ScheduleKind.CRON
    assert value == "0 9 * * 1-5"
    assert tz == "Asia/Shanghai"


def test_normalizer_applies_top_level_tz_when_structured_cron_omits_tz() -> None:
    kind, value, tz = coerce_schedule_from_params(
        {"schedule": {"kind": "cron", "expr": "0 9 * * *"}, "tz": "Asia/Shanghai"}
    )

    assert kind == ScheduleKind.CRON
    assert value == "0 9 * * *"
    assert tz == "Asia/Shanghai"


def test_normalizer_rejects_conflicting_structured_and_top_level_tz() -> None:
    with pytest.raises(ValueError, match="schedule.tz conflicts with tz"):
        coerce_schedule_from_params(
            {
                "schedule": {
                    "kind": "cron",
                    "expr": "0 9 * * *",
                    "tz": "Asia/Shanghai",
                },
                "tz": "America/Los_Angeles",
            }
        )


def test_normalizer_accepts_every_seconds() -> None:
    kind, value, tz = coerce_schedule_from_params(
        {"schedule": {"kind": "every", "every_seconds": 300}}
    )

    assert kind == ScheduleKind.EVERY
    assert value == "300"
    assert tz == ""


def test_normalizer_rejects_every_anchor_until_supported() -> None:
    with pytest.raises(ValueError, match="schedule.anchor_at is not supported"):
        coerce_schedule_from_params(
            {
                "schedule": {
                    "kind": "every",
                    "every_seconds": 300,
                    "anchor_at": "2026-05-18T09:00:00+08:00",
                }
            }
        )


def test_normalizer_accepts_timezone_aware_at() -> None:
    kind, value, tz = coerce_schedule_from_params(
        {"schedule": {"kind": "at", "at": "2026-05-18T09:00:00+08:00"}}
    )

    assert kind == ScheduleKind.AT
    assert value == "2026-05-18T09:00:00+08:00"
    assert tz == ""


def test_normalizer_wraps_legacy_expression() -> None:
    kind, value, tz = coerce_schedule_from_params(
        {"expression": "*/5 * * * *", "tz": "America/Los_Angeles"}
    )

    assert kind == ScheduleKind.CRON
    assert value == "*/5 * * * *"
    assert tz == "America/Los_Angeles"


def test_normalizer_expression_accepts_timezone_alias() -> None:
    # The expression-shorthand path must honour the `timezone` alias exactly
    # like the structured path does via _top_level_tz. A legacy caller passing
    # {"expression": "...", "timezone": "Asia/Shanghai"} used to silently drop
    # the tz while the "tz" key kept it.
    kind, value, tz = coerce_schedule_from_params(
        {"expression": "*/5 * * * *", "timezone": "Asia/Shanghai"}
    )
    assert kind == ScheduleKind.CRON
    assert value == "*/5 * * * *"
    assert tz == "Asia/Shanghai"


def test_normalizer_expression_rejects_conflicting_tz_and_timezone() -> None:
    with pytest.raises(ValueError, match="tz conflicts with timezone"):
        coerce_schedule_from_params(
            {
                "expression": "*/5 * * * *",
                "tz": "Asia/Shanghai",
                "timezone": "America/Los_Angeles",
            }
        )


def test_normalizer_rejects_invalid_tz() -> None:
    with pytest.raises(ValueError, match="schedule.tz invalid"):
        coerce_schedule_from_params(
            {"schedule": {"kind": "cron", "expr": "0 9 * * *", "tz": "Mars/Base"}}
        )


def test_normalizer_rejects_naive_at() -> None:
    with pytest.raises(ValueError, match="schedule.at invalid"):
        coerce_schedule_from_params({"schedule": {"kind": "at", "at": "2026-05-18T09:00:00"}})


def test_normalizer_accepts_structured_cron_with_timezone_alias() -> None:
    kind, value, tz = coerce_schedule_from_params(
        {"schedule": {"kind": "cron", "expr": "0 9 * * 1-5", "timezone": "Asia/Shanghai"}}
    )

    assert kind == ScheduleKind.CRON
    assert value == "0 9 * * 1-5"
    assert tz == "Asia/Shanghai"


def test_normalizer_structured_rejects_conflicting_tz_and_timezone() -> None:
    with pytest.raises(ValueError, match="schedule.tz conflicts with schedule.timezone"):
        coerce_schedule_from_params(
            {
                "schedule": {
                    "kind": "cron",
                    "expr": "0 9 * * *",
                    "tz": "Asia/Shanghai",
                    "timezone": "America/Los_Angeles",
                }
            }
        )


def test_normalizer_structured_accepts_matching_tz_and_timezone() -> None:
    kind, value, tz = coerce_schedule_from_params(
        {
            "schedule": {
                "kind": "cron",
                "expr": "0 9 * * *",
                "tz": "Asia/Shanghai",
                "timezone": "Asia/Shanghai",
            }
        }
    )
    assert kind == ScheduleKind.CRON
    assert value == "0 9 * * *"
    assert tz == "Asia/Shanghai"


def test_normalizer_rejects_conflicting_structured_timezone_and_top_level_tz() -> None:
    with pytest.raises(ValueError, match="schedule.tz conflicts with tz"):
        coerce_schedule_from_params(
            {
                "schedule": {
                    "kind": "cron",
                    "expr": "0 9 * * *",
                    "timezone": "Asia/Shanghai",
                },
                "tz": "America/Los_Angeles",
            }
        )


def test_normalizer_rejects_non_string_timezone_in_structured_schedule() -> None:
    with pytest.raises(ValueError, match="schedule.tz must be a string IANA timezone name"):
        coerce_schedule_from_params(
            {"schedule": {"kind": "cron", "expr": "0 9 * * *", "timezone": 123}}
        )
