"""TurnRunner stage that finalizes a turn after the agent stream ends.

Drives the post-stream side effects between the "flush remaining text"
edge and the ``turn_call_logger.write("turn_end", ...)`` boundary:
heartbeat normalize, transcript ``append_message`` for the assistant
turn, ``_capture_turn_memory`` invocation, ``_persist_turn_error`` for
any pending error, and the ``Session.update(...)`` session-totals
rollup driven off the ``DoneEvent`` snapshot.

Returns ``StageOutcome[TurnFinalizerStageOutput]`` -- not a generator.
The agent stream has exhausted by the time this stage runs; the four
upstream accumulators are fully materialized. The stage emits no
``AgentEvent``s during its body. The ``pending_error_event`` is
surfaced in the stage output after its trace + decision-entry emit.

Side-effect order (load-bearing):

1. Heartbeat-normalize the accumulated text.
2. Transcript ``append_message`` (assistant turn) -- when
   ``(final_text or segments or artifacts)`` and a session manager is
   wired through the port.
3. ``capture_turn_memory`` -- wrapped in log-and-continue try/except.
4. ``persist_turn_error`` -- only when ``error_message`` is truthy.
   The helper owns its own internal try/except.
5. Session totals rollup -- wrapped in log-and-continue try/except,
   only when a DoneEvent is present.

Memory-after-transcript pairing is required (memory reads the
persisted ``final_text``). Errors are persisted before totals are
rolled up so the recorded cause is visible even when totals fail.

No ``TurnHook.after_turn`` fan-out today.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog

if TYPE_CHECKING:
    from agentos.engine.turn_runner.outcome import StageOutcome
    from agentos.engine.types import DoneEvent, ErrorEvent
    from agentos.tools.types import ToolContext

log = structlog.get_logger(__name__)

_UNCONFIRMED_BACKGROUND_TOOL_NAMES = frozenset({"background_process", "process"})

# Turn kinds whose reply belongs to the harness rather than the conversation.
# Their output must not be persisted: it would show up as unexplained
# assistant lines in the user's transcript and then be replayed as context.
_INTERNAL_TURN_KINDS = frozenset({"memory_nudge"})


def _is_internal_turn(run_kind: str | None) -> bool:
    return bool(run_kind) and run_kind in _INTERNAL_TURN_KINDS


def _unconfirmed_background_tool_names(turn_segments: list[dict]) -> list[str]:
    names: list[str] = []
    for segment in turn_segments:
        if not isinstance(segment, dict) or segment.get("type") != "tool_result":
            continue
        name = segment.get("name")
        if not isinstance(name, str):
            continue
        if name not in _UNCONFIRMED_BACKGROUND_TOOL_NAMES:
            continue
        execution_status = segment.get("execution_status")
        if not isinstance(execution_status, dict):
            continue
        if (
            execution_status.get("status") == "unknown"
            and execution_status.get("reason") == "background_running"
        ):
            names.append(name)
    return names


def _with_unconfirmed_action_notice(final_text: str, turn_segments: list[dict]) -> str:
    tool_names = _unconfirmed_background_tool_names(turn_segments)
    if not tool_names:
        return final_text
    if "could not confirm" in final_text.lower():
        return final_text
    tools = ", ".join(dict.fromkeys(tool_names))
    notice = (
        f"Note: I started {tools}, but the tool reported that it was still "
        "running, so I could not confirm the action completed."
    )
    if final_text.strip():
        return f"{final_text.rstrip()}\n\n{notice}"
    return notice


# ---------------------------------------------------------------------------
# Ports -- four narrow Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class TranscriptAppendPort(Protocol):
    """Persist the assistant turn via ``SessionManager.append_message(...)``.

    The port folds the ``accepts_keyword_arg(..., "token_count")``
    introspection so the stage body has no ``inspect`` dependency, and the
    ``session_manager is None`` guard so the stage body has no conditional
    on manager presence. Returns ``True`` if the append fired, ``False``
    when the port declined (no manager configured).

    *message_id* is the row's identity, fixed by the turn before the first
    attempt; an append with an identity the session already holds stores
    nothing and still reports ``True`` (the reply is stored).

    Exceptions propagate to the harness's terminal error handler -- no
    try/except wraps ``append_message``.
    """

    async def append_message(
        self,
        session_key: str,
        *,
        role: str,
        content: str,
        tool_calls: list[Any] | None,
        reasoning_content: str | None,
        turn_usage: dict[str, Any] | None,
        token_count: int | None,
        provider_messages: dict[str, Any] | None = None,
        replaces_message_id: str | None = None,
        message_id: str | None = None,
    ) -> bool: ...


@runtime_checkable
class TurnMemoryCapturePort(Protocol):
    """Wrap ``TurnRunner._capture_turn_memory(...)``.

    The call is wrapped in a log-and-continue try/except inside the
    stage body so the error-handling contract is visible. The port
    forwards verbatim without swallowing.
    """

    async def capture_turn(
        self,
        *,
        agent_id: str,
        session_key: str,
        runtime_message: str,
        final_text: str,
        input_mode: str,
        tool_context: ToolContext | None,
        input_provenance: dict[str, Any] | None,
        run_kind: str,
        no_memory_capture: bool,
    ) -> None: ...


@runtime_checkable
class MemoryNudgePort(Protocol):
    """Wrap ``TurnRunner.note_turn_for_memory_nudge(...)``.

    Synchronous by design: the port only advances a counter and, when a
    review is due, schedules it as a background task. Nothing here awaits the
    review itself, so a slow review cannot hold the turn open.
    """

    def note_turn(
        self,
        *,
        agent_id: str,
        session_key: str,
        input_mode: str,
        run_kind: str,
        no_memory_capture: bool,
        turn_segments: Any,
    ) -> None: ...


@runtime_checkable
class ProactiveCompactionPort(Protocol):
    """Schedule a between-turns compaction when the history is near the trigger.

    Synchronous and cheap; the job runs after the turn has ended
    (:class:`agentos.engine.proactive_compaction.ProactiveCompactionScheduler`).
    """

    def after_turn(self, *, session_key: str, agent_id: str, succeeded: bool) -> None: ...


class _NoopMemoryNudge:
    """Stand-in used when no nudge port is wired. Never nudges."""

    def note_turn(self, **_kwargs: Any) -> None:
        return None


def _turn_usage_payload(
    done_event: Any | None,
    *,
    resolved_model: str | None,
) -> dict[str, Any] | None:
    if done_event is None:
        return None
    model = done_event.model or resolved_model or ""
    return {
        "input_tokens": int(done_event.input_tokens or 0),
        "output_tokens": int(done_event.output_tokens or 0),
        "reasoning_tokens": int(done_event.reasoning_tokens or 0),
        "cached_tokens": int(done_event.cached_tokens or 0),
        "cache_write_tokens": int(done_event.cache_write_tokens or 0),
        "cost_usd": float(done_event.cost_usd or 0.0),
        "billed_cost": float(done_event.billed_cost or 0.0),
        "cost_source": done_event.cost_source or "none",
        "model": model,
        "routed_model": done_event.routed_model or "",
        "routed_tier": done_event.routed_tier or None,
        "routing_source": done_event.routing_source or "none",
        "routing_confidence": float(done_event.routing_confidence or 0.0),
        "routing_applied": bool(getattr(done_event, "routing_applied", True)),
        "rollout_phase": getattr(done_event, "rollout_phase", "full") or "full",
        "baseline_model": done_event.baseline_model or "",
        "savings_pct": float(done_event.savings_pct or 0.0),
        "savings_usd": float(done_event.savings_usd or 0.0),
        "cache_hit_active": bool(done_event.cache_hit_active),
        "total_savings_pct": float(done_event.total_savings_pct or 0.0),
        "total_savings_usd": float(done_event.total_savings_usd or 0.0),
    }


@runtime_checkable
class SessionTotalsPort(Protocol):
    """Roll up session token + cost + cache totals from a DoneEvent.

    Wraps the entire post-DoneEvent block: the
    ``get_session`` read, ``normalize_event_cost_source`` call, the
    four ``next_*`` accumulator computations, the ``rollup_cost_source``
    call, and the ``Session.update`` write. The port folds the
    ``session_manager is None`` guard and the ``current_session is None``
    early-return so the stage body has no conditional on manager
    presence.

    Returns ``CostRollupResult | None``. ``None`` when the port
    declined (no session manager or no current session row); a populated
    snapshot otherwise so the equivalence harness can pin the
    post-rollup ``Session`` row across modes.
    """

    async def rollup(
        self,
        *,
        session_key: str,
        done_event: DoneEvent,
        resolved_model: str,
    ) -> CostRollupResult | None: ...


@runtime_checkable
class TurnErrorPersistPort(Protocol):
    """Wrap ``TurnRunner._persist_turn_error(session_key, event)``.

    The helper owns its own log-and-continue try/except; the
    port forwards verbatim. The helper guards
    ``session_manager is None`` AND ``event is None`` internally -- the
    stage body has no None checks.
    """

    async def persist_error(
        self,
        *,
        session_key: str,
        event: ErrorEvent | None,
    ) -> None: ...


# ---------------------------------------------------------------------------
# Cost-rollup result -- exposed for equivalence-harness pinning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CostRollupResult:
    """Snapshot of the per-turn session-totals update.

    Exposed so the equivalence harness can pin the post-rollup
    ``Session`` row. Not consumed by
    ``TurnContext`` or any downstream stage directly.
    """

    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost_usd: float
    total_cost_usd: float
    billed_cost_usd: float
    estimated_cost_component_usd: float
    cost_source: str
    missing_cost_entries: int
    cache_read: int
    cache_write: int
    model_override: str | None


# ---------------------------------------------------------------------------
# Stage I/O dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TurnFinalizerStageInput:
    """Inputs the TurnFinalizerStage needs at its boundary.

    Pulled from ``TurnContext`` accumulators by the harness plus the
    post-stream ``_run_turn``-body locals (the four
    ``stream_*`` mirrors plus the original ``runtime_message`` /
    ``input_mode`` / ``input_provenance``).
    """

    # From StreamConsumerStage
    final_text_parts: list[str]
    turn_segments: list[dict]
    turn_artifacts: list[dict[str, Any]]
    error_message: str | None
    pending_error_event: ErrorEvent | None
    done_event: DoneEvent | None

    # From InputStage -- the ORIGINAL runtime_message (used by
    # ``_capture_turn_memory`` for memory provenance), NOT the effective
    # post-pipeline string.
    runtime_message: str
    input_mode: str
    input_provenance: dict[str, Any] | None

    # From PromptAssemblerStage
    resolved_model: str

    # From AgentBootstrapStage
    agent_id: str

    # From _run_turn locals
    session_key: str
    tool_context: ToolContext | None
    run_kind: str
    heartbeat_ack_max_chars: int
    no_memory_capture: bool

    # From the Agent: the structured provider-message record of the turn
    # (``agentos.engine.transcript``), persisted next to the flat segments.
    provider_messages: dict[str, Any] | None = None

    # The turn's in-progress row (``agentos.engine.turn_progress``), if one
    # was written; the final row replaces it in the same transaction.
    replaces_message_id: str | None = None

    # Called right after the assistant turn was appended, before memory
    # capture and the totals rollup are awaited. The harness records there
    # that the reply is durable, so a cancel landing later in this stage (or
    # during the append itself) does not persist the turn a second time.
    on_transcript_appended: Callable[[], None] | None = None

    # The turn's reply write, shared with the harness's cancel path (the
    # row's identity and the stop budget). ``None``: a write of its own.
    reply_persistence: ReplyPersistence | None = None


@dataclass(frozen=True)
class TurnFinalizerStageOutput:
    """Outputs the harness applies to ``TurnContext`` after the stage runs.

    Downstream consumers read ``final_text``, ``turn_segments``,
     ``turn_artifacts``, ``error_message``, ``pending_error_event``,
     ``done_event`` for its turn_end trace + decision entry. The
     ``cost_rollup`` snapshot is observability-only (pinned by the
     equivalence harness, not consumed downstream).
    """

    # Heartbeat-normalized final text (the harness reads it for the
    # turn_end trace event and the decision entry).
    final_text: str
    # ``turn_segments`` may be EMPTIED by the heartbeat-empty edge; the
    # stage returns the post-empty value.
    turn_segments: list[dict]
    # Re-exposed unchanged for the turn_end payload.
    turn_artifacts: list[dict[str, Any]]
    error_message: str | None
    pending_error_event: ErrorEvent | None
    done_event: DoneEvent | None
    # Observability snapshot -- None when no DoneEvent or
    # SessionTotalsPort returned None.
    cost_rollup: CostRollupResult | None
    # Did the assistant turn actually persist?
    transcript_appended: bool
    # Did the memory capture fire?
    memory_captured: bool


@dataclass(frozen=True)
class PersistedReply:
    """What :meth:`TurnFinalizerStage.persist_reply` wrote (steps 1-2)."""

    final_text: str
    turn_segments: list[dict]
    transcript_appended: bool


# How long a stopped turn may spend storing its reply, counted once per turn
# from the stop (the cooperative abort request when there was one, else the
# cancel itself).
_APPEND_CANCEL_GRACE_S = 5.0


class ReplyPersistence:
    """The one write of a turn's reply row, shared by every path that stores it.

    A turn stores its reply from the finalizer (the normal path) or, when a
    cancel cuts in, from the harness's cancel path (a finished reply the
    finalizer did not reach, or a stopped turn's partial). Both go through
    one instance, which gives a stop two guarantees:

    * At most once, by construction. The row's identity (:attr:`message_id`)
      is fixed before the first attempt and the store's append is idempotent
      on it (``SessionManager.append_message(message_id=...)``), so neither a
      retry nor an abandoned attempt that committed anyway can leave a second
      row. And only one attempt runs at a time: a later path waits on the
      write already in flight instead of starting its own.
    * Bounded. Once a cancel reached the turn (:meth:`note_cancel`) the write
      has until ``_APPEND_CANCEL_GRACE_S`` after the stop -- one budget for
      the whole turn, whichever path waits -- and a further cancel ends the
      wait at once. A write still running then is abandoned: the row is
      absent or present once, and :attr:`stored` stays ``False`` so the turn
      does not claim a reply it could not confirm.
    """

    def __init__(self, message_id: str | None = None) -> None:
        #: The reply row's identity (``None``: the store picks one, no
        #: idempotency -- test doubles and bare stage runs).
        self.message_id = message_id
        #: The write finished within the bounds and reported the row stored.
        self.stored = False
        #: The finalizer began the session-totals rollup for this turn.
        self.totals_started = False
        self._write: asyncio.Future[bool] | None = None
        self._factory: Callable[[], Awaitable[bool]] | None = None
        self._on_stored: Callable[[], None] | None = None
        self._deadline: float | None = None
        self._stopped = False

    @property
    def attempted(self) -> bool:
        """A write of the reply was started (it may be running or over)."""
        return self._write is not None

    @property
    def cancelled(self) -> bool:
        """A cancel reached the turn: waits from now on are bounded."""
        return self._deadline is not None

    def remaining(self) -> float:
        """Seconds left of the stop budget (unbounded before any cancel)."""
        if self._deadline is None:
            return float("inf")
        if self._stopped:
            return 0.0
        return max(0.0, self._deadline - time.monotonic())

    def note_cancel(self) -> None:
        """A cancel reached the turn: start the stop budget (once)."""
        if self._deadline is not None:
            return
        from agentos.engine.turn_progress import current_turn_stop_signal

        now = time.monotonic()
        start = now
        signal = current_turn_stop_signal()
        aborted_at = getattr(signal, "aborted_at", None)
        if signal is not None and not signal.shutdown and isinstance(aborted_at, float):
            # A cooperative stop came first: the task is cancelled only once
            # its own grace ran out, and that time counts against this budget.
            start = min(now, aborted_at)
        self._deadline = start + _APPEND_CANCEL_GRACE_S

    async def write(
        self,
        factory: Callable[[], Awaitable[bool]] | None = None,
        *,
        on_stored: Callable[[], None] | None = None,
    ) -> bool:
        """Store the reply through *factory*, once; report whether it is stored.

        Starts the write unless one is in flight or already succeeded (a
        failed one is retried with the same identity while the budget lasts).
        Before any cancel the write is awaited to its end and its exception,
        if any, propagates. A cancel that arrives meanwhile waits for the
        write within the budget and then propagates. Called after a cancel
        (the harness's cancel path), it only waits within what is left of
        the budget, and returns.
        """
        if self.stored:
            return True
        task = self._write
        if task is None or self._failed(task):
            factory = factory or self._factory
            if factory is None or self.remaining() <= 0:
                return False
            task = self._start(factory, on_stored)
        if self.cancelled:
            await self._wait(task)
            return self._collect(task)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done() and task.cancelled():
                raise
            self.note_cancel()
            await self._wait(task)
            self._collect(task)
            raise
        return self._collect(task)

    async def bounded(self, factory: Callable[[], Awaitable[Any]], *, what: str) -> bool:
        """Run another step of a cancelled turn within what is left of the budget."""
        if self.remaining() <= 0:
            return False
        task = asyncio.ensure_future(factory())
        await self._wait(task, what=what)
        if task.done() and not task.cancelled():
            task.exception()  # retrieved: the step logs its own failures
            return True
        return False

    def _start(
        self, factory: Callable[[], Awaitable[bool]], on_stored: Callable[[], None] | None
    ) -> asyncio.Future[bool]:
        self._factory = factory
        if on_stored is not None:
            self._on_stored = on_stored
        self._write = asyncio.ensure_future(factory())
        return self._write

    @staticmethod
    def _failed(task: asyncio.Future[Any]) -> bool:
        return task.done() and not task.cancelled() and task.exception() is not None

    async def _wait(self, task: asyncio.Future[Any], *, what: str = "reply_write") -> None:
        """Wait for *task* until the budget runs out or another cancel arrives."""
        while not task.done():
            remaining = self.remaining()
            if remaining <= 0:
                break
            try:
                await asyncio.wait({task}, timeout=None if remaining == float("inf") else remaining)
            except asyncio.CancelledError:
                # The user insisted (a second stop): end the wait now.
                self._stopped = True
        if not task.done():
            task.cancel()
            log.warning(
                "turn_runner.cancelled_write_abandoned",
                step=what,
                message_id=self.message_id,
                second_stop=self._stopped,
            )

    def _collect(self, task: asyncio.Future[bool]) -> bool:
        if not task.done() or task.cancelled():
            return False
        exc = task.exception()
        if exc is not None:
            log.warning(
                "turn_runner.cancelled_write_failed",
                message_id=self.message_id,
                error=str(exc),
            )
            return False
        if task.result() and not self.stored:
            self.stored = True
            if self._on_stored is not None:
                self._on_stored()
        return self.stored


# ---------------------------------------------------------------------------
# Outer stage class
# ---------------------------------------------------------------------------


class TurnFinalizerStage:
    """Persist the assistant turn, capture memory, roll up session totals.

     Stable boundary: runs ONCE per turn, after StreamConsumerStage
     exhausts (and after the harness flushes the trailing text segment).
     The ports execute in this order:

     1. Heartbeat-normalize the accumulated text.
     2. ``TranscriptAppendPort.append_message`` (assistant turn).
     3. ``TurnMemoryCapturePort.capture_turn`` (memory write -- wrapped
        in log-and-continue try/except intentional).
     4. ``TurnErrorPersistPort.persist_error`` (pending error, only if
        ``error_message`` is truthy).
     5. ``SessionTotalsPort.rollup`` (DoneEvent-driven session.update --
        wrapped in log-and-continue try/except intentional).

     The order is load-bearing: transcript persistence MUST precede
     memory capture (memory capture reads ``final_text`` AS PERSISTED);
     error persist MUST precede totals rollup for diagnostic ordering
     that downstream observability relies on.

     Exception model: the stage does NOT wrap the ``append_message``
     call. Any exception there propagates to the harness's terminal error
     handler. A cancel does not interrupt the append half-way: it waits
     for the write within the stop budget (:class:`ReplyPersistence`),
     reports it through ``on_transcript_appended``, then propagates. The
     memory-capture and totals-rollup ports each have their own
     log-and-continue try/except inside the stage body.

     No ``TurnHook.after_turn`` fan-out today; that wiring belongs in a separate
     production hook pass.
    """

    name = "turn_finalizer_stage"

    def __init__(
        self,
        *,
        transcript_append: TranscriptAppendPort,
        turn_memory_capture: TurnMemoryCapturePort,
        session_totals: SessionTotalsPort,
        turn_error_persist: TurnErrorPersistPort,
        memory_nudge: MemoryNudgePort | None = None,
        proactive_compaction: ProactiveCompactionPort | None = None,
    ) -> None:
        self._proactive_compaction = proactive_compaction
        self._transcript_append = transcript_append
        self._turn_memory_capture = turn_memory_capture
        self._session_totals = session_totals
        self._turn_error_persist = turn_error_persist
        # Optional so existing constructions (tests, older wiring) keep
        # working; a stage without it simply never nudges.
        self._memory_nudge = memory_nudge or _NoopMemoryNudge()

    async def persist_reply(self, inp: TurnFinalizerStageInput) -> PersistedReply:
        """Steps 1-2: normalize the reply and append it to the transcript.

        :meth:`run` starts with this. The harness also calls it on its own
        for a turn whose whole reply was produced (its DoneEvent already
        went out) but which a stop cancelled before :meth:`run` reached the
        append, so that reply is persisted as finished, exactly once.
        """
        # Late imports keep the module import-cycle-free.
        import json as _json

        from agentos.engine.runtime import _normalize_heartbeat_text

        # 1. Heartbeat-normalize.
        final_text = "".join(inp.final_text_parts)
        original_final_text = final_text
        final_text = _normalize_heartbeat_text(
            final_text,
            run_kind=inp.run_kind,
            heartbeat_ack_max_chars=inp.heartbeat_ack_max_chars,
        )
        turn_segments = inp.turn_segments
        if (
            original_final_text
            and not final_text
            and turn_segments
            and all(
                isinstance(segment, dict) and segment.get("type") == "text"
                for segment in turn_segments
            )
        ):
            turn_segments = []

        final_text = _with_unconfirmed_action_notice(final_text, turn_segments)

        transcript_appended = False

        # 2. Transcript append.
        #
        # Internal turns are excluded: their reply is addressed to the
        # harness, not the user. Persisting it would leave stray lines like
        # "Nothing to save." in the conversation and, worse, feed them back
        # as context on the next real turn.
        if (final_text or turn_segments or inp.turn_artifacts) and not _is_internal_turn(
            inp.run_kind
        ):
            persisted_content = (
                _json.dumps(
                    {"text": final_text, "artifacts": inp.turn_artifacts},
                    ensure_ascii=False,
                )
                if inp.turn_artifacts
                else final_text
            )
            # Persist reasoning for every provider: the WebUI renders it from
            # history (fetch-on-expand). Whether it is REPLAYED to a provider on
            # later turns stays a per-provider decision
            # (``_should_replay_reasoning_content``), not a persistence one.
            reasoning_content: str | None = None
            if inp.done_event is not None and inp.done_event.reasoning_content:
                reasoning_content = inp.done_event.reasoning_content
            token_count = inp.done_event.output_tokens if inp.done_event is not None else None
            append_kwargs: dict[str, Any] = {}
            if inp.provider_messages is not None:
                append_kwargs["provider_messages"] = inp.provider_messages
            if inp.replaces_message_id is not None:
                append_kwargs["replaces_message_id"] = inp.replaces_message_id
            persistence = inp.reply_persistence or ReplyPersistence()
            if persistence.message_id is not None:
                append_kwargs["message_id"] = persistence.message_id
            port, session_key = self._transcript_append, inp.session_key
            turn_usage = _turn_usage_payload(inp.done_event, resolved_model=inp.resolved_model)
            tool_calls = turn_segments if turn_segments else None

            def append() -> Awaitable[bool]:
                return port.append_message(
                    session_key,
                    role="assistant",
                    content=persisted_content,
                    tool_calls=tool_calls,
                    reasoning_content=reasoning_content,
                    turn_usage=turn_usage,
                    token_count=token_count,
                    **append_kwargs,
                )

            transcript_appended = await persistence.write(
                append, on_stored=inp.on_transcript_appended
            )

        return PersistedReply(
            final_text=final_text,
            turn_segments=turn_segments,
            transcript_appended=transcript_appended,
        )

    async def rollup_totals(self, inp: TurnFinalizerStageInput) -> CostRollupResult | None:
        """Step 5: roll the turn's usage into the session totals (log-and-continue).

        :meth:`run` ends with this. The harness also calls it for a stopped
        turn whose reply was stored before the stop cut the stage short, so
        a completed turn's tokens reach the session totals once.
        """
        if inp.done_event is None:
            return None
        if inp.reply_persistence is not None:
            inp.reply_persistence.totals_started = True
        try:
            return await self._session_totals.rollup(
                session_key=inp.session_key,
                done_event=inp.done_event,
                resolved_model=inp.resolved_model,
            )
        except Exception as exc:  # noqa: BLE001 - log-and-continue intentional
            log.warning(
                "turn_runner.session_usage_persist_failed",
                session_key=inp.session_key,
                error=str(exc),
            )
            return None

    async def run(
        self,
        inp: TurnFinalizerStageInput,
    ) -> StageOutcome[TurnFinalizerStageOutput]:
        from agentos.engine.turn_runner.outcome import StageOutcome

        reply = await self.persist_reply(inp)
        final_text, turn_segments = reply.final_text, reply.turn_segments
        transcript_appended = reply.transcript_appended
        memory_captured = False

        # 3. Memory capture: only once the reply persisted (it reads the text
        # as persisted).
        if transcript_appended:
            try:
                await self._turn_memory_capture.capture_turn(
                    agent_id=inp.agent_id,
                    session_key=inp.session_key,
                    runtime_message=inp.runtime_message,
                    final_text=final_text,
                    input_mode=inp.input_mode,
                    tool_context=inp.tool_context,
                    input_provenance=inp.input_provenance,
                    run_kind=inp.run_kind,
                    no_memory_capture=inp.no_memory_capture,
                )
                memory_captured = True
            except Exception as exc:  # noqa: BLE001 - log-and-continue intentional
                log.warning(
                    "turn_runner.capture_failed",
                    session_key=inp.session_key,
                    agent_id=inp.agent_id,
                    error=str(exc),
                )

        # Periodic memory review. Deliberately outside the transcript-append
        # branch: how many turns the user has taken is independent of whether
        # this deployment persists transcripts, and nesting it there meant the
        # counter never advanced wherever append returns False (no session
        # manager wired), so the nudge silently never fired.
        #
        # Fire-and-forget: the reply is already on the wire, and a review must
        # never hold the turn open -- a slow one would leave the task marked
        # running for its whole duration.
        try:
            self._memory_nudge.note_turn(
                agent_id=inp.agent_id,
                session_key=inp.session_key,
                input_mode=inp.input_mode,
                run_kind=inp.run_kind,
                no_memory_capture=inp.no_memory_capture,
                turn_segments=turn_segments,
            )
        except Exception as exc:  # noqa: BLE001 - log-and-continue intentional
            log.warning(
                "turn_runner.memory_nudge_failed",
                session_key=inp.session_key,
                agent_id=inp.agent_id,
                error=str(exc),
            )

        # 4. Error persist (only when error_message is truthy; the
        # port folds the session-manager-None guard, and the helper
        # also guards event-is-None internally).
        if inp.error_message:
            await self._turn_error_persist.persist_error(
                session_key=inp.session_key,
                event=inp.pending_error_event,
            )

        # 5. Session totals rollup (only when DoneEvent present; the
        # port folds the session-manager-None and
        # current_session-None guards).
        cost_rollup = await self.rollup_totals(inp)

        # --- proactive compaction (stream C) --------------------------------
        # Last step of the success path: the turn is persisted, so a
        # between-turns compaction may be scheduled; it starts after the turn.
        if self._proactive_compaction is not None:
            try:
                self._proactive_compaction.after_turn(
                    session_key=inp.session_key,
                    agent_id=inp.agent_id,
                    succeeded=transcript_appended and not inp.error_message,
                )
            except Exception as exc:  # noqa: BLE001 - log-and-continue intentional
                log.warning(
                    "compaction.proactive_schedule_failed",
                    session_key=inp.session_key,
                    error=str(exc),
                )
        # --- end proactive compaction ----------------------------------------

        return StageOutcome.success(
            TurnFinalizerStageOutput(
                final_text=final_text,
                turn_segments=turn_segments,
                turn_artifacts=inp.turn_artifacts,
                error_message=inp.error_message,
                pending_error_event=inp.pending_error_event,
                done_event=inp.done_event,
                cost_rollup=cost_rollup,
                transcript_appended=transcript_appended,
                memory_captured=memory_captured,
            )
        )
