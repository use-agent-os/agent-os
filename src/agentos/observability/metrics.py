"""In-memory metrics registry and Prometheus exposition formatter."""

from __future__ import annotations

import math
import threading
from collections import defaultdict
from enum import StrEnum
from typing import Any

import structlog

log = structlog.get_logger(__name__)


class MetricType(StrEnum):
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"


def _format_labels(labels: dict[str, str] | None) -> str:
    if not labels:
        return ""
    items: list[str] = []
    for k in sorted(labels.keys()):
        v = str(labels[k])
        escaped = v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        items.append(f'{k}="{escaped}"')
    return "{" + ",".join(items) + "}"


def _format_number(val: float | int) -> str:
    if isinstance(val, int):
        return str(val)
    if math.isinf(val):
        return "+Inf" if val > 0 else "-Inf"
    if math.isnan(val):
        return "NaN"
    # Render clean integer if whole number float
    if val.is_integer():
        return str(int(val))
    return f"{val:.6g}"


class Counter:
    """Thread-safe multi-dimensional metric counter."""

    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...] = ()) -> None:
        self.name = name
        self.help_text = help_text
        self.label_names = label_names
        self._lock = threading.Lock()
        self._values: dict[tuple[tuple[str, str], ...], float] = defaultdict(float)

    def inc(self, value: float = 1.0, labels: dict[str, str] | None = None) -> None:
        if value < 0:
            raise ValueError("Counter increments must be non-negative")
        key = tuple(sorted((k, str(v)) for k, v in (labels or {}).items()))
        with self._lock:
            self._values[key] += value

    def get(self, labels: dict[str, str] | None = None) -> float:
        key = tuple(sorted((k, str(v)) for k, v in (labels or {}).items()))
        with self._lock:
            return self._values.get(key, 0.0)

    def reset(self) -> None:
        with self._lock:
            self._values.clear()

    def format_prometheus(self) -> list[str]:
        with self._lock:
            items = list(self._values.items())
        lines = [
            f"# HELP {self.name} {self.help_text}",
            f"# TYPE {self.name} counter",
        ]
        if not items:
            lines.append(f"{self.name} 0")
            return lines
        for label_tuples, val in sorted(items, key=lambda x: x[0]):
            labels_dict = dict(label_tuples)
            label_str = _format_labels(labels_dict)
            lines.append(f"{self.name}{label_str} {_format_number(val)}")
        return lines


class Gauge:
    """Thread-safe multi-dimensional metric gauge."""

    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...] = ()) -> None:
        self.name = name
        self.help_text = help_text
        self.label_names = label_names
        self._lock = threading.Lock()
        self._values: dict[tuple[tuple[str, str], ...], float] = defaultdict(float)

    def set(self, value: float, labels: dict[str, str] | None = None) -> None:
        key = tuple(sorted((k, str(v)) for k, v in (labels or {}).items()))
        with self._lock:
            self._values[key] = float(value)

    def inc(self, value: float = 1.0, labels: dict[str, str] | None = None) -> None:
        key = tuple(sorted((k, str(v)) for k, v in (labels or {}).items()))
        with self._lock:
            self._values[key] += float(value)

    def dec(self, value: float = 1.0, labels: dict[str, str] | None = None) -> None:
        key = tuple(sorted((k, str(v)) for k, v in (labels or {}).items()))
        with self._lock:
            self._values[key] -= float(value)

    def get(self, labels: dict[str, str] | None = None) -> float:
        key = tuple(sorted((k, str(v)) for k, v in (labels or {}).items()))
        with self._lock:
            return self._values.get(key, 0.0)

    def reset(self) -> None:
        with self._lock:
            self._values.clear()

    def format_prometheus(self) -> list[str]:
        with self._lock:
            items = list(self._values.items())
        lines = [
            f"# HELP {self.name} {self.help_text}",
            f"# TYPE {self.name} gauge",
        ]
        if not items:
            lines.append(f"{self.name} 0")
            return lines
        for label_tuples, val in sorted(items, key=lambda x: x[0]):
            labels_dict = dict(label_tuples)
            label_str = _format_labels(labels_dict)
            lines.append(f"{self.name}{label_str} {_format_number(val)}")
        return lines


DEFAULT_HISTOGRAM_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
)


class Histogram:
    """Thread-safe multi-dimensional metric histogram."""

    def __init__(
        self,
        name: str,
        help_text: str,
        label_names: tuple[str, ...] = (),
        buckets: tuple[float, ...] = DEFAULT_HISTOGRAM_BUCKETS,
    ) -> None:
        self.name = name
        self.help_text = help_text
        self.label_names = label_names
        self.buckets = tuple(sorted(buckets))
        self._lock = threading.Lock()
        # key -> (counts per bucket, sum, count)
        self._data: dict[tuple[tuple[str, str], ...], tuple[list[int], float, int]] = {}

    def observe(self, value: float, labels: dict[str, str] | None = None) -> None:
        val = float(value)
        key = tuple(sorted((k, str(v)) for k, v in (labels or {}).items()))
        with self._lock:
            if key not in self._data:
                self._data[key] = ([0] * len(self.buckets), 0.0, 0)
            bucket_counts, sum_val, count = self._data[key]
            for i, b in enumerate(self.buckets):
                if val <= b:
                    bucket_counts[i] += 1
            self._data[key] = (bucket_counts, sum_val + val, count + 1)

    def reset(self) -> None:
        with self._lock:
            self._data.clear()

    def format_prometheus(self) -> list[str]:
        with self._lock:
            items = list(self._data.items())
        lines = [
            f"# HELP {self.name} {self.help_text}",
            f"# TYPE {self.name} histogram",
        ]
        if not items:
            for b in self.buckets:
                lines.append(f'{self.name}_bucket{{le="{_format_number(b)}"}} 0')
            lines.append(f'{self.name}_bucket{{le="+Inf"}} 0')
            lines.append(f"{self.name}_sum 0")
            lines.append(f"{self.name}_count 0")
            return lines

        for label_tuples, (bucket_counts, sum_val, count) in sorted(items, key=lambda x: x[0]):
            base_labels = dict(label_tuples)
            for i, b in enumerate(self.buckets):
                lbls = {**base_labels, "le": _format_number(b)}
                lines.append(f"{self.name}_bucket{_format_labels(lbls)} {bucket_counts[i]}")
            inf_lbls = {**base_labels, "le": "+Inf"}
            lines.append(f"{self.name}_bucket{_format_labels(inf_lbls)} {count}")
            lines.append(f"{self.name}_sum{_format_labels(base_labels)} {_format_number(sum_val)}")
            lines.append(f"{self.name}_count{_format_labels(base_labels)} {count}")
        return lines


class MetricsRegistry:
    """Central registry of all AgentOS system metrics."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, Counter] = {}
        self._gauges: dict[str, Gauge] = {}
        self._histograms: dict[str, Histogram] = {}
        self._register_default_metrics()

    def _register_default_metrics(self) -> None:
        # Core metrics (contract names locked per AGENTS.md)
        self.register_gauge("agentos_queue_depth", "Pending task queue depth per session")
        self.register_counter("in_flight_turns_total", "Cumulative turns entering execution")
        self.register_counter("turn_cancellations_total", "Cumulative turn cancellations")
        self.register_counter("queue_full_errors_total", "Cumulative queue full rejections")

        # Extended operational metrics
        self.register_counter("agentos_turns_total", "Cumulative agent turns by outcome status")
        self.register_histogram(
            "agentos_turn_duration_seconds",
            "Agent turn execution duration in seconds",
            buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0),
        )
        self.register_counter("agentos_provider_errors_total", "Cumulative LLM provider errors")
        self.register_counter("agentos_tokens_total", "Cumulative tokens processed (input/output)")
        self.register_counter("agentos_spend_dollars_total", "Cumulative LLM spend in USD")
        self.register_counter("agentos_http_requests_total", "Cumulative gateway HTTP requests")
        self.register_histogram(
            "agentos_http_request_duration_seconds",
            "Gateway HTTP request duration in seconds",
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
        )

    def register_counter(
        self, name: str, help_text: str, label_names: tuple[str, ...] = ()
    ) -> Counter:
        with self._lock:
            if name not in self._counters:
                self._counters[name] = Counter(name, help_text, label_names)
            return self._counters[name]

    def register_gauge(self, name: str, help_text: str, label_names: tuple[str, ...] = ()) -> Gauge:
        with self._lock:
            if name not in self._gauges:
                self._gauges[name] = Gauge(name, help_text, label_names)
            return self._gauges[name]

    def register_histogram(
        self,
        name: str,
        help_text: str,
        label_names: tuple[str, ...] = (),
        buckets: tuple[float, ...] = DEFAULT_HISTOGRAM_BUCKETS,
    ) -> Histogram:
        with self._lock:
            if name not in self._histograms:
                self._histograms[name] = Histogram(name, help_text, label_names, buckets)
            return self._histograms[name]

    def get_counter(self, name: str) -> Counter | None:
        with self._lock:
            return self._counters.get(name)

    def get_gauge(self, name: str) -> Gauge | None:
        with self._lock:
            return self._gauges.get(name)

    def get_histogram(self, name: str) -> Histogram | None:
        with self._lock:
            return self._histograms.get(name)

    def record(self, name: str, value: float = 1.0, **labels: Any) -> None:
        """Dynamically record a metric without throwing if unregistered."""
        clean_labels = {k: str(v) for k, v in labels.items() if v is not None}
        with self._lock:
            counter = self._counters.get(name)
            gauge = self._gauges.get(name)
            histogram = self._histograms.get(name)

        if counter is not None:
            counter.inc(value, clean_labels)
        elif gauge is not None:
            gauge.set(value, clean_labels)
        elif histogram is not None:
            histogram.observe(value, clean_labels)
        else:
            # Auto-register as counter if unknown
            new_counter = self.register_counter(name, f"Dynamically registered {name}")
            new_counter.inc(value, clean_labels)

    def reset(self) -> None:
        with self._lock:
            for c in self._counters.values():
                c.reset()
            for g in self._gauges.values():
                g.reset()
            for h in self._histograms.values():
                h.reset()

    def format_prometheus(self) -> str:
        """Format all registered metrics into standard Prometheus text format."""
        with self._lock:
            all_metrics: list[Counter | Gauge | Histogram] = [
                *self._counters.values(),
                *self._gauges.values(),
                *self._histograms.values(),
            ]
        all_metrics.sort(key=lambda m: m.name)
        output_blocks: list[str] = []
        for metric in all_metrics:
            lines = metric.format_prometheus()
            if lines:
                output_blocks.append("\n".join(lines))
        return "\n\n".join(output_blocks) + "\n"


_GLOBAL_REGISTRY: MetricsRegistry | None = None
_GLOBAL_REGISTRY_LOCK = threading.Lock()


def get_metrics_registry() -> MetricsRegistry:
    global _GLOBAL_REGISTRY
    if _GLOBAL_REGISTRY is None:
        with _GLOBAL_REGISTRY_LOCK:
            if _GLOBAL_REGISTRY is None:
                _GLOBAL_REGISTRY = MetricsRegistry()
    return _GLOBAL_REGISTRY


def record_metric(name: str, value: float = 1.0, **labels: Any) -> None:
    """Safe global entry point to record a metric."""
    try:
        get_metrics_registry().record(name, value, **labels)
    except Exception as exc:
        log.debug("metrics.record_error", metric=name, error=str(exc))


def format_prometheus_metrics() -> str:
    """Return Prometheus text exposition for all registered metrics."""
    return get_metrics_registry().format_prometheus()
