"""MCP resilience: background reconnect with backoff, and reviving a crashed stdio server."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Any

import pytest

from agentos.gateway.boot import _discover_configured_mcp_servers
from agentos.gateway.config import GatewayConfig
from agentos.mcp import discovery
from agentos.mcp import supervisor as supervisor_mod
from agentos.mcp.stdio import MCPStdioClient
from agentos.mcp.supervisor import Backoff, MCPSupervisor
from agentos.mcp.types import MCPServerConfig
from agentos.tools.registry import ToolRegistry

DEMO_SERVER = Path(__file__).resolve().parents[1] / "fixtures" / "mcp" / "fastmcp_demo_server.py"


class FakeSleep:
    """Records each backoff delay and returns at once (no real waiting)."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        await asyncio.sleep(0)


async def _until(predicate, timeout: float = 15.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


class LogRecorder:
    """Stands in for a module's structlog logger.

    ``capture_logs`` misses a logger that was cached on first use, which the
    gateway tests do before these run; patching the module logger does not.
    """

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def _record(self, event: str, **kw: Any) -> None:
        self.events.append({"event": event, **kw})

    info = warning = error = debug = _record


@pytest.fixture
def logs(monkeypatch) -> list[dict[str, Any]]:
    import agentos.gateway.boot as boot

    recorder = LogRecorder()
    monkeypatch.setattr(supervisor_mod, "log", recorder)
    monkeypatch.setattr(boot, "log", recorder)
    return recorder.events


@pytest.fixture(autouse=True)
async def _clean(monkeypatch):
    # Depends on monkeypatch so background tasks stop before patches revert.
    yield
    await supervisor_mod.shutdown_supervisor()
    await discovery.close_active_clients()


def _demo(name: str = "demo") -> MCPServerConfig:
    return MCPServerConfig(
        name=name, transport="stdio", command=sys.executable, args=[str(DEMO_SERVER)]
    )


# ── backoff ──────────────────────────────────────────────────────────


def test_backoff_doubles_from_30s_and_caps_at_10min() -> None:
    backoff = Backoff()
    delays = [backoff.next_delay() for _ in range(8)]
    assert delays == [30, 60, 120, 240, 480, 600, 600, 600]
    backoff.reset()
    assert backoff.next_delay() == 30


# ── retry loop with mocked time ──────────────────────────────────────


@pytest.mark.asyncio
async def test_retry_backs_off_until_the_server_connects(monkeypatch, logs) -> None:
    attempts: list[str] = []

    async def discover(config: MCPServerConfig, registry: ToolRegistry, **_: Any) -> list[str]:
        attempts.append(config.name)
        if len(attempts) < 3:
            raise ConnectionError(f"refused #{len(attempts)}")
        return ["mcp_x"]

    monkeypatch.setattr(discovery, "discover_and_register", discover)
    sleep = FakeSleep()
    sup = MCPSupervisor(
        ToolRegistry(), default_connect_timeout=5.0, sleep=sleep, clock=lambda: 1000.0
    )

    sup.schedule_retry(_demo("flaky"), error="boot failed", reason="boot")
    state = sup.retry_state("flaky")
    assert state is not None and state.reason == "boot"
    await _until(lambda: sup.retry_state("flaky") is None)

    assert sleep.delays == [30, 60, 120]
    assert len(attempts) == 3
    failures = [entry for entry in logs if entry["event"] == "mcp.reconnect_failed"]
    # Exactly one line per failed attempt, carrying the next delay.
    assert [(f["attempt"], f["next_retry_seconds"]) for f in failures] == [(1, 60), (2, 120)]
    assert [entry["event"] for entry in logs].count("mcp.reconnected") == 1
    await sup.aclose()


@pytest.mark.asyncio
async def test_retry_state_reports_next_retry_time(monkeypatch) -> None:
    gate = asyncio.Event()

    async def blocked_sleep(delay: float) -> None:
        await gate.wait()

    monkeypatch.setattr(
        discovery, "discover_and_register", lambda *a, **k: asyncio.sleep(0, result=[])
    )
    sup = MCPSupervisor(
        ToolRegistry(), default_connect_timeout=5.0, sleep=blocked_sleep, clock=lambda: 1000.0
    )
    sup.schedule_retry(_demo("s"), error="nope", reason="boot")
    await asyncio.sleep(0)

    state = sup.retry_state("s")
    assert state is not None
    assert state.to_dict() == {
        "reason": "boot",
        "attempts": 0,
        "lastError": "nope",
        "nextRetryAt": 1030.0,
    }
    # A second schedule while one is pending does not start another loop.
    sup.schedule_retry(_demo("s"), error="again", reason="boot")
    assert sup.retry_state("s") is state
    gate.set()
    await _until(lambda: sup.retry_state("s") is None)
    await sup.aclose()


@pytest.mark.asyncio
async def test_forget_and_close_cancel_pending_retries() -> None:
    gate = asyncio.Event()

    async def blocked_sleep(delay: float) -> None:
        await gate.wait()

    sup = MCPSupervisor(ToolRegistry(), default_connect_timeout=5.0, sleep=blocked_sleep)
    sup.schedule_retry(_demo("a"), error="x", reason="boot")
    sup.schedule_retry(_demo("b"), error="x", reason="boot")
    task_a = sup._retries["a"]
    task_b = sup._retries["b"]

    sup.forget("a")
    await asyncio.sleep(0)
    assert task_a.cancelled()
    assert sup.retry_state("a") is None

    await sup.aclose()
    assert task_b.cancelled()
    # Closed: nothing new is scheduled.
    sup.schedule_retry(_demo("c"), error="x", reason="boot")
    assert sup.retry_state("c") is None


@pytest.mark.asyncio
async def test_disabled_server_is_never_retried() -> None:
    sup = MCPSupervisor(ToolRegistry(), default_connect_timeout=5.0, sleep=FakeSleep())
    config = _demo("off")
    config.enabled = False
    sup.schedule_retry(config, error="x", reason="boot")
    assert sup.retry_state("off") is None


# ── boot integration ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_boot_failure_schedules_a_background_retry(tmp_path, monkeypatch, logs) -> None:
    registry = ToolRegistry()
    sleep = FakeSleep()
    calls: list[str] = []

    async def discover(config: MCPServerConfig, reg: ToolRegistry, **_: Any) -> list[str]:
        calls.append(config.name)
        if len(calls) == 1:
            raise ConnectionError("not yet")
        return ["mcp_ok"]

    async def connect_and_list(config: MCPServerConfig) -> Any:
        await discover(config, registry)

    monkeypatch.setattr(discovery, "connect_and_list", connect_and_list)
    monkeypatch.setattr(discovery, "discover_and_register", discover)
    supervisor_mod._supervisor = MCPSupervisor(registry, default_connect_timeout=5.0, sleep=sleep)
    config = GatewayConfig(
        state_dir=str(tmp_path),
        mcp={"enabled": True, "servers": [{"name": "late", "command": "x"}]},
    )

    await _discover_configured_mcp_servers(config, registry)
    boot_failures = [e for e in logs if e["event"] == "build_services.mcp_server_failed"]
    assert len(boot_failures) == 1
    assert boot_failures[0]["retry_in_seconds"] == 30
    await _until(lambda: len(calls) == 2)
    await _until(lambda: supervisor_mod._supervisor.retry_state("late") is None)

    assert sleep.delays == [30]


@pytest.mark.asyncio
async def test_mcp_status_exposes_retry_state(tmp_path) -> None:
    from agentos.gateway.rpc import RpcContext, get_dispatcher

    gate = asyncio.Event()

    async def blocked_sleep(delay: float) -> None:
        await gate.wait()

    registry = ToolRegistry()
    sup = MCPSupervisor(
        registry, default_connect_timeout=5.0, sleep=blocked_sleep, clock=lambda: 50.0
    )
    supervisor_mod._supervisor = sup
    sup.schedule_retry(_demo("demo"), error="refused", reason="boot")
    await asyncio.sleep(0)
    ctx = RpcContext(
        conn_id="t",
        config=GatewayConfig(
            state_dir=str(tmp_path),
            mcp={"enabled": True, "servers": [{"name": "demo", "command": "x"}]},
        ),
        tool_registry=registry,
    )

    result = await get_dispatcher().dispatch("r1", "mcp.status", {}, ctx)

    (server,) = result.payload["servers"]
    assert server["connected"] is False
    assert server["retry"] == {
        "reason": "boot",
        "attempts": 0,
        "lastError": "refused",
        "nextRetryAt": 80.0,
    }
    # The operator disconnecting takes the server over: the retry stops.
    await get_dispatcher().dispatch("r2", "mcp.disconnect", {"name": "demo"}, ctx)
    assert sup.retry_state("demo") is None


# ── a real stdio server that dies ────────────────────────────────────


def _pid(name: str) -> int:
    entry = next(e for e in discovery.active_clients_snapshot() if e.server_name == name)
    assert isinstance(entry.client, MCPStdioClient)
    process = entry.client._process
    assert process is not None
    return process.pid


@pytest.mark.asyncio
async def test_crashed_stdio_server_is_marked_unavailable_and_revived(logs) -> None:
    registry = ToolRegistry()
    sleep = FakeSleep()
    sup = MCPSupervisor(registry, default_connect_timeout=20.0, sleep=sleep)
    config = _demo()
    await discovery.discover_and_register(config, registry)
    sup.watch(config)
    first_pid = _pid("demo")
    assert registry.get("mcp_search") is not None

    unregistered = asyncio.Event()
    real_disconnect = discovery.disconnect_and_unregister

    async def spy_disconnect(owner: str, reg: ToolRegistry) -> int:
        closed = await real_disconnect(owner, reg)
        if reg.get("mcp_search") is None:
            unregistered.set()
        return closed

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(discovery, "disconnect_and_unregister", spy_disconnect)
        os.kill(first_pid, signal.SIGKILL)
        await asyncio.wait_for(unregistered.wait(), 15)
        await _until(
            lambda: registry.get("mcp_search") is not None and sup.retry_state("demo") is None
        )

    assert _pid("demo") != first_pid
    assert sleep.delays == [30]
    events = [e["event"] for e in logs]
    assert events.count("mcp.server_exited") == 1
    assert "mcp.reconnected" in events
    # The revived server is watched again.
    assert "demo" in sup._watchers
    tool = registry.get("mcp_search")
    assert tool is not None
    assert "results for q" in await tool.handler(query="q")
    await sup.aclose()


@pytest.mark.asyncio
async def test_deliberate_disconnect_is_not_revived() -> None:
    registry = ToolRegistry()
    sleep = FakeSleep()
    sup = MCPSupervisor(registry, default_connect_timeout=20.0, sleep=sleep)
    config = _demo()
    await discovery.discover_and_register(config, registry)
    sup.watch(config)
    watcher = sup._watchers["demo"]

    await discovery.disconnect_and_unregister("demo", registry)
    await asyncio.wait_for(watcher, 10)

    assert sup.retry_state("demo") is None
    assert sleep.delays == []
    assert registry.get("mcp_search") is None
    await sup.aclose()
