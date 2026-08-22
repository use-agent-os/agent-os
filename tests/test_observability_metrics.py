from __future__ import annotations

from starlette.testclient import TestClient

from agentos.gateway.app import create_gateway_app
from agentos.gateway.config import GatewayConfig
from agentos.observability.metrics import (
    Counter,
    Gauge,
    Histogram,
    MetricsRegistry,
    record_metric,
)


def test_counter_inc_and_prometheus_format() -> None:
    counter = Counter("test_turns_total", "Test counter help")
    counter.inc(1.0)
    counter.inc(2.0, {"status": "ok", "agent": "default"})

    lines = counter.format_prometheus()
    text = "\n".join(lines)

    assert "# HELP test_turns_total Test counter help" in text
    assert "# TYPE test_turns_total counter" in text
    assert "test_turns_total 1" in text
    assert 'test_turns_total{agent="default",status="ok"} 2' in text


def test_gauge_set_inc_dec_and_format() -> None:
    gauge = Gauge("test_queue_depth", "Test gauge help")
    gauge.set(10.0, {"session": "s1"})
    gauge.inc(2.0, {"session": "s1"})
    gauge.dec(1.0, {"session": "s1"})

    lines = gauge.format_prometheus()
    text = "\n".join(lines)

    assert "# HELP test_queue_depth Test gauge help" in text
    assert "# TYPE test_queue_depth gauge" in text
    assert 'test_queue_depth{session="s1"} 11' in text


def test_histogram_observe_and_buckets() -> None:
    hist = Histogram(
        "test_latency_seconds",
        "Test histogram",
        buckets=(0.1, 0.5, 1.0, 5.0),
    )
    hist.observe(0.05, {"handler": "chat"})
    hist.observe(0.3, {"handler": "chat"})
    hist.observe(2.0, {"handler": "chat"})

    lines = hist.format_prometheus()
    text = "\n".join(lines)

    assert "# HELP test_latency_seconds Test histogram" in text
    assert "# TYPE test_latency_seconds histogram" in text
    assert 'test_latency_seconds_bucket{handler="chat",le="0.1"} 1' in text
    assert 'test_latency_seconds_bucket{handler="chat",le="0.5"} 2' in text
    assert 'test_latency_seconds_bucket{handler="chat",le="1"} 2' in text
    assert 'test_latency_seconds_bucket{handler="chat",le="5"} 3' in text
    assert 'test_latency_seconds_bucket{handler="chat",le="+Inf"} 3' in text
    assert 'test_latency_seconds_count{handler="chat"} 3' in text


def test_metrics_registry_record_and_reset() -> None:
    reg = MetricsRegistry()
    reg.record("agentos_queue_depth", 5, session_key="sess-1")
    reg.record("in_flight_turns_total", 1)

    text = reg.format_prometheus()
    assert "agentos_queue_depth" in text
    assert "in_flight_turns_total" in text

    reg.reset()
    text_after = reg.format_prometheus()
    assert 'agentos_queue_depth{session_key="sess-1"}' not in text_after


def test_gateway_metrics_endpoint() -> None:
    cfg = GatewayConfig()
    app = create_gateway_app(config=cfg)
    with TestClient(app, base_url="http://localhost") as client:
        record_metric("agentos_queue_depth", 42, agent_id="agent-test")
        record_metric("in_flight_turns_total", 1)

        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]
        assert "agentos_queue_depth" in resp.text
        assert "in_flight_turns_total" in resp.text
