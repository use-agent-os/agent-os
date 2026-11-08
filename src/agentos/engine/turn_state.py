"""Per-turn state of ``Agent``'s loop, shared by its step methods.

These were the locals of ``Agent._turn_generator`` before the loop was split
into steps (#3817). The attributes are bound as the turn runs, in the same
order the locals were: turn-wide ones by ``Agent._turn_prepare``, the
iteration's by ``before_request``, and the attempt's by the attempt steps.
Reading one before its step has bound it is a bug, exactly as reading the
local before assignment was, so none of them has a default.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from enum import Enum
from typing import TYPE_CHECKING, Any

from agentos.engine.core.loop import LoopState

if TYPE_CHECKING:
    from agentos.engine.agent import _ProviderAttemptKind, _ProviderRetryPolicy
    from agentos.engine.cache_break_monitor import PromptStateSnapshot
    from agentos.engine.core.stream import StreamAccumulator
    from agentos.engine.fallback import FallbackPolicy
    from agentos.engine.policy import AttemptHistory, ProviderAttemptPolicy
    from agentos.engine.progress_watchdog import ProgressDecision, ProgressWatchdog
    from agentos.engine.repetition_guard import RepetitionTrip, StreamRepetitionGuard
    from agentos.engine.steering import SteeringSource
    from agentos.engine.text_tool_call_detect import TextToolCallStreamGate
    from agentos.engine.tool_batch import ToolBatchExecutor
    from agentos.engine.types import ToolResult
    from agentos.engine.usage import SessionUsage
    from agentos.engine.verification_ledger import VerificationLedger
    from agentos.provider import ChatConfig, Message, ToolDefinition
    from agentos.provider import DoneEvent as ProviderDoneEvent
    from agentos.provider import ErrorEvent as ProviderErrorEvent


class AttemptFlow(Enum):
    """How an attempt step left the provider retry loop."""

    #: Send the request again (a retry, continuation, failover or compaction).
    RETRY = "retry"
    #: Leave the retry loop: the response is usable, or the turn failed.
    EXIT = "exit"


class TurnState(LoopState):
    """The mutable state of one ``Agent`` turn."""

    # -- turn ---------------------------------------------------------------
    thinking_enabled: bool
    thinking_budget: int
    thinking_fallback_done: bool
    chat_cfg: ChatConfig
    turn_messages: list[Message]
    turn_input_index: int
    sealed_len: int
    steering: SteeringSource | None
    runtime_context: str
    runtime_context_hash: str
    log: Any
    loop: asyncio.AbstractEventLoop
    total_deadline: float | None
    tools_supported: bool
    provider_tool_definitions: list[ToolDefinition] | None
    fallback: FallbackPolicy
    retry_after_cap_s: float
    attempt_policy: ProviderAttemptPolicy
    progress_watchdog: ProgressWatchdog
    verification_ledger: VerificationLedger
    overflow_retries: int
    usage_turn_baseline: SessionUsage | None
    total_input_tokens: int
    total_output_tokens: int
    total_reasoning_tokens: int
    total_cached_tokens: int
    total_cache_write_tokens: int
    total_billed_cost: float
    turn_llm_calls: int
    turn_tool_errors: int
    last_actual_model: str
    final_text_parts: list[str]
    final_reasoning_parts: list[str]
    turn_stop_reason: str | None
    turn_stop_details: dict[str, Any] | None
    artifact_delivery_final_response_pending: bool
    artifact_delivery_degraded_final_response: bool
    artifact_delivery_final_response_artifacts: list[dict[str, Any]]
    max_iterations_finalization_attempted: bool
    max_iterations_finalization_pending: bool
    max_iterations_finalization_message: Message | None
    assistant_text_parts: list[str]

    # -- iteration ----------------------------------------------------------
    pending_tools: dict[str, StreamAccumulator]
    iter_input_tokens: int
    iter_output_tokens: int
    iter_reasoning_tokens: int
    iter_reasoning_content: str | None
    iter_thinking_signature: str | None
    provider_error: ProviderErrorEvent | None
    retry_attempt: int
    call_attempt: int
    attempt_history: AttemptHistory
    attempt_started_at: float
    retry_policy: _ProviderRetryPolicy
    attempt_retries_used: dict[_ProviderAttemptKind, int]
    text_tool_call_nudge: list[Message]
    invalid_response_fallback_done: bool
    pause_turn_resumes: int
    thinking_shown_attempt: int | None
    attempt_flow: AttemptFlow | None

    # -- attempt ------------------------------------------------------------
    got_error: bool
    got_done_event: bool
    provider_done_for_log: ProviderDoneEvent | None
    provider_error_for_log: ProviderErrorEvent | None
    call_id: str
    call_started_at: float
    provider_tools_for_call: list[ToolDefinition] | None
    tools_supported_for_call: bool
    ignored_post_delivery_tool_use: bool
    request_messages: list[Message]
    call_chat_cfg: ChatConfig
    estimated_context_tokens: int
    cache_prompt_snapshot: PromptStateSnapshot | None
    text_call_gate: TextToolCallStreamGate | None
    repetition_guard: StreamRepetitionGuard | None
    repetition_trip: RepetitionTrip | None
    attempt_reasoning_parts: list[str]
    attempt_user_visible_emitted: bool
    raw_stream: AsyncIterator[Any]
    deadline_stream: AsyncIterator[Any]
    timed_stream: AsyncIterator[Any]
    response_text: str
    post_tool_turn: bool

    # -- response and tools -------------------------------------------------
    visible_text: str
    preflight_tool_results: dict[str, ToolResult]
    terminal_projection_preflight_error: bool
    tool_deadline: float
    loop_block: ProgressDecision | None
    tool_batch: ToolBatchExecutor


__all__ = ["AttemptFlow", "TurnState"]
