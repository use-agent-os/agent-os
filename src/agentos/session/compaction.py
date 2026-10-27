"""Context window compaction — summarize older messages to free token budget."""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import dataclass, field
from typing import Any, cast

import structlog

from agentos.provider.protocol import provider_connection_config
from agentos.session.compaction_state import (
    REVISES_COMPACTION_ID_KEY,
    build_structured_summary_from_text,
    extract_compaction_obligations,
    merge_structured_payloads,
)

log = structlog.get_logger(__name__)

_COMPACTION_TIMEOUT = 90.0
_MAX_CUSTOM_INSTRUCTIONS_CHARS = 2000


@dataclass
class CompactionConfig:
    base_chunk_ratio: float = 0.4
    min_chunk_ratio: float = 0.15
    safety_margin: float = 1.2
    default_parts: int = 2
    identifier_policy: str = "strict"  # strict | custom | off
    model: str | None = None  # None = use session model
    api_key: str = ""
    base_url: str = "https://openrouter.ai/api/v1"
    timeout_seconds: float = 90.0
    coverage_blocking: bool = False
    # Resolved ``LLMProvider`` the summarizer streams through. When unset, an
    # OpenAI-compatible provider is built from ``api_key``/``base_url``/``model``.
    provider: Any | None = field(default=None, repr=False, compare=False)


@dataclass
class CompactionRequest:
    session_id: str
    entries: list[dict[str, Any]]  # list of {role, content, token_count?}
    context_window_tokens: int
    config: CompactionConfig = field(default_factory=CompactionConfig)
    custom_instructions: str | None = None
    # The summary this compaction revises (it may end with the engine's file
    # block, which is merged). Takes precedence over a ``__prev_summary__:``
    # line in ``custom_instructions``, which can carry one line only.
    previous_summary: str | None = None
    # The durable summary row *previous_summary* came from. When the model
    # revises it, the new summary records ``revises_compaction_id`` in its
    # payload, and replay shows the new summary in place of the old one.
    previous_compaction_id: str | None = None
    previous_summary_payload: dict[str, Any] | None = None


@dataclass
class CompactionResult:
    summary: str
    kept_entries: list[dict[str, Any]]
    removed_count: int
    chunks_processed: int
    summary_source: str = "unknown"  # skipped | fallback | llm | mixed | unknown
    tokens_before: int = 0
    tokens_after: int = 0
    remaining_budget_tokens: int = 0
    summary_payload: dict[str, Any] | None = None
    summary_format: str = "text"
    coverage_status: str = "unknown"
    missing_obligations: list[str] | None = None
    critical_carry_forward: list[str] | None = None
    skip_reason: str | None = None


def _string_value(value: Any) -> str:
    if value is None:
        return ""
    get_secret_value = getattr(value, "get_secret_value", None)
    if callable(get_secret_value):
        value = get_secret_value()
    return str(value).strip()


def build_compaction_config_from_provider(
    provider: Any | None,
    *,
    model_override: str | None = None,
    default_model: str | None = None,
    compaction_config: Any | None = None,
) -> CompactionConfig:
    """Build CompactionConfig from a resolved provider without owning selection."""

    timeout_seconds = getattr(compaction_config, "timeout_seconds", _COMPACTION_TIMEOUT)
    try:
        timeout = float(timeout_seconds)
    except (TypeError, ValueError):
        timeout = _COMPACTION_TIMEOUT

    cfg = CompactionConfig(timeout_seconds=timeout)
    if compaction_config is not None and not bool(getattr(compaction_config, "enabled", True)):
        return cfg

    configured_model = getattr(compaction_config, "model", None) if compaction_config else None
    connection_config = provider_connection_config(provider)
    api_key = connection_config.api_key
    model = connection_config.model
    base_url = connection_config.base_url

    cfg.api_key = api_key
    cfg.model = configured_model or model_override or model or default_model
    if base_url:
        cfg.base_url = base_url
    if provider is not None and (not model or cfg.model == model):
        # Summarize through the provider itself (its protocol, auth, retries)
        # unless the operator pinned a different compaction model.
        cfg.provider = provider
    return cfg


def effective_compaction_model(session: Any | None) -> str | None:
    """Return the model a compaction run for *session* should summarize with.

    A per-session override wins over the session's own model; ``None`` means
    "no opinion", and the provider's configured default applies.
    """
    if session is None:
        return None
    return getattr(session, "model_override", None) or getattr(session, "model", None)


def resolve_compaction_provider(
    provider_selector: Any,
    model_override: str | None = None,
) -> Any | None:
    """Resolve the provider a compaction run should use, or ``None``.

    Compaction must not disturb the live selector: when the selector can be
    cloned, *model_override* is applied to the clone so the turn in flight
    keeps its own model. Every optional step degrades rather than raises — a
    selector that cannot clone, cannot override, or cannot resolve leaves the
    caller with the unmodified selector or with ``None``, and compaction falls
    back to its configured defaults instead of failing the turn.
    """
    if provider_selector is None:
        return None
    selector = provider_selector
    clone = getattr(provider_selector, "clone", None)
    if callable(clone):
        try:
            selector = clone()
        except Exception:  # noqa: BLE001
            selector = provider_selector
    if model_override and selector is not provider_selector:
        override = getattr(selector, "override_model", None)
        if callable(override):
            try:
                override(model_override)
            except Exception:  # noqa: BLE001
                pass
    resolver = getattr(selector, "resolve", None)
    if not callable(resolver):
        return None
    try:
        return resolver()
    except Exception:  # noqa: BLE001
        return None


def compact_accepts_config(compact_fn: Any) -> bool:
    """Return whether a compact callable can accept the optional config arg."""

    side_effect = getattr(compact_fn, "side_effect", None)
    if callable(side_effect):
        compact_fn = side_effect

    try:
        params = list(inspect.signature(compact_fn).parameters.values())
    except (TypeError, ValueError):
        return True

    variadic_kinds = {
        inspect.Parameter.VAR_POSITIONAL,
        inspect.Parameter.VAR_KEYWORD,
    }
    if any(p.kind in variadic_kinds for p in params):
        return True
    if any(p.name == "config" for p in params):
        return True

    positional_kinds = {
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    }
    return len([p for p in params if p.kind in positional_kinds]) >= 3


async def call_compact_with_optional_config(
    compact_fn: Any,
    session_key: str,
    context_window_tokens: int,
    config: CompactionConfig | None,
) -> str:
    """Call compact with config only when the target supports the argument."""

    if config is not None and compact_accepts_config(compact_fn):
        return cast(str, await compact_fn(session_key, context_window_tokens, config))
    return cast(str, await compact_fn(session_key, context_window_tokens))


def _estimate_tokens(text: str) -> int:
    """Delegate to centralized tokenizer (tiktoken with len//4 fallback)."""
    from agentos.session.tokenizer import estimate_tokens

    return estimate_tokens(text)


def _entry_get(entry: Any, key: str, default: Any = None) -> Any:
    if isinstance(entry, dict):
        return entry.get(key, default)
    return getattr(entry, key, default)


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def estimate_entry_replay_tokens(entry: Any) -> int:
    """Estimate the compaction-input size of a persisted transcript entry."""

    content = _entry_get(entry, "content") or ""
    token_count = _entry_get(entry, "token_count")
    try:
        persisted_tokens = int(token_count or 0)
    except (TypeError, ValueError):
        persisted_tokens = 0
    content_tokens = persisted_tokens or (_estimate_tokens(str(content)) if content else 0)

    extra_parts: list[str] = []
    tool_calls = _entry_get(entry, "tool_calls")
    if tool_calls:
        tool_summary = _summarize_tool_calls_for_llm(tool_calls)
        extra_parts.append(tool_summary or _json_text(tool_calls))
    tool_call_id = _entry_get(entry, "tool_call_id")
    if tool_call_id:
        extra_parts.append(str(tool_call_id))
    reasoning_content = _entry_get(entry, "reasoning_content")
    if reasoning_content:
        extra_parts.append(
            "[assistant reasoning omitted from compaction input: "
            f"{len(str(reasoning_content))} chars]"
        )
    extra_tokens = _estimate_tokens("\n".join(extra_parts)) if extra_parts else 0
    return content_tokens + extra_tokens


def estimate_entry_model_replay_tokens(entry: Any) -> int:
    """Estimate the full transcript payload size replayed to the model."""

    content = _entry_get(entry, "content") or ""
    token_count = _entry_get(entry, "token_count")
    try:
        persisted_tokens = int(token_count or 0)
    except (TypeError, ValueError):
        persisted_tokens = 0
    content_tokens = persisted_tokens or (_estimate_tokens(str(content)) if content else 0)

    extra_parts: list[str] = []
    tool_calls = _entry_get(entry, "tool_calls")
    if tool_calls:
        extra_parts.append(_json_text(tool_calls))
    tool_call_id = _entry_get(entry, "tool_call_id")
    if tool_call_id:
        extra_parts.append(str(tool_call_id))
    reasoning_content = _entry_get(entry, "reasoning_content")
    if reasoning_content:
        extra_parts.append(str(reasoning_content))
    extra_tokens = _estimate_tokens("\n".join(extra_parts)) if extra_parts else 0
    return content_tokens + extra_tokens


def _entry_tokens(entry: dict[str, Any]) -> int:
    return estimate_entry_replay_tokens(entry)


def _chunk_entries(entries: list[dict[str, Any]], chunk_ratio: float) -> list[list[dict[str, Any]]]:
    """Split entries into chunks based on ratio of total entries."""
    if not entries:
        return []
    chunk_size = max(1, int(len(entries) * chunk_ratio))
    return [entries[i : i + chunk_size] for i in range(0, len(entries), chunk_size)]


def _build_strict_identifier_instruction() -> str:
    return (
        "IMPORTANT: Preserve all opaque identifiers exactly as written — "
        "UUIDs, hashes, IDs, tokens, API keys, hostnames, IPs, ports, URLs, file names. "
        "Do NOT shorten, reconstruct, or paraphrase any identifier."
    )


def _summarize_if_envelope(content: str) -> str:
    """Replace attachment-envelope JSON with a concise placeholder.

    User messages carrying images are persisted as
    ``{"text": "...", "attachments": [{"type": "image/png", "data": "<base64>"}...]}``
    (see gateway/rpc_sessions.py:_persist_user_message). Feeding the raw JSON
    blob to the compaction LLM wastes context on base64 and confuses the summary.
    Detect the envelope shape and return ``text`` plus a short attachment
    descriptor instead. Non-envelope strings pass through unchanged.
    """
    if not content.startswith('{"text":'):
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
    descs: list[str] = []
    for att in atts:
        if not isinstance(att, dict):
            continue
        name = att.get("name") or "image"
        media = att.get("type") or "image/*"
        descs.append(f"{name} ({media})")
    if descs:
        return f"{text}\n[user attached: {', '.join(descs)}]"
    return text


def _preview_text(text: str, max_chars: int = 240) -> str:
    if len(text) <= max_chars:
        return text
    head_chars = max_chars // 2
    tail_chars = max_chars - head_chars
    omitted = len(text) - head_chars - tail_chars
    return f"{text[:head_chars]}\n[...omitted {omitted} chars...]\n{text[-tail_chars:]}"


def _summarize_tool_value(value: Any) -> str:
    if isinstance(value, str):
        if len(value) <= 240:
            return value
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
        return f"<string chars={len(value)} sha256={digest} preview={_preview_text(value)!r}>"
    if isinstance(value, (int, float, bool)) or value is None:
        return repr(value)
    rendered = _json_text(value)
    if len(rendered) <= 240:
        return rendered
    digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:16]
    return f"<json chars={len(rendered)} sha256={digest} preview={_preview_text(rendered)!r}>"


def _summarize_tool_calls_for_llm(tool_calls: Any) -> str:
    if not isinstance(tool_calls, list) or not tool_calls:
        return ""
    lines = ["[tool payload summary]"]
    for index, segment in enumerate(tool_calls, start=1):
        if not isinstance(segment, dict):
            lines.append(f"- segment {index}: {type(segment).__name__}")
            continue
        seg_type = segment.get("type") or "unknown"
        if seg_type == "tool_use" or isinstance(segment.get("function"), dict):
            tool_name = segment.get("name") or segment.get("function", {}).get("name") or "unknown"
            tool_id = segment.get("tool_use_id") or segment.get("id") or "unknown"
            raw_input = segment.get("input")
            if raw_input is None and isinstance(segment.get("function"), dict):
                raw_input = segment["function"].get("arguments")
            if isinstance(raw_input, str):
                try:
                    parsed_input = json.loads(raw_input)
                except (TypeError, ValueError, json.JSONDecodeError):
                    parsed_input = {"_raw": raw_input}
            elif isinstance(raw_input, dict):
                parsed_input = raw_input
            else:
                parsed_input = {}
            keys = sorted(str(key) for key in parsed_input)
            lines.append(f"- tool_use {tool_id}: {tool_name} keys={keys}")
            for key in keys:
                lines.append(f"  {key}: {_summarize_tool_value(parsed_input.get(key))}")
            continue
        if seg_type == "tool_result":
            result = segment.get("result", "")
            rendered = result if isinstance(result, str) else _json_text(result)
            digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:16]
            lines.append(
                "- tool_result "
                f"{segment.get('tool_use_id') or 'unknown'}: "
                f"is_error={bool(segment.get('is_error'))} "
                f"chars={len(rendered)} sha256={digest} "
                f"preview={_preview_text(rendered)!r}"
            )
            continue
        if seg_type == "text":
            text = str(segment.get("text") or "")
            lines.append(f"- text chars={len(text)} preview={_preview_text(text)!r}")
            continue
        lines.append(f"- {seg_type} keys={sorted(str(key) for key in segment)}")
    return "\n".join(lines)


def _format_chunk_for_llm(chunk: list[dict[str, Any]]) -> str:
    """Format conversation entries into readable text for the compaction LLM."""
    lines: list[str] = []
    for entry in chunk:
        role = entry.get("role", "unknown")
        content = _summarize_if_envelope(str(entry.get("content") or ""))
        rendered_parts = [f"[{role}]: {content}"]
        tool_summary = _summarize_tool_calls_for_llm(entry.get("tool_calls"))
        if tool_summary:
            rendered_parts.append(tool_summary)
        reasoning_content = entry.get("reasoning_content")
        if isinstance(reasoning_content, str) and reasoning_content:
            rendered_parts.append(
                "[assistant reasoning omitted from compaction input: "
                f"{len(reasoning_content)} chars]"
            )
        lines.append("\n".join(part for part in rendered_parts if part))
    return "\n\n".join(lines)


def _summarize_chunk_fallback(chunk: list[dict[str, Any]], policy: str) -> str:
    """Fallback summary when LLM call fails."""
    lines: list[str] = []
    if policy == "strict":
        lines.append(_build_strict_identifier_instruction())
    lines.append(f"[Summary of {len(chunk)} messages]")
    for entry in chunk:
        role = entry.get("role", "unknown")
        content = _summarize_if_envelope(str(entry.get("content") or ""))
        preview = content[:200] + ("..." if len(content) > 200 else "")
        lines.append(f"  [{role}]: {preview}")
    return "\n".join(lines)


def _normalize_custom_instructions(custom_instructions: str | None) -> str:
    if custom_instructions is None:
        return ""
    normalized = custom_instructions.strip()
    if len(normalized) > _MAX_CUSTOM_INSTRUCTIONS_CHARS:
        raise ValueError("custom compaction instructions are too long")
    return normalized


def _summarizer_provider(model: str, api_key: str, base_url: str) -> Any | None:
    """An OpenAI-compatible ``LLMProvider`` for a configured compaction model."""
    if not api_key:
        return None
    from agentos.provider.openai import OpenAIProvider

    url = base_url.rstrip("/")
    if not url.endswith("/v1"):
        url += "/v1"
    try:
        return OpenAIProvider(api_key=api_key, model=model, base_url=url)
    except Exception as exc:  # noqa: BLE001 - invalid credentials fall back
        log.warning("compaction.summarizer_provider_unavailable", error=str(exc))
        return None


async def call_compaction_llm(
    chunk_text: str,
    identifier_instruction: str,
    model: str,
    api_key: str,
    base_url: str = "https://openrouter.ai/api/v1",
    timeout: float = _COMPACTION_TIMEOUT,
    custom_instructions: str | None = None,
    *,
    provider: Any | None = None,
    earlier_summary: str | None = None,
) -> str | None:
    """Summarize a conversation chunk through the ``LLMProvider`` abstraction.

    Uses the engine's structured summary format (the same sections, request
    shape and output bound as in-turn and proactive compaction); with
    *earlier_summary* the model revises that summary instead of starting
    over. *identifier_instruction* is the session's identifier policy (empty
    for none).

    *provider* is the resolved provider of the session (preferred: its own
    protocol, auth, attribution and retries). Without one, an OpenAI-compatible
    provider is built from *api_key*/*base_url*/*model*. Returns ``None`` on
    any failure so the caller falls back to a deterministic digest.
    """
    from agentos.engine.compaction_service import summarize_structured_with_provider

    llm = provider if provider is not None else _summarizer_provider(model, api_key, base_url)
    if llm is None:
        return None
    summary = await summarize_structured_with_provider(
        llm,
        chunk_text,
        earlier_summary=earlier_summary,
        timeout=timeout,
        custom_instructions=_normalize_custom_instructions(custom_instructions) or None,
        identifier_instruction=identifier_instruction,
    )
    if summary is None:
        log.warning("compaction.llm_call_failed", model=model)
    return summary


def _merge_summaries(summaries: list[str]) -> str:
    """Merge chunk summaries into a single cohesive summary.

    Spec requirements: MUST PRESERVE active tasks + status, batch progress,
    last user request, decisions + rationale, TODOs/open questions,
    commitments/follow-ups. Prioritize recent context over older history.
    """
    if len(summaries) == 1:
        return summaries[0]
    merged_lines = ["[Merged context summary]"]
    # Later summaries (more recent) appear last — they take priority
    for i, summary in enumerate(summaries):
        merged_lines.append(f"\n--- Part {i + 1} ---\n{summary}")
    return "\n".join(merged_lines)


def _is_tool_result_entry(entry: dict[str, Any] | None) -> bool:
    if entry is None:
        return False
    if entry.get("role") == "tool" or entry.get("tool_call_id"):
        return True
    content = str(entry.get("content") or "").lstrip()
    return content.startswith("[Tool result ")


def _find_turn_boundary_cut(
    entries: list[dict[str, Any]],
    keep_budget: int,
) -> int:
    """Return the index of the first entry to keep (the cut point).

    The cut is placed at a turn boundary — where the last removed entry is
    NOT an assistant message with a pending tool call, and the first kept
    entry is NOT a tool result that belongs to a removed tool call.

    Strategy:
    1. Start from the token-budget cut (walk from the end, accumulate up to budget).
    2. Walk backward from that cut until we find a boundary that is NOT
       mid-turn (i.e. the cut does not orphan a tool_call/tool_result pair).
    3. If the only possible cut still splits a tool call from its result,
       return 0 so the caller skips compaction instead of orphaning tool state.
    """
    if not entries:
        return 0

    # Compute the legacy cut index: walk from the end, accumulate up to budget.
    kept_tokens = 0
    legacy_keep_start = len(entries)
    for i in range(len(entries) - 1, -1, -1):
        t = _entry_tokens(entries[i])
        if kept_tokens + t <= keep_budget:
            kept_tokens += t
            legacy_keep_start = i
        else:
            break

    if legacy_keep_start == 0:
        # Nothing to remove; caller handles no-op.
        return 0

    # Walk backward from legacy_keep_start toward index 1 looking for a clean
    # turn boundary. A clean boundary can never start with a tool result: that
    # would orphan it from its assistant call, whether or not the immediately
    # preceding entry is itself that assistant call — parallel tool calls put
    # other tool results in between. (An assistant tool call with no result at
    # all yet, i.e. first_kept is None, is not orphaning anything and is a
    # valid place to cut.)
    cut = legacy_keep_start
    while cut > 0:
        first_kept = entries[cut] if cut < len(entries) else None

        if _is_tool_result_entry(first_kept):
            # Move cut one step earlier to avoid orphaning the tool result.
            cut -= 1
            continue

        # Clean boundary found.
        break

    return cut


async def compact_context_new(request: CompactionRequest) -> CompactionResult:
    """Cut-point + turn-boundary-aware + incremental-summary compaction (compaction).

    Differences from :func:`compact_context_legacy`:

    * **Turn-boundary cut**: the split point avoids orphaning an assistant tool
      call from its paired tool result.  The legacy path uses a pure
      token-budget split that may land mid-turn.
    * **Previous summary**: ``request.previous_summary`` (or a one-line
      ``__prev_summary__:<text>`` marker in ``request.custom_instructions``) is
      the base the model revises; without a model summary it is prepended to
      the digest so incremental context accumulates.  Normal custom
      instructions are unaffected.
    * **Summary format**: the engine's structured sections, chunks revising
      one summary in order, and the engine's list of files read and modified
      (merged with the previous summary's) appended after the text -- the
      same builder as in-turn and proactive compaction.

    Everything else (chunk splitting, LLM calls, fallback, token accounting)
    is identical to the legacy path so the eval gate can compare the two
    implementations on the same sessions.
    """
    cfg = request.config
    entries = request.entries
    window = request.context_window_tokens
    total_tokens = sum(_entry_tokens(e) for e in entries)

    # Extract an optional previous-summary prefix injected by the caller.
    # Convention: ``custom_instructions`` may carry ``__prev_summary__:<text>``
    # as the first line.  Strip it before forwarding to ``_normalize_custom_instructions``.
    raw_ci = request.custom_instructions or ""
    prev_summary: str = ""
    prev_from_marker = False
    if raw_ci.startswith("__prev_summary__:"):
        prev_from_marker = True
        first_newline = raw_ci.find("\n")
        if first_newline == -1:
            prev_summary = raw_ci[len("__prev_summary__:") :]
            raw_ci = ""
        else:
            prev_summary = raw_ci[len("__prev_summary__:") : first_newline]
            raw_ci = raw_ci[first_newline + 1 :]
    custom_instructions = _normalize_custom_instructions(raw_ci or None)
    if request.previous_summary:
        prev_summary = request.previous_summary
        prev_from_marker = False

    if not entries:
        return CompactionResult(
            summary="",
            kept_entries=[],
            removed_count=0,
            chunks_processed=0,
            summary_source="skipped",
            tokens_before=0,
            tokens_after=0,
            remaining_budget_tokens=max(window, 0),
            skip_reason="no_entries",
        )

    # If we're within budget, no compaction needed.
    if total_tokens * cfg.safety_margin <= window:
        return CompactionResult(
            summary="",
            kept_entries=entries,
            removed_count=0,
            chunks_processed=0,
            summary_source="skipped",
            tokens_before=total_tokens,
            tokens_after=total_tokens,
            remaining_budget_tokens=max(window - total_tokens, 0),
            skip_reason="within_compaction_budget",
        )

    keep_budget = window // 2

    # compaction: use turn-boundary-aware cut instead of raw token split.
    cut = _find_turn_boundary_cut(entries, keep_budget)
    kept = entries[cut:]
    to_compact = entries[:cut]

    if not to_compact:
        return CompactionResult(
            summary="",
            kept_entries=entries,
            removed_count=0,
            chunks_processed=0,
            summary_source="skipped",
            tokens_before=total_tokens,
            tokens_after=total_tokens,
            remaining_budget_tokens=max(window - total_tokens, 0),
            skip_reason="no_safe_turn_boundary",
        )

    chunk_ratio = max(cfg.min_chunk_ratio, cfg.base_chunk_ratio / cfg.default_parts)
    chunks = _chunk_entries(to_compact, chunk_ratio)

    id_instruction = (
        _build_strict_identifier_instruction() if cfg.identifier_policy == "strict" else ""
    )

    from agentos.engine.compaction_files import (
        attach_file_activity,
        file_activity_from_entries,
        merge_file_activity,
        split_file_activity,
    )

    # The engine's file block of a previous summary is merged, never sent to
    # the model, which revises only the text.
    prev_summary, prev_files = split_file_activity(prev_summary)

    # The chunks revise one summary in order (the first revises the previous
    # summary); after the first failure the rest use the digest, so the
    # model's summary never skips a chunk in the middle.
    rolled: str | None = None
    digests: list[str] = []
    llm_chunks = 0
    fallback_chunks = 0
    llm_available = bool((cfg.api_key or cfg.provider is not None) and cfg.model)
    for chunk in chunks:
        if llm_available and cfg.model:
            llm_result = await call_compaction_llm(
                chunk_text=_format_chunk_for_llm(chunk),
                identifier_instruction=id_instruction,
                model=cfg.model,
                api_key=cfg.api_key,
                base_url=cfg.base_url,
                timeout=cfg.timeout_seconds,
                custom_instructions=custom_instructions or None,
                provider=cfg.provider,
                earlier_summary=rolled if rolled is not None else (prev_summary or None),
            )
            if llm_result:
                rolled, _ = split_file_activity(llm_result)
                llm_chunks += 1
                continue
            llm_available = False
        digests.append(_summarize_chunk_fallback(chunk, cfg.identifier_policy))
        fallback_chunks += 1

    merged = _merge_summaries(([rolled] if rolled is not None else []) + digests)

    # The new summary contains the previous one when the model revised it, or
    # when a ``__prev_summary__`` marker asks for it to be carried as is.
    # Otherwise (no model summary for a durable previous summary) the previous
    # summary is left alone and keeps replaying on its own.
    revised_previous = bool(prev_summary) and rolled is not None
    carried_previous = bool(prev_summary) and rolled is None and prev_from_marker
    if carried_previous:
        merged = f"[Previous context]\n{prev_summary}\n\n[New context]\n{merged}"
    merged = attach_file_activity(
        merged,
        merge_file_activity(
            prev_files if revised_previous or carried_previous else None,
            file_activity_from_entries(to_compact),
        ),
    )

    tokens_after = _estimate_tokens(merged) + sum(_entry_tokens(e) for e in kept)
    if llm_chunks and fallback_chunks:
        summary_source = "mixed"
    elif llm_chunks:
        summary_source = "llm"
    else:
        summary_source = "fallback"

    obligations = extract_compaction_obligations(to_compact)
    structured_summary, coverage = build_structured_summary_from_text(
        merged,
        obligations,
        block_missing_critical=cfg.coverage_blocking,
    )
    if coverage.blocked:
        log.warning(
            "compaction.coverage_blocked",
            missing_obligations=len(coverage.missing_obligations),
            checked_obligations=coverage.checked_obligations,
        )
        return CompactionResult(
            summary="",
            kept_entries=entries,
            removed_count=0,
            chunks_processed=len(chunks),
            summary_source=summary_source,
            tokens_before=total_tokens,
            tokens_after=total_tokens,
            remaining_budget_tokens=max(window - total_tokens, 0),
            summary_payload=structured_summary.model_dump(mode="json"),
            summary_format="structured_v1",
            coverage_status=coverage.status,
            missing_obligations=coverage.missing_obligations,
            critical_carry_forward=coverage.critical_carry_forward,
            skip_reason="coverage_blocked",
        )

    log.info(
        "compaction.new.done",
        removed=len(to_compact),
        kept=len(kept),
        chunks=len(chunks),
        llm_model=cfg.model or "fallback",
        summary_source=summary_source,
        prev_summary_chars=len(prev_summary),
        revised_previous=revised_previous,
    )

    summary_payload = structured_summary.model_dump(mode="json")
    if revised_previous and request.previous_compaction_id:
        # The revised summary replaces its predecessor in replay; carry the
        # predecessor's engine-extracted facts so nothing is lost with it.
        summary_payload = merge_structured_payloads(
            request.previous_summary_payload, summary_payload
        )
        summary_payload[REVISES_COMPACTION_ID_KEY] = request.previous_compaction_id

    return CompactionResult(
        summary=merged,
        kept_entries=kept,
        removed_count=len(to_compact),
        chunks_processed=len(chunks),
        summary_source=summary_source,
        tokens_before=total_tokens,
        tokens_after=tokens_after,
        remaining_budget_tokens=max(window - tokens_after, 0),
        summary_payload=summary_payload,
        summary_format="structured_v1",
        coverage_status=coverage.status,
        missing_obligations=coverage.missing_obligations,
        critical_carry_forward=coverage.critical_carry_forward,
    )


async def compact_context(request: CompactionRequest) -> CompactionResult:
    """Summarize older messages to free context-window budget.

    Delegates to :func:`compact_context_new` — the compaction cut-point +
    turn-boundary-aware pipeline.  The public signature is unchanged so
    every existing call site keeps working without modification.
    """
    return await compact_context_new(request)
