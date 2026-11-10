"""WebSocket connection handler: handshake, frame parsing, event loop."""

from __future__ import annotations

import asyncio
import functools
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog
from pydantic import ValidationError
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from agentos import __version__
from agentos.gateway.access import ConnectionSurface, resolve_client_ip
from agentos.gateway.auth import AccessContext
from agentos.gateway.auth_rate_limit import TOKEN_SCOPE, AuthGuard
from agentos.gateway.config import GatewayConfig
from agentos.gateway.origin_policy import (
    OriginPolicy,
    build_origin_policy,
    origin_in_allowlist,  # noqa: F401 - re-exported for existing importers
    origin_key,
    request_is_forwarded,
)
from agentos.gateway.protocol import (
    CAPABILITIES,
    MAX_BUFFERED_BYTES,
    MAX_PREAUTH_PAYLOAD_BYTES,
    PREAUTH_TIMEOUT_MS,
    WS_CLOSE_MESSAGE_TOO_BIG,
    WS_CLOSE_POLICY_VIOLATION,
    WS_CLOSE_SERVICE_RESTART,
    ClientInfo,
    ConnectParams,
    ErrorCode,
    ErrorDetailCode,
    HelloOk,
    PolicyInfo,
    ReqFrame,
    ResFrame,
    ServerInfo,
    SnapshotInfo,
    make_error_res,
    make_event,
    negotiate_protocol,
    protocol_mismatch_details,
    validation_error_details,
)
from agentos.gateway.rpc import RpcContext, RpcDispatcher
from agentos.gateway.ws_transport import TRANSPORT_PEER_EXTENSION, raise_message_limit
from agentos.session.keys import canonicalize_session_key

log = structlog.get_logger(__name__)


# Shared with the HTTP origin guard and CORS (``origin_policy``); re-exported
# under the names older call sites import.
_origin_key = origin_key


def is_allowed_ws_origin(
    origin: str | None,
    config: GatewayConfig,
    *,
    bind_is_loopback: bool | None = None,
    host_header: str | None = None,
    peer_ip: str | None = None,
    forwarded: bool = False,
    policy: OriginPolicy | None = None,
) -> bool:
    """Return True if a WebSocket handshake ``Origin`` may be accepted.

    The WS upgrade skips ``AuthMiddleware`` and a loopback peer may use
    no-auth Control admission, so a malicious page in the victim's browser
    could otherwise open a socket to the gateway and drive the Control RPC
    surface (Cross-Site WebSocket Hijacking; the CVE-2026-53869 class).
    Browsers always send ``Origin`` on WS handshakes and page JS cannot forge
    it, so it is the correct gate. The decision is ``origin_policy``'s, the
    same one the HTTP guard and CORS apply, on every bind: a missing Origin
    (CLI, node peers) passes, then the allowlist, private same-origin, and a
    loopback origin on a local socket. A malformed Origin fails closed.

    ``bind_is_loopback`` is the posture captured when the app was built:
    ``config.apply`` mutates ``config.host`` in place without rebinding the
    live socket, so reading it here would let a runtime host change alter the
    guard while the process still listens where it did (P2).
    """
    if not origin:
        return True
    if policy is None:
        policy = build_origin_policy(config, bind_is_loopback=bind_is_loopback)
    decision = policy.evaluate(
        origin, host_header=host_header, peer_ip=peer_ip, forwarded=forwarded
    )
    return decision.allowed


# ---------------------------------------------------------------------------
# Outbound writer queue primitives
# ---------------------------------------------------------------------------
#
# When the per-connection writer queue is enabled, every outbound frame
# (events, RPC responses, ticks) is enqueued from any producer task and
# drained sequentially by a dedicated writer task. WS-frame ``seq`` is
# minted by the writer at DEQUEUE time so that a depth-based lossy drop
# never consumes a seq number — the frontend at ``frontend/src/lib/ws-rpc.ts``
# closes the socket on any seq gap.
#
# ``_LOSSY_EVENTS`` is intentionally narrow: the lossy event MUST NOT be
# routed through ``SessionStreamRegistry.record()`` upstream, otherwise a
# silent drop here would create a ``stream_seq`` gap that the frontend
# would filter in its chat stream controller on reconnect. The
# only event that satisfies that constraint today is the
# liveness ``tick`` emitted from ``_tick_loop`` — its name is not prefixed
# ``session.event.`` so ``EventBridge.emit`` skips ``record()`` for it.
# Any future addition to this set MUST be verified against the same
# upstream invariant.
_LOSSY_EVENTS: frozenset[str] = frozenset({"tick"})

# Byte-based slow-consumer policy. Each queued frame is serialized at enqueue
# and its size counted; once more than ``MAX_BUFFERED_BYTES`` (the value
# ``hello-ok.policy.max_buffered_bytes`` advertises) wait for the writer, a
# frame in ``_DROPPABLE_EVENTS`` is dropped and its ``seq`` still consumed, so
# the client sees the gap and resynchronizes (reconnect + replay). Anything
# else closes the socket 1008 ``slow consumer``: losing a state-bearing frame
# silently is never an option. Like ``_LOSSY_EVENTS``, these are periodic
# refreshes that no session stream records.
_DROPPABLE_EVENTS: frozenset[str] = _LOSSY_EVENTS | frozenset({"presence", "health"})

# Sentinel pushed into the outbox by ``_stop_writer`` to wake a writer
# blocked in ``await self._outbox.get()`` and exit cleanly.
_SENTINEL_STOP: Any = object()


@dataclass(slots=True)
class _Flush:
    """Queued by :meth:`WsConnection.flush`; resolved once the writer reaches it."""

    done: asyncio.Future[None]


@dataclass(slots=True)
class _SeqSkip:
    """Queued in place of frames dropped under the byte budget.

    The writer consumes ``count`` seqs when it reaches it, so the gap shows
    exactly where the frames were dropped. Consecutive drops share one.
    """

    count: int = 1


# ``EventFrame.model_dump_json()`` ends with these fields, in this order. An
# event is serialized at enqueue with ``seq=0`` and the writer swaps in the
# real seq, so a frame is serialized exactly once.
_EVENT_SEQ_PLACEHOLDER_SUFFIX = ',"seq":0,"state_version":null}'


def _event_wire_template(event: str, payload: Any, meta: dict[str, Any] | None) -> str:
    return make_event(event, payload, seq=0, meta=meta).model_dump_json()


def _event_wire_with_seq(template: str, seq: int) -> str:
    if template.endswith(_EVENT_SEQ_PLACEHOLDER_SUFFIX):
        head = template[: -len(_EVENT_SEQ_PLACEHOLDER_SUFFIX)]
        return f'{head},"seq":{seq},"state_version":null}}'
    frame = json.loads(template)
    frame["seq"] = seq
    return json.dumps(frame, separators=(",", ":"), ensure_ascii=False)


@dataclass(slots=True)
class _OutboundFrame:
    """A frame queued for the writer task.

    ``seq`` is deliberately absent — it is minted by ``_writer_loop`` at
    dequeue time. ``kind`` is used by same-kind eviction; for events it is
    ``f"event:{event_name}"``, for RPC responses it is ``"res"``. ``wire`` is
    the serialized frame (an event's carries a placeholder seq) and ``size``
    its length, counted against the byte budget while the frame is queued.
    """

    kind: str
    classification: str  # "lossy" or "control"
    payload: Any
    event_name: str | None
    res_frame: ResFrame | None
    meta: dict[str, Any] | None = None
    raw_text: str | None = None
    wire: str | None = None
    size: int = 0


def _payload_field(payload: Any, key: str) -> Any:
    """Best-effort extraction of a field from a payload dict; tolerates non-dicts."""
    if isinstance(payload, dict):
        return payload.get(key)
    return None


@dataclass
class WsConnection:
    """Represents a connected WebSocket client."""

    conn_id: str
    ws: WebSocket
    access: AccessContext = field(
        default_factory=lambda: AccessContext(
            surface=ConnectionSurface.CONTROL,
            admitted=False,
            credential_verified=False,
        )
    )
    connected_at: int = field(default_factory=lambda: int(time.time() * 1000))
    #: The nonce sent in this connection's ``connect.challenge``, kept for a
    #: signed-connect flow and echoed in ``hello-ok.auth``.
    challenge_nonce: str | None = None
    #: The ``client`` block the peer sent in ``connect`` (informational).
    client_info: ClientInfo | None = None
    _seq: int = field(default=0, init=False)
    # Seqs consumed by frames dropped under the byte budget; added to the next
    # minted seq so the client sees the gap.
    _seq_skips: int = field(default=0, init=False, repr=False)
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    # Writer-queue state.
    # ``_queue_enabled`` mirrors the kill-switch config at registration time;
    # once a connection starts in legacy mode it stays in legacy mode for life.
    _queue_enabled: bool = field(default=False, init=False, repr=False)
    _writer_queue_maxsize: int = field(default=512, init=False, repr=False)
    _max_buffered_bytes: int = field(default=MAX_BUFFERED_BYTES, init=False, repr=False)
    _buffered_bytes: int = field(default=0, init=False, repr=False)
    _outbox: asyncio.Queue[Any] | None = field(default=None, init=False, repr=False)
    _writer_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _closing: bool = field(default=False, init=False, repr=False)
    _background_tasks: set[asyncio.Task] = field(default_factory=set, init=False, repr=False)

    @property
    def authenticated(self) -> bool:
        return self.access.admitted

    @property
    def buffered_bytes(self) -> int:
        """Approximate bytes queued for the writer and not yet sent."""
        return self._buffered_bytes

    def next_seq(self) -> int:
        self._seq += 1 + self._seq_skips
        self._seq_skips = 0
        return self._seq

    # ------------------------------------------------------------------
    # Public send entry points
    # ------------------------------------------------------------------

    async def send_event(
        self,
        event: str,
        payload: Any = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        # Atomic check + enqueue. The check and ``put_nowait`` are part of
        # one synchronous flow with no ``await`` between them, so
        # ``_force_close`` cannot flip ``_closing`` mid-flight (asyncio is
        # single-threaded; only awaits yield).
        if self._queue_enabled and self._outbox is not None and not self._closing:
            try:
                wire = _event_wire_template(event, payload, meta)
            except Exception:  # noqa: BLE001 - one bad payload must not kill the writer
                log.warning("gateway.ws_event_serialize_failed", conn_id=self.conn_id, event=event)
                return
            classification = "lossy" if event in _LOSSY_EVENTS else "control"
            frame = _OutboundFrame(
                kind=f"event:{event}",
                classification=classification,
                payload=payload,
                event_name=event,
                res_frame=None,
                meta=meta,
                wire=wire,
                size=len(wire),
            )
            self._enqueue_frame(frame)
            return
        if self._queue_enabled:
            # The writer is being torn down (overflow or disconnect): the frame
            # has nowhere to go, and sending it directly would block this
            # producer on the very socket that stopped reading.
            return
        # Legacy direct-send path (pre-auth, kill-switch off).
        async with self._send_lock:
            if self.ws.client_state == WebSocketState.CONNECTED:
                wire_frame = make_event(event, payload, seq=self.next_seq(), meta=meta)
                await self.ws.send_text(wire_frame.model_dump_json())

    async def send_res(self, frame: ResFrame) -> None:
        # RPC responses are always CONTROL: they carry state-bearing payloads
        # and a slow-client overflow must close the connection rather than
        # silently dropping the response.
        if self._queue_enabled and self._outbox is not None and not self._closing:
            raw = frame.model_dump_json()
            outbound = _OutboundFrame(
                kind="res",
                classification="control",
                payload=None,
                event_name=None,
                res_frame=frame,
                raw_text=raw,
                size=len(raw),
            )
            self._enqueue_frame(outbound)
            return
        if self._queue_enabled:
            return  # writer torn down; see send_event
        async with self._send_lock:
            if self.ws.client_state == WebSocketState.CONNECTED:
                await self.ws.send_text(frame.model_dump_json())

    async def send_pong(self) -> None:
        raw = '{"type":"pong"}'
        if self._queue_enabled and self._outbox is not None and not self._closing:
            outbound = _OutboundFrame(
                kind="pong",
                classification="control",
                payload=None,
                event_name=None,
                res_frame=None,
                raw_text=raw,
                size=len(raw),
            )
            self._enqueue_frame(outbound)
            return
        if self._queue_enabled:
            return  # writer torn down; see send_event
        async with self._send_lock:
            if self.ws.client_state == WebSocketState.CONNECTED:
                await self.ws.send_text(raw)

    async def close(self, code: int = WS_CLOSE_SERVICE_RESTART, reason: str = "") -> None:
        try:
            await self.ws.close(code=code)
        except Exception:
            pass

    async def flush(self, timeout: float = 1.0) -> None:
        """Wait (at most ``timeout``) until every frame queued so far is written.

        A no-op without a running writer. Used before a shutdown closes the
        socket, so the ``shutdown`` event already queued reaches the client.
        """
        if (
            not self._queue_enabled
            or self._outbox is None
            or self._writer_task is None
            or self._closing
        ):
            return
        done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        try:
            self._outbox.put_nowait(_Flush(done))
        except asyncio.QueueFull:
            return
        try:
            await asyncio.wait_for(asyncio.shield(done), timeout=timeout)
        except TimeoutError:
            log.debug("gateway.ws_flush_timeout", conn_id=self.conn_id)

    # ------------------------------------------------------------------
    # Writer task lifecycle
    # ------------------------------------------------------------------

    def _start_writer(
        self, *, maxsize: int, enabled: bool, max_buffered_bytes: int = MAX_BUFFERED_BYTES
    ) -> None:
        """Idempotently boot the per-connection writer task.

        Called from ``handle_ws_connection`` immediately after
        ``registry.register(conn)``. Pre-auth sends do NOT go through the
        queue because the writer task does not exist yet — see Step 4 of
        the plan and the comment block at the registration call site.
        """
        if self._writer_task is not None:
            return
        self._queue_enabled = bool(enabled)
        self._writer_queue_maxsize = int(maxsize)
        self._max_buffered_bytes = max(1, int(max_buffered_bytes))
        if not self._queue_enabled:
            return
        self._outbox = asyncio.Queue(maxsize=self._writer_queue_maxsize)
        self._writer_task = asyncio.create_task(
            self._writer_loop(), name=f"ws-writer-{self.conn_id}"
        )
        log.debug("gateway.ws_writer_started", conn_id=self.conn_id)

    async def _stop_writer(self) -> None:
        """Idempotent writer shutdown for the disconnect path.

        Unlike ``_force_close`` this does NOT call ``ws.close()`` — clean
        disconnects are already signaled by ``WebSocketDisconnect`` and the
        socket is already torn down by the time we hit the ``finally`` of
        ``handle_ws_connection``. Calling ws.close() here would race with
        starlette's own teardown.
        """
        self._closing = True
        task = self._writer_task
        if task is None:
            return
        self._writer_task = None
        # Best-effort wakeup for a writer blocked in ``outbox.get()``.
        if self._outbox is not None:
            try:
                self._outbox.put_nowait(_SENTINEL_STOP)
            except asyncio.QueueFull:
                pass
        if not task.done():
            task.cancel()
            # NOTE: ``gather(..., return_exceptions=True)`` deliberately
            # absorbs the writer's CancelledError as a result *value* so
            # it does not propagate into this teardown path. Do NOT
            # replace this with ``await task`` — that re-raises
            # CancelledError into ``_stop_writer`` and corrupts the
            # cleanup sequence.
            try:
                await asyncio.wait_for(
                    asyncio.gather(task, return_exceptions=True),
                    timeout=2.0,
                )
            except TimeoutError:
                log.warning(
                    "gateway.ws_stop_writer_timeout",
                    conn_id=self.conn_id,
                )
        log.debug("gateway.ws_writer_stopped", conn_id=self.conn_id)

    async def _force_close(self, *, reason: str, code: int = 1011) -> None:
        """Forcefully tear down the connection due to writer backpressure.

        Idempotent. The ``_writer_task is None`` marker doubles as the
        "already-completed force_close" sentinel: the first invocation
        claims the task atomically, cancels it with a bounded timeout,
        then closes the socket. Concurrent invocations no-op.
        """
        self._closing = True
        task = self._writer_task
        if task is None:
            # Either there was never a writer (legacy mode) or another
            # force_close already ran. Either way: nothing to do.
            return
        # Atomically claim ownership so concurrent calls see _writer_task=None.
        self._writer_task = None
        if not task.done():
            task.cancel()
            # NOTE: ``gather(..., return_exceptions=True)`` deliberately
            # absorbs the writer's CancelledError as a result *value* so
            # it does not propagate into this teardown path. Do NOT
            # replace this with ``await task`` — that re-raises
            # CancelledError into ``_force_close`` and corrupts the close
            # sequence.
            try:
                await asyncio.wait_for(
                    asyncio.gather(task, return_exceptions=True),
                    timeout=2.0,
                )
            except TimeoutError:
                log.warning(
                    "gateway.ws_writer_force_close_timeout",
                    conn_id=self.conn_id,
                    reason=reason,
                )
        try:
            await self.ws.close(code=code, reason=reason)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Writer loop and enqueue helper
    # ------------------------------------------------------------------

    async def _writer_loop(self) -> None:
        """Drain ``_outbox`` and serialize frames onto the wire.

        WS-frame ``seq`` is minted here, at dequeue. This guarantees a
        contiguous monotonic ``seq`` even when producers' lossy frames are
        evicted by ``_enqueue_frame`` — evictions never consume a seq. Only
        a byte-budget drop does, on purpose (see ``_DROPPABLE_EVENTS``).
        """
        assert self._outbox is not None
        try:
            while True:
                item = await self._outbox.get()
                if item is _SENTINEL_STOP or self._closing:
                    return
                if isinstance(item, _SeqSkip):
                    self._seq += item.count
                    continue
                if isinstance(item, _Flush):
                    if not item.done.done():
                        item.done.set_result(None)
                    continue
                if not isinstance(item, _OutboundFrame):
                    continue
                self._buffered_bytes = max(0, self._buffered_bytes - item.size)
                if self.ws.client_state != WebSocketState.CONNECTED:
                    return
                try:
                    if item.raw_text is not None:
                        await self.ws.send_text(item.raw_text)
                    elif item.event_name is not None:
                        if item.wire is not None:
                            text = _event_wire_with_seq(item.wire, self.next_seq())
                        else:
                            text = make_event(
                                item.event_name,
                                item.payload,
                                seq=self.next_seq(),
                                meta=item.meta,
                            ).model_dump_json()
                        await self.ws.send_text(text)
                    elif item.res_frame is not None:
                        await self.ws.send_text(item.res_frame.model_dump_json())
                except WebSocketDisconnect:
                    return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.debug(
                        "gateway.ws_writer_send_failed",
                        conn_id=self.conn_id,
                        exc_info=True,
                    )
                    return
        except asyncio.CancelledError:
            raise

    def _over_byte_budget(self, frame: _OutboundFrame) -> bool:
        # A single frame larger than the budget still goes out when nothing
        # else is waiting; the budget bounds a backlog, not one response.
        return (
            self._buffered_bytes > 0
            and self._buffered_bytes + frame.size > self._max_buffered_bytes
        )

    def _enqueue_frame(self, frame: _OutboundFrame) -> None:
        """Synchronous enqueue with classification-aware overflow.

        Caller has already verified ``_queue_enabled`` and ``not _closing``
        and that ``_outbox is not None``. This method MUST NOT ``await`` —
        a yield point here would let ``_force_close`` flip ``_closing``
        between the guard check in ``send_event`` and the enqueue mutation.
        """
        if self._outbox is None:
            return
        if self._over_byte_budget(frame):
            if frame.event_name is not None and frame.event_name in _DROPPABLE_EVENTS:
                # Consume the seq this frame would have taken: the client
                # sees a gap and resynchronizes instead of silently missing
                # a refresh.
                self._record_seq_skip()
                log.warning(
                    "gateway.ws_writer_drop",
                    conn_id=self.conn_id,
                    event_name=frame.event_name,
                    session_key=_payload_field(frame.payload, "session_key"),
                    stream_seq=_payload_field(frame.payload, "stream_seq"),
                    queue_depth=self._outbox.qsize(),
                    buffered_bytes=self._buffered_bytes,
                    eviction=False,
                    seq_consumed=True,
                )
                return
            self._schedule_slow_consumer_close(frame)
            return
        try:
            self._outbox.put_nowait(frame)
            self._buffered_bytes += frame.size
            return
        except asyncio.QueueFull:
            pass

        if frame.classification == "lossy":
            evicted = self._evict_oldest_same_kind(frame.kind)
            if evicted:
                try:
                    self._outbox.put_nowait(frame)
                    self._buffered_bytes += frame.size
                    log.warning(
                        "gateway.ws_writer_drop",
                        conn_id=self.conn_id,
                        event_name=frame.event_name,
                        session_key=_payload_field(frame.payload, "session_key"),
                        stream_seq=_payload_field(frame.payload, "stream_seq"),
                        queue_depth=self._outbox.qsize(),
                        eviction=True,
                    )
                    return
                except asyncio.QueueFull:
                    pass
            # No same-kind candidate or impossibly rare race: drop the new
            # incoming frame to keep the close path moving.
            log.warning(
                "gateway.ws_writer_drop",
                conn_id=self.conn_id,
                event_name=frame.event_name,
                session_key=_payload_field(frame.payload, "session_key"),
                stream_seq=_payload_field(frame.payload, "stream_seq"),
                queue_depth=self._outbox.qsize(),
                eviction=False,
            )
            return

        # CONTROL overflow: cannot drop, cannot block. Schedule force-close.
        # Same-kind eviction policy note: under R-B the lossy set is {tick},
        # which has no session_key, so eviction is keyed on event_name only.
        # If the lossy set is later expanded to session-bearing events, the
        # eviction key MUST become (event_name, session_key) to prevent one
        # session's overflow from evicting another session's queued frame.
        # Keep this invariant if more lossy event kinds are added later.
        self._closing = True
        log.error(
            "gateway.ws_writer_overflow_close",
            conn_id=self.conn_id,
            event_name=frame.event_name,
            session_key=_payload_field(frame.payload, "session_key"),
            stream_seq=_payload_field(frame.payload, "stream_seq"),
            queue_depth=self._outbox.qsize(),
        )
        task = asyncio.create_task(
            self._force_close(reason="writer_backpressure", code=1011),
            name=f"ws-force-close-{self.conn_id}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _record_seq_skip(self) -> None:
        """Mark one dropped frame's seq at its place in the stream."""
        assert self._outbox is not None
        backing = self._outbox._queue  # type: ignore[attr-defined]
        if backing and isinstance(backing[-1], _SeqSkip):
            backing[-1].count += 1
            return
        try:
            self._outbox.put_nowait(_SeqSkip())
        except asyncio.QueueFull:
            # No room for a marker: charge it to the next frame minted.
            self._seq_skips += 1

    def _schedule_slow_consumer_close(self, frame: _OutboundFrame) -> None:
        """Non-droppable frame over the byte budget: close 1008 ``slow consumer``."""
        self._closing = True
        log.error(
            "gateway.ws_slow_consumer_close",
            conn_id=self.conn_id,
            event_name=frame.event_name,
            kind=frame.kind,
            frame_bytes=frame.size,
            buffered_bytes=self._buffered_bytes,
            max_buffered_bytes=self._max_buffered_bytes,
        )
        task = asyncio.create_task(
            self._force_close(reason="slow consumer", code=WS_CLOSE_POLICY_VIOLATION),
            name=f"ws-slow-consumer-close-{self.conn_id}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _evict_oldest_same_kind(self, kind: str) -> bool:
        """Evict the oldest queued frame whose ``kind`` matches.

        Manipulates ``asyncio.Queue._queue`` directly. Safe under asyncio
        because this method is fully synchronous (no await points), and the
        deque is the documented backing store. ``qsize()`` reflects
        ``len(_queue)`` so deletion alone is sufficient bookkeeping for
        our use (we do not use ``join()``/``task_done()``).
        """
        if self._outbox is None:
            return False
        backing = self._outbox._queue  # type: ignore[attr-defined]
        for index, queued in enumerate(backing):
            if isinstance(queued, _OutboundFrame) and queued.kind == kind:
                del backing[index]
                self._buffered_bytes = max(0, self._buffered_bytes - queued.size)
                return True
        return False


class ConnectionRegistry:
    """Tracks all active WebSocket connections."""

    def __init__(self) -> None:
        self._connections: dict[str, WsConnection] = {}

    def register(self, conn: WsConnection) -> None:
        self._connections[conn.conn_id] = conn

    def unregister(self, conn_id: str) -> None:
        self._connections.pop(conn_id, None)

    def get(self, conn_id: str) -> WsConnection | None:
        return self._connections.get(conn_id)

    def all(self) -> list[WsConnection]:
        return list(self._connections.values())

    async def broadcast(self, event: str, payload: Any = None) -> None:
        for conn in self.all():
            if conn.authenticated:
                try:
                    await conn.send_event(event, payload)
                except Exception:
                    pass

    async def broadcast_control(self, event: str, payload: Any = None) -> None:
        """Send to admitted Control connections, except an agent's own shell.

        Channel and node connections never see these frames, nor a connection
        that presented an agent token (the CLI an agent turn spawned). The
        web console on the gateway's own host presents no operator secret, so
        it counts as *unbound* under the agent surface's fail-safe rules; it
        still gets them, as it can read the same list from
        ``GET /api/approvals``.
        """
        for conn in self.all():
            access = conn.access
            if not access.admitted or access.surface != ConnectionSurface.CONTROL:
                continue
            if access.agent is not None and access.agent.via == "token":
                continue
            try:
                await conn.send_event(event, payload)
            except Exception:
                log.debug("ws.broadcast_control_failed", conn_id=conn.conn_id, event=event)


class SubscriptionManager:
    """Track which connections are subscribed to session-level and message-level events."""

    def __init__(self) -> None:
        self._session_subs: set[str] = set()  # conn_ids subscribed to session lifecycle
        self._message_subs: dict[str, set[str]] = {}  # session_key -> {conn_id}
        self._topic_subs: dict[str, set[str]] = {}  # topic -> {conn_id}

    # -- session-level (sessions.subscribe / sessions.unsubscribe) --

    def subscribe_sessions(self, conn_id: str) -> None:
        self._session_subs.add(conn_id)

    def unsubscribe_sessions(self, conn_id: str) -> None:
        self._session_subs.discard(conn_id)

    def get_session_subscribers(self) -> set[str]:
        return set(self._session_subs)

    # -- message-level (sessions.messages.subscribe / unsubscribe) --

    def subscribe_messages(self, conn_id: str, session_key: str) -> None:
        self._message_subs.setdefault(session_key, set()).add(conn_id)

    def unsubscribe_messages(self, conn_id: str, session_key: str) -> None:
        if session_key in self._message_subs:
            self._message_subs[session_key].discard(conn_id)
            if not self._message_subs[session_key]:
                del self._message_subs[session_key]

    def get_message_subscribers(self, session_key: str) -> set[str]:
        return set(self._message_subs.get(session_key, set()))

    # -- topic-level (cron.subscribe / cron.unsubscribe) --

    def subscribe_topic(self, conn_id: str, topic: str) -> None:
        self._topic_subs.setdefault(topic, set()).add(conn_id)

    def unsubscribe_topic(self, conn_id: str, topic: str) -> None:
        if topic in self._topic_subs:
            self._topic_subs[topic].discard(conn_id)
            if not self._topic_subs[topic]:
                del self._topic_subs[topic]

    def get_topic_subscribers(self, topic: str) -> set[str]:
        return set(self._topic_subs.get(topic, set()))

    def remove_connection(self, conn_id: str) -> None:
        """Clean up all subscriptions for a disconnected connection."""
        self._session_subs.discard(conn_id)
        empty_keys: list[str] = []
        for key, subs in self._message_subs.items():
            subs.discard(conn_id)
            if not subs:
                empty_keys.append(key)
        for key in empty_keys:
            del self._message_subs[key]
        empty_topics = []
        for topic, subs in self._topic_subs.items():
            subs.discard(conn_id)
            if not subs:
                empty_topics.append(topic)
        for topic in empty_topics:
            del self._topic_subs[topic]


# Module-level registry shared across connections
_registry = ConnectionRegistry()


def get_registry() -> ConnectionRegistry:
    return _registry


def _utf8_size_exceeds(text: str, limit: int) -> bool:
    """Whether ``text`` encodes to more than ``limit`` UTF-8 bytes (cheap first)."""
    if len(text) > limit:
        return True
    if len(text) * 4 <= limit:
        return False
    return len(text.encode("utf-8", "surrogatepass")) > limit


def _malformed(req_id: str, message: str, details: dict[str, Any]) -> ResFrame:
    return make_error_res(req_id, ErrorCode.INVALID_REQUEST, message, details=details)


async def handle_ws_connection(
    ws: WebSocket,
    config: GatewayConfig,
    dispatcher: RpcDispatcher,
    session_manager: Any = None,
    provider_selector: Any = None,
    tool_registry: Any = None,
    subscription_manager: Any = None,
    channel_manager: Any = None,
    usage_tracker: Any = None,
    skill_loader: Any = None,
    cron_scheduler: Any = None,
    turn_runner: Any = None,
    task_runtime: Any = None,
    heartbeat_service: Any = None,
    heartbeat_loop: Any = None,
    agent_registry: Any = None,
    diagnostics_state: Any = None,
    memory_managers: dict[str, Any] | None = None,
    memory_stores: dict[str, Any] | None = None,
    memory_retrievers: dict[str, Any] | None = None,
    bind_is_loopback: bool | None = None,
    auth_guard: AuthGuard | None = None,
    origin_policy: OriginPolicy | None = None,
) -> None:
    """Main WebSocket connection handler."""
    conn_id = str(uuid.uuid4())
    conn = WsConnection(conn_id=conn_id, ws=ws)
    registry = get_registry()

    # CSWSH / DNS-rebinding guard (every bind): reject a browser handshake the
    # origin policy refuses, before accept(). A cross-site page cannot forge
    # Origin, so this stops it from opening a socket to the gateway and
    # inheriting Control admission. Starlette Headers.get is case-insensitive.
    # bind_is_loopback / origin_policy are captured at app-build time (P2).
    origin = ws.headers.get("origin")
    peer_ip = ws.client.host if ws.client is not None else None
    if not is_allowed_ws_origin(
        origin,
        config,
        bind_is_loopback=bind_is_loopback,
        host_header=ws.headers.get("host"),
        peer_ip=peer_ip,
        forwarded=request_is_forwarded(ws.headers),
        policy=origin_policy,
    ):
        log.warning("ws.origin_rejected", conn_id=conn_id, origin=origin)
        await ws.close(code=WS_CLOSE_POLICY_VIOLATION)
        return

    client_ip = resolve_client_ip(
        peer_ip, ws.headers.get("x-forwarded-for"), config.auth.trusted_proxy
    )
    # Pre-auth budget: a peer may hold only so many sockets that have not
    # finished the handshake. Refused before accept(): no timer, no buffer.
    preauth = auth_guard.preauth if auth_guard is not None else None
    if preauth is not None and not preauth.try_acquire(client_ip):
        log.warning(
            "ws.preauth_budget_exceeded",
            conn_id=conn_id,
            client_ip=client_ip,
            limit=preauth.limit,
        )
        # Refused before accept(): the upgrade never completes, so the client
        # sees an HTTP 403 response rather than a WebSocket close code.
        await ws.close(code=1013)
        return
    try:
        hello = await _handshake(
            conn,
            config,
            dispatcher,
            peer_ip=peer_ip,
            transport_peer=_transport_peer_ip(ws),
            client_ip=client_ip,
            origin=origin,
            auth_guard=auth_guard,
        )
    finally:
        if preauth is not None:
            preauth.release(client_ip)
    if hello is None:
        return
    # Frames before hello-ok were held to MAX_PREAUTH_PAYLOAD_BYTES at the
    # transport (ws_transport.GatewayWebSocketProtocol); from here on the
    # advertised max_payload applies.
    raise_message_limit(getattr(ws, "scope", None), hello.policy.max_payload)

    registry.register(conn)
    # Boundary: pre-auth direct-send ends here. After registry.register(conn),
    # conn._writer_task owns all post-auth sends. send_event/send_res route
    # through conn._outbox; WS-frame seq is minted at dequeue inside the
    # writer loop (NOT at enqueue), so dropped lossy frames never consume a
    # seq number.
    # Kill switch (config.ws_writer_queue_enabled) is read here at registration
    # time only — affects new connections only; existing connections retain
    # their startup-time behavior.
    conn._start_writer(
        maxsize=config.ws_writer_queue_maxsize,
        enabled=config.ws_writer_queue_enabled,
        max_buffered_bytes=hello.policy.max_buffered_bytes,
    )
    client = conn.client_info
    log.info(
        "ws.authenticated",
        conn_id=conn_id,
        surface=conn.access.surface.value,
        client_id=client.id if client else None,
        client_version=client.version if client else None,
        client_mode=client.mode if client else None,
    )

    # Step 7: Main message loop
    tick_task = asyncio.create_task(_tick_loop(conn, hello.policy.tick_interval_ms))
    try:
        await _message_loop(
            conn,
            config,
            dispatcher,
            session_manager,
            provider_selector,
            tool_registry,
            subscription_manager,
            channel_manager,
            usage_tracker,
            skill_loader,
            cron_scheduler,
            turn_runner,
            task_runtime,
            heartbeat_service,
            heartbeat_loop,
            agent_registry,
            diagnostics_state,
            memory_managers,
            memory_stores,
            memory_retrievers,
        )
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.error("ws.error", conn_id=conn_id, error=str(exc))
    finally:
        # Download tickets are bound to this socket and die with it. Revoked
        # before the first await below so a cancelled handler cannot skip it.
        _revoke_download_grants(conn_id)
        # Stop the writer FIRST, before tick_task.cancel() and before
        # registry.unregister. Otherwise an EventBridge.emit on another
        # coroutine could still hold a reference to this connection while
        # the writer task is mid-cancel, producing a "zombie writer"
        # scenario.
        await conn._stop_writer()
        tick_task.cancel()
        try:
            await tick_task
        except asyncio.CancelledError:
            pass
        registry.unregister(conn_id)
        if subscription_manager is not None:
            subscription_manager.remove_connection(conn_id)
        log.info("ws.disconnected", conn_id=conn_id)


async def _refuse_while_starting(conn: WsConnection, req_id: str) -> bool:
    """Supervisor contract: answer ``connect`` during boot with a retry hint.

    While the boot sidecars (channel start) are still running, a connect is
    answered with a retryable ``UNAVAILABLE`` (``details.code`` =
    ``STARTUP_SIDECARS``, ``retry_after_ms``) and closed ``1013`` instead of
    being served by a half-started gateway (gateway/readiness.py).
    """
    from agentos.gateway.readiness import (
        WS_CLOSE_REASON_STARTING,
        WS_CLOSE_TRY_AGAIN_LATER,
        readiness_from_scope,
    )

    readiness = readiness_from_scope(getattr(conn.ws, "scope", None))
    if readiness is None or readiness.accepting_connections():
        return False
    retry_after_ms = readiness.retry_after_ms()
    try:
        await conn.send_res(
            make_error_res(
                req_id,
                ErrorCode.UNAVAILABLE,
                "Gateway is starting; retry shortly",
                retryable=True,
                details={
                    "code": ErrorDetailCode.STARTUP_SIDECARS.value,
                    "retry_after_ms": retry_after_ms,
                    "pending": readiness.pending_sidecars(),
                },
                retry_after_ms=retry_after_ms,
            )
        )
        await conn.ws.close(code=WS_CLOSE_TRY_AGAIN_LATER, reason=WS_CLOSE_REASON_STARTING)
    except Exception:  # noqa: BLE001 - the client may already be gone
        pass
    return True


def _revoke_download_grants(conn_id: str) -> None:
    from agentos.gateway.download_grants import get_download_grants

    get_download_grants().revoke_connection(conn_id)


def _transport_peer_ip(ws: WebSocket) -> str | None:
    """The address the socket itself saw, never one an ``X-Forwarded-For`` claimed.

    :class:`~agentos.gateway.ws_transport.GatewayWebSocketProtocol` records it
    before uvicorn's proxy-header middleware can rewrite ``ws.client`` (which
    it does for any loopback peer). Without that record, ``ws.client`` is only
    trusted when no forwarding header could have rewritten it; otherwise the
    answer is ``None``, which never earns the operator.
    """
    scope = getattr(ws, "scope", None)
    extensions = scope.get("extensions") if isinstance(scope, dict) else None
    if isinstance(extensions, dict) and TRANSPORT_PEER_EXTENSION in extensions:
        peer = extensions[TRANSPORT_PEER_EXTENSION]
        return str(peer[0]) if isinstance(peer, (tuple, list)) and peer else None
    if request_is_forwarded(ws.headers):
        return None
    return ws.client.host if ws.client is not None else None


async def _handshake(
    conn: WsConnection,
    config: GatewayConfig,
    dispatcher: RpcDispatcher,
    *,
    peer_ip: str | None,
    client_ip: str,
    origin: str | None,
    auth_guard: AuthGuard | None,
    transport_peer: str | None = None,
) -> HelloOk | None:
    """``connect.challenge`` → ``connect`` → ``hello-ok``; ``None`` when refused.

    Every frame before ``hello-ok`` is capped at ``MAX_PREAUTH_PAYLOAD_BYTES``
    and validated against ``ReqFrame`` / ``ConnectParams``; a refusal sends
    one typed error response and closes the socket.
    """
    ws = conn.ws
    await ws.accept()
    log.info("ws.connected", conn_id=conn.conn_id, remote=str(ws.client))

    # Step 1: Send connect.challenge. The nonce stays on the connection (a
    # signed connect answers it) and comes back in hello-ok.auth so a client
    # can check it answered this challenge; ts is the server clock (ms).
    nonce = str(uuid.uuid4())
    conn.challenge_nonce = nonce
    try:
        await conn.send_event("connect.challenge", {"nonce": nonce, "ts": int(time.time() * 1000)})
    except WebSocketDisconnect:
        return None

    # Step 2: Pre-auth timeout — client must send connect request
    try:
        preauth_timeout = PREAUTH_TIMEOUT_MS / 1000
        raw = await asyncio.wait_for(ws.receive_text(), timeout=preauth_timeout)
    except TimeoutError:
        log.warning("ws.preauth_timeout", conn_id=conn.conn_id)
        await conn.close()
        return None
    except WebSocketDisconnect:
        return None
    except KeyError:
        # A binary frame: the protocol is JSON text only.
        await conn.send_res(
            _malformed(
                "handshake",
                "The connect frame must be a JSON text frame",
                {"code": ErrorDetailCode.INVALID_FRAME.value},
            )
        )
        await conn.close(code=1003)
        return None

    # Step 3: Size gate, then parse and validate the connect request. The
    # transport already refuses larger frames before reading them when the
    # gateway runs under ws_transport.GatewayWebSocketProtocol; this check
    # covers any other ASGI server.
    if _utf8_size_exceeds(raw, MAX_PREAUTH_PAYLOAD_BYTES):
        log.warning("ws.preauth_frame_too_large", conn_id=conn.conn_id, chars=len(raw))
        await conn.send_res(
            _malformed(
                "handshake",
                "Frame exceeds the pre-authentication size limit",
                {
                    "code": ErrorDetailCode.PAYLOAD_TOO_LARGE.value,
                    "limit_bytes": MAX_PREAUTH_PAYLOAD_BYTES,
                },
            )
        )
        await conn.close(code=WS_CLOSE_MESSAGE_TOO_BIG)
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        await conn.send_res(
            _malformed(
                "handshake",
                "Invalid JSON in connect frame",
                {"code": ErrorDetailCode.INVALID_FRAME.value},
            )
        )
        await conn.close(code=1002)
        return None

    raw_id = data.get("id") if isinstance(data, dict) else None
    fallback_id = raw_id if isinstance(raw_id, str) else "handshake"
    if not isinstance(data, dict) or data.get("type") != "req" or data.get("method") != "connect":
        await conn.send_res(
            _malformed(
                fallback_id,
                "First message must be connect request",
                {"code": ErrorDetailCode.INVALID_FRAME.value},
            )
        )
        await conn.close(code=1002)
        return None

    try:
        frame = ReqFrame.model_validate(data)
    except ValidationError as exc:
        await conn.send_res(
            _malformed(
                fallback_id,
                "Malformed connect request",
                validation_error_details(ErrorDetailCode.INVALID_FRAME, exc),
            )
        )
        await conn.close(code=1002)
        return None
    req_id = frame.id

    # Supervisor contract: a gateway still booting tells the client to retry.
    if await _refuse_while_starting(conn, req_id):
        return None

    try:
        params = ConnectParams.model_validate(frame.params if frame.params is not None else {})
    except ValidationError as exc:
        await conn.send_res(
            _malformed(
                req_id,
                "Malformed connect params",
                validation_error_details(ErrorDetailCode.INVALID_CONNECT_PARAMS, exc),
            )
        )
        await conn.close(code=1002)
        return None
    conn.client_info = params.client

    # Step 4: Resolve binary admission and the fixed protocol surface. A
    # presented gateway token counts against the failed-auth budget; a
    # missing one never does.
    from agentos.gateway.auth import resolve_auth

    auth_params = params.auth or {}
    limiter = auth_guard.limiter if auth_guard is not None else None
    presented = config.auth.mode == "token" and bool(auth_params.get("token"))
    if limiter is not None and presented:
        decision = limiter.check(client_ip, TOKEN_SCOPE, origin=origin)
        if not decision.allowed:
            log.warning(
                "auth.rate_limited",
                conn_id=conn.conn_id,
                client_ip=client_ip,
                origin=origin,
                retry_after_ms=decision.retry_after_ms,
            )
            await conn.send_res(
                make_error_res(
                    req_id,
                    ErrorCode.UNAUTHORIZED,
                    "Too many failed authentication attempts; retry later",
                    retryable=True,
                    details={
                        "code": ErrorDetailCode.AUTH_RATE_LIMITED.value,
                        "retry_after_ms": decision.retry_after_ms,
                    },
                    retry_after_ms=decision.retry_after_ms,
                )
            )
            await conn.close(code=WS_CLOSE_POLICY_VIOLATION)
            return None

    access = resolve_auth(
        config,
        auth_params=auth_params,
        client_kind=params.client_kind,
        peer_ip=peer_ip,
    )
    if access is None:
        if limiter is not None and presented:
            decision = limiter.record_failure(client_ip, TOKEN_SCOPE, origin=origin)
            if decision.delay_s > 0:
                await asyncio.sleep(decision.delay_s)
        await conn.send_res(make_error_res(req_id, ErrorCode.UNAUTHORIZED, "Authentication failed"))
        await conn.close(code=WS_CLOSE_POLICY_VIOLATION)
        return None
    if limiter is not None and presented:
        limiter.record_success(client_ip, TOKEN_SCOPE, origin=origin)
    # An agent's connection is marked as such for its whole life: the trading
    # handlers read this instead of trusting an ``initiator`` param.
    from agentos.gateway.agent_surface import get_agent_surface

    access = get_agent_surface().attach(access, auth_params, peer_ip=transport_peer)

    # Step 5: Negotiate protocol version
    negotiated = negotiate_protocol(params.min_protocol, params.max_protocol)
    if negotiated is None:
        await conn.send_res(
            make_error_res(
                req_id,
                ErrorCode.INVALID_REQUEST,
                "Unsupported protocol version range",
                details=protocol_mismatch_details(params.min_protocol, params.max_protocol),
            )
        )
        await conn.close(code=1002)
        return None

    conn.access = access

    # Step 6: Send HelloOk
    hello = HelloOk(
        protocol=negotiated,
        server=ServerInfo(version=__version__, conn_id=conn.conn_id),
        features=_build_features(dispatcher),
        snapshot=SnapshotInfo(
            uptime_ms=int(time.time() * 1000),
            config_path=config.config_path,
            state_dir=config.state_dir,
            auth_mode=config.auth.mode,
        ),
        policy=PolicyInfo(
            agent_stream_heartbeat_interval_ms=int(
                max(0.0, float(getattr(config, "agent_stream_heartbeat_interval_seconds", 15.0)))
                * 1000
            ),
            agent_stream_idle_timeout_ms=int(
                max(0.0, float(getattr(config, "agent_stream_idle_timeout_seconds", 600.0))) * 1000
            ),
            webui_stream_idle_grace_ms=int(
                max(0.0, float(getattr(config, "webui_stream_idle_grace_seconds", 630.0))) * 1000
            ),
            client_ws_keepalive_timeout_ms=int(
                max(0.0, float(getattr(config, "client_ws_keepalive_timeout_s", 120.0))) * 1000
            ),
        ),
        auth={"nonce": nonce},
    )
    await ws.send_text(hello.model_dump_json())
    return hello


# ---------------------------------------------------------------------------
# Request start queue
# ---------------------------------------------------------------------------
#
# Requests on one connection are admitted through a bounded start queue
# instead of strictly one at a time. At most ``MAX_CONCURRENT_REQUESTS`` run
# at once; one that cannot start within ``REQUEST_START_TIMEOUT_S`` is
# answered ``UNAVAILABLE {details.code: REQUEST_START_TIMEOUT}`` (retryable).
#
# Ordering is kept where clients rely on it (``chat.send`` before
# ``sessions.abort``, ``subscribe`` before the history read, ``reset`` before
# the next send, ``plan.mode.set`` before the send it applies to):
#
# * A request naming a session (``sessionKey`` / ``session_key``, or ``key``
#   on ``sessions.*``) runs in that session's lane: after the previous
#   request on the same session, concurrently with other sessions.
# * ``CONCURRENT_READ_METHODS`` (session-less reads such as ``sessions.list``)
#   wait only for the barriers before them, so a poll is not held back by
#   another session's slow request; later barriers still wait for them.
# * Any other request is a barrier: it starts after every earlier request
#   and every later request waits for it, exactly the old in-order rule.
# * ``UNORDERED_METHODS`` wait for nothing. They neither write state nor
#   read anything a later request on the connection depends on, and they
#   wait seconds on a model or the network (``chat.suggestNext`` at every
#   turn end held back ``chat.history`` by ~5 s).
#
# * A stop (``STOP_METHODS``) right behind another stop of the same session
#   joins it instead of queueing behind it: it waits for what the first stop
#   waited for, and later requests on the session wait for both. The first
#   stop holds its lane until the run settles (up to the cancel grace), and a
#   second stop exists precisely to cut that grace short.
#
# So one slow ``chat.history`` no longer holds back the same tab's other
# sessions, or a second tab sharing the socket.

#: Requests one connection may run at once.
MAX_CONCURRENT_REQUESTS = 4
#: Longest a request may wait for its turn before it is refused (retryable).
REQUEST_START_TIMEOUT_S = 30.0
#: Requests one connection may have admitted but not finished; more are
#: refused at once.
MAX_PENDING_REQUESTS = 256

UNORDERED_METHODS: frozenset[str] = frozenset(
    {
        "chat.suggestNext",
        "acp.doctor",
        "providers.probe",
        "onboarding.channel.probe",
        "trading.probe",
    }
)

#: Stops. A stop queued right behind another stop of the same session runs
#: alongside it (see above).
STOP_METHODS: frozenset[str] = frozenset({"chat.abort", "sessions.abort"})

#: Session-less methods that only read. They run alongside session lanes.
CONCURRENT_READ_METHODS: frozenset[str] = frozenset(
    {
        "acp.agents",
        "acp.sessions",
        "agents.list",
        "channels.status",
        "commands.list_for_surface",
        "config.get",
        "cron.list",
        "doctor.status",
        "health",
        "models.list",
        "projects.list",
        "providers.status",
        "sessions.list",
        "sessions.preview",
        "sessions.questions",
        "skills.list",
        "status",
        "usage.cost",
        "usage.status",
    }
)

_SESSION_PARAM_KEYS: tuple[str, ...] = ("sessionKey", "session_key")
_READ_LANE = "read"


def request_lane(method: str, params: Any) -> str | None:
    """The ordering lane of one request: ``"session:<key>"``, ``"read"``,
    ``"barrier"`` or ``None`` (unordered)."""
    if method in UNORDERED_METHODS:
        return None
    if isinstance(params, dict):
        keys = _SESSION_PARAM_KEYS + (("key",) if method.startswith("sessions.") else ())
        for name in keys:
            value = params.get(name)
            if isinstance(value, str) and value.strip():
                return "session:" + canonicalize_session_key(value)
    if method in CONCURRENT_READ_METHODS:
        return _READ_LANE
    return "barrier"


class RequestStartQueue:
    """Admits one connection's requests in arrival order, a few at a time."""

    def __init__(
        self,
        *,
        max_concurrent: int = MAX_CONCURRENT_REQUESTS,
        start_timeout_s: float = REQUEST_START_TIMEOUT_S,
        max_pending: int = MAX_PENDING_REQUESTS,
    ) -> None:
        self._slots = asyncio.Semaphore(max(1, max_concurrent))
        self._start_timeout_s = start_timeout_s
        self._max_pending = max_pending
        self._tasks: set[asyncio.Task[None]] = set()
        self._last_barrier: asyncio.Task[None] | None = None
        self._since_barrier: set[asyncio.Task[None]] = set()
        #: The requests a later request on the lane must wait for: the last one,
        #: or every stop of a group of consecutive stops.
        self._lane_tails: dict[str, set[asyncio.Task[None]]] = {}
        #: Lanes whose tail is a group of stops: what the group's first stop
        #: waited for, and the barrier it was admitted after.
        self._stop_groups: dict[str, tuple[set[asyncio.Task[None]], asyncio.Task[None] | None]] = {}

    @property
    def pending(self) -> int:
        return len(self._tasks)

    def full(self) -> bool:
        return len(self._tasks) >= self._max_pending

    def submit(
        self,
        lane: str | None,
        run: Callable[[], Awaitable[None]],
        on_start_timeout: Callable[[], Awaitable[None]],
        *,
        name: str = "ws-request",
        stop: bool = False,
    ) -> asyncio.Task[None]:
        """Admit one request. ``stop`` marks a session stop (``STOP_METHODS``)."""
        session_lane = lane is not None and lane not in ("barrier", _READ_LANE)
        group = self._stop_groups.get(lane) if stop and session_lane and lane else None
        joins_group = group is not None and group[1] is self._last_barrier
        deps: set[asyncio.Task[None]] = set()
        if joins_group and group is not None:
            # Behind a stop that is still waiting for its run to settle: start
            # when that stop could, not after it.
            deps |= group[0]
        else:
            if lane is not None and self._last_barrier is not None:
                deps.add(self._last_barrier)
            if lane == "barrier":
                deps |= self._since_barrier
            elif session_lane and lane is not None:
                deps |= self._lane_tails.get(lane, set())
        deps = {task for task in deps if not task.done()}

        task = asyncio.create_task(self._run(deps, run, on_start_timeout), name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        if lane == "barrier":
            self._last_barrier = task
            self._since_barrier = set()
        elif lane == _READ_LANE:
            self._since_barrier.add(task)
        elif lane is not None:
            if joins_group:
                self._lane_tails.setdefault(lane, set()).add(task)
            else:
                self._lane_tails[lane] = {task}
                if stop:
                    self._stop_groups[lane] = (set(deps), self._last_barrier)
                else:
                    self._stop_groups.pop(lane, None)
            self._since_barrier.add(task)
            task.add_done_callback(functools.partial(self._forget_tail, lane))
        if lane is not None:
            task.add_done_callback(self._since_barrier.discard)
        return task

    def _forget_tail(self, lane: str, task: asyncio.Task[None]) -> None:
        tails = self._lane_tails.get(lane)
        if tails is None or task not in tails:
            return
        tails.discard(task)
        if not tails:
            del self._lane_tails[lane]
            self._stop_groups.pop(lane, None)

    async def _run(
        self,
        deps: set[asyncio.Task[None]],
        run: Callable[[], Awaitable[None]],
        on_start_timeout: Callable[[], Awaitable[None]],
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._start_timeout_s
        if deps:
            await asyncio.wait(deps, timeout=max(0.0, deadline - loop.time()))
            if any(not task.done() for task in deps):
                await on_start_timeout()
                # Refused, but still a link in its lane: the requests queued
                # behind this one must keep waiting for what it waited for,
                # or they would overtake a request that is still running.
                await asyncio.wait(deps)
                return
        try:
            await asyncio.wait_for(self._slots.acquire(), timeout=max(0.0, deadline - loop.time()))
        except TimeoutError:
            await on_start_timeout()
            return
        try:
            await run()
        finally:
            self._slots.release()

    def cancel_all(self) -> None:
        for task in list(self._tasks):
            task.cancel()


async def _dispatch_and_send(
    conn: WsConnection,
    dispatcher: RpcDispatcher,
    req_id: str,
    method: str,
    params: Any,
    ctx: RpcContext,
) -> None:
    res = await dispatcher.dispatch(req_id, method, params, ctx)
    try:
        await conn.send_res(res)
    except Exception:  # noqa: BLE001 - the client may be gone by now
        log.debug("ws.request_send_failed", conn_id=conn.conn_id, method=method, exc_info=True)


async def _send_unavailable(
    conn: WsConnection, req_id: str, method: str, detail: ErrorDetailCode, message: str
) -> None:
    log.warning("ws.request_not_started", conn_id=conn.conn_id, method=method, reason=detail.value)
    try:
        await conn.send_res(
            make_error_res(
                req_id,
                ErrorCode.UNAVAILABLE,
                message,
                retryable=True,
                details={"code": detail.value, "method": method},
            )
        )
    except Exception:  # noqa: BLE001 - the client may be gone by now
        log.debug("ws.request_send_failed", conn_id=conn.conn_id, method=method, exc_info=True)


async def _tick_loop(conn: WsConnection, tick_interval_ms: int) -> None:
    interval_s = max(1.0, tick_interval_ms / 1000)
    while True:
        await asyncio.sleep(interval_s)
        try:
            await conn.send_event("tick", {"time_ms": int(time.time() * 1000)})
        except Exception:
            log.debug("ws.tick_failed", conn_id=conn.conn_id, exc_info=True)
            return


async def _message_loop(
    conn: WsConnection,
    config: GatewayConfig,
    dispatcher: RpcDispatcher,
    session_manager: Any,
    provider_selector: Any = None,
    tool_registry: Any = None,
    subscription_manager: Any = None,
    channel_manager: Any = None,
    usage_tracker: Any = None,
    skill_loader: Any = None,
    cron_scheduler: Any = None,
    turn_runner: Any = None,
    task_runtime: Any = None,
    heartbeat_service: Any = None,
    heartbeat_loop: Any = None,
    agent_registry: Any = None,
    diagnostics_state: Any = None,
    memory_managers: dict[str, Any] | None = None,
    memory_stores: dict[str, Any] | None = None,
    memory_retrievers: dict[str, Any] | None = None,
    start_queue: RequestStartQueue | None = None,
) -> None:
    queue = start_queue if start_queue is not None else RequestStartQueue()
    try:
        await _receive_loop(
            conn,
            config,
            dispatcher,
            queue,
            session_manager,
            provider_selector,
            tool_registry,
            subscription_manager,
            channel_manager,
            usage_tracker,
            skill_loader,
            cron_scheduler,
            turn_runner,
            task_runtime,
            heartbeat_service,
            heartbeat_loop,
            agent_registry,
            diagnostics_state,
            memory_managers,
            memory_stores,
            memory_retrievers,
        )
    finally:
        queue.cancel_all()


async def _receive_loop(
    conn: WsConnection,
    config: GatewayConfig,
    dispatcher: RpcDispatcher,
    queue: RequestStartQueue,
    session_manager: Any,
    provider_selector: Any,
    tool_registry: Any,
    subscription_manager: Any,
    channel_manager: Any,
    usage_tracker: Any,
    skill_loader: Any,
    cron_scheduler: Any,
    turn_runner: Any,
    task_runtime: Any,
    heartbeat_service: Any,
    heartbeat_loop: Any,
    agent_registry: Any,
    diagnostics_state: Any,
    memory_managers: dict[str, Any] | None,
    memory_stores: dict[str, Any] | None,
    memory_retrievers: dict[str, Any] | None,
) -> None:
    ws = conn.ws
    keepalive_timeout = max(0.0, float(getattr(config, "client_ws_keepalive_timeout_s", 0.0)))
    while True:
        try:
            if keepalive_timeout > 0.0:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=keepalive_timeout)
            else:
                raw = await ws.receive_text()
        except WebSocketDisconnect:
            return
        except TimeoutError:
            log.warning(
                "gateway.client_ws_keepalive_timeout",
                conn_id=conn.conn_id,
                timeout_s=keepalive_timeout,
            )
            try:
                await ws.close(code=1011)
            except Exception:  # noqa: BLE001
                pass
            return

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            await conn.send_res(
                _malformed("", "Invalid JSON", {"code": ErrorDetailCode.INVALID_FRAME.value})
            )
            continue
        if not isinstance(data, dict):
            await conn.send_res(
                _malformed(
                    "", "Frame must be a JSON object", {"code": ErrorDetailCode.INVALID_FRAME.value}
                )
            )
            continue

        frame_type = data.get("type")

        if frame_type == "ping":
            await conn.send_pong()
            continue

        if frame_type == "pong":
            continue

        if frame_type == "req":
            try:
                frame = ReqFrame.model_validate(data)
            except ValidationError as exc:
                raw_id = data.get("id")
                await conn.send_res(
                    _malformed(
                        raw_id if isinstance(raw_id, str) else "",
                        "Malformed request envelope",
                        validation_error_details(ErrorDetailCode.INVALID_FRAME, exc),
                    )
                )
                continue
            req_id, method, params = frame.id, frame.method, frame.params

            if queue.full():
                await _send_unavailable(
                    conn,
                    req_id,
                    method,
                    ErrorDetailCode.QUEUE_FULL,
                    "Too many requests in flight on this connection; retry shortly",
                )
                continue

            ctx = RpcContext(
                conn_id=conn.conn_id,
                access=conn.access,
                session_manager=session_manager,
                config=config,
                provider_selector=provider_selector,
                model_catalog=getattr(turn_runner, "_model_catalog", None),
                tool_registry=tool_registry,
                subscription_manager=subscription_manager,
                channel_manager=channel_manager,
                usage_tracker=usage_tracker,
                skill_loader=skill_loader,
                cron_scheduler=cron_scheduler,
                turn_runner=turn_runner,
                task_runtime=task_runtime,
                heartbeat_service=heartbeat_service,
                heartbeat_loop=heartbeat_loop,
                agent_registry=agent_registry,
                diagnostics_state=diagnostics_state,
                memory_managers=memory_managers or {},
                memory_stores=memory_stores or {},
                memory_retrievers=memory_retrievers or {},
            )
            queue.submit(
                request_lane(method, params),
                functools.partial(
                    _dispatch_and_send, conn, dispatcher, req_id, method, params, ctx
                ),
                functools.partial(
                    _send_unavailable,
                    conn,
                    req_id,
                    method,
                    ErrorDetailCode.REQUEST_START_TIMEOUT,
                    "The request could not start in time; retry",
                ),
                name=f"ws-req-{conn.conn_id}-{method}",
                stop=method in STOP_METHODS,
            )
        else:
            await conn.send_res(
                _malformed(
                    "",
                    f"Unknown frame type: {frame_type}",
                    {"code": ErrorDetailCode.INVALID_FRAME.value},
                )
            )


def _build_features(dispatcher: RpcDispatcher) -> Any:
    from agentos.gateway.protocol import FeaturesInfo

    methods = dispatcher.list_methods()
    events = [
        "connect.challenge",
        "agent",
        "session.message",
        "sessions.changed",
        "presence",
        "tick",
        "shutdown",
        "health",
        "heartbeat",
        "cron",
        "approval.requested",
        "approval.resolved",
        "config.changed",
    ]
    return FeaturesInfo(methods=methods, events=events, capabilities=sorted(CAPABILITIES))
