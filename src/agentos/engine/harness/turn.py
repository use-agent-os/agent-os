"""``TurnHarness``: the orchestrator of one agent turn (#3819).

The harness runs the eight turn stages in order (input, provider and tools,
prompt assembler, agent bootstrap, history load, attachment, stream
consumer, turn finalizer). It takes them through its constructor, the
same way each stage takes its ports. It also owns the turn's terminal
paths: the spend-budget refusal, a missing provider, cancellation, an
unexpected error, and the router-control replay, which reruns the turn as
a loop rather than by recursion.

``TurnRunner`` builds the harness and supplies its services (sessions,
budgets, trace events, decision log). It still exposes the historical
``TurnRunner._run_turn`` signature, which packs its arguments into a
:class:`TurnRequest`.

The stage results stay locals of :meth:`TurnHarness._run_turn` on purpose:
the stage snapshot tests read them off that frame while a later stage runs.
"""

# The stage results are kept as locals of ``TurnHarness._run_turn`` for the
# stage snapshot tests' frame-walking probes, so some are assigned and never
# read again in the method itself.
# ruff: noqa: F841

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable
from contextlib import aclosing
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Any, TypeVar, cast

import structlog

from agentos.engine.router_decision import build_router_decision_event
from agentos.engine.transcript import transcript_record_of
from agentos.engine.turn_runner.agent_bootstrap_stage import AgentBootstrapStageInput
from agentos.engine.turn_runner.attachment_stage import AttachmentStageInput
from agentos.engine.turn_runner.history_load_stage import HistoryLoadStageInput
from agentos.engine.turn_runner.input_stage import InputStageInput
from agentos.engine.turn_runner.prompt_assembler_stage import PromptAssemblerStageInput
from agentos.engine.turn_runner.provider_and_tools_stage import ProviderAndToolsStageInput
from agentos.engine.turn_runner.stream_consumer_stage import (
    StreamConsumerStageInput,
    _StreamState,
)
from agentos.engine.turn_runner.turn_finalizer_stage import TurnFinalizerStageInput
from agentos.engine.turn_timing import TurnTiming, emit_turn_timing_log
from agentos.engine.types import (
    AgentEvent,
    DoneEvent,
    ErrorEvent,
    RouterControlReplayEvent,
    SteeringEvent,
    WarningEvent,
)
from agentos.observability.prompt_report import build_prompt_report
from agentos.observability.trace import TraceContext
from agentos.observability.turn_call_log import TurnCallLogger, is_turn_call_log_enabled
from agentos.session.terminal_reply import build_terminal_reply, sanitize_agent_error

if TYPE_CHECKING:
    from agentos.engine.runtime import TurnRunner
    from agentos.engine.turn_runner import (
        AgentBootstrapStage,
        AttachmentStage,
        HistoryLoadStage,
        InputStage,
        PromptAssemblerStage,
        ProviderAndToolsStage,
        StreamConsumerStage,
        TurnFinalizerStage,
    )
    from agentos.engine.turn_runner.history_load_stage import HistoryLoadStageOutput
    from agentos.observability.decision_log import PipelineStepRecord
    from agentos.tools.types import ToolContext

# The runner's logger: turn events keep the logger name they always had.
log = structlog.get_logger("agentos.engine.runtime")

_T = TypeVar("_T")


@dataclass(frozen=True)
class TurnRequest:
    """Everything one turn is asked to do (``TurnRunner._run_turn``'s arguments)."""

    message: str
    session_key: str
    agent_id: str
    model: str | None
    attachments: list[dict]
    tool_context: ToolContext | None = None
    timeout: float | None = None
    max_iterations: int | None = None
    iteration_timeout: float | None = None
    tool_timeout: float | None = None
    request_timeout: float | None = None
    max_provider_retries: int | None = None
    length_capped_continuations: int | None = None
    input_mode: str = "user"
    persist_input: bool = False
    input_provenance: dict[str, Any] | None = None
    history_has_persisted_user: bool = True
    fresh_user_session: bool | None = None
    session_intent: str | None = None
    semantic_message: str | None = None
    run_kind: str = "default"
    heartbeat_ack_max_chars: int = 300
    bootstrap_context_mode: str | None = None
    no_memory_capture: bool = False
    ingress_pipeline_steps: list[PipelineStepRecord] | None = None
    router_control_replay_depth: int = 0
    persisted_user_message_id: str | None = None

    def fresh_user_session_flag(self) -> bool:
        if self.fresh_user_session is not None:
            return self.fresh_user_session
        return (
            self.input_mode == "user"
            and self.run_kind == "default"
            and not self.history_has_persisted_user
        )


@dataclass
class TurnStages:
    """The eight turn stages, in the order the harness runs them."""

    input: InputStage
    provider_and_tools: ProviderAndToolsStage
    prompt_assembler: PromptAssemblerStage
    agent_bootstrap: AgentBootstrapStage
    history_load: HistoryLoadStage
    attachment: AttachmentStage
    stream_consumer: StreamConsumerStage
    turn_finalizer: TurnFinalizerStage


@dataclass
class _TurnRun:
    """One run's bookkeeping, shared by the harness's terminal paths."""

    request: TurnRequest
    turn_id: str
    timing: TurnTiming
    started_at: float
    trace_context: TraceContext
    turn_progress: Any
    budget_reservation: str | None = None
    turn_call_logger: TurnCallLogger | None = None
    #: The agent once bootstrap built it; the cancellation path reads its
    #: sealed record whether or not a progress writer is attached.
    agent: Any = None
    #: The agent whose progress sink this run attached (detached on close).
    turn_progress_agent: Any = None
    #: Set once the finalizer's transcript append succeeded: the turn's reply
    #: is durable, so a cancel landing after it must not write it again.
    reply_persisted: bool = False
    # Declared up-front so the cancellation path can read them even when it
    # fires before the stream loop.
    final_text_parts: list[str] = field(default_factory=list)
    turn_segments: list[dict] = field(default_factory=list)
    turn_artifacts: list[dict[str, Any]] = field(default_factory=list)
    artifact_delivery_failures: list[str] = field(default_factory=list)
    #: Whether this run already yielded its terminal DoneEvent.
    done_yielded: bool = False
    #: Set when the stream asked for a router-control replay of the turn.
    replay: TurnRequest | None = None

    def emit(self, runner: TurnRunner, kind: str, **kwargs: Any) -> None:
        request = self.request
        runner._emit_turn_event(
            kind,
            self.trace_context,
            session_key=request.session_key,
            agent_id=request.agent_id,
            turn_id=self.turn_id,
            run_kind=request.run_kind,
            input_mode=request.input_mode,
            **kwargs,
        )


class TurnHarness:
    """Run one agent turn through its stages; see the module docstring."""

    def __init__(self, runner: TurnRunner, stages: TurnStages) -> None:
        self._runner = runner
        self.stages = stages

    async def run_turn(self, request: TurnRequest) -> AsyncGenerator[AgentEvent, None]:
        """Run the turn, then each router-control replay of it, in a loop."""
        replay: TurnRequest | None = request
        while replay is not None:
            run = self._new_run(replay)
            async with aclosing(self._run_turn(run)) as events:
                async for event in events:
                    yield event
            replay = run.replay

    def _new_run(self, request: TurnRequest) -> _TurnRun:
        turn_id = uuid.uuid4().hex
        # Mid-turn durability: the in-progress row after each tool round.
        segments: list[dict] = []
        run = _TurnRun(
            request=request,
            turn_id=turn_id,
            timing=TurnTiming(),
            # Bracket turn setup + stream loop so latency_ms is the full turn.
            started_at=time.monotonic(),
            trace_context=TraceContext.new(
                session_key=request.session_key, turn_id=turn_id, agent_id=request.agent_id
            ),
            turn_progress=self._runner._new_turn_progress_writer(
                request.session_key, request.run_kind, segments
            ),
            turn_segments=segments,
        )
        return run

    async def _run_turn(self, run: _TurnRun) -> AsyncGenerator[AgentEvent, None]:
        """Run the stages once; the stage results stay locals (see module doc)."""
        runner, stages, req = self._runner, self.stages, run.request
        resolved_model = ""
        final_prompt_str = ""
        turn_obj: Any | None = None
        no_memory_capture = req.no_memory_capture
        budget_stop, budget_message = self._start_run(run)
        if budget_stop:
            async for event in self._refuse_over_budget(run, budget_message):
                yield event
            return
        try:
            if budget_message:
                yield runner._handle_runtime_warning(
                    WarningEvent(code="budget_warning", message=budget_message)
                )
            input_out = await self._timed(run, "input", stages.input.run(self._input_of(req)))
            runtime_message = input_out.runtime_message
            semantic_input = input_out.semantic_input
            extra_prompt_context = input_out.extra_prompt_context
            pre_io, extra_prompt_context = await self._pre_provider_io(
                run, runtime_message, extra_prompt_context
            )
            pt_outcome = await self._timed(
                run, "provider_and_tools", stages.provider_and_tools.run(self._provider_of(req))
            )
            if pt_outcome.terminate:
                error_event = cast(ErrorEvent, pt_outcome.require_early_yield())
                async for event in self._fail_without_provider(run, error_event):
                    yield event
                return
            pt_out = pt_outcome.require_output()
            provider = pt_out.provider
            cloned_selector = pt_out.cloned_selector
            tool_defs = pt_out.tool_defs
            tool_handler = pt_out.tool_handler
            tool_context = pt_out.effective_tool_context
            tool_metadata = pt_out.tool_metadata

            pa_input = self._prompt_assembler_of(
                run, input_out, pt_out, extra_prompt_context, pre_io.router_context()
            )
            pa_outcome = await self._timed(
                run, "prompt_assembler", stages.prompt_assembler.run(pa_input)
            )
            pa_out = pa_outcome.require_output()
            provider = pa_out.provider
            turn = turn_obj = pa_out.turn
            effective_runtime_message = pa_out.effective_runtime_message
            final_prompt = final_prompt_str = pa_out.final_prompt
            resolved_model = pa_out.resolved_model
            session_id_for_log = pa_out.session_id_for_log
            cache_breakpoints = pa_out.cache_breakpoints
            request_context_prompt = pa_out.request_context_prompt
            run.trace_context = replace(
                run.trace_context, session_id=pa_out.trace_context_session_id
            )
            # Read by the test_prompt_assembler_stage_snapshot frame-walking
            # probe, with the locals above. Do not remove.
            selector_model = pa_out.selector_model
            prompt_report_for_log = pa_out.prompt_report
            trace_context = run.trace_context
            self._open_turn_call_log(run, pa_out, tool_context, effective_runtime_message)
            self._wire_router_control(tool_context, turn, req.router_control_replay_depth)
            router_event = build_router_decision_event(turn)
            if router_event is not None:
                yield router_event

            ab_input = self._bootstrap_of(run, pt_out, pa_out, tool_context, pre_io.memory_warm())
            ab_outcome = await self._timed(
                run, "agent_bootstrap", stages.agent_bootstrap.run(ab_input)
            )
            ab_out = ab_outcome.require_output()
            agent = run.agent = ab_out.agent
            agent.turn_timing = run.timing
            # Read by the test_agent_bootstrap_stage_snapshot probe. Do not remove.
            agent_config = ab_out.agent_config
            effective_runtime_timeout = ab_out.effective_runtime_timeout
            effective_max_iterations = ab_out.effective_max_iterations
            effective_iteration_timeout = ab_out.effective_iteration_timeout
            effective_tool_timeout = ab_out.effective_tool_timeout
            effective_agent_request_timeout = ab_out.effective_request_timeout
            effective_max_provider_retries = ab_out.effective_max_provider_retries
            private_memory_allowed = ab_out.private_memory_allowed
            sync_manager = ab_out.sync_manager
            self._log_runtime_budget(run, ab_out)

            # History load + request-context prepend. Compaction is checked by
            # the agent before every provider call, not before the turn.
            ch_outcome = await self._timed(
                run,
                "compaction_and_history",
                stages.history_load.run(self._history_of(req, agent, input_out)),
            )
            self._configure_agent_context(agent, ch_outcome.require_output())
            att_input = AttachmentStageInput(
                effective_runtime_message=effective_runtime_message, attachments=req.attachments
            )
            att_outcome = await self._timed(run, "attachment", stages.attachment.run(att_input))
            att_out = att_outcome.require_output()

            self._attach_progress_sink(run, agent)
            stream_state = self._stream_state_of(run)
            stream_inp = self._stream_of(
                run,
                stream_state,
                agent=agent,
                sync_manager=sync_manager,
                private_memory_allowed=private_memory_allowed,
                turn=turn,
                tool_defs=tool_defs,
                att_out=att_out,
                semantic_input=semantic_input,
                effective_runtime_message=effective_runtime_message,
                tool_context=tool_context,
            )
            replay_requested = False
            run.timing.stage_start("stream_consumer")
            # aclosing: leaving the loop (the replay ``break``, a closed or
            # cancelled caller) closes the stage and the agent run under it now.
            async with aclosing(stages.stream_consumer.run(stream_inp)) as stream_events:
                async for event in stream_events:
                    if isinstance(event, RouterControlReplayEvent):
                        replay_requested = True
                        yield event
                        break
                    if isinstance(event, SteeringEvent) and event.no_memory_capture:
                        # A message steered in asked to stay out of memory.
                        no_memory_capture = True
                    if isinstance(event, DoneEvent):
                        run.done_yielded = True
                    yield event
            run.timing.stage_end("stream_consumer")
            if replay_requested:
                # The replay reruns the whole turn (``run_turn`` loops); what
                # this run recorded is superseded, as its segments are.
                if run.turn_progress is not None:
                    await run.turn_progress.discard()
                run.replay = self._replay_of(req, tool_context, no_memory_capture)
                return
            done_event = stream_state.done_event
            if stream_state.current_text_parts:  # flush the remaining text segment
                run.turn_segments.append(
                    {"type": "text", "text": "".join(stream_state.current_text_parts)}
                )
            self._mark_cooperative_abort(run, done_event, agent)

            fin_input = self._finalizer_of(
                run,
                stream_state,
                runtime_message=runtime_message,
                resolved_model=resolved_model,
                tool_context=tool_context,
                no_memory_capture=no_memory_capture,
                agent=agent,
            )
            fin_outcome = await self._timed(
                run, "turn_finalizer", stages.turn_finalizer.run(fin_input)
            )
            fin_out = fin_outcome.require_output()
            if run.turn_progress is not None:
                if fin_out.transcript_appended:
                    run.turn_progress.replaced()
                else:
                    await run.turn_progress.discard()
            run.turn_segments = fin_out.turn_segments
            self._observe_turn_end(
                run,
                pa_out,
                final_text=fin_out.final_text,
                error_message=stream_state.error_message,
                done_event=done_event,
            )
            if stream_state.pending_error_event is not None:
                yield stream_state.pending_error_event
            deferred_done = stream_state.deferred_done_event
            if deferred_done is not None and not run.done_yielded:
                run.done_yielded = True
                yield deferred_done
        except asyncio.CancelledError:
            await self._on_cancelled(run)
            raise
        except Exception as exc:
            async for event in self._on_error(run, exc):
                yield event
        finally:
            self._close_run(run)

    # ------------------------------------------------------------------
    # Stage inputs, built from the run and the earlier stages' outputs
    # ------------------------------------------------------------------

    def _start_run(self, run: _TurnRun) -> tuple[bool, str | None]:
        """Emit ``turn_start``, then pass the spend budget gate.

        The gate is checked (and reserved) before any provider work; the
        reservation is released in ``_close_run`` on every exit path.
        """
        req = run.request
        run.emit(
            self._runner,
            "turn_start",
            seq=1,
            attrs={"input_mode": req.input_mode, "run_kind": req.run_kind},
            payload={"message_chars": len(req.message), "attachment_count": len(req.attachments)},
        )
        budget_stop, budget_message, run.budget_reservation = self._runner._reserve_spend_budget(
            req.session_key
        )
        return budget_stop, budget_message

    async def _pre_provider_io(
        self, run: _TurnRun, runtime_message: str, extra_prompt_context: dict[str, str] | None
    ) -> tuple[Any, dict[str, str] | None]:
        """Start the pre-provider reads; apply recall/knowledge and the tool hydrate.

        Recall, project knowledge, the deferred-tool hydrate and the
        router-context read start together; each is applied at its point.
        """
        req = run.request
        pre_io = self._runner._start_pre_provider_io(
            agent_id=req.agent_id,
            session_key=req.session_key,
            runtime_message=runtime_message,
            router_exclude_last_user=req.history_has_persisted_user or req.persist_input,
        )
        extra_prompt_context = await pre_io.extra_prompt_context(extra_prompt_context)
        await pre_io.hydrate_tools()
        return pre_io, extra_prompt_context

    @staticmethod
    def _stream_state_of(run: _TurnRun) -> _StreamState:
        """The stream state; the four accumulators are the run's own lists, so
        the cancellation path still sees what streamed."""
        return _StreamState(
            current_text_parts=[],
            final_text_parts=run.final_text_parts,
            turn_segments=run.turn_segments,
            turn_artifacts=run.turn_artifacts,
            artifact_delivery_failures=run.artifact_delivery_failures,
        )

    @staticmethod
    async def _timed(run: _TurnRun, stage: str, work: Awaitable[_T]) -> _T:
        """Await one stage between its timing marks (no end mark if it raises)."""
        run.timing.stage_start(stage)
        result = await work
        run.timing.stage_end(stage)
        return result

    def _input_of(self, req: TurnRequest) -> InputStageInput:
        return InputStageInput(
            message=req.message,
            semantic_message=req.semantic_message,
            input_mode=req.input_mode,
            persist_input=req.persist_input,
            input_provenance=req.input_provenance,
            session_key=req.session_key,
            tool_context=req.tool_context,
            session_append=self._runner._session_manager,
        )

    @staticmethod
    def _provider_of(req: TurnRequest) -> ProviderAndToolsStageInput:
        return ProviderAndToolsStageInput(
            session_key=req.session_key,
            agent_id=req.agent_id,
            tool_context=req.tool_context,
            run_kind=req.run_kind,
            input_mode=req.input_mode,
        )

    @staticmethod
    def _prompt_assembler_of(
        run: _TurnRun,
        input_out: Any,
        pt_out: Any,
        extra_prompt_context: dict[str, str] | None,
        router_context: Any,
    ) -> PromptAssemblerStageInput:
        req = run.request
        return PromptAssemblerStageInput(
            runtime_message=input_out.runtime_message,
            semantic_input=input_out.semantic_input,
            extra_prompt_context=extra_prompt_context,
            provider=pt_out.provider,
            cloned_selector=pt_out.cloned_selector,
            tool_defs=pt_out.tool_defs,
            effective_tool_context=pt_out.effective_tool_context,
            tool_metadata=pt_out.tool_metadata,
            session_key=req.session_key,
            agent_id=req.agent_id,
            turn_id=run.turn_id,
            attachments=req.attachments,
            bootstrap_context_mode=req.bootstrap_context_mode,
            model=req.model,
            history_has_persisted_user=req.history_has_persisted_user,
            persist_input=req.persist_input,
            fresh_user_session=req.fresh_user_session_flag(),
            ingress_pipeline_steps=req.ingress_pipeline_steps,
            normalization_metadata=input_out.normalization_metadata,
            router_context=router_context,
        )

    @staticmethod
    def _bootstrap_of(
        run: _TurnRun,
        pt_out: Any,
        pa_out: Any,
        tool_context: ToolContext | None,
        pending_memory_warm: Any,
    ) -> AgentBootstrapStageInput:
        req = run.request
        return AgentBootstrapStageInput(
            provider=pa_out.provider,
            cloned_selector=pt_out.cloned_selector,
            turn=pa_out.turn,
            final_prompt=pa_out.final_prompt,
            cache_breakpoints=pa_out.cache_breakpoints,
            request_context_prompt=pa_out.request_context_prompt,
            resolved_model=pa_out.resolved_model,
            session_id_for_log=pa_out.session_id_for_log,
            tool_handler=pt_out.tool_handler,
            turn_call_logger=run.turn_call_logger,
            tool_context=tool_context,
            session_key=req.session_key,
            agent_id=req.agent_id,
            timeout=req.timeout,
            max_iterations=req.max_iterations,
            iteration_timeout=req.iteration_timeout,
            tool_timeout=req.tool_timeout,
            request_timeout=req.request_timeout,
            max_provider_retries=req.max_provider_retries,
            length_capped_continuations=req.length_capped_continuations,
            pending_memory_warm=pending_memory_warm,
        )

    @staticmethod
    def _history_of(req: TurnRequest, agent: Any, input_out: Any) -> HistoryLoadStageInput:
        return HistoryLoadStageInput(
            agent=agent,
            session_key=req.session_key,
            agent_id=req.agent_id,
            history_has_persisted_user=req.history_has_persisted_user,
            input_message_id=(
                req.persisted_user_message_id
                or getattr(input_out.persisted_entry, "message_id", None)
            ),
        )

    def _stream_of(
        self,
        run: _TurnRun,
        stream_state: _StreamState,
        *,
        agent: Any,
        sync_manager: Any,
        private_memory_allowed: bool,
        turn: Any,
        tool_defs: list[Any],
        att_out: Any,
        semantic_input: str,
        effective_runtime_message: str,
        tool_context: ToolContext | None,
    ) -> StreamConsumerStageInput:
        req, runner = run.request, self._runner
        return StreamConsumerStageInput(
            agent=agent,
            agent_id=req.agent_id,
            sync_manager=sync_manager,
            private_memory_allowed=private_memory_allowed,
            turn=turn,
            tool_defs=tool_defs,
            turn_input=att_out.turn_input,
            extra_messages=att_out.extra_messages,
            semantic_input=semantic_input,
            effective_runtime_message=effective_runtime_message,
            input_provenance=req.input_provenance,
            session_key=req.session_key,
            run_kind=req.run_kind,
            heartbeat_ack_max_chars=req.heartbeat_ack_max_chars,
            bootstrap_context_mode=req.bootstrap_context_mode,
            router_cfg=getattr(runner._config, "agentos_router", None),
            session_manager_present=runner._session_manager is not None,
            state=stream_state,
            tool_context=tool_context,
        )

    @staticmethod
    def _finalizer_of(
        run: _TurnRun,
        stream_state: _StreamState,
        *,
        runtime_message: str,
        resolved_model: str,
        tool_context: ToolContext | None,
        no_memory_capture: bool,
        agent: Any,
    ) -> TurnFinalizerStageInput:
        req = run.request
        return TurnFinalizerStageInput(
            final_text_parts=run.final_text_parts,
            turn_segments=run.turn_segments,
            turn_artifacts=run.turn_artifacts,
            error_message=stream_state.error_message,
            pending_error_event=stream_state.pending_error_event,
            done_event=stream_state.done_event,
            runtime_message=runtime_message,
            input_mode=req.input_mode,
            input_provenance=req.input_provenance,
            resolved_model=resolved_model,
            agent_id=req.agent_id,
            session_key=req.session_key,
            tool_context=tool_context,
            run_kind=req.run_kind,
            heartbeat_ack_max_chars=req.heartbeat_ack_max_chars,
            no_memory_capture=no_memory_capture,
            provider_messages=transcript_record_of(agent, artifacts=run.turn_artifacts),
            replaces_message_id=(
                run.turn_progress.message_id if run.turn_progress is not None else None
            ),
            on_transcript_appended=lambda: TurnHarness._note_reply_persisted(run),
        )

    @staticmethod
    def _note_reply_persisted(run: _TurnRun) -> None:
        """The finalizer's append landed: the reply row replaced the in-progress row."""
        from agentos.engine.turn_progress import current_turn_stop_signal

        run.reply_persisted = True
        signal = current_turn_stop_signal()
        if signal is not None and not signal.aborted:
            # Not a stopped turn's partial reply: the whole answer is in.
            signal.reply_delivered = True
        if run.turn_progress is not None:
            run.turn_progress.replaced()

    @staticmethod
    def _replay_of(
        req: TurnRequest, tool_context: ToolContext | None, no_memory_capture: bool
    ) -> TurnRequest:
        """The request a router-control replay reruns the turn with."""
        return replace(
            req,
            tool_context=tool_context,
            persist_input=False,
            history_has_persisted_user=True,
            fresh_user_session=False,
            no_memory_capture=no_memory_capture,
            router_control_replay_depth=req.router_control_replay_depth + 1,
            persisted_user_message_id=None,
        )

    @staticmethod
    def _attach_progress_sink(run: _TurnRun, agent: Any) -> None:
        if run.turn_progress is None:
            return
        set_progress_sink = getattr(agent, "set_turn_progress_sink", None)
        if callable(set_progress_sink):
            set_progress_sink(run.turn_progress.record)
            run.turn_progress_agent = agent

    @staticmethod
    def _log_runtime_budget(run: _TurnRun, ab_out: Any) -> None:
        if run.turn_call_logger is not None:
            run.turn_call_logger.write(
                "agent_runtime_budget",
                {
                    "max_iterations": ab_out.effective_max_iterations,
                    "max_iterations_source": ab_out.effective_max_iterations_source,
                },
            )

    # ------------------------------------------------------------------
    # Steps of ``_run_turn`` that do not produce stage results
    # ------------------------------------------------------------------

    async def _refuse_over_budget(
        self, run: _TurnRun, budget_message: str | None
    ) -> AsyncIterator[AgentEvent]:
        """End a turn the spend budget refused before any provider work."""
        from agentos.engine.runtime import _terminal_done_for

        req = run.request
        # The refusal hinges on the decision, never on the presentation string:
        # a tracker that reports a stop without a message must still stop.
        budget_error = ErrorEvent(
            message=budget_message or "A configured spend budget limit has been reached.",
            code="budget_exceeded",
        )
        log.warning(
            "turn_runner.budget_exceeded",
            session_key=req.session_key,
            agent_id=req.agent_id,
            message=budget_error.message,
        )
        run.emit(
            self._runner,
            "turn_error",
            seq=2,
            payload={
                "error_type": "BudgetExceeded",
                "error_code": budget_error.code,
                "error_chars": len(budget_error.message),
            },
        )
        await self._runner._persist_turn_error(req.session_key, budget_error)
        yield budget_error
        yield _terminal_done_for(budget_error)

    async def _fail_without_provider(
        self, run: _TurnRun, error_event: ErrorEvent
    ) -> AsyncIterator[AgentEvent]:
        """End a turn no provider could serve: trace, persist, yield (in that order)."""
        from agentos.engine.runtime import _terminal_done_for

        log.error("turn_runner.no_provider", session_key=run.request.session_key)
        run.emit(
            self._runner,
            "turn_error",
            seq=2,
            payload={
                "error_type": "ProviderResolutionError",
                "error_code": error_event.code,
                "error_chars": len(error_event.message),
            },
        )
        await self._runner._persist_turn_error(run.request.session_key, error_event)
        yield error_event
        run.done_yielded = True
        yield _terminal_done_for(error_event)

    def _open_turn_call_log(
        self,
        run: _TurnRun,
        pa_out: Any,
        tool_context: ToolContext | None,
        effective_runtime_message: str,
    ) -> None:
        """Start the per-turn call log (when enabled) with the prompt report."""
        req, runner = run.request, self._runner
        turn = pa_out.turn
        if is_turn_call_log_enabled(runner._diagnostics_state):
            run.turn_call_logger = TurnCallLogger(
                trace_id=run.trace_context.trace_id,
                turn_id=run.turn_id,
                session_key=req.session_key,
                session_id=pa_out.session_id_for_log,
                session_intent=req.session_intent,
                agent_id=req.agent_id,
                provider=pa_out.provider_name,
                model=pa_out.resolved_model,
                source=runner._build_turn_call_source(
                    tool_context, req.input_provenance, run_kind=req.run_kind
                ),
            )
            run.turn_call_logger.write("prompt_report", asdict(pa_out.prompt_report))
            run.turn_call_logger.write(
                "turn_start",
                {
                    "input_mode": req.input_mode,
                    "message": effective_runtime_message,
                    "attachment_count": len(req.attachments),
                    "tool_names": [getattr(td, "name", "") for td in turn.tool_defs],
                },
            )
        log.debug(
            "turn_runner.model_resolved",
            explicit_model=req.model,
            pipeline_model=turn.model,
            selector_model=pa_out.selector_model,
            resolved=pa_out.resolved_model,
            agentos_router_tier=pa_out.agentos_router_tier,
        )

    def _wire_router_control(
        self, tool_context: ToolContext | None, turn: Any, replay_depth: int
    ) -> None:
        if tool_context is None:
            return
        runner = self._runner
        tool_context.router_control_config = getattr(runner._config, "agentos_router", None)
        tool_context.router_control_hold_store = runner._router_control_hold_store
        tool_context.router_control_replay_depth = replay_depth
        tool_context.router_control_turn_hold_applied = bool(
            turn.metadata.get("router_control_hold_applied")
        )

    def _configure_agent_context(self, agent: Any, ch_out: HistoryLoadStageOutput) -> None:
        """Apply the loaded request context and the compaction settings to the agent."""
        runner = self._runner
        agent.config.request_context_prompt = ch_out.final_request_context_prompt
        agent.config.context_overflow_threshold = runner._preflight_compact_ratio()
        agent.config.history_tool_result_max_chars = int(
            getattr(getattr(runner._config, "compaction", None), "history_tool_result_max_chars", 0)
            or 0
        )
        runner._apply_compaction_summary_settings(agent)

    @staticmethod
    def _mark_cooperative_abort(run: _TurnRun, done_event: DoneEvent | None, agent: Any) -> None:
        """A turn that stopped on a cooperative abort ended normally; give its
        partial reply the same "[interrupted]" marker a hard cancel writes,
        and its unanswered tool calls their arguments."""
        from agentos.engine.runtime import (
            _cancelled_partial_response_text,
            _fill_tool_use_inputs_from_record,
        )

        done_outcome = getattr(done_event, "outcome", None)
        if (
            getattr(done_outcome, "kind", None) == "interrupted"
            and getattr(done_outcome, "reason", None) == "cancelled"
        ):
            marked = _cancelled_partial_response_text(
                "".join(run.final_text_parts), run.turn_artifacts
            )
            run.final_text_parts[:] = [marked]
            _fill_tool_use_inputs_from_record(run.turn_segments, transcript_record_of(agent))

    def _observe_turn_end(
        self,
        run: _TurnRun,
        pa_out: Any,
        *,
        final_text: str,
        error_message: str | None,
        done_event: DoneEvent | None,
    ) -> None:
        """Call-log ``turn_end``, the ``turn_end`` trace event, and the decision entry."""
        from agentos.engine.runtime import collect_invoked_skills

        req, runner = run.request, self._runner
        turn, resolved_model = pa_out.turn, pa_out.resolved_model
        session_id_for_log, final_prompt_str = pa_out.session_id_for_log, pa_out.final_prompt
        if run.turn_call_logger is not None:
            run.turn_call_logger.write(
                "turn_end",
                {"final_text": final_text, "segments": run.turn_segments, "error": error_message},
            )
        metadata = turn.metadata
        run.emit(
            runner,
            "turn_end",
            seq=2,
            attrs={"provider": pa_out.provider_name, "model": resolved_model},
            payload={
                "final_text_chars": len(final_text),
                "segment_count": len(run.turn_segments),
                "artifact_count": len(run.turn_artifacts),
                "error": bool(error_message),
                "tool_projection_applied": bool(metadata.get("tool_projection_applied", False)),
                "tool_projection_calls": int(metadata.get("tool_projection_calls", 0) or 0),
                "tool_projection_tokens_saved": int(
                    metadata.get("tool_projection_tokens_saved", 0) or 0
                ),
                "tool_result_store_writes": int(metadata.get("tool_result_store_writes", 0) or 0),
                "tool_result_store_skips": int(metadata.get("tool_result_store_skips", 0) or 0),
            },
        )
        # Best-effort DecisionEntry for this turn; it never breaks the turn.
        prompt_report_for_decision = build_prompt_report(
            turn_id=run.turn_id,
            session_key=req.session_key,
            session_id=session_id_for_log,
            agent_id=req.agent_id,
            system_prompt=final_prompt_str,
            tool_defs=turn.tool_defs,
            metadata=metadata,
            tool_profile=metadata.get("tool_profile"),
        )
        runner._emit_decision_entry(
            turn_id=run.turn_id,
            session_key=req.session_key,
            session_id=session_id_for_log,
            message=req.message,
            final_prompt=final_prompt_str,
            tool_defs=turn.tool_defs,
            turn_obj=turn,
            provider=pa_out.provider,
            resolved_model=resolved_model,
            turn_started_at=run.started_at,
            prompt_report=prompt_report_for_decision,
            session_intent=req.session_intent,
            done_event=done_event,
            trace_id=run.trace_context.trace_id,
            skills_invoked=collect_invoked_skills(run.turn_segments),
            timing=run.timing.summary(),
        )

    async def _on_cancelled(self, run: _TurnRun) -> None:
        """Keep what already streamed of a cancelled turn (the caller re-raises).

        The partial reply is persisted with an ``[interrupted]`` marker, so the
        transcript does not end on an orphan user message. A cancel that lands
        after the finalizer persisted the reply (during memory capture or the
        totals rollup) writes nothing: the turn is already in the transcript.
        """
        from agentos.engine.runtime import (
            _cancelled_partial_response_text,
            _fill_tool_use_inputs_from_record,
        )
        from agentos.engine.turn_progress import stopped_by_shutdown

        runner, session_key = self._runner, run.request.session_key
        partial_text = "".join(run.final_text_parts).rstrip()
        turn_segments, turn_artifacts = run.turn_segments, run.turn_artifacts
        if stopped_by_shutdown():
            # The gateway is stopping under the turn: no "[interrupted]" reply.
            # The in-progress row (or the bare user message) stays the tail, so
            # restart recovery sees how far the turn got.
            log.info("turn_runner.cancelled_by_shutdown", session_key=session_key)
        elif run.reply_persisted:
            log.info("turn_runner.cancelled_after_reply_persisted", session_key=session_key)
        elif runner._session_manager is not None and (
            partial_text
            or turn_segments
            or turn_artifacts
            or transcript_record_of(run.agent) is not None
        ):
            try:
                body = _cancelled_partial_response_text(partial_text, turn_artifacts)
                if turn_artifacts:
                    body = json.dumps(
                        {"text": body, "artifacts": turn_artifacts}, ensure_ascii=False
                    )
                cancel_kwargs: dict[str, Any] = {}
                if run.turn_progress is not None and run.turn_progress.message_id is not None:
                    # The partial row takes the in-progress row's place.
                    cancel_kwargs["replaces_message_id"] = run.turn_progress.message_id
                # The agent sealed the cancelled turn (every tool call paired, an
                # interruption note at the end); persisting that record lets the
                # next turn replay it without pairing repair.
                cancelled_provider_messages = transcript_record_of(
                    run.agent, artifacts=turn_artifacts
                )
                if cancelled_provider_messages is not None:
                    cancel_kwargs["provider_messages"] = cancelled_provider_messages
                    _fill_tool_use_inputs_from_record(turn_segments, cancelled_provider_messages)
                await runner._append_session_message(
                    session_key,
                    role="assistant",
                    content=body,
                    tool_calls=turn_segments if turn_segments else None,
                    **cancel_kwargs,
                )
                if run.turn_progress is not None:
                    run.turn_progress.replaced()
                log.info(
                    "turn_runner.cancelled_partial_persisted",
                    session_key=session_key,
                    text_chars=len(partial_text),
                    segment_count=len(turn_segments),
                )
            except Exception:  # pragma: no cover — defensive: don't swallow the cancel
                log.warning(
                    "turn_runner.cancelled_persist_failed", session_key=session_key, exc_info=True
                )
        if run.turn_call_logger is not None:
            try:
                run.turn_call_logger.write(
                    "turn_cancelled", {"partial_text_chars": len(partial_text)}
                )
            except Exception:
                pass
        run.emit(runner, "turn_cancelled", seq=2, payload={"partial_text_chars": len(partial_text)})

    async def _on_error(self, run: _TurnRun, exc: Exception) -> AsyncIterator[AgentEvent]:
        """Record an unexpected failure; end the turn unless it already ended."""
        from agentos.engine.runtime import _terminal_done_for

        runner, session_key = self._runner, run.request.session_key
        error_code, error_message = sanitize_agent_error(
            {
                "status": "failed",
                "terminal_reason": "error",
                "error_class": type(exc).__name__,
                "error_message": str(exc),
            },
            fallback_error_class="agent_error",
            fallback_error_message=str(exc) or "Agent error",
        )
        event_code = (
            error_code
            if error_code in {"provider_request_too_large", "provider_output_truncated"}
            else "agent_error"
        )
        log.error("turn_runner.failed", session_key=session_key, error=str(exc), exc_info=True)
        if runner._session_manager is not None:
            if event_code == "provider_output_truncated":
                transcript_message = build_terminal_reply(
                    {
                        "status": "failed",
                        "terminal_reason": "output_truncated",
                        "error_class": event_code,
                        "error_message": error_message,
                    }
                )
            else:
                transcript_message = f"Error: {error_message}"
            await runner._append_session_message(
                session_key, role="system", content=transcript_message
            )
        if run.turn_call_logger is not None:
            run.turn_call_logger.write(
                "turn_error", {"error_type": type(exc).__name__, "error": str(exc)}
            )
        run.emit(
            runner,
            "turn_error",
            seq=2,
            payload={"error_type": type(exc).__name__, "error_chars": len(str(exc))},
        )
        if not run.done_yielded:
            # After the turn's DoneEvent nothing more is emitted: the reply
            # already reached the client, so a later failure (persisting,
            # rollup) is recorded above but does not re-open the turn.
            runner_error = ErrorEvent(message=error_message, code=event_code)
            yield runner_error
            run.done_yielded = True
            yield _terminal_done_for(runner_error)

    def _close_run(self, run: _TurnRun) -> None:
        """Every exit path: release the budget reservation, detach progress, log timing."""
        # A reservation that outlived its turn would permanently shrink the
        # ceiling for everyone else.
        self._runner._release_spend_budget(run.budget_reservation)
        if run.turn_progress_agent is not None:
            run.turn_progress_agent.set_turn_progress_sink(None)
        emit_turn_timing_log(run.timing, session_key=run.request.session_key, turn_id=run.turn_id)


__all__ = ["TurnHarness", "TurnRequest", "TurnStages"]
