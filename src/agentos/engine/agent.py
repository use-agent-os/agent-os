"""Agent core — explicit state machine + tool loop.

The loop's order lives in ``agentos.engine.core.run_agent_loop``; ``Agent``
is the facade that prepares a turn and supplies the loop's steps
(``_turn_*`` and ``_attempt_*``). No recursive calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from functools import partial
from typing import Any

import structlog

from agentos.artifacts import artifact_payload
from agentos.context_budget import ContextBudgetGovernor
from agentos.engine.approval_port import (
    ApprovalWaiter,
    default_approval_waiter,
    wait_for_pending_approval,
)
from agentos.engine.cache_break_monitor import (
    check_response_for_cache_break,
    record_prompt_state,
)
from agentos.engine.compaction_coordinator import CompactionCoordinator, CompactionTrigger
from agentos.engine.compaction_service import CompactionService
from agentos.engine.core.loop import LoopConfig, run_agent_loop
from agentos.engine.core.stream import (
    TOOL_ARGUMENT_HEARTBEAT_CHARS,
    AttemptResult,
    IterationStreamTimeoutError,
    StreamAccumulator,
    stream_attempt,
)
from agentos.engine.core.turn_usage import (
    TurnTotals,
    cost_source_for_usage,
    resolve_done_model,
    settle_turn_usage,
)
from agentos.engine.estimate_calibration import EstimateCalibration, request_has_media
from agentos.engine.fallback import FallbackPolicy, backoff_sleep
from agentos.engine.history import limit_turns, repair_tool_pairing
from agentos.engine.hooks.types import Amend, RequestHook, RequestHookCall, Veto
from agentos.engine.loop_guard import (
    append_guidance,
    blocked_batch_results,
    loop_detected_error,
    loop_guard_enforced,
    loop_guard_log_fields,
)
from agentos.engine.outcome import completed_outcome, outcome_from_error
from agentos.engine.policy import (
    AttemptBudget,
    AttemptHistory,
    CompactThenRetry,
    Fail,
    Failover,
    ProviderAttemptPolicy,
    Retry,
)
from agentos.engine.progress_watchdog import (
    ProgressObservation,
    ProgressWatchdog,
    proposed_tool_call,
    tool_call_signature,
)
from agentos.engine.repetition_guard import StreamRepetitionGuard, kept_text
from agentos.engine.session_sanitize import (
    sanitize_session_messages,
    session_payload_chars,
)
from agentos.engine.steering import steering_for, steering_pending, take_steering
from agentos.engine.text_tool_call_detect import (
    TextToolCallStreamGate,
    offered_tool_names,
    text_tool_call_nudge,
    text_tool_call_to_retry,
)
from agentos.engine.thinking import drop_reasoning
from agentos.engine.tokenjuice_adapter import reduce_tool_result_with_tokenjuice
from agentos.engine.tool_batch import ToolBatchExecutor, registry_spec_lookup
from agentos.engine.tool_projection import (
    ToolResultProjector,
    _pending_approval_payload,
)
from agentos.engine.tool_text_compat import strip_synthetic_tool_call_suffix
from agentos.engine.tool_token_estimate import estimate_tokens as get_approx_tokens
from agentos.engine.transcript import (
    RUNTIME_CONTEXT_HEADER,
    SKILLS_CONTEXT_HEADER,
    MessageSizeCache,
    TranscriptRecord,
    append_context_blocks,
    encode_transcript_record,
    latest_context_block_present,
    persistable_messages,
    request_context_delta,
    seal_window,
)
from agentos.engine.turn_progress import (
    PHASE_ROUND_COMPLETE,
    PHASE_TOOLS_RUNNING,
    TurnProgress,
    TurnProgressPhase,
    TurnProgressSink,
    current_turn_stop_signal,
    turn_abort_requested,
)
from agentos.engine.turn_state import AttemptFlow, TurnState
from agentos.engine.turn_timing import NULL_TURN_TIMING, TurnTiming
from agentos.engine.verification_ledger import (
    MUTATING_TOOL_NAMES as _MUTATING_TOOL_NAMES,
)
from agentos.engine.verification_ledger import (
    VerificationLedger,
    build_verification_nudge,
)
from agentos.engine.verification_ledger import (
    mutated_paths as _mutated_paths,
)
from agentos.execution_status import (
    runtime_execution_status,
)
from agentos.observability.turn_call_log import TurnCallLogger
from agentos.provider import (
    ChatConfig,
    ContentBlockText,
    ContentBlockThinking,
    ContentBlockToolResult,
    ContentBlockToolUse,
    LLMProvider,
    Message,
    ToolDefinition,
    provider_metadata,
)
from agentos.provider import (
    DoneEvent as ProviderDoneEvent,
)
from agentos.provider import (
    ErrorEvent as ProviderErrorEvent,
)
from agentos.provider.failures import (
    RETRY_AFTER_KINDS,
    ProviderFailureKind,
    classify_provider_error,
    is_transport_timeout,
)
from agentos.provider.replay import provenance_for_provider
from agentos.provider.request_proof import effective_proof_budget
from agentos.provider.types import ContentBlockImage, StopReason, normalize_stop_reason
from agentos.session.terminal_reply import build_terminal_reply
from agentos.tool_boundary import AgentToolHandler as ToolHandler
from agentos.tools.registry import ToolRegistry
from agentos.tools.types import ToolContext

from .context import ContextAssembly
from .types import (
    AgentConfig,
    AgentEvent,
    AgentState,
    ArtifactEvent,
    CompactionEvent,
    CompactionOutcome,
    DoneEvent,
    ErrorEvent,
    RepetitionStopEvent,
    StateChangeEvent,
    TextDeltaEvent,
    ThinkingLevel,
    ToolCall,
    ToolResult,
    WarningEvent,
)

logger = structlog.get_logger("agentos.engine.agent")

#: Result content of a tool call the turn was cancelled before it finished.
CANCELLED_TOOL_RESULT_CONTENT = (
    "Cancelled: the user stopped the turn before this call finished. It may have "
    "partly run; check its effects before relying on them."
)
#: Closing assistant text of a cancelled turn, replayed to the model.
TURN_INTERRUPTED_NOTE = (
    "[The user interrupted this turn before it finished. Tool calls may have partly run.]"
)

_PROVIDER_OUTPUT_TRUNCATED_REPLY = build_terminal_reply(
    {
        "status": "failed",
        "terminal_reason": "output_truncated",
        "error_class": "provider_output_truncated",
        "error_message": "Provider output limit reached before completion",
    }
)
_PROVIDER_OUTPUT_CONTINUE_PROMPT = (
    "The previous provider response reached its output limit before the task finished. "
    "Continue from the exact point where it stopped. Do not repeat text that has already "
    "been written. If a tool call was interrupted or incomplete, regenerate a complete "
    "tool call from scratch."
)
_PROVIDER_PAUSE_TURN_RESUME_PROMPT = (
    "The previous provider response was paused before the turn finished. Continue from "
    "the exact point where it paused. Do not repeat text that has already been written."
)
# A provider may pause a long-running turn (Anthropic ``pause_turn``). The engine
# resumes it at most this many times per loop iteration, then treats the paused
# response as the end of the turn.
_MAX_PAUSE_TURN_RESUMES = 3
_PROVIDER_REFUSAL_REPLY = "The model declined to respond to this request."
_REPETITION_CONTINUE_PROMPT = (
    "The previous response started repeating the same output over and over, so it was "
    "stopped and the repeated part was removed. Continue the task from where the kept text "
    "ends. Do not repeat anything already written; if nothing new remains, finish with a "
    "short conclusion. If a tool call was dropped, issue it again in full."
)
_REPETITION_STOPPED_REPLY = "The model kept repeating the same output, so the response was stopped."
# CompactionService outcomes that leave the transcript untouched because
# compacting would not help this turn; not retried at the same size.
_COMPACTION_DECLINE_REASONS = frozenset(
    {
        "fixed_context_too_large",
        "current_turn_too_large",
        "not_worth_compacting",
        "compaction_not_smaller",
    }
)


_cost_source_for_usage = cost_source_for_usage


def _is_deepseek_model_id(model_id: str | None) -> bool:
    normalized = (model_id or "").strip().lower()
    return normalized.startswith("deepseek") or "/deepseek" in normalized


def _is_direct_deepseek_v4_model_id(model_id: str | None) -> bool:
    normalized = (model_id or "").strip().lower()
    return normalized in {"deepseek-v4-flash", "deepseek-v4-pro"}


_TOOL_ARGUMENT_HEARTBEAT_CHARS = TOOL_ARGUMENT_HEARTBEAT_CHARS
_PROVIDER_CONTEXT_REPAIR_PROMPT = (
    "A previous tool call was rejected because it reused provider-only compacted "
    "tool arguments. Regenerate the complete tool arguments from the available "
    "source context and retry the tool call. Do not copy compacted placeholders."
)
_LARGE_CONTEXT_INVALID_RESPONSE_INPUT_TOKENS = 30_000


def _positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _is_threshold_denial(result: ToolResult) -> bool:
    try:
        payload = json.loads(result.content)
    except Exception:
        return False
    return (
        isinstance(payload, dict)
        and payload.get("status") == "denied"
        and payload.get("reason") == "threshold_exceeded"
    )


async def _await_outside_slot(awaitable: Awaitable[Any]) -> Any:
    """Identity wrapper for an inline approval wait, so timing can wrap it."""

    return await awaitable


def _artifact_event_kwargs(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "kind",
        "id",
        "sha256",
        "name",
        "mime",
        "size",
        "session_id",
        "session_key",
        "source",
        "created_at",
        "download_url",
        "store",
    }
    return {key: value for key, value in artifact_payload(payload).items() if key in allowed}


def _message_has_tool_result(message: Message | None) -> bool:
    if message is None or not isinstance(message.content, list):
        return False
    return any(getattr(block, "type", None) == "tool_result" for block in message.content)


def _forced_tool_name(tool_choice: Any) -> str | None:
    """Return the tool name a forced ``tool_choice`` targets, else ``None``.

    Handles the OpenAI-style ``{"type": "function", "function": {"name": ...}}``
    and the Anthropic-native ``{"type": "tool", "name": ...}`` forced-tool
    shapes. ``auto``/``any``/``none`` (and non-dict values) target no specific
    tool and return ``None``.
    """
    if not isinstance(tool_choice, dict):
        return None
    choice_type = tool_choice.get("type")
    if choice_type == "function":
        name = (tool_choice.get("function") or {}).get("name")
        return str(name) if name else None
    if choice_type == "tool":
        name = tool_choice.get("name")
        return str(name) if name else None
    return None


def _forced_tool_present(tool_choice: Any, tools: list[ToolDefinition] | None) -> bool:
    """Whether a forced ``tool_choice`` names a tool actually present in ``tools``.

    Forcing a specific tool that is absent from the request's tool list is a hard
    400 on Anthropic (and a silent no-op on openai_compat). Guarding on presence
    keeps a forced ``tool_choice`` a no-op — not an error — whenever the
    tool was filtered out of the call, uniformly across providers. A ``tool_choice``
    that targets no specific tool (``auto``/``any``/``none``) always passes.
    """
    forced_name = _forced_tool_name(tool_choice)
    if forced_name is None:
        return True
    return any(getattr(t, "name", None) == forced_name for t in (tools or []))


def _append_length_capped_continuation(
    turn_messages: list[Message],
    *,
    response_text: str,
    tool_calls: list[ToolCall],
    prompt: str = _PROVIDER_OUTPUT_CONTINUE_PROMPT,
) -> str:
    visible_text = strip_synthetic_tool_call_suffix(
        response_text,
        [tc.tool_name for tc in tool_calls if tc.synthetic_from_text],
    )
    if visible_text:
        turn_messages.append(
            Message(role="assistant", content=[ContentBlockText(text=visible_text)])
        )
    turn_messages.append(Message(role="user", content=prompt))
    return visible_text


def _refusal_category(stop_details: dict[str, Any] | None) -> str | None:
    if not isinstance(stop_details, dict):
        return None
    category = stop_details.get("category")
    if isinstance(category, str) and category.strip():
        return category.strip()
    return None


def _refusal_warning_message(stop_details: dict[str, Any] | None) -> str:
    category = _refusal_category(stop_details)
    if category:
        return (
            f"The model declined this request (safety category: {category}). "
            "The turn ended without a retry."
        )
    return "The model declined this request. The turn ended without a retry."


class _ProviderAttemptKind(StrEnum):
    OK = "ok"
    REASONING_ONLY = "reasoning_only"
    MALFORMED_EMPTY = "malformed_empty"
    INCOMPLETE_TOOLS = "incomplete_tools"
    STREAM_INCOMPLETE = "stream_incomplete"
    LENGTH_CAPPED = "length_capped"
    # The provider declined (safety classifier / content filter). Final: never
    # retried and never failed over, the turn ends with what was produced.
    REFUSAL = "refusal"
    # The provider paused a long-running turn and expects it to be resumed.
    PAUSE_TURN = "pause_turn"
    # The reply wrote a tool call out as text and nothing ran. Detected by the
    # engine, not by the classifier: such a reply classifies as OK.
    TEXT_TOOL_CALL = "text_tool_call"
    # The engine cut a response that degenerated into repeated output.
    REPETITION = "repetition_detected"


_IterationStreamTimeoutError = IterationStreamTimeoutError


class _TurnAbortedError(Exception):
    """The turn was asked to stop (cooperative abort) and should end now."""


def _raise_if_turn_aborted() -> None:
    if turn_abort_requested():
        raise _TurnAbortedError


async def _sleep_unless_aborted(delay: float) -> None:
    """Wait *delay* seconds between attempts; end the turn if it is asked to stop.

    A retry backoff is a checkpoint like the one before every request: an
    abort that lands while the turn waits must not be followed by another
    provider request.
    """

    _raise_if_turn_aborted()
    signal = current_turn_stop_signal()
    if signal is None:
        await asyncio.sleep(delay)
        return
    sleeper = asyncio.ensure_future(asyncio.sleep(delay))
    aborter = asyncio.ensure_future(signal.wait_abort())
    try:
        await asyncio.wait({sleeper, aborter}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        sleeper.cancel()
        aborter.cancel()
        await asyncio.gather(sleeper, aborter, return_exceptions=True)
    _raise_if_turn_aborted()


def _is_large_context_invalid_response(
    kind: _ProviderAttemptKind,
    *,
    input_tokens: int,
) -> bool:
    return (
        kind
        in {
            _ProviderAttemptKind.REASONING_ONLY,
            _ProviderAttemptKind.MALFORMED_EMPTY,
        }
        and input_tokens >= _LARGE_CONTEXT_INVALID_RESPONSE_INPUT_TOKENS
    )


@dataclass(frozen=True)
class _ProviderAttemptClassification:
    kind: _ProviderAttemptKind
    stop_reason: str | None = None
    user_visible_emitted: bool = False


@dataclass(frozen=True)
class _ProviderRetryPolicy:
    max_provider_retries: int
    attempt_budgets: dict[_ProviderAttemptKind, int]
    provider_failure_budgets: dict[ProviderFailureKind, int]

    @classmethod
    def from_provider_budget(
        cls,
        max_provider_retries: int,
        *,
        length_capped_continuations: int = 1,
    ) -> _ProviderRetryPolicy:
        length_capped_continuations = max(1, length_capped_continuations)
        return cls(
            max_provider_retries=max_provider_retries,
            attempt_budgets={
                _ProviderAttemptKind.REASONING_ONLY: 1,
                _ProviderAttemptKind.MALFORMED_EMPTY: 1,
                _ProviderAttemptKind.STREAM_INCOMPLETE: 1,
                _ProviderAttemptKind.LENGTH_CAPPED: length_capped_continuations,
                _ProviderAttemptKind.TEXT_TOOL_CALL: 1,
                _ProviderAttemptKind.REPETITION: 1,
            },
            provider_failure_budgets={ProviderFailureKind.EMPTY_RESPONSE: 1},
        )

    def used_attempts(self) -> dict[_ProviderAttemptKind, int]:
        return {kind: 0 for kind in self.attempt_budgets}

    def can_retry_attempt(
        self,
        kind: _ProviderAttemptKind,
        used: dict[_ProviderAttemptKind, int],
    ) -> bool:
        return self.max_provider_retries > 0 and used.get(kind, 0) < self.attempt_budgets.get(
            kind, 0
        )

    def can_retry_provider_failure(
        self,
        failure_kind: ProviderFailureKind,
        *,
        post_tool_turn: bool,
        provider_retry_attempt: int,
    ) -> bool:
        if failure_kind is ProviderFailureKind.EMPTY_RESPONSE:
            return (
                post_tool_turn
                and self.max_provider_retries > 0
                and provider_retry_attempt
                < self.provider_failure_budgets.get(failure_kind, self.max_provider_retries)
            )
        return provider_retry_attempt < self.max_provider_retries


_COMMAND_TOOL_NAMES = frozenset({"exec_command", "execute_code", "background_process"})
# Sessions whose surface is a person in a chat rather than a checkout. There is
# usually no test command to run there, so the verification nudge is withheld.
_MESSAGING_SESSION_MARKERS = (
    "telegram",
    "slack",
    "discord",
    "msteams",
    "email",
    "whatsapp",
    "signal",
)


def _is_messaging_surface(session_key: str | None) -> bool:
    key = str(session_key or "").casefold()
    return any(marker in key for marker in _MESSAGING_SESSION_MARKERS)


def _classify_provider_attempt(
    *,
    text: str,
    tool_calls: list[ToolCall],
    pending_tools: dict[str, _StreamAccumulator],
    got_done_event: bool,
    stop_reason: str | None,
    reasoning_content: str | None,
    reasoning_tokens: int,
    user_visible_emitted: bool,
) -> _ProviderAttemptClassification:
    visible_text = bool(text.strip())
    if pending_tools:
        return _ProviderAttemptClassification(
            _ProviderAttemptKind.INCOMPLETE_TOOLS,
            stop_reason=stop_reason,
            user_visible_emitted=user_visible_emitted,
        )
    if not got_done_event:
        return _ProviderAttemptClassification(
            _ProviderAttemptKind.STREAM_INCOMPLETE,
            stop_reason=stop_reason,
            user_visible_emitted=user_visible_emitted,
        )
    normalized_stop = normalize_stop_reason(stop_reason)
    if normalized_stop is StopReason.REFUSAL:
        return _ProviderAttemptClassification(
            _ProviderAttemptKind.REFUSAL,
            stop_reason=stop_reason,
            user_visible_emitted=user_visible_emitted,
        )
    if normalized_stop is StopReason.MAX_TOKENS:
        return _ProviderAttemptClassification(
            _ProviderAttemptKind.LENGTH_CAPPED,
            stop_reason=stop_reason,
            user_visible_emitted=user_visible_emitted,
        )
    if normalized_stop is StopReason.PAUSE_TURN and not tool_calls:
        return _ProviderAttemptClassification(
            _ProviderAttemptKind.PAUSE_TURN,
            stop_reason=stop_reason,
            user_visible_emitted=user_visible_emitted,
        )
    if visible_text or tool_calls:
        return _ProviderAttemptClassification(
            _ProviderAttemptKind.OK,
            stop_reason=stop_reason,
            user_visible_emitted=user_visible_emitted,
        )
    if (reasoning_content and reasoning_content.strip()) or reasoning_tokens > 0:
        return _ProviderAttemptClassification(
            _ProviderAttemptKind.REASONING_ONLY,
            stop_reason=stop_reason,
            user_visible_emitted=user_visible_emitted,
        )
    return _ProviderAttemptClassification(
        _ProviderAttemptKind.MALFORMED_EMPTY,
        stop_reason=stop_reason,
        user_visible_emitted=user_visible_emitted,
    )


def _chat_config_with_thinking_disabled(chat_cfg: ChatConfig) -> ChatConfig:
    # model_copy keeps every other field (cache TTL, tool choice, betas, ...)
    # instead of a hand-written list that silently drops new ones.
    return chat_cfg.model_copy(
        update={"thinking": False, "thinking_budget_tokens": 0, "thinking_level": None}
    )


def _strip_historical_image_blocks(messages: list[Message]) -> list[Message]:
    """Remove image payload blocks from history before provider calls.

    Current-turn uploads are passed through ``extra_messages`` and are not part
    of the history list sanitized here. This prevents a later text follow-up
    from replaying stale image input to a text-only route.
    """
    sanitized: list[Message] = []
    for msg in messages:
        content = msg.content
        if not isinstance(content, list):
            sanitized.append(msg)
            continue

        kept: list[Any] = []
        omitted: list[str] = []
        for block in content:
            if isinstance(block, ContentBlockImage):
                media_type = block.media_type or "image"
                omitted.append(f"[historical image omitted: {media_type}]")
                continue
            kept.append(block)

        if not omitted:
            sanitized.append(msg)
            continue

        kept.extend(ContentBlockText(text=marker) for marker in omitted)
        sanitized.append(
            Message(
                role=msg.role,
                content=kept,
                reasoning_content=msg.reasoning_content,
                reasoning_details=msg.reasoning_details,
                provenance=msg.provenance,
            )
        )
    return sanitized


_StreamAccumulator = StreamAccumulator


class Agent:
    """Explicit state-machine agent.

    Lifecycle per turn:
      IDLE -> THINKING -> STREAMING -> [TOOL_CALLING -> THINKING -> ...] -> DONE
      Any step can transition to ERROR.
    """

    #: Per-turn timing recorder; the runtime assigns a fresh one per turn.
    turn_timing: TurnTiming = NULL_TURN_TIMING
    # Runtime-owned estimator calibration (set by the TurnRunner before the
    # turn runs); ``None`` keeps the raw estimate. Last request's size minus
    # its messages (system prompt, tools, config), for the between-turns
    # proactive compaction check.
    estimate_calibration: EstimateCalibration | None = None
    _last_request_overhead_tokens: int | None = None

    def __init__(
        self,
        provider: LLMProvider,
        config: AgentConfig | None = None,
        tool_definitions: list[ToolDefinition] | None = None,
        tool_handler: ToolHandler | None = None,
        usage_tracker: Any | None = None,
        session_key: str | None = None,
        turn_call_logger: TurnCallLogger | None = None,
        memory_sync_manager: Any | None = None,
        tool_registry: ToolRegistry | None = None,
        tool_context: ToolContext | None = None,
        spend_budget_guard: Callable[[str], tuple[bool, str | None]] | None = None,
        request_hooks: Sequence[RequestHook] | None = None,
        approval_waiter: ApprovalWaiter | None = None,
    ) -> None:
        self.provider = provider
        # Waits for a tool call's pending approval (``engine.approval_port``).
        self._approval_waiter: ApprovalWaiter = approval_waiter or default_approval_waiter()
        self.config = config or AgentConfig()
        self.tool_definitions = tool_definitions or []
        self._tool_definition_by_name = {tool.name: tool for tool in self.tool_definitions}
        self.tool_handler = tool_handler
        self._usage_tracker = usage_tracker
        self._session_key = session_key
        self._turn_call_logger = turn_call_logger
        self._tool_registry: ToolRegistry | None = tool_registry
        self._tool_context: ToolContext | None = tool_context
        # Cumulative spend ceilings, re-read between iterations. The turn-start
        # gate alone would let one turn with an unbounded tool loop run
        # arbitrarily far past a ceiling, which is the shape a runaway
        # actually takes.
        self._spend_budget_guard = spend_budget_guard
        self._request_hooks: tuple[RequestHook, ...] = tuple(request_hooks or ())
        self._pending_warnings: list[WarningEvent] = []

        self._state: AgentState = AgentState.IDLE
        self._history: list[Message] = []
        self._context: ContextAssembly | None = None
        # Typed dependency surface. Either constructor injection or legacy
        # attribute assignment from the runtime is accepted; both reach the same
        # internal slot.
        self._memory_sync_manager: Any | None = memory_sync_manager

        self._last_compaction_refusal_reason: str | None = None
        self._tool_failure_loop_counts: dict[tuple[str, str], int] = {}
        # Append-only transcript state for the current/last turn.
        self._transcript_input_entry_id: str | None = None
        self._transcript_input_entry_content: str | None = None
        self._turn_base_len = 0
        self._turn_reset = False
        self._turn_compaction_id: str | None = None
        self._turn_record: TranscriptRecord | None = None
        self._turn_compaction_declined_tokens: int | None = None
        # User rows steered into the current turn (agentos.engine.steering).
        self._turn_steered_entry_ids: list[str] = []
        self._message_sizes = MessageSizeCache(Agent._live_request_jsonable)
        # Tool-result projection and capping, composed (``engine.tool_projection``).
        self._projection = ToolResultProjector(self)
        # Durable mid-turn progress (``agentos.engine.turn_progress``); set by
        # the runtime for the turn it runs, ``None`` when nothing persists.
        self._turn_progress_sink: TurnProgressSink | None = None

    _positive_int = staticmethod(ToolResultProjector._positive_int)

    def _context_overflow_error(self) -> ErrorEvent:
        reason = self._last_compaction_refusal_reason
        if reason == "empty_summary_rejected":
            return ErrorEvent(
                message="Context compaction produced no replacement summary.",
                code="compaction_refused_empty_summary",
            )
        if reason == "current_turn_too_large":
            return ErrorEvent(
                message=(
                    "This turn alone is too large for the model's context window, so "
                    "summarizing earlier history cannot make it fit. Retry with a "
                    "narrower request or a larger-context model."
                ),
                code="provider_request_too_large",
            )
        if reason == "compaction_failed":
            return ErrorEvent(
                message="Context compaction failed before the provider request could be retried.",
                code="compaction_failed",
            )
        if reason == "compaction_not_smaller":
            return ErrorEvent(
                message="Context compaction did not reduce the provider request.",
                code="compaction_not_smaller",
            )
        if reason == "provider_recent_tail_too_large":
            return ErrorEvent(
                message=(
                    "The request is too large for the provider context window after "
                    "automatic context compaction and payload reduction. AgentOS "
                    "preserved the recoverable state; retry with a narrower request "
                    "or a larger-context model."
                ),
                code="provider_request_too_large",
            )
        if reason == "provider_request_budget_exhausted":
            return ErrorEvent(
                message=(
                    "The request is too large for the provider context window after "
                    "automatic context compaction and payload reduction. AgentOS "
                    "preserved the recoverable state; retry with a narrower request "
                    "or a larger-context model."
                ),
                code="provider_request_too_large",
            )
        return ErrorEvent(
            message="Context overflow persists after compaction",
            code="compaction_exhausted",
        )

    def _record_provider_context_overflow_reason(
        self,
        provider_error: ProviderErrorEvent,
    ) -> None:
        if provider_error.code != "provider_request_budget_exhausted":
            return
        proof = self._provider_request_budget_proof(provider_error)
        if proof is None:
            self._last_compaction_refusal_reason = "provider_request_budget_exhausted"
            return
        if proof.get("recent_tail_too_large") is True:
            self._last_compaction_refusal_reason = "provider_recent_tail_too_large"
            return
        if proof.get("compaction_not_smaller") is True:
            self._last_compaction_refusal_reason = "compaction_not_smaller"
            return
        fallback_reason = proof.get("fallback_reason")
        if fallback_reason == "provider_request_budget_exhausted":
            self._last_compaction_refusal_reason = "provider_request_budget_exhausted"

    @staticmethod
    def _provider_request_budget_proof(
        provider_error: ProviderErrorEvent,
    ) -> dict[str, Any] | None:
        if provider_error.code != "provider_request_budget_exhausted":
            return None
        try:
            proof = json.loads(provider_error.message)
        except (TypeError, ValueError):
            return None
        return proof if isinstance(proof, dict) else None

    def _provider_budget_compaction_window_tokens(
        self,
        provider_error: ProviderErrorEvent,
    ) -> int | None:
        proof = self._provider_request_budget_proof(provider_error)
        if proof is None:
            return None
        proof_budget = self._positive_int(
            proof.get("effective_proof_budget") or proof.get("proof_budget")
        )
        if proof_budget is None:
            return None
        estimated_chars = self._positive_int(proof.get("estimated_chars"))
        estimated_tokens = self._positive_int(proof.get("estimated_tokens"))
        if estimated_chars and estimated_tokens:
            window_tokens = int(proof_budget * (estimated_tokens / estimated_chars))
        else:
            window_tokens = proof_budget // 4
        if window_tokens <= 0:
            return None
        return min(self.config.context_window_tokens, window_tokens)

    def _provider_budget_estimated_tokens(
        self,
        provider_error: ProviderErrorEvent,
    ) -> int | None:
        proof = self._provider_request_budget_proof(provider_error)
        if proof is None:
            return None
        return self._positive_int(proof.get("estimated_tokens"))

    def _provider_request_proof_max_chars(self) -> int:
        return self._context_budget_governor().snapshot().provider_request_max_chars

    def _context_budget_governor(self) -> ContextBudgetGovernor:
        return ContextBudgetGovernor.from_config(self.config)

    def _tool_execution_timeout(self, tool_call: ToolCall) -> float:
        timeout = float(self.config.tool_timeout)
        tool_def = self._tool_definition_by_name.get(tool_call.tool_name)
        if tool_def is None:
            return timeout
        static_timeout = getattr(tool_def, "execution_timeout_seconds", None)
        if static_timeout is not None:
            try:
                timeout = max(timeout, float(static_timeout))
            except (TypeError, ValueError):
                pass
        argument_name = getattr(tool_def, "execution_timeout_argument", None)
        if not argument_name:
            return timeout
        raw_value = tool_call.arguments.get(str(argument_name))
        if raw_value is None:
            return timeout
        try:
            argument_timeout = float(raw_value)
        except (TypeError, ValueError):
            return timeout
        if argument_timeout < 0:
            return timeout
        padding = getattr(tool_def, "execution_timeout_padding", 0.0) or 0.0
        try:
            timeout = max(timeout, argument_timeout + float(padding))
        except (TypeError, ValueError):
            timeout = max(timeout, argument_timeout)
        return timeout

    def _tool_activity_heartbeat_interval(self) -> float:
        raw_interval = self.config.metadata.get("tool_activity_heartbeat_interval", 15.0)
        try:
            return float(raw_interval)
        except (TypeError, ValueError):
            return 15.0

    def _approval_wait_timeout(self) -> float:
        raw_timeout = self.config.metadata.get("approval_wait_timeout_seconds", 180.0)
        try:
            return max(0.0, float(raw_timeout))
        except (TypeError, ValueError):
            return 180.0

    def _max_safe_tool_concurrency(self) -> int:
        try:
            value = int(self.config.max_safe_tool_concurrency)
        except (TypeError, ValueError):
            return 8
        return max(1, value)

    def _write_turn_call_log(self, kind: str, **payload: Any) -> None:
        if self._turn_call_logger is not None:
            self._turn_call_logger.write(kind, payload)

    def _write_context_stage(
        self,
        stage: str,
        messages: list[Message],
        **payload: Any,
    ) -> None:
        if self._turn_call_logger is None:
            return
        self._write_turn_call_log(
            "context_stage",
            stage=stage,
            message_count=len(messages),
            payload_chars=session_payload_chars(messages),
            messages=messages,
            **payload,
        )

    def _switch_to_invalid_response_fallback(self, reason: str) -> bool:
        fallback = getattr(self.provider, "fallback_after_invalid_response", None)
        if not callable(fallback):
            return False
        try:
            return bool(fallback(reason))
        except Exception as exc:  # noqa: BLE001 - fallback support is optional
            logger.warning(
                "provider.invalid_response_fallback_failed",
                session_key=self._session_key,
                reason=reason,
                error=str(exc),
            )
            return False

    def _tokenjuice_max_inline_chars(self, fallback: int | None = None) -> int:
        if fallback is not None and fallback > 0:
            return max(1, int(fallback))
        return max(1, int(self.config.tool_result_projection_max_inline_chars))

    def _tokenjuice_tool_reduction(
        self,
        *,
        tool_name: str,
        content: str,
        is_error: bool,
        tool_use_id: str,
        arguments: dict[str, Any] | None = None,
        command: str | None = None,
        cwd: str | None = None,
        max_inline_chars: int | None = None,
    ) -> str | None:
        reduction = reduce_tool_result_with_tokenjuice(
            tool_name=tool_name,
            content=content,
            is_error=is_error,
            tool_use_id=tool_use_id,
            arguments=arguments,
            command=command,
            cwd=cwd,
            max_inline_chars=self._tokenjuice_max_inline_chars(max_inline_chars),
        )
        if reduction is None:
            return None
        self.config.metadata["tool_projection_backend"] = "tokenjuice"
        if reduction.reducer:
            self.config.metadata["tool_projection_tokenjuice_reducer"] = reduction.reducer
        return reduction.inline_text

    @staticmethod
    def _count_image_blocks(messages: list[Message]) -> int:
        count = 0
        for message in messages:
            if not isinstance(message.content, list):
                continue
            count += sum(1 for block in message.content if isinstance(block, ContentBlockImage))
        return count

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def state(self) -> AgentState:
        return self._state

    def clear_history(self) -> None:
        self._history = []

    def set_history(self, messages: list[Message]) -> None:
        self._history = list(messages)

    async def run_turn(
        self,
        message: str,
        extra_messages: list[Message] | None = None,
        semantic_message: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Run one agent turn, yielding AgentEvents.

        Explicit state machine — no recursion. Tool loop iterates until
        the model finishes, unless config.max_iterations is a positive cap.
        """
        stop_signal = current_turn_stop_signal()
        if stop_signal is not None:
            stop_signal.accepts_abort = True
        try:
            async for event in self._turn_generator(message, extra_messages, semantic_message):
                yield event
        finally:
            if stop_signal is not None:
                stop_signal.accepts_abort = False
                stop_signal.in_tool_batch = False

    async def _turn_generator(
        self,
        message: str,
        extra_messages: list[Message] | None = None,
        semantic_message: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Async generator that drives the state machine.

        The loop is :func:`agentos.engine.core.run_agent_loop`; this method
        prepares the turn, runs the loop over the ``_turn_*`` steps and settles
        the turn: the sealed transcript on cancel or abort, the history on
        success, and the one ``DoneEvent`` that ends every turn.
        """
        self._current_turn_message = message
        # A new message is a fresh request: a call that failed in an earlier
        # turn may be exactly what the user is now asking to try again.
        self._tool_failure_loop_counts.clear()

        # ------ IDLE → THINKING ------
        yield self._transition(AgentState.THINKING)

        t = self._turn_prepare(message, extra_messages, semantic_message)
        turn_aborted = False
        try:
            async with aclosing(run_agent_loop(t, self._turn_loop_config())) as loop_events:
                async for event in loop_events:
                    yield event
        except asyncio.CancelledError:
            # Leave a turn that is valid to replay: every tool call paired with
            # a result, and a note telling the model the turn was cut short.
            # The cancellation itself propagates; the runtime publishes the
            # run's aborted terminal frame.
            sealed = self._seal_cancelled_turn(t.turn_messages, "".join(t.assistant_text_parts))
            self._history = sealed
            self._record_turn_transcript(sealed)
            raise
        except _TurnAbortedError:
            # Asked to stop (``TurnStopSignal.request_abort``): the provider
            # stream is closed and no further request is sent. Same sealed
            # record as a hard cancel, but the turn ends normally with one
            # DoneEvent (outcome interrupted / cancelled) instead of raising.
            turn_aborted = True
            partial = "".join(t.assistant_text_parts)
            if partial.strip():
                t.final_text_parts.append(partial)
            sealed = self._seal_cancelled_turn(t.turn_messages, partial)
            self._history = sealed
            self._record_turn_transcript(sealed)
        except TimeoutError:
            if t.artifact_delivery_final_response_pending:
                yield self._finish_artifact_delivery_degraded(
                    t,
                    reason=f"Agent turn timed out after {self.config.timeout}s",
                    code="agent_runtime_timeout",
                )
            else:
                # Total turn deadline exceeded (raised by manual check above)
                yield self._transition(AgentState.ERROR)
                t.terminal_error = ErrorEvent(
                    message=f"Agent turn timed out after {self.config.timeout}s",
                    code="agent_runtime_timeout",
                )
                yield t.terminal_error

        if t.terminal_error is None and not turn_aborted:
            # Persist successful turns into in-memory history. Error turns are
            # persisted by TurnRunner as system errors, while their usage still
            # flows through the final DoneEvent below when provider usage exists.
            self._seal_transcript(t.turn_messages, t.sealed_len)
            self._projection._cap_turn_for_history(t.turn_messages)
            self._history = list(t.turn_messages)
            self._record_turn_transcript(self._history)
            self._write_context_stage("session:after", self._history)

        # ------ → DONE ------
        tracked_model = None
        if not t.last_actual_model and self._usage_tracker and self._session_key:
            su = self._usage_tracker.get(self._session_key)
            tracked_model = su.model_id if su else None
        done_model = resolve_done_model(t.last_actual_model, tracked_model, self.config.model_id)
        from agentos.engine.pricing import lookup_price

        done_provider = provider_metadata(self.provider)
        price = lookup_price(
            done_model,
            provider_id=done_provider.provider_kind or done_provider.provider_name,
        )
        session_totals = (
            self._usage_tracker.session_snapshot(self._session_key)
            if self._usage_tracker and self._session_key
            else None
        )
        turn_usage_delta = (
            self._usage_tracker.session_delta_snapshot(self._session_key, t.usage_turn_baseline)
            if self._usage_tracker and self._session_key
            else None
        )
        done_usage = settle_turn_usage(
            TurnTotals(
                input_tokens=t.total_input_tokens,
                output_tokens=t.total_output_tokens,
                cached_tokens=t.total_cached_tokens,
                cache_write_tokens=t.total_cache_write_tokens,
                billed_cost=t.total_billed_cost,
            ),
            model=done_model,
            price=price,
            delta=turn_usage_delta,
        )

        # Exactly one DoneEvent ends every turn, after any terminal error, so
        # consumers read the result from ``outcome`` instead of guessing it
        # from which events happened to arrive. A failed turn keeps its ERROR
        # state; only a successful one transitions to DONE.
        if turn_aborted:
            turn_outcome = outcome_from_error(code="cancelled", message="The turn was stopped.")
            yield self._transition(AgentState.DONE)
        elif t.terminal_error is None:
            turn_outcome = completed_outcome()
            yield self._transition(AgentState.DONE)
        else:
            turn_outcome = outcome_from_error(
                code=t.terminal_error.code,
                message=t.terminal_error.message,
            )
        yield DoneEvent(
            text="".join(t.final_text_parts),
            input_tokens=done_usage.input_tokens,
            output_tokens=done_usage.output_tokens,
            reasoning_tokens=t.total_reasoning_tokens,
            cached_tokens=done_usage.cached_tokens,
            cache_write_tokens=done_usage.cache_write_tokens,
            iterations=t.iterations,
            cost_usd=done_usage.cost_usd,
            billed_cost=done_usage.billed_cost,
            cost_source=done_usage.cost_source,
            model=done_usage.model,
            runtime_context_hash=t.runtime_context_hash,
            runtime_context_chars=len(t.runtime_context),
            reasoning_content=(
                "\n".join(t.final_reasoning_parts) if t.final_reasoning_parts else None
            ),
            session_totals=session_totals,
            stop_reason=t.turn_stop_reason,
            stop_details=t.turn_stop_details,
            outcome=turn_outcome,
        )
        # Reset for next turn
        self._state = AgentState.IDLE

    def _turn_loop_config(self) -> LoopConfig[TurnState]:
        return LoopConfig(
            before_request=self._turn_before_request,
            stream=self._turn_model_step,
            commit_response=self._turn_commit_response,
            on_final_answer=self._turn_final_answer,
            execute_tools=self._turn_execute_tools,
            after_tools=self._turn_after_tools,
        )

    # ------------------------------------------------------------------
    # Turn loop steps (run by ``run_agent_loop`` through ``LoopConfig``)
    # ------------------------------------------------------------------

    def _turn_prepare(
        self,
        message: str,
        extra_messages: list[Message] | None,
        semantic_message: str | None,
    ) -> TurnState:
        """Load the history, build this turn's input and the turn-wide state."""
        t = TurnState()
        # Use the system prompt from config (wired by gateway via identity.prompt)
        if self._context is None:
            self._context = ContextAssembly(
                system_prompt=self.config.system_prompt or "",
                workspace_dir=self.config.workspace_dir,
            )

        thinking_prompt = semantic_message if semantic_message is not None else message
        t.thinking_enabled, t.thinking_budget = self.config.resolve_thinking(prompt=thinking_prompt)

        # Prepare the loaded transcript ONCE per turn. Structured history is
        # replayed as persisted (append-only); only model-level incompatibilities
        # (replayed reasoning a model cannot take, stale images) and a single
        # pairing check apply. Nothing below rewrites a message once a provider
        # request has carried it.
        loaded_history = list(self._history)
        self._write_context_stage("session:loaded", loaded_history)
        sanitized_history, sanitize_result = sanitize_session_messages(loaded_history)
        if self._drop_replayed_reasoning(t.thinking_enabled):
            sanitized_history = drop_reasoning(sanitized_history)
        sanitized_history = _strip_historical_image_blocks(sanitized_history)
        sanitized_history = repair_tool_pairing(sanitized_history)
        self._write_context_stage(
            "session:sanitized",
            sanitized_history,
            sanitize=sanitize_result,
        )
        history = limit_turns(sanitized_history, self.config.max_history_turns)
        self._write_context_stage(
            "session:limited",
            history,
            removed_messages=max(len(sanitized_history) - len(history), 0),
        )

        # Build initial message list. The persisted history is the stable
        # prefix; this turn's input is appended after it.
        t.turn_messages = list(history)
        self._turn_base_len = len(t.turn_messages)
        self._turn_reset = False
        self._turn_compaction_id = None
        self._turn_record = None
        self._turn_compaction_declined_tokens = None
        self._turn_steered_entry_ids = []
        # Messages the user sends while this turn runs (gateway turns only).
        t.steering = steering_for(self._session_key)
        # Index of this turn's first input message: compaction keeps it and
        # everything after it (the current turn) and summarizes the rest.
        t.turn_input_index = len(t.turn_messages)
        input_messages: list[Message] = list(extra_messages or [])
        # Only append text message if non-empty (multimodal may use extra_messages instead)
        if message:
            input_messages.append(Message(role="user", content=message))
        t.runtime_context = self._runtime_context_block()
        # Volatile per-turn context (skills list, request context, local time)
        # rides as trailing text of THIS turn's last user message and is
        # persisted with it, so no later request rewrites an older message.
        # The skills list and request context are only repeated when they
        # changed since the copy already in the transcript.
        context_blocks: list[str] = []
        skills_block = self._skills_context_block()
        if skills_block is not None and not latest_context_block_present(
            history, header=SKILLS_CONTEXT_HEADER, block=skills_block
        ):
            context_blocks.append(skills_block)
        # Request context is diffed per ``## <title>`` section: only sections
        # that changed since their latest copy in the transcript ride along.
        request_block = request_context_delta(history, self.config.request_context_prompt)
        if request_block is not None:
            context_blocks.append(request_block)
        context_blocks.append(t.runtime_context)
        last_user_index = next(
            (
                index
                for index in range(len(input_messages) - 1, -1, -1)
                if input_messages[index].role == "user"
            ),
            None,
        )
        if last_user_index is None:
            input_messages.append(Message(role="user", content="\n\n".join(context_blocks)))
        else:
            input_messages[last_user_index] = append_context_blocks(
                input_messages[last_user_index], context_blocks
            )
        t.turn_messages.extend(input_messages)
        # Messages from here on are new this turn; they are sealed (shrunk,
        # checked) right before the first provider request that carries them.
        t.sealed_len = self._turn_base_len
        self._write_context_stage("prompt:before", t.turn_messages)
        self._write_context_stage(
            "prompt:images",
            t.turn_messages,
            image_blocks=self._count_image_blocks(t.turn_messages),
        )
        t.runtime_context_hash = hashlib.sha256(t.runtime_context.encode("utf-8")).hexdigest()[:16]

        t.chat_cfg = ChatConfig(
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            system=self._context.system_prompt,
            thinking=t.thinking_enabled,
            thinking_budget_tokens=t.thinking_budget,
            timeout=self.config.request_timeout,
            stop_sequences=self.config.stop_sequences,
            cache_breakpoints=self._cache_breakpoints_without_runtime_context(
                self.config.cache_breakpoints
            ),
            cache_mode=self.config.cache_mode,
            cache_ttl=self.config.cache_ttl,
            model_capabilities=self.config.model_capabilities,
            thinking_level=(
                self.config.thinking if isinstance(self.config.thinking, ThinkingLevel) else None
            ),
            provider_request_max_chars=self._provider_request_proof_max_chars(),
            tool_choice=None,
            # Only an opt-in: the providers drop it where the API would reject it.
            parallel_tool_calls=True if self.config.parallel_tool_calls else None,
            context_window_tokens=self.config.context_window_tokens,
        )
        t.thinking_fallback_done = False

        t.log = structlog.get_logger("agentos.engine.agent")
        t.iterations = 0
        t.overflow_retries = 0
        # Keep lifetime usage separate from the live context-window gauge.
        # Compaction shrinks what the model sees next; it must not erase the
        # turn's already-spent provider tokens from the final DoneEvent.
        t.total_input_tokens = 0
        t.total_output_tokens = 0
        t.total_reasoning_tokens = 0
        t.total_cached_tokens = 0
        t.total_cache_write_tokens = 0
        t.total_billed_cost = 0.0
        t.usage_turn_baseline = (
            self._usage_tracker.session_checkpoint(self._session_key)
            if self._usage_tracker and self._session_key
            else None
        )
        t.turn_llm_calls = 0
        t.turn_tool_errors = 0
        t.last_actual_model = ""
        t.terminal_error = None
        t.final_text_parts = []
        # Normalized stop reason (and refusal details) of the last provider
        # call that completed, surfaced on the turn's DoneEvent for the
        # decision log.
        t.turn_stop_reason = None
        t.turn_stop_details = None
        t.final_reasoning_parts = []
        t.artifact_delivery_final_response_pending = False
        t.artifact_delivery_degraded_final_response = False
        t.artifact_delivery_final_response_artifacts = []
        t.max_iterations_finalization_attempted = False
        t.max_iterations_finalization_pending = False
        t.max_iterations_finalization_message = None
        # One per turn, so the loop check never carries a block across turns.
        t.progress_watchdog = ProgressWatchdog(
            observe_only=not loop_guard_enforced(getattr(self.config, "loop_guard", "enforce"))
        )
        t.verification_ledger = VerificationLedger()
        t.fallback = FallbackPolicy(
            max_retries=self.config.max_provider_retries,
            base_backoff_ms=self.config.retry_base_backoff_ms,
            max_backoff_ms=self.config.retry_max_backoff_ms,
        )
        t.retry_after_cap_s = float(self.config.rate_limit_retry_after_cap_seconds)
        t.attempt_policy = ProviderAttemptPolicy(
            budget=AttemptBudget(
                max_retries=self.config.max_provider_retries,
                budget_seconds=float(self.config.retry_budget_seconds),
                base_backoff_ms=self.config.retry_base_backoff_ms,
                max_backoff_ms=self.config.retry_max_backoff_ms,
                retry_after_cap_s=t.retry_after_cap_s,
                max_timeout_retries=t.fallback.max_timeout_retries,
                timeout_context_recovery=bool(self.config.timeout_context_recovery),
            )
        )
        # The selector-fallback wrapper decides whether a hinted 429 fails over
        # straight away; it has to use the same cap this loop waits under.
        _adopt_retry_after_cap = getattr(self.provider, "set_retry_after_cap_seconds", None)
        if callable(_adopt_retry_after_cap):
            try:
                _adopt_retry_after_cap(t.retry_after_cap_s)
            except Exception:  # noqa: BLE001 - optional provider hook
                logger.debug("provider.retry_after_cap_hook_failed", exc_info=True)

        # Timeout budgets: optional total turn budget, idle LLM stream budget,
        # and per-tool execution budget.
        t.loop = asyncio.get_running_loop()
        t.total_deadline = t.loop.time() + self.config.timeout if self.config.timeout > 0 else None
        t.tools_supported = True
        if self.config.model_capabilities is not None:
            t.tools_supported = bool(
                getattr(self.config.model_capabilities, "supports_tools", True)
            )
        t.provider_tool_definitions = self.tool_definitions or None
        if not t.tools_supported:
            t.provider_tool_definitions = None
        t.assistant_text_parts = []
        return t

    def _turn_budget_error(self, t: TurnState) -> ErrorEvent | None:
        max_llm_calls = self._positive_int(getattr(self.config, "max_turn_llm_calls", 0))
        if max_llm_calls is not None and t.turn_llm_calls > max_llm_calls:
            return ErrorEvent(
                message=(
                    f"Turn stopped after {t.turn_llm_calls} LLM calls "
                    f"(max_turn_llm_calls={max_llm_calls})."
                ),
                code="turn_llm_call_budget_exceeded",
            )
        max_input = self._positive_int(getattr(self.config, "max_turn_input_tokens", 0))
        if max_input is not None and t.total_input_tokens > max_input:
            return ErrorEvent(
                message=(
                    f"Turn stopped after {t.total_input_tokens} input tokens "
                    f"(max_turn_input_tokens={max_input})."
                ),
                code="turn_input_token_budget_exceeded",
            )
        max_output = self._positive_int(getattr(self.config, "max_turn_output_tokens", 0))
        if max_output is not None and t.total_output_tokens > max_output:
            return ErrorEvent(
                message=(
                    f"Turn stopped after {t.total_output_tokens} output tokens "
                    f"(max_turn_output_tokens={max_output})."
                ),
                code="turn_output_token_budget_exceeded",
            )
        max_cost = _positive_float(getattr(self.config, "max_turn_billed_cost_usd", 0.0))
        if max_cost is not None and t.total_billed_cost > max_cost:
            return ErrorEvent(
                message=(
                    f"Turn stopped after ${t.total_billed_cost:.6f} billed cost "
                    f"(max_turn_billed_cost_usd=${max_cost:.6f})."
                ),
                code="turn_billed_cost_budget_exceeded",
            )
        if self._spend_budget_guard is not None and self._session_key:
            try:
                spend_stop, spend_message = self._spend_budget_guard(self._session_key)
            except Exception as exc:  # noqa: BLE001 - a budget check never fails a turn
                logger.warning("agent.spend_budget_check_failed", error=str(exc))
            else:
                if spend_stop:
                    return ErrorEvent(
                        message=(
                            spend_message or "A configured spend budget limit has been reached."
                        ),
                        code="budget_exceeded",
                    )
        max_tool_errors = self._positive_int(getattr(self.config, "max_turn_tool_errors", 0))
        if max_tool_errors is not None and t.turn_tool_errors >= max_tool_errors:
            return ErrorEvent(
                message=(
                    f"Turn stopped after {t.turn_tool_errors} tool errors "
                    f"(max_turn_tool_errors={max_tool_errors})."
                ),
                code="turn_tool_error_budget_exceeded",
            )
        return None

    def _turn_llm_call_budget_error(self, t: TurnState, next_call_number: int) -> ErrorEvent | None:
        max_llm_calls = self._positive_int(getattr(self.config, "max_turn_llm_calls", 0))
        if max_llm_calls is None or next_call_number <= max_llm_calls:
            return None
        return ErrorEvent(
            message=(
                f"Turn stopped before LLM call {next_call_number} "
                f"(max_turn_llm_calls={max_llm_calls})."
            ),
            code="turn_llm_call_budget_exceeded",
        )

    def _finish_artifact_delivery_degraded(
        self,
        t: TurnState,
        *,
        reason: str,
        code: str,
    ) -> WarningEvent:
        if not "".join(t.final_text_parts).strip():
            t.final_text_parts.append(
                self._artifact_delivery_final_response_text(
                    t.artifact_delivery_final_response_artifacts
                )
            )
        t.artifact_delivery_degraded_final_response = True
        t.artifact_delivery_final_response_pending = False
        self._write_turn_call_log(
            "artifact_final_response_degraded",
            reason=reason,
            code=code,
            artifact_count=len(t.artifact_delivery_final_response_artifacts),
        )
        return WarningEvent(
            code="artifact_delivery_final_response_degraded",
            message=(
                "Artifact delivery completed, but the model could not generate "
                "the final explanatory response. Returning a deterministic "
                "completion message instead."
            ),
        )

    def _finish_artifact_delivery_without_provider(self, t: TurnState) -> None:
        final_response_text = self._artifact_delivery_final_response_text(
            t.artifact_delivery_final_response_artifacts
        )
        current_text = "".join(t.final_text_parts)
        if final_response_text not in current_text:
            prefix = "\n\n" if current_text.strip() else ""
            t.final_text_parts.append(prefix + final_response_text)
        self._write_turn_call_log(
            "artifact_final_response_synthesized",
            reason="publish_artifact_completed",
            artifact_count=len(t.artifact_delivery_final_response_artifacts),
        )

    async def _turn_before_request(self, t: TurnState) -> AsyncGenerator[AgentEvent, None]:
        """Iteration guards, then start the iteration and reset its state."""
        # Cooperative abort checkpoint: before every provider request,
        # which is also right after the previous tool batch settled.
        _raise_if_turn_aborted()
        if self.config.max_iterations > 0 and t.iterations >= self.config.max_iterations:
            max_iterations_source = str(
                self.config.metadata.get("agent_max_iterations_source", "agent_config")
            )
            if max_iterations_source == "session config":
                max_iterations_guidance = "Set session agent_max_iterations=0 for unlimited tasks."
            elif max_iterations_source == "gateway config":
                max_iterations_guidance = "Set gateway agent_max_iterations=0 for unlimited tasks."
            elif max_iterations_source.startswith("env "):
                max_iterations_guidance = "Set AGENTOS_AGENT_MAX_ITERATIONS=0 for unlimited tasks."
            elif max_iterations_source == "explicit argument":
                max_iterations_guidance = (
                    "Pass --max-iterations 0 or max_iterations=0 for unlimited tasks."
                )
            else:
                max_iterations_guidance = "Set AgentConfig.max_iterations=0 for unlimited tasks."
            if not t.max_iterations_finalization_attempted:
                t.max_iterations_finalization_attempted = True
                t.max_iterations_finalization_pending = True
                t.max_iterations_finalization_message = Message(
                    role="user",
                    content=(
                        "The configured iteration limit has been reached. "
                        "Do not call tools. Provide the best concise final "
                        "answer from the work completed so far."
                    ),
                )
                self._write_turn_call_log(
                    "turn_policy_decision",
                    action="finalize_partial",
                    reason="max_iterations",
                    code="max_iterations",
                    iteration=t.iterations,
                    max_iterations=self.config.max_iterations,
                    max_iterations_source=max_iterations_source,
                )
            else:
                self._write_turn_call_log(
                    "turn_policy_decision",
                    action="partial",
                    reason="max_iterations",
                    code="max_iterations",
                    iteration=t.iterations,
                    max_iterations=self.config.max_iterations,
                    max_iterations_source=max_iterations_source,
                )
                t.terminal_error = ErrorEvent(
                    message=(
                        f"Reached max_iterations={self.config.max_iterations} "
                        f"from {max_iterations_source} after a finalization attempt. "
                        f"{max_iterations_guidance}"
                    ),
                    code="max_iterations",
                )
                yield t.terminal_error
                t.stop = True
                return

        # Check total turn deadline (if configured)
        if t.total_deadline is not None and t.loop.time() > t.total_deadline:
            raise TimeoutError(f"Agent total timeout after {self.config.timeout}s")

        t.iterations += 1

        # ------ THINKING → STREAMING ------
        yield self._transition(AgentState.STREAMING)

        # Collect this LLM response
        t.assistant_text_parts = []
        t.tool_calls = []
        t.pending_tools = {}
        t.iter_input_tokens = 0
        t.iter_output_tokens = 0
        t.iter_reasoning_tokens = 0
        t.iter_reasoning_content = None
        t.iter_thinking_signature = None
        t.provider_error = None

        t.retry_attempt = 0
        t.call_attempt = 0
        t.attempt_history = AttemptHistory()
        t.attempt_started_at = t.loop.time()
        t.retry_policy = _ProviderRetryPolicy.from_provider_budget(
            t.fallback.max_retries,
            length_capped_continuations=self.config.length_capped_continuations,
        )
        t.attempt_retries_used = t.retry_policy.used_attempts()
        # Request-scoped messages for the one retry after a reply that
        # wrote a tool call as text; never added to turn_messages.
        t.text_tool_call_nudge = []
        t.invalid_response_fallback_done = False
        t.pause_turn_resumes = 0
        # The attempt whose thinking the consumer is seeing. A retry of
        # the same step re-streams reasoning the UI already shows, so
        # later attempts' thinking deltas are not re-emitted (they still
        # reach the transcript through the done event). Continuations,
        # which keep the previous output, reset it.
        t.thinking_shown_attempt = None

    async def _turn_model_step(self, t: TurnState) -> AsyncGenerator[AgentEvent, None]:
        """One model response: request, stream, then retry until it is usable.

        Each attempt runs the ``_attempt_*`` steps in order; a step that sets
        ``t.attempt_flow`` ends the attempt (``RETRY`` sends the request again,
        ``EXIT`` leaves the retry loop).
        """
        while t.retry_attempt <= t.fallback.max_retries:
            t.attempt_flow = None
            for attempt_step in (
                self._attempt_request,
                self._attempt_stream,
                self._attempt_review,
                self._attempt_classify,
            ):
                async for event in attempt_step(t):
                    yield event
                if t.attempt_flow is not None:
                    break
            if t.attempt_flow is None:
                self._attempt_end(t)
            if t.attempt_flow is None:
                async for event in self._attempt_recover(t):
                    yield event
            if t.attempt_flow is AttemptFlow.EXIT:
                break
        if t.terminal_error is not None or t.artifact_delivery_degraded_final_response:
            t.stop = True

    async def _attempt_request(self, t: TurnState) -> AsyncGenerator[AgentEvent, None]:
        """Seal the transcript, build the request and run the pre-request checks."""
        t.provider_error = None
        t.assistant_text_parts = []
        t.tool_calls = []
        t.pending_tools = {}
        t.iter_input_tokens = 0
        t.iter_output_tokens = 0
        t.iter_reasoning_tokens = 0
        t.iter_reasoning_content = None
        t.iter_thinking_signature = None
        t.got_error = False
        t.provider_done_for_log = None
        t.provider_error_for_log = None
        t.call_id = f"{t.iterations}.{t.call_attempt}"
        t.call_started_at = time.monotonic()
        t.provider_tools_for_call = (
            None
            if (t.artifact_delivery_final_response_pending or t.max_iterations_finalization_pending)
            else t.provider_tool_definitions
        )
        t.tools_supported_for_call = (
            t.tools_supported
            and not t.artifact_delivery_final_response_pending
            and not t.max_iterations_finalization_pending
        )
        t.ignored_post_delivery_tool_use = False
        # Seal what was appended since the last request (shrink
        # oversized tool results, strip copied projections, check
        # pairing) -- once; the request is then the transcript as is.
        t.sealed_len = self._seal_transcript(t.turn_messages, t.sealed_len)
        t.request_messages = self._provider_request_messages(
            t.turn_messages,
            finalization_message=(
                t.max_iterations_finalization_message
                if t.max_iterations_finalization_pending
                else None
            ),
        )
        attempt_nudge, t.text_tool_call_nudge = t.text_tool_call_nudge, []
        t.request_messages.extend(attempt_nudge)
        hook_messages: list[Message] = []
        if self._request_hooks:
            hook_veto, hook_notes = self._run_request_hooks(
                t.request_messages, iteration=t.iterations, attempt=t.call_attempt
            )
            if hook_veto is not None:
                yield self._transition(AgentState.ERROR)
                t.terminal_error = ErrorEvent(message=hook_veto.reason, code="request_vetoed")
                yield t.terminal_error
                t.attempt_flow = AttemptFlow.EXIT
                return
            # Request-scoped like the nudge: after the cache-stable
            # prefix, never added to turn_messages.
            hook_messages = [Message(role="user", content=note) for note in hook_notes]
            t.request_messages.extend(hook_messages)
        self._write_context_stage(
            "stream:context",
            t.request_messages,
            call_id=t.call_id,
            iteration=t.iterations,
            attempt=t.call_attempt,
        )

        t.terminal_error = self._turn_llm_call_budget_error(t, t.turn_llm_calls + 1)
        if t.terminal_error is not None:
            self._write_turn_call_log(
                "turn_policy_decision",
                action=(
                    "artifact_degraded_finish"
                    if t.artifact_delivery_final_response_pending
                    else "stop"
                ),
                reason=t.terminal_error.message,
                code=t.terminal_error.code,
                sent_llm_calls=t.turn_llm_calls,
                attempted_llm_call=t.turn_llm_calls + 1,
                iteration=t.iterations,
                attempt=t.call_attempt,
            )
            if t.artifact_delivery_final_response_pending:
                yield self._finish_artifact_delivery_degraded(
                    t,
                    reason=t.terminal_error.message,
                    code=t.terminal_error.code,
                )
                t.terminal_error = None
            else:
                yield self._transition(AgentState.ERROR)
                yield t.terminal_error
            t.attempt_flow = AttemptFlow.EXIT
            return

        t.call_chat_cfg = t.chat_cfg

        # Check overflow against the live provider request before it
        # is sent, not after it streamed: once text or tool_use
        # events are out, re-calling the provider replays them to
        # the stream consumer (#3644).
        t.estimated_context_tokens = self._estimate_live_request_tokens(
            t.request_messages,
            tools=t.provider_tools_for_call,
            config=t.call_chat_cfg,
        )
        self._last_request_overhead_tokens = max(
            0, t.estimated_context_tokens - self._message_tokens(t.request_messages)
        )
        overflow_outcome = await self._preflight_context_overflow(
            t.turn_messages,
            t.estimated_context_tokens,
            turn_input_index=t.turn_input_index,
        )
        if overflow_outcome is None:
            if t.overflow_retries >= self.config.max_overflow_retries:
                yield self._transition(AgentState.ERROR)
                t.terminal_error = self._context_overflow_error()
                yield t.terminal_error
                t.attempt_flow = AttemptFlow.EXIT
                return
            # The estimate is a heuristic: send the request as is.
            # A real provider overflow takes the CONTEXT_OVERFLOW
            # branch below, and the next call checks again.
            t.overflow_retries += 1
            t.log.warning(
                "compaction.preflight_failed",
                attempt=t.overflow_retries,
                max=self.config.max_overflow_retries,
                estimated_tokens=t.estimated_context_tokens,
            )
        elif overflow_outcome.compacted:
            # Lifetime counters keep feeding DoneEvent usage/cost
            # accounting for this turn.
            t.turn_messages = overflow_outcome.messages
            t.sealed_len = len(t.turn_messages)
            t.turn_input_index = overflow_outcome.turn_input_index
            # The stream consumer persists the compaction while
            # handling this event; the system prompt is unchanged.
            yield CompactionEvent(
                compaction_id=overflow_outcome.compaction_id,
                summary=overflow_outcome.summary,
                kept_entries=overflow_outcome.kept_entries,
                kept_count=len(overflow_outcome.messages),
                removed_count=overflow_outcome.removed_count,
            )
            t.overflow_retries = 0  # reset on success
            t.request_messages = self._provider_request_messages(
                t.turn_messages,
                finalization_message=(
                    t.max_iterations_finalization_message
                    if t.max_iterations_finalization_pending
                    else None
                ),
            )
            t.request_messages.extend(attempt_nudge)
            # The request is rebuilt from the compacted transcript: the
            # hooks' request-only notes go back on its tail.
            t.request_messages.extend(hook_messages)
            # Calibrate against the request actually sent.
            t.estimated_context_tokens = self._estimate_live_request_tokens(
                t.request_messages,
                tools=t.provider_tools_for_call,
                config=t.call_chat_cfg,
            )
            self._write_context_stage(
                "stream:context:compacted",
                t.request_messages,
                call_id=t.call_id,
                iteration=t.iterations,
                attempt=t.call_attempt,
            )

        self._write_turn_call_log(
            "llm_request",
            call_id=t.call_id,
            iteration=t.iterations,
            attempt=t.call_attempt,
            messages=t.request_messages,
            tools=t.provider_tools_for_call,
            config=t.call_chat_cfg,
        )
        t.turn_llm_calls += 1
        t.cache_prompt_snapshot = None
        if self._session_key:
            t.cache_prompt_snapshot = record_prompt_state(
                messages=t.request_messages,
                tools=t.provider_tools_for_call,
                config=t.call_chat_cfg,
                model=self.config.model_id or "",
            )

        # Holds back text that may be a tool call written as text,
        # while a retry for that is still available.
        t.text_call_gate = (
            TextToolCallStreamGate(offered_tool_names(t.provider_tools_for_call))
            if t.tools_supported_for_call
            and t.provider_tools_for_call
            and t.retry_policy.can_retry_attempt(
                _ProviderAttemptKind.TEXT_TOOL_CALL, t.attempt_retries_used
            )
            else None
        )
        t.repetition_guard = (
            StreamRepetitionGuard(self.config.repetition_guard)
            if self.config.repetition_guard.enabled
            else None
        )
        t.repetition_trip = None

    def _attempt_on_done(self, t: TurnState, raw_ev: ProviderDoneEvent) -> None:
        """Account the provider's done event of the current attempt."""
        # Make the estimator's drift visible: the
        # compaction trigger runs on this estimate,
        # scaled by the calibration it feeds.
        calibration_factor = self._observe_estimate(
            t.estimated_context_tokens,
            raw_ev.input_tokens,
            usage_estimated=raw_ev.usage_estimated,
            request_messages=t.request_messages,
        )
        logger.debug(
            "agent.context_estimate_vs_provider",
            session_key=self._session_key,
            call_id=t.call_id,
            estimated_tokens=t.estimated_context_tokens,
            provider_input_tokens=raw_ev.input_tokens,
            provider_cached_tokens=raw_ev.cached_tokens,
            provider_cache_write_tokens=raw_ev.cache_write_tokens,
            calibration_factor=calibration_factor,
        )
        t.iter_input_tokens = raw_ev.input_tokens
        t.iter_output_tokens = raw_ev.output_tokens
        t.iter_reasoning_tokens = raw_ev.reasoning_tokens
        t.iter_reasoning_content = raw_ev.reasoning_content
        t.iter_thinking_signature = raw_ev.thinking_signature
        t.total_billed_cost += raw_ev.billed_cost
        t.total_input_tokens += raw_ev.input_tokens
        t.total_output_tokens += raw_ev.output_tokens
        t.total_reasoning_tokens += raw_ev.reasoning_tokens
        t.total_cached_tokens += raw_ev.cached_tokens
        t.total_cache_write_tokens += raw_ev.cache_write_tokens
        if raw_ev.model:
            t.last_actual_model = raw_ev.model
        # Usage/cost accounting is billed-attempt based: discarded
        # invalid responses still consumed provider tokens, but
        # they must not be appended to conversation history or the
        # live context-window gauge below.
        if self._usage_tracker and self._session_key:
            # Forward the provider's real per-call billed_cost so
            # the per-model breakdown can show actual numbers
            # instead of the cache-blind pricing-table estimate.
            # See engine/usage.py:ModelUsage.billed_cost and
            # gateway/rpc_usage.py:_reconcile_breakdown_to_row
            # (the pro-rate fallback now skips when items
            # already carry real billed totals).
            metadata = provider_metadata(self.provider)
            self._usage_tracker.add(
                self._session_key,
                input_tokens=raw_ev.input_tokens,
                output_tokens=raw_ev.output_tokens,
                model_id=raw_ev.model or self.config.model_id or "",
                cache_read_tokens=raw_ev.cached_tokens,
                cache_write_tokens=raw_ev.cache_write_tokens,
                billed_cost=raw_ev.billed_cost,
                provider_id=(metadata.provider_kind or metadata.provider_name),
            )

    async def _attempt_stream(self, t: TurnState) -> AsyncGenerator[AgentEvent, None]:
        """Send the request and stream the response (``core.stream_attempt``)."""
        # The retry budget charges failed attempts from the moment their
        # request goes out: an earlier successful stream (a continuation, a
        # text-tool-call retry) or a compaction before it is not recovery time.
        t.attempt_started_at = t.loop.time()
        t.raw_stream = self.provider.chat(
            t.request_messages,
            tools=t.provider_tools_for_call,
            config=t.call_chat_cfg,
        )
        t.deadline_stream = self._stream_provider_events_with_deadline(
            t.raw_stream,
            loop=t.loop,
            total_deadline=t.total_deadline,
        )
        t.timed_stream = self.turn_timing.provider_stream(t.deadline_stream)
        attempt: AttemptResult | None = None
        async with aclosing(
            stream_attempt(
                t.timed_stream,
                text_parts=t.assistant_text_parts,
                tools_enabled=t.tools_supported_for_call,
                finalizing=(
                    t.artifact_delivery_final_response_pending
                    or t.max_iterations_finalization_pending
                ),
                text_gate=t.text_call_gate,
                repetition_guard=t.repetition_guard,
                show_thinking=t.thinking_shown_attempt in (None, t.call_attempt),
                thinking_fallback_allowed=t.thinking_enabled and not t.thinking_fallback_done,
                canonical_tool_name=self._canonical_tool_name,
                coerce_tool_arguments=self._coerce_tool_arguments,
                on_done=lambda raw_ev: self._attempt_on_done(t, raw_ev),
                started_at=t.call_started_at,
            )
        ) as stream_items:
            async for stream_item in stream_items:
                if isinstance(stream_item, AttemptResult):
                    attempt = stream_item
                else:
                    yield stream_item
        assert attempt is not None
        t.tool_calls = attempt.tool_calls
        t.pending_tools = attempt.pending_tools
        t.attempt_reasoning_parts = attempt.reasoning_parts
        t.provider_done_for_log = attempt.done
        t.got_done_event = attempt.done is not None
        t.provider_error_for_log = attempt.error_event
        t.provider_error = attempt.provider_error
        t.got_error = attempt.got_error
        t.repetition_trip = attempt.repetition_trip
        t.attempt_user_visible_emitted = attempt.user_visible_emitted
        if attempt.ignored_tool_use:
            t.ignored_post_delivery_tool_use = True
        if attempt.showed_thinking:
            t.thinking_shown_attempt = t.call_attempt
        if attempt.thinking_fallback:
            # One-shot thinking/reasoning fallback: retry without thinking.
            t.thinking_fallback_done = True
            t.thinking_enabled = False
            t.thinking_budget = 0
            t.chat_cfg = _chat_config_with_thinking_disabled(t.chat_cfg)
        if attempt.iteration_timeout:
            if t.artifact_delivery_final_response_pending:
                yield self._finish_artifact_delivery_degraded(
                    t,
                    reason=(
                        f"Iteration {t.iterations} exceeded "
                        f"iteration_timeout ({self.config.iteration_timeout}s) "
                        "during final artifact response generation"
                    ),
                    code="iteration_timeout",
                )
                t.attempt_flow = AttemptFlow.EXIT
                return
            yield self._transition(AgentState.ERROR)
            t.terminal_error = ErrorEvent(
                message=(
                    f"Iteration {t.iterations} exceeded iteration_timeout"
                    f" ({self.config.iteration_timeout}s) during LLM streaming"
                ),
                code="iteration_timeout",
            )
            yield t.terminal_error
            t.attempt_flow = AttemptFlow.EXIT
            return

    async def _attempt_review(self, t: TurnState) -> AsyncGenerator[AgentEvent, None]:
        """Log the response; handle a repetition trip or a text-written tool call."""
        if t.repetition_trip is not None:
            # Stop paying for tokens: close every layer of the
            # stream now instead of leaving it to the GC.
            for stream_layer in (t.timed_stream, t.deadline_stream, t.raw_stream):
                await self._close_provider_stream(stream_layer)
            # The provider never sent its usage event, but the call
            # was billed: count an estimate so turn totals, cost and
            # token budgets do not undercount it.
            cut_reasoning_tokens = get_approx_tokens("".join(t.attempt_reasoning_parts))
            t.provider_done_for_log = ProviderDoneEvent(
                stop_reason=StopReason.REPETITION,
                input_tokens=max(0, int(t.estimated_context_tokens)),
                output_tokens=(
                    get_approx_tokens("".join(t.assistant_text_parts)) + cut_reasoning_tokens
                ),
                reasoning_tokens=cut_reasoning_tokens,
                model=t.last_actual_model or self.config.model_id or "",
                usage_estimated=True,
            )
            t.iter_input_tokens = t.provider_done_for_log.input_tokens
            t.iter_output_tokens = t.provider_done_for_log.output_tokens
            t.iter_reasoning_tokens = cut_reasoning_tokens
            t.total_input_tokens += t.iter_input_tokens
            t.total_output_tokens += t.iter_output_tokens
            t.total_reasoning_tokens += cut_reasoning_tokens
            if self._usage_tracker and self._session_key:
                metadata = provider_metadata(self.provider)
                self._usage_tracker.add(
                    self._session_key,
                    input_tokens=t.iter_input_tokens,
                    output_tokens=t.iter_output_tokens,
                    model_id=t.provider_done_for_log.model,
                    provider_id=(metadata.provider_kind or metadata.provider_name),
                )

        text_tool_call = (
            text_tool_call_to_retry(
                text="".join(t.assistant_text_parts),
                gate=t.text_call_gate,
                produced_tool_calls=bool(t.tool_calls or t.pending_tools),
                stream_complete=t.got_done_event and not t.got_error,
                stop_reason=getattr(t.provider_done_for_log, "stop_reason", None),
            )
            if t.text_call_gate is not None
            and self._turn_budget_error(t) is None
            and self._turn_llm_call_budget_error(t, t.turn_llm_calls + 1) is None
            else None
        )
        if t.text_call_gate is not None and text_tool_call is None:
            if held_text := t.text_call_gate.release():
                yield TextDeltaEvent(text=held_text)

        call_duration_ms = int((time.monotonic() - t.call_started_at) * 1000)
        response_payload = {
            "call_id": t.call_id,
            "iteration": t.iterations,
            "attempt": t.call_attempt,
            "duration_ms": call_duration_ms,
            "text": "".join(t.assistant_text_parts),
            "tool_calls": [
                {
                    "tool_use_id": tc.tool_use_id,
                    "name": tc.tool_name,
                    "arguments": tc.arguments,
                }
                for tc in t.tool_calls
            ],
            "got_done_event": t.got_done_event,
        }
        if t.repetition_trip is not None:
            response_payload["repetition"] = {
                "stop_reason": StopReason.REPETITION.value,
                "channel": t.repetition_trip.channel,
                "signal": t.repetition_trip.signal,
                "period": t.repetition_trip.period,
                "repeats": t.repetition_trip.repeats,
                "checked_chars": t.repetition_trip.checked_chars,
            }
        if t.provider_done_for_log is not None:
            response_payload["usage"] = {
                "stop_reason": t.provider_done_for_log.stop_reason,
                "raw_stop_reason": t.provider_done_for_log.raw_stop_reason,
                "stop_details": t.provider_done_for_log.stop_details,
                "input_tokens": t.provider_done_for_log.input_tokens,
                "output_tokens": t.provider_done_for_log.output_tokens,
                "reasoning_tokens": t.provider_done_for_log.reasoning_tokens,
                "cached_tokens": t.provider_done_for_log.cached_tokens,
                "cache_write_tokens": t.provider_done_for_log.cache_write_tokens,
                "billed_cost": t.provider_done_for_log.billed_cost,
                "cost_source": getattr(t.provider_done_for_log, "cost_source", "none"),
                "model": t.provider_done_for_log.model,
            }
        if t.provider_error_for_log is not None:
            response_payload["error"] = {
                "message": t.provider_error_for_log.message,
                "code": t.provider_error_for_log.code,
            }
            self._write_turn_call_log("llm_error", **response_payload)
        else:
            self._write_turn_call_log("llm_response", **response_payload)

        t.terminal_error = self._turn_budget_error(t)
        if t.terminal_error is not None:
            if t.artifact_delivery_final_response_pending:
                yield self._finish_artifact_delivery_degraded(
                    t,
                    reason=t.terminal_error.message,
                    code=t.terminal_error.code,
                )
                t.terminal_error = None
            else:
                yield self._transition(AgentState.ERROR)
                yield t.terminal_error
            t.attempt_flow = AttemptFlow.EXIT
            return
        if t.repetition_trip is not None:
            # Keep the text up to the first repeated copy and drop
            # every tool call the cut response started: it has no
            # done event, so its calls are never safe to run.
            raw_response_text = "".join(t.assistant_text_parts)
            kept_response_text = (
                kept_text(raw_response_text, t.repetition_trip)
                if t.repetition_trip.channel == "text"
                else raw_response_text
            )
            dropped_tool_calls = len(t.tool_calls) + len(t.pending_tools)
            t.assistant_text_parts = [kept_response_text] if kept_response_text else []
            t.tool_calls = []
            t.pending_tools = {}
            repetition_continue = t.retry_policy.can_retry_attempt(
                _ProviderAttemptKind.REPETITION,
                t.attempt_retries_used,
            )
            logger.warning(
                "provider.repetition_detected",
                session_key=self._session_key,
                model=t.last_actual_model or self.config.model_id or "",
                provider=type(self.provider).__name__,
                iteration=t.iterations,
                call_attempt=t.call_attempt,
                channel=t.repetition_trip.channel,
                signal=t.repetition_trip.signal,
                period=t.repetition_trip.period,
                repeats=t.repetition_trip.repeats,
                checked_chars=t.repetition_trip.checked_chars,
                kept_chars=len(kept_response_text),
                removed_chars=len(raw_response_text) - len(kept_response_text),
                dropped_tool_calls=dropped_tool_calls,
                action="continue" if repetition_continue else "stop",
            )
            self._write_turn_call_log(
                "turn_policy_decision",
                action=(
                    "continue_after_repetition"
                    if repetition_continue
                    else "stop_repetitive_response"
                ),
                reason="repetition_detected",
                code="repetition_detected",
                iteration=t.iterations,
                attempt=t.call_attempt,
                channel=t.repetition_trip.channel,
                signal=t.repetition_trip.signal,
                dropped_tool_calls=dropped_tool_calls,
            )
            yield RepetitionStopEvent(
                channel=t.repetition_trip.channel,
                signal=t.repetition_trip.signal,
                period=t.repetition_trip.period,
                removed_text=raw_response_text[len(kept_response_text) :],
            )
            if repetition_continue:
                t.attempt_retries_used[_ProviderAttemptKind.REPETITION] += 1
                t.visible_text = _append_length_capped_continuation(
                    t.turn_messages,
                    response_text=kept_response_text,
                    tool_calls=[],
                    prompt=_REPETITION_CONTINUE_PROMPT,
                )
                if t.visible_text:
                    t.final_text_parts.append(t.visible_text)
                yield WarningEvent(
                    code="repetition_detected",
                    message=(
                        "The response started repeating itself, so it was stopped; continuing once."
                    ),
                )
                t.thinking_shown_attempt = None
                t.call_attempt += 1
                t.attempt_flow = AttemptFlow.RETRY
                return
            yield self._transition(AgentState.ERROR)
            yield WarningEvent(
                code="repetition_detected",
                message="The response kept repeating itself, so it was stopped.",
            )
            t.turn_stop_reason = StopReason.REPETITION.value
            t.terminal_error = ErrorEvent(
                message=_REPETITION_STOPPED_REPLY,
                code="repetition_detected",
            )
            yield t.terminal_error
            t.attempt_flow = AttemptFlow.EXIT
            return
        t.response_text = "".join(t.assistant_text_parts)
        if text_tool_call is not None and t.text_call_gate is not None:
            # The reply wrote a tool call as text and nothing ran.
            # Drop it (the gate kept it off screen) and ask once
            # more, with a nudge on that request only.
            t.attempt_retries_used[_ProviderAttemptKind.TEXT_TOOL_CALL] += 1
            t.text_call_gate.discard()
            t.text_tool_call_nudge = [
                Message(
                    role="assistant",
                    content=[ContentBlockText(text=t.response_text)],
                ),
                Message(role="user", content=text_tool_call_nudge(text_tool_call)),
            ]
            self.turn_timing.record_retry(_ProviderAttemptKind.TEXT_TOOL_CALL.value)
            logger.warning(
                "provider.text_tool_call_retry",
                session_key=self._session_key,
                model=t.last_actual_model or self.config.model_id or "",
                provider=type(self.provider).__name__,
                iteration=t.iterations,
                call_attempt=t.call_attempt,
                tool_name=text_tool_call.tool_name,
                shape=text_tool_call.shape,
                discarded_chars=len(t.response_text) - text_tool_call.start,
            )
            self._write_turn_call_log(
                "turn_policy_decision",
                action="retry_text_tool_call",
                reason="text_tool_call",
                code="text_tool_call",
                iteration=t.iterations,
                attempt=t.call_attempt,
                tool_name=text_tool_call.tool_name,
                shape=text_tool_call.shape,
                budget=t.retry_policy.attempt_budgets[_ProviderAttemptKind.TEXT_TOOL_CALL],
            )
            yield WarningEvent(
                code="provider_text_tool_call_retry",
                message=(
                    "The model wrote a tool call as text instead of calling "
                    "the tool; retrying once."
                ),
            )
            t.call_attempt += 1
            t.attempt_flow = AttemptFlow.RETRY
            return

    async def _attempt_classify(self, t: TurnState) -> AsyncGenerator[AgentEvent, None]:
        """Classify the response and recover from invalid ones (pause, refusal, empty)."""
        if t.ignored_post_delivery_tool_use and not t.response_text.strip():
            if t.artifact_delivery_final_response_pending:
                t.response_text = self._artifact_delivery_final_response_text(
                    t.artifact_delivery_final_response_artifacts
                )
            elif t.max_iterations_finalization_pending:
                t.response_text = (
                    "I reached the configured iteration limit after completing "
                    "the available tool step. Here is the best partial result so far."
                )
            if t.response_text:
                t.assistant_text_parts.append(t.response_text)
                t.attempt_user_visible_emitted = True
                yield TextDeltaEvent(text=t.response_text)
        last_request_msg = t.request_messages[-1] if t.request_messages else None
        t.post_tool_turn = _message_has_tool_result(last_request_msg)
        stop_reason = (
            getattr(t.provider_done_for_log, "stop_reason", None)
            if t.provider_done_for_log is not None
            else None
        )
        attempt_classification = _classify_provider_attempt(
            text=t.response_text,
            tool_calls=t.tool_calls,
            pending_tools=t.pending_tools,
            got_done_event=t.got_done_event,
            stop_reason=stop_reason,
            reasoning_content=t.iter_reasoning_content,
            reasoning_tokens=t.iter_reasoning_tokens,
            user_visible_emitted=t.attempt_user_visible_emitted,
        )
        if not t.got_error and attempt_classification.kind == _ProviderAttemptKind.PAUSE_TURN:
            if t.pause_turn_resumes < _MAX_PAUSE_TURN_RESUMES:
                # Keep what the paused response produced, then ask
                # the provider to carry on. The assistant text goes
                # into history and into the final reply, exactly as
                # for a length-capped continuation. A follow-up user
                # nudge (rather than a trailing assistant message) is
                # used because current Claude models reject an
                # assistant-last request as an unsupported prefill.
                t.pause_turn_resumes += 1
                t.visible_text = _append_length_capped_continuation(
                    t.turn_messages,
                    response_text=t.response_text,
                    tool_calls=t.tool_calls,
                    prompt=_PROVIDER_PAUSE_TURN_RESUME_PROMPT,
                )
                if t.visible_text:
                    t.final_text_parts.append(t.visible_text)
                logger.info(
                    "provider.pause_turn_resume",
                    session_key=self._session_key,
                    model=t.last_actual_model or self.config.model_id or "",
                    provider=type(self.provider).__name__,
                    iteration=t.iterations,
                    call_attempt=t.call_attempt,
                    resume=t.pause_turn_resumes,
                    budget=_MAX_PAUSE_TURN_RESUMES,
                    visible_chars=len(t.visible_text),
                )
                self._write_turn_call_log(
                    "turn_policy_decision",
                    action="resume_paused_turn",
                    reason="pause_turn",
                    code="pause_turn",
                    iteration=t.iterations,
                    attempt=t.call_attempt,
                    resume=t.pause_turn_resumes,
                    budget=_MAX_PAUSE_TURN_RESUMES,
                )
                t.thinking_shown_attempt = None
                t.call_attempt += 1
                t.attempt_flow = AttemptFlow.RETRY
                return
            # Resume budget spent: the paused response is the end of
            # the turn. Re-classify it as a normal end_turn so the
            # usual empty/visible checks apply.
            logger.warning(
                "provider.pause_turn_resume_exhausted",
                session_key=self._session_key,
                iteration=t.iterations,
                call_attempt=t.call_attempt,
                budget=_MAX_PAUSE_TURN_RESUMES,
            )
            self._write_turn_call_log(
                "turn_policy_decision",
                action="end_paused_turn",
                reason="pause_turn_resume_exhausted",
                code="pause_turn",
                iteration=t.iterations,
                attempt=t.call_attempt,
                budget=_MAX_PAUSE_TURN_RESUMES,
            )
            if t.provider_done_for_log is not None:
                t.provider_done_for_log.stop_reason = StopReason.END_TURN
            stop_reason = StopReason.END_TURN
            attempt_classification = _classify_provider_attempt(
                text=t.response_text,
                tool_calls=t.tool_calls,
                pending_tools=t.pending_tools,
                got_done_event=t.got_done_event,
                stop_reason=stop_reason,
                reasoning_content=t.iter_reasoning_content,
                reasoning_tokens=t.iter_reasoning_tokens,
                user_visible_emitted=t.attempt_user_visible_emitted,
            )
        if not t.got_error and attempt_classification.kind == _ProviderAttemptKind.REFUSAL:
            # A safety decline is final: no retry, no failover to
            # another provider (that would be shopping for a model
            # that says yes). Keep whatever text the model wrote
            # before it declined; tool calls from a declined
            # response are never executed.
            refusal_details = (
                t.provider_done_for_log.stop_details
                if t.provider_done_for_log is not None
                else None
            )
            refusal_category = _refusal_category(refusal_details)
            dropped_tool_calls = len(t.tool_calls)
            t.tool_calls = []
            if not t.response_text.strip():
                t.response_text = _PROVIDER_REFUSAL_REPLY
                t.assistant_text_parts.append(t.response_text)
                t.attempt_user_visible_emitted = True
                yield TextDeltaEvent(text=t.response_text)
            logger.warning(
                "provider.refusal",
                session_key=self._session_key,
                model=t.last_actual_model or self.config.model_id or "",
                provider=type(self.provider).__name__,
                iteration=t.iterations,
                call_attempt=t.call_attempt,
                category=refusal_category,
                raw_stop_reason=(
                    t.provider_done_for_log.raw_stop_reason
                    if t.provider_done_for_log is not None
                    else None
                ),
                dropped_tool_calls=dropped_tool_calls,
            )
            self._write_turn_call_log(
                "turn_policy_decision",
                action="finish_refused_turn",
                reason="refusal",
                code="provider_refusal",
                category=refusal_category,
                iteration=t.iterations,
                attempt=t.call_attempt,
                dropped_tool_calls=dropped_tool_calls,
            )
            yield WarningEvent(
                code="provider_refusal",
                message=_refusal_warning_message(refusal_details),
            )
        if not t.got_error and attempt_classification.kind not in {
            _ProviderAttemptKind.OK,
            _ProviderAttemptKind.REFUSAL,
        }:
            logger.warning(
                "provider.invalid_response",
                session_key=self._session_key,
                model=t.last_actual_model or self.config.model_id or "",
                provider=type(self.provider).__name__,
                classification=attempt_classification.kind.value,
                iteration=t.iterations,
                call_attempt=t.call_attempt,
                provider_retry_attempt=t.retry_attempt,
                post_tool_turn=t.post_tool_turn,
                got_done_event=t.got_done_event,
                stop_reason=stop_reason,
                iter_input_tokens=t.iter_input_tokens,
                iter_output_tokens=t.iter_output_tokens,
                iter_reasoning_tokens=t.iter_reasoning_tokens,
                reasoning_chars=len(t.iter_reasoning_content or ""),
            )

            large_context_invalid = _is_large_context_invalid_response(
                attempt_classification.kind,
                input_tokens=t.iter_input_tokens,
            )
            if large_context_invalid:
                if (
                    not t.invalid_response_fallback_done
                    and self._switch_to_invalid_response_fallback(attempt_classification.kind.value)
                ):
                    t.invalid_response_fallback_done = True
                    yield WarningEvent(
                        code="provider_large_context_fallback",
                        message=(
                            "The provider returned no visible response for a "
                            "large input; trying a fallback provider once."
                        ),
                    )
                    t.call_attempt += 1
                    t.attempt_flow = AttemptFlow.RETRY
                    return

                yield self._transition(AgentState.ERROR)
                t.terminal_error = ErrorEvent(
                    message=(
                        "Provider returned no visible response for a large input. "
                        "Send the material as an attachment, summarize or shorten "
                        "the prompt, or use a stronger model."
                    ),
                    code="empty_response",
                )
                yield t.terminal_error
                t.attempt_flow = AttemptFlow.EXIT
                return

            if (
                attempt_classification.kind == _ProviderAttemptKind.REASONING_ONLY
                and t.thinking_enabled
                and t.retry_policy.can_retry_attempt(
                    _ProviderAttemptKind.REASONING_ONLY,
                    t.attempt_retries_used,
                )
            ):
                t.attempt_retries_used[_ProviderAttemptKind.REASONING_ONLY] += 1
                t.thinking_fallback_done = True
                t.thinking_enabled = False
                t.thinking_budget = 0
                t.chat_cfg = _chat_config_with_thinking_disabled(t.chat_cfg)
                yield WarningEvent(
                    code="provider_reasoning_only_retry",
                    message=(
                        "The provider returned reasoning without visible content; "
                        "retrying once with thinking disabled."
                    ),
                )
                t.call_attempt += 1
                t.attempt_flow = AttemptFlow.RETRY
                return

            if (
                attempt_classification.kind == _ProviderAttemptKind.MALFORMED_EMPTY
                and t.retry_policy.can_retry_attempt(
                    _ProviderAttemptKind.MALFORMED_EMPTY,
                    t.attempt_retries_used,
                )
            ):
                t.attempt_retries_used[_ProviderAttemptKind.MALFORMED_EMPTY] += 1
                delay = backoff_sleep(
                    0,
                    t.fallback.base_backoff_ms,
                    t.fallback.max_backoff_ms,
                    _fake=True,
                )
                yield WarningEvent(
                    code="provider_empty_retry",
                    message="The provider returned an empty response; retrying once.",
                )
                await _sleep_unless_aborted(delay)
                t.call_attempt += 1
                t.attempt_flow = AttemptFlow.RETRY
                return

            if (
                attempt_classification.kind == _ProviderAttemptKind.STREAM_INCOMPLETE
                and not attempt_classification.user_visible_emitted
                and t.retry_policy.can_retry_attempt(
                    _ProviderAttemptKind.STREAM_INCOMPLETE,
                    t.attempt_retries_used,
                )
            ):
                t.attempt_retries_used[_ProviderAttemptKind.STREAM_INCOMPLETE] += 1
                delay = backoff_sleep(
                    0,
                    t.fallback.base_backoff_ms,
                    t.fallback.max_backoff_ms,
                    _fake=True,
                )
                yield WarningEvent(
                    code="provider_empty_retry",
                    message=("The provider stream ended before completion; retrying once."),
                )
                await _sleep_unless_aborted(delay)
                t.call_attempt += 1
                t.attempt_flow = AttemptFlow.RETRY
                return

            if (
                attempt_classification.kind == _ProviderAttemptKind.LENGTH_CAPPED
                and t.retry_policy.can_retry_attempt(
                    _ProviderAttemptKind.LENGTH_CAPPED,
                    t.attempt_retries_used,
                )
            ):
                t.attempt_retries_used[_ProviderAttemptKind.LENGTH_CAPPED] += 1
                t.visible_text = _append_length_capped_continuation(
                    t.turn_messages,
                    response_text=t.response_text,
                    tool_calls=t.tool_calls,
                )
                if t.visible_text:
                    t.final_text_parts.append(t.visible_text)
                logger.warning(
                    "provider.output_truncated_continue",
                    session_key=self._session_key,
                    model=t.last_actual_model or self.config.model_id or "",
                    provider=type(self.provider).__name__,
                    iteration=t.iterations,
                    call_attempt=t.call_attempt,
                    tool_calls=len(t.tool_calls),
                    visible_chars=len(t.visible_text),
                )
                yield WarningEvent(
                    code="provider_output_continue",
                    message=(
                        "The provider reached its output limit; continuing "
                        "the response automatically."
                    ),
                )
                t.thinking_shown_attempt = None
                t.call_attempt += 1
                t.attempt_flow = AttemptFlow.RETRY
                return

            if (
                attempt_classification.kind
                in {
                    _ProviderAttemptKind.REASONING_ONLY,
                    _ProviderAttemptKind.MALFORMED_EMPTY,
                }
                and not t.invalid_response_fallback_done
                and self._switch_to_invalid_response_fallback(attempt_classification.kind.value)
            ):
                t.invalid_response_fallback_done = True
                yield WarningEvent(
                    code="provider_empty_retry",
                    message=(
                        "The provider returned no visible response; "
                        "retrying on a fallback provider."
                    ),
                )
                t.call_attempt += 1
                t.attempt_flow = AttemptFlow.RETRY
                return

            yield self._transition(AgentState.ERROR)
            if attempt_classification.kind == _ProviderAttemptKind.INCOMPLETE_TOOLS:
                t.terminal_error = ErrorEvent(
                    message="Provider stream ended with an incomplete tool call",
                    code="incomplete_tool_stream",
                )
                yield t.terminal_error
                t.attempt_flow = AttemptFlow.EXIT
                return
            if attempt_classification.kind == _ProviderAttemptKind.STREAM_INCOMPLETE:
                t.terminal_error = ErrorEvent(
                    message="Provider stream ended before a done event",
                    code="provider_stream_incomplete",
                )
                yield t.terminal_error
                t.attempt_flow = AttemptFlow.EXIT
                return
            if attempt_classification.kind == _ProviderAttemptKind.LENGTH_CAPPED:
                t.visible_text = strip_synthetic_tool_call_suffix(
                    t.response_text,
                    [tc.tool_name for tc in t.tool_calls if tc.synthetic_from_text],
                )
                logger.warning(
                    "provider.output_truncated_exhausted",
                    session_key=self._session_key,
                    model=t.last_actual_model or self.config.model_id or "",
                    provider=type(self.provider).__name__,
                    iteration=t.iterations,
                    call_attempt=t.call_attempt,
                    attempt=t.attempt_retries_used.get(_ProviderAttemptKind.LENGTH_CAPPED, 0),
                    budget=t.retry_policy.attempt_budgets.get(
                        _ProviderAttemptKind.LENGTH_CAPPED, 0
                    ),
                    tool_calls=len(t.tool_calls),
                    visible_chars=len(t.visible_text),
                    partial_preserved=bool(t.visible_text or t.final_text_parts),
                )
                yield WarningEvent(
                    code="provider_output_truncated",
                    message=("The provider stopped because the output limit was reached."),
                )
                t.terminal_error = ErrorEvent(
                    message=_PROVIDER_OUTPUT_TRUNCATED_REPLY,
                    code="provider_output_truncated",
                )
                yield t.terminal_error
                t.attempt_flow = AttemptFlow.EXIT
                return
            logger.warning(
                "provider.empty_response",
                session_key=self._session_key,
                model=t.last_actual_model or self.config.model_id or "",
                provider=type(self.provider).__name__,
                iteration=t.iterations,
                retry_attempt=t.call_attempt,
                post_tool_turn=t.post_tool_turn,
                got_done_event=t.got_done_event,
                stop_reason=stop_reason,
                iter_input_tokens=t.iter_input_tokens,
                iter_output_tokens=t.iter_output_tokens,
                iter_reasoning_tokens=t.iter_reasoning_tokens,
                reasoning_chars=len(t.iter_reasoning_content or ""),
            )
            t.terminal_error = ErrorEvent(
                message="Provider returned an empty response",
                code="empty_response",
            )
            yield t.terminal_error
            t.attempt_flow = AttemptFlow.EXIT
            return

        if (
            not t.got_error
            and attempt_classification.kind == _ProviderAttemptKind.OK
            and normalize_stop_reason(stop_reason) is StopReason.MAX_TOKENS
        ):
            yield WarningEvent(
                code="provider_output_truncated",
                message="The provider stopped because the output limit was reached.",
            )

        if (
            not t.got_error
            and self._session_key
            and t.cache_prompt_snapshot is not None
            and t.provider_done_for_log is not None
        ):
            cache_report = check_response_for_cache_break(
                self._session_key,
                t.cache_prompt_snapshot,
                t.provider_done_for_log.cached_tokens,
                usage_reported=bool(t.provider_done_for_log.input_tokens)
                and getattr(t.provider_done_for_log, "cache_usage_reported", True),
            )
            if cache_report.break_detected:
                logger.warning(
                    "prompt_cache.break_detected",
                    session_key=self._session_key,
                    **cache_report.to_log_dict(),
                )

    def _attempt_end(self, t: TurnState) -> None:
        """A response without a provider error leaves the retry loop."""
        if not t.got_error:
            t.attempt_flow = AttemptFlow.EXIT
            return

        if t.provider_error is None:
            t.call_attempt += 1
            t.attempt_flow = AttemptFlow.RETRY
            return

    async def _attempt_recover(self, t: TurnState) -> AsyncGenerator[AgentEvent, None]:
        """Recover from a provider error: retry, fail over, compact, or fail."""
        if t.provider_error is not None:
            provider_error_status_code = (
                int(t.provider_error.code) if str(t.provider_error.code).isdigit() else None
            )
            failure_kind = classify_provider_error(
                provider_name=getattr(self.provider, "provider_name", ""),
                status_code=provider_error_status_code,
                raw_code=t.provider_error.code,
                message=t.provider_error.message,
            )
            kind = t.fallback.classify_error(
                t.provider_error.message,
                provider_name=getattr(self.provider, "provider_name", ""),
                status_code=provider_error_status_code,
                raw_code=t.provider_error.code,
            )
            if t.artifact_delivery_final_response_pending:
                yield self._finish_artifact_delivery_degraded(
                    t,
                    reason=t.provider_error.message,
                    code=t.provider_error.code,
                )
                t.attempt_flow = AttemptFlow.EXIT
                return
            if t.max_iterations_finalization_pending:
                t.response_text = (
                    "I reached the configured iteration limit, and the "
                    "provider could not generate an additional wrap-up. "
                    "Returning the best partial result from completed work."
                )
                t.assistant_text_parts.append(t.response_text)
                t.provider_done_for_log = ProviderDoneEvent(stop_reason=StopReason.END_TURN)
                t.got_done_event = True
                t.got_error = False
                t.max_iterations_finalization_pending = False
                self._write_turn_call_log(
                    "turn_policy_decision",
                    action="partial_after_finalization_provider_error",
                    reason="max_iterations",
                    code="max_iterations",
                    provider_error_code=t.provider_error.code,
                )
                yield TextDeltaEvent(text=t.response_text)
                t.attempt_flow = AttemptFlow.EXIT
                return
            if (
                failure_kind == ProviderFailureKind.EMPTY_RESPONSE
                and t.retry_policy.can_retry_provider_failure(
                    failure_kind,
                    post_tool_turn=t.post_tool_turn,
                    provider_retry_attempt=t.retry_attempt,
                )
            ):
                delay = backoff_sleep(
                    t.retry_attempt,
                    t.fallback.base_backoff_ms,
                    t.fallback.max_backoff_ms,
                    _fake=True,
                )
                self._write_turn_call_log(
                    "turn_policy_decision",
                    failure_kind=failure_kind.value,
                    error_kind=kind.value,
                    action="retry",
                    reason="empty_response_after_tools",
                    provider_error_code=t.provider_error.code,
                    attempt=t.retry_attempt + 1,
                )
                t.log.warning(
                    "provider.empty_response_retry",
                    attempt=t.retry_attempt + 1,
                    delay_s=round(delay, 2),
                    post_tool_turn=True,
                )
                yield WarningEvent(
                    code="provider_empty_retry",
                    message=(
                        "The provider returned an empty response after tool "
                        "execution; retrying once."
                    ),
                )
                await _sleep_unless_aborted(delay)
                t.retry_attempt += 1
                t.call_attempt += 1
                t.attempt_flow = AttemptFlow.RETRY
                return
            if failure_kind == ProviderFailureKind.CONTEXT_OVERFLOW:
                self._record_provider_context_overflow_reason(t.provider_error)
                provider_compaction_window_tokens = self._provider_budget_compaction_window_tokens(
                    t.provider_error
                )
                provider_estimated_tokens = self._provider_budget_estimated_tokens(t.provider_error)
                provider_compaction_refusal_reason = self._last_compaction_refusal_reason
                overflow_total_tokens = provider_estimated_tokens
                if overflow_total_tokens is None:
                    overflow_total_tokens = (
                        provider_compaction_window_tokens or self.config.context_window_tokens
                    ) + 1
                self._write_turn_call_log(
                    "turn_policy_decision",
                    failure_kind=failure_kind.value,
                    error_kind=kind.value,
                    action=(
                        "fail"
                        if t.overflow_retries >= self.config.max_overflow_retries
                        else "compact_then_retry"
                    ),
                    reason="context_overflow",
                    provider_error_code=t.provider_error.code,
                    attempt=t.retry_attempt + 1,
                )
                if t.overflow_retries >= self.config.max_overflow_retries:
                    yield self._transition(AgentState.ERROR)
                    t.terminal_error = self._context_overflow_error()
                    yield t.terminal_error
                    t.attempt_flow = AttemptFlow.EXIT
                    return
                t.overflow_retries += 1
                yield WarningEvent(
                    code="context_auto_compaction_start",
                    message=(
                        "Provider context limit reached; compacting older context before retrying."
                    ),
                )
                overflow_outcome = await self._check_context_overflow(
                    t.turn_messages,
                    overflow_total_tokens,
                    turn_input_index=t.turn_input_index,
                    compaction_window_tokens=provider_compaction_window_tokens,
                    force=True,
                )
                if overflow_outcome is None:
                    yield self._transition(AgentState.ERROR)
                    t.terminal_error = self._context_overflow_error()
                    yield t.terminal_error
                    t.attempt_flow = AttemptFlow.EXIT
                    return
                if (
                    provider_compaction_refusal_reason
                    and self._last_compaction_refusal_reason is None
                ):
                    self._last_compaction_refusal_reason = provider_compaction_refusal_reason
                next_request_messages = self._provider_request_messages(overflow_outcome.messages)
                if not self._provider_request_is_smaller(
                    t.request_messages,
                    next_request_messages,
                ):
                    yield self._transition(AgentState.ERROR)
                    if self._last_compaction_refusal_reason != "provider_recent_tail_too_large":
                        self._last_compaction_refusal_reason = "compaction_not_smaller"
                    t.terminal_error = self._context_overflow_error()
                    yield t.terminal_error
                    t.attempt_flow = AttemptFlow.EXIT
                    return
                t.turn_messages = overflow_outcome.messages
                t.sealed_len = len(t.turn_messages)
                t.turn_input_index = overflow_outcome.turn_input_index
                yield WarningEvent(
                    code="context_auto_compaction_retry",
                    message="Context compacted; retrying the provider request.",
                )
                yield CompactionEvent(
                    compaction_id=overflow_outcome.compaction_id,
                    summary=overflow_outcome.summary,
                    kept_entries=overflow_outcome.kept_entries,
                    kept_count=len(overflow_outcome.messages),
                    removed_count=overflow_outcome.removed_count,
                )
                t.call_attempt += 1
                t.attempt_flow = AttemptFlow.RETRY
                return
            # One recovery decision for this failed attempt
            # (agentos.engine.policy.attempt): retry the same
            # model, fail over, compact then retry, or fail.
            _is_timeout = is_transport_timeout(
                raw_code=t.provider_error.code,
                message=t.provider_error.message,
            )
            t.attempt_history.spent_s += max(0.0, t.loop.time() - t.attempt_started_at)
            _hint = (
                t.provider_error.retry_after_seconds
                if failure_kind in RETRY_AFTER_KINDS and t.retry_after_cap_s > 0
                else None
            )
            decision = t.attempt_policy.decide(
                kind,
                t.attempt_history,
                message=t.provider_error.message,
                retry_after_s=_hint,
                is_timeout=_is_timeout,
                # Never splice another model's output after text
                # the user already saw from this attempt.
                failover_available=not t.attempt_user_visible_emitted,
            )
            if isinstance(decision, Failover):
                switched = self._switch_to_invalid_response_fallback(t.provider_error.message)
                if switched:
                    t.attempt_history.failed_over = True
                else:
                    decision = t.attempt_policy.decide(
                        kind,
                        t.attempt_history,
                        message=t.provider_error.message,
                        retry_after_s=_hint,
                        is_timeout=_is_timeout,
                        failover_available=False,
                    )
            self._write_turn_call_log(
                "turn_policy_decision",
                failure_kind=failure_kind.value,
                error_kind=kind.value,
                action=decision.action,
                reason=decision.reason,
                provider_error_code=t.provider_error.code,
                attempt=t.retry_attempt + 1,
                spent_s=round(t.attempt_history.spent_s, 3),
            )
            if isinstance(decision, Failover):
                t.log.warning(
                    "provider.recovery_failover",
                    attempt=t.retry_attempt,
                    kind=failure_kind.value,
                    reason=decision.reason,
                )
                yield WarningEvent(
                    code="provider_retry_after_fallback"
                    if decision.reason == "retry_after_over_cap" or _hint is not None
                    else "provider_recovery_fallback",
                    message="The provider is still failing; switching to the fallback model.",
                )
                t.call_attempt += 1
                t.attempt_started_at = t.loop.time()
                t.attempt_flow = AttemptFlow.RETRY
                return
            if isinstance(decision, CompactThenRetry):
                t.attempt_history.timeout_compacted = True
                yield WarningEvent(
                    code="context_auto_compaction_start",
                    message=(
                        "The upstream model timed out on a large request; "
                        "compacting older context before retrying."
                    ),
                )
                timeout_outcome = await self._check_context_overflow(
                    t.turn_messages,
                    self.config.context_window_tokens + 1,
                    turn_input_index=t.turn_input_index,
                    force=True,
                    trigger="timeout",
                )
                timeout_request = (
                    self._provider_request_messages(timeout_outcome.messages)
                    if timeout_outcome is not None
                    else None
                )
                if timeout_outcome is not None and (
                    timeout_request is not None
                    and self._provider_request_is_smaller(t.request_messages, timeout_request)
                ):
                    t.turn_messages = timeout_outcome.messages
                    t.sealed_len = len(t.turn_messages)
                    t.turn_input_index = timeout_outcome.turn_input_index
                    yield WarningEvent(
                        code="context_auto_compaction_retry",
                        message="Context compacted; retrying the provider request.",
                    )
                    yield CompactionEvent(
                        compaction_id=timeout_outcome.compaction_id,
                        summary=timeout_outcome.summary,
                        kept_entries=timeout_outcome.kept_entries,
                        kept_count=len(timeout_outcome.messages),
                        removed_count=timeout_outcome.removed_count,
                    )
                    t.call_attempt += 1
                    t.attempt_started_at = t.loop.time()
                    t.attempt_flow = AttemptFlow.RETRY
                    return
                decision = t.attempt_policy.decide(
                    kind,
                    t.attempt_history,
                    message=t.provider_error.message,
                    retry_after_s=_hint,
                    is_timeout=_is_timeout,
                    failover_available=False,
                )
            if isinstance(decision, Retry):
                t.log.warning(
                    "provider.retry",
                    attempt=t.retry_attempt + 1,
                    kind=kind.value,
                    reason=decision.reason,
                    delay_s=round(decision.delay_s, 2),
                    is_timeout=_is_timeout,
                )
                if decision.reason == "retry_after":
                    yield WarningEvent(
                        code="provider_retry_after_wait",
                        message=(
                            "The provider asked to retry in "
                            f"{_hint:g}s; waiting, then retrying "
                            "the same model."
                        ),
                    )
                elif _is_timeout:
                    yield WarningEvent(
                        code="provider_timeout_retry",
                        message=("The upstream model timed out; retrying before giving up."),
                    )
                await _sleep_unless_aborted(decision.delay_s)
                t.attempt_history.note_retry(decision.delay_s, timeout=_is_timeout)
                t.retry_attempt += 1
                t.call_attempt += 1
                t.attempt_started_at = t.loop.time()
                t.attempt_flow = AttemptFlow.RETRY
                return
            yield self._transition(AgentState.ERROR)
            t.terminal_error = ErrorEvent(
                message=decision.message
                if isinstance(decision, Fail)
                else t.provider_error.message,
                code=t.provider_error.code,
            )
            yield t.terminal_error
            t.attempt_flow = AttemptFlow.EXIT
            return

    async def _turn_commit_response(self, t: TurnState) -> AsyncGenerator[AgentEvent, None]:
        """Check the final response and append it to the transcript."""
        t.response_text = "".join(t.assistant_text_parts)
        final_stop_reason = (
            getattr(t.provider_done_for_log, "stop_reason", None)
            if t.provider_done_for_log is not None
            else None
        )
        if final_stop_reason is not None:
            t.turn_stop_reason = str(final_stop_reason)
            t.turn_stop_details = getattr(t.provider_done_for_log, "stop_details", None)
        final_classification = _classify_provider_attempt(
            text=t.response_text,
            tool_calls=t.tool_calls,
            pending_tools=t.pending_tools,
            got_done_event=t.got_done_event,
            stop_reason=final_stop_reason,
            reasoning_content=t.iter_reasoning_content,
            reasoning_tokens=t.iter_reasoning_tokens,
            user_visible_emitted=t.attempt_user_visible_emitted,
        )
        if final_classification.kind not in {
            _ProviderAttemptKind.OK,
            _ProviderAttemptKind.REFUSAL,
        }:
            logger.warning(
                "provider.invalid_response_unhandled",
                session_key=self._session_key,
                model=t.last_actual_model or self.config.model_id or "",
                provider=type(self.provider).__name__,
                classification=final_classification.kind.value,
                iteration=t.iterations,
                call_attempt=t.call_attempt,
                got_done_event=t.got_done_event,
                stop_reason=final_stop_reason,
                iter_input_tokens=t.iter_input_tokens,
                iter_output_tokens=t.iter_output_tokens,
                iter_reasoning_tokens=t.iter_reasoning_tokens,
                reasoning_chars=len(t.iter_reasoning_content or ""),
            )
            yield self._transition(AgentState.ERROR)
            if final_classification.kind == _ProviderAttemptKind.INCOMPLETE_TOOLS:
                t.terminal_error = ErrorEvent(
                    message="Provider stream ended with an incomplete tool call",
                    code="incomplete_tool_stream",
                )
                yield t.terminal_error
                t.stop = True
                return
            if final_classification.kind == _ProviderAttemptKind.STREAM_INCOMPLETE:
                t.terminal_error = ErrorEvent(
                    message="Provider stream ended before a done event",
                    code="provider_stream_incomplete",
                )
                yield t.terminal_error
                t.stop = True
                return
            if final_classification.kind == _ProviderAttemptKind.LENGTH_CAPPED:
                t.terminal_error = ErrorEvent(
                    message=_PROVIDER_OUTPUT_TRUNCATED_REPLY,
                    code="provider_output_truncated",
                )
                yield t.terminal_error
                t.stop = True
                return
            t.terminal_error = ErrorEvent(
                message="Provider returned an empty response",
                code="empty_response",
            )
            yield t.terminal_error
            t.stop = True
            return

        if t.iter_reasoning_content:
            t.final_reasoning_parts.append(t.iter_reasoning_content)

        assembled_text = "".join(t.assistant_text_parts)
        t.visible_text = strip_synthetic_tool_call_suffix(
            assembled_text,
            [tc.tool_name for tc in t.tool_calls if tc.synthetic_from_text],
        )
        if t.visible_text:
            t.final_text_parts.append(t.visible_text)

        t.preflight_tool_results = {}
        t.terminal_projection_preflight_error = False
        resolved_tool_calls: list[ToolCall] = []
        for tc in t.tool_calls:
            resolved = self._projection._rehydrate_projected_tool_arguments(tc)
            if isinstance(resolved, ToolResult):
                t.preflight_tool_results[tc.tool_use_id] = resolved
                if self._projection._is_provider_context_projection_reuse_result(resolved):
                    t.terminal_projection_preflight_error = True
                resolved_tool_calls.append(
                    self._projection._sanitize_projected_tool_call_arguments(tc)
                )
                continue
            resolved_tool_calls.append(resolved)
        t.tool_calls = resolved_tool_calls

        # Build assistant message for history. Per-block thinking
        # (each block with its own signature / redacted data, in
        # stream order) is replayed unchanged; the joined text with the
        # last signature is only the fallback for older providers.
        assistant_content: list[Any] = []
        done_thinking_blocks = list(getattr(t.provider_done_for_log, "thinking_blocks", None) or [])
        if done_thinking_blocks:
            assistant_content.extend(done_thinking_blocks)
        elif t.iter_reasoning_content and t.iter_thinking_signature:
            assistant_content.append(
                ContentBlockThinking(
                    thinking=t.iter_reasoning_content,
                    signature=t.iter_thinking_signature,
                )
            )
        if t.visible_text:
            assistant_content.append(ContentBlockText(text=t.visible_text))
        for tc in t.tool_calls:
            assistant_content.append(
                ContentBlockToolUse(
                    id=tc.tool_use_id,
                    name=tc.tool_name,
                    input=tc.arguments,
                    thought_signature=tc.thought_signature,
                )
            )
        if assistant_content:
            t.turn_messages.append(
                Message(
                    role="assistant",
                    content=assistant_content,
                    reasoning_content=t.iter_reasoning_content,
                    reasoning_details=getattr(t.provider_done_for_log, "reasoning_details", None),
                    # Read after the stream: a failover wrapper now
                    # reports the provider that actually answered.
                    provenance=provenance_for_provider(self.provider),
                )
            )
        # The response is on record (turn_messages, final_text_parts): it is
        # no longer a partial reply, so a cancel or abort from here on must
        # not seal it a second time.
        t.assistant_text_parts = []

        # Detect incomplete tool calls (stream interrupted mid-generation)
        if t.pending_tools and not t.tool_calls:
            t.log.warning(
                "agent.stream_interrupted",
                session_key=self._session_key,
                pending_tool_ids=list(t.pending_tools.keys()),
                pending_tool_names=[acc.tool_name for acc in t.pending_tools.values()],
                got_done_event=t.got_done_event,
                text_len=len(assembled_text),
                iteration=t.iterations,
            )
        if not t.got_done_event and (assembled_text or t.pending_tools):
            t.log.warning(
                "agent.provider_stream_incomplete",
                session_key=self._session_key,
                got_text=bool(assembled_text),
                pending_tools=len(t.pending_tools),
                tool_calls=len(t.tool_calls),
            )

        # No tool calls → we're done

    async def _turn_final_answer(self, t: TurnState) -> AsyncGenerator[AgentEvent, None]:
        """The response called no tools: end the turn or take a steering message."""
        t.max_iterations_finalization_pending = False
        # The model is answering rather than acting: this is the
        # moment it stops, and the last chance to notice that it is
        # stopping on edits nothing checked.
        verification_decision = t.verification_ledger.decide(
            is_messaging_surface=_is_messaging_surface(self._session_key)
        )
        if verification_decision.needs_verification:
            nudge = build_verification_nudge(verification_decision)
            self._write_turn_call_log(
                "verification_ledger",
                reason=verification_decision.reason,
                changed_paths=list(verification_decision.changed_paths),
                details=verification_decision.details,
                guidance=nudge,
            )
            yield WarningEvent(code="unverified_code_changes", message=nudge)
        # A message the user sent meanwhile outranks ending the turn.
        if not (
            t.max_iterations_finalization_attempted or t.artifact_delivery_final_response_pending
        ):
            steered = await take_steering(t.steering, t.turn_messages, self._turn_steered_entry_ids)
            if steered is not None:
                yield steered
                if self._turn_progress_sink is not None:
                    # The answer and the message taken after it
                    # are on record before the next model call.
                    t.sealed_len = self._seal_transcript(t.turn_messages, t.sealed_len)
                    await self._report_turn_progress(
                        t.turn_messages,
                        round_index=t.iterations,
                        phase=PHASE_ROUND_COMPLETE,
                    )
                yield self._transition(AgentState.THINKING)
                t.next_iteration = True
                return
        t.stop = True
        return

    async def _turn_execute_tools(self, t: TurnState) -> AsyncGenerator[AgentEvent, None]:
        """Run the response's tool calls as one batch."""
        t.tool_deadline = t.loop.time() + self.config.iteration_timeout

        # A batch that repeats a dead end is answered, not run.
        t.loop_block = t.progress_watchdog.admit(
            [proposed_tool_call(tc.tool_name, tc.arguments) for tc in t.tool_calls],
            iteration=t.iterations,
        )
        if t.loop_block is not None:
            self._write_turn_call_log("progress_watchdog", **loop_guard_log_fields(t.loop_block))
            if t.loop_block.action == "block":
                t.preflight_tool_results.update(blocked_batch_results(t.loop_block, t.tool_calls))
            else:
                t.loop_block = None

        # The calls are on record before they run: a crash from here
        # on leaves them in history with an unknown outcome instead of
        # losing them.
        await self._report_turn_progress(
            t.turn_messages, round_index=t.iterations, phase=PHASE_TOOLS_RUNNING
        )

        # ------ STREAMING → TOOL_CALLING ------
        yield self._transition(AgentState.TOOL_CALLING)

        # Execute tools under each tool's declared concurrency and
        # deliver every result as soon as it finishes; approvals are
        # awaited concurrently. ``outcomes`` keeps the original
        # tool_calls order, one result per tool_use.
        t.tool_batch = ToolBatchExecutor(
            execute=self._execute_tool,
            project=self._projection._project_tool_result_for_delivery,
            timeout_for=self._tool_execution_timeout,
            spec_for=registry_spec_lookup(self._tool_registry),
            deadline=t.tool_deadline,
            total_deadline=t.total_deadline,
            iteration_timeout=self.config.iteration_timeout,
            session_key=self._session_key,
            max_concurrency=self._max_safe_tool_concurrency(),
            heartbeat_interval=self._tool_activity_heartbeat_interval(),
            approval_wait_timeout=self._approval_wait_timeout(),
            pending_approval=_pending_approval_payload,
            wait_for_approval=self.turn_timing.timed_approval(
                partial(wait_for_pending_approval, self._approval_waiter)
            ),
            outside_wait=self.turn_timing.timed_approval(_await_outside_slot),
            artifact_event=lambda a: ArtifactEvent(**_artifact_event_kwargs(a)),
            pending_warnings=self._pending_warnings,
            preflight_results=t.preflight_tool_results,
            call_log=self._write_turn_call_log,
            iteration=t.iterations,
            clock=t.loop.time,
            steering_pending=(None if t.steering is None else lambda: steering_pending(t.steering)),
        )
        # Cooperative abort checkpoint: the last one before tools run. A stop
        # that landed after the response streamed (or while the consumer
        # handled its events) ends the turn here; the sealed record pairs
        # these calls with cancelled results.
        _raise_if_turn_aborted()
        batch_signal = current_turn_stop_signal()
        if batch_signal is not None:
            batch_signal.in_tool_batch = True
        try:
            async for event in self.turn_timing.tool_batch(
                t.tool_batch.run(t.tool_calls), calls=len(t.tool_calls)
            ):
                yield event
        finally:
            if batch_signal is not None:
                batch_signal.in_tool_batch = False

    async def _turn_after_tools(self, t: TurnState) -> AsyncGenerator[AgentEvent, None]:
        """Guards after the batch, then feed results back and decide what is next."""
        tool_result_blocks = t.tool_batch.result_blocks()
        executed_results = t.tool_batch.executed_results
        turn_yielded = any(
            self._is_turn_yield_result(result) or result.terminates_turn
            for result in executed_results
        )

        terminal_artifacts = self._terminal_artifact_delivery_artifacts(executed_results)
        if terminal_artifacts:
            t.artifact_delivery_final_response_artifacts = terminal_artifacts

        t.turn_tool_errors += sum(1 for result in executed_results if result.is_error)
        first_tool_error = next(
            (result for result in executed_results if result.is_error),
            None,
        )
        arguments_by_id = {tc.tool_use_id: tc.arguments for tc in t.tool_calls}

        # Record what this iteration changed and whether it checked it.
        # Order matters: a check only counts for edits made before it.
        for result in executed_results:
            if result.is_error:
                continue
            arguments = arguments_by_id.get(result.tool_use_id) or {}
            if result.tool_name in _MUTATING_TOOL_NAMES:
                t.verification_ledger.record_mutation(_mutated_paths(arguments))
            elif result.tool_name in _COMMAND_TOOL_NAMES:
                t.verification_ledger.record_command(
                    str(arguments.get("command") or arguments.get("code") or "")
                )
        watchdog_decision = t.progress_watchdog.observe(
            ProgressObservation(
                iteration=t.iterations,
                provider_call_count=t.turn_llm_calls,
                successful_tool_result=any(not result.is_error for result in executed_results),
                user_visible_output=bool("".join(t.final_text_parts).strip()),
                artifact_completed=bool(terminal_artifacts),
                tool_error_signature=(
                    None
                    if first_tool_error is None
                    else (f"{first_tool_error.tool_name}:{str(first_tool_error.content)[:160]}")
                ),
                tool_calls=tuple(
                    tool_call_signature(
                        result.tool_name,
                        arguments_by_id.get(result.tool_use_id),
                        result.content,
                        is_error=result.is_error,
                    )
                    for result in executed_results
                ),
            )
        )
        if watchdog_decision.action != "observe":
            self._write_turn_call_log(
                "progress_watchdog", **loop_guard_log_fields(watchdog_decision)
            )
            if not t.progress_watchdog.observe_only:
                append_guidance(tool_result_blocks, t.tool_calls, watchdog_decision)
        if t.loop_block is not None and t.loop_block.terminal:
            yield self._transition(AgentState.ERROR)
            t.terminal_error = loop_detected_error(t.loop_block)
            yield t.terminal_error
            t.stop = True
            return
        t.terminal_error = self._turn_budget_error(t)
        if t.terminal_error is not None:
            if t.artifact_delivery_final_response_pending:
                yield self._finish_artifact_delivery_degraded(
                    t,
                    reason=t.terminal_error.message,
                    code=t.terminal_error.code,
                )
                t.terminal_error = None
            else:
                yield self._transition(AgentState.ERROR)
                yield t.terminal_error
            t.stop = True
            return

        if any(_is_threshold_denial(result) for result in executed_results):
            yield self._transition(AgentState.ERROR)
            t.terminal_error = ErrorEvent(
                message=(
                    "Autonomous execution paused after repeated sandbox denials. "
                    "Human intervention is required before continuing."
                ),
                code="sandbox_threshold_exceeded",
            )
            yield t.terminal_error
            t.stop = True
            return

        # Per-iteration deadline check after tool execution
        if t.loop.time() > t.tool_deadline:
            yield self._transition(AgentState.ERROR)
            t.terminal_error = ErrorEvent(
                message=(
                    f"Iteration {t.iterations} exceeded iteration_timeout"
                    f" ({self.config.iteration_timeout}s) during tool execution"
                ),
                code="iteration_timeout",
            )
            yield t.terminal_error
            t.stop = True
            return

        # Feed tool results back as user message
        t.turn_messages.append(
            Message(role="user", content=tool_result_blocks)  # type: ignore[arg-type]
        )
        if t.terminal_projection_preflight_error:
            self._write_turn_call_log(
                "tool_argument_projection_rehydrate_recovery",
                iteration=t.iterations,
                tool_use_ids=sorted(t.preflight_tool_results),
            )
        if terminal_artifacts:
            self._finish_artifact_delivery_without_provider(t)
            t.stop = True
            return
        steered = await take_steering(t.steering, t.turn_messages, self._turn_steered_entry_ids)
        if steered is not None:
            yield steered
        elif turn_yielded:
            t.stop = True
            return

        if self._turn_progress_sink is not None:
            # The round is complete: seal it now (the next request
            # would seal it unchanged) and put it on record.
            t.sealed_len = self._seal_transcript(t.turn_messages, t.sealed_len)
            await self._report_turn_progress(
                t.turn_messages, round_index=t.iterations, phase=PHASE_ROUND_COMPLETE
            )

        # ------ TOOL_CALLING → THINKING ------
        yield self._transition(AgentState.THINKING)
        # Loop continues

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _stream_provider_events_with_deadline(
        self,
        stream: AsyncIterator[Any],
        *,
        loop: asyncio.AbstractEventLoop,
        total_deadline: float | None,
    ) -> AsyncIterator[Any]:
        stream_iter = stream.__aiter__()
        while True:
            wait_budget = max(0.001, self.config.iteration_timeout)
            if total_deadline is not None:
                remaining_total = total_deadline - loop.time()
                if remaining_total <= 0:
                    await self._close_provider_stream(stream_iter)
                    raise TimeoutError(f"Agent total timeout after {self.config.timeout}s")
                wait_budget = min(wait_budget, remaining_total)

            next_event: asyncio.Future[Any] = asyncio.ensure_future(stream_iter.__anext__())
            stop_signal = current_turn_stop_signal()
            abort_wait: asyncio.Future[Any] | None = (
                asyncio.ensure_future(stop_signal.wait_abort()) if stop_signal is not None else None
            )
            try:
                waiting = {next_event} if abort_wait is None else {next_event, abort_wait}
                done, _ = await asyncio.wait(
                    waiting, timeout=wait_budget, return_when=asyncio.FIRST_COMPLETED
                )
            except asyncio.CancelledError:
                if abort_wait is not None:
                    abort_wait.cancel()
                # The pending __anext__ runs in its own task: cancelling this
                # turn does not stop it, and the provider stream (and its HTTP
                # connection) would stay open until GC. Stop it, close the
                # stream, then let the cancellation through.
                next_event.cancel()
                with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration, Exception):
                    await next_event
                await self._close_provider_stream(stream_iter)
                raise
            if abort_wait is not None:
                if abort_wait in done:
                    # Asked to stop mid-stream: stop the pending read, close
                    # the stream (and its HTTP connection) and end the turn.
                    # The abort wins over an event that was already buffered
                    # in the same tick: a stop must not let a buffered
                    # tool call through.
                    next_event.cancel()
                    with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration, Exception):
                        await next_event
                    await self._close_provider_stream(stream_iter)
                    raise _TurnAbortedError
                abort_wait.cancel()
            if not done:
                next_event.cancel()
                with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                    await next_event
                # Both timeout exits abandon the generator without ever
                # resuming it, so its own `async with client.stream(...)`
                # __aexit__ (closing the underlying httpx connection) would
                # otherwise only run whenever the GC finalizes it -- not
                # deterministic, and can lag significantly in a long-lived
                # gateway process. Close explicitly before raising either way.
                await self._close_provider_stream(stream_iter)
                if total_deadline is not None and loop.time() >= total_deadline:
                    raise TimeoutError(f"Agent total timeout after {self.config.timeout}s")
                raise _IterationStreamTimeoutError
            try:
                yield next_event.result()
            except StopAsyncIteration:
                return

    @staticmethod
    async def _close_provider_stream(stream_iter: AsyncIterator[Any]) -> None:
        aclose = getattr(stream_iter, "aclose", None)
        if not callable(aclose):
            return
        try:
            await aclose()
        except Exception as exc:  # noqa: BLE001 - cleanup must not mask timeout
            logger.debug("provider_stream.close_failed", error=str(exc))

    def _provider_request_messages(
        self,
        messages: list[Message],
        *,
        finalization_message: Message | None = None,
    ) -> list[Message]:
        """Return the provider request view: the sealed transcript as is.

        Every rewrite the old request builder did per call (tool-result
        overrides, marker stripping, aggregate compaction, argument
        sanitizing, pairing repair, metadata sanitizing) now happens once,
        when a message is sealed (:meth:`_seal_transcript`), so consecutive
        requests share a byte-identical prefix. Only the max-iterations
        finalization prompt is request-scoped.
        """
        if finalization_message is not None:
            return [*messages, finalization_message]
        return list(messages)

    def _run_request_hooks(
        self, request_messages: list[Message], *, iteration: int, attempt: int
    ) -> tuple[Veto | None, list[str]]:
        """Fan ``before_request`` out; the first veto wins, amendments add up."""

        call = RequestHookCall(
            messages=tuple(request_messages),
            session_key=self._session_key,
            iteration=iteration,
            attempt=attempt,
        )
        notes: list[str] = []
        for hook in self._request_hooks:
            name = getattr(hook, "name", type(hook).__name__)
            try:
                decision = hook.before_request(call)
            except Exception as exc:  # noqa: BLE001 - a hook must not break the loop
                logger.warning(
                    "agent.request_hook_failed",
                    hook=name,
                    error=str(exc),
                    fail_closed=bool(getattr(hook, "fail_closed", False)),
                )
                if getattr(hook, "fail_closed", False):
                    return Veto(reason=f"The request guard {name!r} failed."), notes
                continue
            if isinstance(decision, Veto):
                self._write_turn_call_log("request_hook_veto", hook=name, reason=decision.reason)
                return decision, notes
            if isinstance(decision, Amend) and decision.context.strip():
                notes.append(decision.context)
        return None, notes

    @staticmethod
    def _seal_cancelled_turn(messages: list[Message], partial_text: str) -> list[Message]:
        """The turn's messages made safe to replay after a cancellation.

        Tool calls still waiting for a result get a cancelled error result, and
        the turn ends with an assistant message holding any text that had
        streamed plus :data:`TURN_INTERRUPTED_NOTE`, so the next request needs
        no pairing repair and the model knows tools may have partly run.
        """

        sealed = list(messages)
        answered = {
            block.tool_use_id
            for message in sealed
            if isinstance(message.content, list)
            for block in message.content
            if isinstance(block, ContentBlockToolResult)
        }
        pending = [
            block
            for message in sealed
            if message.role == "assistant" and isinstance(message.content, list)
            for block in message.content
            if isinstance(block, ContentBlockToolUse) and block.id not in answered
        ]
        if pending:
            sealed.append(
                Message(
                    role="user",
                    content=[
                        ContentBlockToolResult(
                            tool_use_id=block.id,
                            content=CANCELLED_TOOL_RESULT_CONTENT,
                            is_error=True,
                        )
                        for block in pending
                    ],
                )
            )
        text = partial_text.rstrip()
        note = f"{text}\n\n{TURN_INTERRUPTED_NOTE}" if text else TURN_INTERRUPTED_NOTE
        sealed.append(Message(role="assistant", content=[ContentBlockText(text=note)]))
        return sealed

    def _seal_transcript(self, messages: list[Message], sealed_len: int) -> int:
        """Seal ``messages[sealed_len:]`` in place; return the new sealed length."""
        if sealed_len >= len(messages):
            return len(messages)
        report = seal_window(
            messages,
            sealed_len,
            shrink_tool_result=self._projection._shrink_tool_result_for_transcript,
            repair_prompt=_PROVIDER_CONTEXT_REPAIR_PROMPT,
        )
        if report.replay_pairs_stripped:
            self.config.metadata["tool_argument_projection_replay_stripped"] = (
                self.config.metadata.get("tool_argument_projection_replay_stripped", 0)
                + report.replay_pairs_stripped
            )
            self._write_turn_call_log(
                "tool_argument_projection_replay_stripped",
                tool_use_ids=sorted(report.stripped_tool_use_ids),
                stripped_blocks=report.replay_pairs_stripped * 2,
            )
        if report.pairing_repairs:
            logger.warning(
                "agent.transcript_seal_pairing_repaired",
                session_key=self._session_key,
                start=sealed_len,
            )
        return len(messages)

    def _drop_replayed_reasoning(self, thinking_enabled: bool) -> bool:
        """Whether this model must not receive replayed reasoning (opt-in drop).

        Thinking blocks (with their signatures) and ``reasoning_content`` are
        part of the append-only transcript and replayed by default; providers
        that cannot use them skip them when serializing. They are dropped on
        load only when the model's capabilities say it takes no reasoning at
        all and this turn does not ask for it.
        """
        caps = self.config.model_capabilities
        if caps is None or thinking_enabled:
            return False
        if bool(getattr(caps, "supports_reasoning", False)):
            return False
        return str(getattr(caps, "reasoning_format", "none") or "none") == "none"

    def set_transcript_input_entry_id(
        self,
        entry_id: str | None,
        content: str | None = None,
    ) -> None:
        """Record the persisted user row this turn's input message replays.

        The structured record names it so a reload does not replay the row
        twice; a compaction keeps it (by its stored *content*) when it
        rewrites the durable transcript.
        """
        self._transcript_input_entry_id = entry_id
        self._transcript_input_entry_content = content if entry_id is not None else None

    def _turn_transcript_record(self, messages: list[Message]) -> TranscriptRecord:
        if self._turn_reset:
            appended = list(messages)
        else:
            appended = list(messages[self._turn_base_len :])
        return TranscriptRecord(
            messages=persistable_messages(appended),
            mode="reset" if self._turn_reset else "append",
            input_entry_id=self._transcript_input_entry_id,
            compaction_id=self._turn_compaction_id,
            steered_entry_ids=tuple(self._turn_steered_entry_ids),
        )

    def _record_turn_transcript(self, messages: list[Message]) -> None:
        self._turn_record = self._turn_transcript_record(messages)

    def set_turn_progress_sink(self, sink: TurnProgressSink | None) -> None:
        """Receive the turn's record after each tool round (``None`` stops it)."""
        self._turn_progress_sink = sink

    async def _report_turn_progress(
        self,
        messages: list[Message],
        *,
        round_index: int,
        phase: TurnProgressPhase,
    ) -> None:
        sink = self._turn_progress_sink
        if sink is None:
            return
        try:
            record = encode_transcript_record(self._turn_transcript_record(messages))
            await sink(TurnProgress(record=record, round=round_index, phase=phase))
        except Exception:  # noqa: BLE001 - progress is best-effort
            logger.warning(
                "agent.turn_progress_failed",
                session_key=self._session_key,
                round=round_index,
                phase=phase,
                exc_info=True,
            )

    def transcript_record(self) -> dict[str, Any] | None:
        """The structured record of the last successful turn, for persistence.

        ``None`` when the last turn failed (error turns are persisted by the
        runtime as flat rows) or produced nothing.
        """
        record = self._turn_record
        if record is None or (record.mode == "append" and not record.messages):
            return None
        return encode_transcript_record(record)

    @staticmethod
    def _provider_request_is_smaller(before: list[Message], after: list[Message]) -> bool:
        return len(after) < len(before) or session_payload_chars(after) < session_payload_chars(
            before
        )

    def _runtime_context_block(self) -> str:
        now = datetime.now().astimezone()
        tzinfo = now.tzinfo
        tz_name = getattr(tzinfo, "key", None) or str(tzinfo) if tzinfo is not None else "local"
        lines = [
            RUNTIME_CONTEXT_HEADER,
            f"Current local date/time: {now.isoformat(timespec='minutes')} ({now.strftime('%a')})",
            f"Time zone / location hint: {tz_name}",
            "Use this runtime context for questions about the current date, time, or local "
            "time zone. Do not treat it as a user request.",
        ]
        return "\n".join(lines)

    @staticmethod
    def _cache_breakpoints_without_runtime_context(
        cache_breakpoints: list[dict[str, str]] | None,
    ) -> list[dict[str, str]] | None:
        if not cache_breakpoints:
            return None
        return list(cache_breakpoints)

    def _skills_context_block(self) -> str | None:
        prompt = self.config.skills_context_prompt
        if not prompt or not prompt.strip():
            return None
        lines = [
            SKILLS_CONTEXT_HEADER,
            "This is runtime-provided context, not a user request.",
            "Use it only to decide whether to call skill_view for the current task.",
            prompt.strip(),
        ]
        return "\n".join(lines)

    def _transition(self, to: AgentState) -> StateChangeEvent:
        ev = StateChangeEvent(from_state=self._state, to_state=to)
        self._state = to
        return ev

    @staticmethod
    def _is_turn_yield_result(result: ToolResult) -> bool:
        if result.tool_name != "sessions_yield" or result.is_error:
            return False
        try:
            payload = json.loads(result.content)
        except json.JSONDecodeError:
            return False
        if not isinstance(payload, dict):
            return False
        return payload.get("status") == "yielded"

    @staticmethod
    def _terminal_artifact_delivery_artifacts(
        results: list[ToolResult],
    ) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        for result in results:
            if result.tool_name != "publish_artifact" or result.is_error:
                continue
            if result.artifacts:
                artifacts.extend(result.artifacts)
                continue
            try:
                payload = json.loads(result.content)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("status") not in {"published", "already_published"}:
                continue
            artifact = payload.get("artifact")
            artifacts.append(artifact if isinstance(artifact, dict) else {})
        return artifacts

    @staticmethod
    def _artifact_delivery_final_response_text(
        artifacts: list[dict[str, Any]],
    ) -> str:
        names = [
            str(item.get("name") or item.get("filename") or "").strip()
            for item in artifacts
            if isinstance(item, dict)
        ]
        named = [name for name in names if name]
        if named:
            return "The generated file is ready: " + ", ".join(named) + "."
        return "The generated file is ready."

    @staticmethod
    def _live_request_jsonable(value: Any) -> Any:
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            try:
                return model_dump(mode="json", exclude_none=True)
            except TypeError:
                return model_dump(mode="json")
        if isinstance(value, list | tuple):
            return [Agent._live_request_jsonable(item) for item in value]
        if isinstance(value, dict):
            return {str(key): Agent._live_request_jsonable(item) for key, item in value.items()}
        if hasattr(value, "__dict__"):
            return {
                str(key): Agent._live_request_jsonable(item)
                for key, item in vars(value).items()
                if not str(key).startswith("_")
            }
        try:
            json.dumps(value)
        except TypeError:
            return repr(value)
        return value

    def _estimate_live_request_tokens(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition] | None = None,
        config: ChatConfig | None = None,
    ) -> int:
        """Estimate the current provider request size without lifetime usage.

        Same value as ``len(json.dumps(request, sort_keys=True)) // 4``, but
        each sealed message is serialized once per turn (memoized), not once
        per provider call.
        """

        payload: dict[str, Any] = {}
        if tools:
            payload["tools"] = [self._live_request_jsonable(tool) for tool in tools]
        if config is not None:
            if config.system:
                payload["system"] = config.system
            config_payload = config.model_dump(
                mode="json",
                exclude_none=True,
                exclude={"system", "model_capabilities"},
            )
            payload.update(config_payload)

        rest_chars = len(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
        # '"messages": ' + the list, plus ", " when other keys exist.
        messages_chars = len('"messages": ') + self._message_sizes.messages_chars(messages)
        estimated_chars = rest_chars + messages_chars + (2 if payload else 0)
        return max(1, estimated_chars // 4)

    def _message_tokens(self, messages: Sequence[Message]) -> int:
        return max(0, self._message_sizes.messages_chars(messages) // 4)

    def _calibration_key(self) -> tuple[str, str]:
        return str(getattr(self.provider, "provider_name", "") or ""), str(
            self.config.model_id or ""
        )

    def estimate_scale(self) -> float:
        """Calibration factor for this provider/model's estimates (1.0 = raw)."""
        if self.estimate_calibration is None:
            return 1.0
        return self.estimate_calibration.factor(*self._calibration_key())

    def _observe_estimate(
        self,
        estimated_tokens: int,
        provider_input_tokens: int,
        *,
        usage_estimated: bool = False,
        request_messages: Sequence[Message] = (),
    ) -> float:
        """Feed one provider usage sample to the calibration; return the factor.

        Estimated usage and requests with image/document blocks are skipped.
        """
        if self.estimate_calibration is None:
            return 1.0
        provider, model = self._calibration_key()
        self.estimate_calibration.observe(
            provider,
            model,
            estimated_tokens=estimated_tokens,
            provider_input_tokens=provider_input_tokens,
            usage_estimated=usage_estimated,
            has_media=not usage_estimated and request_has_media(request_messages),
        )
        return round(self.estimate_calibration.factor(provider, model), 3)

    def _compaction_coordinator(self) -> CompactionCoordinator:
        return CompactionCoordinator(self.config, self.provider, self._session_key)

    def _compaction_service(self, *, window_tokens: int | None = None) -> CompactionService:
        del window_tokens
        return self._compaction_coordinator().service()

    def _compaction_budget_tokens(self) -> int | None:
        """The provider's own no-rewrite request budget, in tokens.

        Providers project/compact any payload over their effective proof
        budget (``provider_request_max_chars`` minus headroom) on every
        request. Compacting under that line keeps the provider payload
        untouched -- and its prompt cache warm -- instead of letting it be
        rewritten request after request in the band between the two.
        """
        proof_chars = self._provider_request_proof_max_chars()
        if proof_chars <= 0:
            return None
        return max(1, effective_proof_budget(proof_chars) // 4)

    def _compaction_input_entry(self) -> dict[str, Any] | None:
        content = self._transcript_input_entry_content
        if self._transcript_input_entry_id is None or content is None:
            return None
        return {"role": "user", "content": content}

    async def _preflight_context_overflow(
        self,
        turn_messages: list[Message],
        estimated_context_tokens: int,
        *,
        turn_input_index: int,
    ) -> CompactionOutcome | None:
        """Compact before a provider call when the live request is over threshold.

        Delegates to :class:`CompactionService` (one trigger, one shape). The
        current turn -- this turn's input and every tool round since -- is kept
        verbatim after a single summary exchange.

        Returns an uncompacted outcome when the request is under the threshold
        or there is nothing to compact, and ``None`` when compaction failed.

        The estimate (and every message-size estimate the service makes) is
        scaled by the provider/model calibration factor, 1.0 until calibrated.
        """
        scale = self.estimate_scale()
        return await self._check_context_overflow(
            turn_messages,
            max(1, int(estimated_context_tokens * scale)),
            turn_input_index=turn_input_index,
            token_scale=scale,
            trigger="preflight",
        )

    async def _check_context_overflow(
        self,
        messages: list[Message],
        estimated_context_tokens: int,
        *,
        turn_input_index: int | None = None,
        compaction_window_tokens: int | None = None,
        force: bool = False,
        token_scale: float = 1.0,
        trigger: CompactionTrigger = "overflow",
    ) -> CompactionOutcome | None:
        """Run compaction for *trigger*; ``None`` signals a failed compaction."""
        self._last_compaction_refusal_reason = None
        window_tokens = compaction_window_tokens or self.config.context_window_tokens
        input_index = len(messages) if turn_input_index is None else turn_input_index
        declined_at = self._turn_compaction_declined_tokens
        if not force and declined_at is not None and estimated_context_tokens <= declined_at * 1.25:
            # This turn already found compaction not worth it at about this
            # size; do not pay for (or log) the same attempt on every request.
            return CompactionOutcome(messages=messages, turn_input_index=input_index)
        result = await self._compaction_coordinator().compact(
            messages,
            trigger=trigger,
            turn_input_index=input_index,
            estimated_tokens=estimated_context_tokens,
            window_tokens=window_tokens,
            message_tokens=(
                self._message_tokens
                if token_scale == 1.0
                else lambda items: int(self._message_tokens(items) * token_scale)
            ),
            force=force,
            input_entry=self._compaction_input_entry(),
            budget_tokens=self._compaction_budget_tokens(),
        )
        if result.status == "failed":
            self._last_compaction_refusal_reason = result.reason or "compaction_failed"
            return None
        if result.reason in _COMPACTION_DECLINE_REASONS and not force:
            self._turn_compaction_declined_tokens = estimated_context_tokens
        if result.reason == "current_turn_too_large":
            # Nothing was summarized or rewritten. Before a call the request
            # goes out as is (the estimate is a heuristic); after a provider
            # overflow it is a terminal, user-facing error.
            self._last_compaction_refusal_reason = "current_turn_too_large"
            if force:
                return None
        if not result.compacted:
            return CompactionOutcome(messages=messages, turn_input_index=input_index)

        # Trigger 6: post-compaction sync
        if self._memory_sync_manager is not None:
            self._memory_sync_manager.mark_dirty()
        self._turn_reset = True
        self._turn_compaction_id = result.compaction_id
        self._message_sizes.retain_only(result.messages)
        return CompactionOutcome(
            messages=result.messages,
            compacted=True,
            summary=result.summary,
            kept_entries=result.kept_entries,
            removed_count=result.removed_count,
            compaction_id=result.compaction_id,
            turn_input_index=result.turn_input_index,
        )

    def _canonical_tool_name(self, name: str, *, tool_use_id: str | None = None) -> str:
        """The offered tool name a model's near-miss name unambiguously means.

        Applied where the stream hands over a tool call, so scheduling,
        timeouts, approvals, events and the persisted ``tool_use`` block all
        carry the registered name. Pass ``tool_use_id`` to log the rewrite.
        """
        offered = [definition.name for definition in self.tool_definitions]
        if not offered or name in offered:
            return name
        from agentos.tools.tool_name_resolution import resolve_tool_name_among

        resolution = resolve_tool_name_among(name, offered)
        if resolution is None:
            return name
        if tool_use_id is not None:
            logger.info(
                "agent.tool_name_resolved",
                session_key=self._session_key,
                tool_use_id=tool_use_id,
                requested=resolution.requested,
                tool=resolution.resolved,
                rules=list(resolution.rules),
            )
        return resolution.resolved

    def _coerce_tool_arguments(self, tool_name: str, arguments: Any) -> Any:
        """Apply the lossless argument repairs dispatch would apply, up front.

        Uses the schema of the tool definition sent with the request, so the
        persisted ``tool_use`` input, replayed history and dispatch all carry
        the value that actually runs. Rejection stays in dispatch: arguments
        that do not fully validate are returned unchanged.
        """
        definition = self._tool_definition_by_name.get(tool_name)
        if definition is None:
            definition = next(
                (item for item in self.tool_definitions if item.name == tool_name), None
            )
        schema = getattr(definition, "input_schema", None)
        if schema is None or not isinstance(arguments, dict):
            return arguments
        from agentos.tools.argument_admission import coerce_tool_arguments

        try:
            coerced = coerce_tool_arguments(
                getattr(schema, "properties", None) or {},
                getattr(schema, "required", None) or [],
                arguments,
            )
        except Exception:  # noqa: BLE001 - dispatch still validates the call
            return arguments
        return arguments if coerced is None else coerced

    async def _execute_tool(self, tc: ToolCall) -> ToolResult:
        """Dispatch a tool call to the registered handler."""
        args_hash = hashlib.sha256(
            json.dumps(tc.arguments, ensure_ascii=False, sort_keys=True, default=str).encode(
                "utf-8"
            )
        ).hexdigest()
        failure_signature = (tc.tool_name, args_hash)
        block_threshold = max(
            0,
            int(getattr(self.config, "tool_failure_loop_block_threshold", 0) or 0),
        )
        if (
            block_threshold > 0
            and self._tool_failure_loop_counts.get(failure_signature, 0) >= block_threshold - 1
        ):
            return ToolResult(
                tool_use_id=tc.tool_use_id,
                tool_name=tc.tool_name,
                content=(
                    f"The exact same {tc.tool_name} call has already failed repeatedly. "
                    "Do not retry this exact call unchanged. Use a different approach, "
                    "change the arguments, or explain the blocker to the user."
                ),
                is_error=True,
                execution_status=runtime_execution_status(
                    "error",
                    reason="tool_failure_loop_exhausted",
                ),
            )
        if self.tool_handler is None:
            result = ToolResult(
                tool_use_id=tc.tool_use_id,
                tool_name=tc.tool_name,
                content=f"No tool handler registered for tool '{tc.tool_name}'",
                is_error=True,
                execution_status=runtime_execution_status(
                    "error",
                    reason="runtime_error",
                ),
            )
        else:
            try:
                resolved = self._projection._rehydrate_projected_tool_arguments(tc)
                if isinstance(resolved, ToolResult):
                    result = resolved
                else:
                    tc = resolved
                    result = await self.tool_handler(tc)
            except Exception as exc:  # noqa: BLE001
                result = ToolResult(
                    tool_use_id=tc.tool_use_id,
                    tool_name=tc.tool_name,
                    content=f"Tool '{tc.tool_name}' raised: {exc}",
                    is_error=True,
                    execution_status=runtime_execution_status(
                        "error",
                        reason="runtime_error",
                    ),
                )
        if result.is_error:
            self._tool_failure_loop_counts[failure_signature] = (
                self._tool_failure_loop_counts.get(failure_signature, 0) + 1
            )
        else:
            self._tool_failure_loop_counts.pop(failure_signature, None)
            if tc.tool_name in {
                "apply_patch",
                "background_process",
                "edit_file",
                "execute_code",
                "exec_command",
                "git_commit",
                "install_skill_deps",
                "write_file",
            }:
                self._tool_failure_loop_counts.clear()
        return result

    # ------------------------------------------------------------------
    # Subagent factory
    # ------------------------------------------------------------------
