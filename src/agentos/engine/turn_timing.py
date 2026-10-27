"""Per-turn wall-clock breakdown: where the time of one turn goes.

A :class:`TurnTiming` is created at the start of ``TurnRunner._run_turn`` and
fed by a handful of call sites:

* each turn stage call is bracketed by :meth:`TurnTiming.stage_start` and
  :meth:`TurnTiming.stage_end` (two added lines, the call itself unchanged);
* ``Agent._turn_generator`` iterates every provider stream through
  :meth:`TurnTiming.provider_stream`, every tool batch through
  :meth:`TurnTiming.tool_batch`, and waits for approvals through the callable
  returned by :meth:`TurnTiming.timed_approval`.
* engine-initiated retries of a model call are counted per category through
  :meth:`TurnTiming.record_retry` (for example ``text_tool_call``).

:meth:`TurnTiming.summary` returns a JSON-ready dict (integer milliseconds)
that goes into the decision log; :meth:`TurnTiming.log_fields` is the flat
form logged as ``turn.timing``. Recording never raises into the turn: every
method is a few arithmetic operations on the injected monotonic clock.

``NULL_TURN_TIMING`` is a recorder that records nothing, used as the default
when an :class:`~agentos.engine.agent.Agent` runs outside a runtime turn.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import structlog

log = structlog.get_logger(__name__)

T = TypeVar("T")

#: The eight ``_run_turn`` stages, in execution order.
STAGE_NAMES: tuple[str, ...] = (
    "input",
    "provider_and_tools",
    "prompt_assembler",
    "agent_bootstrap",
    "compaction_and_history",
    "attachment",
    "stream_consumer",
    "turn_finalizer",
)

#: Per-turn detail lists are capped so one runaway tool loop cannot grow a
#: decision-log row without bound. Totals keep counting past the cap.
MAX_DETAIL_ENTRIES = 100


def _ms(seconds: float) -> int:
    return max(0, int(round(seconds * 1000)))


@dataclass
class ProviderCallTiming:
    """One provider request: request start through end of its stream."""

    request_started: float
    first_event: float | None = None
    first_text: float | None = None
    stream_done: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass
class ToolBatchTiming:
    started: float
    calls: int
    ended: float | None = None
    approval_wait: float = 0.0


class TurnTiming:
    """Monotonic-clock recorder for one turn. Not thread-safe; one event loop."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.started_at = clock()
        self.stages: dict[str, float] = {}
        self._stage_open: dict[str, float] = {}
        self.provider_calls: list[ProviderCallTiming] = []
        self.tool_batches: list[ToolBatchTiming] = []
        self._provider_call_count = 0
        self._provider_total = 0.0
        self._first_request_at: float | None = None
        self._first_text_at: float | None = None
        self._tool_total = 0.0
        self._tool_batch_count = 0
        self._tool_call_count = 0
        self._approval_total = 0.0
        self._approval_active = 0
        self._approval_since: float | None = None
        self._open_batch: ToolBatchTiming | None = None
        self.retries: dict[str, int] = {}
        self.logged = False

    # ── stages ──────────────────────────────────────────────────────────

    def stage_start(self, name: str) -> None:
        """Mark the start of stage ``name``."""

        self._stage_open[name] = self._clock()

    def stage_end(self, name: str) -> None:
        """Charge the time since :meth:`stage_start` to stage ``name``.

        A stage that ran more than once accumulates; an end without a start
        is ignored.
        """

        started = self._stage_open.pop(name, None)
        if started is not None:
            self.stages[name] = self.stages.get(name, 0.0) + (self._clock() - started)

    # ── provider calls ──────────────────────────────────────────────────

    async def provider_stream(self, stream: AsyncIterator[Any]) -> AsyncIterator[Any]:
        """Pass provider events through, marking request/first event/text/done.

        Provider heartbeats are transport keep-alives, not model output, so
        they do not count as the first event. The call is closed on the
        provider's ``done`` or ``error`` event (the consumer may stop
        iterating right after either), or when the stream ends or raises.
        """

        now = self._clock()
        call = ProviderCallTiming(request_started=now)
        self._provider_call_count += 1
        if self._first_request_at is None:
            self._first_request_at = now
        if len(self.provider_calls) < MAX_DETAIL_ENTRIES:
            self.provider_calls.append(call)
        try:
            async for event in stream:
                kind = getattr(event, "kind", None)
                if kind != "provider_heartbeat" and call.first_event is None:
                    call.first_event = self._clock()
                if kind == "text_delta" and call.first_text is None and getattr(event, "text", ""):
                    call.first_text = self._clock()
                    if self._first_text_at is None:
                        self._first_text_at = call.first_text
                elif kind == "done":
                    call.input_tokens = _int_or_none(getattr(event, "input_tokens", None))
                    call.output_tokens = _int_or_none(getattr(event, "output_tokens", None))
                    self._end_provider_call(call)
                elif kind == "error":
                    self._end_provider_call(call)
                yield event
        finally:
            self._end_provider_call(call)

    def _end_provider_call(self, call: ProviderCallTiming) -> None:
        if call.stream_done is not None:
            return
        call.stream_done = self._clock()
        self._provider_total += call.stream_done - call.request_started

    def record_retry(self, category: str) -> None:
        """Count one engine-initiated model-call retry of ``category``."""

        self.retries[category] = self.retries.get(category, 0) + 1

    # ── tool batches and approvals ──────────────────────────────────────

    async def tool_batch(self, stream: AsyncIterator[T], *, calls: int) -> AsyncIterator[T]:
        """Iterate one tool batch's event stream, timing the batch."""

        batch = ToolBatchTiming(started=self._clock(), calls=calls)
        self._tool_batch_count += 1
        self._tool_call_count += calls
        if len(self.tool_batches) < MAX_DETAIL_ENTRIES:
            self.tool_batches.append(batch)
        self._open_batch = batch
        try:
            async for event in stream:
                yield event
        finally:
            if batch.ended is None:
                batch.ended = self._clock()
                self._tool_total += batch.ended - batch.started
            if self._open_batch is batch:
                self._open_batch = None

    def timed_approval(
        self, wait: Callable[..., Awaitable[T]] | None
    ) -> Callable[..., Awaitable[T]] | None:
        """Wrap an approval-wait callable so its wall time is recorded.

        Concurrent waits are counted once: approval wait time is the time
        during which at least one approval was outstanding.
        """

        if wait is None:
            return None

        async def _wait(*args: Any, **kwargs: Any) -> T:
            self._approval_enter()
            try:
                return await wait(*args, **kwargs)
            finally:
                self._approval_exit()

        return _wait

    def _approval_enter(self) -> None:
        if self._approval_active == 0:
            self._approval_since = self._clock()
        self._approval_active += 1

    def _approval_exit(self) -> None:
        self._approval_active = max(0, self._approval_active - 1)
        if self._approval_active == 0 and self._approval_since is not None:
            waited = self._clock() - self._approval_since
            self._approval_since = None
            self._approval_total += waited
            if self._open_batch is not None:
                self._open_batch.approval_wait += waited

    # ── output ──────────────────────────────────────────────────────────

    def summary(self) -> dict[str, Any]:
        """JSON-ready breakdown, integer milliseconds."""

        now = self._clock()
        start = self.started_at
        pre_provider = (
            self._first_request_at - start if self._first_request_at is not None else (now - start)
        )
        approval_total = self._approval_total
        if self._approval_since is not None:
            approval_total += now - self._approval_since
        return {
            "total_ms": _ms(now - start),
            "pre_provider_ms": _ms(pre_provider),
            "first_text_ms": (
                _ms(self._first_text_at - start) if self._first_text_at is not None else None
            ),
            "provider_ms": _ms(self._provider_total),
            "tool_ms": _ms(self._tool_total),
            "approval_wait_ms": _ms(approval_total),
            "finalize_ms": _ms(self.stages.get("turn_finalizer", 0.0)),
            "iterations": self._provider_call_count,
            "tool_batches": self._tool_batch_count,
            "tool_calls": self._tool_call_count,
            "stages": {name: _ms(self.stages[name]) for name in STAGE_NAMES if name in self.stages},
            "retries": dict(self.retries),
            "provider_calls": [self._call_dict(c) for c in self.provider_calls],
            "tool_batch_detail": [
                {
                    "started_ms": _ms(b.started - start),
                    "duration_ms": _ms((b.ended if b.ended is not None else now) - b.started),
                    "calls": b.calls,
                    "approval_wait_ms": _ms(b.approval_wait),
                }
                for b in self.tool_batches
            ],
        }

    def _call_dict(self, call: ProviderCallTiming) -> dict[str, Any]:
        def since_request(mark: float | None) -> int | None:
            return None if mark is None else _ms(mark - call.request_started)

        return {
            "request_started_ms": _ms(call.request_started - self.started_at),
            "first_event_ms": since_request(call.first_event),
            "first_text_ms": since_request(call.first_text),
            "stream_done_ms": since_request(call.stream_done),
            "input_tokens": call.input_tokens,
            "output_tokens": call.output_tokens,
        }

    def log_fields(self) -> dict[str, Any]:
        """Flat key/value form of :meth:`summary` for the ``turn.timing`` line."""

        summary = self.summary()
        flat = {
            key: value
            for key, value in summary.items()
            if key not in {"stages", "provider_calls", "tool_batch_detail", "retries"}
        }
        for name, ms in summary["stages"].items():
            flat[f"stage_{name}_ms"] = ms
        for category, count in summary["retries"].items():
            flat[f"retry_{category}"] = count
        first_call = summary["provider_calls"][0] if summary["provider_calls"] else None
        if first_call is not None:
            flat["first_call_first_event_ms"] = first_call["first_event_ms"]
            flat["first_call_first_text_ms"] = first_call["first_text_ms"]
        return flat


class _NullTurnTiming(TurnTiming):
    """Records nothing; lets Agent call sites stay unconditional."""

    async def provider_stream(self, stream: AsyncIterator[Any]) -> AsyncIterator[Any]:
        async for event in stream:
            yield event

    async def tool_batch(self, stream: AsyncIterator[T], *, calls: int) -> AsyncIterator[T]:
        async for event in stream:
            yield event

    def timed_approval(
        self, wait: Callable[..., Awaitable[T]] | None
    ) -> Callable[..., Awaitable[T]] | None:
        return wait

    def record_retry(self, category: str) -> None:
        return None


NULL_TURN_TIMING: TurnTiming = _NullTurnTiming()


def emit_turn_timing_log(timing: TurnTiming, **context: Any) -> None:
    """Log the ``turn.timing`` line once per recorder; never raises."""

    if timing.logged:
        return
    timing.logged = True
    try:
        log.info("turn.timing", **context, **timing.log_fields())
    except Exception as exc:  # pragma: no cover - observability must not break turns
        log.debug("turn.timing_failed", error=str(exc))


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
