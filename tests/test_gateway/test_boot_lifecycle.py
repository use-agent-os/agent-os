"""Crash-loop breaker (#3690): boot ledger, safe mode, self-clear, manual override."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.gateway import boot_lifecycle as bl
from agentos.gateway.boot_lifecycle import (
    UNCLEAN_BOOT_THRESHOLD,
    UNCLEAN_WINDOW_S,
    BootLedger,
    BootTracker,
    evaluate_unclean_boots,
)
from agentos.gateway.config import GatewayConfig
from agentos.gateway.rpc import RpcContext, get_dispatcher


class _Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _tracker(db: Path, clock: _Clock, pid: int = 1) -> BootTracker:
    return BootTracker(BootLedger(db), clock=clock, pid=pid)


def _crash_boots(db: Path, clock: _Clock, count: int, *, gap_s: float = 10.0) -> None:
    """``count`` boots that each died without a clean shutdown (kill -9)."""
    for i in range(count):
        _tracker(db, clock, pid=100 + i).start()
        clock.now += gap_s


def test_a_boot_that_never_ended_is_marked_unclean_by_the_next(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    clock = _Clock()
    first = _tracker(db, clock)
    first.start()
    clock.now += 5
    second = _tracker(db, clock, pid=2)
    decision = second.start()
    assert decision.active is False
    rows = BootLedger(db).unclean_since(0)
    assert [row.id for row in rows] == [first.boot_id]
    assert rows[0].reason == "ended without a clean shutdown"


def test_three_unclean_boots_in_the_window_start_safe_mode(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    clock = _Clock()
    _crash_boots(db, clock, UNCLEAN_BOOT_THRESHOLD)
    fourth = _tracker(db, clock, pid=9)
    decision = fourth.start()
    assert decision.active is True
    assert decision.unclean_boots == 3
    assert "3 unclean gateway boots" in decision.reason
    # Clears when the oldest of the three leaves the 5-minute window.
    assert decision.clears_at == pytest.approx(1_000_000.0 + UNCLEAN_WINDOW_S)
    snap = fourth.snapshot()
    assert snap["active"] is True
    assert snap["clears_at"].endswith("Z")
    assert len(snap["recent"]) == 3


def test_two_unclean_boots_are_not_enough(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    clock = _Clock()
    _crash_boots(db, clock, UNCLEAN_BOOT_THRESHOLD - 1)
    assert _tracker(db, clock).start().active is False


def test_unclean_boots_older_than_the_window_do_not_count(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    clock = _Clock()
    _crash_boots(db, clock, 3, gap_s=200.0)  # spread over 10 minutes
    assert _tracker(db, clock).start().active is False


def test_clean_shutdowns_and_startup_failures_never_count(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    clock = _Clock()
    for i in range(3):
        tracker = _tracker(db, clock, pid=10 + i)
        tracker.start()
        tracker.note_stopping("shutdown")
        tracker.mark_clean()
        tracker.mark_clean("ignored: first reason wins")
        clock.now += 5
    for i in range(3):
        tracker = _tracker(db, clock, pid=20 + i)
        tracker.start()
        tracker.mark_startup_failed("GatewayConfigError: schema migration failed")
        clock.now += 5
    assert _tracker(db, clock).start().active is False
    with sqlite3.connect(db) as conn:
        reasons = [r[0] for r in conn.execute("SELECT reason FROM gateway_boots ORDER BY rowid")]
    assert reasons[:3] == ["shutdown"] * 3
    assert all(r.startswith("startup_failed: GatewayConfigError") for r in reasons[3:6])


@pytest.mark.asyncio
async def test_safe_mode_self_clears_when_the_window_drains(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    clock = _Clock()
    _crash_boots(db, clock, 3)
    tracker = _tracker(db, clock)
    assert tracker.start().active is True
    resumed: list[str] = []

    async def resume(reason: str) -> None:
        resumed.append(reason)

    tracker.set_resume_hook(resume)

    clock.now += 60
    assert await tracker.reevaluate() is False
    assert tracker.safe_mode_active is True
    assert resumed == []

    clock.now = 1_000_000.0 + UNCLEAN_WINDOW_S + 1
    assert await tracker.reevaluate() is True
    assert tracker.safe_mode_active is False
    assert resumed == ["window_drained"]
    assert tracker.snapshot()["cleared_by"] == "window_drained"
    # Once cleared, re-evaluation is a no-op.
    assert await tracker.reevaluate() is False
    assert resumed == ["window_drained"]


@pytest.mark.asyncio
async def test_manual_override_clears_and_stops_counting_the_crashes(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    clock = _Clock()
    _crash_boots(db, clock, 3)
    tracker = _tracker(db, clock)
    assert tracker.start().active is True
    resumed: list[str] = []

    async def resume(reason: str) -> None:
        resumed.append(reason)

    tracker.set_resume_hook(resume)
    assert await tracker.exit_safe_mode(reason="channels.start") is True
    assert resumed == ["channels.start"]
    tracker.mark_clean("shutdown")

    # The next boot, still inside the window, is not in safe mode.
    clock.now += 5
    assert _tracker(db, clock, pid=77).start().active is False


@pytest.mark.asyncio
async def test_boots_that_ran_a_while_before_crashing_count_by_when_they_died(
    tmp_path: Path,
) -> None:
    db = tmp_path / "sessions.db"
    clock = _Clock()
    long_run = _tracker(db, clock, pid=200)
    long_run.start()
    clock.now += 600  # ran ten minutes, heartbeating, then died
    await long_run.heartbeat()
    clock.now += 5
    _crash_boots(db, clock, 2, gap_s=5)  # and the next two die at once
    decision = _tracker(db, clock, pid=9).start()
    # By start time the long run is 10+ minutes old and would not count; by
    # when it died it is one of three crashes inside five minutes.
    assert decision.active is True
    assert decision.unclean_boots == 3
    assert decision.clears_at == pytest.approx(1_000_000.0 + 600 + UNCLEAN_WINDOW_S)
    recent = decision.to_payload()["recent"]
    assert [row["ended_at"] for row in recent] == sorted(
        (row["ended_at"] for row in recent), reverse=True
    )


@pytest.mark.asyncio
async def test_safe_mode_since_is_the_first_trip_time(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    clock = _Clock()
    _crash_boots(db, clock, 3)
    tracker = _tracker(db, clock)
    first = tracker.start()
    assert first.active is True
    tripped = tracker.snapshot()["since"]
    for _ in range(3):
        clock.now += 30
        assert await tracker.reevaluate() is False
    assert tracker.snapshot()["since"] == tripped


def test_an_older_ledger_without_heartbeats_is_upgraded(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE gateway_boots (id TEXT PRIMARY KEY, pid INTEGER NOT NULL, "
            "started_at REAL NOT NULL, ended_at REAL, clean INTEGER, "
            "reason TEXT NOT NULL DEFAULT '', counted INTEGER NOT NULL DEFAULT 1)"
        )
    tracker = _tracker(db, _Clock())
    assert tracker.start().error is None
    with sqlite3.connect(db) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(gateway_boots)")]
    assert cols[-1] == "last_seen_at"


def test_ledger_failure_fails_open(tmp_path: Path) -> None:
    not_a_db = tmp_path / "sessions.db"
    not_a_db.mkdir()  # sqlite cannot open a directory
    tracker = BootTracker(BootLedger(not_a_db), clock=_Clock())
    decision = tracker.start()
    assert decision.active is False
    assert decision.error
    assert tracker.snapshot()["error"]
    tracker.mark_clean()  # never raises either


def test_evaluate_ignores_uncounted_rows() -> None:
    rows = [
        bl.BootRow(id=str(i), pid=i, started_at=100.0 + i, ended_at=None, clean=False, reason="")
        for i in range(3)
    ]
    assert evaluate_unclean_boots(rows, now=110.0).active is True
    uncounted = [bl.BootRow(**{**row.__dict__, "counted": False}) for row in rows]
    assert evaluate_unclean_boots(uncounted, now=110.0).active is False


def test_migration_v014_matches_the_ledger_schema(tmp_path: Path) -> None:
    from agentos.gateway.boot import _resolve_migrations_dir
    from agentos.persistence.migrator import apply_pending

    db = tmp_path / "sessions.db"
    applied = apply_pending(str(db), _resolve_migrations_dir())
    assert "V014__gateway_boots" in applied
    with sqlite3.connect(db) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(gateway_boots)")]
    assert cols == [
        "id",
        "pid",
        "started_at",
        "ended_at",
        "clean",
        "reason",
        "counted",
        "last_seen_at",
    ]
    clock = _Clock()
    _crash_boots(db, clock, 3)
    assert _tracker(db, clock).start().active is True


# ── the boot sequence in safe mode ───────────────────────────────────


class _FakeScheduler:
    def __init__(self) -> None:
        self.started = 0
        self.events: list[str] = []

    def register_handler(self, key: str, *_args: Any) -> None:
        self.events.append(f"handler:{key}")

    async def list_jobs(self) -> list[Any]:
        return []

    async def start(self) -> None:
        self.started += 1
        self.events.append("start")

    async def stop(self) -> None:
        return None


class _FakeChannelManager:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.running: set[str] = set()

    async def start_all(self) -> dict[str, bool]:
        self.calls.append("start_all")
        self.running = {"tg"}
        return {"tg": True}

    async def start_stopped(self) -> dict[str, bool]:
        self.calls.append("start_stopped")
        started = {"tg"} - self.running
        self.running |= started
        return {name: True for name in started}

    def start_errors(self) -> dict[str, dict[str, str]]:
        return {}

    def items(self) -> Any:
        return {"tg": object()}.items()

    def get(self, name: str) -> Any:
        return object() if name == "tg" else None

    async def stop_all(self) -> None:
        self.running = set()

    async def health(self) -> dict[str, Any]:
        return {}


async def _boot_in_safe_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from agentos.gateway import boot

    class FakeTurnRunner:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def set_session_lock_provider(self, _provider: Any) -> None:
            pass

    captured: dict[str, Any] = {}
    scheduler = _FakeScheduler()

    async def fake_build_services(**kwargs: Any) -> Any:
        captured.update(kwargs)

        async def close() -> None:
            return None

        return SimpleNamespace(
            provider_selector=object(),
            tool_registry=object(),
            session_manager=object(),
            skill_loader=object(),
            usage_tracker=object(),
            config=kwargs["config"],
            memory_sync_managers={},
            model_catalog=None,
            memory_retrievers={},
            turn_capture_services={},
            cron_scheduler=scheduler,
            task_runtime=None,
            agent_registry=None,
            memory_managers={},
            memory_stores={},
            _turn_runner_ref=[],
            heartbeat_loop=None,
            close=close,
        )

    monkeypatch.setattr("agentos.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr(boot, "build_services", fake_build_services)
    monkeypatch.setattr(boot, "_setup_file_logging", lambda config: None)
    monkeypatch.setattr(boot, "emit_skill_filter_banner", lambda config: None)
    monkeypatch.setattr("agentos.gateway.pidlock.GatewayPidLock.acquire", lambda self: None)
    monkeypatch.setattr("agentos.gateway.pidlock.GatewayPidLock.release", lambda self: None)
    captured["trading_engine_starts"] = 0

    def fake_start_trading_engine(config: Any) -> None:
        captured["trading_engine_starts"] += 1

    monkeypatch.setattr(boot, "_start_trading_engine", fake_start_trading_engine)

    db = tmp_path / "state" / "sessions.db"
    clock = _Clock()
    _crash_boots(db, clock, 3)
    tracker = _tracker(db, clock)
    manager = _FakeChannelManager()
    config = GatewayConfig(
        state_dir=str(tmp_path / "state"),
        workspace_dir=str(tmp_path / "workspace"),
        control_ui={"enabled": False},
        channels={"channels": []},
    )
    server = await boot.start_gateway_server(
        config=config, channel_manager=manager, run=False, boot_tracker=tracker
    )
    return server, captured, scheduler, manager, tracker


@pytest.mark.asyncio
async def test_boot_in_safe_mode_holds_back_channels_cron_and_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from starlette.testclient import TestClient

    server, captured, scheduler, manager, tracker = await _boot_in_safe_mode(tmp_path, monkeypatch)
    try:
        assert captured["start_scheduler"] is False
        assert manager.calls == []
        assert scheduler.started == 0
        # The trading engine (DCA buys, triggers) is held back with the scheduler.
        assert captured["trading_engine_starts"] == 0
        assert server.app.state.boot_tracker is tracker
        ready = TestClient(server.app, base_url="http://localhost").get("/ready")
        assert ready.status_code == 200
        assert ready.json()["state"] == "safe_mode"
        assert ready.json()["safe_mode"]["active"] is True

        # doctor reports it, with the way out.
        doctor = await get_dispatcher().dispatch(
            "d1", "doctor.status", {"deep": False}, RpcContext(conn_id="t", config=server.config)
        )
        finding = next(f for f in doctor.payload["findings"] if f["id"] == "gateway.safe_mode")
        commands = [step.get("command") for step in finding["fixSteps"]]
        assert "agentos channels start --yes" in commands

        # channels.start with a name starts that one only; safe mode holds.
        ctx = RpcContext(conn_id="t", config=server.config, channel_manager=manager)
        manager.start_channel = _record(manager, "start_channel")  # type: ignore[attr-defined]
        one = await get_dispatcher().dispatch("s1", "channels.start", {"name": "tg"}, ctx)
        assert one.error is None, one.error
        assert one.payload["safe_mode"]["active"] is True
        assert manager.calls == ["start_channel:tg"]

        # channels.start without a name is the override: everything resumes.
        res = await get_dispatcher().dispatch("s2", "channels.start", {}, ctx)
        assert res.error is None, res.error
        assert res.payload["safe_mode_cleared"] is True
        assert tracker.safe_mode_active is False
        assert manager.calls == ["start_channel:tg", "start_stopped"]
        assert scheduler.started == 1
        # The resume starts the trading engine it held back, once.
        assert captured["trading_engine_starts"] == 1
        again = TestClient(server.app, base_url="http://localhost").get("/ready")
        assert again.json()["state"] == "ready"
    finally:
        await server.close()
    assert bl.get_boot_tracker() is None


def _record(manager: _FakeChannelManager, label: str) -> Any:
    async def _call(name: str) -> None:
        manager.calls.append(f"{label}:{name}")
        manager.running.add(name)

    return _call


@pytest.mark.asyncio
async def test_boot_outside_safe_mode_starts_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentos.gateway import boot

    db = tmp_path / "state" / "sessions.db"
    tracker = _tracker(db, _Clock())
    manager = _FakeChannelManager()
    scheduler = _FakeScheduler()

    async def fake_build_services(**kwargs: Any) -> Any:
        # The gateway never lets build_services start the scheduler: the
        # catch-up must not run before the handlers exist.
        assert kwargs["start_scheduler"] is False

        async def close() -> None:
            return None

        return SimpleNamespace(
            provider_selector=object(),
            tool_registry=object(),
            session_manager=object(),
            skill_loader=object(),
            usage_tracker=object(),
            config=kwargs["config"],
            memory_sync_managers={},
            model_catalog=None,
            memory_retrievers={},
            turn_capture_services={},
            cron_scheduler=scheduler,
            task_runtime=None,
            agent_registry=None,
            memory_managers={},
            memory_stores={},
            _turn_runner_ref=[],
            close=close,
        )

    class FakeTurnRunner:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def set_session_lock_provider(self, _provider: Any) -> None:
            pass

    monkeypatch.setattr("agentos.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr(boot, "build_services", fake_build_services)
    monkeypatch.setattr(boot, "_setup_file_logging", lambda config: None)
    monkeypatch.setattr(boot, "emit_skill_filter_banner", lambda config: None)
    engine_starts: list[Any] = []
    monkeypatch.setattr(boot, "_start_trading_engine", engine_starts.append)
    monkeypatch.setattr("agentos.gateway.pidlock.GatewayPidLock.acquire", lambda self: None)
    monkeypatch.setattr("agentos.gateway.pidlock.GatewayPidLock.release", lambda self: None)
    config = GatewayConfig(
        state_dir=str(tmp_path / "state"),
        workspace_dir=str(tmp_path / "workspace"),
        control_ui={"enabled": False},
        channels={"channels": []},
    )
    server = await boot.start_gateway_server(
        config=config, channel_manager=manager, run=False, boot_tracker=tracker
    )
    try:
        assert manager.calls == ["start_all"]
        # Started exactly once, after all four handlers were registered.
        assert scheduler.events == [
            "handler:agent_run",
            "handler:static_message",
            "handler:system_event",
            "handler:script_run",
            "start",
        ]
        # The trading engine (DCA buys, price triggers, wallet sync) starts
        # with the gateway, not on the first trading RPC.
        assert len(engine_starts) == 1
        assert server.readiness.state(gateway_ready=True) == "ready"
        assert server.readiness.startup_complete() is True
    finally:
        await server.close()
    # close() recorded the boot clean.
    with sqlite3.connect(db) as conn:
        clean, reason = conn.execute("SELECT clean, reason FROM gateway_boots").fetchone()
    assert (clean, reason) == (1, "shutdown")


@pytest.mark.asyncio
async def test_a_channel_that_fails_at_boot_is_named_in_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from starlette.testclient import TestClient

    from agentos.gateway import boot

    class FailingManager(_FakeChannelManager):
        async def start_all(self) -> dict[str, bool]:
            return {"tg": False}

        def start_errors(self) -> dict[str, dict[str, str]]:
            return {"tg": {"error_type": "ConnectionError", "error": "no route to host"}}

        def failed_channels(self) -> dict[str, str]:
            return {"tg": "ConnectionError: no route to host"}

    async def fake_build_services(**kwargs: Any) -> Any:
        async def close() -> None:
            return None

        return SimpleNamespace(
            provider_selector=object(),
            tool_registry=object(),
            session_manager=object(),
            skill_loader=object(),
            usage_tracker=object(),
            config=kwargs["config"],
            memory_sync_managers={},
            model_catalog=None,
            memory_retrievers={},
            turn_capture_services={},
            cron_scheduler=None,
            task_runtime=None,
            agent_registry=None,
            memory_managers={},
            memory_stores={},
            _turn_runner_ref=[],
            close=close,
        )

    class FakeTurnRunner:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def set_session_lock_provider(self, _provider: Any) -> None:
            pass

    monkeypatch.setattr("agentos.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr(boot, "build_services", fake_build_services)
    monkeypatch.setattr(boot, "_setup_file_logging", lambda config: None)
    monkeypatch.setattr(boot, "emit_skill_filter_banner", lambda config: None)
    monkeypatch.setattr("agentos.gateway.pidlock.GatewayPidLock.acquire", lambda self: None)
    monkeypatch.setattr("agentos.gateway.pidlock.GatewayPidLock.release", lambda self: None)
    config = GatewayConfig(
        state_dir=str(tmp_path / "state"),
        workspace_dir=str(tmp_path / "workspace"),
        control_ui={"enabled": False},
        channels={"channels": []},
    )
    server = await boot.start_gateway_server(
        config=config,
        channel_manager=FailingManager(),
        run=False,
        boot_tracker=_tracker(tmp_path / "state" / "sessions.db", _Clock()),
    )
    try:
        body = TestClient(server.app, base_url="http://localhost").get("/ready").json()
        # A down channel does not make the gateway unready, but it is named.
        assert body["ready"] is True
        assert body["failing"] == ["channel:tg"]
        assert body["startup"]["failed"] == {"channels": "tg: ConnectionError: no route to host"}
    finally:
        await server.close()
