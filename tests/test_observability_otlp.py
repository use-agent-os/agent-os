from __future__ import annotations

from typing import Any

import pytest

from agentos.observability.otlp import (
    OtlpTraceSink,
    _iso_to_unix_nano,
    _to_hex16,
    _to_hex32,
)
from agentos.observability.trace import TraceContext, TraceEvent


def test_hex_conversion_helpers() -> None:
    assert len(_to_hex32("4bf92f3577b34da6a3ce929d0e0e4736")) == 32
    assert len(_to_hex32("custom-arbitrary-trace-id-12345")) == 32
    assert len(_to_hex16("00f067aa0ba902b7")) == 16
    assert len(_to_hex16("arbitrary-run-id-987")) == 16


def test_iso_to_unix_nano() -> None:
    nano = _iso_to_unix_nano("2026-08-22T06:00:00Z")
    assert isinstance(nano, int)
    assert nano > 0


def test_build_export_payload() -> None:
    sink = OtlpTraceSink(
        endpoint="http://localhost:4318",
        service_name="agentos-test",
        service_version="1.0.0",
    )

    ctx = TraceContext.new(
        trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
        session_key="sess-test",
        turn_id="turn-42",
        run_id="run-100",
        parent_run_id="parent-050",
        agent_id="main-agent",
    )
    event = TraceEvent(
        kind="llm_call",
        context=ctx,
        attrs={"model": "deepseek-chat", "temperature": 0.7, "stream": True},
    )

    payload = sink.build_export_payload([event])

    assert "resourceSpans" in payload
    resource_spans = payload["resourceSpans"]
    assert len(resource_spans) == 1

    scope_spans = resource_spans[0]["scopeSpans"]
    assert len(scope_spans) == 1

    spans = scope_spans[0]["spans"]
    assert len(spans) == 1

    span = spans[0]
    assert span["traceId"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert len(span["spanId"]) == 16
    assert span["name"] == "agentos.llm_call"

    attr_dict = {a["key"]: a["value"] for a in span["attributes"]}
    assert attr_dict["agentos.kind"]["stringValue"] == "llm_call"
    assert attr_dict["agentos.agent_id"]["stringValue"] == "main-agent"
    assert attr_dict["attr.model"]["stringValue"] == "deepseek-chat"
    assert attr_dict["attr.stream"]["boolValue"] is True


@pytest.mark.asyncio
async def test_otlp_flush_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    captured_requests: list[dict[str, Any]] = []

    class _MockClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _MockClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, url: str, json: Any = None, headers: Any = None) -> Any:
            captured_requests.append({"url": url, "json": json, "headers": headers})

            class _MockResp:
                status_code = 200

                def raise_for_status(self) -> None:
                    pass

            return _MockResp()

    monkeypatch.setattr(httpx, "AsyncClient", _MockClient)

    sink = OtlpTraceSink(endpoint="http://collector.internal:4318/v1/traces")
    ctx = TraceContext.new(trace_id="test-trace-1")
    event = TraceEvent(kind="turn_start", context=ctx)

    sink.write(event)
    success = await sink.flush()

    assert success is True
    assert len(captured_requests) == 1
    assert captured_requests[0]["url"] == "http://collector.internal:4318/v1/traces"
    assert "resourceSpans" in captured_requests[0]["json"]


def test_otlp_queue_capacity_bounds() -> None:
    sink = OtlpTraceSink(max_queue_size=5)
    ctx = TraceContext.new(trace_id="bound-test")

    # Write 10 events into queue with max_queue_size=5
    for i in range(10):
        sink.write(TraceEvent(kind=f"event_{i}", context=ctx))

    assert len(sink._queue) == 5
    # The 5 remaining should be the newest events (5..9)
    kinds = [e.kind for e in sink._queue]
    assert kinds == ["event_5", "event_6", "event_7", "event_8", "event_9"]


def test_trace_sink_registration_and_fanout(tmp_path: Any) -> None:
    from agentos.observability.trace import (
        MemoryTraceSink,
        clear_trace_sinks,
        get_trace_sinks,
        register_trace_sink,
        unregister_trace_sink,
        write_trace_event,
    )

    clear_trace_sinks()
    mem_sink = MemoryTraceSink()
    try:
        register_trace_sink(mem_sink)
        assert mem_sink in get_trace_sinks()

        ctx = TraceContext.new(trace_id="fanout-trace")
        event = TraceEvent(kind="custom_action", context=ctx)

        path = write_trace_event(event, log_dir=tmp_path)
        assert path.exists()
        assert len(mem_sink.events) == 1
        assert mem_sink.events[0].kind == "custom_action"

        unregister_trace_sink(mem_sink)
        assert mem_sink not in get_trace_sinks()
    finally:
        clear_trace_sinks()


@pytest.mark.asyncio
async def test_boot_build_services_otlp_lifecycle() -> None:
    from agentos.gateway.boot import build_services
    from agentos.gateway.config import GatewayConfig
    from agentos.observability.trace import clear_trace_sinks, get_trace_sinks

    clear_trace_sinks()
    cfg = GatewayConfig.model_validate(
        {
            "observability": {
                "otlp_enabled": True,
                "otlp_endpoint": "http://collector:4318",
                "otlp_service_name": "agentos-prod",
            }
        }
    )

    try:
        svc = await build_services(config=cfg)
        assert svc.otlp_trace_sink is not None
        assert svc.otlp_trace_sink in get_trace_sinks()
        assert svc.otlp_trace_sink.service_name == "agentos-prod"

        await svc.close()
        assert svc.otlp_trace_sink is None
        assert len(get_trace_sinks()) == 0
    finally:
        clear_trace_sinks()


@pytest.mark.asyncio
async def test_otlp_interval_flush(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    import httpx

    flushed_events: list[dict[str, Any]] = []

    class _MockClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _MockClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, url: str, json: Any = None, headers: Any = None) -> Any:
            flushed_events.append(json)

            class _MockResp:
                status_code = 200

                def raise_for_status(self) -> None:
                    pass

            return _MockResp()

    monkeypatch.setattr(httpx, "AsyncClient", _MockClient)

    sink = OtlpTraceSink(
        endpoint="http://collector.internal:4318",
        flush_interval_s=0.05,
    )
    sink.start()
    assert sink._flush_task is not None

    ctx = TraceContext.new(trace_id="test-interval-flush")
    sink.write(TraceEvent(kind="timed_event", context=ctx))

    # Wait for periodic flush task to trigger
    for _ in range(20):
        if flushed_events:
            break
        await asyncio.sleep(0.02)

    assert len(flushed_events) == 1
    assert len(sink._queue) == 0

    await sink.close()
    assert sink._flush_task is None


@pytest.mark.asyncio
async def test_otlp_concurrent_flush_serialized(monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent flush() calls must serialize on _flush_lock.

    Regression for #672: without the lock, two flush() coroutines could post
    concurrently, delivering spans out of order and re-queueing the same
    events twice on failure. With the lock, each flush drains the queue
    exactly once and only one HTTP post is in flight at a time.
    """
    import asyncio
    import time

    import httpx

    in_flight = 0
    max_in_flight = 0
    posts: list[tuple[int, float]] = []

    class _MockClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _MockClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, url: str, json: Any = None, headers: Any = None) -> Any:
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            posts.append((time.monotonic_ns(), len(json["resourceSpans"])))
            await asyncio.sleep(0.05)  # hold the lock open so overlap is visible

            class _MockResp:
                status_code = 200

                def raise_for_status(self) -> None:
                    pass

            in_flight -= 1
            return _MockResp()

    monkeypatch.setattr(httpx, "AsyncClient", _MockClient)

    sink = OtlpTraceSink(endpoint="http://collector.internal:4318/v1/traces")
    ctx = TraceContext.new(trace_id="concurrent-flush-test")

    # Fire several flushes concurrently when the queue already has events.
    for i in range(10):
        sink.write(TraceEvent(kind=f"ev_{i}", context=ctx))

    results = await asyncio.gather(*[sink.flush() for _ in range(5)])

    assert all(results), "every concurrent flush should report success"
    assert max_in_flight == 1, (
        f"flush() calls must be serialized by _flush_lock; observed {max_in_flight} posts in flight"
    )
    assert sink._queue == [], "queue must be fully drained after serialized flush"


@pytest.mark.asyncio
async def test_otlp_concurrent_failed_flush_no_double_requeue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On failure, events must be re-queued exactly once, not per concurrent caller."""

    import asyncio

    import httpx

    class _FailingClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _FailingClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, url: str, json: Any = None, headers: Any = None) -> Any:
            await asyncio.sleep(0.02)
            raise httpx.ConnectError("collector unreachable")

    monkeypatch.setattr(httpx, "AsyncClient", _FailingClient)

    sink = OtlpTraceSink(
        endpoint="http://collector.internal:4318/v1/traces",
        max_queue_size=100,
    )
    ctx = TraceContext.new(trace_id="concurrent-fail-test")

    for i in range(5):
        sink.write(TraceEvent(kind=f"ev_{i}", context=ctx))

    # With serialization, the first flush re-queues once; the remaining
    # concurrent callers find an empty queue and return True (nothing to do).
    # The bug being fixed: without the lock, every caller would have drained
    # and re-queued the same 5 events, leaving duplicates in the queue.
    results = await asyncio.gather(*[sink.flush() for _ in range(5)])
    assert any(r is False for r in results), "at least one flush hit the failing export"
    assert len(sink._queue) == 5, (
        "events must be re-queued exactly once after concurrent failed flushes; "
        f"queue now holds {len(sink._queue)} events"
    )


def test_otlp_multithreaded_writes() -> None:
    import threading

    sink = OtlpTraceSink(max_queue_size=100)
    ctx = TraceContext.new(trace_id="thread-test")

    def _worker(thread_idx: int) -> None:
        for i in range(20):
            sink.write(TraceEvent(kind=f"t{thread_idx}_e{i}", context=ctx))

    threads = [threading.Thread(target=_worker, args=(t,)) for t in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(sink._queue) == 100
