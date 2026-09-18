"""``active_hours`` is read in the host's local time, not in UTC.

The heartbeat module documents the window as "24-hour local time", but every
caller passes ``datetime.now(UTC)`` and the check read ``.hour`` straight off
it, so the window drifted by the host's UTC offset (#2603).

The probes run in a child interpreter started with ``TZ=JST-9`` (UTC+9, no
DST). That spelling is honoured by both glibc and the Windows CRT, so the
tests need neither ``time.tzset`` (POSIX-only) nor a host that happens to sit
outside UTC -- CI runners are on UTC, where the bug is invisible.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentos.scheduler.heartbeat import HeartbeatConfig
from agentos.scheduler.heartbeat_loop import HeartbeatLoop

_PROBE = r"""
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from agentos.scheduler.heartbeat import (
    HeartbeatEvent,
    HeartbeatRunner,
    parse_heartbeat_md,
    parse_loop_overrides,
)
from agentos.scheduler.heartbeat_loop import HeartbeatLoop

workspace = Path(sys.argv[1])
out = {}

probe = datetime(2026, 1, 15, 0, 0, tzinfo=UTC).astimezone()
out["offset_hours"] = probe.utcoffset() / timedelta(hours=1)

# 02:00 UTC is 11:00 local, 14:00 UTC is 23:00 local.
morning_utc = datetime(2026, 1, 15, 2, 0, tzinfo=UTC)
night_utc = datetime(2026, 1, 15, 14, 0, tzinfo=UTC)

day = parse_heartbeat_md("---\nactive_hours: [9, 21]\n---\n")
out["day_window_at_11_local"] = day.is_within_active_hours(morning_utc)
out["day_window_at_23_local"] = day.is_within_active_hours(night_utc)

overnight = parse_heartbeat_md("---\nactive_hours: [22, 6]\n---\n")
out["overnight_window_at_23_local"] = overnight.is_within_active_hours(night_utc)
out["overnight_window_at_11_local"] = overnight.is_within_active_hours(morning_utc)

runner = HeartbeatRunner(day)
runner.ingest(HeartbeatEvent(kind="probe", emitted_at=morning_utc - timedelta(minutes=5)))
out["runner_ticks_at_11_local"] = len(runner.poll(now=morning_utc))


def loop_with_window(window):
    heartbeat_md = workspace / "HEARTBEAT.md"
    heartbeat_md.write_text(
        f"---\nactive_hours: [{window[0]}, {window[1]}]\n---\nCheck the inbox.\n",
        encoding="utf-8",
    )
    config = MagicMock()
    config.heartbeat.enabled = True
    config.heartbeat.config_path = str(heartbeat_md)
    config.heartbeat.prompt = "ping"
    config.workspace_dir = str(workspace)
    config.workspace_strict = False
    service = AsyncMock()
    service.run_once.return_value = MagicMock(status="ran", reason=None)
    loop = HeartbeatLoop(config=config, heartbeat_service=service)
    loop.apply_overrides(parse_loop_overrides(heartbeat_md))
    return loop, service


async def run_once(window):
    loop, service = loop_with_window(window)
    result = await loop.run_once_now(
        reason="cron", agent_id="main", session_key="agent:main:main"
    )
    return {"reason": result.reason, "service_calls": service.run_once.await_count}


# Two-hour windows, so an hour rolling over mid-probe still lands inside.
local_hour = datetime.now().astimezone().hour
utc_hour = datetime.now(UTC).hour
out["run_once_local_window"] = asyncio.run(run_once((local_hour, (local_hour + 2) % 24)))
out["run_once_utc_window"] = asyncio.run(run_once((utc_hour, (utc_hour + 2) % 24)))

print(json.dumps(out))
"""


@pytest.fixture(scope="module")
def probe(tmp_path_factory: pytest.TempPathFactory) -> dict:
    workspace: Path = tmp_path_factory.mktemp("heartbeat_workspace")
    script = workspace / "probe.py"
    script.write_text(_PROBE, encoding="utf-8")
    env = dict(os.environ)
    env["TZ"] = "JST-9"
    completed = subprocess.run(
        [sys.executable, str(script), str(workspace)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_probe_runs_in_a_zone_nine_hours_ahead_of_utc(probe: dict) -> None:
    # Guard: without it, a platform that ignored TZ would run every probe on
    # UTC, where the old and new readings agree and the tests prove nothing.
    assert probe["offset_hours"] == 9


def test_day_window_is_open_at_a_local_hour_inside_it(probe: dict) -> None:
    assert probe["day_window_at_11_local"] is True


def test_day_window_is_closed_at_a_local_hour_outside_it(probe: dict) -> None:
    assert probe["day_window_at_23_local"] is False


def test_overnight_window_is_open_late_in_the_local_evening(probe: dict) -> None:
    assert probe["overnight_window_at_23_local"] is True


def test_overnight_window_is_closed_in_the_local_morning(probe: dict) -> None:
    assert probe["overnight_window_at_11_local"] is False


def test_runner_poll_emits_a_tick_inside_the_local_window(probe: dict) -> None:
    assert probe["runner_ticks_at_11_local"] == 1


def test_run_once_now_runs_inside_a_window_around_the_local_hour(probe: dict) -> None:
    assert probe["run_once_local_window"] == {"reason": None, "service_calls": 1}


def test_run_once_now_skips_a_window_that_only_matches_the_utc_hour(probe: dict) -> None:
    assert probe["run_once_utc_window"] == {"reason": "quiet-hours", "service_calls": 0}


def test_no_window_is_always_open() -> None:
    moment = datetime(2026, 1, 15, 3, 0, tzinfo=UTC)
    assert HeartbeatConfig(active_hours=None).is_within_active_hours(moment) is True
    assert HeartbeatLoop._within_active_hours(None, moment) is True


def test_naive_moment_keeps_its_own_hour() -> None:
    # A naive datetime is already local time; converting it must not shift it.
    moment = datetime(2026, 1, 15, 10, 0)
    assert HeartbeatConfig(active_hours=(9, 11)).is_within_active_hours(moment) is True
    assert HeartbeatLoop._within_active_hours((11, 12), moment) is False
