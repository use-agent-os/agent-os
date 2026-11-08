"""TurnRunner: shared agent orchestration layer.

Single convergence point for all entry points (Web UI, CLI, Channel).
Extracted from gateway/rpc_sessions.py:_run_agent_turn() closure.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import contextvars
import copy
import hashlib
import inspect
import json
import os
import platform
import time
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, SupportsInt, TypeGuard, cast

import structlog

from agentos.artifacts import artifact_marker
from agentos.attachment_refs import (
    is_attachment_ref,
    read_attachment_ref_bytes,
    transcript_material_path,
)
from agentos.bootstrap_types import BootstrapFileReport
from agentos.compat.inspect_utils import accepts_keyword_arg
from agentos.contracts.attachments import (
    ALLOWED_MEDIA_TYPES as _ALLOWED_ENGINE_MEDIA_TYPES,
)
from agentos.contracts.attachments import (
    MAX_ATTACHMENTS as _MAX_ATTACHMENT_COUNT,
)
from agentos.contracts.attachments import (
    TEXT_ATTACHMENT_MIMES as _ENGINE_TEXT_FAMILY_MIMES,
)
from agentos.contracts.attachments import (
    attachment_size_limit_for_mime as _attachment_size_limit_for_mime,
)
from agentos.engine.agent import Agent, ToolHandler
from agentos.engine.approval_port import default_approval_waiter
from agentos.engine.cache_break_monitor import notify_compaction
from agentos.engine.harness.ports import (
    AgentTurnRunner,
    CompactionPersistRejectedError,
    MemorySyncNotifier,
    PromptReportBuilder,
    RequestContextPrepender,
)
from agentos.engine.harness.turn import TurnHarness, TurnRequest, TurnStages
from agentos.engine.hooks import (
    CompactionHook,
    DefaultTraceEmitterHook,
    RequestHook,
    ToolHook,
    TurnEvent,
    TurnHook,
    TurnHookContext,
)
from agentos.engine.outcome import outcome_from_error, turn_outcome_details
from agentos.engine.pipeline import DEFAULT_PROMPT_STEPS, PromptStepRegistry, TurnContext
from agentos.engine.pricing import PriceEntry, lookup_price
from agentos.engine.proactive_compaction import ProactiveCompactionScheduler
from agentos.engine.repetition_guard import (
    RepetitionGuardSettings,
    repetition_guard_settings_from,
)
from agentos.engine.router_loop import router_loop
from agentos.engine.turn_policy import TurnPolicyResolver
from agentos.engine.turn_progress import TurnProgressWriter
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
from agentos.engine.turn_runner.agent_bootstrap_stage import (
    _AgentConfigAuxiliaries,
    _MemorySnapshotResult,
    _ResolvedBudgets,
    _ResolvedCatalog,
)
from agentos.engine.turn_runner.pre_provider_io import (
    MEMORY_CONTEXT_KEY,
    PROJECT_KNOWLEDGE_KEY,
    PreProviderIO,
    RecallBudget,
    merge_extra_context_block,
)
from agentos.engine.turn_runner.prompt_assembler_stage import RunPipelineRequest
from agentos.engine.turn_runner.turn_finalizer_stage import CostRollupResult
from agentos.engine.types import (
    AgentConfig,
    AgentEvent,
    DoneEvent,
    ErrorEvent,
    ThinkingLevel,
    ToolResultEvent,
    WarningEvent,
)
from agentos.execution_status import (
    mark_execution_status_truncated,
    normalize_execution_status,
)
from agentos.observability.decision_log import (
    DecisionEntry,
    PipelineStepRecord,
    SavingsTelemetry,
    build_intent_summary,
    compute_hashes,
    write_decision_entry,
)
from agentos.observability.prompt_report import PromptReport
from agentos.observability.trace import TraceContext, TraceEvent, write_trace_event
from agentos.paths import media_root_from_config
from agentos.provider import (
    ErrorEvent as ProviderErrorEvent,
)
from agentos.provider import (
    ProviderFailureKind,
    ProviderHeartbeatEvent,
    ProviderRecoveryAction,
    classify_provider_error,
    decide_recovery_action,
)
from agentos.provider.circuit_breaker import trips_breaker
from agentos.provider.failures import honoured_retry_after
from agentos.provider.retry_after import DEFAULT_RETRY_AFTER_CAP_SECONDS
from agentos.result_budget import persisted_result_max_chars
from agentos.router_control import (
    RouterControlHoldStore,
    render_router_control_prompt_block,
)
from agentos.safety import injection_guard, permission_matrix, sandbox, tool_tiers
from agentos.session.compaction_lifecycle import (
    COMPACTION_REPLAYED_EVENT,
    compaction_lifecycle_payload,
    durable_receipt_allows_destructive_compaction,
    flush_receipt_allows_destructive_compaction,
    flush_receipt_is_successful_flush,
    flush_receipt_status_for_compaction,
    mark_compaction_flush_status_with_retry,
    new_compaction_id,
)
from agentos.session.context_view import (
    build_compaction_context_records,
    build_provider_compaction_context,
)
from agentos.session.cost_rollup import (
    normalize_event_cost_source,
)
from agentos.session.keys import (
    allows_private_memory_prompt_injection,
    canonicalize_session_key,
    is_subagent_key,
    normalize_agent_id,
)
from agentos.session.manager import TranscriptSnapshot
from agentos.session.terminal_reply import build_terminal_reply, sanitize_agent_error
from agentos.tools.types import CallerKind, InteractionMode, ToolContext
from agentos.util.bounded_registry import BoundedRegistry

# Stable user-facing envelope for LLM timeouts.
_LLM_TIMEOUT_ENVELOPE: dict[str, Any] = {
    "status": "error",
    "error_class": "llm_timeout",
    "user_message": "The model took too long to respond. Please try again.",
    "retry_allowed": True,
}
_DEFAULT_AGENT_RUNTIME_TIMEOUT_SECONDS: float = 48 * 60 * 60
_DEFAULT_LLM_REQUEST_TIMEOUT_SECONDS: float = 120.0
_DEFAULT_LLM_TIMEOUT_SECONDS: float = _DEFAULT_LLM_REQUEST_TIMEOUT_SECONDS
_ROUTER_PREV_ASSISTANT_MAX_CHARS: Final[int] = 8000
_ROUTER_HISTORY_USER_MAX_CHARS: Final[int] = 8000
_ROUTER_HISTORY_USER_MAX_TURNS: Final[int] = 4
# Newest transcript rows read for the router context before falling back to
# the whole transcript (only when the window lacks what the context needs).
_ROUTER_CONTEXT_WINDOW_ENTRIES: Final[int] = 128
_CONTEXT_SUMMARY_MARKER: Final[str] = "[Context Summary]"
# Extra-context keys whose value is fixed for the life of a session (or changes
# only on an explicit edit); rendered into the cached system prompt.
_SESSION_FROZEN_EXTRA_CONTEXT_KEYS: Final[frozenset[str]] = frozenset(
    {"Project Knowledge", "Subagent Task Protocol"}
)
_COMPACTION_SUMMARY_CONTEXT_HEADER: Final[str] = "[Compacted Session Summaries]"
_COMPACTION_SUMMARY_CONTEXT_MAX_CHARS: Final[int] = 16_000
_DEFAULT_PREFLIGHT_COMPACT_RATIO: Final[float] = 0.85
_IMAGE_GENERATION_TOOL_NAMES: Final[frozenset[str]] = frozenset({"image_generate"})
_ARTIFACT_DELIVERY_FAILURE_MARKER: Final[str] = "File delivery failed:"
_ARTIFACT_DELIVERY_TOOL_NAME: Final[str] = "publish_artifact"
_ARTIFACT_DELIVERY_FAILURE_MAX_CHARS: Final[int] = 360


def collect_invoked_skills(
    turn_segments: list[dict],
    *,
    extra_first: list[str] | None = None,
) -> list[str]:
    """Collect skill names from skill_view tool segments."""

    seen: set[str] = set()
    result: list[str] = []
    for name in extra_first or []:
        if isinstance(name, str) and name and name not in seen:
            seen.add(name)
            result.append(name)
    for segment in turn_segments:
        tool_name = segment.get("name")
        if tool_name not in {"skill_view"}:
            continue
        skill_name = (segment.get("input") or {}).get("name")
        if not isinstance(skill_name, str) or not skill_name or skill_name in seen:
            continue
        seen.add(skill_name)
        result.append(skill_name)
    return result


# Per-call-chain owner tracking for session-lock re-entry detection.
# A ContextVar is copied into child asyncio Tasks created while a turn is
# running, which matters for stream wrappers such as heartbeat_stream. Treating
# the lock id as the ownership token lets those child tasks enter without
# self-deadlocking while unrelated tasks still see their own context values.
_SESSION_LOCK_OWNER: contextvars.ContextVar[dict[int, asyncio.Task[Any]]] = contextvars.ContextVar(
    "_session_lock_owner"
)
_SESSION_LOCK_BYPASS_ONLY: contextvars.ContextVar[set[int] | None] = contextvars.ContextVar(
    "_session_lock_bypass_only",
    default=None,
)


def _compute_route_input_savings_usd(
    max_price_per_m: float,
    routed_price_per_m: float,
    input_tokens: int,
) -> float:
    """49b7e08 agentos-router savings formula: input-price delta times input tokens."""
    return round(max(0.0, (max_price_per_m - routed_price_per_m) * input_tokens / 1_000_000), 6)


@dataclass(frozen=True)
class _SavingsBaseline:
    model: str = ""
    price: PriceEntry = field(default_factory=lambda: PriceEntry(0.0, 0.0))
    cost_usd: float = 0.0


@dataclass(frozen=True)
class _ComprehensiveTurnSavings:
    pct: float = 0.0
    usd: float = 0.0
    baseline_model: str = ""
    baseline_cost_usd: float = 0.0
    actual_cost_usd: float = 0.0


def _refusal_category_from_details(stop_details: object) -> str | None:
    if not isinstance(stop_details, dict):
        return None
    category = stop_details.get("category")
    return category if isinstance(category, str) and category else None


def _non_negative_int(value: object) -> int:
    if value is None:
        return 0
    if not isinstance(value, str | bytes | bytearray | SupportsInt):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _token_cost_usd(input_tokens: float, output_tokens: float, price: PriceEntry) -> float:
    return (
        max(0.0, float(input_tokens)) * price.input_per_m / 1_000_000
        + max(0.0, float(output_tokens)) * price.output_per_m / 1_000_000
    )


def _tier_value(tier: object, key: str, default: object = None) -> object:
    if isinstance(tier, Mapping):
        return tier.get(key, default)
    return getattr(tier, key, default)


def _iter_text_tier_models(tiers: object) -> list[tuple[str, str]]:
    if not isinstance(tiers, Mapping):
        return []
    models: list[tuple[str, str]] = []
    for tier in tiers.values():
        if bool(_tier_value(tier, "image_only", False)):
            continue
        model = str(_tier_value(tier, "model", "") or "").strip()
        if model:
            provider = str(_tier_value(tier, "provider", "") or "").strip()
            models.append((provider, model))
    return models


def _provider_for_tier_model(tiers: object, model_id: str) -> str:
    for provider, model in _iter_text_tier_models(tiers):
        if model == model_id:
            return provider
    return ""


def _provider_for_tier(tiers: object, tier_name: str) -> str:
    if not isinstance(tiers, Mapping) or not tier_name:
        return ""
    return str(_tier_value(tiers.get(tier_name), "provider", "") or "").strip()


def _select_savings_baseline_model(
    tiers: object,
    baseline_input_tokens: float,
    baseline_output_tokens: float,
) -> _SavingsBaseline:
    best = _SavingsBaseline(cost_usd=-1.0)
    for provider, model in _iter_text_tier_models(tiers):
        price = lookup_price(model, provider_id=provider)
        cost_usd = _token_cost_usd(baseline_input_tokens, baseline_output_tokens, price)
        if cost_usd > best.cost_usd:
            best = _SavingsBaseline(model=model, price=price, cost_usd=cost_usd)
    if best.cost_usd < 0:
        return _SavingsBaseline()
    return best


def _short_output_savings_rate(metadata: Mapping[str, Any], estimated_pct: float) -> float:
    prompt_policy = str(metadata.get("prompt_policy") or "").strip().upper()
    active = prompt_policy == "P0" or bool(metadata.get("short_reply_active"))
    if not active:
        return 0.0
    try:
        rate = float(estimated_pct)
    except (TypeError, ValueError):
        return 0.0
    if rate <= 0.0 or rate >= 1.0:
        return 0.0
    return rate


def _restored_output_side_tokens(
    actual_output_side_tokens: int,
    metadata: Mapping[str, Any],
    estimated_output_savings_pct: float,
) -> float:
    rate = _short_output_savings_rate(metadata, estimated_output_savings_pct)
    if rate <= 0.0 or actual_output_side_tokens <= 0:
        return float(actual_output_side_tokens)
    return actual_output_side_tokens / (1.0 - rate)


def _compute_comprehensive_turn_savings(
    event: DoneEvent,
    metadata: Mapping[str, Any],
    tiers: object,
    routed_model: str,
    *,
    routed_provider: str = "",
    routed_tier: str = "",
    estimated_output_savings_pct: float = 0.03,
) -> _ComprehensiveTurnSavings:
    """Estimate per-turn savings from token counts and model prices only."""
    actual_input_tokens = _non_negative_int(event.input_tokens)
    actual_output_side_tokens = _non_negative_int(event.output_tokens) + _non_negative_int(
        event.reasoning_tokens
    )
    tool_tokens_saved = _non_negative_int(metadata.get("tool_projection_tokens_saved"))
    baseline_input_tokens = actual_input_tokens + tool_tokens_saved
    baseline_output_tokens = _restored_output_side_tokens(
        actual_output_side_tokens,
        metadata,
        estimated_output_savings_pct,
    )

    baseline = _select_savings_baseline_model(
        tiers,
        baseline_input_tokens,
        baseline_output_tokens,
    )
    actual_model = routed_model or event.model
    provider = (
        routed_provider
        or _provider_for_tier(tiers, routed_tier)
        or _provider_for_tier_model(tiers, actual_model)
    )
    routed_price = lookup_price(actual_model, provider_id=provider)
    actual_cost_usd = _token_cost_usd(
        actual_input_tokens,
        actual_output_side_tokens,
        routed_price,
    )

    if baseline.cost_usd <= 0.0:
        return _ComprehensiveTurnSavings(
            baseline_model=baseline.model,
            baseline_cost_usd=max(0.0, baseline.cost_usd),
            actual_cost_usd=actual_cost_usd,
        )

    savings_usd = round(max(0.0, baseline.cost_usd - actual_cost_usd), 6)
    savings_pct = 0.0
    if savings_usd > 0.0:
        savings_pct = round(max(0.0, min(99.9, (savings_usd / baseline.cost_usd) * 100)), 1)

    return _ComprehensiveTurnSavings(
        pct=savings_pct,
        usd=savings_usd,
        baseline_model=baseline.model,
        baseline_cost_usd=baseline.cost_usd,
        actual_cost_usd=actual_cost_usd,
    )


def _normalize_capture_kind(value: str) -> str:
    return value.strip().lower().replace("-", "_").replace(".", "_").replace(":", "_")


class MemorySourceUnreadableError(Exception):
    """Curated memory exists on disk but could not be read this attempt.

    Distinct from "there is no memory": callers must not freeze an empty
    snapshot on this, or a transient read failure blinds the agent for the
    rest of the session.
    """


# Curated-memory write tools. A turn that calls one of these has already done
# the thing the nudge would ask for, so it resets the counter instead.
_MEMORY_WRITE_TOOL_NAMES: Final[frozenset[str]] = frozenset({"memory", "memory_save"})

# The only tools the background review may see. It runs after the user's turn
# with nobody watching, so anything outside the curated-memory surface can
# only misfire: a review that reached for ``apply_patch`` or ``write_file``
# tripped the out-of-workspace gate and put an approval prompt in front of
# the user for a write they never asked for.
_MEMORY_REVIEW_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    {"memory", "memory_save", "memory_get", "memory_search", "memory_delete"}
)

# Cap on retained nudge counters. Far above any realistic concurrent-session
# count, low enough that the dict cannot grow without bound in a gateway that
# runs for weeks.
_MAX_NUDGE_COUNTERS: Final[int] = 2048

# Prompt for the periodic memory review. Adapted from hermes-agent
# (MIT, © 2025 Nous Research). Two properties matter and are easy to lose:
# it names what is worth keeping (durable facts about the user, not task
# chatter), and it gives an explicit way to decline, so a review with nothing
# to save stops instead of inventing an entry to justify the call.
_MEMORY_REVIEW_PROMPT: Final[str] = (
    "Review the conversation above and consider saving to memory.\n\n"
    "Focus on:\n"
    "1. Has the user revealed durable things about themselves — their role, "
    "preferences, working style, or context that will still matter in a "
    "later session?\n"
    "2. Has the user stated expectations about how you should work — "
    "conventions to follow, things to avoid, corrections they had to repeat?\n\n"
    "Save anything that stands out with the memory tool, consolidating "
    "existing entries rather than duplicating them. Skip anything one-off, "
    "already saved, or specific to the current task.\n\n"
    "If nothing is worth saving, reply exactly 'Nothing to save.' and stop."
)

# How many review writes to name in one log line. The point is to show the
# user what landed, not to reproduce the store.
# How many intervals a run of self-directed writes may defer the review before
# it runs anyway. Without a ceiling, an agent that saves on every turn is never
# reviewed -- which is the state the nudge was found in.
_NUDGE_MAX_DEFERRAL_INTERVALS: Final[int] = 2

_MEMORY_REVIEW_LOG_MAX_ENTRIES: Final[int] = 5
_MEMORY_REVIEW_LOG_ENTRY_CHARS: Final[int] = 120


def _summarize_memory_write(arguments: Any) -> str:
    """Render one memory tool call as ``target/action: content``.

    Batches collapse to their operation count: naming every op would push a
    single log line past anything readable, and the count is enough to tell a
    consolidation from a one-off save.
    """
    if not isinstance(arguments, dict):
        return "write"
    target = str(arguments.get("target") or "memory")
    operations = arguments.get("operations")
    if isinstance(operations, list) and operations:
        actions = ",".join(
            sorted({str(op.get("action", "?")) for op in operations if isinstance(op, dict)})
        )
        return f"{target}/batch[{len(operations)} ops: {actions or '?'}]"
    action = str(arguments.get("action") or "?")
    content = str(arguments.get("content") or arguments.get("old_text") or "").strip()
    if len(content) > _MEMORY_REVIEW_LOG_ENTRY_CHARS:
        content = content[:_MEMORY_REVIEW_LOG_ENTRY_CHARS] + "…"
    return f"{target}/{action}: {content}" if content else f"{target}/{action}"


# Boot-path initialization of the safety baseline. All four submodules
# are imported here so tool dispatch and ingress guards can consult them
# without late imports.
#
# The tuple pins the imports to module scope so the linter does not drop them
# as "unused" — dispatch paths reach these modules via attribute lookup at
# call time, not through named references in this file. Keeping the reference
# explicit makes the load-time invariant legible to readers.
_SAFETY_MODULES: Final[tuple[Any, ...]] = (
    injection_guard,
    tool_tiers,
    permission_matrix,
    sandbox,
)


if TYPE_CHECKING:
    from agentos.observability.turn_call_log import TurnCallLogger
    from agentos.provider.protocol import LLMProvider

log = structlog.get_logger(__name__)


def _strip_context_summary_marker(content: str) -> str:
    """Return summary text from a legacy transcript summary marker."""
    if content.startswith(_CONTEXT_SUMMARY_MARKER):
        return content[len(_CONTEXT_SUMMARY_MARKER) :].lstrip("\r\n")
    return content


def _format_compaction_summary_context(summary_texts: list[str]) -> str | None:
    """Render durable summaries as request-scoped context, newest context preserved."""
    deduped: list[str] = []
    seen: set[str] = set()
    for raw in summary_texts:
        text = raw.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        deduped.append(text)
    if not deduped:
        return None

    blocks = [f"[Summary {idx}]\n{text}" for idx, text in enumerate(deduped, start=1)]
    rendered = f"{_COMPACTION_SUMMARY_CONTEXT_HEADER}\n" + "\n\n".join(blocks)
    if len(rendered) <= _COMPACTION_SUMMARY_CONTEXT_MAX_CHARS:
        return rendered
    tail_budget = (
        _COMPACTION_SUMMARY_CONTEXT_MAX_CHARS - len(_COMPACTION_SUMMARY_CONTEXT_HEADER) - 80
    )
    tail_budget = max(1000, tail_budget)
    return (
        f"{_COMPACTION_SUMMARY_CONTEXT_HEADER}\n"
        "[Earlier compaction summary context truncated to fit request budget.]\n"
        f"{rendered[-tail_budget:]}"
    )


def _prepend_request_context_prompt(
    existing_request_context: str | None,
    prepended_context: str | None,
) -> str | None:
    """Place session summary context before volatile per-turn context."""
    if not prepended_context or not prepended_context.strip():
        return existing_request_context
    if not existing_request_context or not existing_request_context.strip():
        return prepended_context.strip()
    return f"{prepended_context.strip()}\n\n{existing_request_context.strip()}"


_MAX_TOOL_RESULT_CHARS = 2000
_MAX_TOOL_RESULT_METADATA_VALUE_CHARS = 256
_MAX_PERSISTED_TOOL_ARGUMENT_FIELD_CHARS = 4096
_PERSISTED_TOOL_ARGUMENT_PREVIEW_CHARS = 512
_PERSISTED_TOOL_ARGUMENT_PROJECTION_PREFIX = "[historical_tool_argument_omitted]\n"
_TOOL_ARGUMENT_PAYLOAD_FIELDS: Final[dict[str, frozenset[str]]] = {
    "write_file": frozenset({"content"}),
    "edit_file": frozenset({"old_text", "new_text"}),
}
_TOOL_RESULT_METADATA_KEYS: Final[frozenset[str]] = frozenset(
    {
        "provider",
        "query",
        "fallback_from",
        "error",
        "error_class",
        "error_kind",
    }
)
_SENTINELS: Final[frozenset[str]] = frozenset({"NO_REPLY", "HEARTBEAT_OK"})
_HEARTBEAT_ACK_TOKEN: Final[str] = "HEARTBEAT_OK"
# Markdown a model puts around a bare token. The system prompt itself shows the
# sentinels as code spans, so `NO_REPLY` and **HEARTBEAT_OK** are ordinary replies.
_SENTINEL_WRAPPERS: Final[str] = "`*_~"
# Thinking-level sources the router owns; effort-by-tier may cap these.
_EFFORT_BY_TIER_SOURCES: frozenset[str] = frozenset({"tier_default", "controller"})

_THINKING_ALIASES: Final[dict[str, str]] = {
    "x-high": "xhigh",
    "x_high": "xhigh",
    "extra-high": "xhigh",
    "extra_high": "xhigh",
    "extra high": "xhigh",
    "highest": "high",
    "max": "high",
    "on": "low",
    "true": "medium",
    "none": "off",
    "false": "off",
}


def _truncate_json_string(value: str, max_chars: int) -> str:
    if max_chars <= 0 or len(value) <= max_chars:
        return value
    if max_chars == 1:
        return "…"
    return value[: max_chars - 1] + "…"


def _compact_json_for_tool_result_preview(
    value: Any,
    *,
    max_string_chars: int,
    max_list_items: int,
) -> Any:
    """Return a JSON-serializable preview that keeps structure bounded."""

    if isinstance(value, str):
        return _truncate_json_string(value, max_string_chars)
    if isinstance(value, list):
        return [
            _compact_json_for_tool_result_preview(
                item,
                max_string_chars=max_string_chars,
                max_list_items=max_list_items,
            )
            for item in value[:max_list_items]
        ]
    if isinstance(value, dict):
        return {
            str(key): _compact_json_for_tool_result_preview(
                item,
                max_string_chars=max_string_chars,
                max_list_items=max_list_items,
            )
            for key, item in value.items()
        }
    return value


def _bounded_tool_result_metadata(
    parsed: Mapping[str, Any],
) -> dict[str, str | int | float | bool | None]:
    """Return bounded scalar metadata safe to store beside capped result text."""

    metadata: dict[str, str | int | float | bool | None] = {}
    for key in _TOOL_RESULT_METADATA_KEYS:
        if key not in parsed:
            continue
        value = parsed[key]
        if isinstance(value, str):
            metadata[key] = _truncate_json_string(
                value,
                _MAX_TOOL_RESULT_METADATA_VALUE_CHARS,
            )
        elif isinstance(value, int | float | bool) or value is None:
            metadata[key] = value
    return metadata


def _json_tool_result_preview(parsed: Any, original_chars: int, max_chars: int) -> str:
    """Build a bounded, valid-JSON preview for persisted transcript display.

    Tool results are often structured JSON consumed by the web UI. A plain
    prefix slice can turn them into invalid JSON and hide top-level metadata
    such as the active search provider. This helper prefers a valid JSON
    preview with explicit truncation metadata while keeping the historical
    transcript size cap.
    """

    if isinstance(parsed, dict):
        base: dict[str, Any] = dict(parsed)
    else:
        base = {"value": parsed}
    base["result_truncated"] = True
    base["result_original_chars"] = original_chars

    for max_list_items in (5, 3, 2, 1, 0):
        for max_string_chars in (512, 256, 128, 64, 32, 16):
            compacted = _compact_json_for_tool_result_preview(
                base,
                max_string_chars=max_string_chars,
                max_list_items=max_list_items,
            )
            rendered = json.dumps(compacted, ensure_ascii=False, indent=2)
            if len(rendered) <= max_chars:
                return rendered

    fallback: dict[str, Any] = {
        "result_truncated": True,
        "result_original_chars": original_chars,
    }
    if isinstance(parsed, dict):
        fallback.update(_bounded_tool_result_metadata(parsed))
    rendered = json.dumps(fallback, ensure_ascii=False, indent=2)
    if len(rendered) <= max_chars:
        return rendered
    return json.dumps({"result_truncated": True}, ensure_ascii=False)


def _tool_argument_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _persisted_tool_argument_projection(
    *,
    tool_name: str,
    tool_use_id: str,
    field: str,
    value_text: str,
    path_hint: Any,
) -> str:
    lines = [
        _PERSISTED_TOOL_ARGUMENT_PROJECTION_PREFIX.rstrip("\n"),
        f"tool: {tool_name}",
        f"tool_use_id: {tool_use_id}",
        f"field: {field}",
        f"original_chars: {len(value_text)}",
        f"sha256: {hashlib.sha256(value_text.encode('utf-8')).hexdigest()}",
    ]
    if isinstance(path_hint, str) and path_hint.strip():
        lines.append(f"path: {path_hint.strip()}")
    lines.extend(
        [
            "head:",
            value_text[:_PERSISTED_TOOL_ARGUMENT_PREVIEW_CHARS],
            "tail:",
            value_text[-_PERSISTED_TOOL_ARGUMENT_PREVIEW_CHARS:],
        ]
    )
    return "\n".join(lines)


def _persisted_tool_use_input(
    tool_name: str,
    tool_use_id: str,
    arguments: dict[str, Any],
    *,
    max_field_chars: int = _MAX_PERSISTED_TOOL_ARGUMENT_FIELD_CHARS,
) -> dict[str, Any]:
    """Create the transcript-safe input for persisted file-writing tool calls."""

    payload_fields = _TOOL_ARGUMENT_PAYLOAD_FIELDS.get(tool_name)
    if not payload_fields:
        return arguments

    projected = dict(arguments)
    changed = False
    path_hint = projected.get("path")
    for argument_name in payload_fields:
        if argument_name not in projected:
            continue
        value_text = _tool_argument_text(projected[argument_name])
        if len(value_text) <= max_field_chars:
            continue
        projected[argument_name] = _persisted_tool_argument_projection(
            tool_name=tool_name,
            tool_use_id=tool_use_id,
            field=argument_name,
            value_text=value_text,
            path_hint=path_hint,
        )
        changed = True

    return projected if changed else arguments


def _persisted_tool_result_segment(
    event: ToolResultEvent,
    *,
    max_chars: int | None = None,
) -> dict[str, Any]:
    """Create the transcript `tool_result` segment for a streamed event.

    The ceiling comes from the tool unless the caller names one. Truncating here
    cannot be undone by anything downstream — the request builder compacts from
    whatever this wrote, not from the original — so a tool whose result is
    instructions rather than output raises its own ceiling and keeps the loss at
    the layer that has a budget to defend.
    """

    if max_chars is None:
        max_chars = persisted_result_max_chars(
            event.tool_name,
            default=_MAX_TOOL_RESULT_CHARS,
        )

    result = event.result
    segment: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": event.tool_use_id,
        "name": event.tool_name,
        "result": result,
        "is_error": event.is_error,
    }
    if event.execution_status is not None:
        segment["execution_status"] = normalize_execution_status(event.execution_status)
    if len(result) <= max_chars:
        return segment

    segment["result_truncated"] = True
    segment["result_original_chars"] = len(result)
    if "execution_status" in segment:
        segment["execution_status"] = mark_execution_status_truncated(segment["execution_status"])
    try:
        parsed = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        segment["result"] = result[:max_chars]
        return segment

    if isinstance(parsed, dict):
        segment.update(_bounded_tool_result_metadata(parsed))
    segment["result"] = _json_tool_result_preview(parsed, len(result), max_chars)
    return segment


def _artifact_delivery_failure_summary(event: ToolResultEvent) -> str | None:
    if event.tool_name != _ARTIFACT_DELIVERY_TOOL_NAME or not event.is_error:
        return None
    raw = event.result.strip()
    summary = raw
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        candidate = (
            parsed.get("user_message")
            or parsed.get("message")
            or parsed.get("error")
            or parsed.get("error_class")
        )
        if isinstance(candidate, str) and candidate.strip():
            summary = candidate.strip()
    summary = " ".join(summary.split())
    if len(summary) > _ARTIFACT_DELIVERY_FAILURE_MAX_CHARS:
        summary = summary[: _ARTIFACT_DELIVERY_FAILURE_MAX_CHARS - 3].rstrip() + "..."
    return summary or "publish_artifact failed"


def _artifact_delivery_failure_notice(*, partial: bool = False) -> str:
    if partial:
        return (
            f"{_ARTIFACT_DELIVERY_FAILURE_MARKER} some generated files were attached, "
            "but at least one file could not be attached. Ask me to resend the "
            "missing file after I correct the generated file path."
        )
    return (
        f"{_ARTIFACT_DELIVERY_FAILURE_MARKER} no downloadable file was attached "
        "to this response. Ask me to resend the file after I correct the generated "
        "file path."
    )


def _cancelled_partial_response_text(
    partial_text: str,
    artifacts: list[dict[str, Any]],
) -> str:
    partial_text = partial_text.rstrip()
    if artifacts:
        names = [
            str(item.get("name") or item.get("filename") or "").strip()
            for item in artifacts
            if isinstance(item, dict)
        ]
        named = [name for name in names if name]
        delivered = (
            "The generated file was delivered: " + ", ".join(named) + "."
            if named
            else "The generated file was delivered."
        )
        return f"{partial_text}\n\n{delivered}" if partial_text else delivered
    return f"{partial_text}\n\n[interrupted]" if partial_text else "[interrupted]"


def _should_add_artifact_delivery_failure_notice(
    *,
    failure_summaries: list[str],
    turn_artifacts: list[dict[str, Any]],
    final_text: str,
) -> bool:
    if not failure_summaries:
        return False
    return _ARTIFACT_DELIVERY_FAILURE_MARKER not in final_text


_SUBAGENT_TASK_PROTOCOL: Final[str] = (
    "You are a spawned subagent. Execute only the delegated task and return "
    "a compact result for the parent agent to use. Prefer a direct answer; "
    "call tools only when the task explicitly requires external state, files, "
    "network data, or tool output. If the delegated task asks you to reply with "
    "an exact phrase, only reply, output a sentinel token, or avoid explanation, "
    "Do not call tools and return exactly that requested text. Do not treat "
    "uppercase sentinel-like strings as shell commands, filenames, or config keys."
)


def _classify_provider_event(provider_name: str, event: ProviderErrorEvent) -> ProviderFailureKind:
    return classify_provider_error(
        provider_name=provider_name,
        status_code=int(event.code) if str(event.code).isdigit() else None,
        raw_code=event.code,
        message=event.message,
    )


def _kind_uses_selector_fallback(kind: ProviderFailureKind) -> bool:
    return decide_recovery_action(kind) in {
        ProviderRecoveryAction.FALLBACK_PROVIDER,
        ProviderRecoveryAction.RETRY_THEN_FALLBACK,
    }


def _should_use_selector_fallback(provider_name: str, event: ProviderErrorEvent) -> bool:
    return _kind_uses_selector_fallback(_classify_provider_event(provider_name, event))


def _unwrap_sentinel(text: str) -> str:
    """Return *text* without the Markdown and closing full stop around it.

    A code span around NO_REPLY, ``**HEARTBEAT_OK**`` and ``NO_REPLY.`` all mean
    the bare token. Compared as written, each went out to the channel verbatim.
    """
    unwrapped = text.strip().strip(_SENTINEL_WRAPPERS)
    return unwrapped.rstrip(".!").strip(_SENTINEL_WRAPPERS).strip()


def _normalize_heartbeat_text(
    text: str,
    *,
    run_kind: str,
    heartbeat_ack_max_chars: int,
) -> str:
    stripped = _unwrap_sentinel(text)
    if stripped in _SENTINELS:
        log.debug("turn_runner.sentinel_suppressed", sentinel=stripped)
        return ""
    if run_kind != "heartbeat":
        return text

    def _suppressed(payload: str) -> bool:
        return len(_unwrap_sentinel(payload)) <= heartbeat_ack_max_chars

    if stripped.startswith(_HEARTBEAT_ACK_TOKEN):
        remainder = stripped[len(_HEARTBEAT_ACK_TOKEN) :].strip()
        if _suppressed(remainder):
            return ""

    if stripped.endswith(_HEARTBEAT_ACK_TOKEN):
        remainder = stripped[: -len(_HEARTBEAT_ACK_TOKEN)].strip()
        if _suppressed(remainder):
            return ""

    return text


def _drop_unpaired_tool_use_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    paired_ids = {
        segment.get("tool_use_id")
        for segment in segments
        if isinstance(segment, dict) and segment.get("type") == "tool_result"
    }
    return [
        segment
        for segment in segments
        if not (
            isinstance(segment, dict)
            and segment.get("type") == "tool_use"
            and segment.get("tool_use_id") not in paired_ids
        )
    ]


def _selector_model_name(provider: Any, selector: Any) -> str:
    m = getattr(provider, "_model", None) or getattr(provider, "model", None)
    if not m:
        cfg = getattr(selector, "current_config", None)
        m = getattr(cfg, "model", "") if cfg is not None else ""
    return str(m or "")


def _selector_has_fallback(selector: Any) -> bool:
    fn = getattr(selector, "has_fallback", None)
    if callable(fn):
        try:
            return bool(fn())
        except Exception:
            return False
    return False


class _SelectorFallbackProvider:
    """Provider wrapper that switches to selector fallback on pre-content errors.

    Streaming rule: every event is forwarded as soon as the stream has produced
    one that proves the provider accepted the request — a non-empty text or
    thinking delta, a tool-use event, or a provider heartbeat. Only a provider
    error that arrives before any *content* event (text, thinking, tool use)
    triggers the transparent failover to the selector's next provider; once
    content has been forwarded, later errors pass through and the agent loop's
    own retry policy handles them. Heartbeats are forwarded immediately but are
    not content, so an error after heartbeats alone may still fail over.

    Also feeds the selector's circuit breaker: provider-health failures are
    counted so a sustained outage opens the breaker, and the first forwarded
    content event (or a clean ``done``) closes it again.

    A rate limit / overload that carries a short ``Retry-After`` hint (at most
    ``retry_after_cap_seconds``) is *not* failed over here: the error is passed
    through untouched so the agent loop can wait out the hint and retry the
    same model, keeping its prompt cache. The agent asks this wrapper to fail
    over (``fallback_after_invalid_response``) only once its retries are spent.
    """

    def __init__(
        self,
        provider: Any,
        selector: Any,
        *,
        retry_after_cap_seconds: float = DEFAULT_RETRY_AFTER_CAP_SECONDS,
    ) -> None:
        self._provider = provider
        self._selector = selector
        self._retry_after_cap_seconds = float(retry_after_cap_seconds)

    def set_retry_after_cap_seconds(self, seconds: float) -> None:
        """Adopt the agent's ``rate_limit_retry_after_cap_seconds``."""
        self._retry_after_cap_seconds = float(seconds)

    def _honoured_retry_after(
        self, kind: ProviderFailureKind, event: ProviderErrorEvent
    ) -> float | None:
        return honoured_retry_after(
            kind,
            getattr(event, "retry_after_seconds", None),
            self._retry_after_cap_seconds,
        )

    def _note_failure(self, kind: ProviderFailureKind, event: ProviderErrorEvent) -> bool:
        """Record ``event`` against the breaker; True when it counted as a trip."""
        retry_after = getattr(event, "retry_after_seconds", None)
        self._record_failure(kind, event.message, retry_after)
        return trips_breaker(
            kind,
            retry_after,
            retry_after_cap_seconds=self._retry_after_cap_seconds,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)

    @property
    def provider_name(self) -> str:
        return getattr(self._provider, "provider_name", "")

    def _record_failure(
        self,
        kind: ProviderFailureKind,
        reason: str,
        retry_after_seconds: float | None = None,
    ) -> None:
        recorder = getattr(self._selector, "record_provider_failure", None)
        if recorder is None:
            return
        try:
            if retry_after_seconds is None:
                recorder(kind, reason)
                return
            try:
                recorder(
                    kind,
                    reason,
                    retry_after_seconds=retry_after_seconds,
                    retry_after_cap_seconds=self._retry_after_cap_seconds,
                )
            except TypeError:
                # A selector stand-in that predates the hint (tests, plugins):
                # apply the exemption here and record the old way.
                if trips_breaker(
                    kind,
                    retry_after_seconds,
                    retry_after_cap_seconds=self._retry_after_cap_seconds,
                ):
                    recorder(kind, reason)
        except Exception:  # pragma: no cover - breaker must never break a turn
            log.debug("selector_fallback.breaker_failure_record_failed", exc_info=True)

    def _record_success(self) -> None:
        recorder = getattr(self._selector, "record_provider_success", None)
        if recorder is None:
            return
        try:
            recorder()
        except Exception:  # pragma: no cover - breaker must never break a turn
            log.debug("selector_fallback.breaker_success_record_failed", exc_info=True)

    def fallback_after_invalid_response(self, reason: str) -> bool:
        try:
            self._provider = self._selector.next_fallback_after_failure(RuntimeError(reason))
        except Exception:
            return False
        return True

    def chat(
        self,
        messages: list[Any],
        tools: Any = None,
        config: Any = None,
    ) -> AsyncIterator[Any]:
        return self._chat(messages, tools=tools, config=config)

    async def _chat(
        self,
        messages: list[Any],
        tools: Any = None,
        config: Any = None,
    ) -> AsyncIterator[Any]:
        emitted_content = False
        accepted = False
        saw_provider_error = False
        recorded_success = False
        recorded_failure = False
        # Only events that prove nothing (empty deltas, unknown kinds) seen before
        # the provider has shown any sign of life are held back, so a failover
        # does not leak a dead attempt's noise into the fallback's stream.
        pre_accept_buffer: list[Any] = []

        def drain_pre_accept_buffer() -> list[Any]:
            drained = list(pre_accept_buffer)
            pre_accept_buffer.clear()
            return drained

        async for event in self._provider.chat(messages, tools=tools, config=config):
            if emitted_content:
                yield event
                continue

            if isinstance(event, ProviderErrorEvent):
                saw_provider_error = True
                kind = _classify_provider_event(self.provider_name, event)
                if not recorded_failure:
                    # One vote per stream: a provider that emits several error
                    # events for one request is still a single failed turn.
                    recorded_failure = self._note_failure(kind, event)
                if self._honoured_retry_after(kind, event) is not None:
                    # Short Retry-After: hand it to the agent loop, which waits
                    # it out and retries this same model.
                    for buffered_event in drain_pre_accept_buffer():
                        yield buffered_event
                    yield event
                    continue
                if _kind_uses_selector_fallback(kind):
                    failed_model = _selector_model_name(self._provider, self._selector)
                    try:
                        self._provider = self._selector.next_fallback_after_failure(
                            RuntimeError(event.message)
                        )
                    except Exception:
                        for buffered_event in drain_pre_accept_buffer():
                            yield buffered_event
                        yield event
                        return
                    fallback_model = _selector_model_name(self._provider, self._selector)
                    yield ProviderHeartbeatEvent(
                        phase="llm_fallback",
                        message=(
                            f"Model {failed_model or 'primary'} failed; "
                            f"automatically switching to fallback model {fallback_model}."
                        ),
                    )
                    async for fallback_event in self._fallback_chat(messages, tools, config):
                        yield fallback_event
                    return
                for buffered_event in drain_pre_accept_buffer():
                    yield buffered_event
                yield event
                continue

            if _is_provider_content_event(event):
                for buffered_event in drain_pre_accept_buffer():
                    yield buffered_event
                accepted = True
                emitted_content = True
                if not recorded_success:
                    recorded_success = True
                    self._record_success()
                yield event
                continue

            if getattr(event, "kind", "") == "done":
                for buffered_event in drain_pre_accept_buffer():
                    yield buffered_event
                # A clean stream proves the provider is healthy — including a
                # turn that never emits any content at all.
                if not saw_provider_error and not recorded_success:
                    recorded_success = True
                    self._record_success()
                yield event
                continue

            if isinstance(event, ProviderHeartbeatEvent):
                # Liveness, not content: forward at once so idle watchdogs see
                # it, but keep the transparent failover available.
                for buffered_event in drain_pre_accept_buffer():
                    yield buffered_event
                accepted = True
                yield event
                continue

            if accepted:
                yield event
                continue

            pre_accept_buffer.append(event)

        for buffered_event in drain_pre_accept_buffer():
            yield buffered_event

    async def _fallback_chat(
        self,
        messages: list[Any],
        tools: Any,
        config: Any,
    ) -> AsyncIterator[Any]:
        """Stream the post-failover provider, recording its own breaker outcome.

        Same rule as :meth:`_chat`: events are forwarded as they arrive, and a
        further failover only happens while no content has been forwarded yet.
        """
        emitted_content = False
        saw_provider_error = False
        recorded_success = False
        recorded_failure = False
        async for event in self._provider.chat(messages, tools=tools, config=config):
            if isinstance(event, ProviderErrorEvent):
                saw_provider_error = True
                # Classify every error: a later error must not be judged by an
                # earlier one's kind.
                kind = _classify_provider_event(self.provider_name, event)
                if not recorded_failure:
                    recorded_failure = self._note_failure(kind, event)
                if (
                    not emitted_content
                    and self._honoured_retry_after(kind, event) is None
                    and _kind_uses_selector_fallback(kind)
                    and _selector_has_fallback(self._selector)
                ):
                    failed_model = _selector_model_name(self._provider, self._selector)
                    try:
                        self._provider = self._selector.next_fallback_after_failure(
                            RuntimeError(event.message)
                        )
                        fallback_model = _selector_model_name(self._provider, self._selector)
                        yield ProviderHeartbeatEvent(
                            phase="llm_fallback",
                            message=(
                                f"Model {failed_model or 'fallback'} failed; "
                                f"automatically switching to fallback model {fallback_model}."
                            ),
                        )
                        async for next_ev in self._fallback_chat(messages, tools, config):
                            yield next_ev
                        return
                    except Exception:
                        pass
            elif _is_provider_content_event(event):
                emitted_content = True
                if not recorded_success:
                    recorded_success = True
                    self._record_success()
            elif (
                not recorded_success
                and getattr(event, "kind", "") == "done"
                and not saw_provider_error
            ):
                recorded_success = True
                self._record_success()
            yield event

    async def list_models(self) -> list[Any]:
        return list(await self._provider.list_models())


_PROVIDER_TOOL_USE_EVENT_KINDS: Final = frozenset(
    {"tool_use_start", "tool_use_delta", "tool_use_end"}
)


def _is_provider_content_event(event: Any) -> bool:
    """Return True for an event that commits the stream to this provider.

    Non-empty text or thinking deltas and any tool-use event count; heartbeats,
    errors, ``done`` and empty deltas do not.
    """
    kind = getattr(event, "kind", "")
    if kind in ("text_delta", "thinking_delta"):
        return bool(getattr(event, "text", ""))
    return kind in _PROVIDER_TOOL_USE_EVENT_KINDS


@dataclass
class MemorySnapshot:
    """Frozen memory content for stable system prompt prefixes."""

    memory_md: str | None = None
    daily_notes: dict[str, str] = field(default_factory=dict)


@dataclass
class BootstrapSnapshot:
    """Frozen workspace bootstrap files for stable per-session prompt prefixes."""

    workspace_files: dict[str, str] = field(default_factory=dict)
    report: list[BootstrapFileReport] = field(default_factory=list)


_PDF_ATTACHMENT_TEXT_LIMIT = 200_000
_TEXT_ATTACHMENT_TEXT_LIMIT = 200_000
_PREVIEW_ONLY_TEXT_ATTACHMENT_CHARS = 4_000
_PREVIEW_ONLY_TEXT_ATTACHMENT_LINES = 80

_XML_ATTR_ESCAPES = {
    "<": "&lt;",
    ">": "&gt;",
    "&": "&amp;",
    '"': "&quot;",
    "'": "&apos;",
}


def _xml_escape_attr(value: str) -> str:
    """XML-escape characters that would break an HTML/XML attribute value.

    Matches the file-context wrapper escaping contract.
    """

    return "".join(_XML_ATTR_ESCAPES.get(ch, ch) for ch in value)


def _sanitize_attachment_filename(value: Any, fallback: str = "attachment") -> str:
    """Strip newlines/tabs and trim; fall back if the result is empty."""

    if not isinstance(value, str):
        return fallback
    cleaned = value.replace("\r", " ").replace("\n", " ").replace("\t", " ").strip()
    return cleaned or fallback


def _escape_file_block_content(value: str) -> str:
    """Escape literal ``</file>`` and ``<file `` substrings inside payloads.

    Without this, a user-supplied CSV / markdown body containing the wrapper
    sentinel could be mis-parsed by the model as the boundary of a *different*
    attachment, enabling prompt-injection. The replacement is XML-entity
    style so the payload remains human-readable in the prompt.
    """

    import re as _re

    # Order matters: do the close-tag pattern first so we don't double-escape
    # the prefix it shares with the open-tag pattern.
    out = _re.sub(r"<\s*/\s*file\s*>", "&lt;/file&gt;", value, flags=_re.IGNORECASE)
    out = _re.sub(r"<\s*file\b", "&lt;file", out, flags=_re.IGNORECASE)
    return out


def _render_file_context_block(filename: str, mime: str, content: str) -> str:
    """Render a ``<file name="…" mime="…">\\n<content>\\n</file>`` envelope."""

    safe_name = _xml_escape_attr(_sanitize_attachment_filename(filename))
    safe_mime = _xml_escape_attr(mime)
    safe_content = _escape_file_block_content(content)
    return f'<file name="{safe_name}" mime="{safe_mime}">\n{safe_content}\n</file>'


def _truncate_attachment_text(text: str, *, limit: int = _PDF_ATTACHMENT_TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[attachment text truncated: {len(text)} chars total]"


def _preview_attachment_text(
    text: str,
    *,
    char_limit: int = _PREVIEW_ONLY_TEXT_ATTACHMENT_CHARS,
    line_limit: int = _PREVIEW_ONLY_TEXT_ATTACHMENT_LINES,
) -> tuple[str, bool]:
    lines = text.splitlines(keepends=True)
    preview = "".join(lines[:line_limit])
    truncated = len(lines) > line_limit
    if len(preview) > char_limit:
        preview = preview[:char_limit]
        truncated = True
    elif len(text) > len(preview):
        truncated = True
    return preview, truncated


def _attachment_ref_material_path(
    attachment: dict[str, Any],
    *,
    media_root: Path | None,
) -> str | None:
    path = attachment.get("_material_path")
    if isinstance(path, str) and path:
        return path
    if media_root is None or not is_attachment_ref(attachment):
        return None
    scope = attachment.get("scope")
    sha = attachment.get("sha256") or attachment.get("material_id")
    if not isinstance(scope, str) or not isinstance(sha, str):
        return None
    try:
        return str(transcript_material_path(media_root, scope, sha))
    except ValueError:
        return None


def _render_preview_only_attachment_text(
    attachment: dict[str, Any],
    *,
    filename: str,
    mime: str,
    raw_bytes: bytes,
    media_root: Path | None,
) -> str:
    try:
        decoded = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return "[attachment unavailable: declared text content is not valid UTF-8]"

    preview, truncated = _preview_attachment_text(decoded)
    material_path = _attachment_ref_material_path(attachment, media_root=media_root)
    estimated_tokens = attachment.get("_material_estimated_tokens")
    estimated_line = (
        f"estimated_tokens: {estimated_tokens}"
        if isinstance(estimated_tokens, int)
        else "estimated_tokens: unknown"
    )
    path_line = f"path: {material_path}" if material_path else "path: unavailable"
    read_hint = (
        f'read_full: use read_file(path="{material_path}", offset=1, limit=200) '
        "and adjust offset/limit as needed."
        if material_path
        else "read_full: material path unavailable."
    )
    truncation = (
        f"\n\n[attachment preview truncated: {len(decoded)} chars total]" if truncated else ""
    )
    return (
        "[large text attachment materialized]\n"
        f"name: {filename}\n"
        f"mime: {mime}\n"
        f"size_bytes: {len(raw_bytes)}\n"
        f"{estimated_line}\n"
        f"{path_line}\n"
        f"{read_hint}\n\n"
        "preview:\n"
        f"{preview}"
        f"{truncation}"
    )


def _extract_pdf_attachment_text(raw_bytes: bytes, filename: str) -> str:
    """Extract text from a PDF attachment before it reaches any provider.

    PDFs are converted into plain text context so provider-specific document
    block handling cannot silently drop files that an adapter does not know how
    to encode.
    """

    import io

    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise ValueError("PDF text extraction requires pdfplumber") from exc

    try:
        page_texts: list[str] = []
        with pdfplumber.open(io.BytesIO(raw_bytes)) as doc:
            for index, page in enumerate(doc.pages, start=1):
                page_text = page.extract_text() or ""
                if page_text.strip():
                    page_texts.append(f"--- Page {index} ---\n{page_text}")
    except Exception as exc:  # noqa: BLE001 - pdfplumber raises several parser errors
        raise ValueError(f"PDF attachment {filename!r} could not be read: {exc}") from exc

    extracted = "\n\n".join(page_texts).strip()
    if not extracted:
        raise ValueError(f"PDF attachment {filename!r} has no extractable text")
    return _truncate_attachment_text(extracted)


# Strong past-tense / perfect-aspect phrases that signal the model is claiming
# to have produced an image. Only checked when ``image_generate`` is available
# and was not invoked. Future-tense ("I'll draw…", "给你画…") is intentionally
# excluded — those express intent and are often followed by an actual tool call
# in the same or next iteration; flagging them is noisy.
_IMAGE_CLAIM_PATTERNS = (
    # Chinese: perfect aspect / demonstrative past
    "已生成图片",
    "生成了图片",
    "画了一张",
    "这是生成的图",
    "已为您生成",
    "已经画好",
    "绘制好了",
    # English: past / perfect tense
    "generated an image",
    "i have created the image",
    "i've created the image",
    "i have generated the image",
    "i've generated the image",
    # Specific "here is/here's the image I …" — require the "I" pronoun to
    # avoid matching "here's the image you uploaded".
    "here is the image i",
    "here's the image i",
    # Markdown embed of a fake generated asset.
    "![generated",
)


def _claims_image_without_tool_use(
    final_text: str,
    tool_defs: list[Any],
    turn_segments: list[dict],
) -> bool:
    """Detect: model claimed image generation but never called image_generate.

    Returns True only when the tool was *available* (so we know the model had
    the option) and *not called* in this turn yet the final text matches a claim
    pattern. Used to surface a non-persistent UI warning; never writes to transcript.
    """
    tool_names = {getattr(td, "name", "") for td in tool_defs}
    if "image_generate" not in tool_names:
        return False
    had_image_call = any(
        isinstance(seg, dict)
        and seg.get("type") == "tool_use"
        and seg.get("name") == "image_generate"
        for seg in turn_segments
    )
    if had_image_call:
        return False
    if not final_text:
        return False
    lowered = final_text.lower()
    return any(p.lower() in lowered for p in _IMAGE_CLAIM_PATTERNS)


def _derive_router_tier_fallbacks(
    turn: TurnContext,
    cloned_selector: Any,
) -> list[Any] | None:
    """Derive fallback provider configs from the router's active tiers.

    When Auto Pilot selects a model for a turn, other configured text tiers in the
    active profile serve as automatic failovers if the routed model fails (e.g.
    times out or encounters upstream errors).
    """
    if not turn.metadata.get("routing_applied"):
        return None
    routed_tier = turn.metadata.get("routed_tier")
    if not routed_tier:
        return None
    router_cfg = getattr(turn.config, "agentos_router", None)
    if router_cfg is None or not getattr(router_cfg, "enabled", False):
        return None
    tiers = getattr(router_cfg, "tiers", None)
    if not isinstance(tiers, dict) or not tiers:
        return None

    tier_preferences = {
        "c0": ["c1", "c2", "c3"],
        "c1": ["c2", "c0", "c3"],
        "c2": ["c1", "c3", "c0"],
        "c3": ["c2", "c1", "c0"],
    }
    preferred_order = tier_preferences.get(str(routed_tier).lower(), ["c1", "c2", "c0", "c3"])
    candidate_tiers = [t for t in preferred_order if t in tiers and t != routed_tier]
    for t in tiers:
        if t not in candidate_tiers and t != routed_tier:
            candidate_tiers.append(t)

    primary_cfg = getattr(cloned_selector, "current_config", None)
    if primary_cfg is None and hasattr(cloned_selector, "_chain") and cloned_selector._chain:
        primary_cfg = cloned_selector._chain[0]
    if primary_cfg is None:
        return None

    from agentos.provider.selector import ProviderConfig

    fallbacks: list[ProviderConfig] = []
    seen_models: set[str] = {turn.model or primary_cfg.model}

    for tier_name in candidate_tiers:
        tier_data = tiers.get(tier_name)
        if not isinstance(tier_data, dict):
            continue
        if tier_data.get("image_only"):
            continue
        candidate_model = tier_data.get("model")
        if not candidate_model or candidate_model in seen_models:
            continue
        seen_models.add(candidate_model)

        candidate_provider = str(
            tier_data.get("provider") or getattr(primary_cfg, "provider", "") or ""
        )
        fallbacks.append(
            ProviderConfig(
                provider=candidate_provider,
                model=candidate_model,
                api_key=getattr(primary_cfg, "api_key", ""),
                base_url=getattr(primary_cfg, "base_url", ""),
                org_id=getattr(primary_cfg, "org_id", ""),
                proxy=getattr(primary_cfg, "proxy", ""),
                provider_routing=getattr(primary_cfg, "provider_routing", {}),
            )
        )

    return fallbacks if fallbacks else None


def _terminal_done_for(error: ErrorEvent) -> DoneEvent:
    """The DoneEvent that ends a turn the runner itself failed.

    Every turn ends with exactly one DoneEvent after any ErrorEvent; when the
    agent loop never ran (a refused budget) or raised, the runner supplies it.
    """

    return DoneEvent(
        outcome=outcome_from_error(code=error.code, message=error.message),
    )


def _harness_stage(name: str) -> Any:
    """A ``TurnRunner`` attribute that reads and replaces one harness stage."""

    def get(runner: TurnRunner) -> Any:
        return getattr(runner._harness.stages, name)

    def set_(runner: TurnRunner, stage: Any) -> None:
        setattr(runner._harness.stages, name, stage)

    return property(get, set_, doc=f"The harness's ``{name}`` stage.")


class TurnRunner:
    """Orchestrates a complete agent turn: provider → tools → prompt → pipeline → Agent.

    Uses supplied per-session locking and owns transcript persistence.
    All entry points (Web RPC, CLI, Channel) converge here.

    Lock ordering invariant:
        TurnRunner no longer owns an internal lock dict.
        Per-session locks are supplied by an external ``session_lock_provider``
        (``Callable[[str], asyncio.Lock]``) injected at construction time.

        Gateway path: provider = ``TaskRuntime._get_session_lock_for_turn``.
        It returns the short write lock used for transcript/session state
        mutation. TaskRuntime owns a separate execution lock and marks the
        call chain so ``TurnRunner.run()`` skips its legacy coarse acquire while
        append adapters still acquire the write lock.

        CLI / standalone path: provider = ``_standalone_lock_provider`` from
        ``build_turn_runner_from_services``, which maintains its own dict.

        The old model/approval-wide write lock is eliminated on the gateway
        path. External I/O must stay outside the write lock.
    """

    def __init__(
        self,
        provider_selector: Any,
        tool_registry: Any | None = None,
        session_manager: Any | None = None,
        skill_loader: Any | None = None,
        usage_tracker: Any | None = None,
        config: Any | None = None,
        memory_sync_managers: dict[str, Any] | None = None,
        model_catalog: Any | None = None,
        memory_retrievers: dict[str, Any] | None = None,
        turn_capture_services: dict[str, Any] | None = None,
        memory_provider_managers: dict[str, Any] | None = None,
        session_lock_provider: Callable[[str], asyncio.Lock] | None = None,
        diagnostics_state: Any | None = None,
        turn_hooks: Sequence[TurnHook] | None = None,
        compaction_hooks: Sequence[CompactionHook] | None = None,
        tool_hooks: Sequence[ToolHook] | None = None,
        request_hooks: Sequence[RequestHook] | None = None,
    ) -> None:
        self._provider_selector = provider_selector
        self._tool_registry = tool_registry
        self._session_manager = session_manager
        self._skill_loader = skill_loader
        self._usage_tracker = usage_tracker
        self._config = config
        self._last_agent_max_iterations_source = "AgentConfig default"
        self._memory_sync_managers = memory_sync_managers
        self._model_catalog = model_catalog
        self._memory_retrievers = memory_retrievers
        self._turn_capture_services = turn_capture_services
        # External memory provider managers (Plan B), keyed by agent_id. Empty /
        # None unless a provider is configured AND available at boot. Every
        # provider wiring site is a single ``None``/empty-dict check so the
        # disabled default path adds zero awaits/imports to the hot path.
        self._memory_provider_managers = memory_provider_managers
        self._diagnostics_state = diagnostics_state
        self._router_control_hold_store = RouterControlHoldStore()
        # TurnHook surface. The default trace hook reproduces the inline trace
        # event behavior while keeping the event sink replaceable at construction.
        if turn_hooks is None:
            self._turn_hooks: tuple[TurnHook, ...] = (DefaultTraceEmitterHook(),)
        else:
            self._turn_hooks = tuple(turn_hooks)
        # CompactionHook surface. HistoryLoadStage fans
        # before/after-compact events out through these hooks. Empty tuple by
        # default means compaction runs with no hook fan-out.
        self._compaction_hooks: tuple[CompactionHook, ...] = (
            tuple(compaction_hooks) if compaction_hooks else ()
        )
        # ToolHook surface. build_tool_handler uses these to wrap tool execution.
        self._tool_hooks: tuple[ToolHook, ...] = tuple(tool_hooks) if tool_hooks else ()
        self._request_hooks: tuple[RequestHook, ...] = tuple(request_hooks) if request_hooks else ()
        # Per-session lock provider.
        # Gateway path: task_runtime._get_session_lock_for_turn (wired in boot.py).
        # CLI/standalone path: _standalone_lock_provider from build_turn_runner_from_services.
        # Test/direct-construction path: fallback dict created here inside a closure.
        # TurnRunner no longer owns a named per-session lock dict as an instance attribute.
        # The lock dict lives entirely in the provider closure.
        if session_lock_provider is None:
            _fallback_locks: dict[str, asyncio.Lock] = {}

            def _fallback_provider(key: str) -> asyncio.Lock:
                return _fallback_locks.setdefault(key, asyncio.Lock())

            session_lock_provider = _fallback_provider
        self._session_lock_provider = session_lock_provider
        # Frozen memory snapshots keyed by (agent_id, session_key).
        # Captured at session start, refreshed on write/compaction.
        self._memory_snapshots: BoundedRegistry[tuple[str, str], MemorySnapshot] = BoundedRegistry(
            name="TurnRunner._memory_snapshots",
            session_of=lambda key, _value: key[1],
        )
        # Frozen bootstrap snapshots keyed by (agent_id, session_key, context_mode).
        # Captured on first prompt assembly so bootstrap-source edits do not
        # churn the cacheable prefix mid-session.
        self._bootstrap_snapshots: BoundedRegistry[tuple[str, str, str], BootstrapSnapshot] = (
            BoundedRegistry(
                name="TurnRunner._bootstrap_snapshots",
                session_of=lambda key, _value: key[1],
            )
        )
        # User turns since the last memory review, keyed (agent_id, session_key).
        self._memory_nudge_counters: dict[tuple[str, str], int] = {}
        self._turn_compacted_sessions: set[str] = set()
        self._active_pre_compaction_flush_tasks: dict[str, asyncio.Task] = {}
        self._background_tasks: set[asyncio.Task] = set()
        # Last persisted row each session's loaded history covers, keyed by
        # session key; anchors inline compaction persistence so rows appended
        # mid-turn (queued follow-ups) are never overwritten or archived.
        self._compaction_snapshots: dict[str, TranscriptSnapshot] = {}
        # TurnRunner stage decomposition InputStage instance. Holds no per-turn state;
        # constructed once. Active unconditionally as of.
        input_stage = InputStage(extra_ctx=self)
        # TurnRunner stage decomposition ProviderAndToolsStage instance. Holds no
        # per-turn state. Active unconditionally as of.
        provider_and_tools_stage = ProviderAndToolsStage(
            provider_resolver=self,
            tool_builder=self,
        )
        # TurnRunner stage decomposition PromptAssemblerStage instance. Holds no
        # per-turn state. Active unconditionally as of.
        prompt_assembler_stage = PromptAssemblerStage(
            prompt_assembler=self,
            pipeline_executor=self,
            router_context=self,
            prompt_config_resolver=self,
            prompt_report_builder=PromptReportBuilder(),
            session_id_resolver=self,
            memory_fingerprint=self,
        )
        # TurnRunner stage decomposition AgentBootstrapStage instance. Holds no
        # per-turn state. Active unconditionally as of.
        agent_bootstrap_stage = AgentBootstrapStage(
            timeout_budget=self,
            model_catalog=self,
            agent_config_builder=self,
            memory_snapshot=self,
            agent_factory=self,
        )
        # TurnRunner stage decomposition HistoryLoadStage instance. Holds no
        # per-turn state. Active unconditionally as of.
        # Between-turns (proactive) compaction + estimator calibration.
        self._proactive_compaction = ProactiveCompactionScheduler(self)
        history_load_stage = HistoryLoadStage(
            history_loader=self,
            request_context_prepender=RequestContextPrepender(),
            turn_start_observer=self._proactive_compaction,
        )
        # TurnRunner stage decomposition AttachmentStage instance. Holds no per-turn
        # state. Active unconditionally as of.
        attachment_stage = AttachmentStage(
            builder=self,
        )
        # TurnRunner stage decomposition StreamConsumerStage instance. Holds no
        # per-turn state. Active unconditionally as of. The
        # warning transformer binds ``self._handle_runtime_warning`` as
        # a one-method callable; the recording-fake discipline applies
        # identically to a Protocol-shaped port.
        stream_consumer_stage = StreamConsumerStage(
            agent_run=AgentTurnRunner(),
            compaction_persist=self,
            memory_snapshot_refresh=self,
            memory_sync_notify=MemorySyncNotifier(),
            warning_transformer=self._handle_runtime_warning,
            compaction_hooks=self._compaction_hooks,
        )
        # TurnRunner stage decomposition TurnFinalizerStage instance. Holds no
        # per-turn state. Active unconditionally as of. Adapter
        # contracts:
        #   * TranscriptAppendPort folds the ``token_count`` introspect
        #     and the ``session_manager is None`` guard.
        #   * TurnMemoryCapturePort forwards verbatim; the stage owns
        #     the log-and-continue try/except.
        #   * SessionTotalsPort inlines the post-DoneEvent cost rollup
        #     bit-identically to the legacy slice.
        #   * TurnErrorPersistPort forwards verbatim; the helper owns
        #     its own try/except + None guards.
        turn_finalizer_stage = TurnFinalizerStage(
            transcript_append=self,
            turn_memory_capture=self,
            session_totals=self,
            turn_error_persist=self,
            memory_nudge=self,
            proactive_compaction=self._proactive_compaction,
        )
        # The orchestrator of every turn; the stages get their ports above.
        self._harness = TurnHarness(
            self,
            TurnStages(
                input=input_stage,
                provider_and_tools=provider_and_tools_stage,
                prompt_assembler=prompt_assembler_stage,
                agent_bootstrap=agent_bootstrap_stage,
                history_load=history_load_stage,
                attachment=attachment_stage,
                stream_consumer=stream_consumer_stage,
                turn_finalizer=turn_finalizer_stage,
            ),
        )
        # The pre-turn prompt steps, in order (``engine.pipeline``).
        self._prompt_steps: PromptStepRegistry = DEFAULT_PROMPT_STEPS
        # Memory-nudge reviews running in the background (MemoryNudgePort).
        self._memory_nudge_pending: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------
    # Stage ports: TurnRunner implements the stages' Protocol ports
    # itself; the stages are built with ``port=self`` (#3819).
    # ------------------------------------------------------------------

    # -- ExtraContextResolver ----------
    # The two static extra-context helpers.
    #

    def extra_context_for(self, ctx: ToolContext | None) -> dict[str, str]:
        # Imported lazily to avoid a runtime → turn_runner → runtime cycle.

        return self._extra_context_for_tool_context(ctx)

    def merge(
        self,
        base: dict[str, str] | None,
        extra: dict[str, str],
    ) -> dict[str, str] | None:

        return self._merge_extra_prompt_context(base, extra)

    # -- ProviderResolverPort ----------
    # Calls ``TurnRunner._resolve_provider``.

    def resolve_provider(self) -> tuple[Any | None, Any | None]:
        return self._resolve_provider()

    # -- ToolBuilderPort ----------
    # Wraps ``TurnRunner._build_tools`` and the two ``ToolContext`` mutators.

    async def with_artifact_context(
        self,
        ctx: ToolContext,
        session_key: str,
    ) -> ToolContext:
        return await self._with_artifact_context(ctx, session_key)

    def with_runtime_write_callbacks(
        self,
        ctx: ToolContext,
        agent_id: str,
    ) -> ToolContext:
        return self._with_runtime_write_callbacks(ctx, agent_id)

    def build_tools(
        self,
        ctx: ToolContext | None,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[list[Any], ToolHandler | None]:
        return self._build_tools(ctx, metadata=metadata)

    # -- PromptAssemblerPort ----------
    # Calls ``TurnRunner._assemble_prompt``.

    def assemble_prompt(
        self,
        agent_id: str,
        tool_defs: list[Any],
        *,
        session_key: str | None,
        semantic_message: str | None,
        extra_context: dict[str, str] | None,
        prompt_metadata: dict[str, Any],
        bootstrap_context_mode: str | None,
        fresh_user_session: bool = False,
    ) -> str | tuple[str, str]:
        return self._assemble_prompt(
            agent_id,
            tool_defs,
            session_key=session_key,
            semantic_message=semantic_message,
            extra_context=extra_context,
            prompt_metadata=prompt_metadata,
            bootstrap_context_mode=bootstrap_context_mode,
            fresh_user_session=fresh_user_session,
        )

    # -- PipelineExecutionPort ----------
    # Wraps ``TurnRunner._run_pipeline`` and unpack ``RunPipelineRequest``.

    async def run_pipeline(
        self,
        request: RunPipelineRequest,
    ) -> tuple[Any, Any]:
        return await self._run_pipeline(
            request.runtime_message,
            request.session_key,
            request.provider,
            request.cloned_selector,
            request.tool_defs,
            request.base_prompt,
            request.attachments,
            semantic_message=request.semantic_message,
            ingress_pipeline_steps=request.ingress_pipeline_steps,
            prev_assistant_text=request.prev_assistant_text,
            prev_assistant_usage=request.prev_assistant_usage,
            history_user_texts=request.history_user_texts,
            flags_text_override=request.flags_text_override,
            tool_context=request.tool_context,
            normalization_metadata=request.normalization_metadata,
        )

    # -- RouterContextPort ----------
    # Wraps ``TurnRunner._router_previous_assistant_context``.

    async def fetch_router_context(
        self,
        session_key: str,
        *,
        exclude_last_user: bool,
    ) -> dict[str, Any]:
        return await self._router_previous_assistant_context(
            session_key,
            exclude_last_user=exclude_last_user,
        )

    # -- PromptConfigResolverPort ----------
    # Wraps ``TurnRunner._resolve_prompt_config``.

    def resolve_prompt_config(
        self,
        turn: Any,
    ) -> tuple[str, list[Any] | None, str | None]:
        return self._resolve_prompt_config(turn)

    # -- SessionIdResolverPort ----------
    # Wraps ``TurnRunner._resolve_session_id_for_log``.

    async def resolve_session_id_for_log(
        self,
        session_key: str,
    ) -> str | None:
        return await self._resolve_session_id_for_log(session_key)

    # -- MemoryFingerprintPort ----------
    # Wraps ``TurnRunner._config.memory_mode_fingerprint`` defensively.
    #
    # Returns ``None`` when the config is absent, when the method is missing,
    # or when the call raises — applying the defensive ``try/except``
    # pattern used previously inline.

    def memory_mode_fingerprint(self) -> dict[str, str] | None:
        config = self._config
        if config is None:
            return None
        if not hasattr(config, "memory_mode_fingerprint"):
            return None
        try:
            return cast(dict[str, str] | None, config.memory_mode_fingerprint())
        except Exception:  # noqa: BLE001 - defensive
            return None

    # -- TimeoutBudgetPort ----------
    # Wraps the five ``TurnRunner._resolve_agent_*`` helpers as a single port.
    #
    # The port method composes the resolver chain in the order the inline body
    # walks it. ``effective_runtime_timeout`` honors the per-call
    # ``timeout`` override; the other four resolvers consume the per-call
    # explicit override and the session/env/config fallback chain
    # internally.

    def resolve_budgets(
        self,
        *,
        session_key: str,
        timeout: float | None,
        max_iterations: int | None,
        iteration_timeout: float | None,
        tool_timeout: float | None,
        request_timeout: float | None,
        max_provider_retries: int | None,
    ) -> _ResolvedBudgets:
        runtime_timeout = (
            float(timeout)
            if timeout is not None
            else self._resolve_agent_runtime_timeout(session_key)
        )
        self._last_agent_max_iterations_source = "unknown"
        resolved_max_iterations = self._resolve_agent_max_iterations(session_key, max_iterations)
        max_iterations_source = getattr(
            self,
            "_last_agent_max_iterations_source",
            "unknown",
        )
        return _ResolvedBudgets(
            runtime_timeout=runtime_timeout,
            max_iterations=resolved_max_iterations,
            max_iterations_source=max_iterations_source,
            iteration_timeout=self._resolve_agent_iteration_timeout(session_key, iteration_timeout),
            tool_timeout=self._resolve_agent_tool_timeout(session_key, tool_timeout),
            request_timeout=self._resolve_agent_request_timeout(session_key, request_timeout),
            max_provider_retries=self._resolve_agent_max_provider_retries(
                session_key, max_provider_retries
            ),
        )

    # -- ModelCatalogPort ----------
    # Wraps ``TurnRunner._model_catalog`` lookups with a None-fallback.
    #
    # Folds the ``self._model_catalog is None`` branch into the port method so
    # the stage body has no conditional on catalog presence.

    def lookup(self, model_id: str) -> _ResolvedCatalog:
        llm_cfg = getattr(self._config, "llm", None) if self._config else None
        user_max_tokens = getattr(llm_cfg, "max_tokens", 0)
        if self._model_catalog is not None:
            provider_name = getattr(llm_cfg, "provider", "openrouter")
            max_tokens = self._model_catalog.resolve_max_tokens(
                model_id, user_override=user_max_tokens, provider_name=provider_name
            )
            context_window = self._model_catalog.resolve_context_window(model_id, provider_name)
            base_url = getattr(llm_cfg, "base_url", "")
            capabilities = self._model_catalog.get_capabilities(
                model_id, provider_name=provider_name, base_url=base_url
            )
        else:
            max_tokens = user_max_tokens if user_max_tokens > 0 else 16384
            context_window = 200_000
            capabilities = None
        return _ResolvedCatalog(
            max_tokens=max_tokens,
            context_window=context_window,
            capabilities=capabilities,
        )

    # -- AgentConfigBuilderPort ----------
    # Wraps the five ``TurnRunner`` helpers AgentConfig assembly needs.
    #
    # The inline body calls ``_resolve_turn_thinking(turn)``,
    # ``_resolve_memory_source_dir(agent_id)``, and reads
    # ``media_root_from_config(self._config) / "tool-results"`` plus a
    # handful of ``getattr`` reads off ``_mem_cfg`` / ``_agent_token_cfg``.
    # The port method returns a typed ``_AgentConfigAuxiliaries`` value that
    # the stage feeds straight into ``AgentConfig(...)``.

    def build_auxiliaries(
        self,
        *,
        agent_id: str,
        session_key: str,
        session_id_for_log: str | None,
        turn: Any,
    ) -> _AgentConfigAuxiliaries:
        from agentos.paths import media_root_from_config

        agent_token_cfg = (
            getattr(self._config, "agent_token_saving", None) if self._config else None
        )
        thinking = self._resolve_turn_thinking(turn)
        return _AgentConfigAuxiliaries(
            thinking=thinking,
            parallel_tool_calls=self._resolve_parallel_tool_calls(),
            loop_guard=self._resolve_loop_guard(),
            repetition_guard=self._resolve_repetition_guard(),
            retry_budget_seconds=getattr(self._config, "agent_retry_budget_seconds", None),
            timeout_context_recovery=getattr(self._config, "agent_timeout_context_recovery", None),
            tool_result_store_dir=str(media_root_from_config(self._config) / "tool-results"),
            tool_result_store_session_id=session_id_for_log or session_key,
            tool_result_projection_max_inline_chars=getattr(
                agent_token_cfg,
                "tool_result_projection_max_inline_chars",
                60_000,
            ),
            tool_result_store_max_bytes=getattr(
                agent_token_cfg,
                "tool_result_store_max_bytes",
                8 * 1024 * 1024,
            ),
            tool_result_store_disk_budget_bytes=getattr(
                agent_token_cfg,
                "tool_result_store_disk_budget_bytes",
                256 * 1024 * 1024,
            ),
            tool_result_store_retention_seconds=getattr(
                agent_token_cfg,
                "tool_result_store_retention_seconds",
                7 * 24 * 60 * 60,
            ),
        )

    # -- MemorySnapshotPort ----------
    # Wraps the per-agent memory warm + per-(agent_id, session_key) snapshot capture.
    #
    # The port method wraps the runner's ``_memory_sync_managers`` lookup +
    # ``warm_session`` call + ``_memory_snapshots`` dict mutation. The
    # contract is narrow: this writer fires exactly once on the first
    # turn when ``private_memory_allowed`` is true. The dict has other
    # writers elsewhere in the runner (notably ``refresh_memory_snapshot``
    # and the in-turn compaction-refresh path) that this port method does
    # NOT replace and does NOT need to coordinate with.

    async def warm_and_capture(
        self,
        *,
        agent_id: str,
        session_key: str,
        warmed: bool = False,
    ) -> _MemorySnapshotResult:
        from agentos.engine.runtime import MemorySnapshot
        from agentos.session.keys import (
            allows_private_memory_prompt_injection,
        )

        sync_manager = (
            self._memory_sync_managers.get(agent_id) if self._memory_sync_managers else None
        )
        if sync_manager is not None and not warmed:
            await sync_manager.warm_session(session_key)

        private_memory_allowed = allows_private_memory_prompt_injection(session_key)
        snap_key = (agent_id, session_key)
        if private_memory_allowed and snap_key not in self._memory_snapshots:
            from agentos.engine.runtime import MemorySourceUnreadableError

            workspace = self._resolve_memory_source_dir(agent_id)
            try:
                memory_md = self._load_memory_md(workspace)
            except MemorySourceUnreadableError:
                # Leave the snapshot unset so the next turn retries. Freezing
                # an empty one here would blind the agent for the whole
                # session over a transient read failure.
                pass
            else:
                self._memory_snapshots[snap_key] = MemorySnapshot(
                    memory_md=memory_md,
                    # Daily notes are dropped unconditionally before injection
                    # (see the omit block in runtime.py). Reading them here
                    # only to discard them there cost a directory scan plus
                    # file reads on every session start.
                    daily_notes={},
                )
        return _MemorySnapshotResult(
            sync_manager=sync_manager,
            private_memory_allowed=private_memory_allowed,
        )

    # -- AgentFactoryPort ----------
    # Wraps the typed ``Agent(...)`` constructor.
    #
    # The port method injects the runner-singleton dependencies
    # (``usage_tracker``, ``tool_registry``) so the stage never
    # sees those runtime attributes directly.

    def build_agent(
        self,
        *,
        provider: LLMProvider,
        config: AgentConfig,
        tool_definitions: list[Any],
        tool_handler: ToolHandler | None,
        session_key: str,
        turn_call_logger: TurnCallLogger | None,
        memory_sync_manager: Any | None,
        tool_context: ToolContext | None,
    ) -> Agent:
        from agentos.engine.agent import Agent

        return Agent(
            provider=provider,
            config=config,
            tool_definitions=tool_definitions,
            tool_handler=tool_handler,
            usage_tracker=self._usage_tracker,
            session_key=session_key,
            turn_call_logger=turn_call_logger,
            memory_sync_manager=memory_sync_manager,
            tool_registry=self._tool_registry,
            tool_context=tool_context,
            spend_budget_guard=self._check_spend_budget,
            request_hooks=getattr(self, "_request_hooks", ()),
            # The process's approval queue, resolved when a wait starts.
            approval_waiter=default_approval_waiter(),
        )

    # -- HistoryLoaderPort ----------
    # Calls ``TurnRunner._load_history``.
    #
    # The helper mutates ``agent._history`` via
    # ``agent.set_history`` internally; the port method preserves that.
    # Exceptions propagate (no surrounding
    # try/except).

    async def load(
        self,
        *,
        agent: Agent,
        session_key: str,
        trim_last_user: bool,
        input_message_id: str | None = None,
    ) -> str | None:
        if input_message_id:
            return await self._load_history(
                agent,
                session_key,
                trim_last_user=trim_last_user,
                input_message_id=input_message_id,
            )
        return await self._load_history(
            agent,
            session_key,
            trim_last_user=trim_last_user,
        )

    # -- CompactionPersistPort ----------
    # Wraps ``SessionManager.persist_compaction_result`` + ``notify_compaction``.
    #
    # Compaction refresh: the port method forwards the persist call verbatim,
    # anchored on the ``TranscriptSnapshot`` ``_load_history`` recorded for the
    # session, and follows it with a completed lifecycle notification. Failed
    # persistence is handled by the stream consumer stage so completed is never
    # emitted before durable storage succeeds. The re-entrancy contract on
    # ``persist_compaction_result`` is untouched.

    async def persist_and_notify(
        self,
        *,
        session_key: str,
        summary: str,
        kept_entries: list[Any],
        compaction_id: str | None = None,
    ) -> None:
        from agentos.engine.cache_break_monitor import notify_compaction
        from agentos.session.compaction_lifecycle import (
            COMPACTION_PERSISTED_EVENT,
            compaction_effect_payload,
            compaction_lifecycle_payload,
            new_compaction_id,
        )
        from agentos.session.manager import TranscriptSnapshot

        session_manager = self._session_manager
        if session_manager is None:
            return
        persist_method = session_manager.persist_compaction_result
        params = inspect.signature(persist_method).parameters
        persist_kwargs: dict[str, Any] = {}
        if "compaction_id" in params or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        ):
            persist_kwargs["compaction_id"] = compaction_id
        if "trigger_reason" in params or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        ):
            persist_kwargs["trigger_reason"] = "agent_inline_overflow"
        # Anchor the rewrite on the rows the agent's history was loaded from
        # so a follow-up queued mid-turn is never overwritten or archived.
        snapshots = getattr(self, "_compaction_snapshots", None)
        snapshot = snapshots.get(session_key) if snapshots is not None else None
        if snapshot is not None and "snapshot" in params:
            persist_kwargs["snapshot"] = snapshot
        # Durable pre-image before the destructive rewrite (the guarantee the
        # removed before-turn preflight/T3 paths gave). A failing checkpoint
        # raises here, so nothing is archived without it. Ephemeral sessions
        # (subagent, cron) are compacted without a checkpoint, as before.
        await self._record_compaction_checkpoint(session_key, compaction_id)
        async with self._session_write_context(session_key):
            persisted = await persist_method(
                session_key,
                summary,
                kept_entries,
                **persist_kwargs,
            )
        if isinstance(persisted, TranscriptSnapshot):
            # The next compaction of this same in-memory history must anchor
            # on the kept tail that was just written, not on the old rows.
            if snapshots is not None:
                snapshots[session_key] = persisted
        elif "snapshot" in params:
            # A snapshot-aware manager returns None only when it wrote nothing
            # (stale snapshot, missing session); do not report it as persisted.
            raise CompactionPersistRejectedError(
                f"inline compaction for {session_key} was not persisted: the transcript "
                "changed since the agent loaded its history"
            )
        compaction_id = compaction_id or new_compaction_id()
        notify_compaction(
            session_key,
            source="automatic",
            phase="agent_inline_overflow",
            status="completed",
            kept_count=len(kept_entries),
            summary_len=len(summary or ""),
            **compaction_effect_payload(status="completed"),
            **compaction_lifecycle_payload(
                compaction_id,
                COMPACTION_PERSISTED_EVENT,
            ),
        )

    async def _record_compaction_checkpoint(
        self, session_key: str, compaction_id: str | None
    ) -> None:
        from agentos.engine.compaction_service import is_ephemeral_session
        from agentos.session.compaction_lifecycle import new_compaction_id

        if is_ephemeral_session(session_key):
            return
        session_manager = self._session_manager
        if not callable(getattr(type(session_manager), "record_memory_checkpoint", None)):
            return
        get_transcript = getattr(session_manager, "get_transcript", None)
        if not callable(get_transcript):
            return
        transcript = get_transcript(session_key)
        if inspect.isawaitable(transcript):
            transcript = await transcript
        if not isinstance(transcript, list) or not transcript:
            return
        await self._record_checkpoint_before_compaction(
            session_key,
            transcript,
            turn_id=compaction_id or new_compaction_id(),
            source="agent_inline_compaction",
        )

    # -- MemorySnapshotRefreshPort ----------
    # Refresh ``runner._memory_snapshots[(agent_id, session_key)]`` after compaction.
    #
    # Resolves the memory source dir, loads
    # MEMORY.md + daily notes, writes the frozen snapshot. Respects
    # ``private_memory_allowed`` -- when false, the dict is not written.

    def refresh_snapshot(
        self,
        *,
        agent_id: str,
        session_key: str,
        private_memory_allowed: bool,
    ) -> None:
        from agentos.engine.runtime import MemorySnapshot

        workspace = self._resolve_memory_source_dir(agent_id)
        if private_memory_allowed:
            self._memory_snapshots[(agent_id, session_key)] = MemorySnapshot(
                memory_md=self._load_memory_md(workspace),
                daily_notes={},  # dropped before injection; see runtime.py
            )

    # -- AttachmentMessageBuilderPort ----------
    # Wraps ``TurnRunner._build_attachment_messages`` + media-root lookup.
    #
    # Forwards verbatim to the static helper and resolves
    # ``media_root`` from the runner instance on every call. The stage
    # body never sees the runner.
    #
    # Exception contract: the helper raises ``ValueError`` on
    # validation failures (count cap, disallowed media type, ref without
    # media_root, invalid base64, oversize). The port method does NOT add a
    # try/except — exceptions propagate to the stage which propagates
    # them to the outer ``_run_turn`` terminal handler.

    def build(
        self,
        message: str,
        attachments: list[dict],
    ) -> list[Any] | None:
        return self._build_attachment_messages(
            message,
            attachments,
            media_root=self._attachment_media_root(),
        )

    # -- TranscriptAppendPort ----------
    # Wraps ``SessionManager.append_message`` for the assistant turn persist.
    #
    # Folds two responsibilities into the port method so the stage
    # body has no ``inspect`` dependency and no ``session_manager is None``
    # conditional:
    #
    # 1. ``accepts_keyword_arg(session_manager.append_message, "token_count")``
    #    introspection: passes ``token_count`` only when the manager
    #    accepts it.
    # 2. The ``session_manager is None`` guard: returns ``False`` (no
    #    append) instead of calling.
    #
    # Exceptions from ``append_message`` propagate to the stage and out
    # to the outer ``_run_turn`` terminal handler -- there is
    # no try/except here.

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
    ) -> bool:
        session_manager = self._session_manager
        if session_manager is None:
            return False
        append_kwargs: dict[str, Any] = {
            "role": role,
            "content": content,
            "tool_calls": tool_calls,
        }
        if reasoning_content is not None:
            append_kwargs["reasoning_content"] = reasoning_content
        if turn_usage is not None and accepts_keyword_arg(
            session_manager.append_message, "turn_usage"
        ):
            append_kwargs["turn_usage"] = turn_usage
        if accepts_keyword_arg(session_manager.append_message, "token_count"):
            append_kwargs["token_count"] = token_count
        if provider_messages is not None and accepts_keyword_arg(
            session_manager.append_message, "provider_messages"
        ):
            append_kwargs["provider_messages"] = provider_messages
        if replaces_message_id is not None and accepts_keyword_arg(
            session_manager.append_message, "replaces_message_id"
        ):
            append_kwargs["replaces_message_id"] = replaces_message_id
        await self._append_session_message(session_key, **append_kwargs)
        return True

    # -- TurnMemoryCapturePort ----------
    # Calls ``TurnRunner._capture_turn_memory``.
    #
    # Forwards verbatim. The log-and-continue try/except
    # lives in the stage body, NOT the port method -- the error-handling
    # contract is visible in the stage code.

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
    ) -> None:
        await self._capture_turn_memory(
            agent_id=agent_id,
            session_key=session_key,
            runtime_message=runtime_message,
            final_text=final_text,
            input_mode=input_mode,
            tool_context=tool_context,
            input_provenance=input_provenance,
            run_kind=run_kind,
            no_memory_capture=no_memory_capture,
        )

    # -- MemoryNudgePort ----------
    # Wraps the memory-nudge counter as a Protocol port.
    #
    # Advances the counter and, when a review comes due, schedules it as a
    # detached task. The stage never awaits the review: the reply has already
    # been delivered, and blocking here would keep the task marked running for
    # as long as the review takes.
    #
    # The task reference is held until completion so the loop cannot collect it
    # mid-flight, which would cancel the review silently.

    def note_turn(
        self,
        *,
        agent_id: str,
        session_key: str,
        input_mode: str,
        run_kind: str,
        no_memory_capture: bool,
        turn_segments: Any,
    ) -> None:
        task = asyncio.create_task(
            self._note_and_maybe_review(
                agent_id=agent_id,
                session_key=session_key,
                input_mode=input_mode,
                run_kind=run_kind,
                no_memory_capture=no_memory_capture,
                turn_segments=turn_segments,
            )
        )
        self._memory_nudge_pending.add(task)
        task.add_done_callback(self._memory_nudge_pending.discard)

    async def _note_and_maybe_review(
        self,
        *,
        agent_id: str,
        session_key: str,
        input_mode: str,
        run_kind: str,
        no_memory_capture: bool,
        turn_segments: Any,
    ) -> None:
        prior_user_turns: int | None = None
        if (agent_id, session_key) not in self._memory_nudge_counters:
            prior_user_turns = await self._count_prior_user_turns(session_key)
        due = self._note_turn_for_memory_nudge(
            agent_id=agent_id,
            session_key=session_key,
            input_mode=input_mode,
            run_kind=run_kind,
            no_memory_capture=no_memory_capture,
            turn_segments=turn_segments,
            prior_user_turns=prior_user_turns,
        )
        if due:
            await self._run_memory_nudge_review(
                agent_id=agent_id,
                session_key=session_key,
            )

    async def _count_prior_user_turns(self, session_key: str) -> int | None:
        """User messages already persisted for this session, or None if unknown.

        Read once per session per process, only to seed a counter that does
        not exist yet -- afterwards the in-memory count carries the session.
        """
        manager = getattr(self, "_session_manager", None)
        if manager is None:
            return None
        try:
            entries = await manager.get_transcript(session_key)
        except Exception:  # noqa: BLE001 — seeding is best-effort
            return None
        return sum(1 for e in entries or [] if getattr(e, "role", None) == "user")

    # -- SessionTotalsPort ----------
    # Roll up session token + cost + cache totals from a DoneEvent.
    #
    # Performs as a single transaction: ``get_session`` read,
    # ``normalize_event_cost_source`` call, four ``next_*`` accumulators,
    # ``rollup_cost_source`` call, ``Session.update`` write. Folds two
    # early-return guards (``session_manager is None`` and
    # ``current_session is None``) so the stage body has no conditional
    # on either; the stage's outer try/except wraps the port method call
    # with log-and-continue semantics.

    async def rollup(
        self,
        *,
        session_key: str,
        done_event: DoneEvent,
        resolved_model: str,  # noqa: ARG002 - reserved for future model-pinning
    ) -> CostRollupResult | None:
        from agentos.session.cost_rollup import (
            normalize_event_cost_source,
            rollup_cost_source,
        )

        session_manager = self._session_manager
        if session_manager is None:
            return None
        async with self._session_write_context(session_key):
            current_session = await session_manager.get_session(session_key)
            if current_session is None:
                return None

            done_total_tokens = done_event.input_tokens + done_event.output_tokens
            event_cost_source = normalize_event_cost_source(
                done_event.cost_source,
                input_tokens=done_event.input_tokens,
                output_tokens=done_event.output_tokens,
                cache_read_tokens=done_event.cached_tokens,
                cache_write_tokens=done_event.cache_write_tokens,
                cost_usd=done_event.cost_usd,
                billed_cost_usd=done_event.billed_cost,
            )
            next_total_cost = (
                getattr(current_session, "total_cost_usd", 0.0) or 0.0
            ) + done_event.cost_usd
            next_billed_cost = (
                getattr(current_session, "billed_cost_usd", 0.0) or 0.0
            ) + done_event.billed_cost
            next_estimated_component = (
                getattr(current_session, "estimated_cost_component_usd", 0.0) or 0.0
            )
            if event_cost_source == "agentos_estimate":
                next_estimated_component += done_event.cost_usd
            elif event_cost_source == "mixed":
                next_estimated_component += max(
                    0.0,
                    done_event.cost_usd - done_event.billed_cost,
                )
            next_missing_entries = getattr(current_session, "missing_cost_entries", 0) or 0
            if event_cost_source == "unavailable":
                next_missing_entries += 1
            next_cost_source = rollup_cost_source(
                billed_cost_usd=next_billed_cost,
                estimated_cost_component_usd=next_estimated_component,
                missing_cost_entries=next_missing_entries,
            )
            next_input_tokens = (
                getattr(current_session, "input_tokens", 0) or 0
            ) + done_event.input_tokens
            next_output_tokens = (
                getattr(current_session, "output_tokens", 0) or 0
            ) + done_event.output_tokens
            next_total_tokens = (
                getattr(current_session, "total_tokens", 0) or 0
            ) + done_total_tokens
            next_estimated_cost = (
                getattr(current_session, "estimated_cost_usd", 0.0) or 0.0
            ) + done_event.cost_usd
            next_cache_read = (
                getattr(current_session, "cache_read", 0) or 0
            ) + done_event.cached_tokens
            next_cache_write = (
                getattr(current_session, "cache_write", 0) or 0
            ) + done_event.cache_write_tokens
            next_model_override = done_event.model or getattr(
                current_session, "model_override", None
            )

            # Persist the last actual model into usage metadata only.
            # Writing it to session.model would pin future turns and
            # silently bypass agentos-router routing.
            await session_manager.update(
                session_key,
                input_tokens=next_input_tokens,
                output_tokens=next_output_tokens,
                total_tokens=next_total_tokens,
                total_tokens_fresh=True,
                estimated_cost_usd=next_estimated_cost,
                total_cost_usd=next_total_cost,
                billed_cost_usd=next_billed_cost,
                estimated_cost_component_usd=next_estimated_component,
                cost_source=next_cost_source,
                missing_cost_entries=next_missing_entries,
                cache_read=next_cache_read,
                cache_write=next_cache_write,
                model_override=next_model_override,
            )
        return CostRollupResult(
            input_tokens=next_input_tokens,
            output_tokens=next_output_tokens,
            total_tokens=next_total_tokens,
            estimated_cost_usd=next_estimated_cost,
            total_cost_usd=next_total_cost,
            billed_cost_usd=next_billed_cost,
            estimated_cost_component_usd=next_estimated_component,
            cost_source=next_cost_source,
            missing_cost_entries=next_missing_entries,
            cache_read=next_cache_read,
            cache_write=next_cache_write,
            model_override=next_model_override,
        )

    # -- TurnErrorPersistPort ----------
    # Calls ``TurnRunner._persist_turn_error``.
    #
    # Forwards verbatim. The helper owns its own log-and-continue
    # try/except and guards both ``session_manager is None`` and
    # ``event is None`` internally, so the port method and stage body have no
    # additional guards.

    async def persist_error(
        self,
        *,
        session_key: str,
        event: ErrorEvent | None,
    ) -> None:
        await self._persist_turn_error(session_key, event)

    # The harness's stages, readable and replaceable under their old names.
    _input_stage = _harness_stage("input")
    _provider_and_tools_stage = _harness_stage("provider_and_tools")
    _prompt_assembler_stage = _harness_stage("prompt_assembler")
    _agent_bootstrap_stage = _harness_stage("agent_bootstrap")
    _history_load_stage = _harness_stage("history_load")
    _attachment_stage = _harness_stage("attachment")
    _stream_consumer_stage = _harness_stage("stream_consumer")
    _turn_finalizer_stage = _harness_stage("turn_finalizer")

    @property
    def router_control_hold_store(self) -> RouterControlHoldStore:
        """Session-scoped router holds, shared by the router_control tool and
        the user-facing /c0-/c3 slash commands (gateway ``router.hold.*`` RPC)."""
        return self._router_control_hold_store

    @property
    def router_control_config(self) -> Any:
        """Active Pilot Router config section, or None when not configured."""
        return getattr(self._config, "agentos_router", None)

    def has_compacted_this_turn(self, session_key: str) -> bool:
        return session_key in self._turn_compacted_sessions

    def mark_compacted_this_turn(self, session_key: str) -> None:
        self._turn_compacted_sessions.add(session_key)

    def clear_compacted_this_turn(self, session_key: str) -> None:
        self._turn_compacted_sessions.discard(session_key)

    def clear_compaction_turn_state(self, session_key: str) -> None:
        self._turn_compacted_sessions.discard(session_key)
        # The anchor only means something for the turn that loaded the history;
        # the next turn's _load_history records a fresh one.
        self._compaction_snapshots.pop(session_key, None)
        # The turn is over: a proactive compaction it scheduled may start.
        proactive = getattr(self, "_proactive_compaction", None)
        if proactive is not None:
            proactive.turn_ended(session_key)

    def refresh_memory_snapshot(self, agent_id: str) -> None:
        """Refresh frozen snapshots for all sessions of the given agent.

        Called by the on_memory_write callback when the agent writes to
        MEMORY.md, USER.md, or daily notes via memory_save, memory_delete,
        or memory (add/replace/remove).
        """
        ws = self._resolve_memory_source_dir(agent_id)
        new_snap = MemorySnapshot(
            memory_md=self._load_memory_md(ws),
            daily_notes={},  # dropped before injection; see the omit block below
        )
        for key in list(self._memory_snapshots):
            if key[0] == agent_id:
                self._memory_snapshots[key] = new_snap

    def _handle_memory_source_write(self, agent_id: str, path: str) -> None:
        """Refresh memory index/snapshots after a source Markdown file write."""
        sync_manager = (
            self._memory_sync_managers.get(agent_id) if self._memory_sync_managers else None
        )
        mark_dirty = getattr(sync_manager, "mark_dirty", None)
        if callable(mark_dirty):
            mark_dirty()
        self.refresh_memory_snapshot(agent_id)

    def _handle_bootstrap_source_write(self, agent_id: str, path: str) -> None:
        """Drop frozen bootstrap snapshots after a bootstrap workspace file write."""
        self._bootstrap_snapshots.discard_where(lambda key, _value: key[0] == agent_id)

    def _with_runtime_write_callbacks(
        self, tool_context: ToolContext, agent_id: str
    ) -> ToolContext:
        """Attach runtime snapshot refresh callbacks without discarding caller hooks."""
        if not tool_context.memory_source_dir:
            try:
                tool_context = replace(
                    tool_context,
                    memory_source_dir=str(self._resolve_memory_source_dir(agent_id)),
                )
            except Exception:  # noqa: BLE001 - memory path should not block tool setup
                pass

        previous_memory_write = tool_context.on_memory_source_write
        if previous_memory_write is None:
            tool_context = replace(
                tool_context,
                on_memory_source_write=self._handle_memory_source_write,
            )
        else:

            def _on_memory_source_write(agent_id: str, path: str) -> None:
                previous_memory_write(agent_id, path)
                self._handle_memory_source_write(agent_id, path)

            tool_context = replace(
                tool_context,
                on_memory_source_write=_on_memory_source_write,
            )

        previous_bootstrap_write = tool_context.on_bootstrap_source_write
        if previous_bootstrap_write is None:
            return replace(
                tool_context,
                on_bootstrap_source_write=self._handle_bootstrap_source_write,
            )

        def _on_bootstrap_source_write(agent_id: str, path: str) -> None:
            previous_bootstrap_write(agent_id, path)
            self._handle_bootstrap_source_write(agent_id, path)

        return replace(
            tool_context,
            on_bootstrap_source_write=_on_bootstrap_source_write,
        )

    async def _with_artifact_context(
        self,
        tool_context: ToolContext,
        session_key: str,
    ) -> ToolContext:
        attachments_cfg = getattr(self._config, "attachments", None)
        media_root = self._attachment_media_root()
        session_id = await self._resolve_session_id_for_log(session_key)
        if not session_id:
            session_id = session_key.split(":")[-1] or session_key
        return replace(
            tool_context,
            session_key=session_key,
            artifact_media_root=str(media_root),
            artifact_session_id=session_id,
            workspace_file_writes=[],
            artifact_max_bytes=getattr(attachments_cfg, "artifact_max_bytes", None),
            artifact_disk_budget_bytes=getattr(
                attachments_cfg,
                "artifact_disk_budget_bytes",
                None,
            ),
        )

    def _provider_manager_for(self, agent_id: str) -> Any | None:
        """Resolve the external memory provider manager for an agent, or None.

        Zero-cost when no provider is configured: the dict is None/empty on the
        disabled default path, so this returns ``None`` after a single lookup.
        Falls back to the ``main`` agent's manager, mirroring how the other
        per-agent memory tiers resolve.
        """
        managers = self._memory_provider_managers
        if not managers:
            return None
        return managers.get(agent_id) or managers.get("main")

    async def _resolve_session_id_for_prefetch(self, session_key: str) -> str:
        """Best-effort provider recall scoping key. Empty string on any miss."""
        if self._session_manager is None:
            return ""
        try:
            session = await self._session_manager.get_session(session_key)
        except Exception:  # noqa: BLE001 — recall scoping is best-effort
            return ""
        return str(getattr(session, "session_id", "") or "")

    # Default hard deadline for provider recall; ``[memory]
    # recall_timeout_seconds`` overrides it.
    RECALL_TIMEOUT_SECONDS_DEFAULT = 1.5
    _recall_budget_state: RecallBudget | None = None

    def _recall_budget(self) -> RecallBudget:
        budget = self._recall_budget_state
        if budget is None:
            budget = self._recall_budget_state = RecallBudget()
        return budget

    def _recall_timeout_seconds(self) -> float:
        value = getattr(getattr(self._config, "memory", None), "recall_timeout_seconds", None)
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            return self.RECALL_TIMEOUT_SECONDS_DEFAULT
        return float(value)

    async def _fetch_prefetch_block(
        self,
        *,
        agent_id: str,
        session_id: str,
        message: str,
        timeout: float | None = None,
    ) -> str:
        """Return the provider's fenced recall block for ``message``, or ``""``.

        Bounded by ``[memory] recall_timeout_seconds``: a recall that misses
        the deadline is not cancelled; it finishes in the background and its
        block can stand in on the session's next turn (see ``RecallBudget``).
        """
        manager = self._provider_manager_for(agent_id)
        if manager is None or not message:
            return ""
        return await self._recall_budget().recall(
            (agent_id, session_id) if session_id else None,
            lambda: manager.prefetch_all(message, session_id=session_id),
            timeout=self._recall_timeout_seconds() if timeout is None else timeout,
            log_fields={"agent_id": agent_id},
        )

    async def _recall_block_for_turn(self, *, agent_id: str, session_key: str, message: str) -> str:
        return await self._fetch_prefetch_block(
            agent_id=agent_id,
            session_id=await self._resolve_session_id_for_prefetch(session_key),
            message=message,
        )

    def _concurrent_turn_setup(self) -> bool:
        task_runtime = getattr(self._config, "task_runtime", None)
        return getattr(task_runtime, "concurrent_turn_setup", True) is not False

    def _start_pre_provider_io(
        self,
        *,
        agent_id: str,
        session_key: str,
        runtime_message: str,
        router_exclude_last_user: bool,
    ) -> PreProviderIO:
        """Start the independent pre-provider reads (see ``pre_provider_io``)."""
        from agentos.tools.deferred import hydrate_loaded_tools

        concurrent = self._concurrent_turn_setup()
        return PreProviderIO(
            concurrent=concurrent,
            # Gated on provider presence so the disabled default adds only a
            # dict lookup.
            recall=(
                (
                    lambda: self._recall_block_for_turn(
                        agent_id=agent_id,
                        session_key=session_key,
                        message=runtime_message,
                    )
                )
                if self._provider_manager_for(agent_id) is not None
                else None
            ),
            project_knowledge=lambda: self._project_knowledge_block(session_key),
            hydrate_tools=lambda: hydrate_loaded_tools(self._session_manager, session_key),
            router_context=(
                (
                    lambda: self._router_previous_assistant_context(
                        session_key,
                        exclude_last_user=router_exclude_last_user,
                    )
                )
                if concurrent
                else None
            ),
            memory_warm=self._memory_warm_for(agent_id, session_key) if concurrent else None,
        )

    def _memory_warm_for(
        self, agent_id: str, session_key: str
    ) -> Callable[[], Coroutine[Any, Any, None]] | None:
        sync_manager = (
            self._memory_sync_managers.get(agent_id) if self._memory_sync_managers else None
        )
        if sync_manager is None:
            return None
        return lambda: sync_manager.warm_session(session_key)

    async def _augment_extra_context_with_prefetch(
        self,
        *,
        agent_id: str,
        session_id: str,
        message: str,
        extra_context: dict[str, str] | None,
        timeout: float | None = None,
    ) -> dict[str, str] | None:
        """Return ``extra_context`` with the provider's fenced recall injected.

        The prefetched block is per-turn volatile content, so it rides in
        ``extra_context`` (rendered as a ``## <key>`` block in the dynamic
        suffix) rather than the cacheable base prompt. Best-effort and bounded:
        a slow provider cannot stall the turn — on timeout or error we log and
        inject nothing, returning the context unchanged. No-op (returns the
        input object) when no provider is configured.
        """
        block = await self._fetch_prefetch_block(
            agent_id=agent_id,
            session_id=session_id,
            message=message,
            timeout=timeout,
        )
        return merge_extra_context_block(extra_context, MEMORY_CONTEXT_KEY, block)

    # Injection ceiling for project knowledge. Kept equal to
    # SessionManager.PROJECT_KNOWLEDGE_MAX_CHARS (a release-consistency test
    # asserts this) so nothing that saves is silently cut here; the guard
    # still truncates rows written under the old, larger write cap or edited
    # out-of-band.
    PROJECT_KNOWLEDGE_INJECT_MAX_CHARS = 24_000

    async def _augment_extra_context_with_project_knowledge(
        self,
        *,
        session_key: str,
        extra_context: dict[str, str] | None,
    ) -> dict[str, str] | None:
        """Return ``extra_context`` with the session's project knowledge injected."""
        block = await self._project_knowledge_block(session_key)
        return merge_extra_context_block(extra_context, PROJECT_KNOWLEDGE_KEY, block)

    async def _project_knowledge_block(self, session_key: str) -> str | None:
        """Return the session's wrapped project-knowledge block, or ``None``.

        Sessions grouped into a project share the project's free-form
        knowledge text: it is read fresh from storage each turn (an edit is
        picked up on the next turn, no snapshot freeze) and rendered as a
        ``## Project Knowledge`` block in the dynamic suffix — the same seam
        workspace files use, so the cacheable base hash never varies by
        project. User-authored text is wrapped as untrusted, like workspace
        file content. Best-effort: any failure injects nothing.
        """
        if self._session_manager is None or not session_key:
            return None
        getter = getattr(self._session_manager, "get_project_knowledge_for_session", None)
        if not callable(getter):
            return None
        try:
            knowledge = await getter(session_key)
        except Exception as exc:  # noqa: BLE001 — knowledge injection is best-effort
            log.warning(
                "turn_runner.project_knowledge_load_failed",
                session_key=session_key,
                error=str(exc),
            )
            return None
        if not knowledge:
            return None
        cap = self.PROJECT_KNOWLEDGE_INJECT_MAX_CHARS
        if len(knowledge) > cap:
            knowledge = knowledge[:cap] + "\n... [project knowledge truncated]"
        return injection_guard.wrap_untrusted(knowledge, source="project:knowledge")

    async def _capture_turn_memory(
        self,
        *,
        agent_id: str,
        session_key: str,
        runtime_message: str,
        final_text: str,
        input_mode: str,
        tool_context: ToolContext | None,
        input_provenance: dict[str, Any] | None,
        run_kind: str = "default",
        no_memory_capture: bool = False,
    ) -> None:
        memory_cfg = getattr(self._config, "memory", None)
        if not self._turn_memory_capture_allowed(
            no_memory_capture=no_memory_capture,
            input_mode=input_mode,
            run_kind=run_kind,
            input_provenance=input_provenance,
            memory_config=memory_cfg,
        ):
            return
        if self._session_manager is None or not self._turn_capture_services:
            return
        capture_service = self._turn_capture_services.get(
            agent_id
        ) or self._turn_capture_services.get("main")
        if capture_service is None:
            return
        session = await self._session_manager.get_session(session_key)
        if session is None:
            return
        await capture_service.capture_turn(
            session_key=session_key,
            session_id=getattr(session, "session_id", ""),
            user_text=runtime_message,
            assistant_text=final_text,
            source=self._build_turn_call_source(
                tool_context,
                input_provenance,
                run_kind=run_kind,
            ),
            captured_at=datetime.now(tz=UTC),
            no_memory_capture=no_memory_capture,
        )

        # Mirror the completed turn to the external provider (Plan B). Both
        # calls are non-blocking background enqueues that no-op when no
        # provider is configured — the turn path never awaits provider I/O.
        provider_manager = self._provider_manager_for(agent_id)
        if provider_manager is not None:
            session_id = getattr(session, "session_id", "")
            provider_manager.sync_all(
                runtime_message,
                final_text,
                session_id=session_id,
                messages=[
                    {"role": "user", "content": runtime_message},
                    {"role": "assistant", "content": final_text},
                ],
            )
            provider_manager.queue_prefetch_all(runtime_message, session_id=session_id)

    def _memory_nudge_config(self) -> Any | None:
        memory_cfg = getattr(self._config, "memory", None)
        return getattr(memory_cfg, "nudge", None) if memory_cfg is not None else None

    def _memory_nudge_allowed(
        self,
        *,
        nudge_cfg: Any | None,
        no_memory_capture: bool,
        input_mode: str,
        run_kind: str | None,
    ) -> bool:
        """Whether this turn may advance the nudge counter.

        Deliberately stricter than turn capture: only real user turns count.
        Machine traffic (cron, heartbeat, subagent, and the review turn
        itself) says nothing durable about the user, and letting it advance
        the counter would fire reviews over transcripts that are pure harness.
        """
        if nudge_cfg is None or not getattr(nudge_cfg, "enabled", False):
            return False
        if int(getattr(nudge_cfg, "interval", 0)) <= 0:
            return False
        if no_memory_capture or input_mode != "user":
            return False
        return not self._capture_filter_matches(
            run_kind, getattr(nudge_cfg, "excluded_run_kinds", [])
        )

    @staticmethod
    def _turn_used_memory_tool(turn_segments: Any) -> bool:
        """True when the model called a curated-memory tool this turn.

        A model that curates unprompted does not need to be nudged, so a
        self-directed write resets the counter the same way a review would.
        """
        for segment in turn_segments or []:
            if not isinstance(segment, dict):
                continue
            name = segment.get("name") or segment.get("tool") or segment.get("tool_name")
            if isinstance(name, str) and name in _MEMORY_WRITE_TOOL_NAMES:
                return True
        return False

    def _note_turn_for_memory_nudge(
        self,
        *,
        agent_id: str,
        session_key: str,
        input_mode: str,
        run_kind: str,
        no_memory_capture: bool,
        turn_segments: Any,
        prior_user_turns: int | None = None,
    ) -> bool:
        """Advance the nudge counter; return True when a review is due.

        The counter is held in memory rather than recomputed from history on
        every turn, because the history window caps: its length plateaus in
        exactly the long sessions the nudge exists for.

        In-memory state alone is not enough, though. A TurnRunner is rebuilt
        on every CLI invocation, and the gateway evicts idle agents, so a
        fresh runner starts at zero and a session that spans processes would
        never reach the interval -- the nudge would silently never fire. When
        no counter exists yet, *prior_user_turns* seeds it from the persisted
        transcript so the count survives the process that made it.
        """
        nudge_cfg = self._memory_nudge_config()
        if not self._memory_nudge_allowed(
            nudge_cfg=nudge_cfg,
            no_memory_capture=no_memory_capture,
            input_mode=input_mode,
            run_kind=run_kind,
        ):
            return False

        key = (agent_id, session_key)
        self._evict_nudge_counters_if_needed()
        interval = int(getattr(nudge_cfg, "interval", 0))
        prior = self._memory_nudge_counters.get(key)
        if prior is None:
            prior = self._hydrate_nudge_counter(
                interval=interval, prior_user_turns=prior_user_turns
            )
        count = prior + 1

        # Every user turn advances the counter, including one that wrote to
        # memory itself. A self-directed write saves the fact that was salient
        # in that turn; the review re-reads the whole conversation and
        # consolidates, and only the second one prunes and merges.
        #
        # A write still defers the review, because running one immediately
        # after the model curated is mostly wasted. But the deferral is
        # bounded: clearing the counter outright meant a model that saves on
        # most turns never reached the interval and was never reviewed at all,
        # and an unbounded hold has the same end state for a model that saves
        # on every turn. Past _NUDGE_MAX_DEFERRAL_INTERVALS the review runs
        # regardless.
        if (
            self._turn_used_memory_tool(turn_segments)
            and count < interval * _NUDGE_MAX_DEFERRAL_INTERVALS
        ):
            self._memory_nudge_counters[key] = count
            return False

        if count < interval:
            self._memory_nudge_counters[key] = count
            return False
        self._memory_nudge_counters[key] = 0
        return True

    @staticmethod
    def _hydrate_nudge_counter(
        *,
        interval: int,
        prior_user_turns: int | None,
    ) -> int:
        """Seed a missing counter from the number of user turns already stored.

        Modular arithmetic recovers the session's phase: 27 prior turns at an
        interval of 10 means 7 turns into the current cycle, so the next
        review is 3 turns away. This is an approximation -- a self-directed
        memory write earlier in the session shifted the real phase off the
        multiple -- but the cost of being wrong is nudging a few turns early
        or late, against the alternative of never nudging at all.
        """
        if interval <= 0 or not prior_user_turns or prior_user_turns <= 0:
            return 0
        # The current turn is counted by the caller, so exclude it here.
        return max(0, prior_user_turns - 1) % interval

    def forget_memory_nudge_counter(self, session_key: str) -> None:
        """Drop nudge counters for *session_key* so they do not outlive it.

        Sessions have no teardown hook reaching this far, so this is a
        courtesy for callers that do know a session ended; the size cap in
        ``_note_turn_for_memory_nudge`` is what actually bounds the dict.
        """
        for key in [k for k in self._memory_nudge_counters if k[1] == session_key]:
            self._memory_nudge_counters.pop(key, None)

    def _evict_nudge_counters_if_needed(self) -> None:
        """Keep the counter dict bounded in a long-lived gateway process.

        Nothing notifies this runner when a session ends, so without a cap
        the dict grows once per session for the life of the process. Losing a
        counter only costs a re-seed from the transcript on that session's
        next turn, so evicting the oldest half is cheap; insertion order
        approximates least-recently-started.
        """
        if len(self._memory_nudge_counters) <= _MAX_NUDGE_COUNTERS:
            return
        for key in list(self._memory_nudge_counters)[: _MAX_NUDGE_COUNTERS // 2]:
            self._memory_nudge_counters.pop(key, None)

    async def _run_memory_nudge_review(
        self,
        *,
        agent_id: str,
        session_key: str,
    ) -> None:
        """Run one background review turn asking the agent to curate memory.

        Runs as its own turn against the same session, so the review sees the
        conversation it is reviewing. `input_mode="system_event"` plus the
        `memory_nudge` run kind keep it out of turn capture and out of its own
        counter, so a review can never trigger another review.
        """
        nudge_cfg = self._memory_nudge_config()
        if nudge_cfg is None:
            return
        timeout = float(getattr(nudge_cfg, "timeout_seconds", 90.0))

        async def _drive() -> None:
            # The review writes to memory on the user's behalf with nobody
            # watching, so its outcome is recorded rather than discarded. A
            # memory the user cannot see is one they cannot correct, and a
            # silent review is indistinguishable from one that never ran --
            # which is exactly how the counter bug below stayed hidden.
            # Unattended and memory-only: the review must never be able to
            # block the user with an approval prompt. Without the allowlist
            # it inherited the full tool surface, and a model that "saved"
            # via apply_patch surfaced an out-of-workspace approval for a
            # background write the user never asked for.
            tool_context = ToolContext(
                interaction_mode=InteractionMode.UNATTENDED,
                agent_id=agent_id,
                session_key=session_key,
                workspace_dir=str(self._resolve_memory_source_dir(agent_id)),
                source_kind="memory_nudge",
                allowed_tools=set(_MEMORY_REVIEW_TOOL_NAMES),
            )
            writes: list[str] = []
            failures: list[str] = []
            async for event in self.run(
                message=_MEMORY_REVIEW_PROMPT,
                session_key=session_key,
                tool_context=tool_context,
                agent_id=agent_id,
                input_mode="system_event",
                run_kind="memory_nudge",
                no_memory_capture=True,
                persist_input=False,
                max_iterations=int(getattr(nudge_cfg, "max_iterations", 6)),
                input_provenance={"kind": "memory_nudge"},
            ):
                if (
                    getattr(event, "kind", "") != "tool_result"
                    or getattr(event, "tool_name", "") not in _MEMORY_WRITE_TOOL_NAMES
                ):
                    continue
                summary = _summarize_memory_write(getattr(event, "arguments", None))
                if getattr(event, "is_error", False):
                    failures.append(summary)
                else:
                    writes.append(summary)

            log.info(
                "memory_nudge.review_done",
                agent_id=agent_id,
                session_key=session_key,
                wrote=len(writes),
                failed=len(failures),
                entries=writes[:_MEMORY_REVIEW_LOG_MAX_ENTRIES],
                errors=failures[:_MEMORY_REVIEW_LOG_MAX_ENTRIES],
            )

        try:
            await asyncio.wait_for(_drive(), timeout=timeout)
        except TimeoutError:
            log.warning(
                "memory_nudge.review_timeout",
                agent_id=agent_id,
                session_key=session_key,
                timeout_seconds=timeout,
            )
        except Exception as exc:  # noqa: BLE001 — a failed review must not fail the turn
            log.warning(
                "memory_nudge.review_failed",
                agent_id=agent_id,
                session_key=session_key,
                error=str(exc),
            )

    @staticmethod
    def _capture_filter_matches(value: str | None, excluded_values: Any) -> bool:
        if not value:
            return False
        if isinstance(excluded_values, str):
            raw_patterns = [excluded_values]
        else:
            raw_patterns = list(excluded_values or [])
        normalized_value = _normalize_capture_kind(value)
        value_parts = {part for part in normalized_value.split("_") if part}
        for pattern in raw_patterns:
            if pattern is None:
                continue
            normalized_pattern = _normalize_capture_kind(str(pattern))
            if not normalized_pattern:
                continue
            if normalized_value == normalized_pattern or normalized_pattern in value_parts:
                return True
        return False

    @staticmethod
    def _input_provenance_kind(input_provenance: dict[str, Any] | None) -> str | None:
        if not isinstance(input_provenance, dict):
            return None
        kind = input_provenance.get("kind")
        return str(kind) if kind is not None and str(kind) else None

    @classmethod
    def _turn_memory_capture_allowed(
        cls,
        *,
        no_memory_capture: bool,
        input_mode: str,
        run_kind: str | None,
        input_provenance: dict[str, Any] | None,
        memory_config: Any | None,
    ) -> bool:
        if no_memory_capture or input_mode != "user":
            return False
        if memory_config is None:
            return True
        if cls._capture_filter_matches(
            run_kind,
            getattr(memory_config, "capture_excluded_run_kinds", []),
        ):
            return False
        provenance_kind = cls._input_provenance_kind(input_provenance)
        if cls._capture_filter_matches(
            provenance_kind,
            getattr(memory_config, "capture_excluded_provenance_kinds", []),
        ):
            return False
        return True

    def _get_session_lock(self, session_key: str) -> asyncio.Lock:
        """Return the per-session lock for *session_key* from the external provider.

        TurnRunner no longer owns an internal lock dict.  All per-session
        locks are managed by the provider supplied at construction
        (TaskRuntime._get_session_lock_for_turn for the gateway path, or the
        standalone provider for CLI paths).

        External callers (rpc_sessions.py, channel_dispatch.py) that call this
        directly receive the short write lock used for transcript/session state
        mutation. Gateway TaskRuntime uses a separate execution lock for the
        long-running turn lifecycle.
        """
        return self._session_lock_provider(session_key)

    def get_session_lock(self, session_key: str) -> asyncio.Lock:
        """Public lock-provider seam for RPC/session services."""
        return self._get_session_lock(session_key)

    def set_session_lock_provider(self, provider: Callable[[str], asyncio.Lock]) -> None:
        """Replace the lock provider at the gateway composition root."""
        self._session_lock_provider = provider

    @contextlib.asynccontextmanager
    async def _session_write_context(self, session_key: str) -> AsyncIterator[None]:
        lock = self.get_session_lock(session_key)
        bypass_only = _SESSION_LOCK_BYPASS_ONLY.get(None)
        if bypass_only is not None and id(lock) in bypass_only:
            async with lock:
                yield
            return
        yield

    def _new_turn_progress_writer(
        self,
        session_key: str,
        run_kind: str,
        segments: list[dict],
    ) -> TurnProgressWriter | None:
        """The turn's in-progress row writer, or ``None`` when none is kept.

        Internal turns persist nothing; a session manager that cannot replace
        a row (test doubles, older stores) gets no mid-turn rows either.
        """
        from agentos.engine.turn_runner.turn_finalizer_stage import _is_internal_turn

        if _is_internal_turn(run_kind) or not TurnProgressWriter.supported(self._session_manager):
            return None
        return TurnProgressWriter(
            session_key=session_key,
            session_manager=self._session_manager,
            segments=segments,
            write_context=self._session_write_context,
            tool_use_input=_persisted_tool_use_input,
        )

    async def _append_session_message(self, session_key: str, **append_kwargs: Any) -> Any:
        if self._session_manager is None:
            return None
        async with self._session_write_context(session_key):
            return await self._session_manager.append_message(
                session_key,
                **append_kwargs,
            )

    async def run(
        self,
        message: str,
        session_key: str,
        tool_context: ToolContext,
        agent_id: str = "main",
        model: str | None = None,
        attachments: list[dict] | None = None,
        timeout: float | None = None,
        max_iterations: int | None = None,
        iteration_timeout: float | None = None,
        tool_timeout: float | None = None,
        request_timeout: float | None = None,
        max_provider_retries: int | None = None,
        length_capped_continuations: int | None = None,
        input_mode: str = "user",
        persist_input: bool = False,
        input_provenance: dict[str, Any] | None = None,
        history_has_persisted_user: bool = True,
        fresh_user_session: bool | None = None,
        session_intent: str | None = None,
        semantic_message: str | None = None,
        run_kind: str = "default",
        heartbeat_ack_max_chars: int = 300,
        bootstrap_context_mode: str | None = None,
        no_memory_capture: bool = False,
        ingress_pipeline_steps: list[PipelineStepRecord] | None = None,
        router_control_replay_depth: int = 0,
        persisted_user_message_id: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Run one agent turn with full orchestration.

        Acquires per-session lock, then:
        1. Resolve provider (cloned selector — no shared state mutation)
        2. Build tools + handler from registry (filtered by tool_context)
        3. Assemble identity system prompt
        4. Run pre-turn pipeline (model routing, Pilot Router, skills, prompt cache)
        5. Load session history
        6. Construct and run Agent
        7. Persist assistant response to transcript
        """
        session_key = canonicalize_session_key(session_key)
        agent_id = normalize_agent_id(agent_id)
        lock = self.get_session_lock(session_key)
        effective_tool_context = replace(
            tool_context,
            session_key=session_key,
            tool_run_budget_key=f"{session_key}:{uuid.uuid4().hex}",
            router_control_config=getattr(self._config, "agentos_router", None),
            router_control_hold_store=self._router_control_hold_store,
            router_control_replay_depth=router_control_replay_depth,
            router_control_turn_hold_applied=False,
        )
        # Re-entry detection: check whether this call chain already serializes
        # the turn lifecycle. On the gateway path TaskRuntime marks ownership
        # while holding its execution lock, so TurnRunner skips the legacy
        # coarse lock. lock.locked() is intentionally NOT used because it cannot
        # distinguish owners under concurrent turns.
        current_task = asyncio.current_task()
        owner_map = _SESSION_LOCK_OWNER.get(None)
        _caller_holds_lock = owner_map is not None and id(lock) in owner_map
        if _caller_holds_lock:
            # Same call chain already serializes this turn.
            try:
                async for event in self._run_turn(
                    message,
                    session_key,
                    agent_id,
                    model,
                    attachments or [],
                    effective_tool_context,
                    timeout=timeout,
                    max_iterations=max_iterations,
                    iteration_timeout=iteration_timeout,
                    tool_timeout=tool_timeout,
                    request_timeout=request_timeout,
                    max_provider_retries=max_provider_retries,
                    length_capped_continuations=length_capped_continuations,
                    input_mode=input_mode,
                    persist_input=persist_input,
                    input_provenance=input_provenance,
                    history_has_persisted_user=history_has_persisted_user,
                    fresh_user_session=fresh_user_session,
                    session_intent=session_intent,
                    semantic_message=semantic_message,
                    run_kind=run_kind,
                    heartbeat_ack_max_chars=heartbeat_ack_max_chars,
                    bootstrap_context_mode=bootstrap_context_mode,
                    no_memory_capture=no_memory_capture,
                    ingress_pipeline_steps=ingress_pipeline_steps,
                    router_control_replay_depth=router_control_replay_depth,
                    persisted_user_message_id=persisted_user_message_id,
                ):
                    yield event
            finally:
                self.clear_compaction_turn_state(session_key)
        else:
            async with lock:
                # Record this Task as the lock owner in the ContextVar so that
                # any nested call to run() within the same Task can detect re-entry.
                _map: dict[int, asyncio.Task[Any]] = dict(owner_map or {})
                if current_task is not None:
                    _map[id(lock)] = current_task
                _token = _SESSION_LOCK_OWNER.set(_map)
                try:
                    async for event in self._run_turn(
                        message,
                        session_key,
                        agent_id,
                        model,
                        attachments or [],
                        effective_tool_context,
                        timeout=timeout,
                        max_iterations=max_iterations,
                        iteration_timeout=iteration_timeout,
                        tool_timeout=tool_timeout,
                        request_timeout=request_timeout,
                        max_provider_retries=max_provider_retries,
                        length_capped_continuations=length_capped_continuations,
                        input_mode=input_mode,
                        persist_input=persist_input,
                        input_provenance=input_provenance,
                        history_has_persisted_user=history_has_persisted_user,
                        fresh_user_session=fresh_user_session,
                        session_intent=session_intent,
                        semantic_message=semantic_message,
                        run_kind=run_kind,
                        heartbeat_ack_max_chars=heartbeat_ack_max_chars,
                        bootstrap_context_mode=bootstrap_context_mode,
                        no_memory_capture=no_memory_capture,
                        ingress_pipeline_steps=ingress_pipeline_steps,
                        router_control_replay_depth=router_control_replay_depth,
                        persisted_user_message_id=persisted_user_message_id,
                    ):
                        yield event
                finally:
                    self.clear_compaction_turn_state(session_key)
                    _SESSION_LOCK_OWNER.reset(_token)

    async def _run_turn(
        self,
        message: str,
        session_key: str,
        agent_id: str,
        model: str | None,
        attachments: list[dict],
        tool_context: ToolContext | None = None,
        timeout: float | None = None,
        max_iterations: int | None = None,
        iteration_timeout: float | None = None,
        tool_timeout: float | None = None,
        request_timeout: float | None = None,
        max_provider_retries: int | None = None,
        length_capped_continuations: int | None = None,
        input_mode: str = "user",
        persist_input: bool = False,
        input_provenance: dict[str, Any] | None = None,
        history_has_persisted_user: bool = True,
        fresh_user_session: bool | None = None,
        session_intent: str | None = None,
        semantic_message: str | None = None,
        run_kind: str = "default",
        heartbeat_ack_max_chars: int = 300,
        bootstrap_context_mode: str | None = None,
        no_memory_capture: bool = False,
        ingress_pipeline_steps: list[PipelineStepRecord] | None = None,
        router_control_replay_depth: int = 0,
        persisted_user_message_id: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Run one turn through ``TurnHarness`` (``engine/harness/turn.py``)."""
        request = TurnRequest(
            message=message,
            session_key=session_key,
            agent_id=agent_id,
            model=model,
            attachments=attachments,
            tool_context=tool_context,
            timeout=timeout,
            max_iterations=max_iterations,
            iteration_timeout=iteration_timeout,
            tool_timeout=tool_timeout,
            request_timeout=request_timeout,
            max_provider_retries=max_provider_retries,
            length_capped_continuations=length_capped_continuations,
            input_mode=input_mode,
            persist_input=persist_input,
            input_provenance=input_provenance,
            history_has_persisted_user=history_has_persisted_user,
            fresh_user_session=fresh_user_session,
            session_intent=session_intent,
            semantic_message=semantic_message,
            run_kind=run_kind,
            heartbeat_ack_max_chars=heartbeat_ack_max_chars,
            bootstrap_context_mode=bootstrap_context_mode,
            no_memory_capture=no_memory_capture,
            ingress_pipeline_steps=ingress_pipeline_steps,
            router_control_replay_depth=router_control_replay_depth,
            persisted_user_message_id=persisted_user_message_id,
        )
        # aclosing: abandoning this generator closes the harness run at once,
        # so its ``finally`` (budget release, progress detach) runs now.
        async with aclosing(self._harness.run_turn(request)) as events:
            async for event in events:
                yield event

    @staticmethod
    def _write_trace_event(
        kind: str,
        context: TraceContext,
        *,
        seq: int | None = None,
        attrs: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        try:
            write_trace_event(
                TraceEvent(
                    kind=kind,
                    context=context,
                    privacy="operational",
                    seq=seq,
                    attrs=attrs or {},
                    payload=payload or {},
                )
            )
        except Exception as exc:  # pragma: no cover - observability must not break turns
            log.debug("trace_event.write_failed", kind=kind, error=str(exc))

    def _emit_turn_event(
        self,
        kind: str,
        context: TraceContext | None,
        *,
        session_key: str,
        agent_id: str,
        turn_id: str | None = None,
        run_kind: str | None = None,
        input_mode: str | None = None,
        seq: int | None = None,
        attrs: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Fan a turn event out through the registered ``TurnHook`` chain."""

        if context is None:
            return
        hook_ctx = TurnHookContext(
            session_key=session_key,
            agent_id=agent_id,
            turn_id=turn_id,
            run_kind=run_kind,
            input_mode=input_mode,
            trace_context=context,
        )
        event = TurnEvent(
            kind=kind,
            seq=seq,
            attrs=dict(attrs or {}),
            payload=dict(payload or {}),
        )
        for hook in self._turn_hooks:
            try:
                hook.on_event(hook_ctx, event)
            except Exception as exc:  # noqa: BLE001 - hooks must not break turns
                log.warning(
                    "turn_hook.on_event_failed",
                    hook=getattr(hook, "name", type(hook).__name__),
                    kind=kind,
                    error=str(exc),
                )

    @staticmethod
    def _build_turn_call_source(
        tool_context: ToolContext | None,
        input_provenance: dict[str, Any] | None,
        *,
        run_kind: str | None = None,
    ) -> dict[str, Any]:
        """Build stable source metadata for raw call-log filtering."""

        source: dict[str, Any] = {}
        if tool_context is not None:
            source.update(
                {
                    "caller_kind": str(tool_context.caller_kind),
                    "channel_kind": tool_context.channel_kind,
                    "channel_id": tool_context.channel_id,
                    "sender_id": tool_context.sender_id,
                    "source_kind": tool_context.source_kind,
                    "source_name": tool_context.source_name,
                }
            )
        if run_kind:
            source["run_kind"] = run_kind
        if input_provenance:
            source["input_provenance"] = input_provenance
            provenance_kind = TurnRunner._input_provenance_kind(input_provenance)
            if provenance_kind:
                source["input_provenance_kind"] = provenance_kind
        return source

    async def _resolve_session_id_for_log(self, session_key: str) -> str | None:
        """Best-effort lookup of the transcript identity for observability."""

        if self._session_manager is None:
            return None
        try:
            if hasattr(self._session_manager, "get_session"):
                node = await self._session_manager.get_session(session_key)
            else:
                from agentos.gateway.session_services import get_session_storage

                storage = get_session_storage(self._session_manager)
                node = await storage.get_session(session_key) if storage is not None else None
        except Exception:
            return None
        session_id = getattr(node, "session_id", None)
        return session_id if isinstance(session_id, str) and session_id else None

    def _resolve_provider(self) -> tuple[Any | None, Any | None]:
        """Clone the selector and resolve provider (no shared state mutation)."""
        if self._provider_selector is None:
            return None, None
        cloned = self._provider_selector.clone()
        return cloned.resolve(), cloned

    def _check_spend_budget(self, session_key: str) -> tuple[bool, str | None]:
        """Evaluate configured ``[budgets]`` ceilings for this session.

        Returns ``(hard_stop, message)``. A budget check must never be the
        reason a turn fails, so any unexpected error fails open with a log
        line rather than blocking work the operator did not ask to block.
        """
        tracker = self._usage_tracker
        if tracker is None:
            return False, None
        budgets = getattr(self._config, "budgets", None) if self._config else None
        if budgets is None:
            return False, None
        check = getattr(tracker, "check_budget_limits", None)
        if not callable(check):
            return False, None
        try:
            hard_stop, message = check(session_key, budgets)
        except Exception as exc:  # noqa: BLE001 - budget checks must fail open
            log.warning(
                "turn_runner.budget_check_failed",
                session_key=session_key,
                error=str(exc),
            )
            return False, None
        return bool(hard_stop), message

    def _reserve_spend_budget(self, session_key: str) -> tuple[bool, str | None, str | None]:
        """Admit this turn against ``[budgets]`` and hold headroom for it.

        Returns ``(hard_stop, message, reservation_id)``. Like the plain check,
        this fails open: a tracker that cannot reserve must not be the reason a
        turn is refused. A tracker without the reservation API (older builds,
        and the stub trackers tests use) falls back to the check-only gate,
        which is the pre-reservation behaviour rather than an error.
        """
        tracker = self._usage_tracker
        if tracker is None:
            return False, None, None
        budgets = getattr(self._config, "budgets", None) if self._config else None
        if budgets is None:
            return False, None, None
        reserve = getattr(tracker, "reserve_turn_budget", None)
        if not callable(reserve):
            hard_stop, message = self._check_spend_budget(session_key)
            return hard_stop, message, None
        try:
            hard_stop, message, reservation_id = reserve(session_key, budgets)
        except Exception as exc:  # noqa: BLE001 - budget checks must fail open
            log.warning(
                "turn_runner.budget_reserve_failed",
                session_key=session_key,
                error=str(exc),
            )
            return False, None, None
        return bool(hard_stop), message, reservation_id

    def _release_spend_budget(self, reservation_id: str | None) -> None:
        """Give back the headroom held by :py:meth:`_reserve_spend_budget`."""
        if not reservation_id:
            return
        tracker = self._usage_tracker
        release = getattr(tracker, "release_turn_budget", None) if tracker is not None else None
        if not callable(release):
            return
        try:
            release(reservation_id)
        except Exception as exc:  # noqa: BLE001 - a stuck release must not fail the turn
            log.warning("turn_runner.budget_release_failed", error=str(exc))

    def _handle_runtime_warning(self, event: WarningEvent) -> WarningEvent:
        return event

    async def _persist_turn_error(
        self,
        session_key: str,
        event: ErrorEvent | None,
    ) -> None:
        """Best-effort durable transcript record for terminal turn errors."""
        if self._session_manager is None or event is None:
            return
        error_code, message = sanitize_agent_error(
            {
                "status": "failed",
                "terminal_reason": event.code,
                "error_class": event.code,
                "error_message": event.message,
            },
            fallback_error_class=event.code,
            fallback_error_message=event.message or "Unknown error",
        )
        event_code = (
            error_code
            if error_code in {"provider_request_too_large", "provider_output_truncated"}
            else event.code
        )
        outcome_details = turn_outcome_details(
            outcome_from_error(
                code=event_code,
                message=message,
                error_class=event_code,
            )
        )
        if event_code == "provider_output_truncated":
            transcript_message = build_terminal_reply(
                {
                    "status": "failed",
                    "terminal_reason": "output_truncated",
                    "error_class": event_code,
                    "error_message": message,
                }
            )
        else:
            transcript_message = f"Error: {message}"
        try:
            if event_code == "current_turn_context_exhausted":
                compact = getattr(self._session_manager, "compact", None)
                if callable(compact):
                    budget = int(
                        getattr(self._config, "context_budget_tokens", None)
                        or getattr(self._config, "context_window_tokens", None)
                        or 100_000
                    )
                    try:
                        maybe_summary = compact(session_key, budget)
                        if inspect.isawaitable(maybe_summary):
                            await maybe_summary
                    except Exception as exc:  # noqa: BLE001 - error append must still run
                        log.warning(
                            "turn_runner.error_compaction_failed",
                            session_key=session_key,
                            code=event_code,
                            error=str(exc),
                        )
            await self._append_session_message(
                session_key,
                role="system",
                content=transcript_message,
            )
            log.info(
                "turn_runner.error_persisted",
                session_key=session_key,
                code=event_code,
                **outcome_details,
            )
        except Exception as exc:  # noqa: BLE001 - persistence must not mask the original error
            log.warning(
                "turn_runner.error_persist_failed",
                session_key=session_key,
                code=event_code,
                **outcome_details,
                error=str(exc),
            )

    @staticmethod
    def _non_bool_number(value: Any) -> TypeGuard[int | float]:
        return not isinstance(value, bool) and isinstance(value, int | float)

    @staticmethod
    def _non_bool_int(value: Any) -> TypeGuard[int]:
        return not isinstance(value, bool) and isinstance(value, int)

    def _turn_policy(self) -> TurnPolicyResolver:
        # Built per call: tests and reloads swap the session manager and config.
        return TurnPolicyResolver(
            session_manager=self._session_manager, config=self._config, log=log
        )

    def _resolve_agent_runtime_timeout(self, session_key: str) -> float:
        """Whole-turn runtime timeout; ``0`` disables the runtime budget.

        The old ``llm_timeout_seconds`` setting remains a legacy runtime alias.
        """
        return self._turn_policy().runtime_timeout(
            session_key, default=_DEFAULT_AGENT_RUNTIME_TIMEOUT_SECONDS
        )

    def _resolve_agent_max_iterations(
        self,
        session_key: str,
        explicit: int | None = None,
    ) -> int:
        """Resolve model/tool loop budget for this turn."""
        policy = self._turn_policy().max_iterations(session_key, explicit)
        self._last_agent_max_iterations_source = policy.max_iterations_source
        return policy.max_iterations

    def _resolve_agent_iteration_timeout(
        self,
        session_key: str,
        explicit: float | None = None,
    ) -> float:
        """Per-iteration timeout: explicit > session > env > gateway config > default."""
        return self._turn_policy().iteration_timeout(session_key, explicit)

    def _resolve_agent_tool_timeout(
        self,
        session_key: str,
        explicit: float | None = None,
    ) -> float:
        """Per-tool execution timeout for this turn."""
        return self._turn_policy().tool_timeout(session_key, explicit)

    def _resolve_agent_request_timeout(
        self,
        session_key: str,
        explicit: float | None = None,
    ) -> float:
        """Single LLM request timeout (agent-runtime path); falls back to the LLM timeout."""
        return self._turn_policy().request_timeout(
            session_key, explicit, default=lambda: self._resolve_llm_timeout(session_key)
        )

    def _resolve_agent_max_provider_retries(
        self,
        session_key: str,
        explicit: int | None = None,
    ) -> int:
        """Max provider retries for this turn."""
        return self._turn_policy().max_provider_retries(session_key, explicit)

    def _resolve_turn_thinking(self, turn: Any) -> bool | ThinkingLevel:
        """Resolve explicit config thinking before agentos-router suggestions."""

        llm_cfg = getattr(self._config, "llm", None) if self._config else None
        explicit = getattr(llm_cfg, "thinking", None)
        parsed = self._parse_thinking_level(
            explicit,
            source="config",
        )
        if parsed is not None:
            return parsed
        if explicit is not None and str(explicit).strip():
            return False

        metadata = getattr(turn, "metadata", {}) or {}
        if not metadata.get("thinking_requested"):
            return False

        parsed = self._parse_thinking_level(
            metadata.get("thinking_level", "medium"),
            source="agentos_router",
        )
        if isinstance(parsed, ThinkingLevel):
            parsed = self._effort_by_tier(parsed, metadata)
        return parsed if parsed is not None else False

    def _effort_by_tier(self, level: ThinkingLevel, metadata: Mapping[str, Any]) -> ThinkingLevel:
        """Cap a router-chosen thinking level at the routed tier's ceiling.

        Router-owned levels are capped: the shipped tier level
        (``tier_default``) and the pilot controller's per-turn pick
        (``controller``), which is what the default ``pilot-v1`` strategy
        uses on every routed turn. User intent is never capped: an
        operator-configured tier level (``tier_config``), a router-control
        hold (the user picked the route), an explicit ``[llm] thinking``
        (handled before this) or a route the turn did not honor stay as they
        are. ``[agentos_router] effort_by_tier = false``
        turns it off. The model choice is never touched.
        """
        from agentos.router_tiers import effort_for_tier

        router_cfg = getattr(self._config, "agentos_router", None) if self._config else None
        if not getattr(router_cfg, "effort_by_tier", True):
            return level
        if metadata.get("router_control_hold_applied"):
            return level
        if metadata.get("thinking_level_source") not in _EFFORT_BY_TIER_SOURCES:
            return level
        if metadata.get("routing_applied") is False:
            return level
        tier = metadata.get("routed_tier")
        capped = ThinkingLevel(effort_for_tier(tier, level.value))
        if capped is not level:
            log.info(
                "turn_runner.effort_by_tier",
                tier=tier,
                thinking_level=capped.value,
                tier_level=level.value,
                source=metadata.get("thinking_level_source"),
            )
        return capped

    def _resolve_parallel_tool_calls(self) -> bool:
        """``[tools] parallel_calls``: ask providers for parallel tool calls."""

        tools_cfg = getattr(self._config, "tools", None) if self._config else None
        return bool(getattr(tools_cfg, "parallel_calls", True))

    def _resolve_loop_guard(self) -> str:
        """``[tools] loop_guard``: act on repeated no-progress tool calls."""

        tools_cfg = getattr(self._config, "tools", None) if self._config else None
        return str(getattr(tools_cfg, "loop_guard", "enforce") or "enforce")

    def _resolve_repetition_guard(self) -> RepetitionGuardSettings:
        """``[llm.repetition_guard]``: thresholds for the degenerate-output guard."""

        llm_cfg = getattr(self._config, "llm", None) if self._config else None
        return repetition_guard_settings_from(getattr(llm_cfg, "repetition_guard", None))

    @staticmethod
    def _parse_thinking_level(value: Any, *, source: str) -> bool | ThinkingLevel | None:
        if value is None:
            return None
        if isinstance(value, ThinkingLevel):
            return value
        if isinstance(value, bool):
            return value

        raw = str(value).strip().lower()
        if not raw:
            return None
        normalized = _THINKING_ALIASES.get(raw.replace("_", "-"), raw)
        try:
            return ThinkingLevel(normalized)
        except ValueError:
            log.warning("turn_runner.invalid_thinking_level", source=source, value=value)
            return None

    def _resolve_llm_timeout(self, session_key: str) -> float:
        """Resolve single provider-request timeout for this turn."""

        sm = self._session_manager
        if sm is not None and hasattr(sm, "get_session_config"):
            try:
                session_cfg = sm.get_session_config(session_key)
                if session_cfg is not None:
                    per_session = getattr(session_cfg, "llm_request_timeout_seconds", None)
                    if isinstance(per_session, int | float) and per_session > 0:
                        return float(per_session)
            except Exception:  # noqa: BLE001
                pass

        gw_timeout = getattr(self._config, "llm_request_timeout_seconds", None)
        if isinstance(gw_timeout, int | float) and gw_timeout > 0:
            return float(gw_timeout)
        return _DEFAULT_LLM_REQUEST_TIMEOUT_SECONDS

    def _build_tools(
        self,
        ctx: ToolContext | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[list, ToolHandler | None]:
        """Build tool definitions and handler from registry, filtered by ToolContext."""
        tools_config = getattr(self._config, "tools", None)
        if (tools_config is not None and not getattr(tools_config, "enabled", True)) or (
            self._tool_registry is None
        ):
            # No tools this turn: drop a deferred-tool surface left by an
            # earlier one, or the prompt would still advertise tool_search.
            from agentos.tools.deferred import forget_surface

            forget_surface(ctx.session_key if ctx is not None else None)
        if tools_config is not None and not getattr(tools_config, "enabled", True):
            if metadata is not None:
                metadata["tool_profile"] = "disabled"
            log.info("tool_context_disabled")
            return [], None
        if self._tool_registry is None:
            return [], None
        from agentos.tools.dispatch import build_tool_handler
        from agentos.tools.policy import apply_tool_policy_from_config
        from agentos.tools.registry import resolve_profile

        loaded_skills: list[Any] = []
        if self._skill_loader is not None:
            try:
                loaded_skills = list(self._skill_loader.load_all())
            except Exception:
                loaded_skills = []

        if ctx is not None:
            ctx = apply_tool_policy_from_config(
                ctx,
                available_tools=self._tool_registry.list_names(),
                config=self._config,
            )
            if ctx.tool_policy:
                from agentos.tools.policy import apply_tool_policy_layer

                hard_denied = set(ctx.denied_tools)
                ctx = apply_tool_policy_layer(
                    ctx,
                    ctx.tool_policy,
                    available_tools=self._tool_registry.list_names(),
                    hard_denied=hard_denied,
                )
            ctx = self._apply_runtime_capability_denies(ctx)
            ctx = self._apply_user_route_pin_denies(ctx)
            log.debug(
                "tool_policy.policy_pre",
                allowed_tool_count=len(self._tool_registry.to_tool_definitions(ctx)),
                denied_count=len(ctx.denied_tools),
                profile=resolve_profile(ctx).value,
            )
        log.info(
            "tool_context_created",
            caller_kind=ctx.caller_kind if ctx else "none",
            denied_count=len(ctx.denied_tools) if ctx else 0,
        )
        # Deferred tool loading: the request carries the always-loaded tools,
        # tool_search, and whatever the session has loaded through it.
        from agentos.tools.deferred import surface_for_turn

        deferred_surface = surface_for_turn(
            self._tool_registry,
            ctx,
            config=self._config,
            session_manager=self._session_manager,
        )
        tool_defs = (
            deferred_surface.tool_definitions()
            if deferred_surface is not None
            else self._tool_registry.request_tool_definitions(ctx, defer_loading=False)
        )
        profile = resolve_profile(ctx)
        log.debug(
            "tool_policy.profile_post",
            allowed_tool_count=len(tool_defs),
            denied_count=len(ctx.denied_tools) if ctx else 0,
            profile=profile.value,
        )
        if metadata is not None:
            metadata["tool_profile"] = profile.value
        known_skill_names = {
            skill.name
            for skill in loaded_skills
            if not getattr(skill, "disable_model_invocation", False)
        }
        tool_handler = build_tool_handler(
            self._tool_registry,
            ctx,
            known_skill_names=known_skill_names,
            tool_hooks=self._tool_hooks,
        )
        if deferred_surface is not None:
            tool_handler = deferred_surface.guard(tool_handler)
        return tool_defs, tool_handler

    def _filter_tool_defs_by_capability(self, tool_defs: list) -> list:
        """Compatibility shim; runtime capability filtering is resolved in ToolContext."""
        return tool_defs

    def _apply_user_route_pin_denies(self, ctx: ToolContext) -> ToolContext:
        """Withdraw ``router_control`` while the user has pinned a route.

        A user pin outranks the model's own routing, so leaving the tool on the
        surface would only invite calls that cannot take effect. Denying it
        instead removes the tool AND — because the Router Control prompt block
        is rendered only when the tool is in ``tool_defs`` — its target menu,
        so the model spends no tokens describing a lever it does not have.

        Only USER holds gate this. A hold the model set for itself through the
        tool must not then hide the tool, which would strand the session on a
        transient escalation with no way back.
        """

        session_key = ctx.session_key
        if not session_key:
            return ctx
        try:
            from agentos.session.keys import canonicalize_session_key

            hold = self._router_control_hold_store.get_user_hold(
                canonicalize_session_key(session_key)
            )
        except Exception:  # noqa: BLE001 - tool surfacing must survive a hold-store fault
            return ctx
        if hold is None:
            return ctx
        return replace(ctx, denied_tools=set(ctx.denied_tools) | {"router_control"})

    def _apply_runtime_capability_denies(self, ctx: ToolContext) -> ToolContext:
        from agentos.tools.policy import (
            ToolSurfaceCapabilities,
            detect_runtime_tool_surface_capabilities,
            resolve_runtime_tool_surface,
        )

        detected = detect_runtime_tool_surface_capabilities(
            channel_backing=(
                ctx.caller_kind in {CallerKind.CHANNEL, CallerKind.WEB} and bool(ctx.channel_id)
            )
        )
        capabilities = ToolSurfaceCapabilities(
            session_manager=getattr(self, "_session_manager", None) is not None,
            task_runtime=detected.task_runtime,
            scheduler=detected.scheduler,
            gateway_config=getattr(self, "_config", None) is not None,
            channel_backing=detected.channel_backing,
            image_generation=detected.image_generation,
            x_search=detected.x_search,
            browser=detected.browser,
            flows=detected.flows,
        )
        return resolve_runtime_tool_surface(ctx, capabilities=capabilities)

    @staticmethod
    def _extra_context_for_tool_context(ctx: ToolContext | None) -> dict[str, str]:
        if ctx is None or ctx.caller_kind is not CallerKind.SUBAGENT:
            return {}
        return {"Subagent Task Protocol": _SUBAGENT_TASK_PROTOCOL}

    @staticmethod
    def _merge_extra_prompt_context(
        base: dict[str, str] | None,
        extra: dict[str, str],
    ) -> dict[str, str] | None:
        if not extra:
            return base
        if base is None:
            return dict(extra)
        merged = dict(base)
        merged.update(extra)
        return merged

    @staticmethod
    def _render_volatile_block(
        daily_notes: dict[str, str] | None,
        workspace_files: dict[str, str] | None,
        extra_context: dict[str, str] | None,
        prompt_mode: str = "full",
        wrap_untrusted_workspace: bool = True,
    ) -> str:
        """Render per-turn / per-day volatile content as the dynamic suffix.

        Replaces three previously-cacheable blocks once carried by
        the prior ``identity/templates/system_prompt.j2`` template:

        1. ``## Recent Notes`` (daily_notes) — gated on prompt_mode != minimal.
        2. ``## Workspace Files (injected)`` — gated on prompt_mode != minimal,
           with SOUL.md / IDENTITY.md filtered out (parsed elsewhere into
           AgentProfile.identity).
        3. ``## <key>`` blocks for each ``extra_context`` entry (no gating).

        Each section's bytes match what the prior Jinja render produced for
        the same inputs.
        Sections are joined directly with no separator — adjacent ``\\n\\n``
        terminators in each section already provide the visual break, the
        same way the prior template rendered them inline. The final result
        is right-stripped of newlines so it slots cleanly into the dynamic
        suffix (``base + "\\n\\n" + suffix`` is reassembled downstream).
        """
        sections: list[str] = []

        # 1. ## Recent Notes (daily_notes), suppressed in minimal mode.
        if daily_notes and prompt_mode != "minimal":
            buf = "## Recent Notes\n\n"
            for filename, content in daily_notes.items():
                buf += f"### {filename}\n\n{content}\n\n"
            sections.append(buf)

        # 2. ## Workspace Files (injected), suppressed in minimal mode.
        # SOUL.md / IDENTITY.md are filtered (parsed elsewhere into
        # AgentProfile.identity); if every entry is filtered out, no header
        # is emitted at all so the volatile suffix doesn't carry a stranded
        # bare heading whose tuple-return would later trip downstream
        # consumers (empty-suffix invariant).
        if workspace_files and prompt_mode != "minimal":
            visible = {
                filename: content
                for filename, content in workspace_files.items()
                if filename not in ("SOUL.md", "IDENTITY.md")
            }
            if visible:
                buf = "## Workspace Files (injected)\n\n"
                # Filenames are masked as ``### Workspace Context N`` so the
                # template surface mirrors pilot's filename-non-exposure
                # convention (commit 93dfb8a). BOOTSTRAP.md is the exception:
                # it gets a named heading so the model recognizes it as a
                # one-shot setup ritual and removes the file on completion
                # (see identity/templates/bootstrap/BOOTSTRAP.md).
                context_index = 0
                for filename, content in visible.items():
                    if filename == "BOOTSTRAP.md":
                        buf += f"### One-Shot Workspace Bootstrap\n\n{content}\n\n"
                        continue
                    context_index += 1
                    rendered_content = (
                        injection_guard.wrap_untrusted(content, source=f"workspace:{filename}")
                        if wrap_untrusted_workspace
                        else content
                    )
                    buf += f"### Workspace Context {context_index}\n\n{rendered_content}\n\n"
                sections.append(buf)

        # 3. extra_context — emitted as ## <key> blocks regardless of mode.
        if extra_context:
            buf = ""
            for key, value in extra_context.items():
                buf += f"## {key}\n\n{value}\n\n"
            if buf:
                sections.append(buf)

        if not sections:
            return ""
        return "".join(sections).rstrip("\n")

    def _assemble_prompt(
        self,
        agent_id: str,
        tool_defs: list,
        session_key: str | None = None,
        semantic_message: str | None = None,
        extra_context: dict[str, str] | None = None,
        prompt_metadata: dict[str, Any] | None = None,
        bootstrap_context_mode: str | None = None,
        fresh_user_session: bool = False,
    ) -> str | tuple[str, str]:
        """Assemble identity system prompt via Jinja2 template.

        Uses frozen snapshot when available (keyed by agent_id + session_key),
        falls back to live disk reads for backwards compatibility.

        Returns ``str`` for the prompt-cache-stable case; returns
        ``(base, dynamic_context)`` only when daily notes, workspace files, or
        tool-context blocks need to stay outside the cacheable prefix.
        """
        from agentos.identity.parser import parse_agents, parse_identity, parse_soul
        from agentos.identity.prompt import assemble_system_prompt
        from agentos.identity.types import AgentIdentity, AgentProfile
        from agentos.identity.workspace import (
            filter_workspace_filenames_for_session,
            filter_workspace_files_for_session,
            load_workspace_files_budgeted_with_report,
        )

        configured_agent_name = getattr(self._config, "agent_name", None) if self._config else None
        agent_name = (
            configured_agent_name.strip()
            if isinstance(configured_agent_name, str) and configured_agent_name.strip()
            else None
        )
        bootstrap_workspace_dir = self._resolve_bootstrap_workspace_dir(agent_id)
        bootstrap_context_key = bootstrap_context_mode or "full"
        bootstrap_snap_key = (agent_id, session_key, bootstrap_context_key) if session_key else None
        bootstrap_snap = (
            self._bootstrap_snapshots.get(bootstrap_snap_key)
            if bootstrap_snap_key is not None
            else None
        )
        if bootstrap_snap is not None:
            workspace_files = dict(bootstrap_snap.workspace_files)
            visible_bootstrap_report = list(bootstrap_snap.report)
        else:
            safety_cfg = getattr(self._config, "safety", None) if self._config else None
            bootstrap_filenames = (
                ("HEARTBEAT.md",)
                if bootstrap_context_mode == "heartbeat_light"
                else filter_workspace_filenames_for_session(None, session_key)
            )
            if bootstrap_context_mode == "unattended":
                bootstrap_filenames = tuple(
                    name for name in bootstrap_filenames if name != "BOOTSTRAP.md"
                )
            elif bootstrap_context_mode == "stateless":
                bootstrap_filenames = tuple(
                    name for name in bootstrap_filenames if name == "TOOLS.md"
                )
            elif bootstrap_context_mode == "stateless_keep_project_rules":
                bootstrap_filenames = tuple(
                    name for name in bootstrap_filenames if name in {"AGENTS.md", "TOOLS.md"}
                )
            loaded_workspace_files, bootstrap_report = load_workspace_files_budgeted_with_report(
                str(bootstrap_workspace_dir),
                per_file_max_chars=self._resolve_bootstrap_max_chars(),
                total_max_chars=self._resolve_bootstrap_total_max_chars(),
                filenames=bootstrap_filenames,
                injection_scan_mode=getattr(safety_cfg, "injection_scan_mode", "report"),
            )
            workspace_files = filter_workspace_files_for_session(
                loaded_workspace_files,
                session_key,
            )
            subagents_cfg = getattr(self._config, "subagents", None) if self._config else None
            if (
                session_key
                and is_subagent_key(session_key)
                and getattr(subagents_cfg, "prompt_compact", False)
            ):
                workspace_files = {
                    name: content
                    for name, content in workspace_files.items()
                    if name in {"AGENTS.md", "TOOLS.md"}
                }
            visible_bootstrap_report = [
                report for report in bootstrap_report if report.filename in workspace_files
            ]
            if bootstrap_snap_key is not None:
                self._bootstrap_snapshots[bootstrap_snap_key] = BootstrapSnapshot(
                    workspace_files=dict(workspace_files),
                    report=list(visible_bootstrap_report),
                )
        memory_source_dir = self._resolve_memory_source_dir(agent_id)
        stateless_prompt = bootstrap_context_mode in {
            "stateless",
            "stateless_keep_project_rules",
        }
        private_memory_allowed = (
            False if stateless_prompt else allows_private_memory_prompt_injection(session_key)
        )

        # Use frozen snapshot if available, otherwise read from disk
        snap_key = (agent_id, session_key) if session_key else None
        snap = self._memory_snapshots.get(snap_key) if snap_key else None
        if not private_memory_allowed:
            memory_text = None
            daily = {}
        elif snap is not None:
            memory_text = snap.memory_md
            daily = snap.daily_notes
        else:
            daily = {}
            memory_text = self._load_memory_md(memory_source_dir)
        # The curated store now owns MEMORY.md / USER.md injection via the
        # ``## Memory`` block (usage header + sanitization). Drop the raw
        # copies from the volatile "Workspace Files" block so they are not
        # injected twice.
        #
        # IMPORTANT: this pop must NOT be gated on the rendered header being
        # present in ``memory_text``. When the curated user block gets
        # dropped whole by the inject_limit budget (see
        # ``_load_curated_memory_block``), ``memory_text`` no longer
        # contains "USER PROFILE (who the user is)" even though the curated
        # store still manages USER.md -- gating on that string would let the
        # RAW, UNSANITIZED USER.md re-enter the prompt via the
        # workspace-files block. Sanitization must not depend on injection
        # success, so pop unconditionally whenever the curated path was used
        # at all (i.e. private memory injection is allowed for this
        # session), independent of which blocks survived the budget.
        if memory_text and "MEMORY (your personal notes)" in memory_text:
            workspace_files.pop("MEMORY.md", None)
        if private_memory_allowed:
            workspace_files.pop("USER.md", None)
        # External memory provider STATIC block (Plan B). Joins the curated
        # memory blocks in the cacheable base prompt (provider identity /
        # capability text is stable across turns). No-op when no provider is
        # configured or the provider yields no block. Gated by private-memory
        # policy alongside the curated blocks so stateless prompts stay clean.
        if private_memory_allowed:
            provider_manager = self._provider_manager_for(agent_id)
            if provider_manager is not None:
                static_block = provider_manager.build_system_prompt()
                if static_block:
                    memory_text = (
                        f"{memory_text}\n\n{static_block}" if memory_text else static_block
                    )
        # Daily notes are not auto-injected, and are no longer read from disk
        # either: every load was immediately discarded here, so the directory
        # scan and file reads bought nothing.
        #
        # `daily_notes_omitted` reports the POLICY, not a count. Deriving it
        # from "how many did we drop" would flip it to False now that nothing
        # is loaded, and the decision log would go quiet about a policy that
        # is still in force -- the reader could no longer tell "notes were
        # suppressed" from "this workspace has no notes". The count is 0
        # because none were read, which is what the separate counter says.
        daily_notes_count_before_omit = len(daily)
        daily_notes_omitted = True
        daily = {}
        if prompt_metadata is not None:
            prompt_metadata["daily_notes_omitted"] = daily_notes_omitted
            prompt_metadata["daily_notes_count_before_omit"] = daily_notes_count_before_omit
            prompt_metadata["daily_notes_policy_reason"] = "auto_injection_disabled"
            if fresh_user_session:
                prompt_metadata["daily_notes_fresh_session_omitted"] = True
            prompt_metadata["memory_md_present"] = memory_text is not None
            prompt_metadata["injected_workspace_files_count"] = len(workspace_files)
            prompt_metadata["bootstrap_files"] = visible_bootstrap_report
            if not private_memory_allowed:
                prompt_metadata["memory_prompt_injection_skipped"] = (
                    "stateless" if stateless_prompt else "session-scope"
                )
            retrieval_metadata = self._effective_memory_retrieval_metadata(agent_id)
            prompt_metadata["retrieval_mode"] = retrieval_metadata.get("retrieval_mode")
            prompt_metadata["embedding_requested_provider"] = retrieval_metadata.get(
                "embedding_requested_provider"
            )
            prompt_metadata["embedding_effective_provider"] = retrieval_metadata.get(
                "embedding_effective_provider"
            )
            prompt_metadata["embedding_model"] = retrieval_metadata.get("embedding_model")
            prompt_metadata["memory_retrieval_vector_weight"] = retrieval_metadata.get(
                "vector_weight"
            )
            prompt_metadata["memory_retrieval_text_weight"] = retrieval_metadata.get("text_weight")
            prompt_metadata["memory_mode_fingerprint"] = retrieval_metadata

        soul_doc = parse_soul(workspace_files["SOUL.md"]) if "SOUL.md" in workspace_files else None
        identity_fields = (
            parse_identity(workspace_files["IDENTITY.md"])
            if "IDENTITY.md" in workspace_files
            else None
        )
        agents_doc = (
            parse_agents(workspace_files["AGENTS.md"]) if "AGENTS.md" in workspace_files else None
        )
        if agent_name is None and identity_fields is not None:
            agent_name = identity_fields.name
        prompt_mode = "full"
        tools_cfg = getattr(self._config, "tools", None)
        if getattr(tools_cfg, "profile", None) == "memory_only":
            prompt_mode = "minimal"

        agent_profile = AgentProfile(
            agent_id=agent_id,
            identity=AgentIdentity(
                name=agent_name,
                emoji=identity_fields.emoji if identity_fields else None,
                theme=identity_fields.theme if identity_fields else None,
                avatar=identity_fields.avatar if identity_fields else None,
                soul=soul_doc,
                identity_fields=identity_fields,
            ),
            agents_doc=agents_doc,
            workspace_files=workspace_files,
            prompt_mode=prompt_mode,
        )
        os_name = os.uname().sysname if hasattr(os, "uname") else platform.system()
        runtime_info = {
            "os": os_name,
            "shell": os.environ.get("SHELL", ""),
            "workspace_dir": str(bootstrap_workspace_dir),
        }
        # Both flags are stable for the life of the process (channel and
        # heartbeat config are boot-time) or for the session kind
        # (bootstrap_context_mode keys the snapshot), so gating on them does
        # not churn the cacheable base.
        channels_enabled = bool(getattr(getattr(self._config, "channels", None), "channels", None))
        unattended_context = bool(
            getattr(getattr(self._config, "heartbeat", None), "enabled", False)
        ) or bootstrap_context_mode in {"heartbeat_light", "unattended"}
        from agentos.tools.deferred import prompt_tool_names

        prompt_tools, deferred_prompt_tools = prompt_tool_names(tool_defs, session_key)
        base_prompt = assemble_system_prompt(
            agent_profile,
            tools=prompt_tools or None,
            deferred_tools=deferred_prompt_tools,
            memory=memory_text,
            runtime_info=runtime_info,
            docs_path=self._resolve_docs_path(),
            heartbeat_prompt=getattr(self._config, "heartbeat_prompt", None),
            channels_enabled=channels_enabled,
            unattended_context=unattended_context,
        )
        # Session-frozen content -- workspace files (snapshotted per session),
        # daily notes, project knowledge, the subagent protocol and the
        # router-control block (config) -- joins the cached system prompt: it
        # does not change from one request to the next, and carrying it with
        # every user turn would re-append tens of KB into the append-only
        # transcript. Only genuinely per-turn keys (memory recall, internal
        # event mode, ...) ride in the dynamic suffix, which the agent sends
        # as section-diffed trailing context of the user turn.
        frozen_extra = {
            key: value
            for key, value in (extra_context or {}).items()
            if key in _SESSION_FROZEN_EXTRA_CONTEXT_KEYS
        }
        turn_extra = {
            key: value
            for key, value in (extra_context or {}).items()
            if key not in _SESSION_FROZEN_EXTRA_CONTEXT_KEYS
        }
        wrap_untrusted_workspace = getattr(
            getattr(self._config, "safety", None),
            "wrap_untrusted_workspace",
            True,
        )
        frozen_blocks: list[str] = []
        frozen_block = self._render_volatile_block(
            daily_notes=daily,
            workspace_files=workspace_files,
            extra_context=frozen_extra,
            prompt_mode=prompt_mode,
            wrap_untrusted_workspace=wrap_untrusted_workspace,
        )
        if frozen_block:
            frozen_blocks.append(frozen_block)
        if tool_defs and any(getattr(td, "name", "") == "router_control" for td in tool_defs):
            router_block = render_router_control_prompt_block(
                getattr(self._config, "agentos_router", None)
            )
            if router_block:
                frozen_blocks.append(f"## Router Control\n\n{router_block}")
        if frozen_blocks:
            base_prompt = "\n\n".join([base_prompt, *frozen_blocks])

        dynamic_blocks: list[str] = []
        volatile_block = self._render_volatile_block(
            daily_notes=None,
            workspace_files=None,
            extra_context=turn_extra,
            prompt_mode=prompt_mode,
            wrap_untrusted_workspace=wrap_untrusted_workspace,
        )
        if volatile_block:
            dynamic_blocks.append(volatile_block)

        if dynamic_blocks:
            return base_prompt, "\n\n".join(dynamic_blocks)
        return base_prompt

    @staticmethod
    def _resolve_docs_path() -> str | None:
        return None

    def _resolve_memory_source_dir(self, agent_id: str):
        from agentos.agents.scope import resolve_agent_memory_source_dir

        source = getattr(getattr(self._config, "memory", None), "source", "state")
        return resolve_agent_memory_source_dir(agent_id, self._config, source=source)

    def _effective_memory_retrieval_metadata(self, agent_id: str) -> dict[str, str]:
        retrievers = self._memory_retrievers or {}
        for key in (agent_id, "main"):
            retriever = retrievers.get(key)
            metadata_fn = getattr(retriever, "effective_retrieval_metadata", None)
            if callable(metadata_fn):
                try:
                    metadata = metadata_fn()
                except Exception:
                    continue
                if isinstance(metadata, dict):
                    return {str(k): str(v) for k, v in metadata.items()}

        memory_cfg = getattr(self._config, "memory", None)
        configured_mode = str(getattr(memory_cfg, "retrieval_mode", "hybrid"))
        effective_mode = "fts_only" if configured_mode == "fts_only" else configured_mode
        return {
            "configured_retrieval_mode": configured_mode,
            "retrieval_mode": effective_mode,
            "embedding_requested_provider": "",
            "embedding_effective_provider": "",
            "embedding_model": "",
            "vector_weight": str(getattr(memory_cfg, "vector_weight", "")),
            "text_weight": str(getattr(memory_cfg, "text_weight", "")),
        }

    def _resolve_bootstrap_workspace_dir(self, agent_id: str):
        from agentos.agents.scope import resolve_agent_workspace_dir

        return resolve_agent_workspace_dir(agent_id, self._config)

    def _resolve_bootstrap_max_chars(self) -> int:
        value = getattr(self._config, "bootstrap_max_chars", None) if self._config else None
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return int(value)
        return 20_000

    def _resolve_bootstrap_total_max_chars(self) -> int:
        value = getattr(self._config, "bootstrap_total_max_chars", None) if self._config else None
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return int(value)
        return 50_000

    def _load_memory_md(self, workspace_dir: Any, max_chars: int | None = None) -> str | None:
        """Build the curated MEMORY.md + USER.md block for system-prompt injection.

        Runs the one-time free-form → §-entry migration, then loads a
        ``CuratedMemoryStore`` and returns its frozen snapshot blocks (usage
        header + per-entry threat sanitization) joined memory-then-user. Falls
        back to the legacy raw-file read only when the curated store yields no
        block (e.g. a ``memory.md`` lowercase file the store does not manage).
        """
        from pathlib import Path

        if max_chars is None:
            max_chars = getattr(getattr(self._config, "memory", None), "inject_limit", 6400)
        root = Path(workspace_dir)

        curated = self._load_curated_memory_block(root, max_chars=max_chars)
        if curated is not None:
            return curated

        # Legacy fallback: lowercase memory.md, or any file the curated store
        # does not treat as MEMORY.md.
        memory_file = root / "MEMORY.md"
        if not memory_file.is_file():
            memory_file = root / "memory.md"
        if not memory_file.is_file():
            return None
        try:
            content = memory_file.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        if not content:
            return None
        if len(content) > max_chars:
            return content[:max_chars] + "\n..."
        return content

    def _load_curated_memory_block(
        self, memory_dir: Any, *, max_chars: int | None = None
    ) -> str | None:
        """Return the joined curated MEMORY.md + USER.md snapshot block, or None.

        Migrates a pre-curated free-form MEMORY.md before the first load, then
        renders the store's frozen snapshot blocks. Returns None when neither
        store has any entries so the caller can apply its legacy fallback.

        Blocks are included whole, in priority order (memory block first, then
        user block): a block that would push the joined result past
        ``max_chars`` is dropped entirely rather than sliced mid-block, so the
        usage header inside a kept block always matches what was injected. The
        one exception is a pathological memory block that alone exceeds
        ``max_chars`` — that block is still sliced (legacy behavior) so a
        broken store still injects something, with a warning logged.
        """
        from pathlib import Path

        from agentos.memory.curated import CuratedMemoryStore
        from agentos.memory.curated_migration import migrate_freeform_memory_md

        memory_cfg = getattr(self._config, "memory", None)
        memory_limit = getattr(memory_cfg, "curated_memory_char_limit", 4000)
        user_limit = getattr(memory_cfg, "curated_user_char_limit", 2000)

        root = Path(memory_dir)
        try:
            migrate_freeform_memory_md(root, memory_limit)
        except OSError:
            pass

        store = CuratedMemoryStore(
            memory_dir=root,
            memory_char_limit=memory_limit,
            user_char_limit=user_limit,
        )
        store.load_from_disk()
        if store.load_failed:
            # The file exists but could not be read. Returning None here would
            # be indistinguishable from "no memory yet", and the caller freezes
            # that result for the whole session -- so one transient lock or
            # permission blip would blind the agent until the session ends,
            # silently. Raise so the snapshot is not frozen and the next turn
            # retries the read.
            raise MemorySourceUnreadableError(
                f"curated memory unreadable: {sorted(store.load_failed)}"
            )
        named_blocks = [
            (name, block)
            for name, block in (
                ("memory", store.snapshot_block("memory")),
                ("user", store.snapshot_block("user")),
            )
            if block
        ]
        if not named_blocks:
            return None

        if max_chars is None:
            return "\n\n".join(block for _, block in named_blocks)

        first_name, first_block = named_blocks[0]
        if len(first_block) > max_chars:
            # Pathological case: even the highest-priority block alone
            # overflows the limit. Fall back to a raw slice of that block
            # only, so the caller still gets something injected, rather than
            # dropping memory injection entirely.
            log.warning(
                "curated_memory.inject_truncated",
                block=first_name,
                block_chars=len(first_block),
                max_chars=max_chars,
            )
            return first_block[:max_chars] + "\n..."

        included: list[str] = [first_block]
        joined_len = len(first_block)
        for name, block in named_blocks[1:]:
            candidate_len = joined_len + len("\n\n") + len(block)
            if candidate_len > max_chars:
                # Drop this (and, implicitly, any lower-priority) block whole
                # rather than slicing mid-block.
                continue
            included.append(block)
            joined_len = candidate_len

        return "\n\n".join(included)

    async def _run_pipeline(
        self,
        message: str,
        session_key: str,
        provider: Any,
        cloned_selector: Any,
        tool_defs: list,
        base_prompt: str | tuple[str, str],
        attachments: list[dict],
        semantic_message: str | None = None,
        ingress_pipeline_steps: list[PipelineStepRecord] | None = None,
        prev_assistant_text: str | None = None,
        prev_assistant_usage: dict[str, Any] | None = None,
        history_user_texts: list[str] | None = None,
        flags_text_override: str | None = None,
        tool_context: ToolContext | None = None,
        normalization_metadata: dict[str, Any] | None = None,
    ) -> tuple[Any, Any]:
        """Run the pre-turn pipeline and re-resolve provider if model changed.

        Pre-seeds ``turn.metadata['pipeline_steps']`` with any
        ``ingress_pipeline_steps`` recorded by the turn-ingress helper
        (under DecisionLog ownership). The engine pipeline's
        ``setdefault`` then appends step records to the same list, so
        ``DecisionEntry`` ends up with ingress records first followed by
        engine pipeline records.
        """
        from agentos.agentos_router.llm_judge import DEFAULT_ROUTING_TIMEOUT_SECONDS
        from agentos.engine import steps as prompt_steps
        from agentos.engine.pipeline import TurnContext, run_pipeline
        from agentos.engine.steps.agentos_router import (
            commit_deferred_router_history,
        )

        router_cfg = getattr(self._config, "agentos_router", None)
        router_timeout = float(
            getattr(router_cfg, "routing_timeout_seconds", None) or DEFAULT_ROUTING_TIMEOUT_SECONDS
        )

        def _copy_router_turn(turn: TurnContext) -> TurnContext:
            metadata: dict[str, Any] = {}
            for key, value in turn.metadata.items():
                try:
                    metadata[key] = copy.deepcopy(value)
                except Exception:
                    metadata[key] = value
            pipeline_steps = metadata.get("pipeline_steps")
            if isinstance(pipeline_steps, list):
                metadata["pipeline_steps"] = list(pipeline_steps)
            metadata["_defer_agentos_router_history"] = True
            return replace(
                turn,
                tool_defs=list(turn.tool_defs),
                attachments=list(turn.attachments),
                metadata=metadata,
            )

        async def _bounded_apply_agentos_router(turn: TurnContext) -> TurnContext:
            async def _run_router_step() -> TurnContext:
                return await prompt_steps.apply_agentos_router(_copy_router_turn(turn))

            try:
                # On the long-lived router loop (``engine.router_loop``): a
                # blocking router still times out here and the turn fails open.
                routed = await asyncio.wait_for(
                    router_loop().run(_run_router_step()), timeout=router_timeout
                )
                return commit_deferred_router_history(routed)
            except TimeoutError as exc:
                raise TimeoutError(f"Pilot Router timed out after {router_timeout:g}s") from exc

        _bounded_apply_agentos_router.__name__ = "apply_agentos_router"

        initial_metadata: dict[str, Any] = {
            "skill_loader": self._skill_loader,
            "router_control_hold_store": self._router_control_hold_store,
            # Surface the resolved per-agent workspace so tools that run
            # exec_command find it without falling through to
            # default_workspace_dir(). Prefer tool_context.workspace_dir
            # (already resolved with the gateway config in rpc_sessions /
            # channel_dispatch / scheduler); fall back to resolving from
            # agent_id on the tool_context, then to an empty string.
            "bootstrap_workspace_dir": (
                getattr(tool_context, "workspace_dir", None)
                or (
                    str(
                        self._resolve_bootstrap_workspace_dir(
                            getattr(tool_context, "agent_id", "main") or "main"
                        )
                    )
                    if tool_context is not None
                    else ""
                )
            ),
        }
        if normalization_metadata is not None:
            initial_metadata["input_normalization"] = dict(normalization_metadata)
            material_tokens = normalization_metadata.get("material_estimated_tokens")
            if type(material_tokens) is int and material_tokens > 0:
                initial_metadata["material_estimated_tokens"] = material_tokens
        if ingress_pipeline_steps:
            initial_metadata["pipeline_steps"] = list(ingress_pipeline_steps)
        if prev_assistant_text:
            initial_metadata["router_prev_assistant_text"] = prev_assistant_text
        if prev_assistant_usage:
            initial_metadata["router_prev_assistant_usage"] = dict(prev_assistant_usage)
        if history_user_texts:
            initial_metadata["router_history_user_texts"] = list(history_user_texts)
        if flags_text_override:
            initial_metadata["router_flags_text_override"] = flags_text_override
        if tool_context is not None:
            initial_metadata["channel_kind"] = tool_context.channel_kind
            initial_metadata["channel_id"] = tool_context.channel_id

        turn = TurnContext(
            message=message,
            session_key=session_key,
            config=self._config,
            provider=provider,
            model="",
            tool_defs=tool_defs,
            system_prompt=base_prompt,
            attachments=attachments,
            metadata=initial_metadata,
            raw_message=semantic_message,
        )
        turn = await run_pipeline(
            turn,
            self._prompt_steps.resolve({"apply_agentos_router": _bounded_apply_agentos_router}),
        )

        # Apply routed model back to cloned selector (local, not shared)
        if turn.model and cloned_selector is not None:
            tier_fallbacks = _derive_router_tier_fallbacks(turn, cloned_selector)
            try:
                cloned_selector.override_model(turn.model, fallbacks=tier_fallbacks)
            except TypeError:
                cloned_selector.override_model(turn.model)
            provider = cloned_selector.resolve()

        return turn, provider

    async def _router_previous_assistant_context(
        self,
        session_key: str,
        *,
        exclude_last_user: bool = False,
    ) -> dict[str, Any]:
        """Return transcript context for the V4 router, excluding the current user turn.

        Reads the newest ``_ROUTER_CONTEXT_WINDOW_ENTRIES`` rows first: the
        context only needs the last few user texts and the last assistant
        text, and deserializing a long session's whole transcript on every
        turn is the expensive part. When the window does not hold all of
        that, the whole transcript is read as before, so the result is the
        same either way.
        """
        if self._session_manager is None:
            return {}
        get_recent = getattr(type(self._session_manager), "get_recent_transcript", None)
        if get_recent is not None:
            try:
                recent = list(
                    await self._session_manager.get_recent_transcript(
                        session_key, _ROUTER_CONTEXT_WINDOW_ENTRIES
                    )
                    or []
                )
            except Exception:  # noqa: BLE001 - fall back to the full read below
                recent = None
            if recent is not None:
                context, complete = self._router_context_from_entries(
                    recent, exclude_last_user=exclude_last_user
                )
                if complete or len(recent) < _ROUTER_CONTEXT_WINDOW_ENTRIES:
                    return context
        get_transcript = getattr(self._session_manager, "get_transcript", None)
        if not callable(get_transcript):
            return {}
        try:
            transcript = get_transcript(session_key)
            if inspect.isawaitable(transcript):
                transcript = await transcript
        except Exception:  # noqa: BLE001 - router context must never block a turn
            log.debug("turn_runner.router_context_failed", session_key=session_key)
            return {}
        context, _complete = self._router_context_from_entries(
            list(transcript or []), exclude_last_user=exclude_last_user
        )
        return context

    def _router_context_from_entries(
        self,
        entries: list[Any],
        *,
        exclude_last_user: bool,
    ) -> tuple[dict[str, Any], bool]:
        """Build the router context from ``entries`` (oldest first).

        The flag says whether ``entries`` held everything the context can use
        (a full set of recent user texts and an assistant text), i.e. whether
        any older rows could still change the result.
        """
        user_texts: list[str] = []
        for index, entry in enumerate(entries):
            if getattr(entry, "role", None) != "user":
                continue
            if exclude_last_user and index == len(entries) - 1:
                continue
            content = getattr(entry, "content", None)
            if not isinstance(content, str) or not content.strip():
                continue
            unpacked = self._maybe_unpack_attachments(content)
            text = unpacked.strip() if isinstance(unpacked, str) else content.strip()
            if len(text) > _ROUTER_HISTORY_USER_MAX_CHARS:
                text = text[-_ROUTER_HISTORY_USER_MAX_CHARS:]
            user_texts.append(text)

        context: dict[str, Any] = {}
        if user_texts:
            context["history_user_texts"] = user_texts[-_ROUTER_HISTORY_USER_MAX_TURNS:]
        users_complete = len(user_texts) >= _ROUTER_HISTORY_USER_MAX_TURNS

        for entry in reversed(entries):
            if getattr(entry, "role", None) != "assistant":
                continue
            content = getattr(entry, "content", None)
            if not isinstance(content, str) or not content.strip():
                continue
            text = content.strip()
            if len(text) > _ROUTER_PREV_ASSISTANT_MAX_CHARS:
                text = text[-_ROUTER_PREV_ASSISTANT_MAX_CHARS:]
            context["prev_assistant_text"] = text
            token_count = getattr(entry, "token_count", None)
            if (
                isinstance(token_count, int)
                and not isinstance(token_count, bool)
                and token_count > 0
            ):
                context["prev_assistant_usage"] = {"output_tokens": token_count}
            return context, users_complete
        return context, False

    def _resolve_prompt_config(self, turn: Any) -> tuple[str, list | None, str | None]:
        """Resolve final system prompt and cache breakpoints from pipeline output."""
        final_prompt = turn.system_prompt
        cache_breakpoints = None
        request_context_prompt = None

        if turn.metadata.get("cache_enabled") and isinstance(final_prompt, tuple):
            base, dynamic = final_prompt
            cache_breakpoints = [{"text": base, "cache": "true"}]
            final_prompt = base
            request_context_prompt = dynamic
        elif turn.metadata.get("cache_enabled") and isinstance(final_prompt, str):
            base = turn.metadata.get("cache_base_prompt") or final_prompt
            if isinstance(base, str) and base:
                cache_breakpoints = [{"text": base, "cache": "true"}]
        elif isinstance(final_prompt, tuple):
            final_prompt = "\n\n".join(final_prompt)

        return final_prompt, cache_breakpoints, request_context_prompt

    async def _record_checkpoint_before_compaction(
        self,
        session_key: str,
        transcript: Sequence[Any],
        *,
        turn_id: str,
        source: str,
    ) -> bool:
        if self._session_manager is None:
            return False
        method = getattr(type(self._session_manager), "record_memory_checkpoint", None)
        if method is None:
            method = getattr(
                getattr(self._session_manager, "__dict__", {}),
                "get",
                lambda *_: None,
            )("record_memory_checkpoint")
        if not callable(method):
            return False
        async with self._session_write_context(session_key):
            receipt = await self._session_manager.record_memory_checkpoint(
                session_key,
                list(transcript),
                turn_id=turn_id,
                source=source,
            )
        return durable_receipt_allows_destructive_compaction(receipt)

    def _emit_decision_entry(
        self,
        *,
        turn_id: str,
        session_key: str,
        session_id: str | None = None,
        message: str,
        final_prompt: str,
        tool_defs: list[Any],
        turn_obj: Any | None,
        provider: Any | None,
        resolved_model: str,
        turn_started_at: float,
        prompt_report: PromptReport | None = None,
        session_intent: str | None = None,
        done_event: DoneEvent | None = None,
        trace_id: str | None = None,
        skills_invoked: list[str] | None = None,
        timing: dict[str, Any] | None = None,
    ) -> None:
        """Write one DecisionEntry for this turn (best-effort, never raises).

        Pipeline steps are read off ``turn_obj.metadata['pipeline_steps']``
        (populated by :func:`pipeline.run_pipeline`). Token counts are pulled
        from ``usage_tracker`` when available; otherwise default to 0.
        """

        try:
            tool_names = [getattr(td, "name", "") for td in tool_defs]
            prompt_hash, system_prompt_hash, tool_list_hash = compute_hashes(
                message, final_prompt, [n for n in tool_names if n]
            )

            pipeline_steps: list[PipelineStepRecord] = []
            if turn_obj is not None:
                pipeline_steps = list(turn_obj.metadata.get("pipeline_steps", []))

            # Per-turn token counts come from the final DoneEvent (which carries
            # cumulative input_tokens / output_tokens for the whole turn). The
            # legacy code looked up `usage_tracker.last_input_tokens`, but
            # UsageTracker exposes only per-session aggregates and never had
            # `last_input_tokens` / `last_output_tokens` attributes — the
            # getattr defaults silently produced zero on every turn. See
            # engine/usage.py for the actual UsageTracker surface.
            if done_event is not None:
                tokens_input = int(done_event.input_tokens or 0)
                tokens_output = int(done_event.output_tokens or 0)
            else:
                tokens_input = 0
                tokens_output = 0

            latency_ms = int((time.monotonic() - turn_started_at) * 1000)
            ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            tool_choice = "auto" if tool_defs else "none"
            provider_name = type(provider).__name__ if provider is not None else ""

            # Populate SavingsTelemetry
            savings_telemetry = SavingsTelemetry()
            if turn_obj is not None:
                metadata = turn_obj.metadata
                router_cfg = getattr(self._config, "agentos_router", None)
                agentos_router_tiers = getattr(router_cfg, "tiers", {})

                # Pilot Router
                savings_telemetry.routed_model = metadata.get("routed_model")
                savings_telemetry.baseline_model = metadata.get("baseline_model")
                savings_telemetry.routing_confidence = metadata.get("routing_confidence")
                savings_telemetry.routing_savings_pct = metadata.get("savings_pct")

                _max_p = float(metadata.get("savings_max_price_per_m") or 0.0)
                _rte_p = float(metadata.get("savings_routed_price_per_m") or 0.0)
                if done_event is not None:
                    savings_telemetry.routing_savings_usd_estimated_vs_baseline = (
                        _compute_route_input_savings_usd(
                            _max_p,
                            _rte_p,
                            done_event.input_tokens,
                        )
                    )

                # Tool-result projection (values will be set in agent.py)
                savings_telemetry.tool_projection_applied = metadata.get(
                    "tool_projection_applied",
                    False,
                )
                savings_telemetry.tool_projection_calls = metadata.get("tool_projection_calls", 0)
                savings_telemetry.tool_projection_tokens_before = metadata.get(
                    "tool_projection_tokens_before",
                    0,
                )
                savings_telemetry.tool_projection_tokens_after = metadata.get(
                    "tool_projection_tokens_after",
                    0,
                )
                savings_telemetry.tool_projection_tokens_saved = metadata.get(
                    "tool_projection_tokens_saved",
                    0,
                )
                savings_telemetry.tool_result_store_writes = metadata.get(
                    "tool_result_store_writes",
                    0,
                )
                savings_telemetry.tool_result_store_skips = metadata.get(
                    "tool_result_store_skips",
                    0,
                )

                # Thinking mode
                savings_telemetry.thinking_mode = metadata.get("thinking_mode")

                # Short-reply prompt enforcement
                savings_telemetry.short_reply_active = metadata.get("prompt_policy") == "P0"
                if savings_telemetry.short_reply_active and done_event is not None:
                    estimated_output_savings_pct = getattr(
                        router_cfg,
                        "estimated_output_savings_pct",
                        0.03,
                    )
                    output_side_tokens = _non_negative_int(
                        done_event.output_tokens
                    ) + _non_negative_int(done_event.reasoning_tokens)
                    restored_output_tokens = _restored_output_side_tokens(
                        output_side_tokens,
                        metadata,
                        estimated_output_savings_pct,
                    )
                    savings_telemetry.short_reply_savings_tokens_estimated = round(
                        max(0.0, restored_output_tokens - output_side_tokens)
                    )
                    baseline = _select_savings_baseline_model(
                        agentos_router_tiers,
                        _non_negative_int(done_event.input_tokens)
                        + _non_negative_int(
                            metadata.get("tool_projection_tokens_saved"),
                        ),
                        restored_output_tokens,
                    )
                    if baseline.price.output_per_m > 0:
                        savings_telemetry.short_reply_savings_usd_estimated_vs_baseline = round(
                            (savings_telemetry.short_reply_savings_tokens_estimated / 1_000_000)
                            * baseline.price.output_per_m,
                            6,
                        )

                # Cache Hit — fires when EITHER AgentOS's prompt-cache split
                # infra reports a hit OR the upstream provider returns
                # `cached_tokens > 0` (OpenRouter prompt-cache passthrough).
                # Without the OR, provider-side cache hits were silently
                # losing the active flag while still recording tokens_saved.
                provider_cache_hit = done_event is not None and (done_event.cached_tokens or 0) > 0
                agentos_cache_hit = metadata.get("cache_mode") == "hit"
                event_cache_hit = bool(getattr(done_event, "cache_hit_active", False))
                savings_telemetry.cache_hit_active = (
                    event_cache_hit or provider_cache_hit or agentos_cache_hit
                )
                if done_event is not None:
                    savings_telemetry.cache_hit_tokens_saved = done_event.cached_tokens
                    if savings_telemetry.cache_hit_tokens_saved > 0 and _max_p > 0:
                        savings_telemetry.cache_hit_usd_estimated_vs_baseline = round(
                            (savings_telemetry.cache_hit_tokens_saved / 1_000_000) * _max_p, 6
                        )

                savings_telemetry.billed_cost_usd = (
                    done_event.billed_cost if done_event is not None else None
                )
                savings_telemetry.cost_usd = done_event.cost_usd if done_event is not None else None
                savings_telemetry.cost_source = (
                    normalize_event_cost_source(
                        done_event.cost_source,
                        input_tokens=done_event.input_tokens,
                        output_tokens=done_event.output_tokens,
                        cache_read_tokens=done_event.cached_tokens,
                        cache_write_tokens=done_event.cache_write_tokens,
                        cost_usd=done_event.cost_usd,
                        billed_cost_usd=done_event.billed_cost,
                    )
                    if done_event is not None
                    else None
                )

                # Total savings is the comprehensive per-turn estimate used by
                # the popup. It intentionally excludes billed-cost and cache-hit
                # effects so it remains a token/price estimate.
                if done_event is not None:
                    savings_telemetry.total_savings_pct = done_event.total_savings_pct
                    savings_telemetry.total_savings_usd = done_event.total_savings_usd

            entry = DecisionEntry(
                turn_id=turn_id,
                session_key=session_key,
                session_id=session_id,
                session_intent=session_intent,
                intent_summary=build_intent_summary(message),
                trace_id=trace_id or turn_id,
                tool_profile=prompt_report.tool_profile if prompt_report else None,
                prompt_hash=prompt_hash,
                system_prompt_hash=system_prompt_hash,
                tool_list_hash=tool_list_hash,
                tool_choice=tool_choice,
                tokens_input=tokens_input,
                tokens_output=tokens_output,
                model=resolved_model,
                provider=provider_name,
                latency_ms=latency_ms,
                ts=ts,
                skills_invoked=skills_invoked if skills_invoked is not None else [],
                pipeline_steps=pipeline_steps,
                savings=savings_telemetry,
                system_chars=prompt_report.system_chars if prompt_report else 0,
                tool_count=prompt_report.tool_count if prompt_report else 0,
                tools_schema_chars=prompt_report.tools_schema_chars if prompt_report else 0,
                skill_count=prompt_report.skill_count if prompt_report else 0,
                skills_prompt_chars=prompt_report.skills_prompt_chars if prompt_report else 0,
                skills_render_mode=(prompt_report.skills_render_mode if prompt_report else None),
                skills_description_max_chars=(
                    prompt_report.skills_description_max_chars if prompt_report else None
                ),
                memory_md_present=prompt_report.memory_md_present if prompt_report else False,
                daily_notes_omitted=(prompt_report.daily_notes_omitted if prompt_report else False),
                daily_notes_count_before_omit=(
                    prompt_report.daily_notes_count_before_omit if prompt_report else 0
                ),
                daily_notes_policy_reason=(
                    prompt_report.daily_notes_policy_reason if prompt_report else None
                ),
                injected_workspace_files_count=(
                    prompt_report.injected_workspace_files_count if prompt_report else 0
                ),
                bootstrap_files=prompt_report.bootstrap_files if prompt_report else [],
                memory_mode_fingerprint=(
                    prompt_report.memory_mode_fingerprint if prompt_report else {}
                ),
                retrieval_mode=prompt_report.retrieval_mode if prompt_report else None,
                cache_mode=prompt_report.cache_mode if prompt_report else None,
                cache_base_hash=prompt_report.cache_base_hash if prompt_report else None,
                cache_dynamic_hash=(prompt_report.cache_dynamic_hash if prompt_report else None),
                cache_read_input_tokens=(
                    int(done_event.cached_tokens or 0) if done_event is not None else 0
                ),
                cache_creation_input_tokens=(
                    int(done_event.cache_write_tokens or 0) if done_event is not None else 0
                ),
                resolved_model=(prompt_report.resolved_model if prompt_report else None)
                or resolved_model,
                alias_resolution_chain=(
                    prompt_report.alias_resolution_chain
                    if prompt_report and prompt_report.alias_resolution_chain
                    else ([resolved_model] if resolved_model else [])
                ),
                provider_after_rewrite=(
                    prompt_report.provider_after_rewrite if prompt_report else None
                )
                or provider_name,
                cache_legacy_hash=prompt_report.cache_legacy_hash if prompt_report else None,
                cache_shadow_final_hash=(
                    prompt_report.cache_shadow_final_hash if prompt_report else None
                ),
                cache_key_collision=(prompt_report.cache_key_collision if prompt_report else False),
                reasoning_hint_resolved=(
                    prompt_report.reasoning_hint_resolved if prompt_report else None
                ),
                cache_base_chars=prompt_report.cache_base_chars if prompt_report else 0,
                cache_dynamic_chars=prompt_report.cache_dynamic_chars if prompt_report else 0,
                runtime_context_hash=(
                    done_event.runtime_context_hash if done_event is not None else None
                ),
                runtime_context_chars=(
                    done_event.runtime_context_chars if done_event is not None else 0
                ),
                stop_reason=done_event.stop_reason if done_event is not None else None,
                refusal_category=(
                    _refusal_category_from_details(done_event.stop_details)
                    if done_event is not None and done_event.stop_reason == "refusal"
                    else None
                ),
                timing=timing or {},
            )
            write_decision_entry(entry)
        except Exception as exc:  # pragma: no cover — observability must not break turns
            log.warning("decision_log.write_failed", error=str(exc))

    def _pre_compaction_flush_timeout_seconds(self) -> float:
        memory_cfg = getattr(self._config, "memory", None)
        raw_timeout = getattr(memory_cfg, "flush_timeout_seconds", 15.0)
        try:
            timeout = float(raw_timeout)
        except (TypeError, ValueError):
            return 15.0
        return max(timeout, 0.0)

    def _pre_compaction_flush_background_timeout_seconds(self) -> float:
        memory_cfg = getattr(self._config, "memory", None)
        raw_timeout = getattr(memory_cfg, "flush_background_timeout_seconds", 120.0)
        try:
            timeout = float(raw_timeout)
        except (TypeError, ValueError):
            return 120.0
        return max(timeout, 0.0)

    def _consume_pre_compaction_flush_task(
        self,
        session_key: str,
        task: asyncio.Task,
        event_prefix: str,
        *,
        background: bool = False,
        compaction_id: str | None = None,
    ) -> None:
        if self._active_pre_compaction_flush_tasks.get(session_key) is not task:
            return
        self._active_pre_compaction_flush_tasks.pop(session_key, None)
        try:
            receipt = task.result()
        except asyncio.CancelledError:
            log.debug(f"{event_prefix}.flush_cancelled", session_key=session_key)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                f"{event_prefix}.flush_failed",
                session_key=session_key,
                error=str(exc),
                background=background,
            )
            if background and compaction_id:
                self._schedule_pre_compaction_flush_status_update(
                    session_key,
                    compaction_id,
                    "failed_retryable",
                    event_prefix,
                )
        else:
            self._log_pre_compaction_flush_receipt(
                event_prefix,
                session_key,
                receipt,
                duration_ms=getattr(receipt, "duration_ms", 0),
                background=background,
            )
            if background and compaction_id:
                self._schedule_pre_compaction_flush_status_update(
                    session_key,
                    compaction_id,
                    flush_receipt_status_for_compaction(receipt, self._config),
                    event_prefix,
                )

    def _schedule_pre_compaction_flush_status_update(
        self,
        session_key: str,
        compaction_id: str,
        status: str,
        event_prefix: str,
    ) -> None:
        if self._session_manager is None:
            return
        mark_status = getattr(self._session_manager, "mark_compaction_flush_receipt_status", None)
        if not callable(mark_status):
            return
        task = asyncio.create_task(
            mark_compaction_flush_status_with_retry(
                mark_status,
                session_key=session_key,
                compaction_id=compaction_id,
                status=status,
                log=log,
                failed_event=f"{event_prefix}.flush_status_update_failed",
                updated_event=f"{event_prefix}.flush_status_updated",
                skipped_event=f"{event_prefix}.flush_status_update_skipped",
            )
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _log_pre_compaction_flush_receipt(
        self,
        event_prefix: str,
        session_key: str,
        receipt: Any,
        *,
        duration_ms: int,
        background: bool,
    ) -> None:
        result_status = getattr(receipt, "result_status", None)
        if flush_receipt_is_successful_flush(receipt):
            log.info(
                f"{event_prefix}.flush_done",
                session_key=session_key,
                mode=getattr(receipt, "mode", "unknown"),
                result_status=result_status,
                message_count=getattr(receipt, "message_count", 0),
                duration_ms=duration_ms,
                background=background,
            )
            return

        log.warning(
            f"{event_prefix}.flush_degraded",
            session_key=session_key,
            error=getattr(receipt, "error", None) or "degraded_flush_receipt",
            mode=getattr(receipt, "mode", "unknown"),
            result_status=result_status,
            integrity_status=getattr(receipt, "integrity_status", None),
            indexed_chunk_count=getattr(receipt, "indexed_chunk_count", None),
            output_coverage_status=getattr(receipt, "output_coverage_status", None),
            invalid_candidate_count=getattr(receipt, "invalid_candidate_count", None),
            candidate_missing_ids=getattr(receipt, "candidate_missing_ids", None),
            obligation_status=getattr(receipt, "obligation_status", None),
            obligation_missing_ids=getattr(receipt, "obligation_missing_ids", None),
            background=background,
        )

    @staticmethod
    def _receipt_value(receipt: Any, name: str, default: Any) -> Any:
        if isinstance(receipt, Mapping):
            return receipt.get(name, default)
        return getattr(receipt, name, default)

    @staticmethod
    def _receipt_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def _flush_receipt_allows_destructive_compaction(self, receipt: Any) -> bool:
        return flush_receipt_allows_destructive_compaction(receipt)

    def _apply_compaction_summary_settings(self, agent: Any) -> None:
        """Point the in-turn summarizer at the cheap side-task model."""
        from agentos.gateway.session_titler import fast_model_hint

        compaction_cfg = getattr(self._config, "compaction", None)
        try:
            timeout = float(getattr(compaction_cfg, "summary_timeout_seconds", 45.0) or 45.0)
        except (TypeError, ValueError):
            timeout = 45.0
        agent.config.compaction_summary_timeout_seconds = timeout
        try:
            agent.config.compaction_summary_model_hint = fast_model_hint(
                self._config, task="compaction"
            )
        except Exception:  # noqa: BLE001 - a hint is optional
            agent.config.compaction_summary_model_hint = ("", "")

    def _preflight_compact_ratio(self) -> float:
        raw_ratio = getattr(self._config, "preflight_compact_ratio", None)
        if raw_ratio is None:
            return _DEFAULT_PREFLIGHT_COMPACT_RATIO
        try:
            ratio = float(raw_ratio)
        except (TypeError, ValueError):
            return _DEFAULT_PREFLIGHT_COMPACT_RATIO
        if ratio <= 0.0 or ratio > 1.0:
            return _DEFAULT_PREFLIGHT_COMPACT_RATIO
        return ratio

    async def _load_history(
        self,
        agent: Agent,
        session_key: str,
        *,
        trim_last_user: bool = True,
        input_message_id: str | None = None,
    ) -> str | None:
        """Load existing transcript as agent history.

        *input_message_id* names the persisted user row this turn answers
        (``sessions.send`` / the input stage persist it before the run). It is
        taken out of the history wherever it sits -- a follow-up queued while
        the previous turn ran is persisted before that turn's reply -- and
        recorded as the turn's input. Without it the trailing user row is
        trimmed, as before.
        """
        if self._session_manager is None:
            return None

        transcript = await self._session_manager.get_transcript(session_key)
        await self._remember_compaction_snapshot(session_key, transcript)

        from agentos.engine.transcript import (
            build_history_from_transcript,
            legacy_migration_for,
        )

        summary_markers: list[str] = []
        # Structured rows replay their provider messages verbatim; rows written
        # before the structured record existed go through the legacy
        # reconstruction plus a one-time deterministic migration.
        loaded = build_history_from_transcript(
            transcript,
            unpack_user=self._maybe_unpack_attachments,
            unpack_assistant=self._maybe_unpack_assistant_artifacts,
            summary_marker=_CONTEXT_SUMMARY_MARKER,
            strip_summary_marker=_strip_context_summary_marker,
            legacy_migrate=legacy_migration_for(agent),
            current_input_id=input_message_id,
        )
        history = loaded.messages
        summary_markers.extend(loaded.summary_markers)
        input_entry: Any = loaded.input_entry
        # Strip the caller-appended user turn only when the transcript really
        # ended on a user entry; an assistant entry that reconstructs into
        # assistant + user(tool_result) must keep its tool_result tail.
        if (
            input_entry is None
            and trim_last_user
            and loaded.last_entry_was_user
            and history
            and history[-1].role == "user"
        ):
            history.pop()
            input_entry = loaded.last_user_entry
        remember_input = getattr(agent, "set_transcript_input_entry_id", None)
        if callable(remember_input):
            input_entry_id = getattr(input_entry, "message_id", None)
            input_content = getattr(input_entry, "content", None)
            remember_input(
                input_entry_id if isinstance(input_entry_id, str) else None,
                input_content if isinstance(input_content, str) else None,
            )
        context_states = await self._load_context_states(session_key)
        reset_entry_id = loaded.reset_entry_id
        if reset_entry_id is not None:
            # The history starts from a compaction record that already carries
            # its summary; older durable summaries/states are inside it.
            context_states = [
                state
                for state in context_states
                if int(getattr(state, "covered_through_id", 0) or 0) >= reset_entry_id
            ]
        provider = getattr(agent, "provider", None)
        provider_context = build_provider_compaction_context(
            context_states=context_states,
            provider_kind=str(getattr(provider, "provider_name", "")),
        )
        if provider_context.messages:
            history = provider_context.messages + history
        if history:
            agent.set_history(history)
        return await self._compaction_summary_context(
            session_key,
            summary_markers,
            context_states=context_states,
            skip_covered_through_ids=provider_context.covered_through_ids,
            min_covered_through_id=reset_entry_id,
        )

    async def _remember_compaction_snapshot(
        self, session_key: str, transcript: list[Any] | None
    ) -> None:
        """Record which persisted rows the agent's history is built from.

        ``persist_compaction_result`` anchors an inline compaction on it so a
        follow-up ``sessions.send`` appends after this point is re-appended
        verbatim instead of being overwritten by the agent's stale kept tail.
        An empty transcript still records the session so a later compaction
        of purely in-memory messages treats every persisted row as newer.
        """
        snapshots = getattr(self, "_compaction_snapshots", None)
        if snapshots is None:
            return
        snapshots.pop(session_key, None)
        last = transcript[-1] if transcript else None
        session_id = getattr(last, "session_id", None)
        message_id = getattr(last, "message_id", None)
        if session_id and message_id:
            snapshots[session_key] = TranscriptSnapshot(
                session_id=str(session_id), through_message_id=str(message_id)
            )
            return
        if last is not None:
            return
        get_session = getattr(self._session_manager, "get_session", None)
        if not callable(get_session):
            return
        try:
            node = get_session(session_key)
            if inspect.isawaitable(node):
                node = await node
        except Exception:  # noqa: BLE001 - the anchor is best-effort
            log.debug("compaction_snapshot.session_lookup_failed", session_key=session_key)
            return
        session_id = getattr(node, "session_id", None)
        if session_id:
            snapshots[session_key] = TranscriptSnapshot(
                session_id=str(session_id), through_message_id=None
            )

    async def _load_context_states(self, session_key: str) -> list[Any]:
        context_states: list[Any] = []
        get_context_states = getattr(self._session_manager, "get_context_states", None)
        if callable(get_context_states):
            try:
                context_states = await get_context_states(session_key)
            except KeyError:
                context_states = []
            except Exception as exc:  # pragma: no cover - context state is best-effort
                log.warning(
                    "compaction_context_state.load_failed",
                    session_key=session_key,
                    error=str(exc),
                )
                context_states = []
        return context_states

    async def _compaction_summary_context(
        self,
        session_key: str,
        legacy_summary_markers: list[str],
        *,
        context_states: list[Any] | None = None,
        skip_covered_through_ids: set[int] | None = None,
        min_covered_through_id: int | None = None,
    ) -> str | None:
        """Return durable compaction summaries as request-scoped context."""
        summaries: list[Any] = []
        get_summaries = getattr(self._session_manager, "get_summaries", None)
        if callable(get_summaries):
            try:
                summaries = await get_summaries(session_key)
            except KeyError:
                summaries = []
            except Exception as exc:  # pragma: no cover - summary context is best-effort
                log.warning(
                    "compaction_summary_context.load_failed",
                    session_key=session_key,
                    error=str(exc),
                )
                summaries = []
        if min_covered_through_id is not None:
            summaries = [
                summary
                for summary in summaries
                if int(getattr(summary, "covered_through_id", 0) or 0) >= min_covered_through_id
            ]
        loaded_context_states = (
            await self._load_context_states(session_key)
            if context_states is None
            else context_states
        )
        context_records = build_compaction_context_records(
            context_states=loaded_context_states,
            summaries=summaries,
            legacy_summary_markers=legacy_summary_markers,
            skip_covered_through_ids=skip_covered_through_ids,
        )
        context_items = [record.text for record in context_records]
        if context_items:
            replayed_compaction_ids = list(
                dict.fromkeys(
                    record.compaction_id
                    for record in context_records
                    if record.compaction_id is not None
                )
            )
            replay_compaction_id = (
                replayed_compaction_ids[0] if replayed_compaction_ids else new_compaction_id()
            )
            notify_compaction(
                session_key,
                source="automatic",
                phase="summary_replay",
                status="replayed",
                summary_count=len(context_items),
                summary_len=sum(len(text) for text in context_items),
                context_state_count=len(loaded_context_states),
                replayed_compaction_ids=replayed_compaction_ids,
                **compaction_lifecycle_payload(
                    replay_compaction_id,
                    COMPACTION_REPLAYED_EVENT,
                ),
            )
        return _format_compaction_summary_context(context_items)

    @staticmethod
    def _maybe_unpack_attachments(content: str) -> Any:
        """Reduce persisted attachment envelopes to text-only history.

        User messages with attachments are persisted as a JSON envelope
        ``{"text": "...", "attachments": [{"type": "image/png", "data": "<b64>"}...]}``
        in ``transcript_entries.content`` (see rpc_sessions._persist_user_message).
        Historical images must not be sent again on later turns: OpenRouter can
        route a text follow-up to a text model, and replaying an old image block
        then fails with "No endpoints found that support image input". Keep the
        original text and a compact non-image marker so the model knows an
        attachment existed without receiving its bytes.

        Returns the original string for non-envelope content so non-attachment
        history (assistant text, tool results) is unaffected. On any parse error,
        missing key, or invalid attachment entry, fall back to the original string
        to keep history loading crash-proof.
        """
        if not content or not content.lstrip().startswith("{"):
            return content
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            return content
        if not isinstance(parsed, dict) or "text" not in parsed:
            return content
        text = parsed.get("text")
        if not isinstance(text, str):
            return content
        atts = parsed.get("attachments") or []
        if not isinstance(atts, list) or not atts:
            return text

        omitted: list[str] = []
        for att in atts:
            if not isinstance(att, dict):
                continue
            media_type = att.get("type") or att.get("mime") or att.get("media_type")
            if not (isinstance(media_type, str) and media_type in _ALLOWED_ENGINE_MEDIA_TYPES):
                continue
            # Persisted attachment envelope: ``sha256_ref`` indicates the bytes live on
            # disk under media/transcripts/<session>/<sha>; for replay we
            # emit a marker (the engine never re-sends the bytes anyway).
            data = att.get("data")
            sha_ref = att.get("sha256_ref")
            missing_reason = att.get("missing_reason")
            if not (
                (isinstance(data, str) and data)
                or (isinstance(sha_ref, str) and sha_ref)
                or (isinstance(missing_reason, str) and missing_reason)
            ):
                continue
            name = att.get("name")
            fallback = "image" if media_type.startswith("image/") else "attachment"
            label = name if isinstance(name, str) and name.strip() else fallback
            omitted.append(f"[historical attachment omitted: {label} ({media_type})]")
        if not omitted:
            return text
        return "\n".join([text, *omitted]).strip()

    @staticmethod
    def _maybe_unpack_assistant_artifacts(content: str) -> str:
        if not content or not content.lstrip().startswith("{"):
            return content
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            return content
        if not isinstance(parsed, dict) or "artifacts" not in parsed:
            return content
        text = parsed.get("text")
        artifacts = parsed.get("artifacts")
        if not isinstance(text, str) or not isinstance(artifacts, list):
            return content
        markers = [
            artifact_marker(artifact) for artifact in artifacts if isinstance(artifact, dict)
        ]
        if not markers:
            return text
        return "\n".join([text, *markers]).strip()

    @staticmethod
    def _attachment_media_root_from_config(config: Any | None) -> Path:
        return media_root_from_config(config)

    def _attachment_media_root(self) -> Path:
        return self._attachment_media_root_from_config(self._config)

    @staticmethod
    def _build_attachment_messages(
        message: str,
        attachments: list[dict],
        *,
        media_root: Path | None = None,
    ) -> list | None:
        """Build a multimodal user message that carries the attachments.

        The engine sees one normalised attachment shape. Provider
        conversion is deliberately narrow:

          * ``image/*``           -> ``ContentBlockImage``
          * ``application/pdf``   -> local text extraction, then ``ContentBlockText``
          * text-family / json    -> ``ContentBlockText`` wrapped in an
                                     ``<file name="…" mime="…">…</file>``
                                     envelope with escaped filename and content
                                     boundaries.
        """

        if not attachments:
            return None
        if len(attachments) > _MAX_ATTACHMENT_COUNT:
            raise ValueError(f"attachments supports at most {_MAX_ATTACHMENT_COUNT} items")

        from agentos.provider.types import (
            ContentBlockImage,
            ContentBlockText,
            Message,
        )

        # An attachment sent with no text -- a photo with no caption, a file
        # dropped into the web chat with an empty box -- arrives as message "".
        # An empty text block is not "no text": Anthropic rejects it outright
        # (text content blocks must be non-empty), failing the whole turn. A
        # user turn made only of attachments is valid for every provider.
        prompt_blocks: list[Any] = [ContentBlockText(text=message)] if message.strip() else []
        attachment_blocks: list[Any] = []
        for index, att in enumerate(attachments, start=1):
            att_type = att.get("type")
            media_type: str | None = att_type if isinstance(att_type, str) else None
            if media_type is None or media_type not in _ALLOWED_ENGINE_MEDIA_TYPES:
                mime = att.get("mime") or att.get("media_type")
                if isinstance(mime, str) and mime in _ALLOWED_ENGINE_MEDIA_TYPES:
                    media_type = mime
            if media_type is None or media_type not in _ALLOWED_ENGINE_MEDIA_TYPES:
                raise ValueError(f"attachments[{index}] media type {att_type!r} is not allowed")
            if is_attachment_ref(att):
                missing_ref_marker = ""
                if media_root is None:
                    raise ValueError(f"attachments[{index}] media_root is required")
                try:
                    raw_bytes = read_attachment_ref_bytes(att, media_root=media_root)
                except FileNotFoundError:
                    raw_bytes = b""
                    missing_ref_marker = "[attachment unavailable: material file is missing]"
                except ValueError as exc:
                    raw_bytes = b""
                    missing_ref_marker = f"[attachment unavailable: {exc}]"
                data = base64.b64encode(raw_bytes).decode("ascii") if raw_bytes else ""
            else:
                missing_ref_marker = ""
                data_raw = att.get("data")
                if not isinstance(data_raw, str) or not data_raw:
                    raise ValueError(f"attachments[{index}].data is required")
                data = data_raw
                try:
                    raw_bytes = base64.b64decode(data, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise ValueError(f"attachments[{index}].data must be valid base64") from exc
            max_bytes = _attachment_size_limit_for_mime(
                media_type,
                staged=media_type == "application/pdf" and att.get("_was_staged") is True,
            )
            if len(raw_bytes) > max_bytes:
                raise ValueError(f"attachments[{index}] exceeds the {max_bytes} byte limit")

            name_raw = att.get("name")
            filename = _sanitize_attachment_filename(name_raw)
            if missing_ref_marker:
                wrapped = _render_file_context_block(filename, media_type, missing_ref_marker)
                attachment_blocks.append(ContentBlockText(text=wrapped))
                continue

            if media_type.startswith("image/"):
                attachment_blocks.append(ContentBlockImage(media_type=media_type, data=data))
            elif media_type == "application/pdf":
                try:
                    extracted_pdf_text = _extract_pdf_attachment_text(raw_bytes, filename)
                except ValueError as exc:
                    extracted_pdf_text = (
                        f"[attachment unavailable: PDF text could not be extracted: {exc}]"
                    )
                wrapped = _render_file_context_block(filename, media_type, extracted_pdf_text)
                attachment_blocks.append(ContentBlockText(text=wrapped))
            elif media_type in _ENGINE_TEXT_FAMILY_MIMES:
                if is_attachment_ref(att) and att.get("_provider_inline_policy") == "preview_only":
                    decoded_text = _render_preview_only_attachment_text(
                        att,
                        filename=filename,
                        mime=media_type,
                        raw_bytes=raw_bytes,
                        media_root=media_root,
                    )
                else:
                    try:
                        decoded_text = _truncate_attachment_text(
                            raw_bytes.decode("utf-8"),
                            limit=_TEXT_ATTACHMENT_TEXT_LIMIT,
                        )
                    except UnicodeDecodeError:
                        decoded_text = (
                            "[attachment unavailable: declared text content is not valid UTF-8]"
                        )
                wrapped = _render_file_context_block(filename, media_type, decoded_text)
                attachment_blocks.append(ContentBlockText(text=wrapped))
            else:  # pragma: no cover - guarded by allow-list above
                raise ValueError(f"attachments[{index}] media type {media_type!r} is not handled")

        return [
            Message(
                role="user",
                content=prompt_blocks + attachment_blocks,  # type: ignore[arg-type]
            )
        ]
