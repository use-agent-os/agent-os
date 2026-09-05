from __future__ import annotations

import pytest
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


def test_gateway_metrics_endpoint_disabled() -> None:
    cfg = GatewayConfig.model_validate({"observability": {"metrics_enabled": False}})
    app = create_gateway_app(config=cfg)
    with TestClient(app, base_url="http://localhost") as client:
        resp = client.get("/metrics")
        assert resp.status_code == 404


def test_gateway_metrics_endpoint_custom_path() -> None:
    cfg = GatewayConfig.model_validate(
        {"observability": {"metrics_enabled": True, "metrics_path": "/custom-metrics"}}
    )
    app = create_gateway_app(config=cfg)
    with TestClient(app, base_url="http://localhost") as client:
        resp = client.get("/custom-metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]

        # Default /metrics should not exist when customized
        resp_old = client.get("/metrics")
        assert resp_old.status_code == 404


def test_metrics_cardinality_and_session_key_exclusion() -> None:
    reg = MetricsRegistry(max_dynamic_metrics=5)

    # Session key is stripped to prevent cardinality leak
    reg.record("agentos_queue_depth", 10, session_key="user-sess-123", agent_id="default")
    text = reg.format_prometheus()
    assert "session_key" not in text
    assert 'agent_id="default"' in text

    # Per-metric series cap
    counter = Counter("bounded_counter", "help", max_series=3)
    for i in range(10):
        counter.inc(1.0, {"label_idx": str(i)})
    assert len(counter._values) == 3

    # Registry dynamic metric name cap
    for i in range(10):
        reg.record(f"dynamic_metric_{i}", 1.0)
    assert len(reg._dynamic_metrics) == 5


def test_agentos_queue_depth_help_text() -> None:
    reg = MetricsRegistry()
    text = reg.format_prometheus()
    assert "# HELP agentos_queue_depth Pending task queue depth across all sessions" in text


def test_gateway_config_rejects_metrics_path_under_control_ui_base_path() -> None:
    # Exact overlap with control_ui.base_path
    with pytest.raises(ValueError, match="cannot overlap or be located under control_ui.base_path"):
        GatewayConfig.model_validate(
            {
                "control_ui": {"base_path": "/control"},
                "observability": {"metrics_path": "/control"},
            }
        )

    # Subpath under control_ui.base_path
    with pytest.raises(ValueError, match="cannot overlap or be located under control_ui.base_path"):
        GatewayConfig.model_validate(
            {
                "control_ui": {"base_path": "/control"},
                "observability": {"metrics_path": "/control/metrics"},
            }
        )


def test_emit_metric_records_and_strips_session_identifiers() -> None:
    from agentos.observability.metrics import _emit_metric, get_metrics_registry

    _emit_metric(
        "turn_cancellations_total",
        value=1,
        reason="reply_task_error",
        session_key="agent:main:123",
        session_id="sess-456",
        turn_id="turn-789",
    )
    formatted = get_metrics_registry().format_prometheus()
    assert 'turn_cancellations_total{reason="reply_task_error"}' in formatted
    assert "session_key" not in formatted
    assert "session_id" not in formatted
