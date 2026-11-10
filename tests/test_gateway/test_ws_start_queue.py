"""Bounded request start queue and the byte-based slow-consumer policy."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import structlog
from starlette.websockets import WebSocketDisconnect, WebSocketState

from agentos.gateway.access import ConnectionSurface
from agentos.gateway.auth import AccessContext
from agentos.gateway.config import GatewayConfig
from agentos.gateway.rpc.registry import RpcRegistry
from agentos.gateway.websocket import (
    MAX_CONCURRENT_REQUESTS,
    REQUEST_START_TIMEOUT_S,
    RequestStartQueue,
    WsConnection,
    _message_loop,
    request_lane,
)

# ── fakes ──────────────────────────────────────────────────────────────────


class _FakeWs:
    client_state = WebSocketState.CONNECTED

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.got_sent = asyncio.Event()

    async def receive_text(self) -> str:
        raw = await self.inbox.get()
        if raw is None:
            raise WebSocketDisconnect()
        return raw

    async def send_text(self, raw: str) -> None:
        self.sent.append(json.loads(raw))
        self.got_sent.set()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        return None


def _conn(ws: _FakeWs, conn_id: str = "c") -> WsConnection:
    return WsConnection(
        conn_id=conn_id,
        ws=ws,  # type: ignore[arg-type]
        access=AccessContext(
            surface=ConnectionSurface.CONTROL, admitted=True, credential_verified=True
        ),
    )


def _req(req_id: str, method: str, params: dict[str, Any] | None = None) -> str:
    return json.dumps({"type": "req", "id": req_id, "method": method, "params": params or {}})


async def _answered(ws: _FakeWs, req_id: str) -> dict[str, Any]:
    while True:
        for frame in ws.sent:
            if frame.get("id") == req_id:
                return frame
        ws.got_sent.clear()
        await ws.got_sent.wait()


def _ids(ws: _FakeWs) -> list[str]:
    return [frame["id"] for frame in ws.sent]


# ── lanes ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "params", "lane"),
    [
        ("chat.send", {"sessionKey": "agent:main:webchat:a"}, "session:agent:main:webchat:a"),
        ("chat.history", {"session_key": "agent:Main:webchat:a"}, "session:agent:main:webchat:a"),
        ("sessions.abort", {"key": "agent:main:webchat:a"}, "session:agent:main:webchat:a"),
        ("plan.mode.set", {"sessionKey": "agent:main:webchat:a"}, "session:agent:main:webchat:a"),
        # ``key`` names a session only on sessions.*; env.set's key is a variable.
        ("env.set", {"key": "OPENAI_API_KEY"}, "barrier"),
        ("sessions.list", {}, "read"),
        ("config.get", None, "read"),
        ("sessions.create", {"agentId": "main"}, "barrier"),
        ("config.patch", None, "barrier"),
        ("chat.suggestNext", {"sessionKey": "agent:main:webchat:a"}, None),
        ("providers.probe", {}, None),
    ],
)
def test_request_lane(method: str, params: Any, lane: str | None) -> None:
    assert request_lane(method, params) == lane


def test_defaults_match_the_contract() -> None:
    assert MAX_CONCURRENT_REQUESTS == 4
    assert REQUEST_START_TIMEOUT_S == 30.0


# ── ordering and concurrency through the message loop ─────────────────────


async def test_slow_request_on_one_session_does_not_hold_back_another() -> None:
    registry = RpcRegistry()
    release = asyncio.Event()

    @registry.method("chat.history")
    async def _history(params: dict | None, ctx: Any) -> dict:
        if params and params.get("sessionKey") == "slow":
            await release.wait()
        return {"key": params.get("sessionKey") if params else None}

    ws = _FakeWs()
    loop = asyncio.create_task(_message_loop(_conn(ws), GatewayConfig(), registry, None))
    await ws.inbox.put(_req("1", "chat.history", {"sessionKey": "slow"}))
    await ws.inbox.put(_req("2", "chat.history", {"sessionKey": "fast"}))
    await asyncio.wait_for(_answered(ws, "2"), timeout=2.0)
    assert _ids(ws) == ["2"]
    release.set()
    await asyncio.wait_for(_answered(ws, "1"), timeout=2.0)
    await ws.inbox.put(None)
    await asyncio.wait_for(loop, timeout=2.0)


async def test_requests_on_the_same_session_keep_their_order() -> None:
    registry = RpcRegistry()
    release = asyncio.Event()
    order: list[str] = []

    @registry.method("chat.send")
    async def _send(params: dict | None, ctx: Any) -> dict:
        await release.wait()
        order.append("send")
        return {}

    @registry.method("sessions.abort")
    async def _abort(params: dict | None, ctx: Any) -> dict:
        order.append("abort")
        return {}

    ws = _FakeWs()
    loop = asyncio.create_task(_message_loop(_conn(ws), GatewayConfig(), registry, None))
    # Different spellings of the same session land in one lane.
    await ws.inbox.put(_req("1", "chat.send", {"sessionKey": "agent:main:webchat:x"}))
    await ws.inbox.put(_req("2", "sessions.abort", {"key": "agent:MAIN:webchat:x"}))
    await asyncio.sleep(0.05)
    assert ws.sent == []
    release.set()
    await asyncio.wait_for(_answered(ws, "2"), timeout=2.0)
    assert order == ["send", "abort"]
    assert _ids(ws) == ["1", "2"]
    await ws.inbox.put(None)
    await asyncio.wait_for(loop, timeout=2.0)


async def test_session_less_requests_are_barriers() -> None:
    registry = RpcRegistry()
    release = asyncio.Event()
    order: list[str] = []

    @registry.method("chat.history")
    async def _history(params: dict | None, ctx: Any) -> dict:
        await release.wait()
        order.append("history")
        return {}

    @registry.method("config.patch")
    async def _patch(params: dict | None, ctx: Any) -> dict:
        order.append("patch")
        return {}

    @registry.method("chat.send")
    async def _send(params: dict | None, ctx: Any) -> dict:
        order.append("send")
        return {}

    ws = _FakeWs()
    loop = asyncio.create_task(_message_loop(_conn(ws), GatewayConfig(), registry, None))
    await ws.inbox.put(_req("1", "chat.history", {"sessionKey": "a"}))
    await ws.inbox.put(_req("2", "config.patch"))
    await ws.inbox.put(_req("3", "chat.send", {"sessionKey": "b"}))
    await asyncio.sleep(0.05)
    # The barrier waits for the earlier history; the later send waits for it.
    assert order == []
    release.set()
    await asyncio.wait_for(_answered(ws, "3"), timeout=2.0)
    assert order == ["history", "patch", "send"]
    await ws.inbox.put(None)
    await asyncio.wait_for(loop, timeout=2.0)


async def test_session_less_reads_do_not_wait_for_a_slow_session_but_barriers_do() -> None:
    registry = RpcRegistry()
    release = asyncio.Event()
    order: list[str] = []

    @registry.method("chat.history")
    async def _history(params: dict | None, ctx: Any) -> dict:
        await release.wait()
        order.append("history")
        return {}

    @registry.method("sessions.list")
    async def _list(params: dict | None, ctx: Any) -> dict:
        order.append("list")
        return {}

    @registry.method("config.patch")
    async def _patch(params: dict | None, ctx: Any) -> dict:
        order.append("patch")
        return {}

    ws = _FakeWs()
    loop = asyncio.create_task(_message_loop(_conn(ws), GatewayConfig(), registry, None))
    await ws.inbox.put(_req("1", "chat.history", {"sessionKey": "slow"}))
    await ws.inbox.put(_req("2", "sessions.list"))
    await asyncio.wait_for(_answered(ws, "2"), timeout=2.0)
    assert order == ["list"]  # the poll did not wait for the slow history
    await ws.inbox.put(_req("3", "config.patch"))
    await ws.inbox.put(_req("4", "sessions.list"))
    await asyncio.sleep(0.05)
    assert order == ["list"]  # the barrier waits for history; the read waits for it
    release.set()
    await asyncio.wait_for(_answered(ws, "4"), timeout=2.0)
    assert order == ["list", "history", "patch", "list"]
    await ws.inbox.put(None)
    await asyncio.wait_for(loop, timeout=2.0)


async def test_at_most_four_requests_run_at_once() -> None:
    registry = RpcRegistry()
    release = asyncio.Event()
    running = 0
    peak = 0

    @registry.method("chat.history")
    async def _history(params: dict | None, ctx: Any) -> dict:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await release.wait()
        running -= 1
        return {}

    ws = _FakeWs()
    loop = asyncio.create_task(_message_loop(_conn(ws), GatewayConfig(), registry, None))
    for i in range(6):
        await ws.inbox.put(_req(str(i), "chat.history", {"sessionKey": f"s{i}"}))
    await asyncio.sleep(0.05)
    assert running == 4
    release.set()
    for i in range(6):
        await asyncio.wait_for(_answered(ws, str(i)), timeout=2.0)
    assert peak == 4
    await ws.inbox.put(None)
    await asyncio.wait_for(loop, timeout=2.0)


async def test_request_that_cannot_start_in_time_is_refused_retryable() -> None:
    registry = RpcRegistry()
    release = asyncio.Event()

    @registry.method("chat.send")
    async def _send(params: dict | None, ctx: Any) -> dict:
        await release.wait()
        return {}

    ws = _FakeWs()
    queue = RequestStartQueue(start_timeout_s=0.1)
    loop = asyncio.create_task(
        _message_loop(_conn(ws), GatewayConfig(), registry, None, start_queue=queue)
    )
    await ws.inbox.put(_req("1", "chat.send", {"sessionKey": "a"}))
    await ws.inbox.put(_req("2", "chat.send", {"sessionKey": "a"}))
    res = await asyncio.wait_for(_answered(ws, "2"), timeout=2.0)
    assert res["ok"] is False
    assert res["error"]["code"] == "UNAVAILABLE"
    assert res["error"]["retryable"] is True
    assert res["error"]["details"] == {"code": "REQUEST_START_TIMEOUT", "method": "chat.send"}
    release.set()
    assert (await asyncio.wait_for(_answered(ws, "1"), timeout=2.0))["ok"] is True
    await ws.inbox.put(None)
    await asyncio.wait_for(loop, timeout=2.0)


@pytest.mark.parametrize("lane", ["session:a", "barrier"])
async def test_a_refused_request_still_holds_back_the_ones_behind_it(lane: str) -> None:
    """a1 runs long, a2 is refused for timing out, a3 must not start before a1 ends."""
    queue = RequestStartQueue(start_timeout_s=0.1)
    release = asyncio.Event()
    events: list[str] = []

    async def _a1() -> None:
        events.append("a1 start")
        await release.wait()
        events.append("a1 end")

    async def _a2() -> None:  # pragma: no cover - refused before it starts
        events.append("a2 start")

    async def _a3() -> None:
        events.append("a3 start")

    async def _refused(name: str) -> None:
        events.append(f"{name} refused")

    first = queue.submit(lane, _a1, lambda: _refused("a1"))
    second = queue.submit(lane, _a2, lambda: _refused("a2"))
    await asyncio.sleep(0.15)
    assert events == ["a1 start", "a2 refused"]
    third = queue.submit(lane, _a3, lambda: _refused("a3"))
    await asyncio.sleep(0.03)
    assert "a3 start" not in events  # still behind a1, through the refused a2
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second, third), timeout=2.0)
    assert events == ["a1 start", "a2 refused", "a1 end", "a3 start"]


async def test_slot_wait_also_counts_against_the_start_timeout() -> None:
    queue = RequestStartQueue(max_concurrent=1, start_timeout_s=0.1)
    release = asyncio.Event()
    timed_out: list[str] = []

    async def _block() -> None:
        await release.wait()

    async def _never() -> None:  # pragma: no cover - must not start
        raise AssertionError("started past its deadline")

    async def _timeout() -> None:
        timed_out.append("late")

    first = queue.submit("session:a", _block, _timeout)
    second = queue.submit("session:b", _never, _timeout)
    await asyncio.wait_for(second, timeout=2.0)
    assert timed_out == ["late"]
    release.set()
    await asyncio.wait_for(first, timeout=2.0)


async def test_too_many_pending_requests_are_refused_at_once() -> None:
    registry = RpcRegistry()
    release = asyncio.Event()

    @registry.method("chat.send")
    async def _send(params: dict | None, ctx: Any) -> dict:
        await release.wait()
        return {}

    ws = _FakeWs()
    queue = RequestStartQueue(max_pending=2)
    loop = asyncio.create_task(
        _message_loop(_conn(ws), GatewayConfig(), registry, None, start_queue=queue)
    )
    for i in range(3):
        await ws.inbox.put(_req(str(i), "chat.send", {"sessionKey": "a"}))
    res = await asyncio.wait_for(_answered(ws, "2"), timeout=2.0)
    assert res["error"]["code"] == "UNAVAILABLE"
    assert res["error"]["details"]["code"] == "QUEUE_FULL"
    release.set()
    await ws.inbox.put(None)
    await asyncio.wait_for(loop, timeout=2.0)


async def test_pending_requests_are_cancelled_on_disconnect() -> None:
    registry = RpcRegistry()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    @registry.method("chat.history")
    async def _slow(params: dict | None, ctx: Any) -> dict:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {}

    ws = _FakeWs()
    loop = asyncio.create_task(_message_loop(_conn(ws), GatewayConfig(), registry, None))
    await ws.inbox.put(_req("1", "chat.history", {"sessionKey": "a"}))
    await asyncio.wait_for(started.wait(), timeout=2.0)
    await ws.inbox.put(None)
    await asyncio.wait_for(loop, timeout=2.0)
    await asyncio.wait_for(cancelled.wait(), timeout=2.0)


# ── byte-based slow-consumer policy ────────────────────────────────────────


class _SlowReader:
    """A socket whose send blocks until released: a client not reading."""

    def __init__(self) -> None:
        self.client_state = WebSocketState.CONNECTED
        self.sent: list[str] = []
        self.unblock = asyncio.Event()
        self.close_code: int | None = None
        self.close_reason: str | None = None

    async def send_text(self, text: str) -> None:
        await self.unblock.wait()
        self.sent.append(text)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_code = code
        self.close_reason = reason
        self.client_state = WebSocketState.DISCONNECTED


def _slow_conn(max_bytes: int) -> tuple[WsConnection, _SlowReader]:
    ws = _SlowReader()
    conn = WsConnection(conn_id="slow", ws=ws)  # type: ignore[arg-type]
    conn._start_writer(maxsize=512, enabled=True, max_buffered_bytes=max_bytes)  # noqa: SLF001
    return conn, ws


async def test_non_droppable_overflow_closes_1008_slow_consumer() -> None:
    conn, ws = _slow_conn(max_bytes=2_000)
    try:
        await conn.send_event("session.event.text_delta", {"chunk": "a" * 100})
        await asyncio.sleep(0)  # the writer takes the first frame and blocks
        with structlog.testing.capture_logs() as logs:
            for _ in range(30):
                await conn.send_event("session.event.text_delta", {"chunk": "b" * 100})
            await asyncio.sleep(0.05)
        assert ws.close_code == 1008
        assert ws.close_reason == "slow consumer"
        assert any(entry["event"] == "gateway.ws_slow_consumer_close" for entry in logs)
    finally:
        ws.unblock.set()
        await conn._stop_writer()  # noqa: SLF001


async def test_droppable_frames_over_budget_are_dropped_and_consume_a_seq() -> None:
    conn, ws = _slow_conn(max_bytes=600)
    try:
        await conn.send_event("session.event.text_delta", {"chunk": "first"})
        await asyncio.sleep(0)  # in flight, blocked
        await conn.send_event("session.event.text_delta", {"chunk": "x" * 500})
        assert conn.buffered_bytes > 500
        await conn.send_event("tick", {"time_ms": 1})  # over budget: dropped
        await conn.send_event("presence", {"n": 1})  # over budget: dropped
        assert ws.close_code is None
        ws.unblock.set()
        for _ in range(100):
            if len(ws.sent) >= 2:
                break
            await asyncio.sleep(0.01)
        await conn.send_event("session.event.text_delta", {"chunk": "after"})
        for _ in range(100):
            if len(ws.sent) >= 3:
                break
            await asyncio.sleep(0.01)
        frames = [json.loads(text) for text in ws.sent]
        assert [frame["event"] for frame in frames] == ["session.event.text_delta"] * 3
        # Two dropped refreshes consumed seqs 3 and 4: the client sees the gap.
        assert [frame["seq"] for frame in frames] == [1, 2, 5]
        assert conn.buffered_bytes == 0
    finally:
        ws.unblock.set()
        await conn._stop_writer()  # noqa: SLF001


async def test_a_single_large_frame_goes_out_when_nothing_is_queued() -> None:
    conn, ws = _slow_conn(max_bytes=100)
    ws.unblock.set()
    try:
        await conn.send_event("session.event.tool_result", {"blob": "z" * 5_000})
        for _ in range(100):
            if ws.sent:
                break
            await asyncio.sleep(0.01)
        assert ws.close_code is None
        assert json.loads(ws.sent[0])["payload"]["blob"] == "z" * 5_000
    finally:
        await conn._stop_writer()  # noqa: SLF001


async def test_event_wire_is_byte_identical_to_the_model_serialization() -> None:
    from agentos.gateway.protocol import make_event

    conn, ws = _slow_conn(max_bytes=10_000)
    ws.unblock.set()
    try:
        payload = {"chunk": "héllo", "n": [1, 2.5, None, True], "nested": {"k": "v"}}
        await conn.send_event("session.event.text_delta", payload, meta={"replayed": True})
        for _ in range(100):
            if ws.sent:
                break
            await asyncio.sleep(0.01)
        expected = make_event(
            "session.event.text_delta", payload, seq=1, meta={"replayed": True}
        ).model_dump_json()
        assert ws.sent[0] == expected
    finally:
        await conn._stop_writer()  # noqa: SLF001


async def test_flush_waits_until_queued_frames_are_on_the_wire() -> None:
    conn, ws = _slow_conn(max_bytes=10_000)
    try:
        await conn.send_event("shutdown", {"reason": "signal:SIGTERM"})
        flushing = asyncio.create_task(conn.flush(timeout=2.0))
        await asyncio.sleep(0.05)
        assert not flushing.done()
        ws.unblock.set()
        await asyncio.wait_for(flushing, timeout=2.0)
        assert json.loads(ws.sent[0])["event"] == "shutdown"
    finally:
        ws.unblock.set()
        await conn._stop_writer()  # noqa: SLF001


# ── a second stop from the same connection ────────────────────────────────


async def test_a_second_stop_joins_the_first_instead_of_queueing_behind_it() -> None:
    registry = RpcRegistry()
    release_send = asyncio.Event()
    release_first_stop = asyncio.Event()
    order: list[str] = []

    @registry.method("chat.send")
    async def _send(params: dict | None, ctx: Any) -> dict:
        await release_send.wait()
        order.append("send")
        return {}

    @registry.method("chat.abort")
    async def _abort(params: dict | None, ctx: Any) -> dict:
        nth = (params or {}).get("nth")
        order.append(f"abort{nth}")
        if nth == 1:
            await release_first_stop.wait()
        return {}

    @registry.method("chat.history")
    async def _history(params: dict | None, ctx: Any) -> dict:
        order.append("history")
        return {}

    ws = _FakeWs()
    loop = asyncio.create_task(_message_loop(_conn(ws), GatewayConfig(), registry, None))
    key = {"sessionKey": "agent:main:webchat:s"}
    await ws.inbox.put(_req("1", "chat.send", key))
    await ws.inbox.put(_req("2", "chat.abort", {**key, "nth": 1}))
    await ws.inbox.put(_req("3", "chat.abort", {**key, "nth": 2}))
    await ws.inbox.put(_req("4", "chat.history", key))
    await asyncio.sleep(0.05)
    # Both stops still wait for the send before them.
    assert order == []
    release_send.set()
    # The second stop does not wait for the first one to finish.
    await asyncio.wait_for(_answered(ws, "3"), timeout=2.0)
    assert order == ["send", "abort1", "abort2"]
    # A later request on the session waits for every stop of the group.
    await asyncio.sleep(0.05)
    assert "history" not in order
    release_first_stop.set()
    await asyncio.wait_for(_answered(ws, "4"), timeout=2.0)
    assert order[-1] == "history"
    await ws.inbox.put(None)
    await asyncio.wait_for(loop, timeout=2.0)


async def test_a_stop_after_another_request_still_waits_for_it() -> None:
    registry = RpcRegistry()
    release = asyncio.Event()
    order: list[str] = []

    @registry.method("chat.abort")
    async def _abort(params: dict | None, ctx: Any) -> dict:
        nth = (params or {}).get("nth")
        order.append(f"abort{nth}")
        if nth == 1:
            await release.wait()
        return {}

    @registry.method("chat.send")
    async def _send(params: dict | None, ctx: Any) -> dict:
        order.append("send")
        return {}

    ws = _FakeWs()
    loop = asyncio.create_task(_message_loop(_conn(ws), GatewayConfig(), registry, None))
    key = {"sessionKey": "agent:main:webchat:s"}
    # stop, send, stop: the send breaks the group, so the last stop waits for
    # the send, which waits for the first stop.
    await ws.inbox.put(_req("1", "chat.abort", {**key, "nth": 1}))
    await ws.inbox.put(_req("2", "chat.send", key))
    await ws.inbox.put(_req("3", "chat.abort", {**key, "nth": 3}))
    await asyncio.sleep(0.05)
    assert order == ["abort1"]
    release.set()
    await asyncio.wait_for(_answered(ws, "3"), timeout=2.0)
    assert order == ["abort1", "send", "abort3"]
    await ws.inbox.put(None)
    await asyncio.wait_for(loop, timeout=2.0)


class _AbortSessions:
    """Session manager surface ``chat.abort`` reads: one session, no children."""

    def __init__(self) -> None:
        self.storage = self
        self._storage = self

    async def get_session(self, key: str) -> Any:
        return object()

    async def list_sessions(self, **_: Any) -> list[Any]:
        return []


async def test_second_chat_abort_on_one_connection_force_stops_the_turn() -> None:
    """The real ``chat.abort`` handler over one socket against a real runtime:
    the first stop asks the turn to stop and waits out the grace; the second
    one, sent while the first is pending, cancels the turn at once and both
    answer with the stopped run."""
    from agentos.engine.turn_progress import current_turn_stop_signal
    from agentos.gateway.routing import RouteEnvelope, SourceKind
    from agentos.gateway.rpc_chat import _handle_chat_abort
    from agentos.gateway.task_runtime import TaskRuntime
    from tests.test_gateway.test_task_runtime_cooperative_cancel import _Storage

    session_key = "agent:main:webchat:stop-twice"
    started = asyncio.Event()

    async def handler(run: Any) -> None:
        signal = current_turn_stop_signal()
        assert signal is not None
        signal.accepts_abort = True
        started.set()
        await asyncio.sleep(60)  # ignores the cooperative abort

    runtime = TaskRuntime(storage=_Storage({}), turn_handler=handler, cancel_grace_s=30.0)
    handle = await runtime.enqueue(
        RouteEnvelope(
            source_kind=SourceKind.SYSTEM,
            source_name="test",
            agent_id="main",
            session_key=session_key,
        ),
        "hi",
        run_kind="default",
    )
    await asyncio.wait_for(started.wait(), timeout=2)

    registry = RpcRegistry()
    registry.register("chat.abort", _handle_chat_abort)
    ws = _FakeWs()
    loop = asyncio.create_task(
        _message_loop(_conn(ws), GatewayConfig(), registry, _AbortSessions(), task_runtime=runtime)
    )
    try:
        await ws.inbox.put(_req("1", "chat.abort", {"sessionKey": session_key}))
        await asyncio.sleep(0.1)
        assert runtime.is_task_active(handle.task_id)  # the grace is running
        await ws.inbox.put(_req("2", "chat.abort", {"sessionKey": session_key}))
        second = await asyncio.wait_for(_answered(ws, "2"), timeout=3.0)
        first = await asyncio.wait_for(_answered(ws, "1"), timeout=3.0)
    finally:
        await ws.inbox.put(None)
        await asyncio.wait_for(loop, timeout=2.0)
        await runtime.shutdown()

    assert second["ok"] is True, second
    assert second["payload"]["aborted"] == [handle.task_id]
    assert first["ok"] is True, first
    assert first["payload"]["aborted"] == [handle.task_id]
    assert not runtime.is_task_active(handle.task_id)
