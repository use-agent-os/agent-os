"""A log line's first argument is its ``event``: no call may pass ``event=`` too.

``log.warning("emit.send_failed", event=name)`` hands structlog two values for
``event`` and raises ``TypeError`` — at every log level, the filtered no-op
included. Seven ``except`` blocks logged that way, so the failure each one
was written to absorb (a listener that raised, a connection that could not
be sent to, a payload that could not be serialized) surfaced as a
``TypeError`` instead, and whatever came after it in the loop never ran.
"""

from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog
from starlette.websockets import WebSocketState

from agentos.application.approval_events import (
    APPROVAL_RESOLVED,
    add_approval_listener,
    notify_approval_event,
)
from agentos.gateway import rpc_sessions
from agentos.gateway import websocket as ws_mod
from agentos.gateway.access import ConnectionSurface
from agentos.gateway.approval_broadcast import ApprovalBroadcaster
from agentos.gateway.auth import AccessContext
from agentos.gateway.websocket import ConnectionRegistry, SubscriptionManager, WsConnection

SRC = Path(__file__).resolve().parents[2] / "src" / "agentos"
LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "fatal", "msg", "log"}
)


def _event_keyword_calls(root: Path) -> list[str]:
    hits: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in LOG_METHODS
                and node.args
                and any(keyword.arg == "event" for keyword in node.keywords)
            ):
                hits.append(f"{path.relative_to(root)}:{node.lineno}")
    return hits


def test_no_log_call_passes_event_as_a_keyword() -> None:
    assert _event_keyword_calls(SRC) == []


def test_the_scan_sees_the_call_it_guards_against(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text(
        'log.warning("emit.send_failed", conn_id=1, event=name)\n'
        'log.info(\n    "multi.line",\n    event=name,\n)\n'
        'log.info("fine", event_name=name)\n'
        "bus.emit(event=name)\n",
        encoding="utf-8",
    )
    assert _event_keyword_calls(tmp_path) == ["bad.py:1", "bad.py:2"]


@pytest.fixture(params=[logging.DEBUG, logging.WARNING, logging.CRITICAL])
def log_level(request: pytest.FixtureRequest) -> int:
    """Every threshold: the bug raised from the filtered no-op as well."""
    level = int(request.param)
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(level))
    return level


def test_a_failing_approval_listener_does_not_fail_the_write(log_level: int) -> None:
    seen: list[str] = []

    def broken(name: str, payload: dict[str, Any]) -> None:
        raise RuntimeError("listener down")

    removers = [
        add_approval_listener(broken),
        add_approval_listener(lambda name, payload: seen.append(name)),
    ]
    try:
        notify_approval_event(APPROVAL_RESOLVED, {"id": "a1"})
    finally:
        for remove in removers:
            remove()
    # The listener after the broken one still hears about the change.
    assert seen == [APPROVAL_RESOLVED]


async def test_a_failing_approval_broadcast_is_absorbed(log_level: int) -> None:
    async def broken(event_name: str, payload: dict[str, Any]) -> None:
        raise RuntimeError("no connections")

    broadcaster = ApprovalBroadcaster(asyncio.get_running_loop(), broken)
    await broadcaster._deliver(APPROVAL_RESOLVED, {"id": "a1"})


class _Conn:
    def __init__(self, conn_id: str, *, broken: bool = False) -> None:
        self.conn_id = conn_id
        self.broken = broken
        self.access = AccessContext(
            surface=ConnectionSurface.CONTROL, admitted=True, credential_verified=True
        )
        self.events: list[str] = []

    @property
    def authenticated(self) -> bool:
        return True

    async def send_event(self, event: str, payload: Any = None, meta: Any = None) -> None:
        if self.broken:
            raise ConnectionError("socket gone")
        self.events.append(event)


async def test_one_dead_connection_does_not_stop_a_control_broadcast(log_level: int) -> None:
    registry = ConnectionRegistry()
    dead, alive = _Conn("dead", broken=True), _Conn("alive")
    registry.register(dead)  # type: ignore[arg-type]
    registry.register(alive)  # type: ignore[arg-type]

    await registry.broadcast_control("approval.requested", {"id": "a1"})

    assert alive.events == ["approval.requested"]


async def test_one_dead_connection_does_not_stop_a_session_event(
    log_level: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = ConnectionRegistry()
    conns = [_Conn("a", broken=True), _Conn("b"), _Conn("c", broken=True), _Conn("d")]
    subscriptions = SubscriptionManager()
    for conn in conns:
        registry.register(conn)  # type: ignore[arg-type]
        subscriptions.subscribe_messages(conn.conn_id, "agent:main:main")
    monkeypatch.setattr(ws_mod, "_registry", registry)
    ctx = SimpleNamespace(subscription_manager=subscriptions, session_manager=None)

    await rpc_sessions._emit_to_subscribers(
        ctx,  # type: ignore[arg-type]
        "agent:main:main",
        "session.message",
        {"text": "hi"},
    )

    assert [conn.events for conn in conns if not conn.broken] == [
        ["session.message"],
        ["session.message"],
    ]


class _Socket:
    def __init__(self) -> None:
        self.client_state = WebSocketState.CONNECTED
        self.sent: list[str] = []
        self.client = None

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.client_state = WebSocketState.DISCONNECTED


async def test_a_payload_that_cannot_be_serialized_is_dropped_not_raised(
    log_level: int,
) -> None:
    socket = _Socket()
    conn = WsConnection(conn_id="c1", ws=socket)  # type: ignore[arg-type]
    conn._start_writer(maxsize=8, enabled=True)
    try:
        # One bad payload must not kill the writer, or reach the caller.
        await conn.send_event("sessions.changed", {"bad": object()})
        await conn.send_event("sessions.changed", {"ok": True})
        for _ in range(50):
            if socket.sent:
                break
            await asyncio.sleep(0.01)
    finally:
        await conn.close()
    assert len(socket.sent) == 1 and '"ok":true' in socket.sent[0]
