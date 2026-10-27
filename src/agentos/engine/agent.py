"""Agent core — explicit state machine + tool loop.

Core loop is under 500 lines. No recursive calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

import structlog

from agentos.artifacts import artifact_payload
from agentos.context_budget import ContextBudgetClass, ContextBudgetGovernor
from agentos.engine.cache_break_monitor import (
    check_response_for_cache_break,
    record_prompt_state,
)
from agentos.engine.compaction_service import CompactionService, auxiliary_chunk_summarizer
from agentos.engine.estimate_calibration import EstimateCalibration, request_has_media
from agentos.engine.fallback import FallbackPolicy, backoff_sleep, retry_after_delay
from agentos.engine.history import limit_turns, repair_tool_pairing
from agentos.engine.progress_watchdog import (
    ProgressObservation,
    ProgressWatchdog,
    guidance_for,
    tool_call_signature,
)
from agentos.engine.session_sanitize import (
    sanitize_session_messages,
    session_payload_chars,
)
from agentos.engine.thinking import drop_reasoning
from agentos.engine.tokenjuice_adapter import reduce_tool_result_with_tokenjuice
from agentos.engine.tool_batch import ToolBatchExecutor, registry_spec_lookup
from agentos.engine.tool_result_store import (
    ToolResultRecord,
    ToolResultStore,
    ToolResultStoreBudgetError,
)
from agentos.engine.tool_text_compat import strip_synthetic_tool_call_suffix
from agentos.engine.tool_token_estimate import estimate_tokens as get_approx_tokens
from agentos.engine.transcript import (
    COMPACTED_TOOL_ARGUMENT_MARKERS as _COMPACTED_TOOL_ARGUMENT_MARKERS,
)
from agentos.engine.transcript import (
    HISTORICAL_TOOL_ARGUMENT_PROJECTION_PREFIX as _HISTORICAL_TOOL_ARGUMENT_PROJECTION_PREFIX,
)
from agentos.engine.transcript import (
    INVALID_PROVIDER_CONTEXT_ARGUMENTS_KEY as _INVALID_PROVIDER_CONTEXT_ARGUMENTS_KEY,
)
from agentos.engine.transcript import (
    INVALID_PROVIDER_CONTEXT_PROJECTION_PREFIX as _INVALID_PROVIDER_CONTEXT_PROJECTION_PREFIX,
)
from agentos.engine.transcript import (
    RUNTIME_CONTEXT_HEADER,
    SKILLS_CONTEXT_HEADER,
    MessageSizeCache,
    TranscriptRecord,
    append_context_blocks,
    encode_transcript_record,
    history_argument_projection,
    latest_context_block_present,
    persistable_messages,
    request_context_delta,
    seal_window,
)
from agentos.engine.transcript import (
    TOOL_ARGUMENT_PROJECTION_PREFIX as _TOOL_ARGUMENT_PROJECTION_PREFIX,
)
from agentos.engine.turn_timing import NULL_TURN_TIMING, TurnTiming
from agentos.engine.verification_ledger import (
    VerificationLedger,
    build_verification_nudge,
)
from agentos.execution_status import (
    mark_execution_status_truncated,
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
    ProviderHeartbeatEvent,
    ToolDefinition,
    ToolUseEndEvent,
    provider_metadata,
)
from agentos.provider import (
    DoneEvent as ProviderDoneEvent,
)
from agentos.provider import (
    ErrorEvent as ProviderErrorEvent,
)
from agentos.provider import (
    TextDeltaEvent as ProviderTextDelta,
)
from agentos.provider import (
    ThinkingDeltaEvent as ProviderThinkingDelta,
)
from agentos.provider import (
    ToolUseStartEvent as ProviderToolUseStart,
)
from agentos.provider.failures import (
    ProviderFailureKind,
    classify_provider_error,
    honoured_retry_after,
    is_transport_timeout,
)
from agentos.provider.replay import provenance_for_provider
from agentos.provider.request_proof import effective_proof_budget
from agentos.provider.types import ContentBlockImage, StopReason, normalize_stop_reason
from agentos.result_budget import (
    ToolResultBudgetClass,
    compact_tool_result_content,
    resolve_budget_class,
)
from agentos.session.terminal_reply import build_terminal_reply
from agentos.tool_boundary import AgentToolHandler as ToolHandler
from agentos.tools.registry import ToolRegistry
from agentos.tools.types import ToolContext

from .context import ContextAssembly
from .subagent import SubagentManager, SubagentSpec
from .types import (
    AgentConfig,
    AgentEvent,
    AgentState,
    ArtifactEvent,
    CompactionEvent,
    CompactionOutcome,
    DoneEvent,
    ErrorEvent,
    RunHeartbeatEvent,
    StateChangeEvent,
    TextDeltaEvent,
    ThinkingEvent,
    ThinkingLevel,
    ToolCall,
    ToolResult,
    ToolUseStartEvent,
    WarningEvent,
)

logger = structlog.get_logger("agentos.engine.agent")

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


def _cost_source_for_usage(cost_usd: float, billed_cost: float) -> str:
    if billed_cost > 0.0 and abs(cost_usd - billed_cost) <= 1e-9:
        return "provider_billed"
    if billed_cost > 0.0:
        return "mixed"
    if cost_usd > 0.0:
        return "agentos_estimate"
    return "unavailable"


def _is_deepseek_model_id(model_id: str | None) -> bool:
    normalized = (model_id or "").strip().lower()
    return normalized.startswith("deepseek") or "/deepseek" in normalized


def _is_direct_deepseek_v4_model_id(model_id: str | None) -> bool:
    normalized = (model_id or "").strip().lower()
    return normalized in {"deepseek-v4-flash", "deepseek-v4-pro"}


_LARGE_JSON_TOOL_FIELD_KEYS: frozenset[str] = frozenset({"body", "body_base64"})
_LARGE_JSON_TOOL_FIELD_CHARS = 20_000
_TOOL_ARGUMENT_HEARTBEAT_CHARS = 4096
_PROVIDER_CONTEXT_PROJECTION_REUSED_REASON = "provider_context_projection_reused"
_PROVIDER_CONTEXT_REPAIR_PROMPT = (
    "A previous tool call was rejected because it reused provider-only compacted "
    "tool arguments. Regenerate the complete tool arguments from the available "
    "source context and retry the tool call. Do not copy compacted placeholders."
)
_LARGE_CONTEXT_INVALID_RESPONSE_INPUT_TOKENS = 30_000


def _large_json_field_replacement(value: str) -> dict[str, object]:
    return {
        "omitted": True,
        "omitted_chars": len(value),
        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
        "reason": "large_tool_result_field",
    }


def _omit_large_json_value(value: Any) -> tuple[Any, bool]:
    if isinstance(value, dict):
        changed = False
        sanitized_dict: dict[str, Any] = {}
        for key, item in value.items():
            if (
                key in _LARGE_JSON_TOOL_FIELD_KEYS
                and isinstance(item, str)
                and len(item) > _LARGE_JSON_TOOL_FIELD_CHARS
            ):
                sanitized_dict[key] = _large_json_field_replacement(item)
                changed = True
                continue
            sanitized, child_changed = _omit_large_json_value(item)
            sanitized_dict[key] = sanitized
            changed = changed or child_changed
        return sanitized_dict, changed
    if isinstance(value, list):
        changed = False
        sanitized_list: list[Any] = []
        for item in value:
            sanitized, child_changed = _omit_large_json_value(item)
            sanitized_list.append(sanitized)
            changed = changed or child_changed
        return sanitized_list, changed
    return value, False


def _omit_large_json_tool_fields(content: str) -> tuple[str, bool]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return content, False
    sanitized, changed = _omit_large_json_value(parsed)
    if not changed:
        return content, False
    return json.dumps(sanitized, ensure_ascii=False, indent=2), True


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


_PENDING_APPROVAL_STATUSES: frozenset[str] = frozenset({"approval_required", "approval_pending"})


def _pending_approval_payload(content: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("status") not in _PENDING_APPROVAL_STATUSES:
        return None
    approval_id = payload.get("approval_id")
    if not isinstance(approval_id, str) or not approval_id:
        return None
    return payload


async def _await_outside_slot(awaitable: Awaitable[Any]) -> Any:
    """Identity wrapper for an inline approval wait, so timing can wrap it."""

    return await awaitable


async def _wait_for_pending_approval_resolution(
    payload: dict[str, Any],
    *,
    timeout: float,
) -> None:
    approval_id = payload.get("approval_id")
    if not isinstance(approval_id, str) or not approval_id:
        return
    try:
        from agentos.gateway.approval_queue import get_approval_queue

        await get_approval_queue().wait(approval_id, timeout=timeout)
    except KeyError:
        return


def _tool_result_content_has_artifact(content: str) -> bool:
    try:
        payload = json.loads(content)
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    if isinstance(payload.get("artifact"), dict) or isinstance(payload.get("artifacts"), list):
        return True
    return payload.get("status") in {"published", "already_published"}


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


class _IterationStreamTimeoutError(TimeoutError):
    """Raised when provider streaming exceeds the active Agent iteration budget."""


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


_MUTATING_TOOL_NAMES = frozenset({"write_file", "edit_file", "apply_patch"})
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


def _mutated_paths(arguments: dict[str, Any]) -> list[str]:
    """Pull the touched paths out of a mutating tool's arguments."""

    paths: list[str] = []
    for key in ("path", "file_path", "target"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            paths.append(value.strip())
    patch = arguments.get("patch")
    if isinstance(patch, str):
        # apply_patch names its files inside the patch body.
        for line in patch.splitlines():
            stripped = line.strip()
            for marker in ("*** Update File:", "*** Add File:", "*** Delete File:"):
                if stripped.startswith(marker):
                    candidate = stripped[len(marker) :].strip()
                    if candidate:
                        paths.append(candidate)
    return paths


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


@dataclass
class _StreamAccumulator:
    """Accumulates streaming fragments for a single tool call."""

    tool_use_id: str
    tool_name: str
    synthetic_from_text: bool = False
    json_buf: list[str] = field(default_factory=list)
    json_chars: int = 0

    def finish(self) -> dict[str, Any]:
        raw = "".join(self.json_buf)
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            return {"_raw": raw}


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
        subagent_manager: SubagentManager | None = None,
        usage_tracker: Any | None = None,
        session_key: str | None = None,
        turn_call_logger: TurnCallLogger | None = None,
        memory_sync_manager: Any | None = None,
        tool_registry: ToolRegistry | None = None,
        tool_context: ToolContext | None = None,
        spend_budget_guard: Callable[[str], tuple[bool, str | None]] | None = None,
    ) -> None:
        self.provider = provider
        self.config = config or AgentConfig()
        self.tool_definitions = tool_definitions or []
        self._tool_definition_by_name = {tool.name: tool for tool in self.tool_definitions}
        self.tool_handler = tool_handler
        self.subagent_manager = subagent_manager or SubagentManager()
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
        self._message_sizes = MessageSizeCache(Agent._live_request_jsonable)

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

    @staticmethod
    def _positive_int(value: Any) -> int | None:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

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

    @staticmethod
    def _context_budget_class(
        budget_class: ToolResultBudgetClass | None,
    ) -> ContextBudgetClass:
        if budget_class is ToolResultBudgetClass.EXTERNAL:
            return ContextBudgetClass.EXTERNAL
        if budget_class is ToolResultBudgetClass.ARTIFACT:
            return ContextBudgetClass.ARTIFACT
        if budget_class is ToolResultBudgetClass.ERROR:
            return ContextBudgetClass.ERROR
        if budget_class is ToolResultBudgetClass.CONTROL:
            return ContextBudgetClass.CONTROL
        return ContextBudgetClass.LOCAL

    def _tool_result_provider_request_max_chars(
        self,
        budget_class: ToolResultBudgetClass | None = None,
    ) -> int:
        return self._context_budget_governor().tool_result_provider_chars_for(
            self._context_budget_class(budget_class)
        )

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

    @staticmethod
    def _tool_call_string_arg(
        tool_call: ToolCall | None,
        *names: str,
    ) -> str | None:
        if tool_call is None:
            return None
        for name in names:
            value = tool_call.arguments.get(name)
            if isinstance(value, str) and value.strip():
                return value
        return None

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

    def _tool_result_projection_for_provider(
        self,
        content: str,
        *,
        tool_use_id: str,
        tool_name: str,
        reason: str,
        max_preview_chars: int,
    ) -> str:
        max_preview_chars = max(0, int(max_preview_chars))
        if max_preview_chars > 0:
            max_preview_chars = max(1, min(max_preview_chars, 4_000))
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        stored = self._store_tool_result_snapshot(
            content,
            tool_use_id=tool_use_id,
            tool_name=tool_name,
        )
        handle_line = f"tool_result_handle: {stored.handle}\n" if stored is not None else ""
        if max_preview_chars <= 0:
            head = ""
            tail = ""
        elif len(content) <= max_preview_chars:
            head = content
            tail = ""
        else:
            head_chars = max(1, int(max_preview_chars * 0.65))
            tail_chars = max(0, max_preview_chars - head_chars)
            head = content[:head_chars]
            tail = content[-tail_chars:] if tail_chars else ""
        omitted = max(0, len(content) - len(head) - len(tail))
        projection = (
            "[tool_result_projection]\n"
            f"tool: {tool_name}\n"
            f"tool_use_id: {tool_use_id}\n"
            f"original_chars: {len(content)}\n"
            f"sha256: {digest}\n"
            f"{handle_line}"
            f"omitted_chars: {omitted}\n"
            f"reason: {reason}.\n"
            f"head:\n{head}"
        )
        if tail:
            projection += f"\n...\ntail:\n{tail}"
        return projection

    def _store_tool_result_snapshot(
        self,
        content: str,
        *,
        tool_use_id: str,
        tool_name: str,
    ) -> ToolResultRecord | None:
        if not self.config.tool_result_store_dir:
            return None
        session_id = self.config.tool_result_store_session_id or self._session_key
        session_key = self.config.tool_result_store_session_key or self._session_key
        agent_id = self.config.tool_result_store_agent_id
        if not agent_id and session_key:
            from agentos.session.keys import parse_agent_id

            agent_id = parse_agent_id(session_key)
        if not session_id or not session_key or not agent_id:
            self.config.metadata["tool_result_store_skips"] = (
                self.config.metadata.get("tool_result_store_skips", 0) + 1
            )
            return None
        try:
            record = ToolResultStore(self.config.tool_result_store_dir).write(
                content,
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                session_id=session_id,
                session_key=session_key,
                agent_id=agent_id,
                max_bytes=self.config.tool_result_store_max_bytes,
                disk_budget_bytes=self.config.tool_result_store_disk_budget_bytes,
                retention_seconds=self.config.tool_result_store_retention_seconds,
            )
        except ToolResultStoreBudgetError as exc:
            self.config.metadata["tool_result_store_skips"] = (
                self.config.metadata.get("tool_result_store_skips", 0) + 1
            )
            logger.info(
                "tool_result_store.skipped",
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                reason=str(exc),
            )
            return None
        except Exception as exc:  # pragma: no cover - storage must not break turns
            logger.warning(
                "tool_result_store.write_failed",
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                error=str(exc),
            )
            return None
        self.config.metadata["tool_result_store_writes"] = (
            self.config.metadata.get("tool_result_store_writes", 0) + 1
        )
        return record

    async def _project_tool_result_for_llm(
        self,
        result: ToolResult,
        *,
        tool_call: ToolCall | None = None,
    ) -> ToolResult:
        guarded_content, guarded = _omit_large_json_tool_fields(result.content)
        if guarded:
            result = ToolResult(
                tool_use_id=result.tool_use_id,
                tool_name=result.tool_name,
                content=guarded_content,
                is_error=result.is_error,
                artifacts=list(result.artifacts),
                execution_status=(
                    mark_execution_status_truncated(result.execution_status)
                    if result.execution_status is not None
                    else None
                ),
                terminates_turn=result.terminates_turn,
            )
            self.config.metadata["tool_json_guard_applied"] = True
            self.config.metadata["tool_json_guard_calls"] = (
                self.config.metadata.get("tool_json_guard_calls", 0) + 1
            )

        self.config.metadata["tool_projection_attempts"] = (
            self.config.metadata.get("tool_projection_attempts", 0) + 1
        )
        projected_content = self._tokenjuice_tool_reduction(
            tool_name=result.tool_name,
            content=result.content,
            is_error=result.is_error,
            tool_use_id=result.tool_use_id,
            arguments=tool_call.arguments if tool_call is not None else None,
            command=self._tool_call_string_arg(tool_call, "command"),
            cwd=self._tool_call_string_arg(tool_call, "workdir", "cwd"),
        )
        if projected_content is None:
            self.config.metadata["tool_projection_noops"] = (
                self.config.metadata.get("tool_projection_noops", 0) + 1
            )
            self._write_turn_call_log(
                "tool_projection_noop",
                tool_use_id=result.tool_use_id,
                name=result.tool_name,
                original_chars=len(result.content),
            )
            return result

        stored = self._store_tool_result_snapshot(
            result.content,
            tool_use_id=result.tool_use_id,
            tool_name=result.tool_name,
        )
        stored_handle = stored.handle if stored is not None else None
        if stored is not None:
            projected_content = (
                "[tool_result_projection]\n"
                f"tool_result_handle: {stored.handle}\n"
                f"sha256: {stored.sha256}\n"
                f"original_chars: {stored.chars}\n"
                f"{projected_content}"
            )

        tokens_before = get_approx_tokens(result.content)
        tokens_after = get_approx_tokens(projected_content)
        self.config.metadata["tool_projection_applied"] = True
        self.config.metadata["tool_projection_calls"] = (
            self.config.metadata.get("tool_projection_calls", 0) + 1
        )
        self.config.metadata["tool_projection_tokens_before"] = (
            self.config.metadata.get("tool_projection_tokens_before", 0) + tokens_before
        )
        self.config.metadata["tool_projection_tokens_after"] = (
            self.config.metadata.get("tool_projection_tokens_after", 0) + tokens_after
        )
        self.config.metadata["tool_projection_tokens_saved"] = self.config.metadata.get(
            "tool_projection_tokens_saved", 0
        ) + max(0, tokens_before - tokens_after)

        self._write_turn_call_log(
            "tool_projection_applied",
            tool_use_id=result.tool_use_id,
            name=result.tool_name,
            tool_result_handle=stored_handle,
            original_chars=len(result.content),
            projected_chars=len(projected_content),
        )
        return ToolResult(
            tool_use_id=result.tool_use_id,
            tool_name=result.tool_name,
            content=projected_content,
            is_error=result.is_error,
            artifacts=list(result.artifacts),
            execution_status=result.execution_status,
            terminates_turn=result.terminates_turn,
        )

    async def _canonicalize_tool_result(
        self,
        result: ToolResult,
        *,
        tool_call: ToolCall | None = None,
    ) -> ToolResult:
        return await self._project_tool_result_for_llm(result, tool_call=tool_call)

    async def _project_tool_result_for_delivery(
        self,
        result: ToolResult,
        *,
        tool_call: ToolCall | None = None,
    ) -> ToolResult:
        if _pending_approval_payload(result.content) is not None:
            return result
        return await self._project_tool_result_for_llm(
            result,
            tool_call=tool_call,
        )

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
        async for event in self._turn_generator(message, extra_messages, semantic_message):
            yield event

    async def _turn_generator(
        self,
        message: str,
        extra_messages: list[Message] | None = None,
        semantic_message: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Async generator that drives the state machine."""
        self._current_turn_message = message

        # ------ IDLE → THINKING ------
        yield self._transition(AgentState.THINKING)

        # Use the system prompt from config (wired by gateway via identity.prompt)
        if self._context is None:
            self._context = ContextAssembly(
                system_prompt=self.config.system_prompt or "",
                workspace_dir=self.config.workspace_dir,
            )

        thinking_prompt = semantic_message if semantic_message is not None else message
        thinking_enabled, thinking_budget = self.config.resolve_thinking(prompt=thinking_prompt)

        # Prepare the loaded transcript ONCE per turn. Structured history is
        # replayed as persisted (append-only); only model-level incompatibilities
        # (replayed reasoning a model cannot take, stale images) and a single
        # pairing check apply. Nothing below rewrites a message once a provider
        # request has carried it.
        loaded_history = list(self._history)
        self._write_context_stage("session:loaded", loaded_history)
        sanitized_history, sanitize_result = sanitize_session_messages(loaded_history)
        if self._drop_replayed_reasoning(thinking_enabled):
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
        turn_messages: list[Message] = list(history)
        self._turn_base_len = len(turn_messages)
        self._turn_reset = False
        self._turn_compaction_id = None
        self._turn_record = None
        self._turn_compaction_declined_tokens = None
        # Index of this turn's first input message: compaction keeps it and
        # everything after it (the current turn) and summarizes the rest.
        turn_input_index = len(turn_messages)
        input_messages: list[Message] = list(extra_messages or [])
        # Only append text message if non-empty (multimodal may use extra_messages instead)
        if message:
            input_messages.append(Message(role="user", content=message))
        runtime_context = self._runtime_context_block()
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
        context_blocks.append(runtime_context)
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
        turn_messages.extend(input_messages)
        # Messages from here on are new this turn; they are sealed (shrunk,
        # checked) right before the first provider request that carries them.
        sealed_len = self._turn_base_len
        self._write_context_stage("prompt:before", turn_messages)
        self._write_context_stage(
            "prompt:images",
            turn_messages,
            image_blocks=self._count_image_blocks(turn_messages),
        )
        runtime_context_hash = hashlib.sha256(runtime_context.encode("utf-8")).hexdigest()[:16]

        chat_cfg = ChatConfig(
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            system=self._context.system_prompt,
            thinking=thinking_enabled,
            thinking_budget_tokens=thinking_budget,
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
        _thinking_fallback_done = False

        _log = structlog.get_logger("agentos.engine.agent")
        iterations = 0
        overflow_retries = 0
        # Keep lifetime usage separate from the live context-window gauge.
        # Compaction shrinks what the model sees next; it must not erase the
        # turn's already-spent provider tokens from the final DoneEvent.
        total_input_tokens = 0
        total_output_tokens = 0
        total_reasoning_tokens = 0
        total_cached_tokens = 0
        total_cache_write_tokens = 0
        total_billed_cost = 0.0
        usage_turn_baseline = (
            self._usage_tracker.session_checkpoint(self._session_key)
            if self._usage_tracker and self._session_key
            else None
        )
        turn_llm_calls = 0
        turn_tool_errors = 0
        last_actual_model = ""
        terminal_error: ErrorEvent | None = None
        final_text_parts: list[str] = []
        # Normalized stop reason (and refusal details) of the last provider
        # call that completed, surfaced on the turn's DoneEvent for the
        # decision log.
        turn_stop_reason: str | None = None
        turn_stop_details: dict[str, Any] | None = None
        final_reasoning_parts: list[str] = []
        artifact_delivery_final_response_pending = False
        artifact_delivery_degraded_final_response = False
        artifact_delivery_final_response_artifacts: list[dict[str, Any]] = []
        max_iterations_finalization_attempted = False
        max_iterations_finalization_pending = False
        max_iterations_finalization_message: Message | None = None
        progress_watchdog = ProgressWatchdog(observe_only=True)
        verification_ledger = VerificationLedger()
        _fallback = FallbackPolicy(
            max_retries=self.config.max_provider_retries,
            base_backoff_ms=self.config.retry_base_backoff_ms,
            max_backoff_ms=self.config.retry_max_backoff_ms,
        )
        _retry_after_cap_s = float(self.config.rate_limit_retry_after_cap_seconds)
        # The selector-fallback wrapper decides whether a hinted 429 fails over
        # straight away; it has to use the same cap this loop waits under.
        _adopt_retry_after_cap = getattr(self.provider, "set_retry_after_cap_seconds", None)
        if callable(_adopt_retry_after_cap):
            try:
                _adopt_retry_after_cap(_retry_after_cap_s)
            except Exception:  # noqa: BLE001 - optional provider hook
                logger.debug("provider.retry_after_cap_hook_failed", exc_info=True)

        # Timeout budgets: optional total turn budget, idle LLM stream budget,
        # and per-tool execution budget.
        _loop = asyncio.get_running_loop()
        _total_deadline = _loop.time() + self.config.timeout if self.config.timeout > 0 else None
        tools_supported = True
        if self.config.model_capabilities is not None:
            tools_supported = bool(getattr(self.config.model_capabilities, "supports_tools", True))
        provider_tool_definitions = self.tool_definitions or None
        if not tools_supported:
            provider_tool_definitions = None

        def _positive_float(value: Any) -> float | None:
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                return None
            return parsed if parsed > 0 else None

        def _turn_budget_error() -> ErrorEvent | None:
            max_llm_calls = self._positive_int(getattr(self.config, "max_turn_llm_calls", 0))
            if max_llm_calls is not None and turn_llm_calls > max_llm_calls:
                return ErrorEvent(
                    message=(
                        f"Turn stopped after {turn_llm_calls} LLM calls "
                        f"(max_turn_llm_calls={max_llm_calls})."
                    ),
                    code="turn_llm_call_budget_exceeded",
                )
            max_input = self._positive_int(getattr(self.config, "max_turn_input_tokens", 0))
            if max_input is not None and total_input_tokens > max_input:
                return ErrorEvent(
                    message=(
                        f"Turn stopped after {total_input_tokens} input tokens "
                        f"(max_turn_input_tokens={max_input})."
                    ),
                    code="turn_input_token_budget_exceeded",
                )
            max_output = self._positive_int(getattr(self.config, "max_turn_output_tokens", 0))
            if max_output is not None and total_output_tokens > max_output:
                return ErrorEvent(
                    message=(
                        f"Turn stopped after {total_output_tokens} output tokens "
                        f"(max_turn_output_tokens={max_output})."
                    ),
                    code="turn_output_token_budget_exceeded",
                )
            max_cost = _positive_float(getattr(self.config, "max_turn_billed_cost_usd", 0.0))
            if max_cost is not None and total_billed_cost > max_cost:
                return ErrorEvent(
                    message=(
                        f"Turn stopped after ${total_billed_cost:.6f} billed cost "
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
            if max_tool_errors is not None and turn_tool_errors >= max_tool_errors:
                return ErrorEvent(
                    message=(
                        f"Turn stopped after {turn_tool_errors} tool errors "
                        f"(max_turn_tool_errors={max_tool_errors})."
                    ),
                    code="turn_tool_error_budget_exceeded",
                )
            return None

        def _turn_llm_call_budget_error(next_call_number: int) -> ErrorEvent | None:
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
            *,
            reason: str,
            code: str,
        ) -> WarningEvent:
            nonlocal artifact_delivery_degraded_final_response
            nonlocal artifact_delivery_final_response_pending
            if not "".join(final_text_parts).strip():
                final_text_parts.append(
                    self._artifact_delivery_final_response_text(
                        artifact_delivery_final_response_artifacts
                    )
                )
            artifact_delivery_degraded_final_response = True
            artifact_delivery_final_response_pending = False
            self._write_turn_call_log(
                "artifact_final_response_degraded",
                reason=reason,
                code=code,
                artifact_count=len(artifact_delivery_final_response_artifacts),
            )
            return WarningEvent(
                code="artifact_delivery_final_response_degraded",
                message=(
                    "Artifact delivery completed, but the model could not generate "
                    "the final explanatory response. Returning a deterministic "
                    "completion message instead."
                ),
            )

        def _finish_artifact_delivery_without_provider() -> None:
            final_response_text = self._artifact_delivery_final_response_text(
                artifact_delivery_final_response_artifacts
            )
            current_text = "".join(final_text_parts)
            if final_response_text not in current_text:
                prefix = "\n\n" if current_text.strip() else ""
                final_text_parts.append(prefix + final_response_text)
            self._write_turn_call_log(
                "artifact_final_response_synthesized",
                reason="publish_artifact_completed",
                artifact_count=len(artifact_delivery_final_response_artifacts),
            )

        try:
            while True:
                if self.config.max_iterations > 0 and iterations >= self.config.max_iterations:
                    max_iterations_source = str(
                        self.config.metadata.get("agent_max_iterations_source", "agent_config")
                    )
                    if max_iterations_source == "session config":
                        max_iterations_guidance = (
                            "Set session agent_max_iterations=0 for unlimited tasks."
                        )
                    elif max_iterations_source == "gateway config":
                        max_iterations_guidance = (
                            "Set gateway agent_max_iterations=0 for unlimited tasks."
                        )
                    elif max_iterations_source.startswith("env "):
                        max_iterations_guidance = (
                            "Set AGENTOS_AGENT_MAX_ITERATIONS=0 for unlimited tasks."
                        )
                    elif max_iterations_source == "explicit argument":
                        max_iterations_guidance = (
                            "Pass --max-iterations 0 or max_iterations=0 for unlimited tasks."
                        )
                    else:
                        max_iterations_guidance = (
                            "Set AgentConfig.max_iterations=0 for unlimited tasks."
                        )
                    if not max_iterations_finalization_attempted:
                        max_iterations_finalization_attempted = True
                        max_iterations_finalization_pending = True
                        max_iterations_finalization_message = Message(
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
                            iteration=iterations,
                            max_iterations=self.config.max_iterations,
                            max_iterations_source=max_iterations_source,
                        )
                    else:
                        self._write_turn_call_log(
                            "turn_policy_decision",
                            action="partial",
                            reason="max_iterations",
                            code="max_iterations",
                            iteration=iterations,
                            max_iterations=self.config.max_iterations,
                            max_iterations_source=max_iterations_source,
                        )
                        terminal_error = ErrorEvent(
                            message=(
                                f"Reached max_iterations={self.config.max_iterations} "
                                f"from {max_iterations_source} after a finalization attempt. "
                                f"{max_iterations_guidance}"
                            ),
                            code="max_iterations",
                        )
                        yield terminal_error
                        break

                # Check total turn deadline (if configured)
                if _total_deadline is not None and _loop.time() > _total_deadline:
                    raise TimeoutError(f"Agent total timeout after {self.config.timeout}s")

                iterations += 1

                # ------ THINKING → STREAMING ------
                yield self._transition(AgentState.STREAMING)

                # Collect this LLM response
                assistant_text_parts: list[str] = []
                tool_calls: list[ToolCall] = []
                pending_tools: dict[str, _StreamAccumulator] = {}
                tool_argument_heartbeat_chars: dict[str, int] = {}
                iter_input_tokens = 0
                iter_output_tokens = 0
                iter_reasoning_tokens = 0
                iter_reasoning_content: str | None = None
                iter_thinking_signature: str | None = None
                provider_error: ProviderErrorEvent | None = None

                _retry_attempt = 0
                _call_attempt = 0
                _retry_policy = _ProviderRetryPolicy.from_provider_budget(
                    _fallback.max_retries,
                    length_capped_continuations=self.config.length_capped_continuations,
                )
                _attempt_retries_used = _retry_policy.used_attempts()
                _invalid_response_fallback_done = False
                _retry_after_fallback_done = False
                _pause_turn_resumes = 0
                # The attempt whose thinking the consumer is seeing. A retry of
                # the same step re-streams reasoning the UI already shows, so
                # later attempts' thinking deltas are not re-emitted (they still
                # reach the transcript through the done event). Continuations,
                # which keep the previous output, reset it.
                _thinking_shown_attempt: int | None = None
                while _retry_attempt <= _fallback.max_retries:
                    provider_error = None
                    assistant_text_parts = []
                    tool_calls = []
                    pending_tools = {}
                    tool_argument_heartbeat_chars = {}
                    iter_input_tokens = 0
                    iter_output_tokens = 0
                    iter_reasoning_tokens = 0
                    iter_reasoning_content = None
                    iter_thinking_signature = None
                    _got_error = False
                    provider_done_for_log: ProviderDoneEvent | None = None
                    provider_error_for_log: ProviderErrorEvent | None = None
                    call_id = f"{iterations}.{_call_attempt}"
                    call_started_at = time.monotonic()
                    provider_tools_for_call = (
                        None
                        if (
                            artifact_delivery_final_response_pending
                            or max_iterations_finalization_pending
                        )
                        else provider_tool_definitions
                    )
                    tools_supported_for_call = (
                        tools_supported
                        and not artifact_delivery_final_response_pending
                        and not max_iterations_finalization_pending
                    )
                    ignored_post_delivery_tool_use = False
                    # Seal what was appended since the last request (shrink
                    # oversized tool results, strip copied projections, check
                    # pairing) -- once; the request is then the transcript as is.
                    sealed_len = self._seal_transcript(turn_messages, sealed_len)
                    request_messages = self._provider_request_messages(
                        turn_messages,
                        finalization_message=(
                            max_iterations_finalization_message
                            if max_iterations_finalization_pending
                            else None
                        ),
                    )
                    self._write_context_stage(
                        "stream:context",
                        request_messages,
                        call_id=call_id,
                        iteration=iterations,
                        attempt=_call_attempt,
                    )

                    terminal_error = _turn_llm_call_budget_error(turn_llm_calls + 1)
                    if terminal_error is not None:
                        self._write_turn_call_log(
                            "turn_policy_decision",
                            action=(
                                "artifact_degraded_finish"
                                if artifact_delivery_final_response_pending
                                else "stop"
                            ),
                            reason=terminal_error.message,
                            code=terminal_error.code,
                            sent_llm_calls=turn_llm_calls,
                            attempted_llm_call=turn_llm_calls + 1,
                            iteration=iterations,
                            attempt=_call_attempt,
                        )
                        if artifact_delivery_final_response_pending:
                            yield _finish_artifact_delivery_degraded(
                                reason=terminal_error.message,
                                code=terminal_error.code,
                            )
                            terminal_error = None
                        else:
                            yield self._transition(AgentState.ERROR)
                            yield terminal_error
                        break

                    call_chat_cfg = chat_cfg

                    # Check overflow against the live provider request before it
                    # is sent, not after it streamed: once text or tool_use
                    # events are out, re-calling the provider replays them to
                    # the stream consumer (#3644).
                    estimated_context_tokens = self._estimate_live_request_tokens(
                        request_messages,
                        tools=provider_tools_for_call,
                        config=call_chat_cfg,
                    )
                    self._last_request_overhead_tokens = max(
                        0, estimated_context_tokens - self._message_tokens(request_messages)
                    )
                    overflow_outcome = await self._preflight_context_overflow(
                        turn_messages,
                        estimated_context_tokens,
                        turn_input_index=turn_input_index,
                    )
                    if overflow_outcome is None:
                        if overflow_retries >= self.config.max_overflow_retries:
                            yield self._transition(AgentState.ERROR)
                            terminal_error = self._context_overflow_error()
                            yield terminal_error
                            break
                        # The estimate is a heuristic: send the request as is.
                        # A real provider overflow takes the CONTEXT_OVERFLOW
                        # branch below, and the next call checks again.
                        overflow_retries += 1
                        _log.warning(
                            "compaction.preflight_failed",
                            attempt=overflow_retries,
                            max=self.config.max_overflow_retries,
                            estimated_tokens=estimated_context_tokens,
                        )
                    elif overflow_outcome.compacted:
                        # Lifetime counters keep feeding DoneEvent usage/cost
                        # accounting for this turn.
                        turn_messages = overflow_outcome.messages
                        sealed_len = len(turn_messages)
                        turn_input_index = overflow_outcome.turn_input_index
                        # The stream consumer persists the compaction while
                        # handling this event; the system prompt is unchanged.
                        yield CompactionEvent(
                            compaction_id=overflow_outcome.compaction_id,
                            summary=overflow_outcome.summary,
                            kept_entries=overflow_outcome.kept_entries,
                            kept_count=len(overflow_outcome.messages),
                            removed_count=overflow_outcome.removed_count,
                        )
                        overflow_retries = 0  # reset on success
                        request_messages = self._provider_request_messages(
                            turn_messages,
                            finalization_message=(
                                max_iterations_finalization_message
                                if max_iterations_finalization_pending
                                else None
                            ),
                        )
                        # Calibrate against the request actually sent.
                        estimated_context_tokens = self._estimate_live_request_tokens(
                            request_messages,
                            tools=provider_tools_for_call,
                            config=call_chat_cfg,
                        )
                        self._write_context_stage(
                            "stream:context:compacted",
                            request_messages,
                            call_id=call_id,
                            iteration=iterations,
                            attempt=_call_attempt,
                        )

                    self._write_turn_call_log(
                        "llm_request",
                        call_id=call_id,
                        iteration=iterations,
                        attempt=_call_attempt,
                        messages=request_messages,
                        tools=provider_tools_for_call,
                        config=call_chat_cfg,
                    )
                    turn_llm_calls += 1
                    cache_prompt_snapshot = None
                    if self._session_key:
                        cache_prompt_snapshot = record_prompt_state(
                            messages=request_messages,
                            tools=provider_tools_for_call,
                            config=call_chat_cfg,
                            model=self.config.model_id or "",
                        )

                    _got_done_event = False
                    attempt_user_visible_emitted = False
                    try:
                        raw_stream = self.provider.chat(
                            request_messages,
                            tools=provider_tools_for_call,
                            config=call_chat_cfg,
                        )
                        async for raw_ev in self.turn_timing.provider_stream(
                            self._stream_provider_events_with_deadline(
                                raw_stream,
                                loop=_loop,
                                total_deadline=_total_deadline,
                            )
                        ):
                            if isinstance(raw_ev, ProviderTextDelta):
                                assistant_text_parts.append(raw_ev.text)
                                if raw_ev.text:
                                    attempt_user_visible_emitted = True
                                yield TextDeltaEvent(text=raw_ev.text)

                            elif isinstance(raw_ev, ProviderThinkingDelta):
                                # Reasoning is display-only: never mixed into the
                                # reply text, never counted as user-visible output.
                                if raw_ev.text and _thinking_shown_attempt in (
                                    None,
                                    _call_attempt,
                                ):
                                    _thinking_shown_attempt = _call_attempt
                                    yield ThinkingEvent(text=raw_ev.text)

                            elif isinstance(raw_ev, ProviderToolUseStart):
                                if not tools_supported_for_call:
                                    if (
                                        artifact_delivery_final_response_pending
                                        or max_iterations_finalization_pending
                                    ):
                                        ignored_post_delivery_tool_use = True
                                    continue
                                pending_tools[raw_ev.tool_use_id] = _StreamAccumulator(
                                    tool_use_id=raw_ev.tool_use_id,
                                    tool_name=raw_ev.tool_name,
                                    synthetic_from_text=raw_ev.synthetic_from_text,
                                )
                                tool_argument_heartbeat_chars[raw_ev.tool_use_id] = 0
                                attempt_user_visible_emitted = True
                                yield ToolUseStartEvent(
                                    tool_use_id=raw_ev.tool_use_id,
                                    tool_name=raw_ev.tool_name,
                                    synthetic_from_text=raw_ev.synthetic_from_text,
                                )

                            elif raw_ev.kind == "tool_use_delta":
                                if not tools_supported_for_call:
                                    continue
                                acc = pending_tools.get(raw_ev.tool_use_id)  # type: ignore[union-attr]
                                if acc:
                                    json_fragment = raw_ev.json_fragment  # type: ignore[union-attr]
                                    acc.json_buf.append(json_fragment)
                                    acc.json_chars += len(json_fragment)
                                    last_heartbeat_chars = tool_argument_heartbeat_chars.get(
                                        raw_ev.tool_use_id, 0
                                    )
                                    if (
                                        acc.json_chars - last_heartbeat_chars
                                        >= _TOOL_ARGUMENT_HEARTBEAT_CHARS
                                    ):
                                        tool_argument_heartbeat_chars[raw_ev.tool_use_id] = (
                                            acc.json_chars
                                        )
                                        yield RunHeartbeatEvent(
                                            phase="llm_tool_arguments",
                                            elapsed_ms=int(
                                                (time.monotonic() - call_started_at) * 1000
                                            ),
                                            idle_ms=0,
                                            message=(f"Receiving {acc.tool_name} arguments"),
                                        )

                            elif isinstance(raw_ev, ToolUseEndEvent):
                                if not tools_supported_for_call:
                                    if (
                                        artifact_delivery_final_response_pending
                                        or max_iterations_finalization_pending
                                    ):
                                        ignored_post_delivery_tool_use = True
                                    continue
                                acc = pending_tools.pop(raw_ev.tool_use_id, None)
                                tool_argument_heartbeat_chars.pop(raw_ev.tool_use_id, None)
                                if acc and acc.json_buf:
                                    arguments = acc.finish()
                                else:
                                    arguments = raw_ev.arguments
                                synthetic_from_text = (
                                    acc.synthetic_from_text
                                    if acc is not None
                                    else raw_ev.synthetic_from_text
                                )
                                tool_calls.append(
                                    ToolCall(
                                        tool_use_id=raw_ev.tool_use_id,
                                        tool_name=raw_ev.tool_name,
                                        arguments=arguments,
                                        synthetic_from_text=synthetic_from_text,
                                        thought_signature=raw_ev.thought_signature,
                                    )
                                )

                            elif isinstance(raw_ev, ProviderDoneEvent):
                                provider_done_for_log = raw_ev
                                _got_done_event = True
                                # Make the estimator's drift visible: the
                                # compaction trigger runs on this estimate,
                                # scaled by the calibration it feeds.
                                calibration_factor = self._observe_estimate(
                                    estimated_context_tokens,
                                    raw_ev.input_tokens,
                                    usage_estimated=raw_ev.usage_estimated,
                                    request_messages=request_messages,
                                )
                                logger.debug(
                                    "agent.context_estimate_vs_provider",
                                    session_key=self._session_key,
                                    call_id=call_id,
                                    estimated_tokens=estimated_context_tokens,
                                    provider_input_tokens=raw_ev.input_tokens,
                                    provider_cached_tokens=raw_ev.cached_tokens,
                                    provider_cache_write_tokens=raw_ev.cache_write_tokens,
                                    calibration_factor=calibration_factor,
                                )
                                iter_input_tokens = raw_ev.input_tokens
                                iter_output_tokens = raw_ev.output_tokens
                                iter_reasoning_tokens = raw_ev.reasoning_tokens
                                iter_reasoning_content = raw_ev.reasoning_content
                                iter_thinking_signature = raw_ev.thinking_signature
                                total_billed_cost += raw_ev.billed_cost
                                total_input_tokens += raw_ev.input_tokens
                                total_output_tokens += raw_ev.output_tokens
                                total_reasoning_tokens += raw_ev.reasoning_tokens
                                total_cached_tokens += raw_ev.cached_tokens
                                total_cache_write_tokens += raw_ev.cache_write_tokens
                                if raw_ev.model:
                                    last_actual_model = raw_ev.model
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
                                        provider_id=(
                                            metadata.provider_kind or metadata.provider_name
                                        ),
                                    )

                            elif isinstance(raw_ev, ProviderErrorEvent):
                                provider_error_for_log = raw_ev
                                # One-shot thinking/reasoning fallback
                                _err_lower = raw_ev.message.lower()
                                if (
                                    thinking_enabled
                                    and not _thinking_fallback_done
                                    and ("thinking" in _err_lower or "reasoning" in _err_lower)
                                ):
                                    _thinking_fallback_done = True
                                    thinking_enabled = False
                                    thinking_budget = 0
                                    chat_cfg = _chat_config_with_thinking_disabled(chat_cfg)
                                    _got_error = True
                                    break  # break stream, retry

                                provider_error = raw_ev
                                _got_error = True
                                break  # break stream loop

                            elif isinstance(raw_ev, ProviderHeartbeatEvent):
                                yield RunHeartbeatEvent(
                                    phase=raw_ev.phase,
                                    message=raw_ev.message,
                                )
                    except _IterationStreamTimeoutError:
                        if artifact_delivery_final_response_pending:
                            yield _finish_artifact_delivery_degraded(
                                reason=(
                                    f"Iteration {iterations} exceeded "
                                    f"iteration_timeout ({self.config.iteration_timeout}s) "
                                    "during final artifact response generation"
                                ),
                                code="iteration_timeout",
                            )
                            break
                        yield self._transition(AgentState.ERROR)
                        terminal_error = ErrorEvent(
                            message=(
                                f"Iteration {iterations} exceeded iteration_timeout"
                                f" ({self.config.iteration_timeout}s) during LLM streaming"
                            ),
                            code="iteration_timeout",
                        )
                        yield terminal_error
                        break

                    call_duration_ms = int((time.monotonic() - call_started_at) * 1000)
                    response_payload = {
                        "call_id": call_id,
                        "iteration": iterations,
                        "attempt": _call_attempt,
                        "duration_ms": call_duration_ms,
                        "text": "".join(assistant_text_parts),
                        "tool_calls": [
                            {
                                "tool_use_id": tc.tool_use_id,
                                "name": tc.tool_name,
                                "arguments": tc.arguments,
                            }
                            for tc in tool_calls
                        ],
                        "got_done_event": _got_done_event,
                    }
                    if provider_done_for_log is not None:
                        response_payload["usage"] = {
                            "stop_reason": provider_done_for_log.stop_reason,
                            "raw_stop_reason": provider_done_for_log.raw_stop_reason,
                            "stop_details": provider_done_for_log.stop_details,
                            "input_tokens": provider_done_for_log.input_tokens,
                            "output_tokens": provider_done_for_log.output_tokens,
                            "reasoning_tokens": provider_done_for_log.reasoning_tokens,
                            "cached_tokens": provider_done_for_log.cached_tokens,
                            "cache_write_tokens": provider_done_for_log.cache_write_tokens,
                            "billed_cost": provider_done_for_log.billed_cost,
                            "cost_source": getattr(provider_done_for_log, "cost_source", "none"),
                            "model": provider_done_for_log.model,
                        }
                    if provider_error_for_log is not None:
                        response_payload["error"] = {
                            "message": provider_error_for_log.message,
                            "code": provider_error_for_log.code,
                        }
                        self._write_turn_call_log("llm_error", **response_payload)
                    else:
                        self._write_turn_call_log("llm_response", **response_payload)

                    # -- after async for (retry loop level) --
                    terminal_error = _turn_budget_error()
                    if terminal_error is not None:
                        if artifact_delivery_final_response_pending:
                            yield _finish_artifact_delivery_degraded(
                                reason=terminal_error.message,
                                code=terminal_error.code,
                            )
                            terminal_error = None
                        else:
                            yield self._transition(AgentState.ERROR)
                            yield terminal_error
                        break
                    response_text = "".join(assistant_text_parts)
                    if ignored_post_delivery_tool_use and not response_text.strip():
                        if artifact_delivery_final_response_pending:
                            response_text = self._artifact_delivery_final_response_text(
                                artifact_delivery_final_response_artifacts
                            )
                        elif max_iterations_finalization_pending:
                            response_text = (
                                "I reached the configured iteration limit after completing "
                                "the available tool step. Here is the best partial result so far."
                            )
                        if response_text:
                            assistant_text_parts.append(response_text)
                            attempt_user_visible_emitted = True
                            yield TextDeltaEvent(text=response_text)
                    last_request_msg = request_messages[-1] if request_messages else None
                    post_tool_turn = _message_has_tool_result(last_request_msg)
                    stop_reason = (
                        getattr(provider_done_for_log, "stop_reason", None)
                        if provider_done_for_log is not None
                        else None
                    )
                    attempt_classification = _classify_provider_attempt(
                        text=response_text,
                        tool_calls=tool_calls,
                        pending_tools=pending_tools,
                        got_done_event=_got_done_event,
                        stop_reason=stop_reason,
                        reasoning_content=iter_reasoning_content,
                        reasoning_tokens=iter_reasoning_tokens,
                        user_visible_emitted=attempt_user_visible_emitted,
                    )
                    if (
                        not _got_error
                        and attempt_classification.kind == _ProviderAttemptKind.PAUSE_TURN
                    ):
                        if _pause_turn_resumes < _MAX_PAUSE_TURN_RESUMES:
                            # Keep what the paused response produced, then ask
                            # the provider to carry on. The assistant text goes
                            # into history and into the final reply, exactly as
                            # for a length-capped continuation. A follow-up user
                            # nudge (rather than a trailing assistant message) is
                            # used because current Claude models reject an
                            # assistant-last request as an unsupported prefill.
                            _pause_turn_resumes += 1
                            visible_text = _append_length_capped_continuation(
                                turn_messages,
                                response_text=response_text,
                                tool_calls=tool_calls,
                                prompt=_PROVIDER_PAUSE_TURN_RESUME_PROMPT,
                            )
                            if visible_text:
                                final_text_parts.append(visible_text)
                            logger.info(
                                "provider.pause_turn_resume",
                                session_key=self._session_key,
                                model=last_actual_model or self.config.model_id or "",
                                provider=type(self.provider).__name__,
                                iteration=iterations,
                                call_attempt=_call_attempt,
                                resume=_pause_turn_resumes,
                                budget=_MAX_PAUSE_TURN_RESUMES,
                                visible_chars=len(visible_text),
                            )
                            self._write_turn_call_log(
                                "turn_policy_decision",
                                action="resume_paused_turn",
                                reason="pause_turn",
                                code="pause_turn",
                                iteration=iterations,
                                attempt=_call_attempt,
                                resume=_pause_turn_resumes,
                                budget=_MAX_PAUSE_TURN_RESUMES,
                            )
                            _thinking_shown_attempt = None
                            _call_attempt += 1
                            continue
                        # Resume budget spent: the paused response is the end of
                        # the turn. Re-classify it as a normal end_turn so the
                        # usual empty/visible checks apply.
                        logger.warning(
                            "provider.pause_turn_resume_exhausted",
                            session_key=self._session_key,
                            iteration=iterations,
                            call_attempt=_call_attempt,
                            budget=_MAX_PAUSE_TURN_RESUMES,
                        )
                        self._write_turn_call_log(
                            "turn_policy_decision",
                            action="end_paused_turn",
                            reason="pause_turn_resume_exhausted",
                            code="pause_turn",
                            iteration=iterations,
                            attempt=_call_attempt,
                            budget=_MAX_PAUSE_TURN_RESUMES,
                        )
                        if provider_done_for_log is not None:
                            provider_done_for_log.stop_reason = StopReason.END_TURN
                        stop_reason = StopReason.END_TURN
                        attempt_classification = _classify_provider_attempt(
                            text=response_text,
                            tool_calls=tool_calls,
                            pending_tools=pending_tools,
                            got_done_event=_got_done_event,
                            stop_reason=stop_reason,
                            reasoning_content=iter_reasoning_content,
                            reasoning_tokens=iter_reasoning_tokens,
                            user_visible_emitted=attempt_user_visible_emitted,
                        )
                    if (
                        not _got_error
                        and attempt_classification.kind == _ProviderAttemptKind.REFUSAL
                    ):
                        # A safety decline is final: no retry, no failover to
                        # another provider (that would be shopping for a model
                        # that says yes). Keep whatever text the model wrote
                        # before it declined; tool calls from a declined
                        # response are never executed.
                        refusal_details = (
                            provider_done_for_log.stop_details
                            if provider_done_for_log is not None
                            else None
                        )
                        refusal_category = _refusal_category(refusal_details)
                        dropped_tool_calls = len(tool_calls)
                        tool_calls = []
                        if not response_text.strip():
                            response_text = _PROVIDER_REFUSAL_REPLY
                            assistant_text_parts.append(response_text)
                            attempt_user_visible_emitted = True
                            yield TextDeltaEvent(text=response_text)
                        logger.warning(
                            "provider.refusal",
                            session_key=self._session_key,
                            model=last_actual_model or self.config.model_id or "",
                            provider=type(self.provider).__name__,
                            iteration=iterations,
                            call_attempt=_call_attempt,
                            category=refusal_category,
                            raw_stop_reason=(
                                provider_done_for_log.raw_stop_reason
                                if provider_done_for_log is not None
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
                            iteration=iterations,
                            attempt=_call_attempt,
                            dropped_tool_calls=dropped_tool_calls,
                        )
                        yield WarningEvent(
                            code="provider_refusal",
                            message=_refusal_warning_message(refusal_details),
                        )
                    if not _got_error and attempt_classification.kind not in {
                        _ProviderAttemptKind.OK,
                        _ProviderAttemptKind.REFUSAL,
                    }:
                        logger.warning(
                            "provider.invalid_response",
                            session_key=self._session_key,
                            model=last_actual_model or self.config.model_id or "",
                            provider=type(self.provider).__name__,
                            classification=attempt_classification.kind.value,
                            iteration=iterations,
                            call_attempt=_call_attempt,
                            provider_retry_attempt=_retry_attempt,
                            post_tool_turn=post_tool_turn,
                            got_done_event=_got_done_event,
                            stop_reason=stop_reason,
                            iter_input_tokens=iter_input_tokens,
                            iter_output_tokens=iter_output_tokens,
                            iter_reasoning_tokens=iter_reasoning_tokens,
                            reasoning_chars=len(iter_reasoning_content or ""),
                        )

                        large_context_invalid = _is_large_context_invalid_response(
                            attempt_classification.kind,
                            input_tokens=iter_input_tokens,
                        )
                        if large_context_invalid:
                            if (
                                not _invalid_response_fallback_done
                                and self._switch_to_invalid_response_fallback(
                                    attempt_classification.kind.value
                                )
                            ):
                                _invalid_response_fallback_done = True
                                yield WarningEvent(
                                    code="provider_large_context_fallback",
                                    message=(
                                        "The provider returned no visible response for a "
                                        "large input; trying a fallback provider once."
                                    ),
                                )
                                _call_attempt += 1
                                continue

                            yield self._transition(AgentState.ERROR)
                            terminal_error = ErrorEvent(
                                message=(
                                    "Provider returned no visible response for a large input. "
                                    "Send the material as an attachment, summarize or shorten "
                                    "the prompt, or use a stronger model."
                                ),
                                code="empty_response",
                            )
                            yield terminal_error
                            break

                        if (
                            attempt_classification.kind == _ProviderAttemptKind.REASONING_ONLY
                            and thinking_enabled
                            and _retry_policy.can_retry_attempt(
                                _ProviderAttemptKind.REASONING_ONLY,
                                _attempt_retries_used,
                            )
                        ):
                            _attempt_retries_used[_ProviderAttemptKind.REASONING_ONLY] += 1
                            _thinking_fallback_done = True
                            thinking_enabled = False
                            thinking_budget = 0
                            chat_cfg = _chat_config_with_thinking_disabled(chat_cfg)
                            yield WarningEvent(
                                code="provider_reasoning_only_retry",
                                message=(
                                    "The provider returned reasoning without visible content; "
                                    "retrying once with thinking disabled."
                                ),
                            )
                            _call_attempt += 1
                            continue

                        if (
                            attempt_classification.kind == _ProviderAttemptKind.MALFORMED_EMPTY
                            and _retry_policy.can_retry_attempt(
                                _ProviderAttemptKind.MALFORMED_EMPTY,
                                _attempt_retries_used,
                            )
                        ):
                            _attempt_retries_used[_ProviderAttemptKind.MALFORMED_EMPTY] += 1
                            delay = backoff_sleep(
                                0,
                                _fallback.base_backoff_ms,
                                _fallback.max_backoff_ms,
                                _fake=True,
                            )
                            yield WarningEvent(
                                code="provider_empty_retry",
                                message="The provider returned an empty response; retrying once.",
                            )
                            await asyncio.sleep(delay)
                            _call_attempt += 1
                            continue

                        if (
                            attempt_classification.kind == _ProviderAttemptKind.STREAM_INCOMPLETE
                            and not attempt_classification.user_visible_emitted
                            and _retry_policy.can_retry_attempt(
                                _ProviderAttemptKind.STREAM_INCOMPLETE,
                                _attempt_retries_used,
                            )
                        ):
                            _attempt_retries_used[_ProviderAttemptKind.STREAM_INCOMPLETE] += 1
                            delay = backoff_sleep(
                                0,
                                _fallback.base_backoff_ms,
                                _fallback.max_backoff_ms,
                                _fake=True,
                            )
                            yield WarningEvent(
                                code="provider_empty_retry",
                                message=(
                                    "The provider stream ended before completion; retrying once."
                                ),
                            )
                            await asyncio.sleep(delay)
                            _call_attempt += 1
                            continue

                        if (
                            attempt_classification.kind == _ProviderAttemptKind.LENGTH_CAPPED
                            and _retry_policy.can_retry_attempt(
                                _ProviderAttemptKind.LENGTH_CAPPED,
                                _attempt_retries_used,
                            )
                        ):
                            _attempt_retries_used[_ProviderAttemptKind.LENGTH_CAPPED] += 1
                            visible_text = _append_length_capped_continuation(
                                turn_messages,
                                response_text=response_text,
                                tool_calls=tool_calls,
                            )
                            if visible_text:
                                final_text_parts.append(visible_text)
                            logger.warning(
                                "provider.output_truncated_continue",
                                session_key=self._session_key,
                                model=last_actual_model or self.config.model_id or "",
                                provider=type(self.provider).__name__,
                                iteration=iterations,
                                call_attempt=_call_attempt,
                                tool_calls=len(tool_calls),
                                visible_chars=len(visible_text),
                            )
                            yield WarningEvent(
                                code="provider_output_continue",
                                message=(
                                    "The provider reached its output limit; continuing "
                                    "the response automatically."
                                ),
                            )
                            _thinking_shown_attempt = None
                            _call_attempt += 1
                            continue

                        if (
                            attempt_classification.kind
                            in {
                                _ProviderAttemptKind.REASONING_ONLY,
                                _ProviderAttemptKind.MALFORMED_EMPTY,
                            }
                            and not _invalid_response_fallback_done
                            and self._switch_to_invalid_response_fallback(
                                attempt_classification.kind.value
                            )
                        ):
                            _invalid_response_fallback_done = True
                            yield WarningEvent(
                                code="provider_empty_retry",
                                message=(
                                    "The provider returned no visible response; "
                                    "retrying on a fallback provider."
                                ),
                            )
                            _call_attempt += 1
                            continue

                        yield self._transition(AgentState.ERROR)
                        if attempt_classification.kind == _ProviderAttemptKind.INCOMPLETE_TOOLS:
                            terminal_error = ErrorEvent(
                                message="Provider stream ended with an incomplete tool call",
                                code="incomplete_tool_stream",
                            )
                            yield terminal_error
                            break
                        if attempt_classification.kind == _ProviderAttemptKind.STREAM_INCOMPLETE:
                            terminal_error = ErrorEvent(
                                message="Provider stream ended before a done event",
                                code="provider_stream_incomplete",
                            )
                            yield terminal_error
                            break
                        if attempt_classification.kind == _ProviderAttemptKind.LENGTH_CAPPED:
                            visible_text = strip_synthetic_tool_call_suffix(
                                response_text,
                                [tc.tool_name for tc in tool_calls if tc.synthetic_from_text],
                            )
                            logger.warning(
                                "provider.output_truncated_exhausted",
                                session_key=self._session_key,
                                model=last_actual_model or self.config.model_id or "",
                                provider=type(self.provider).__name__,
                                iteration=iterations,
                                call_attempt=_call_attempt,
                                attempt=_attempt_retries_used.get(
                                    _ProviderAttemptKind.LENGTH_CAPPED, 0
                                ),
                                budget=_retry_policy.attempt_budgets.get(
                                    _ProviderAttemptKind.LENGTH_CAPPED, 0
                                ),
                                tool_calls=len(tool_calls),
                                visible_chars=len(visible_text),
                                partial_preserved=bool(visible_text or final_text_parts),
                            )
                            yield WarningEvent(
                                code="provider_output_truncated",
                                message=(
                                    "The provider stopped because the output limit was reached."
                                ),
                            )
                            terminal_error = ErrorEvent(
                                message=_PROVIDER_OUTPUT_TRUNCATED_REPLY,
                                code="provider_output_truncated",
                            )
                            yield terminal_error
                            break
                        logger.warning(
                            "provider.empty_response",
                            session_key=self._session_key,
                            model=last_actual_model or self.config.model_id or "",
                            provider=type(self.provider).__name__,
                            iteration=iterations,
                            retry_attempt=_call_attempt,
                            post_tool_turn=post_tool_turn,
                            got_done_event=_got_done_event,
                            stop_reason=stop_reason,
                            iter_input_tokens=iter_input_tokens,
                            iter_output_tokens=iter_output_tokens,
                            iter_reasoning_tokens=iter_reasoning_tokens,
                            reasoning_chars=len(iter_reasoning_content or ""),
                        )
                        terminal_error = ErrorEvent(
                            message="Provider returned an empty response",
                            code="empty_response",
                        )
                        yield terminal_error
                        break

                    if (
                        not _got_error
                        and attempt_classification.kind == _ProviderAttemptKind.OK
                        and normalize_stop_reason(stop_reason) is StopReason.MAX_TOKENS
                    ):
                        yield WarningEvent(
                            code="provider_output_truncated",
                            message="The provider stopped because the output limit was reached.",
                        )

                    if (
                        not _got_error
                        and self._session_key
                        and cache_prompt_snapshot is not None
                        and provider_done_for_log is not None
                    ):
                        cache_report = check_response_for_cache_break(
                            self._session_key,
                            cache_prompt_snapshot,
                            provider_done_for_log.cached_tokens,
                            usage_reported=bool(provider_done_for_log.input_tokens)
                            and getattr(provider_done_for_log, "cache_usage_reported", True),
                        )
                        if cache_report.break_detected:
                            logger.warning(
                                "prompt_cache.break_detected",
                                session_key=self._session_key,
                                **cache_report.to_log_dict(),
                            )

                    if not _got_error:
                        break  # stream OK, exit retry loop

                    if provider_error is None:
                        _call_attempt += 1
                        continue

                    if provider_error is not None:
                        provider_error_status_code = (
                            int(provider_error.code) if str(provider_error.code).isdigit() else None
                        )
                        failure_kind = classify_provider_error(
                            provider_name=getattr(self.provider, "provider_name", ""),
                            status_code=provider_error_status_code,
                            raw_code=provider_error.code,
                            message=provider_error.message,
                        )
                        kind = _fallback.classify_error(
                            provider_error.message,
                            provider_name=getattr(self.provider, "provider_name", ""),
                            status_code=provider_error_status_code,
                            raw_code=provider_error.code,
                        )
                        if artifact_delivery_final_response_pending:
                            yield _finish_artifact_delivery_degraded(
                                reason=provider_error.message,
                                code=provider_error.code,
                            )
                            break
                        if max_iterations_finalization_pending:
                            response_text = (
                                "I reached the configured iteration limit, and the "
                                "provider could not generate an additional wrap-up. "
                                "Returning the best partial result from completed work."
                            )
                            assistant_text_parts.append(response_text)
                            provider_done_for_log = ProviderDoneEvent(
                                stop_reason=StopReason.END_TURN
                            )
                            _got_done_event = True
                            _got_error = False
                            max_iterations_finalization_pending = False
                            self._write_turn_call_log(
                                "turn_policy_decision",
                                action="partial_after_finalization_provider_error",
                                reason="max_iterations",
                                code="max_iterations",
                                provider_error_code=provider_error.code,
                            )
                            yield TextDeltaEvent(text=response_text)
                            break
                        if (
                            failure_kind == ProviderFailureKind.EMPTY_RESPONSE
                            and _retry_policy.can_retry_provider_failure(
                                failure_kind,
                                post_tool_turn=post_tool_turn,
                                provider_retry_attempt=_retry_attempt,
                            )
                        ):
                            delay = backoff_sleep(
                                _retry_attempt,
                                _fallback.base_backoff_ms,
                                _fallback.max_backoff_ms,
                                _fake=True,
                            )
                            _log.warning(
                                "provider.empty_response_retry",
                                attempt=_retry_attempt + 1,
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
                            await asyncio.sleep(delay)
                            _retry_attempt += 1
                            _call_attempt += 1
                            continue
                        if failure_kind == ProviderFailureKind.CONTEXT_OVERFLOW:
                            self._record_provider_context_overflow_reason(provider_error)
                            provider_compaction_window_tokens = (
                                self._provider_budget_compaction_window_tokens(provider_error)
                            )
                            provider_estimated_tokens = self._provider_budget_estimated_tokens(
                                provider_error
                            )
                            provider_compaction_refusal_reason = (
                                self._last_compaction_refusal_reason
                            )
                            overflow_total_tokens = provider_estimated_tokens
                            if overflow_total_tokens is None:
                                overflow_total_tokens = (
                                    provider_compaction_window_tokens
                                    or self.config.context_window_tokens
                                ) + 1
                            if overflow_retries >= self.config.max_overflow_retries:
                                yield self._transition(AgentState.ERROR)
                                terminal_error = self._context_overflow_error()
                                yield terminal_error
                                break
                            overflow_retries += 1
                            yield WarningEvent(
                                code="context_auto_compaction_start",
                                message=(
                                    "Provider context limit reached; compacting older "
                                    "context before retrying."
                                ),
                            )
                            overflow_outcome = await self._check_context_overflow(
                                turn_messages,
                                overflow_total_tokens,
                                turn_input_index=turn_input_index,
                                compaction_window_tokens=provider_compaction_window_tokens,
                                force=True,
                            )
                            if overflow_outcome is None:
                                yield self._transition(AgentState.ERROR)
                                terminal_error = self._context_overflow_error()
                                yield terminal_error
                                break
                            if (
                                provider_compaction_refusal_reason
                                and self._last_compaction_refusal_reason is None
                            ):
                                self._last_compaction_refusal_reason = (
                                    provider_compaction_refusal_reason
                                )
                            next_request_messages = self._provider_request_messages(
                                overflow_outcome.messages
                            )
                            if not self._provider_request_is_smaller(
                                request_messages,
                                next_request_messages,
                            ):
                                yield self._transition(AgentState.ERROR)
                                if (
                                    self._last_compaction_refusal_reason
                                    != "provider_recent_tail_too_large"
                                ):
                                    self._last_compaction_refusal_reason = "compaction_not_smaller"
                                terminal_error = self._context_overflow_error()
                                yield terminal_error
                                break
                            turn_messages = overflow_outcome.messages
                            sealed_len = len(turn_messages)
                            turn_input_index = overflow_outcome.turn_input_index
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
                            _call_attempt += 1
                            continue
                        _retry_after_s = honoured_retry_after(
                            failure_kind,
                            provider_error.retry_after_seconds,
                            _retry_after_cap_s,
                        )
                        if _retry_after_s is not None:
                            # The provider named a short wait. Honour it on the
                            # same model (keeps the prompt cache and the price)
                            # rather than a blind backoff or a failover.
                            if _fallback.should_retry(kind, _retry_attempt):
                                delay = retry_after_delay(_retry_after_s)
                                _log.warning(
                                    "provider.retry_after_wait",
                                    attempt=_retry_attempt + 1,
                                    kind=failure_kind.value,
                                    retry_after_s=round(_retry_after_s, 3),
                                    delay_s=round(delay, 2),
                                )
                                yield WarningEvent(
                                    code="provider_retry_after_wait",
                                    message=(
                                        "The provider asked to retry in "
                                        f"{_retry_after_s:g}s; waiting, then retrying "
                                        "the same model."
                                    ),
                                )
                                await asyncio.sleep(delay)
                                _retry_attempt += 1
                                _call_attempt += 1
                                continue
                            # Retries spent: now fail over, once, as an unhinted
                            # rate limit would have done on the first error.
                            if (
                                not _retry_after_fallback_done
                                and self._switch_to_invalid_response_fallback(
                                    provider_error.message
                                )
                            ):
                                _retry_after_fallback_done = True
                                _log.warning(
                                    "provider.retry_after_fallback",
                                    attempt=_retry_attempt,
                                    kind=failure_kind.value,
                                    retry_after_s=round(_retry_after_s, 3),
                                )
                                yield WarningEvent(
                                    code="provider_retry_after_fallback",
                                    message=(
                                        "The provider is still asking to wait; "
                                        "switching to the fallback model."
                                    ),
                                )
                                _call_attempt += 1
                                continue
                        _is_timeout = is_transport_timeout(
                            raw_code=provider_error.code,
                            message=provider_error.message,
                        )
                        if not _fallback.should_retry_timeout(
                            kind, _retry_attempt, is_timeout=_is_timeout
                        ):
                            yield self._transition(AgentState.ERROR)
                            _err_msg = provider_error.message
                            if _is_timeout:
                                _err_msg = (
                                    f"{provider_error.message}. "
                                    "The upstream model endpoint did not respond "
                                    "within the timeout window. Try switching "
                                    "to a different model tier with /c0, /c2, "
                                    "or /auto to restore automatic routing."
                                )
                            terminal_error = ErrorEvent(
                                message=_err_msg,
                                code=provider_error.code,
                            )
                            yield terminal_error
                            break
                        delay = backoff_sleep(
                            _retry_attempt,
                            _fallback.base_backoff_ms,
                            _fallback.max_backoff_ms,
                            _fake=True,
                        )
                        _log.warning(
                            "provider.retry",
                            attempt=_retry_attempt + 1,
                            kind=kind.value,
                            delay_s=round(delay, 2),
                            is_timeout=_is_timeout,
                        )
                        if _is_timeout:
                            yield WarningEvent(
                                code="provider_timeout_retry",
                                message=(
                                    "The upstream model timed out; retrying once before giving up."
                                ),
                            )
                        await asyncio.sleep(delay)
                        _retry_attempt += 1
                        _call_attempt += 1

                if terminal_error is not None:
                    break
                if artifact_delivery_degraded_final_response:
                    break

                response_text = "".join(assistant_text_parts)
                final_stop_reason = (
                    getattr(provider_done_for_log, "stop_reason", None)
                    if provider_done_for_log is not None
                    else None
                )
                if final_stop_reason is not None:
                    turn_stop_reason = str(final_stop_reason)
                    turn_stop_details = getattr(provider_done_for_log, "stop_details", None)
                final_classification = _classify_provider_attempt(
                    text=response_text,
                    tool_calls=tool_calls,
                    pending_tools=pending_tools,
                    got_done_event=_got_done_event,
                    stop_reason=final_stop_reason,
                    reasoning_content=iter_reasoning_content,
                    reasoning_tokens=iter_reasoning_tokens,
                    user_visible_emitted=attempt_user_visible_emitted,
                )
                if final_classification.kind not in {
                    _ProviderAttemptKind.OK,
                    _ProviderAttemptKind.REFUSAL,
                }:
                    logger.warning(
                        "provider.invalid_response_unhandled",
                        session_key=self._session_key,
                        model=last_actual_model or self.config.model_id or "",
                        provider=type(self.provider).__name__,
                        classification=final_classification.kind.value,
                        iteration=iterations,
                        call_attempt=_call_attempt,
                        got_done_event=_got_done_event,
                        stop_reason=final_stop_reason,
                        iter_input_tokens=iter_input_tokens,
                        iter_output_tokens=iter_output_tokens,
                        iter_reasoning_tokens=iter_reasoning_tokens,
                        reasoning_chars=len(iter_reasoning_content or ""),
                    )
                    yield self._transition(AgentState.ERROR)
                    if final_classification.kind == _ProviderAttemptKind.INCOMPLETE_TOOLS:
                        terminal_error = ErrorEvent(
                            message="Provider stream ended with an incomplete tool call",
                            code="incomplete_tool_stream",
                        )
                        yield terminal_error
                        break
                    if final_classification.kind == _ProviderAttemptKind.STREAM_INCOMPLETE:
                        terminal_error = ErrorEvent(
                            message="Provider stream ended before a done event",
                            code="provider_stream_incomplete",
                        )
                        yield terminal_error
                        break
                    if final_classification.kind == _ProviderAttemptKind.LENGTH_CAPPED:
                        terminal_error = ErrorEvent(
                            message=_PROVIDER_OUTPUT_TRUNCATED_REPLY,
                            code="provider_output_truncated",
                        )
                        yield terminal_error
                        break
                    terminal_error = ErrorEvent(
                        message="Provider returned an empty response",
                        code="empty_response",
                    )
                    yield terminal_error
                    break

                if iter_reasoning_content:
                    final_reasoning_parts.append(iter_reasoning_content)

                assembled_text = "".join(assistant_text_parts)
                visible_text = strip_synthetic_tool_call_suffix(
                    assembled_text,
                    [tc.tool_name for tc in tool_calls if tc.synthetic_from_text],
                )
                if visible_text:
                    final_text_parts.append(visible_text)

                preflight_tool_results: dict[str, ToolResult] = {}
                terminal_projection_preflight_error = False
                resolved_tool_calls: list[ToolCall] = []
                for tc in tool_calls:
                    resolved = self._rehydrate_projected_tool_arguments(tc)
                    if isinstance(resolved, ToolResult):
                        preflight_tool_results[tc.tool_use_id] = resolved
                        if self._is_provider_context_projection_reuse_result(resolved):
                            terminal_projection_preflight_error = True
                        resolved_tool_calls.append(self._sanitize_projected_tool_call_arguments(tc))
                        continue
                    resolved_tool_calls.append(resolved)
                tool_calls = resolved_tool_calls

                # Build assistant message for history. Per-block thinking
                # (each block with its own signature / redacted data, in
                # stream order) is replayed unchanged; the joined text with the
                # last signature is only the fallback for older providers.
                assistant_content: list[Any] = []
                done_thinking_blocks = list(
                    getattr(provider_done_for_log, "thinking_blocks", None) or []
                )
                if done_thinking_blocks:
                    assistant_content.extend(done_thinking_blocks)
                elif iter_reasoning_content and iter_thinking_signature:
                    assistant_content.append(
                        ContentBlockThinking(
                            thinking=iter_reasoning_content,
                            signature=iter_thinking_signature,
                        )
                    )
                if visible_text:
                    assistant_content.append(ContentBlockText(text=visible_text))
                for tc in tool_calls:
                    assistant_content.append(
                        ContentBlockToolUse(
                            id=tc.tool_use_id,
                            name=tc.tool_name,
                            input=tc.arguments,
                            thought_signature=tc.thought_signature,
                        )
                    )
                if assistant_content:
                    turn_messages.append(
                        Message(
                            role="assistant",
                            content=assistant_content,
                            reasoning_content=iter_reasoning_content,
                            reasoning_details=getattr(
                                provider_done_for_log, "reasoning_details", None
                            ),
                            # Read after the stream: a failover wrapper now
                            # reports the provider that actually answered.
                            provenance=provenance_for_provider(self.provider),
                        )
                    )

                # Detect incomplete tool calls (stream interrupted mid-generation)
                if pending_tools and not tool_calls:
                    _log.warning(
                        "agent.stream_interrupted",
                        session_key=self._session_key,
                        pending_tool_ids=list(pending_tools.keys()),
                        pending_tool_names=[acc.tool_name for acc in pending_tools.values()],
                        got_done_event=_got_done_event,
                        text_len=len(assembled_text),
                        iteration=iterations,
                    )
                if not _got_done_event and (assembled_text or pending_tools):
                    _log.warning(
                        "agent.provider_stream_incomplete",
                        session_key=self._session_key,
                        got_text=bool(assembled_text),
                        pending_tools=len(pending_tools),
                        tool_calls=len(tool_calls),
                    )

                # No tool calls → we're done
                if not tool_calls:
                    max_iterations_finalization_pending = False
                    # The model is answering rather than acting: this is the
                    # moment it stops, and the last chance to notice that it is
                    # stopping on edits nothing checked.
                    verification_decision = verification_ledger.decide(
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
                    break

                tool_deadline = _loop.time() + self.config.iteration_timeout

                # ------ STREAMING → TOOL_CALLING ------
                yield self._transition(AgentState.TOOL_CALLING)

                # Execute tools under each tool's declared concurrency and
                # deliver every result as soon as it finishes; approvals are
                # awaited concurrently. ``outcomes`` keeps the original
                # tool_calls order, one result per tool_use.
                tool_batch = ToolBatchExecutor(
                    execute=self._execute_tool,
                    project=self._project_tool_result_for_delivery,
                    timeout_for=self._tool_execution_timeout,
                    spec_for=registry_spec_lookup(self._tool_registry),
                    deadline=tool_deadline,
                    total_deadline=_total_deadline,
                    iteration_timeout=self.config.iteration_timeout,
                    session_key=self._session_key,
                    max_concurrency=self._max_safe_tool_concurrency(),
                    heartbeat_interval=self._tool_activity_heartbeat_interval(),
                    approval_wait_timeout=self._approval_wait_timeout(),
                    pending_approval=_pending_approval_payload,
                    wait_for_approval=self.turn_timing.timed_approval(
                        _wait_for_pending_approval_resolution
                    ),
                    outside_wait=self.turn_timing.timed_approval(_await_outside_slot),
                    artifact_event=lambda a: ArtifactEvent(**_artifact_event_kwargs(a)),
                    pending_warnings=self._pending_warnings,
                    preflight_results=preflight_tool_results,
                    call_log=self._write_turn_call_log,
                    iteration=iterations,
                    clock=_loop.time,
                )
                async for event in self.turn_timing.tool_batch(
                    tool_batch.run(tool_calls), calls=len(tool_calls)
                ):
                    yield event
                tool_result_blocks = tool_batch.result_blocks()
                executed_results = tool_batch.executed_results
                turn_yielded = any(
                    self._is_turn_yield_result(result) or result.terminates_turn
                    for result in executed_results
                )

                terminal_artifacts = self._terminal_artifact_delivery_artifacts(executed_results)
                if terminal_artifacts:
                    artifact_delivery_final_response_artifacts = terminal_artifacts

                turn_tool_errors += sum(1 for result in executed_results if result.is_error)
                first_tool_error = next(
                    (result for result in executed_results if result.is_error),
                    None,
                )
                arguments_by_id = {tc.tool_use_id: tc.arguments for tc in tool_calls}

                # Record what this iteration changed and whether it checked it.
                # Order matters: a check only counts for edits made before it.
                for result in executed_results:
                    if result.is_error:
                        continue
                    arguments = arguments_by_id.get(result.tool_use_id) or {}
                    if result.tool_name in _MUTATING_TOOL_NAMES:
                        verification_ledger.record_mutation(_mutated_paths(arguments))
                    elif result.tool_name in _COMMAND_TOOL_NAMES:
                        verification_ledger.record_command(
                            str(arguments.get("command") or arguments.get("code") or "")
                        )
                watchdog_decision = progress_watchdog.observe(
                    ProgressObservation(
                        iteration=iterations,
                        provider_call_count=turn_llm_calls,
                        successful_tool_result=any(
                            not result.is_error for result in executed_results
                        ),
                        user_visible_output=bool("".join(final_text_parts).strip()),
                        artifact_completed=bool(terminal_artifacts),
                        tool_error_signature=(
                            None
                            if first_tool_error is None
                            else (
                                f"{first_tool_error.tool_name}:"
                                f"{str(first_tool_error.content)[:160]}"
                            )
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
                        "progress_watchdog",
                        action=watchdog_decision.action,
                        reason=watchdog_decision.reason,
                        details=watchdog_decision.details,
                        guidance=guidance_for(watchdog_decision),
                    )
                terminal_error = _turn_budget_error()
                if terminal_error is not None:
                    if artifact_delivery_final_response_pending:
                        yield _finish_artifact_delivery_degraded(
                            reason=terminal_error.message,
                            code=terminal_error.code,
                        )
                        terminal_error = None
                    else:
                        yield self._transition(AgentState.ERROR)
                        yield terminal_error
                    break

                if any(_is_threshold_denial(result) for result in executed_results):
                    yield self._transition(AgentState.ERROR)
                    terminal_error = ErrorEvent(
                        message=(
                            "Autonomous execution paused after repeated sandbox denials. "
                            "Human intervention is required before continuing."
                        ),
                        code="sandbox_threshold_exceeded",
                    )
                    yield terminal_error
                    break

                # Per-iteration deadline check after tool execution
                if _loop.time() > tool_deadline:
                    yield self._transition(AgentState.ERROR)
                    terminal_error = ErrorEvent(
                        message=(
                            f"Iteration {iterations} exceeded iteration_timeout"
                            f" ({self.config.iteration_timeout}s) during tool execution"
                        ),
                        code="iteration_timeout",
                    )
                    yield terminal_error
                    break

                # Feed tool results back as user message
                turn_messages.append(
                    Message(role="user", content=tool_result_blocks)  # type: ignore[arg-type]
                )
                if terminal_projection_preflight_error:
                    self._write_turn_call_log(
                        "tool_argument_projection_rehydrate_recovery",
                        iteration=iterations,
                        tool_use_ids=sorted(preflight_tool_results),
                    )
                if terminal_artifacts:
                    _finish_artifact_delivery_without_provider()
                    break
                if turn_yielded:
                    break

                # ------ TOOL_CALLING → THINKING ------
                yield self._transition(AgentState.THINKING)
                # Loop continues

        except TimeoutError:
            if artifact_delivery_final_response_pending:
                yield _finish_artifact_delivery_degraded(
                    reason=f"Agent turn timed out after {self.config.timeout}s",
                    code="agent_runtime_timeout",
                )
            else:
                # Total turn deadline exceeded (raised by manual check above)
                yield self._transition(AgentState.ERROR)
                terminal_error = ErrorEvent(
                    message=f"Agent turn timed out after {self.config.timeout}s",
                    code="agent_runtime_timeout",
                )
                yield terminal_error

        if terminal_error is None:
            # Persist successful turns into in-memory history. Error turns are
            # persisted by TurnRunner as system errors, while their usage still
            # flows through the final DoneEvent below when provider usage exists.
            self._seal_transcript(turn_messages, sealed_len)
            self._cap_turn_for_history(turn_messages)
            self._history = list(turn_messages)
            self._record_turn_transcript(self._history)
            self._write_context_stage("session:after", self._history)

        # ------ → DONE ------
        # Compute per-turn cost from pricing table
        done_model = last_actual_model
        if not done_model and self._usage_tracker and self._session_key:
            su = self._usage_tracker.get(self._session_key)
            if su and su.model_id:
                done_model = su.model_id
        if not done_model:
            done_model = self.config.model_id or ""
        from agentos.engine.pricing import lookup_price

        done_provider = provider_metadata(self.provider)
        price = lookup_price(
            done_model,
            provider_id=done_provider.provider_kind or done_provider.provider_name,
        )
        estimated_cost = (
            total_input_tokens * price.input_per_m + total_output_tokens * price.output_per_m
        ) / 1_000_000
        if total_billed_cost > 0.0:
            done_cost = total_billed_cost
            cost_source = "provider_billed"
        elif estimated_cost > 0.0:
            done_cost = estimated_cost
            cost_source = "agentos_static_estimate"
        else:
            done_cost = 0.0
            cost_source = "unavailable"

        session_totals = (
            self._usage_tracker.session_snapshot(self._session_key)
            if self._usage_tracker and self._session_key
            else None
        )
        turn_usage_delta = (
            self._usage_tracker.session_delta_snapshot(self._session_key, usage_turn_baseline)
            if self._usage_tracker and self._session_key
            else None
        )
        done_input_tokens = total_input_tokens
        done_output_tokens = total_output_tokens
        done_cached_tokens = total_cached_tokens
        done_cache_write_tokens = total_cache_write_tokens
        done_billed_cost = total_billed_cost
        if turn_usage_delta and (
            turn_usage_delta.input_tokens
            or turn_usage_delta.output_tokens
            or turn_usage_delta.cache_read_tokens
            or turn_usage_delta.cache_write_tokens
            or turn_usage_delta.cost_usd
            or turn_usage_delta.billed_cost
        ):
            done_input_tokens = turn_usage_delta.input_tokens
            done_output_tokens = turn_usage_delta.output_tokens
            done_cached_tokens = turn_usage_delta.cache_read_tokens
            done_cache_write_tokens = turn_usage_delta.cache_write_tokens
            done_cost = turn_usage_delta.cost_usd
            done_billed_cost = turn_usage_delta.billed_cost
            cost_source = _cost_source_for_usage(done_cost, done_billed_cost)

        has_usage = bool(
            done_input_tokens
            or done_output_tokens
            or total_reasoning_tokens
            or done_cached_tokens
            or done_cache_write_tokens
            or done_billed_cost
        )
        if terminal_error is None or has_usage:
            if terminal_error is None:
                yield self._transition(AgentState.DONE)
            yield DoneEvent(
                text="".join(final_text_parts),
                input_tokens=done_input_tokens,
                output_tokens=done_output_tokens,
                reasoning_tokens=total_reasoning_tokens,
                cached_tokens=done_cached_tokens,
                cache_write_tokens=done_cache_write_tokens,
                iterations=iterations,
                cost_usd=done_cost,
                billed_cost=done_billed_cost,
                cost_source=cost_source,
                model=done_model,
                runtime_context_hash=runtime_context_hash,
                runtime_context_chars=len(runtime_context),
                reasoning_content=(
                    "\n".join(final_reasoning_parts) if final_reasoning_parts else None
                ),
                session_totals=session_totals,
                stop_reason=turn_stop_reason,
                stop_details=turn_stop_details,
            )
        # Reset for next turn
        self._state = AgentState.IDLE

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
            done, _ = await asyncio.wait({next_event}, timeout=wait_budget)
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

    def _seal_transcript(self, messages: list[Message], sealed_len: int) -> int:
        """Seal ``messages[sealed_len:]`` in place; return the new sealed length."""
        if sealed_len >= len(messages):
            return len(messages)
        report = seal_window(
            messages,
            sealed_len,
            shrink_tool_result=self._shrink_tool_result_for_transcript,
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

    def _shrink_tool_result_for_transcript(
        self,
        block: ContentBlockToolResult,
        tool_name: str,
    ) -> ContentBlockToolResult:
        """Project one oversized tool result to its budget before first send.

        The full payload goes to the on-disk tool-result store (the projection
        names its handle); the transcript keeps the projection forever. Error
        and artifact-producing results are never projected.
        """
        if not isinstance(block.content, str) or block.is_error:
            return block
        content = block.content
        budget_class = resolve_budget_class(tool_name)
        result_cap = self._tool_result_provider_request_max_chars(budget_class)
        if result_cap <= 0 or len(content) <= result_cap:
            return block
        if _tool_result_content_has_artifact(content):
            return block
        if budget_class is ToolResultBudgetClass.CONTROL:
            replacement = compact_tool_result_content(
                tool_name=tool_name,
                content=content,
                max_preview_chars=160,
                budget_class=budget_class,
                is_error=block.is_error,
            )
        else:
            replacement = self._tool_result_projection_for_provider(
                content,
                tool_use_id=block.tool_use_id,
                tool_name=tool_name or "tool",
                reason=(
                    "external tool result compacted for provider request context"
                    if budget_class is ToolResultBudgetClass.EXTERNAL
                    else "tool result compacted for provider request context"
                ),
                max_preview_chars=min(result_cap, 4_000),
            )
        if len(replacement) >= len(content):
            return block
        self.config.metadata["tool_provider_guard_projection_applied"] = True
        self.config.metadata["tool_provider_guard_projection_calls"] = (
            self.config.metadata.get("tool_provider_guard_projection_calls", 0) + 1
        )
        self.config.metadata["tool_projection_applied"] = True
        self.config.metadata["tool_projection_calls"] = (
            self.config.metadata.get("tool_projection_calls", 0) + 1
        )
        return ContentBlockToolResult(
            tool_use_id=block.tool_use_id,
            content=replacement,
            is_error=block.is_error,
            execution_status=(
                mark_execution_status_truncated(block.execution_status)
                if block.execution_status is not None
                else None
            ),
        )

    def _history_tool_payload_cap(self) -> int:
        """Max chars a tool result / argument keeps once its turn is over.

        ``[compaction] history_tool_result_max_chars``; ``0`` (default) means
        2% of the context window in chars, clamped to 4k-12k: a finished
        turn's 120k-char file read is not worth replaying at full size on
        every later request, and its full text stays in the tool-result store.
        """
        configured = self._positive_int(getattr(self.config, "history_tool_result_max_chars", 0))
        if configured is not None:
            return configured
        window_chars = int(self.config.context_window_tokens) * 4
        return max(4_000, min(12_000, int(window_chars * 0.02)))

    def _cap_turn_for_history(self, messages: list[Message]) -> None:
        """Bound this turn's tool payloads once, at the turn boundary.

        During the turn every result was sent at its per-class budget. Before
        the turn is persisted, results and tool-call argument fields over the
        history cap become head/tail projections that name their tool-result
        store handle. Earlier turns were capped when they ended and are never
        touched again, so the next request re-sends this turn's tail once (one
        cache write) and every later request repeats it byte for byte.
        """
        start = 2 if self._turn_reset else self._turn_base_len
        if start >= len(messages):
            return
        cap = self._history_tool_payload_cap()
        tool_names: dict[str, str] = {}
        for index in range(start, len(messages)):
            message = messages[index]
            if not isinstance(message.content, list):
                continue
            blocks: list[Any] = []
            changed = False
            for block in message.content:
                replacement = block
                if isinstance(block, ContentBlockToolUse):
                    tool_names[block.id] = block.name
                    replacement = self._cap_tool_use_arguments(block, cap)
                elif isinstance(block, ContentBlockToolResult):
                    replacement = self._cap_tool_result(
                        block, cap, tool_names.get(block.tool_use_id, "tool")
                    )
                changed = changed or replacement is not block
                blocks.append(replacement)
            if changed:
                messages[index] = Message(
                    role=message.role,
                    content=blocks,
                    reasoning_content=message.reasoning_content,
                    reasoning_details=message.reasoning_details,
                    provenance=message.provenance,
                )
                self._message_sizes.retain_only(messages)

    def _cap_tool_result(
        self,
        block: ContentBlockToolResult,
        cap: int,
        tool_name: str,
    ) -> ContentBlockToolResult:
        content = block.content
        if not isinstance(content, str) or len(content) <= cap:
            return block
        if _tool_result_content_has_artifact(content):
            return block
        projection = self._tool_result_projection_for_provider(
            content,
            tool_use_id=block.tool_use_id,
            tool_name=tool_name,
            reason="tool result from an earlier turn, shortened for history",
            max_preview_chars=min(cap, 4_000),
        )
        if len(projection) >= len(content):
            return block
        return ContentBlockToolResult(
            tool_use_id=block.tool_use_id,
            content=projection,
            is_error=block.is_error,
            execution_status=(
                mark_execution_status_truncated(block.execution_status)
                if block.execution_status is not None
                else None
            ),
        )

    def _cap_tool_use_arguments(self, block: ContentBlockToolUse, cap: int) -> ContentBlockToolUse:
        oversized = [
            key for key, value in block.input.items() if isinstance(value, str) and len(value) > cap
        ]
        if not oversized:
            return block
        arguments = dict(block.input)
        for key in oversized:
            value = str(block.input[key])
            stored = self._store_tool_result_snapshot(
                value,
                tool_use_id=f"{block.id}:{key}",
                tool_name=block.name,
            )
            arguments[key] = history_argument_projection(
                tool_name=block.name,
                tool_use_id=block.id,
                field_name=key,
                value=value,
                handle=stored.handle if stored is not None else None,
            )
        return ContentBlockToolUse(
            id=block.id,
            name=block.name,
            input=arguments,
            thought_signature=block.thought_signature,
        )

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

    def _record_turn_transcript(self, messages: list[Message]) -> None:
        if self._turn_reset:
            appended = list(messages)
        else:
            appended = list(messages[self._turn_base_len :])
        self._turn_record = TranscriptRecord(
            messages=persistable_messages(appended),
            mode="reset" if self._turn_reset else "append",
            input_entry_id=self._transcript_input_entry_id,
            compaction_id=self._turn_compaction_id,
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

    def _compaction_service(self, *, window_tokens: int | None = None) -> CompactionService:
        del window_tokens
        hint = getattr(self.config, "compaction_summary_model_hint", None) or ("", "")
        return CompactionService(
            provider=self.provider,
            ratio=float(self.config.context_overflow_threshold),
            session_key=self._session_key,
            # Summaries go through the auxiliary side-task client (cheap model,
            # no thinking, accounted as aux:compaction), never the turn's own
            # model, and are bounded by their own timeout.
            chunk_summarizer=auxiliary_chunk_summarizer(
                hint=(str(hint[0] or ""), str(hint[1] or "")),
                session_key=self._session_key,
            ),
            timeout=float(getattr(self.config, "compaction_summary_timeout_seconds", 45.0) or 45.0),
        )

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
    ) -> CompactionOutcome | None:
        """Run the compaction service; ``None`` signals a failed compaction."""
        self._last_compaction_refusal_reason = None
        window_tokens = compaction_window_tokens or self.config.context_window_tokens
        input_index = len(messages) if turn_input_index is None else turn_input_index
        declined_at = self._turn_compaction_declined_tokens
        if not force and declined_at is not None and estimated_context_tokens <= declined_at * 1.25:
            # This turn already found compaction not worth it at about this
            # size; do not pay for (or log) the same attempt on every request.
            return CompactionOutcome(messages=messages, turn_input_index=input_index)
        result = await self._compaction_service().compact(
            messages,
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

    @staticmethod
    def _is_provider_context_projection_reuse_result(result: ToolResult) -> bool:
        status: Mapping[str, Any] = result.execution_status or {}
        return bool(
            result.is_error
            and isinstance(status, dict)
            and status.get("reason") == _PROVIDER_CONTEXT_PROJECTION_REUSED_REASON
        )

    @staticmethod
    def _parse_tool_argument_projection(value: str) -> dict[str, str] | None:
        if not value.startswith(_TOOL_ARGUMENT_PROJECTION_PREFIX):
            return None
        metadata: dict[str, str] = {}
        for line in value.splitlines()[1:]:
            if line in {"head:", "tail:"}:
                break
            key, separator, raw_value = line.partition(":")
            if not separator:
                continue
            metadata[key.strip()] = raw_value.strip()
        return metadata

    @staticmethod
    def _provider_projection_placeholder(tool_name: str, field: str) -> str:
        return (
            f"[invalid_provider_context_projection:{tool_name}.{field}] "
            "provider-only compacted tool argument omitted; regenerate the real "
            "argument instead of copying provider context."
        )

    @staticmethod
    def _has_provider_context_argument_marker(arguments: dict[str, Any]) -> bool:
        return arguments.get(_INVALID_PROVIDER_CONTEXT_ARGUMENTS_KEY) is True or any(
            arguments.get(marker) is True for marker in _COMPACTED_TOOL_ARGUMENT_MARKERS
        )

    @staticmethod
    def _provider_compacted_arguments_placeholder(
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            _INVALID_PROVIDER_CONTEXT_ARGUMENTS_KEY: True,
            "tool": tool_name,
            "reason": "provider_context_omitted",
        }

    def _sanitize_projected_tool_call_arguments(self, tc: ToolCall) -> ToolCall:
        if self._has_provider_context_argument_marker(tc.arguments):
            return ToolCall(
                tool_use_id=tc.tool_use_id,
                tool_name=tc.tool_name,
                arguments=self._provider_compacted_arguments_placeholder(
                    tc.tool_name,
                    tc.arguments,
                ),
                synthetic_from_text=tc.synthetic_from_text,
                origin_trace=tc.origin_trace,
            )
        sanitized = dict(tc.arguments)
        changed = False
        for argument_name, value in tc.arguments.items():
            if not isinstance(value, str) or not value.startswith(
                (
                    _TOOL_ARGUMENT_PROJECTION_PREFIX,
                    _HISTORICAL_TOOL_ARGUMENT_PROJECTION_PREFIX,
                    _INVALID_PROVIDER_CONTEXT_PROJECTION_PREFIX,
                )
            ):
                continue
            sanitized[argument_name] = self._provider_projection_placeholder(
                tc.tool_name,
                argument_name,
            )
            changed = True
        if not changed:
            return tc
        return ToolCall(
            tool_use_id=tc.tool_use_id,
            tool_name=tc.tool_name,
            arguments=sanitized,
            synthetic_from_text=tc.synthetic_from_text,
            origin_trace=tc.origin_trace,
        )

    def _projection_rehydrate_error(
        self,
        tc: ToolCall,
        *,
        field: str,
        reason: str,
    ) -> ToolResult:
        self.config.metadata["tool_argument_projection_rehydrate_failures"] = (
            self.config.metadata.get(
                "tool_argument_projection_rehydrate_failures",
                0,
            )
            + 1
        )
        self._write_turn_call_log(
            "tool_argument_projection_rehydrate_failed",
            tool_use_id=tc.tool_use_id,
            tool_name=tc.tool_name,
            field=field,
            reason=reason,
        )
        return ToolResult(
            tool_use_id=tc.tool_use_id,
            tool_name=tc.tool_name,
            content=(
                f"The {tc.tool_name}.{field} input was not available in executable "
                "form. The tool was not run; regenerate the complete argument and "
                "retry the tool call."
            ),
            is_error=True,
            execution_status=runtime_execution_status(
                "error",
                reason=_PROVIDER_CONTEXT_PROJECTION_REUSED_REASON,
            ),
        )

    def _provider_compacted_arguments_error(
        self,
        tc: ToolCall,
        *,
        reason: str,
    ) -> ToolResult:
        self.config.metadata["tool_argument_projection_rehydrate_failures"] = (
            self.config.metadata.get(
                "tool_argument_projection_rehydrate_failures",
                0,
            )
            + 1
        )
        self._write_turn_call_log(
            "tool_argument_projection_rehydrate_failed",
            tool_use_id=tc.tool_use_id,
            tool_name=tc.tool_name,
            reason=reason,
        )
        return ToolResult(
            tool_use_id=tc.tool_use_id,
            tool_name=tc.tool_name,
            content=(
                f"The {tc.tool_name} arguments were not available in executable "
                "form. The tool was not run; regenerate the complete arguments and "
                "retry the tool call."
            ),
            is_error=True,
            execution_status=runtime_execution_status(
                "error",
                reason=_PROVIDER_CONTEXT_PROJECTION_REUSED_REASON,
            ),
        )

    def _rehydrate_projected_tool_arguments(
        self,
        tc: ToolCall,
    ) -> ToolCall | ToolResult:
        if self._has_provider_context_argument_marker(tc.arguments):
            return self._provider_compacted_arguments_error(
                tc,
                reason="provider_compacted_arguments_reused",
            )
        for argument_name, value in tc.arguments.items():
            if not isinstance(value, str) or not value.startswith(
                (
                    _TOOL_ARGUMENT_PROJECTION_PREFIX,
                    _HISTORICAL_TOOL_ARGUMENT_PROJECTION_PREFIX,
                    _INVALID_PROVIDER_CONTEXT_PROJECTION_PREFIX,
                )
            ):
                continue
            return self._projection_rehydrate_error(
                tc,
                field=argument_name,
                reason="provider_projection_reused",
            )
        return tc

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
                resolved = self._rehydrate_projected_tool_arguments(tc)
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

    def _make_child_agent(self, spec: SubagentSpec, depth: int) -> Agent:
        from agentos.session.keys import parse_agent_id
        from agentos.tools.types import (
            SUBAGENT_TOOL_DENY,
            CallerKind,
            InteractionMode,
            ToolContext,
            current_tool_context,
        )

        parent_session_key = self._session_key or "unknown"
        subagent_label = spec.label or "subagent"

        # Schema-time filtering: subagents cannot see dangerous tools
        filtered_defs = [td for td in self.tool_definitions if td.name not in SUBAGENT_TOOL_DENY]
        subagent_ctx = ToolContext(
            caller_kind=CallerKind.SUBAGENT,
            interaction_mode=InteractionMode.UNATTENDED,
            subagent_depth=depth,
            agent_id=parse_agent_id(parent_session_key),
            workspace_dir=spec.workspace_dir or self.config.workspace_dir,
            session_key=f"subagent:{parent_session_key}",
            channel_kind="subagent",
            channel_id=f"subagent:{parent_session_key}",
            sender_id=parent_session_key,
            denied_tools=set(SUBAGENT_TOOL_DENY),
            tool_run_budget_key=(
                f"subagent:{parent_session_key}:{subagent_label}:{depth}:{uuid.uuid4().hex}"
            ),
        )

        async def _subagent_tool_handler(tc: ToolCall) -> ToolResult:
            if self.tool_handler is None:
                return ToolResult(
                    tool_use_id=tc.tool_use_id,
                    tool_name=tc.tool_name,
                    content=f"No tool handler registered for tool '{tc.tool_name}'",
                    is_error=True,
                    execution_status=runtime_execution_status(
                        "error",
                        reason="runtime_error",
                    ),
                )
            token = current_tool_context.set(subagent_ctx)
            try:
                return await self.tool_handler(tc)
            finally:
                current_tool_context.reset(token)

        child_cfg = AgentConfig(
            max_iterations=spec.max_iterations,
            timeout=spec.timeout,
            max_tokens=self.config.max_tokens,
            max_turn_llm_calls=self.config.max_turn_llm_calls,
            max_turn_input_tokens=self.config.max_turn_input_tokens,
            max_turn_output_tokens=self.config.max_turn_output_tokens,
            max_turn_billed_cost_usd=self.config.max_turn_billed_cost_usd,
            max_turn_tool_errors=self.config.max_turn_tool_errors,
            length_capped_continuations=self.config.length_capped_continuations,
            context_window_tokens=self.config.context_window_tokens,
            workspace_dir=spec.workspace_dir or self.config.workspace_dir,
            tool_result_projection_max_inline_chars=(
                self.config.tool_result_projection_max_inline_chars
            ),
            tool_result_provider_request_max_chars=(
                self.config.tool_result_provider_request_max_chars
            ),
            provider_request_proof_max_chars=self.config.provider_request_proof_max_chars,
            tool_use_argument_provider_request_max_chars=(
                self.config.tool_use_argument_provider_request_max_chars
            ),
            tool_use_argument_projection_enabled=(self.config.tool_use_argument_projection_enabled),
            tool_failure_loop_block_threshold=(self.config.tool_failure_loop_block_threshold),
            max_safe_tool_concurrency=self.config.max_safe_tool_concurrency,
            parallel_tool_calls=self.config.parallel_tool_calls,
            tool_result_store_dir=self.config.tool_result_store_dir,
            tool_result_store_session_id=self.config.tool_result_store_session_id,
            tool_result_store_session_key=self.config.tool_result_store_session_key,
            tool_result_store_agent_id=self.config.tool_result_store_agent_id,
            tool_result_store_max_bytes=self.config.tool_result_store_max_bytes,
            tool_result_store_disk_budget_bytes=(self.config.tool_result_store_disk_budget_bytes),
            tool_result_store_retention_seconds=(self.config.tool_result_store_retention_seconds),
        )
        return Agent(
            provider=self.provider,
            config=child_cfg,
            tool_definitions=filtered_defs,
            tool_handler=_subagent_tool_handler,
            subagent_manager=SubagentManager(spawn_depth=depth),
        )

    async def spawn_subagent(self, spec: SubagentSpec) -> str:
        """Spawn a subagent and return its run_id."""
        handle = await self.subagent_manager.spawn(spec, self._make_child_agent)
        return handle.run_id
