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
