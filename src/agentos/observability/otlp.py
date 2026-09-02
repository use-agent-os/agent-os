"""OTLP trace and span exporter for OpenTelemetry collectors."""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from datetime import datetime
from typing import Any

import structlog

from agentos import __version__
from agentos.env import trust_env as _trust_env
from agentos.observability.trace import TraceEvent, TraceSink

log = structlog.get_logger(__name__)


def _to_hex32(val: str) -> str:
    """Convert any trace_id string to a valid 32-character hexadecimal string."""
    clean = val.replace("-", "").strip().lower()
    if len(clean) == 32 and all(c in "0123456789abcdef" for c in clean):
        return clean
    # Hash into 32-hex characters (usedforsecurity=False keeps working on FIPS-mode hosts)
    return hashlib.md5(val.encode("utf-8"), usedforsecurity=False).hexdigest()


def _to_hex16(val: str) -> str:
    """Convert any span/run id string to a valid 16-character hexadecimal string."""
    clean = val.replace("-", "").strip().lower()
    if len(clean) == 16 and all(c in "0123456789abcdef" for c in clean):
        return clean
    # Hash and take first 16 hex characters
    return hashlib.sha256(val.encode("utf-8")).hexdigest()[:16]


def _iso_to_unix_nano(ts_str: str) -> int:
    """Convert ISO-8601 UTC timestamp to nanoseconds since Unix epoch."""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1_000_000_000)
    except Exception:
        return int(time.time() * 1_000_000_000)


def _to_otlp_value(val: Any) -> dict[str, Any]:
    """Convert Python primitive to OTLP AnyValue object."""
    if isinstance(val, bool):
        return {"boolValue": val}
    if isinstance(val, int):
        return {"intValue": str(val)}
    if isinstance(val, float):
        return {"doubleValue": val}
    if isinstance(val, (list, tuple)):
        return {"arrayValue": {"values": [_to_otlp_value(v) for v in val]}}
    if isinstance(val, dict):
        return {
            "kvlistValue": {
                "values": [{"key": str(k), "value": _to_otlp_value(v)} for k, v in val.items()]
            }
        }
    return {"stringValue": str(val)}


class OtlpTraceSink(TraceSink):
    """Trace sink that exports spans to an OpenTelemetry collector over OTLP/HTTP."""

    def __init__(
        self,
        endpoint: str = "http://localhost:4318",
        *,
        headers: dict[str, str] | None = None,
        service_name: str = "agentos",
        service_version: str | None = None,
        batch_size: int = 100,
        flush_interval_s: float = 5.0,
        max_queue_size: int = 1000,
        allow_raw: bool = False,
    ) -> None:
        raw_ep = endpoint.rstrip("/")
        if not raw_ep.endswith("/v1/traces"):
            self.endpoint = f"{raw_ep}/v1/traces"
        else:
            self.endpoint = raw_ep
        self.headers = headers or {}
        self.service_name = service_name
        self.service_version = service_version if service_version is not None else __version__
        self.batch_size = batch_size
        self.flush_interval_s = flush_interval_s
        self.max_queue_size = max_queue_size
        self.allow_raw = allow_raw

        self._queue: list[TraceEvent] = []
        self._queue_lock = threading.Lock()
        self._flush_lock = asyncio.Lock()
        self._flush_task: asyncio.Task[None] | None = None
        self._closed = False

    def start(self) -> None:
        """Start the background periodic flush task if not already running."""
        if self._closed or self.flush_interval_s <= 0:
            return
        if self._flush_task is None or self._flush_task.done():
            try:
                loop = asyncio.get_running_loop()
                if loop.is_running():
                    self._flush_task = loop.create_task(self._periodic_flush())
            except RuntimeError:
                pass

    async def _periodic_flush(self) -> None:
        """Periodic background flush loop."""
        while not self._closed:
            try:
                await asyncio.sleep(self.flush_interval_s)
                with self._queue_lock:
                    has_items = bool(self._queue)
                if has_items:
                    await self.flush()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("otlp.periodic_flush_failed", error=str(exc))

    def write(self, event: TraceEvent) -> None:
        """Buffer a trace event for OTLP export with bounded queue capacity."""
        if event.privacy == "raw" and not self.allow_raw:
            return
        if self._closed:
            return
        should_flush = False
        with self._queue_lock:
            if len(self._queue) >= self.max_queue_size:
                # Bound memory growth by evicting the oldest unexported event
                self._queue.pop(0)
            self._queue.append(event)
            if len(self._queue) >= self.batch_size:
                should_flush = True

        if self._flush_task is None or self._flush_task.done():
            self.start()

        if should_flush:
            try:
                loop = asyncio.get_running_loop()
                if loop.is_running():
                    loop.create_task(self.flush())
            except RuntimeError:
                pass

    def build_export_payload(self, events: list[TraceEvent]) -> dict[str, Any]:
        """Transform AgentOS TraceEvents into OTLP JSON ExportTraceServiceRequest."""
        spans: list[dict[str, Any]] = []
        for ev in events:
            trace_id = _to_hex32(ev.context.trace_id)
            span_seed = ev.context.run_id or ev.context.turn_id or ev.context.task_id or ev.kind
            span_id = _to_hex16(f"{span_seed}:{ev.seq or 0}")
            parent_span_id = _to_hex16(ev.context.parent_run_id) if ev.context.parent_run_id else ""

            ts_nano = _iso_to_unix_nano(ev.ts)
            # Duration approximation: default 1ms span for point events
            end_nano = ts_nano + 1_000_000

            attributes: list[dict[str, Any]] = [
                {"key": "agentos.kind", "value": {"stringValue": ev.kind}},
                {"key": "agentos.privacy", "value": {"stringValue": ev.privacy}},
            ]
            if ev.context.session_key:
                attributes.append(
                    {"key": "agentos.session_key", "value": {"stringValue": ev.context.session_key}}
                )
            if ev.context.session_id:
                attributes.append(
                    {"key": "agentos.session_id", "value": {"stringValue": ev.context.session_id}}
                )
            if ev.context.turn_id:
                attributes.append(
                    {"key": "agentos.turn_id", "value": {"stringValue": ev.context.turn_id}}
                )
            if ev.context.task_id:
                attributes.append(
                    {"key": "agentos.task_id", "value": {"stringValue": ev.context.task_id}}
                )
            if ev.context.agent_id:
                attributes.append(
                    {"key": "agentos.agent_id", "value": {"stringValue": ev.context.agent_id}}
                )

            for k, v in ev.attrs.items():
                attributes.append({"key": f"attr.{k}", "value": _to_otlp_value(v)})

            span_dict: dict[str, Any] = {
                "traceId": trace_id,
                "spanId": span_id,
                "name": f"agentos.{ev.kind}",
                "kind": 1,  # SPAN_KIND_INTERNAL
                "startTimeUnixNano": str(ts_nano),
                "endTimeUnixNano": str(end_nano),
                "attributes": attributes,
                "status": {"code": 1},  # STATUS_CODE_OK
            }
            if parent_span_id:
                span_dict["parentSpanId"] = parent_span_id
            spans.append(span_dict)

        return {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": self.service_name}},
                            {
                                "key": "service.version",
                                "value": {"stringValue": self.service_version},
                            },
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "agentos.observability"},
                            "spans": spans,
                        }
                    ],
                }
            ]
        }

    async def flush(self) -> bool:
        """Flush all buffered events to the OTLP HTTP collector.

        Acquires ``_flush_lock`` so concurrent calls from batch-triggered
        flush and ``_periodic_flush`` do not race.
        """
        if self._flush_lock.locked():
            return False
        async with self._flush_lock:
            import httpx

            with self._queue_lock:
                if not self._queue:
                    return True
                events_to_send = self._queue[:]
                self._queue.clear()

            if not events_to_send:
                return True

            payload = self.build_export_payload(events_to_send)
            req_headers = {
                "Content-Type": "application/json",
                **self.headers,
            }

            try:
                async with httpx.AsyncClient(timeout=10.0, trust_env=_trust_env()) as client:
                    resp = await client.post(self.endpoint, json=payload, headers=req_headers)
                    resp.raise_for_status()
                    return True
            except Exception as exc:
                log.warning("otlp.export_failed", endpoint=self.endpoint, error=str(exc))
                # Put unsent events back on failure up to max_queue_size
                with self._queue_lock:
                    remaining_space = max(0, self.max_queue_size - len(self._queue))
                    if remaining_space > 0:
                        self._queue = events_to_send[-remaining_space:] + self._queue
                return False

    async def close(self) -> None:
        """Close sink, cancel background flush task, and perform final flush."""
        self._closed = True
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._flush_task = None
        await self.flush()
