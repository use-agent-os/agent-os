"""SIGTERM / SIGINT run the orderly gateway shutdown; a second signal forces.

Every non-interactive stop sends SIGTERM (``agentos gateway stop``, the
desktop supervisor, launchd, systemd). uvicorn's own signal capture used to
record it, return from ``serve()`` and re-raise it afterwards, so the process
died before ``GatewayServer.close()`` drained turns, stopped channels and
told clients. The first test runs a real gateway process.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.gateway import boot
from agentos.gateway.boot import FORCE_DRAIN_TIMEOUT_S, GatewayServer
from agentos.gateway.config import GatewayConfig
from agentos.gateway.restart import GatewayRestarter, RestartOrigin, restart_request_path

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_ready(port: int, proc: subprocess.Popen[str], timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"gateway exited early: {proc.returncode}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/ready", timeout=1) as res:
                if res.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.2)
    raise AssertionError("gateway did not become ready")


async def _handshake(port: int) -> Any:
    import websockets

    ws = await websockets.connect(f"ws://127.0.0.1:{port}/ws")
    challenge = json.loads(await ws.recv())
    assert challenge["event"] == "connect.challenge"
    await ws.send(
        json.dumps(
            {
                "type": "req",
                "id": "c",
                "method": "connect",
                "params": {"minProtocol": 3, "maxProtocol": 3},
            }
        )
    )
    hello = json.loads(await ws.recv())
    assert hello["type"] == "hello-ok"
    return ws


async def _frames_until_close(ws: Any) -> tuple[list[dict[str, Any]], int | None]:
    import websockets

    frames: list[dict[str, Any]] = []
    try:
        while True:
            frames.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=30)))
    except websockets.ConnectionClosed as exc:
        return frames, exc.rcvd.code if exc.rcvd is not None else None


def test_sigterm_drains_broadcasts_shutdown_and_exits_zero(tmp_path: Path) -> None:
    port = _free_port()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("AGENTOS_GATEWAY_", "AGENTOS_AUTH_"))
    }
    env.update(
        AGENTOS_STATE_DIR=str(tmp_path / "state"),
        PYTHONUNBUFFERED="1",
        NO_COLOR="1",
        AGENTOS_NO_UPDATE_NOTICE="1",
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "agentos.cli.main", "gateway", "run", "--port", str(port)],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_ready(port, proc)

        async def scenario() -> tuple[list[dict[str, Any]], int | None]:
            ws = await _handshake(port)
            proc.send_signal(signal.SIGTERM)
            return await _frames_until_close(ws)

        frames, close_code = asyncio.run(scenario())
        output, _ = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    assert proc.returncode == 0, output
    shutdown = [frame for frame in frames if frame.get("event") == "shutdown"]
    # A signal is a terminal stop: no restart estimate (supervisor contract).
    assert shutdown and shutdown[0]["payload"] == {
        "reason": "signal:SIGTERM",
        "restart_expected_ms": None,
    }
    assert close_code == 1012
    assert "gateway.shutdown_signal" in output
    assert "gateway.mcp_clients_closed" in output
    assert "reason=signal:SIGTERM" in output
    assert "gateway.stopped" in output


# ── in-process ─────────────────────────────────────────────────────────────


class _HangingRuntime:
    """A task runtime whose graceful drain never finishes on its own."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.admitting = True
        self._turn: asyncio.Future[None] | None = None

    def stop_admission(self) -> None:
        self.admitting = False

    async def shutdown(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        if kwargs.get("graceful"):
            self._turn = asyncio.get_running_loop().create_future()
            await asyncio.shield(self._turn)
        elif kwargs.get("cancel") and self._turn is not None and not self._turn.done():
            self._turn.set_result(None)


class _FakeUvicorn:
    def __init__(self) -> None:
        self.should_exit = False
        self.force_exit = False


def _server(runtime: Any = None) -> GatewayServer:
    services = SimpleNamespace(task_runtime=runtime, close=_async_noop)
    server = GatewayServer(app=SimpleNamespace(state=SimpleNamespace()), config=GatewayConfig())
    server._services = services  # type: ignore[assignment]
    server._server = _FakeUvicorn()  # type: ignore[assignment]
    return server


async def _async_noop() -> None:
    return None


async def test_first_signal_starts_close_once_second_forces_the_drain() -> None:
    runtime = _HangingRuntime()
    server = _server(runtime)

    server._on_shutdown_signal(int(signal.SIGTERM))
    task = server.shutdown_task
    assert task is not None
    assert runtime.admitting is False
    assert server.app.state.gateway_ready is False
    await asyncio.sleep(0.05)
    assert not task.done()  # still draining the hanging turn
    assert runtime.calls == [{"graceful": True, "graceful_timeout": 30.0}]

    # The same signal again is the operator insisting: force it.
    server._on_shutdown_signal(int(signal.SIGTERM))
    assert server.shutdown_task is task
    await asyncio.wait_for(task, timeout=FORCE_DRAIN_TIMEOUT_S + 3)
    assert runtime.calls[1] == {"cancel": True, "timeout": FORCE_DRAIN_TIMEOUT_S}
    assert server._server.should_exit is True  # type: ignore[union-attr]
    # Not force_exit: that skips the ASGI lifespan shutdown (browser cleanup)
    # and leaves its task to die with a CancelledError traceback.
    assert server._server.force_exit is False  # type: ignore[union-attr]


async def test_request_shutdown_is_idempotent() -> None:
    server = _server()
    first = server.request_shutdown("signal:SIGINT")
    assert server.request_shutdown("signal:SIGTERM") is first
    await first


async def test_signal_during_a_restart_keeps_the_drain_but_never_execs(tmp_path: Path) -> None:
    events: list[str] = []

    class _Server:
        def __init__(self) -> None:
            self.app = SimpleNamespace(state=SimpleNamespace(gateway_ready=True))
            self._services = SimpleNamespace(task_runtime=None)

        async def close(self, reason: str = "shutdown", **kwargs: Any) -> None:
            events.append(f"close:{reason}")

    async def _sleep(_seconds: float) -> None:
        return None

    execs: list[Any] = []
    restarter = GatewayRestarter(
        _Server(),
        SimpleNamespace(state_dir=str(tmp_path / "state")),
        exec_fn=lambda *args: execs.append(args),
        task_factory=asyncio.create_task,
        sleep=_sleep,
        supported=True,
    )
    restarter.request(reason="test", mode="safe", origin=RestartOrigin(kind="none"))
    restarter.cancel_exec("signal:SIGTERM")
    await restarter.task

    assert events == ["close:restart:safe"]
    assert restarter.exec_plan is None
    restarter.exec_if_pending()
    assert execs == []
    assert not restart_request_path(SimpleNamespace(state_dir=str(tmp_path / "state"))).exists()


def test_start_gateway_server_owns_signals_and_disables_uvicorn_capture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    holder: dict[str, Any] = {}

    class _Config:
        def __init__(self, **kwargs: Any) -> None:
            pass

    class _Server:
        def __init__(self, config: Any) -> None:
            self.should_exit = False
            self.force_exit = False
            holder["server"] = self

        def capture_signals(self) -> Any:  # pragma: no cover - must be replaced
            raise AssertionError("uvicorn signal capture must be disabled")

        async def serve(self) -> None:
            with self.capture_signals():
                while not self.should_exit:
                    await asyncio.sleep(0.01)

    monkeypatch.setattr(boot.uvicorn, "Config", _Config)
    monkeypatch.setattr(boot.uvicorn, "Server", _Server)
    monkeypatch.setattr(boot, "emit_skill_filter_banner", lambda config: None)
    monkeypatch.setattr("agentos.gateway.pidlock.GatewayPidLock.acquire", lambda self: None)
    monkeypatch.setattr("agentos.gateway.pidlock.GatewayPidLock.release", lambda self: None)

    async def run() -> tuple[tuple[int, ...], tuple[int, ...]]:
        config = GatewayConfig(
            state_dir=str(tmp_path / "state"),
            workspace_dir=str(tmp_path / "workspace"),
            control_ui={"enabled": False},
            channels={"channels": []},
        )
        handle = await boot.start_gateway_server(config=config, run=True)
        installed = handle._installed_signals
        await asyncio.sleep(0.05)
        await handle.close()
        return installed, handle._installed_signals

    installed, after_close = asyncio.run(run())
    assert set(installed) == {int(signal.SIGTERM), int(signal.SIGINT)}
    assert after_close == ()
    assert holder["server"].capture_signals is contextlib.nullcontext


class _FrozenConnection:
    """A client that never answers the closing handshake."""

    def __init__(self) -> None:
        self.authenticated = True
        self.closed = False

    async def send_event(self, event: str, payload: Any = None) -> None:
        return None

    async def flush(self, timeout: float = 1.0) -> None:
        return None

    async def close(self) -> None:
        await asyncio.sleep(3600)


async def test_a_frozen_client_cannot_stall_the_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentos.gateway.websocket import ConnectionRegistry

    registry = ConnectionRegistry()
    for index in range(3):
        conn = _FrozenConnection()
        conn.conn_id = f"frozen-{index}"  # type: ignore[attr-defined]
        registry.register(conn)  # type: ignore[arg-type]
    monkeypatch.setattr(boot, "get_registry", lambda: registry)
    monkeypatch.setattr(boot, "WS_CLOSE_TIMEOUT_S", 0.2)
    server = _server()

    started = asyncio.get_running_loop().time()
    await asyncio.wait_for(server.close("test"), timeout=5)
    # Concurrent and bounded: three frozen sockets cost one timeout, not three.
    assert asyncio.get_running_loop().time() - started < 1.5


def test_uvicorn_bounds_its_own_graceful_wait(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    class _Config:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    class _Server:
        def __init__(self, config: Any) -> None:
            self.should_exit = False
            self.force_exit = False

        async def serve(self) -> None:
            while not self.should_exit:
                await asyncio.sleep(0.01)

    monkeypatch.setattr(boot.uvicorn, "Config", _Config)
    monkeypatch.setattr(boot.uvicorn, "Server", _Server)
    monkeypatch.setattr(boot, "emit_skill_filter_banner", lambda config: None)
    monkeypatch.setattr("agentos.gateway.pidlock.GatewayPidLock.acquire", lambda self: None)
    monkeypatch.setattr("agentos.gateway.pidlock.GatewayPidLock.release", lambda self: None)

    async def run() -> None:
        config = GatewayConfig(
            state_dir=str(tmp_path / "state"),
            workspace_dir=str(tmp_path / "workspace"),
            control_ui={"enabled": False},
            channels={"channels": []},
        )
        handle = await boot.start_gateway_server(config=config, run=True)
        await handle.close()

    asyncio.run(run())
    assert captured["timeout_graceful_shutdown"] == boot.UVICORN_GRACEFUL_SHUTDOWN_S
    from agentos.gateway.ws_transport import GatewayWebSocketProtocol

    assert captured["ws"] is GatewayWebSocketProtocol


async def test_the_wake_worker_stops_before_the_runtime_drains() -> None:
    from agentos.gateway.subagent_wakes import get_wake_outbox, set_wake_outbox

    order: list[str] = []
    outbox_during_drain: list[Any] = []
    sentinel = object()

    class _Runtime:
        def stop_admission(self) -> None:
            return None

        async def shutdown(self, **kwargs: Any) -> None:
            order.append("drain")
            outbox_during_drain.append(get_wake_outbox())

    class _Worker:
        async def stop(self) -> None:
            order.append("worker_stop")

    server = _server(_Runtime())
    server._subagent_wake_worker = _Worker()
    server._wake_outbox_installed = True
    set_wake_outbox(sentinel)  # type: ignore[arg-type]
    try:
        await server.close("shutdown", drain_timeout=1.0)
    finally:
        installed = get_wake_outbox()
        set_wake_outbox(None)

    assert order[:2] == ["worker_stop", "drain"]
    # Turns ending during the drain still record their wakes.
    assert outbox_during_drain == [sentinel]
    assert installed is None
