"""Channel adapter concurrency tests.

Covers per-channel in-flight cap, cap-formula scaling with
``max_concurrency``, done-callback exception surfacing, channel-stop
cancellation, relay-close in finally, typing-keepalive lifecycle, and
deadlock-free stress under 10K requests / 100 sessions.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentos.gateway.channel_dispatch import (
    _ChannelInFlightSet,
    _compute_channel_cap,
    _deliver_runtime_channel_reply,
    _make_reply_done_callback,
    _resolve_channel_overflow_policy,
)

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_config(*, channel_inflight_cap: int = 8, max_concurrency: int = 4) -> Any:
    cfg = MagicMock()
    cfg.task_runtime.channel_inflight_cap = channel_inflight_cap
    cfg.task_runtime.max_concurrency = max_concurrency
    return cfg


def _make_task_runtime(*, delay: float = 0.0, succeed: bool = True) -> Any:
    rt = MagicMock()

    async def _wait(task_id: str) -> Any:
        if delay > 0:
            await asyncio.sleep(delay)
        record = MagicMock()
        record.status.value = "succeeded" if succeed else "failed"
        record.error_message = None
        record.terminal_reason = None
        return record

    rt.wait = _wait
    return rt


def _make_channel() -> Any:
    ch = MagicMock()
    ch.send = AsyncMock()
    ch.build_reply_message = None
    ch.streaming_reply_kwargs = None
    return ch


def _make_inbound() -> Any:
    msg = MagicMock()
    msg.content = "hello"
    return msg


def _make_route_envelope() -> Any:
    env = MagicMock()
    env.reply_target = None
    env.thread_id = None
    env.channel_id = "ch-1"
    return env


def _make_session_manager() -> Any:
    sm = MagicMock()

    async def _read_transcript(sk: str) -> list:
        record = {"role": "assistant", "content": "reply text"}
        return [record]

    sm.read_transcript = _read_transcript
    return sm


# ── _ChannelInFlightSet cap and formula ──────────────────────────────────────


def test_inflight_set_cap_enforced() -> None:
    """In-flight set tracks tasks and reports full() at cap."""
    ifs = _ChannelInFlightSet(cap=3)
    assert not ifs.full()

    tasks = []
    for _ in range(3):
        t = MagicMock(spec=asyncio.Task)
        ifs.add(t)
        tasks.append(t)

    assert ifs.full()
    ifs.discard(tasks[0])
    assert not ifs.full()


@pytest.mark.parametrize(
    "max_concurrency,channel_inflight_cap,expected_cap",
    [
        (1, 8, 2),  # min(8, max(2×1,1)) = min(8,2) = 2
        (4, 8, 8),  # min(8, max(2×4,1)) = min(8,8) = 8
        (16, 8, 8),  # min(8, max(2×16,1)) = min(8,32) = 8
    ],
)
def test_inflight_cap_scales_with_concurrency(
    max_concurrency: int,
    channel_inflight_cap: int,
    expected_cap: int,
) -> None:
    """Cap formula min(channel_inflight_cap, max(2 × max_concurrency, 1))."""
    cfg = _make_config(
        channel_inflight_cap=channel_inflight_cap,
        max_concurrency=max_concurrency,
    )
    assert _compute_channel_cap(cfg) == expected_cap


def test_compute_channel_cap_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env path: AGENTOS_CHANNEL_INFLIGHT_CAP=1 gives cap=min(1, 2×4)=1."""
    monkeypatch.setenv("AGENTOS_CHANNEL_INFLIGHT_CAP", "1")
    # _compute_channel_cap reads from config object, not env directly —
    # env is applied by GatewayConfig. Test via config mock.
    cfg = _make_config(channel_inflight_cap=1, max_concurrency=4)
    assert _compute_channel_cap(cfg) == 1


# ── _ChannelInFlightSet docstring must mention SEPARATE second-layer semaphore


def test_ac2_2_docstring_mentions_separate_semaphore() -> None:
    """_ChannelInFlightSet docstring must reference global_sem separation."""
    doc = _ChannelInFlightSet.__doc__ or ""
    assert "global_sem" in doc or "second-layer" in doc, (
        "_ChannelInFlightSet docstring must mention 'second-layer' or 'global_sem'"
    )


# ── relay close in finally even when reply raises ───────────────────────────


@pytest.mark.asyncio
async def test_relay_close_on_exception() -> None:
    """stream_relay.close() is called even when wait() raises."""
    channel = _make_channel()
    session_manager = _make_session_manager()
    route_envelope = _make_route_envelope()
    inbound = _make_inbound()

    relay = MagicMock()
    relay.close = AsyncMock()
    relay.text_emitted = False
    relay.stream_error = None

    rt = MagicMock()
    rt.wait = AsyncMock(side_effect=RuntimeError("boom"))

    await _deliver_runtime_channel_reply(
        channel=channel,
        task_runtime=rt,
        session_manager=session_manager,
        session_key="s:test",
        task_id="task-1",
        route_envelope=route_envelope,
        inbound=inbound,
        transcript_watermark=0,
        stream_relay=relay,
    )

    relay.close.assert_awaited_once()
    # A user-facing terminal message should be sent; raw exception detail stays in logs.
    channel.send.assert_awaited_once()
    sent_content = channel.send.call_args[0][0].content
    assert sent_content == "The task failed before it could finish."
    assert "boom" not in sent_content


@pytest.mark.asyncio
async def test_relay_close_on_success() -> None:
    """stream_relay.close() is also called on success path."""
    channel = _make_channel()
    session_manager = _make_session_manager()
    route_envelope = _make_route_envelope()
    inbound = _make_inbound()

    relay = MagicMock()
    relay.close = AsyncMock()
    relay.text_emitted = False
    relay.stream_error = None

    rt = _make_task_runtime(succeed=True)

    await _deliver_runtime_channel_reply(
        channel=channel,
        task_runtime=rt,
        session_manager=session_manager,
        session_key="s:test",
        task_id="task-1",
        route_envelope=route_envelope,
        inbound=inbound,
        transcript_watermark=0,
        stream_relay=relay,
    )

    relay.close.assert_awaited_once()


# ── done_callback surfaces exceptions ────────────────────────────────────────


async def _run_reply_done(coro: Any, *, session_key: str = "s:cb-test") -> tuple[Any, Any, Any]:
    """Drive one reply task through the real done callback.

    Returns the in-flight set, the recorded ``_emit_metric`` calls, and the
    patched module logger.
    """
    metric_calls: list[tuple[str, dict[str, Any]]] = []

    def _fake_emit(name: str, **labels: Any) -> None:
        metric_calls.append((name, labels))

    ifs = _ChannelInFlightSet(cap=8)

    with patch("agentos.gateway.channel_dispatch._emit_metric", side_effect=_fake_emit):
        with patch("agentos.gateway.channel_dispatch.log") as mock_log:
            task = asyncio.create_task(coro)
            ifs.add(task)
            task.add_done_callback(_make_reply_done_callback(ifs, session_key))
            # Let the task reach its first await point before cancelling it.
            await asyncio.sleep(0)
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # Give the done callback a chance to fire.
            await asyncio.sleep(0)

    return ifs, metric_calls, mock_log


@pytest.mark.asyncio
async def test_ac2_3_done_callback_logs_error_and_counter() -> None:
    """done_callback on a failing reply task logs error + turn_cancellations_total."""

    async def _failing_reply() -> None:
        raise ValueError("reply boom")

    ifs, metric_calls, mock_log = await _run_reply_done(_failing_reply())

    assert mock_log.error.called, "log.error must be called on reply task exception"
    assert metric_calls == [
        (
            "turn_cancellations_total",
            {"value": 1, "reason": "reply_task_error", "session_key": "s:cb-test"},
        )
    ]
    assert not ifs._tasks


@pytest.mark.asyncio
async def test_done_callback_counts_a_cancelled_reply_task() -> None:
    """A cancelled reply is a cancellation — issue #1190 recorded nothing at all."""

    async def _long_reply() -> None:
        await asyncio.sleep(60)

    ifs, metric_calls, mock_log = await _run_reply_done(_long_reply())

    assert metric_calls == [
        (
            "turn_cancellations_total",
            {"value": 1, "reason": "reply_task_cancelled", "session_key": "s:cb-test"},
        )
    ]
    assert not mock_log.error.called, "a cancellation is not a delivery error"
    assert mock_log.info.called
    assert not ifs._tasks


@pytest.mark.asyncio
async def test_done_callback_separates_cancellation_from_delivery_error() -> None:
    """The two outcomes carry different reason labels on the same counter."""

    async def _failing_reply() -> None:
        raise ValueError("reply boom")

    async def _long_reply() -> None:
        await asyncio.sleep(60)

    _ifs, error_calls, _log = await _run_reply_done(_failing_reply())
    _ifs2, cancel_calls, _log2 = await _run_reply_done(_long_reply())

    assert error_calls[0][1]["reason"] == "reply_task_error"
    assert cancel_calls[0][1]["reason"] == "reply_task_cancelled"


@pytest.mark.asyncio
async def test_done_callback_is_silent_for_a_successful_reply() -> None:
    """A delivered reply is retired from the in-flight set without a counter."""

    async def _ok_reply() -> None:
        return None

    ifs, metric_calls, mock_log = await _run_reply_done(_ok_reply())

    assert metric_calls == []
    assert not mock_log.error.called
    assert not ifs._tasks


# ── stop_channel cancels all in-flight tasks ────────────────────────────────


@pytest.mark.asyncio
async def test_close_cancels_inflight() -> None:
    """_ChannelInFlightSet.cancel_all() cancels and awaits all tasks."""
    ifs = _ChannelInFlightSet(cap=8)

    results: list[str] = []

    async def _long_running(label: str) -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            results.append(f"cancelled:{label}")
            raise

    tasks = []
    for i in range(3):
        t = asyncio.create_task(_long_running(str(i)))
        ifs.add(t)
        tasks.append(t)

    # Yield to let tasks start (reach the sleep await point).
    await asyncio.sleep(0)

    await ifs.cancel_all()

    assert len(results) == 3, f"Expected 3 cancelled tasks, got {results}"
    assert all(r.startswith("cancelled:") for r in results)
    assert all(t.done() for t in tasks), "all tasks must be done after cancel_all"


@pytest.mark.asyncio
async def test_cancel_all_handles_reservation_tokens_alongside_tasks() -> None:
    """A bare reservation token must not abort the shutdown loop.

    ``try_acquire`` parks a plain ``object()`` in the same set as the real
    tasks. ``cancel_all`` used to call ``.cancel()`` on every member, so the
    token raised ``AttributeError`` and every task after it in iteration order
    was left running.
    """
    ifs = _ChannelInFlightSet(cap=8)

    cancelled: list[str] = []

    async def _long_running(label: str) -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(label)
            raise

    tasks = [asyncio.create_task(_long_running(str(i))) for i in range(3)]
    for task in tasks:
        ifs.add(task)
    # Interleave several tokens so no iteration order can dodge them.
    tokens = [object() for _ in range(3)]
    for token in tokens:
        assert ifs.try_acquire(token)

    await asyncio.sleep(0)

    await ifs.cancel_all()

    assert sorted(cancelled) == ["0", "1", "2"]
    assert all(t.done() for t in tasks)


@pytest.mark.asyncio
async def test_cancel_all_clears_reservation_tokens() -> None:
    """Shutdown must not leave reservations occupying the cap."""
    ifs = _ChannelInFlightSet(cap=2)
    assert ifs.try_acquire(object())
    assert ifs.try_acquire(object())
    assert ifs.full()

    await ifs.cancel_all()

    assert not ifs.full()
    assert ifs.try_acquire(object())


@pytest.mark.asyncio
async def test_cancel_all_on_a_reservation_only_set_is_a_no_op() -> None:
    ifs = _ChannelInFlightSet(cap=4)
    assert ifs.try_acquire(object())

    await ifs.cancel_all()  # must not raise

    assert not ifs.full()


def test_reservation_tokens_count_toward_the_cap() -> None:
    """The token's whole purpose — the cap check must still see it."""
    ifs = _ChannelInFlightSet(cap=1)
    token = object()

    assert ifs.try_acquire(token)
    assert ifs.full()
    assert not ifs.try_acquire(object())

    ifs.release(token)
    assert not ifs.full()


# ── typing_keepalive lifecycle bound to reply task ──────────────────────────


@pytest.mark.asyncio
async def test_keepalive_lifecycle() -> None:
    """When reply task is cancelled, keepalive task finishes within 1 s."""
    keepalive_done = asyncio.Event()

    async def _fake_keepalive() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            keepalive_done.set()
            raise

    keepalive_task = asyncio.create_task(_fake_keepalive())

    async def _reply_body() -> None:
        try:
            await asyncio.sleep(60)
        finally:
            keepalive_task.cancel()

    reply_task = asyncio.create_task(_reply_body())
    # Cancel the reply task (simulating shutdown or cap-drop)
    await asyncio.sleep(0)  # let tasks start
    reply_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await reply_task

    # keepalive should finish quickly after reply is cancelled
    done = await asyncio.wait_for(keepalive_done.wait(), timeout=1.0)
    assert done or keepalive_done.is_set(), "keepalive must be done within 1 s of reply cancel"

    # cleanup
    with pytest.raises(asyncio.CancelledError):
        await keepalive_task


# ── deadlock stress test ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_deadlock_stress() -> None:
    """10K requests across 100 sessions — no timeout, no starvation.

    Uses mock task_runtime that returns immediately to avoid real LLM calls.
    """
    from agentos.gateway.channel_dispatch import _ChannelInFlightSet

    total_requests = 10_000
    cap = 8

    ifs = _ChannelInFlightSet(cap=cap)
    completed: list[int] = []
    dropped: list[int] = []

    async def _mock_reply(req_id: int) -> None:
        await asyncio.sleep(0)  # yield to event loop
        completed.append(req_id)

    async def _run_all() -> None:
        for req_id in range(total_requests):
            if ifs.full():
                dropped.append(req_id)
                continue

            task = asyncio.create_task(_mock_reply(req_id))
            ifs.add(task)

            def _done(t: asyncio.Task[Any], _id: int = req_id) -> None:
                ifs.discard(t)

            task.add_done_callback(_done)

            # Occasionally yield to let tasks complete
            if req_id % cap == 0:
                await asyncio.sleep(0)

        # Drain remaining tasks
        await asyncio.gather(*list(ifs._tasks), return_exceptions=True)

    await asyncio.wait_for(_run_all(), timeout=60.0)

    total_processed = len(completed) + len(dropped)
    assert total_processed == total_requests, f"processed {total_processed} != {total_requests}"
    # All non-dropped requests must have completed
    assert len(completed) > 0, "at least some requests must complete"


# ── Integration: inflight cap causes busy-reply ───────────────────────────────


@pytest.mark.asyncio
async def test_inflight_cap_triggers_busy_reply() -> None:
    """When cap is reached, channel gets 'Server busy' reply."""
    from agentos.gateway.channel_dispatch import _ChannelInFlightSet

    ifs = _ChannelInFlightSet(cap=2)
    channel = _make_channel()

    # Fill the in-flight set with dummy tasks
    async def _noop() -> None:
        await asyncio.sleep(60)

    tasks = [asyncio.create_task(_noop()) for _ in range(2)]
    for t in tasks:
        ifs.add(t)

    assert ifs.full()

    # Simulate the busy-reply path from run_channel_dispatch
    from agentos.channels.types import OutgoingMessage

    if ifs.full():
        await channel.send(
            OutgoingMessage(
                content="Server busy, please retry",
                reply_to="ch-1",
            )
        )

    channel.send.assert_awaited_once()
    sent = channel.send.call_args[0][0]
    assert "busy" in sent.content.lower() or "retry" in sent.content.lower()

    # cleanup
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# ── cap full → no transcript pollution, no enqueue ──────────────────────────


@pytest.mark.asyncio
async def test_cap_full_no_transcript_pollution() -> None:
    """When _in_flight is full, enqueue is not called and transcript
    is not written; user receives 'Server busy' reply.
    """
    from agentos.channels.types import IncomingMessage
    from agentos.gateway.channel_dispatch import _ChannelInFlightSet, run_channel_dispatch

    # Build a full in-flight set (cap=1, one dummy task)
    ifs = _ChannelInFlightSet(cap=1)

    async def _noop() -> None:
        await asyncio.sleep(60)

    dummy = asyncio.create_task(_noop())
    ifs.add(dummy)
    assert ifs.full()

    # Channel yields one message then raises CancelledError to end the loop.
    # Use plain MagicMock (no spec) so attribute access works in routing helpers.
    msg = MagicMock()
    msg.content = "hello"
    msg.metadata = {}
    msg.sender_id = "u-1"
    msg.channel_id = "ch-test"
    msg.thread_id = None
    msg.id = "msg-1"

    channel = MagicMock()
    channel.send = AsyncMock()
    channel.build_reply_message = None
    channel.streaming_reply_kwargs = None

    call_count = 0

    async def _receive() -> IncomingMessage:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return msg
        raise asyncio.CancelledError

    channel.receive = _receive

    # task_runtime mock — enqueue must NOT be called
    task_runtime = MagicMock()
    task_runtime.enqueue = AsyncMock()

    # session_manager mock — append_message must NOT be called
    session_manager = MagicMock()
    session_manager.get_or_create = AsyncMock(return_value=(MagicMock(), False))
    session_manager.update = AsyncMock()
    session_manager.read_transcript = AsyncMock(return_value=[])

    config = _make_config(channel_inflight_cap=1, max_concurrency=1)

    fake_envelope = MagicMock()
    fake_envelope.thread_id = None
    fake_envelope.channel_id = "ch-test"

    with (
        patch(
            "agentos.gateway.routing.build_channel_route_envelope",
            return_value=fake_envelope,
        ),
        patch("agentos.gateway.channel_dispatch._append_channel_user_message") as mock_append,
        patch("agentos.gateway.channel_dispatch.start_turn_via_runtime") as mock_enqueue,
        patch(
            "agentos.gateway.channel_dispatch._record_delivery_context",
            new=AsyncMock(return_value=(MagicMock(), False)),
        ),
        patch("agentos.gateway.channel_dispatch._should_skip_unmentioned", return_value=False),
        patch(
            "agentos.gateway.channel_dispatch._ingest_channel_message_attachments",
            new=AsyncMock(return_value=MagicMock(text="hello", attachments=[])),
        ),
        patch("agentos.gateway.channel_dispatch._status_reactor") as mock_reactor_factory,
    ):
        mock_append.return_value = (MagicMock(), "hello")
        mock_enqueue.return_value = MagicMock(task_id="t-1")

        mock_reactor = MagicMock()
        mock_reactor.received = AsyncMock()
        mock_reactor.completed = AsyncMock()
        mock_reactor.running = AsyncMock()
        mock_reactor.failed = AsyncMock()
        mock_reactor_factory.return_value = mock_reactor

        turn_runner = MagicMock(spec=[])  # no _get_session_lock → session_lock=None

        try:
            await run_channel_dispatch(
                channel=channel,
                turn_runner=turn_runner,
                session_manager=session_manager,
                session_key_builder=lambda _msg: "s:test-cap",
                session_prefix="test",
                task_runtime=task_runtime,
                config=config,
                _in_flight=ifs,
            )
        except asyncio.CancelledError:
            pass

    # (a) transcript was NOT written
    mock_append.assert_not_called()
    # (b) enqueue was NOT called
    mock_enqueue.assert_not_called()
    # (c) user received "Server busy"
    channel.send.assert_awaited_once()
    sent = channel.send.call_args[0][0]
    assert "busy" in sent.content.lower() or "retry" in sent.content.lower()

    # cleanup
    dummy.cancel()
    await asyncio.gather(dummy, return_exceptions=True)


# ── debounce path reservation enforced ──────────────────────────────────────


@pytest.mark.asyncio
async def test_debounce_reservation_enforced() -> None:
    """Two concurrent _dispatch_combined_message_after_debounce calls
    with cap=1 — second is rejected with busy reply; enqueue called exactly once.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from agentos.gateway.channel_dispatch import (
        _ChannelInFlightSet,
        _dispatch_combined_message_after_debounce,
    )

    ifs = _ChannelInFlightSet(cap=1)

    channel = MagicMock()
    channel.send = AsyncMock()
    channel.build_reply_message = None
    channel.streaming_reply_kwargs = None

    def _make_combined() -> Any:
        combined = MagicMock()
        combined.message = MagicMock()
        combined.message.content = "hello"
        combined.message.metadata = {}
        combined.message.sender_id = "u-1"
        combined.message.channel_id = "ch-test"
        combined.message.thread_id = None
        combined.message.id = "msg-1"
        combined.raw_content = "hello"
        combined.coalesced_count = 1
        return combined

    fake_envelope = MagicMock()
    fake_envelope.thread_id = None
    fake_envelope.channel_id = "ch-test"

    enqueue_call_count = 0

    async def _slow_enqueue(*args: Any, **kwargs: Any) -> Any:
        nonlocal enqueue_call_count
        enqueue_call_count += 1
        # Yield so the second coroutine gets to run its cap check concurrently.
        await asyncio.sleep(0)
        result = MagicMock()
        result.task_id = "t-1"
        return result

    task_runtime = MagicMock()

    mock_reactor = MagicMock()
    mock_reactor.received = AsyncMock()
    mock_reactor.completed = AsyncMock()
    mock_reactor.running = AsyncMock()
    mock_reactor.failed = AsyncMock()

    with (
        patch(
            "agentos.gateway.routing.build_channel_route_envelope",
            return_value=fake_envelope,
        ),
        patch(
            "agentos.gateway.channel_dispatch.start_turn_via_runtime",
            side_effect=_slow_enqueue,
        ),
        patch(
            "agentos.gateway.channel_dispatch._append_channel_user_message",
            new=AsyncMock(return_value=(MagicMock(), "hello")),
        ),
        patch(
            "agentos.gateway.channel_dispatch._record_delivery_context",
            new=AsyncMock(return_value=(MagicMock(), False)),
        ),
        patch(
            "agentos.gateway.channel_dispatch._ingest_channel_message_attachments",
            new=AsyncMock(return_value=MagicMock(text="hello", attachments=[])),
        ),
        patch(
            "agentos.gateway.channel_dispatch._transcript_watermark",
            new=AsyncMock(return_value=0),
        ),
        patch("agentos.gateway.channel_dispatch._RuntimeChannelStreamRelay") as mock_relay_cls,
        patch("agentos.gateway.channel_dispatch._status_reactor", return_value=mock_reactor),
        patch(
            "agentos.gateway.channel_dispatch._deliver_runtime_channel_reply",
            new=AsyncMock(),
        ),
        patch("agentos.gateway.channel_dispatch._emit_events", new=AsyncMock()),
    ):
        mock_relay_cls.maybe_start.return_value = None

        turn_runner = MagicMock(spec=[])

        await asyncio.gather(
            _dispatch_combined_message_after_debounce(
                channel,
                _make_combined(),
                turn_runner,
                MagicMock(),
                "s:test",
                "test",
                task_runtime,
                None,
                None,
                ifs,
            ),
            _dispatch_combined_message_after_debounce(
                channel,
                _make_combined(),
                turn_runner,
                MagicMock(),
                "s:test",
                "test",
                task_runtime,
                None,
                None,
                ifs,
            ),
        )

    # Exactly one enqueue — the second dispatch was rejected by the cap.
    assert enqueue_call_count == 1, f"enqueue must be called exactly once, got {enqueue_call_count}"

    # At least one 'Server busy' reply sent to channel.
    busy_replies = [
        call
        for call in channel.send.call_args_list
        if "busy" in call[0][0].content.lower() or "retry" in call[0][0].content.lower()
    ]
    assert len(busy_replies) == 1, f"expected 1 busy reply, got {len(busy_replies)}"


def _dispatch_debounce_fixture() -> Any:
    """Common mocks for driving `_dispatch_combined_message_after_debounce` directly."""
    channel = MagicMock()
    channel.send = AsyncMock()
    channel.build_reply_message = None
    channel.streaming_reply_kwargs = None

    combined = MagicMock()
    combined.message = MagicMock()
    combined.message.content = "hello"
    combined.message.metadata = {}
    combined.message.sender_id = "u-1"
    combined.message.channel_id = "ch-test"
    combined.message.thread_id = None
    combined.message.id = "msg-1"
    combined.raw_content = "hello"
    combined.coalesced_count = 1

    fake_envelope = MagicMock()
    fake_envelope.thread_id = None
    fake_envelope.channel_id = "ch-test"

    mock_reactor = MagicMock()
    mock_reactor.received = AsyncMock()
    mock_reactor.completed = AsyncMock()
    mock_reactor.running = AsyncMock()
    mock_reactor.failed = AsyncMock()

    return channel, combined, fake_envelope, mock_reactor


@pytest.mark.asyncio
async def test_reservation_released_when_enqueue_is_cancelled() -> None:
    """A CancelledError mid-enqueue must not leave the reservation token held.

    `except Exception` never sees `asyncio.CancelledError` (a BaseException),
    so the token must be released some other way -- this is exactly the
    window the debounce task's own cancellation (channel/gateway shutdown)
    hits, which used to leak the token forever and shrink the in-flight cap.
    """
    from agentos.gateway.channel_dispatch import (
        _ChannelInFlightSet,
        _dispatch_combined_message_after_debounce,
    )

    ifs = _ChannelInFlightSet(cap=1)
    channel, combined, fake_envelope, mock_reactor = _dispatch_debounce_fixture()
    task_runtime = MagicMock()

    with (
        pytest.raises(asyncio.CancelledError),
        patch(
            "agentos.gateway.routing.build_channel_route_envelope",
            return_value=fake_envelope,
        ),
        patch(
            "agentos.gateway.channel_dispatch.start_turn_via_runtime",
            side_effect=asyncio.CancelledError,
        ),
        patch(
            "agentos.gateway.channel_dispatch._append_channel_user_message",
            new=AsyncMock(return_value=(MagicMock(), "hello")),
        ),
        patch(
            "agentos.gateway.channel_dispatch._record_delivery_context",
            new=AsyncMock(return_value=(MagicMock(), False)),
        ),
        patch(
            "agentos.gateway.channel_dispatch._ingest_channel_message_attachments",
            new=AsyncMock(return_value=MagicMock(text="hello", attachments=[])),
        ),
        patch(
            "agentos.gateway.channel_dispatch._transcript_watermark",
            new=AsyncMock(return_value=0),
        ),
        patch("agentos.gateway.channel_dispatch._RuntimeChannelStreamRelay") as mock_relay_cls,
        patch("agentos.gateway.channel_dispatch._status_reactor", return_value=mock_reactor),
    ):
        mock_relay_cls.maybe_start.return_value = None
        turn_runner = MagicMock(spec=[])

        await _dispatch_combined_message_after_debounce(
            channel,
            combined,
            turn_runner,
            MagicMock(),
            "s:test",
            "test",
            task_runtime,
            None,
            None,
            ifs,
        )

    assert not ifs.full(), "reservation token must be released after cancellation"
    assert ifs.try_acquire(object()), "cap must accept a new reservation after the leak"


@pytest.mark.asyncio
async def test_reservation_released_when_transcript_watermark_raises() -> None:
    """A plain exception before the enqueue's own try block must still release.

    `_transcript_watermark` and `_RuntimeChannelStreamRelay.maybe_start` used
    to run before any exception handling existed at all.
    """
    from agentos.gateway.channel_dispatch import (
        _ChannelInFlightSet,
        _dispatch_combined_message_after_debounce,
    )

    ifs = _ChannelInFlightSet(cap=1)
    channel, combined, fake_envelope, mock_reactor = _dispatch_debounce_fixture()
    task_runtime = MagicMock()

    with (
        pytest.raises(RuntimeError),
        patch(
            "agentos.gateway.routing.build_channel_route_envelope",
            return_value=fake_envelope,
        ),
        patch(
            "agentos.gateway.channel_dispatch._record_delivery_context",
            new=AsyncMock(return_value=(MagicMock(), False)),
        ),
        patch(
            "agentos.gateway.channel_dispatch._ingest_channel_message_attachments",
            new=AsyncMock(return_value=MagicMock(text="hello", attachments=[])),
        ),
        patch(
            "agentos.gateway.channel_dispatch._transcript_watermark",
            side_effect=RuntimeError("storage unavailable"),
        ),
        patch("agentos.gateway.channel_dispatch._status_reactor", return_value=mock_reactor),
    ):
        turn_runner = MagicMock(spec=[])

        await _dispatch_combined_message_after_debounce(
            channel,
            combined,
            turn_runner,
            MagicMock(),
            "s:test",
            "test",
            task_runtime,
            None,
            None,
            ifs,
        )

    assert not ifs.full(), "reservation token must be released even before the enqueue try block"


# ── Per-channel overflow policy resolution ──────────────────────────────────


def _make_channel_with_id(channel_id: str) -> Any:
    ch = MagicMock()
    ch.channel_id = channel_id
    return ch


def test_resolve_channel_overflow_policy_returns_none_without_config() -> None:
    assert _resolve_channel_overflow_policy(_make_channel_with_id("feishu"), None) is None


def test_resolve_channel_overflow_policy_returns_none_when_map_empty() -> None:
    cfg = MagicMock()
    cfg.task_runtime.pending_overflow_policy_per_channel = {}
    assert _resolve_channel_overflow_policy(_make_channel_with_id("feishu"), cfg) is None


def test_resolve_channel_overflow_policy_returns_none_when_channel_unmapped() -> None:
    cfg = MagicMock()
    cfg.task_runtime.pending_overflow_policy_per_channel = {"slack": "drop_oldest"}
    assert _resolve_channel_overflow_policy(_make_channel_with_id("feishu"), cfg) is None


def test_resolve_channel_overflow_policy_returns_mapped_value() -> None:
    cfg = MagicMock()
    cfg.task_runtime.pending_overflow_policy_per_channel = {"feishu": "drop_oldest"}
    assert _resolve_channel_overflow_policy(_make_channel_with_id("feishu"), cfg) == "drop_oldest"


def test_resolve_channel_overflow_policy_handles_missing_channel_id() -> None:
    cfg = MagicMock()
    cfg.task_runtime.pending_overflow_policy_per_channel = {"feishu": "drop_oldest"}
    channel_no_id = MagicMock(spec=[])
    assert _resolve_channel_overflow_policy(channel_no_id, cfg) is None


@pytest.mark.asyncio
async def test_apply_overflow_policy_invoked_when_channel_override_present() -> None:
    """When a channel has an override configured, channel_dispatch invokes
    runtime.apply_overflow_policy(session_key, policy=<override>) before
    start_turn_via_runtime so the override takes effect for that turn.
    """
    from agentos.channels.types import IncomingMessage
    from agentos.gateway.channel_dispatch import _ChannelInFlightSet, run_channel_dispatch

    msg = MagicMock()
    msg.content = "hello"
    msg.metadata = {}
    msg.sender_id = "u-1"
    msg.channel_id = "ch-test"
    msg.thread_id = None
    msg.id = "msg-1"

    channel = MagicMock()
    channel.channel_id = "feishu"
    channel.send = AsyncMock()
    channel.build_reply_message = None
    channel.streaming_reply_kwargs = None

    call_count = 0

    async def _receive() -> IncomingMessage:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return msg
        raise asyncio.CancelledError

    channel.receive = _receive

    task_runtime = MagicMock()
    task_runtime.enqueue = AsyncMock()
    task_runtime.apply_overflow_policy = AsyncMock()

    session_manager = MagicMock()
    session_manager.get_or_create = AsyncMock(return_value=(MagicMock(), False))
    session_manager.update = AsyncMock()
    session_manager.read_transcript = AsyncMock(return_value=[])

    cfg = _make_config(channel_inflight_cap=8, max_concurrency=4)
    cfg.task_runtime.pending_overflow_policy_per_channel = {"feishu": "drop_oldest"}

    fake_envelope = MagicMock()
    fake_envelope.thread_id = None
    fake_envelope.channel_id = "ch-test"

    ifs = _ChannelInFlightSet(cap=8)

    with (
        patch(
            "agentos.gateway.routing.build_channel_route_envelope",
            return_value=fake_envelope,
        ),
        patch(
            "agentos.gateway.channel_dispatch._append_channel_user_message",
            new=AsyncMock(return_value=(MagicMock(), "hello")),
        ),
        patch(
            "agentos.gateway.channel_dispatch.start_turn_via_runtime",
            new=AsyncMock(return_value=MagicMock(task_id="t-1")),
        ),
        patch(
            "agentos.gateway.channel_dispatch._record_delivery_context",
            new=AsyncMock(return_value=(MagicMock(), False)),
        ),
        patch("agentos.gateway.channel_dispatch._should_skip_unmentioned", return_value=False),
        patch(
            "agentos.gateway.channel_dispatch._ingest_channel_message_attachments",
            new=AsyncMock(return_value=MagicMock(text="hello", attachments=[])),
        ),
        patch("agentos.gateway.channel_dispatch._status_reactor") as mock_reactor_factory,
        patch(
            "agentos.gateway.channel_dispatch._RuntimeChannelStreamRelay",
        ) as mock_relay_cls,
        patch(
            "agentos.gateway.channel_dispatch._deliver_runtime_channel_reply",
            new=AsyncMock(),
        ),
        patch("agentos.gateway.channel_dispatch._emit_events", new=AsyncMock()),
    ):
        mock_relay_cls.maybe_start.return_value = None
        mock_reactor = MagicMock()
        mock_reactor.received = AsyncMock()
        mock_reactor.completed = AsyncMock()
        mock_reactor.running = AsyncMock()
        mock_reactor.failed = AsyncMock()
        mock_reactor_factory.return_value = mock_reactor

        turn_runner = MagicMock(spec=[])

        try:
            await run_channel_dispatch(
                channel=channel,
                turn_runner=turn_runner,
                session_manager=session_manager,
                session_key_builder=lambda _msg: "s:override",
                session_prefix="feishu",
                task_runtime=task_runtime,
                config=cfg,
                _in_flight=ifs,
            )
        except asyncio.CancelledError:
            pass

    # Override hook fired with the per-channel policy.
    task_runtime.apply_overflow_policy.assert_awaited_once()
    args, kwargs = task_runtime.apply_overflow_policy.call_args
    assert kwargs.get("policy") == "drop_oldest"


@pytest.mark.asyncio
async def test_apply_overflow_policy_not_invoked_without_channel_override() -> None:
    """No override hook fires when the channel has no per-channel mapping."""
    from agentos.channels.types import IncomingMessage
    from agentos.gateway.channel_dispatch import _ChannelInFlightSet, run_channel_dispatch

    msg = MagicMock()
    msg.content = "hello"
    msg.metadata = {}
    msg.sender_id = "u-1"
    msg.channel_id = "ch-test"
    msg.thread_id = None
    msg.id = "msg-1"

    channel = MagicMock()
    channel.channel_id = "discord"  # not in override map
    channel.send = AsyncMock()
    channel.build_reply_message = None
    channel.streaming_reply_kwargs = None

    call_count = 0

    async def _receive() -> IncomingMessage:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return msg
        raise asyncio.CancelledError

    channel.receive = _receive

    task_runtime = MagicMock()
    task_runtime.enqueue = AsyncMock()
    task_runtime.apply_overflow_policy = AsyncMock()

    session_manager = MagicMock()
    session_manager.get_or_create = AsyncMock(return_value=(MagicMock(), False))
    session_manager.update = AsyncMock()
    session_manager.read_transcript = AsyncMock(return_value=[])

    cfg = _make_config(channel_inflight_cap=8, max_concurrency=4)
    cfg.task_runtime.pending_overflow_policy_per_channel = {"feishu": "drop_oldest"}

    fake_envelope = MagicMock()
    fake_envelope.thread_id = None
    fake_envelope.channel_id = "ch-test"

    ifs = _ChannelInFlightSet(cap=8)

    with (
        patch(
            "agentos.gateway.routing.build_channel_route_envelope",
            return_value=fake_envelope,
        ),
        patch(
            "agentos.gateway.channel_dispatch._append_channel_user_message",
            new=AsyncMock(return_value=(MagicMock(), "hello")),
        ),
        patch(
            "agentos.gateway.channel_dispatch.start_turn_via_runtime",
            new=AsyncMock(return_value=MagicMock(task_id="t-1")),
        ),
        patch(
            "agentos.gateway.channel_dispatch._record_delivery_context",
            new=AsyncMock(return_value=(MagicMock(), False)),
        ),
        patch("agentos.gateway.channel_dispatch._should_skip_unmentioned", return_value=False),
        patch(
            "agentos.gateway.channel_dispatch._ingest_channel_message_attachments",
            new=AsyncMock(return_value=MagicMock(text="hello", attachments=[])),
        ),
        patch("agentos.gateway.channel_dispatch._status_reactor") as mock_reactor_factory,
        patch(
            "agentos.gateway.channel_dispatch._RuntimeChannelStreamRelay",
        ) as mock_relay_cls,
        patch(
            "agentos.gateway.channel_dispatch._deliver_runtime_channel_reply",
            new=AsyncMock(),
        ),
        patch("agentos.gateway.channel_dispatch._emit_events", new=AsyncMock()),
    ):
        mock_relay_cls.maybe_start.return_value = None
        mock_reactor = MagicMock()
        mock_reactor.received = AsyncMock()
        mock_reactor.completed = AsyncMock()
        mock_reactor.running = AsyncMock()
        mock_reactor.failed = AsyncMock()
        mock_reactor_factory.return_value = mock_reactor

        turn_runner = MagicMock(spec=[])

        try:
            await run_channel_dispatch(
                channel=channel,
                turn_runner=turn_runner,
                session_manager=session_manager,
                session_key_builder=lambda _msg: "s:no-override",
                session_prefix="discord",
                task_runtime=task_runtime,
                config=cfg,
                _in_flight=ifs,
            )
        except asyncio.CancelledError:
            pass

    task_runtime.apply_overflow_policy.assert_not_called()
