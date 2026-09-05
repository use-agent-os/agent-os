"""Tests ensuring background fire-and-forget tasks retain references until completion."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.requests import Request
from starlette.websockets import WebSocketState

from agentos.channels.slack import SlackChannel
from agentos.gateway.websocket import WsConnection, _OutboundFrame


class _FakeWebSocket:
    def __init__(self) -> None:
        self.client_state = WebSocketState.CONNECTED
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.client_state = WebSocketState.DISCONNECTED


class _FakeSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)


@pytest.mark.asyncio
async def test_slack_socket_mode_interactive_retains_task() -> None:
    ch = SlackChannel(token="xoxb-test", slack_channel_id="C123")
    unblock = asyncio.Event()
    started = asyncio.Event()

    async def fake_handle_interactive(payload: dict[str, Any]) -> None:
        started.set()
        await unblock.wait()

    ch._handle_slack_interactive = fake_handle_interactive  # type: ignore[method-assign]

    envelope = {
        "envelope_id": "env-interactive",
        "type": "interactive",
        "payload": {"type": "block_actions", "actions": []},
    }

    ws = _FakeSocket()
    await ch._handle_socket_frame(ws, json.dumps(envelope))
    await started.wait()

    assert len(ch._background_tasks) == 1
    task = next(iter(ch._background_tasks))
    assert not task.done()

    unblock.set()
    await task
    await asyncio.sleep(0)
    assert len(ch._background_tasks) == 0


@pytest.mark.asyncio
async def test_slack_webhook_interactive_retains_task() -> None:
    ch = SlackChannel(token="xoxb-test", slack_channel_id="C123", signing_secret="dummy")
    ch._verify_signature = lambda *args: True  # type: ignore[method-assign]

    unblock = asyncio.Event()
    started = asyncio.Event()

    async def fake_handle_interactive(payload: dict[str, Any]) -> None:
        started.set()
        await unblock.wait()

    ch._handle_slack_interactive = fake_handle_interactive  # type: ignore[method-assign]

    form_data = {
        "payload": json.dumps({"type": "block_actions", "actions": []}),
    }

    async def fake_form() -> dict[str, Any]:
        return form_data

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b""}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/slack/events",
        "headers": [
            (b"content-type", b"application/x-www-form-urlencoded"),
            (b"x-slack-request-timestamp", str(int(time.time())).encode()),
            (b"x-slack-signature", b"dummy"),
        ],
    }
    req = Request(scope, receive=receive)
    req.form = fake_form  # type: ignore[assignment,method-assign]

    resp = await ch._handle_webhook(req)
    assert resp.status_code == 200

    await started.wait()
    assert len(ch._background_tasks) == 1
    task = next(iter(ch._background_tasks))
    assert not task.done()

    unblock.set()
    await task
    await asyncio.sleep(0)
    assert len(ch._background_tasks) == 0


@pytest.mark.asyncio
async def test_ws_connection_overflow_close_retains_task() -> None:
    ws = _FakeWebSocket()
    conn = WsConnection(conn_id="test-conn", ws=ws)  # type: ignore[arg-type]
    conn._outbox = asyncio.Queue(maxsize=1)
    # Fill the queue
    conn._outbox.put_nowait("dummy")

    unblock = asyncio.Event()
    started = asyncio.Event()

    async def fake_force_close(*, reason: str, code: int) -> None:
        started.set()
        await unblock.wait()

    conn._force_close = fake_force_close  # type: ignore[assignment,method-assign]

    frame = _OutboundFrame(
        kind="res",
        classification="control",
        payload=None,
        event_name="test_event",
        res_frame=None,
    )
    conn._enqueue_frame(frame)

    await started.wait()
    assert len(conn._background_tasks) == 1
    task = next(iter(conn._background_tasks))
    assert not task.done()

    unblock.set()
    await task
    await asyncio.sleep(0)
    assert len(conn._background_tasks) == 0


@pytest.mark.asyncio
async def test_turn_runner_compaction_flush_status_retains_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentos.engine.runtime import TurnRunner

    unblock = asyncio.Event()
    started = asyncio.Event()

    async def fake_mark_status(*args: Any, **kwargs: Any) -> None:
        started.set()
        await unblock.wait()

    monkeypatch.setattr(
        "agentos.engine.runtime.mark_compaction_flush_status_with_retry",
        fake_mark_status,
    )

    mock_session_mgr = MagicMock()
    mock_session_mgr.mark_compaction_flush_receipt_status = AsyncMock()

    runner = TurnRunner(
        provider_selector=MagicMock(),
        session_manager=mock_session_mgr,
    )

    runner._schedule_pre_compaction_flush_status_update(
        session_key="sess-1",
        compaction_id="comp-1",
        status="failed_retryable",
        event_prefix="test.prefix",
    )

    await started.wait()
    assert len(runner._background_tasks) == 1
    task = next(iter(runner._background_tasks))
    assert not task.done()

    unblock.set()
    await task
    await asyncio.sleep(0)
    assert len(runner._background_tasks) == 0


@pytest.mark.asyncio
async def test_slack_stop_cancels_background_tasks() -> None:
    ch = SlackChannel(token="xoxb-test", slack_channel_id="C123")
    started = asyncio.Event()

    async def fake_handle_interactive(payload: dict[str, Any]) -> None:
        started.set()
        await asyncio.sleep(100)

    ch._handle_slack_interactive = fake_handle_interactive  # type: ignore[method-assign]

    envelope = {
        "envelope_id": "env-interactive",
        "type": "interactive",
        "payload": {"type": "block_actions", "actions": []},
    }

    ws = _FakeSocket()
    await ch._handle_socket_frame(ws, json.dumps(envelope))
    await started.wait()

    assert len(ch._background_tasks) == 1
    task = next(iter(ch._background_tasks))
    assert not task.done()

    await ch.stop()
    assert len(ch._background_tasks) == 0
    assert task.done()


@pytest.mark.asyncio
async def test_tui_runtime_cancel_retains_abort_task() -> None:
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    from agentos.cli.tui.backend.contracts import TuiRuntimeConfig, TuiRuntimeHooks
    from agentos.cli.tui.backend.runtime import run_tui_runtime

    cancel_cb = None
    input_queue: asyncio.Queue[str | None] = asyncio.Queue()

    class FakeSurface:
        def __init__(self) -> None:
            self.redraw_callback = MagicMock()

        def set_cancel_callback(self, cb: Any) -> None:
            nonlocal cancel_cb
            cancel_cb = cb

        def set_shutdown_callback(self, cb: Any) -> None:
            pass

        def emit_eof(self) -> None:
            input_queue.put_nowait(None)

        async def next_line(self) -> str | None:
            return await input_queue.get()

    @asynccontextmanager
    async def surface_factory() -> AsyncIterator[FakeSurface]:
        yield FakeSurface()

    turn_started = asyncio.Event()
    turn_hang = asyncio.Event()

    async def fake_dispatch(user_input: str) -> bool:
        turn_started.set()
        await turn_hang.wait()
        return True

    abort_started = asyncio.Event()
    abort_unblock = asyncio.Event()

    async def fake_abort() -> None:
        abort_started.set()
        await abort_unblock.wait()

    hooks = TuiRuntimeHooks(on_cancel_active_turn=fake_abort)
    config = TuiRuntimeConfig(
        task_name="test-turn",
        install_signal_handlers=lambda **kwargs: lambda: None,
    )

    runtime_task = asyncio.create_task(
        run_tui_runtime(
            dispatch=fake_dispatch,
            surface_factory=surface_factory,  # type: ignore[arg-type]
            config=config,
            hooks=hooks,
        )
    )

    input_queue.put_nowait("test input")
    await turn_started.wait()

    assert cancel_cb is not None
    cancel_cb()

    await abort_started.wait()
    abort_unblock.set()

    turn_hang.set()
    input_queue.put_nowait(None)
    await runtime_task


@pytest.mark.asyncio
async def test_otlp_write_flush_retains_task() -> None:
    from agentos.observability.otlp import OtlpTraceSink
    from agentos.observability.trace import TraceContext, TraceEvent

    sink = OtlpTraceSink(
        endpoint="http://localhost:4318",
        batch_size=1,
        flush_interval_s=0,
    )

    unblock = asyncio.Event()
    started = asyncio.Event()

    async def fake_flush() -> bool:
        started.set()
        await unblock.wait()
        return True

    sink.flush = fake_flush  # type: ignore[method-assign]

    ctx = TraceContext.new(
        trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
        session_key="sess-test",
    )
    event = TraceEvent(kind="llm_call", context=ctx)

    sink.write(event)
    await started.wait()

    assert len(sink._background_tasks) == 1
    task = next(iter(sink._background_tasks))
    assert not task.done()

    unblock.set()
    await task
    await asyncio.sleep(0)
    assert len(sink._background_tasks) == 0


@pytest.mark.asyncio
async def test_otlp_close_cancels_background_tasks() -> None:
    from agentos.observability.otlp import OtlpTraceSink
    from agentos.observability.trace import TraceContext, TraceEvent

    sink = OtlpTraceSink(
        endpoint="http://localhost:4318",
        batch_size=1,
        flush_interval_s=0,
    )

    started = asyncio.Event()

    async def fake_flush() -> bool:
        started.set()
        await asyncio.sleep(100)
        return True

    sink.flush = fake_flush  # type: ignore[method-assign]

    ctx = TraceContext.new(
        trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
        session_key="sess-test",
    )
    event = TraceEvent(kind="llm_call", context=ctx)

    sink.write(event)
    await started.wait()

    assert len(sink._background_tasks) == 1
    task = next(iter(sink._background_tasks))
    assert not task.done()

    await sink.close()
    assert len(sink._background_tasks) == 0
    assert task.done()


@pytest.mark.asyncio
async def test_retain_task_helper() -> None:
    from agentos.asyncio_utils import _BACKGROUND_TASKS, retain_task

    tasks: set[asyncio.Task[Any]] = set()

    async def sample_coro() -> None:
        await asyncio.sleep(0.01)

    t = asyncio.create_task(sample_coro())
    retain_task(t, tasks)
    assert t in tasks
    await t
    await asyncio.sleep(0)
    assert t not in tasks

    # Test default module-level set
    t2 = asyncio.create_task(sample_coro())
    retain_task(t2)
    assert t2 in _BACKGROUND_TASKS
    await t2
    await asyncio.sleep(0)
    assert t2 not in _BACKGROUND_TASKS
