"""Proactive compaction: compact a session between turns, off the critical path.

The in-turn preflight compaction makes the turn that crosses the trigger wait
for the summarizer (up to its timeout) before the first provider request.
This module moves that work between turns: when a turn ends with the
session's history at ``[compaction] proactive_ratio`` x the compaction
trigger or more, one background job per session compacts it with the same
:class:`~agentos.engine.compaction_service.CompactionService` and summarizer
and persists the result the way the in-turn path does (durable checkpoint,
``persist_compaction_result`` anchored on a transcript snapshot, a ``reset``
transcript record that carries the summary exchange, lifecycle
notifications, compaction hooks). The next turn loads the compacted history,
so its preflight check is a no-op.

Concurrency rules:

* a job starts only after the turn that scheduled it has ended
  (:meth:`ProactiveCompactionScheduler.turn_ended`) and the session then
  stayed idle for ``[compaction] proactive_idle_seconds``: a user who is
  still chatting would cancel a summarizer call that is already billed. A
  job cancelled while summarizing doubles that delay for the session (up to
  :data:`MAX_PROACTIVE_IDLE_SECONDS`) until a job runs to completion. A
  process that exits inside the delay (a one-shot CLI run) cancels the job
  before it calls any model;
* every turn bumps the session's generation before it loads history
  (:meth:`ProactiveCompactionScheduler.before_history_load`), under the same
  per-session gate the job holds while it persists. A job whose generation
  is stale persists nothing -- the next turn end checks again -- so a turn
  never runs on a history that is rewritten underneath it. A job that has
  already persisted is never cancelled (only its notifications remain);
* the job persists while holding the runtime's session lock, the lock
  transcript writers take, so no row is appended in the middle of the
  rewrite;
* subagent and cron sessions (:func:`is_ephemeral_session`) are short-lived
  and compacted in memory only; they get no proactive job.

Preflight compaction stays the safety net for whatever this misses.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from agentos.engine.compaction_service import (
    CompactionResult,
    CompactionService,
    auxiliary_chunk_summarizer,
    is_ephemeral_session,
    is_summary_pair,
    summary_messages,
    trigger_limit_tokens,
)
from agentos.engine.estimate_calibration import EstimateCalibration

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentos.provider.types import Message

log = structlog.get_logger(__name__)

PROACTIVE_PHASE = "proactive_background"
DEFAULT_PROACTIVE_RATIO = 0.70
DEFAULT_PROACTIVE_IDLE_SECONDS = 20.0
MAX_PROACTIVE_IDLE_SECONDS = 300.0

# Job phases, as seen by ``before_history_load``.
_WAITING = "waiting"  # idle delay: cancelling costs nothing
_SUMMARIZING = "summarizing"  # a summarizer call may be in flight (billed)
_PERSISTED = "persisted"  # passed the generation check under the gate


@dataclass
class _Pending:
    agent: Any
    agent_id: str
    generation: int
    estimated_tokens: int
    limit_tokens: int


@dataclass
class _SessionState:
    generation: int = 0
    agent: Any = None
    agent_id: str = "main"
    pending: _Pending | None = None
    task: asyncio.Task[None] | None = None
    phase: str = _WAITING
    gate: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(frozen=True)
class _TurnView:
    """The finished turn as the compaction service needs it."""

    messages: list[Message]
    turn_input_index: int
    overhead_tokens: int
    scale: float


def _compaction_settings(config: Any) -> Any:
    return getattr(config, "compaction", None)


def proactive_enabled(config: Any) -> bool:
    return bool(getattr(_compaction_settings(config), "proactive", True))


def proactive_ratio(config: Any) -> float:
    raw = getattr(_compaction_settings(config), "proactive_ratio", DEFAULT_PROACTIVE_RATIO)
    try:
        ratio = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_PROACTIVE_RATIO
    if ratio <= 0.0 or ratio > 1.0:
        return DEFAULT_PROACTIVE_RATIO
    return ratio


def proactive_idle_seconds(config: Any) -> float:
    raw = getattr(
        _compaction_settings(config), "proactive_idle_seconds", DEFAULT_PROACTIVE_IDLE_SECONDS
    )
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_PROACTIVE_IDLE_SECONDS
    if seconds < 0.0 or seconds != seconds:  # negative or NaN
        return DEFAULT_PROACTIVE_IDLE_SECONDS
    return min(seconds, MAX_PROACTIVE_IDLE_SECONDS)


def calibration_enabled(config: Any) -> bool:
    return bool(getattr(_compaction_settings(config), "estimate_calibration", True))


def _turn_view(agent: Any) -> _TurnView | None:
    """Messages, current-turn start and request overhead of a finished turn."""
    history = list(getattr(agent, "_history", None) or [])
    overhead = getattr(agent, "_last_request_overhead_tokens", None)
    if not history or overhead is None:
        return None
    # Same rule as ``Agent._cap_turn_for_history``: a turn that compacted
    # starts right after its summary exchange.
    start = 2 if getattr(agent, "_turn_reset", False) else int(getattr(agent, "_turn_base_len", 0))
    if start <= 0 or start >= len(history):
        return None
    scale_of = getattr(agent, "estimate_scale", None)
    scale = float(scale_of()) if callable(scale_of) else 1.0
    return _TurnView(
        messages=history,
        turn_input_index=start,
        overhead_tokens=int(overhead),
        scale=scale,
    )


def _scaled(message_tokens: Callable[[Sequence[Message]], int], scale: float) -> Any:
    if scale == 1.0:
        return message_tokens
    return lambda items: int(message_tokens(items) * scale)


def _trigger_limit(agent: Any) -> int:
    budget_of = getattr(agent, "_compaction_budget_tokens", None)
    budget = budget_of() if callable(budget_of) else None
    return int(
        trigger_limit_tokens(
            int(agent.config.context_window_tokens),
            float(agent.config.context_overflow_threshold),
            budget,
        )
    )


def _compaction_service(agent: Any, session_key: str) -> CompactionService:
    """The agent's own compaction service, reporting the proactive phase."""
    hint = getattr(agent.config, "compaction_summary_model_hint", None) or ("", "")
    return CompactionService(
        provider=agent.provider,
        ratio=float(agent.config.context_overflow_threshold),
        session_key=session_key,
        chunk_summarizer=auxiliary_chunk_summarizer(
            hint=(str(hint[0] or ""), str(hint[1] or "")),
            session_key=session_key,
        ),
        timeout=float(getattr(agent.config, "compaction_summary_timeout_seconds", 45.0) or 45.0),
        phase=PROACTIVE_PHASE,
    )


class ProactiveCompactionScheduler:
    """One proactive compaction job per session, between turns.

    *runner* is the :class:`~agentos.engine.runtime.TurnRunner`; the
    scheduler reads its config, session manager, session lock and
    compaction hooks.
    """

    def __init__(self, runner: Any) -> None:
        self._runner = runner
        self._sessions: dict[str, _SessionState] = {}
        self._calibration = EstimateCalibration()
        # Per session: how many jobs in a row were cancelled after their
        # summarizer call started. Each one doubles the idle delay.
        self._backoff: dict[str, int] = {}
        # Seam for tests (fake clock); the job's idle wait.
        self._sleep: Callable[[float], Any] = asyncio.sleep

    # -- turn lifecycle --------------------------------------------------

    @property
    def calibration(self) -> EstimateCalibration:
        return self._calibration

    def _enabled(self) -> bool:
        return proactive_enabled(getattr(self._runner, "_config", None))

    async def before_history_load(self, *, session_key: str, agent: Any, agent_id: str) -> None:
        """A turn is about to load *session_key*'s history.

        Bumps the session generation (so a job that has not persisted yet
        never will) and waits for a job that is persisting right now, so the
        load sees either the old or the compacted transcript, whole. Also
        hands the runtime's estimator calibration to the turn's agent.
        """
        if calibration_enabled(getattr(self._runner, "_config", None)):
            try:
                agent.estimate_calibration = self._calibration
            except AttributeError:  # pragma: no cover - agent doubles
                pass
        state = self._sessions.get(session_key)
        if state is None:
            if not self._enabled():
                return
            state = self._sessions[session_key] = _SessionState()
        async with state.gate:
            state.generation += 1
            state.agent = agent
            state.agent_id = agent_id
            state.pending = None
            task = state.task
            phase = state.phase
        if task is None or task.done() or phase == _PERSISTED:
            # Nothing running, or the job already persisted: it only has its
            # notifications and hooks left, which must not be cut short.
            return
        # Still waiting or summarizing: its result could not be persisted any
        # more. A cancelled summarizer call was billed, so back off.
        task.cancel()
        billed = phase == _SUMMARIZING
        if billed:
            self._backoff[session_key] = self._backoff.get(session_key, 0) + 1
        log.info(
            "compaction.proactive",
            session_key=session_key,
            action="skipped",
            reason="turn_started",
            phase=phase,
            next_idle_seconds=self._idle_delay(session_key),
        )

    def _idle_delay(self, session_key: str) -> float:
        """The idle wait before a job for *session_key* calls the summarizer."""
        base = proactive_idle_seconds(getattr(self._runner, "_config", None))
        misses = self._backoff.get(session_key, 0)
        if misses <= 0 or base <= 0.0:
            return base
        return min(MAX_PROACTIVE_IDLE_SECONDS, base * float(1 << min(misses, 16)))

    def after_turn(self, *, session_key: str, agent_id: str, succeeded: bool) -> None:
        """The turn's transcript is persisted: schedule a job when it is due.

        Synchronous and cheap (size estimates only); the job itself starts
        when the turn has ended.
        """
        state = self._sessions.get(session_key)
        if state is None or state.agent is None or not succeeded or not self._enabled():
            return
        if is_ephemeral_session(session_key):
            return
        if state.task is not None and not state.task.done():
            return
        agent = state.agent
        try:
            view = _turn_view(agent)
            if view is None:
                return
            message_tokens = _scaled(agent._message_tokens, view.scale)
            estimated = int(view.overhead_tokens * view.scale) + message_tokens(view.messages)
            limit = _trigger_limit(agent)
        except Exception as exc:  # noqa: BLE001 - a missed schedule is harmless
            log.warning("compaction.proactive_estimate_failed", error=str(exc))
            return
        ratio = proactive_ratio(getattr(self._runner, "_config", None))
        if limit <= 0 or estimated < ratio * limit:
            # Back under the ratio (an in-turn compaction ran): start afresh.
            self._backoff.pop(session_key, None)
            return
        state.pending = _Pending(
            agent=agent,
            agent_id=agent_id,
            generation=state.generation,
            estimated_tokens=estimated,
            limit_tokens=limit,
        )
        log.info(
            "compaction.proactive",
            session_key=session_key,
            action="scheduled",
            estimated_tokens=estimated,
            limit_tokens=limit,
            proactive_ratio=ratio,
            calibration_factor=round(view.scale, 3),
        )

    def turn_ended(self, session_key: str) -> None:
        """The turn (and its session lock) is over: start the pending job."""
        state = self._sessions.get(session_key)
        if state is None:
            return
        state.agent = None
        pending, state.pending = state.pending, None
        if pending is None:
            if state.task is None or state.task.done():
                self._sessions.pop(session_key, None)
            return
        # A fresh context: the job must not inherit the finished turn's
        # session-lock ownership markers (it takes the lock itself).
        state.phase = _WAITING
        task = asyncio.get_running_loop().create_task(
            self._run_job(session_key, state, pending, self._idle_delay(session_key)),
            context=contextvars.Context(),
        )
        state.task = task
        background = getattr(self._runner, "_background_tasks", None)
        if isinstance(background, set):
            background.add(task)
            task.add_done_callback(background.discard)

    async def wait_idle(self, session_key: str | None = None) -> None:
        """Wait for running jobs (tests, shutdown)."""
        states = (
            [self._sessions[session_key]]
            if session_key is not None and session_key in self._sessions
            else list(self._sessions.values())
            if session_key is None
            else []
        )
        tasks = [state.task for state in states if state.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # -- the job ---------------------------------------------------------

    async def _run_job(
        self, session_key: str, state: _SessionState, pending: _Pending, delay: float
    ) -> None:
        try:
            if delay > 0.0:
                # Idle wait: a turn that starts meanwhile cancels the job before
                # any model call (and so does a process that exits).
                await self._sleep(delay)
            state.phase = _SUMMARIZING
            if await self._compact(session_key, state, pending):
                self._backoff.pop(session_key, None)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - background job must not crash the loop
            log.warning(
                "compaction.proactive",
                session_key=session_key,
                action="failed",
                reason="error",
                error=str(exc),
            )
        finally:
            if (
                self._sessions.get(session_key) is state
                and state.pending is None
                and state.agent is None
            ):
                self._sessions.pop(session_key, None)

    def _skip(self, session_key: str, reason: str, **fields: Any) -> None:
        log.info(
            "compaction.proactive",
            session_key=session_key,
            action="skipped",
            reason=reason,
            **fields,
        )

    async def _compact(self, session_key: str, state: _SessionState, pending: _Pending) -> bool:
        """Run the job; False when a turn won the race after the summarizer ran."""
        runner = self._runner
        manager = getattr(runner, "_session_manager", None)
        if manager is None:
            return True
        agent = pending.agent
        view = _turn_view(agent)
        if view is None:
            self._skip(session_key, "no_turn_view")
            return True
        transcript = await manager.get_transcript(session_key)
        tail = _kept_tail(transcript, getattr(agent, "_transcript_input_entry_id", None))
        if tail is None:
            self._skip(session_key, "no_structured_turn_record")
            return True
        tail_index, record, current = tail
        prefix = view.messages[: view.turn_input_index]
        if len(view.messages) - view.turn_input_index != len(current):
            self._skip(session_key, "history_mismatch")
            return True
        messages = [*prefix, *current]
        message_tokens = _scaled(agent._message_tokens, view.scale)
        estimated = int(view.overhead_tokens * view.scale) + message_tokens(messages)
        log.info(
            "compaction.proactive",
            session_key=session_key,
            action="started",
            estimated_tokens=estimated,
            limit_tokens=pending.limit_tokens,
        )
        budget_of = getattr(agent, "_compaction_budget_tokens", None)
        result = await _compaction_service(agent, session_key).compact(
            messages,
            turn_input_index=len(prefix),
            estimated_tokens=estimated,
            window_tokens=int(agent.config.context_window_tokens),
            message_tokens=message_tokens,
            budget_tokens=budget_of() if callable(budget_of) else None,
            proactive=True,
        )
        if not result.compacted:
            self._skip(session_key, result.reason or result.status)
            return True
        return await self._persist(
            session_key,
            state,
            pending,
            result,
            transcript=transcript,
            tail_index=tail_index,
            record=record,
            current=current,
        )

    async def _persist(
        self,
        session_key: str,
        state: _SessionState,
        pending: _Pending,
        result: CompactionResult,
        *,
        transcript: list[Any],
        tail_index: int,
        record: Any,
        current: list[Message],
    ) -> bool:
        from agentos.engine.cache_break_monitor import notify_compaction
        from agentos.engine.hooks import CompactionState, fire_after_compact, fire_before_compact
        from agentos.engine.transcript import (
            TranscriptRecord,
            encode_transcript_record,
            persistable_messages,
        )
        from agentos.engine.turn_runner.harness import _TurnRunnerCompactionPersistAdapter
        from agentos.session.compaction_lifecycle import (
            COMPACTION_PERSISTED_EVENT,
            compaction_effect_payload,
            compaction_lifecycle_payload,
        )
        from agentos.session.manager import TranscriptSnapshot

        runner = self._runner
        manager = runner._session_manager
        compaction_id = result.compaction_id
        last = transcript[-1]
        snapshot = TranscriptSnapshot(
            session_id=str(last.session_id), through_message_id=str(last.message_id)
        )
        reset_record = encode_transcript_record(
            TranscriptRecord(
                messages=persistable_messages([*summary_messages(result.summary), *current]),
                mode="reset",
                input_entry_id=record.input_entry_id,
                compaction_id=compaction_id,
                steered_entry_ids=record.steered_entry_ids,
            )
        )
        kept_entries: list[dict[str, Any]] = []
        for entry in transcript[tail_index:]:
            kept: dict[str, Any] = {"role": entry.role, "content": entry.content}
            if entry is transcript[-1]:
                kept["provider_messages"] = reset_record
            kept_entries.append(kept)

        hook_state = CompactionState(
            session_key=session_key,
            agent_id=pending.agent_id,
            total_tokens=result.tokens_before,
            threshold_tokens=pending.limit_tokens,
            extra={"phase": PROACTIVE_PHASE, "summary": result.summary},
        )
        hooks = tuple(getattr(runner, "_compaction_hooks", ()) or ())
        async with runner.get_session_lock(session_key), state.gate:
            if state.generation != pending.generation:
                # A turn loaded history after the summarizer ran: billed and lost.
                self._backoff[session_key] = self._backoff.get(session_key, 0) + 1
                self._skip(session_key, "turn_started")
                return False
            # Past this point the job persists (or fails to) under the gate;
            # a turn that starts afterwards must not cancel what is left.
            state.phase = _PERSISTED
            await fire_before_compact(hooks, hook_state)
            try:
                # Durable pre-image first; a failing checkpoint raises and
                # nothing is archived (same rule as the in-turn path).
                await _TurnRunnerCompactionPersistAdapter(runner)._record_checkpoint(
                    session_key, compaction_id
                )
                persist = manager.persist_compaction_result
                kwargs: dict[str, Any] = {
                    "compaction_id": compaction_id,
                    "trigger_reason": PROACTIVE_PHASE,
                }
                if "snapshot" in inspect.signature(persist).parameters:
                    kwargs["snapshot"] = snapshot
                persisted = await persist(session_key, result.summary, kept_entries, **kwargs)
            except Exception as exc:  # noqa: BLE001 - reported, transcript untouched
                log.warning(
                    "compaction.proactive",
                    session_key=session_key,
                    action="failed",
                    reason="persist_failed",
                    error=str(exc),
                )
                notify_compaction(
                    session_key,
                    source="automatic",
                    phase=PROACTIVE_PHASE,
                    status="failed",
                    reason="persist_failed",
                    message=str(exc),
                    **compaction_effect_payload(status="failed", reason="persist_failed"),
                    **compaction_lifecycle_payload(compaction_id or "", COMPACTION_PERSISTED_EVENT),
                )
                await fire_after_compact(
                    hooks, hook_state, {"status": "failed", "reason": "persist_failed"}
                )
                return True
            if persisted is None and "snapshot" in kwargs:
                # The session was reset, truncated or rotated while the job
                # ran: nothing was rewritten and nothing failed. The next turn
                # end checks again.
                self._skip(session_key, "transcript_changed")
                await fire_after_compact(
                    hooks, hook_state, {"status": "skipped", "reason": "transcript_changed"}
                )
                return True
        sync_manager = getattr(pending.agent, "_memory_sync_manager", None)
        if sync_manager is not None:
            sync_manager.mark_dirty()
        notify_compaction(
            session_key,
            source="automatic",
            phase=PROACTIVE_PHASE,
            status="completed",
            kept_count=len(kept_entries),
            summary_len=len(result.summary),
            **compaction_effect_payload(status="completed"),
            **compaction_lifecycle_payload(compaction_id or "", COMPACTION_PERSISTED_EVENT),
        )
        await fire_after_compact(
            hooks,
            hook_state,
            {"status": "completed", "summary": result.summary, "kept_entries": kept_entries},
        )
        log.info(
            "compaction.proactive",
            session_key=session_key,
            action="compacted",
            compaction_id=compaction_id,
            tokens_before=result.tokens_before,
            tokens_after=result.tokens_after,
            removed=result.removed_count,
            kept_rows=len(kept_entries),
            summary_source=result.summary_source,
        )
        return True


def _kept_tail(
    transcript: Sequence[Any], input_entry_id: str | None
) -> tuple[int, Any, list[Message]] | None:
    """Locate the finished turn in the persisted transcript.

    Returns ``(index of its input row, its record, its messages)`` when the
    transcript ends on that turn's structured assistant record, else
    ``None``. A row after the record (a follow-up queued meanwhile) belongs
    to the next turn, which will run soon anyway; the job then skips.
    """
    from agentos.engine.transcript import decode_transcript_record

    if not transcript or not input_entry_id:
        return None
    record_index = len(transcript) - 1
    if getattr(transcript[record_index], "role", None) != "assistant":
        return None
    record = decode_transcript_record(getattr(transcript[record_index], "provider_messages", None))
    if record is None or record.input_entry_id != input_entry_id:
        return None
    input_index = next(
        (
            index
            for index, entry in enumerate(transcript)
            if getattr(entry, "message_id", None) == input_entry_id
        ),
        None,
    )
    if input_index is None or input_index > record_index:
        return None
    current = list(record.messages)
    if record.mode == "reset":
        if not is_summary_pair(current):
            return None
        current = current[2:]
    if not current:
        return None
    return input_index, record, current
