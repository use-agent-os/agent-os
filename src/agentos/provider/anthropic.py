"""AnthropicProvider — streams via Anthropic Messages API using httpx."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

import httpx
import structlog

from agentos.execution_status import derive_is_error

from .error_body import read_bounded_body, summarize_error_body
from .replay import prepare_replay, provenance_for_provider
from .request_proof import (
    ProviderRequestBudgetExceededError,
    prove_provider_payload_from_env,
)
from .retry_after import retry_after_from_response
from .transport import (
    PooledHttpClient,
    StreamIdleTimeoutError,
    guard_tool_stream,
    iter_lines_with_idle_timeout,
    resolve_stream_idle_timeout,
    stream_idle_error_event,
    stream_timeouts,
)
from .types import (
    ChatConfig,
    ContentBlockRedactedThinking,
    ContentBlockThinking,
    DoneEvent,
    ErrorEvent,
    Message,
    ModelInfo,
    StopReason,
    StreamEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolChoice,
    ToolDefinition,
    ToolUseDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    normalize_stop_reason,
)

log = structlog.get_logger(__name__)

_ANTHROPIC_API_BASE = "https://api.anthropic.com"
_ANTHROPIC_VERSION = "2023-06-01"


def _uses_authorization_bearer(base_url: str) -> bool:
    """MiniMax Anthropic-compatible APIs require Authorization."""
    normalized = base_url.lower()
    return "api.minimaxi.com" in normalized or "api.minimax.io" in normalized


_KNOWN_MODELS: list[dict[str, Any]] = [
    {
        "model_id": "claude-opus-5-5",
        "display_name": "Claude Opus 5.5",
        "context_window": 1000000,
        "max_output_tokens": 128000,
        "input_cost_per_1k": 0.004,
        "output_cost_per_1k": 0.02,
    },
    {
        "model_id": "claude-sonnet-5-5",
        "display_name": "Claude Sonnet 5.5",
        "context_window": 1000000,
        "max_output_tokens": 128000,
        "input_cost_per_1k": 0.002,
        "output_cost_per_1k": 0.01,
    },
    {
        "model_id": "claude-fable-5-1",
        "display_name": "Claude Fable 5.1",
        "context_window": 1000000,
        "max_output_tokens": 128000,
        "input_cost_per_1k": 0.01,
        "output_cost_per_1k": 0.05,
    },
    {
        "model_id": "claude-opus-5",
        "display_name": "Claude Opus 5",
        "context_window": 1000000,
        "max_output_tokens": 128000,
        "input_cost_per_1k": 0.005,
        "output_cost_per_1k": 0.025,
    },
    {
        "model_id": "claude-sonnet-5",
        "display_name": "Claude Sonnet 5",
        "context_window": 1000000,
        "max_output_tokens": 128000,
        "input_cost_per_1k": 0.002,
        "output_cost_per_1k": 0.01,
    },
    {
        "model_id": "claude-opus-4-8",
        "display_name": "Claude Opus 4.8",
        "context_window": 1000000,
        "max_output_tokens": 128000,
        "input_cost_per_1k": 0.005,
        "output_cost_per_1k": 0.025,
    },
    {
        "model_id": "claude-opus-4-6",
        "display_name": "Claude Opus 4.6",
        "context_window": 1000000,
        "max_output_tokens": 128000,
        "input_cost_per_1k": 0.005,
        "output_cost_per_1k": 0.025,
    },
    {
        "model_id": "claude-sonnet-4-6",
        "display_name": "Claude Sonnet 4.6",
        "context_window": 1000000,
        "max_output_tokens": 128000,
        "input_cost_per_1k": 0.003,
        "output_cost_per_1k": 0.015,
    },
    {
        "model_id": "claude-haiku-4-5",
        "display_name": "Claude Haiku 4.5",
        "context_window": 200000,
        "max_output_tokens": 64000,
        "input_cost_per_1k": 0.001,
        "output_cost_per_1k": 0.005,
    },
    {
        "model_id": "claude-haiku-4-5-20251001",
        "display_name": "Claude Haiku 4.5",
        "context_window": 200000,
        "max_output_tokens": 64000,
        "input_cost_per_1k": 0.001,
        "output_cost_per_1k": 0.005,
    },
]


def _build_tool_payload(tool: ToolDefinition) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_schema.model_dump(exclude_none=True),
    }
    if tool.strict:
        # Strict tool use is GA (no beta header); the schema must set
        # ``additionalProperties: false`` itself.
        payload["strict"] = True
    return payload


def _translate_tool_choice(tool_choice: Any) -> dict[str, Any] | None:
    if tool_choice is None:
        return None
    if isinstance(tool_choice, ToolChoice):
        if tool_choice.type == "tool":
            if not tool_choice.name:
                return {"type": "any"}
            typed: dict[str, Any] = {"type": "tool", "name": tool_choice.name}
        else:
            typed = {"type": tool_choice.type}
        if tool_choice.disable_parallel_tool_use is not None and tool_choice.type != "none":
            typed["disable_parallel_tool_use"] = tool_choice.disable_parallel_tool_use
        return typed
    if isinstance(tool_choice, str):
        # "auto"/"any"/"none" map directly, OpenAI's "required" is "any";
        # anything else -> auto.
        if tool_choice == "required":
            return {"type": "any"}
        return {"type": tool_choice if tool_choice in {"auto", "any", "none"} else "auto"}
    if not isinstance(tool_choice, dict):
        return None
    choice_type = tool_choice.get("type")
    if choice_type == "function":
        name = (tool_choice.get("function") or {}).get("name") or tool_choice.get("name")
        return {"type": "tool", "name": name} if name else {"type": "any"}
    if choice_type in {"auto", "any", "none", "tool"}:
        return dict(tool_choice)
    return None


def _build_tool_choice_payload(
    tool_choice: Any,
    parallel_tool_calls: bool | None = None,
) -> dict[str, Any] | None:
    """Translate a ChatConfig.tool_choice into Anthropic's native format.

    Callers pass a :class:`ToolChoice`, Anthropic-native dicts
    (``{"type": "tool"|"any"|"auto", ...}``), the OpenAI-style forced-tool dict
    (``{"type": "function", "function": {"name": ...}}``) or a bare string.
    Anthropic uses ``{"type": "tool", "name": ...}`` to force a specific tool.
    ``parallel_tool_calls=False`` adds ``disable_parallel_tool_use`` (on an
    ``auto`` choice when none was given).
    """
    payload = _translate_tool_choice(tool_choice)
    if parallel_tool_calls is False:
        if payload is None:
            payload = {"type": "auto"}
        if payload.get("type") != "none":
            payload.setdefault("disable_parallel_tool_use", True)
    return payload


def _supports_document_blocks(model: str) -> bool:
    """Return True if the SKU supports Anthropic's native ``document`` block.

    Claude 3.5 Sonnet+ and the Claude 4.x Sonnet/Opus families support
    documents. Haiku — including Haiku 4.5 — does not. Older Claude 3 SKUs are
    likewise excluded; we keep the gate conservative so a regression here
    surfaces as a graceful skip rather than a 400 from the API.
    """
    m = model.lower()
    if "haiku" in m:
        return False
    return True


def _increment_document_block_rejected(code: str) -> None:
    """Hook called when Anthropic returns a non-200 for a request that carried
    a document block. The default is a no-op; observability backends and
    tests monkeypatch this.
    """
    return None


def _increment_document_block_unsupported() -> None:
    """Hook called when the adapter substitutes a fallback text block because
    the active model does not support ``document`` blocks. Default no-op.
    """
    return None


def _document_unsupported_fallback_text(title: str | None) -> str:
    label = title or "untitled document"
    return f"[document attached but not consumable by this model] ({label})"


def _has_document_block(messages: list[Message]) -> bool:
    for msg in messages:
        if isinstance(msg.content, str):
            continue
        for block in msg.content:
            if getattr(block, "type", None) == "document":
                return True
    return False


def _build_message_payload(msg: Message, model: str | None = None) -> dict[str, Any]:
    if isinstance(msg.content, str):
        return {"role": msg.role, "content": msg.content}
    parts: list[dict[str, Any]] = []
    tool_result_parts: list[dict[str, Any]] = []
    for block in msg.content:
        if block.type == "text":
            parts.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            parts.append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                }
            )
        elif block.type == "image":
            if block.source_type == "url":
                parts.append({"type": "image", "source": {"type": "url", "url": block.data}})
            else:
                parts.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": block.media_type,
                            "data": block.data,
                        },
                    }
                )
        elif block.type == "document":
            if model is not None and not _supports_document_blocks(model):
                parts.append(
                    {
                        "type": "text",
                        "text": _document_unsupported_fallback_text(block.title),
                    }
                )
                _increment_document_block_unsupported()
                continue
            doc_block: dict[str, Any] = {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": block.media_type,
                    "data": block.data,
                },
            }
            if block.title is not None:
                doc_block["title"] = block.title
            parts.append(doc_block)
        elif block.type == "thinking":
            thinking_block: dict[str, Any] = {
                "type": "thinking",
                "thinking": block.thinking,
            }
            if block.signature:
                thinking_block["signature"] = block.signature
            parts.append(thinking_block)
        elif block.type == "redacted_thinking":
            parts.append({"type": "redacted_thinking", "data": block.data})
        elif block.type == "compaction":
            compaction_block: dict[str, Any] = {"type": "compaction"}
            if block.content is not None:
                compaction_block["content"] = block.content
            if block.cache_control:
                compaction_block["cache_control"] = block.cache_control
            parts.append(compaction_block)
        elif block.type == "tool_result":
            is_error = (
                derive_is_error(block.execution_status)
                if block.execution_status is not None
                else block.is_error
            )
            tool_result_parts.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.tool_use_id,
                    "content": block.content,
                    "is_error": is_error,
                }
            )
    if tool_result_parts:
        parts = tool_result_parts + parts
    return {"role": msg.role, "content": parts}


#: Anthropic accepts at most this many ``cache_control`` markers per request.
_MAX_CACHE_BREAKPOINTS = 4
#: Block types that may carry ``cache_control`` (thinking blocks may not).
_CACHEABLE_BLOCK_TYPES = frozenset(
    {"text", "image", "document", "tool_use", "tool_result", "search_result"}
)


def _caching_enabled(cfg: ChatConfig) -> bool:
    """Prompt caching is on for ``cache_mode`` auto/on, or when the caller
    already marked a system breakpoint (older callers leave ``cache_mode`` at
    its ``"off"`` default but pass ``cache_breakpoints``)."""
    if cfg.cache_mode in {"auto", "on"}:
        return True
    return any(bp.get("cache") for bp in cfg.cache_breakpoints or [])


def _stable_cache_control(cfg: ChatConfig) -> dict[str, Any]:
    """``cache_control`` for the long-lived prefix (system prompt, tools)."""
    if cfg.cache_ttl == "1h":
        return {"type": "ephemeral", "ttl": "1h"}
    return {"type": "ephemeral"}


def _build_system_payload(cfg: ChatConfig) -> str | list[dict[str, Any]] | None:
    if not cfg.system:
        return None
    if not cfg.cache_breakpoints:
        if _caching_enabled(cfg):
            return [
                {"type": "text", "text": cfg.system, "cache_control": _stable_cache_control(cfg)}
            ]
        return cfg.system

    blocks: list[dict[str, Any]] = []
    for bp in cfg.cache_breakpoints:
        text = bp.get("text", "")
        if not text:
            continue
        block: dict[str, Any] = {"type": "text", "text": text}
        if bp.get("cache"):
            block["cache_control"] = _stable_cache_control(cfg)
        blocks.append(block)
    return blocks or cfg.system


def _count_cache_markers(value: Any) -> int:
    if isinstance(value, list):
        return sum(_count_cache_markers(item) for item in value)
    if isinstance(value, dict):
        return 1 if value.get("cache_control") else 0
    return 0


def _normalize_message_content_blocks(messages: list[dict[str, Any]]) -> None:
    """Send every non-empty string content as one text block.

    A string and a one-text-block list are the same prompt, but keeping one
    wire shape means request N+1 repeats request N's bytes exactly, apart from
    where the rolling ``cache_control`` marker sits.
    """
    for message in messages:
        content = message.get("content")
        if isinstance(content, str) and content:
            message["content"] = [{"type": "text", "text": content}]


def _mark_last_user_message(messages: list[dict[str, Any]]) -> bool:
    """Put the rolling breakpoint on the last cacheable block of the last user turn."""
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            return False
        for block in reversed(content):
            if not isinstance(block, dict) or block.get("type") not in _CACHEABLE_BLOCK_TYPES:
                continue
            if block.get("type") == "text" and not block.get("text"):
                continue
            if block.get("cache_control"):
                return False
            block["cache_control"] = {"type": "ephemeral"}
            return True
        return False
    return False


def _apply_cache_breakpoints(payload: dict[str, Any], cfg: ChatConfig) -> None:
    """Place prompt-cache breakpoints on tools and the conversation tail.

    Anthropic caches the prefix ``tools -> system -> messages`` up to each
    marker. The system prompt is marked by :func:`_build_system_payload`; this
    adds (a) a rolling marker on the last block of the last user message, so
    each tool-loop iteration reads the previous one's prefix from cache, and
    (b) a marker on the last tool definition, so the tool list stays cached
    when the conversation prefix changes. Markers already present (system,
    compaction blocks) count against the limit of four; the rolling marker
    wins over the tools marker because it covers the tools too.

    With ``cache_ttl="1h"`` the system and tools markers use the one-hour TTL
    and the rolling marker keeps the 5 minute default: it moves every request,
    so a 1h write there would be paid at the higher rate for nothing, and
    Anthropic requires longer-TTL markers to come before shorter ones, which
    this order satisfies.
    """
    if not _caching_enabled(cfg):
        return
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return
    _normalize_message_content_blocks(messages)
    budget = (
        _MAX_CACHE_BREAKPOINTS
        - _count_cache_markers(payload.get("system"))
        - sum(_count_cache_markers(m.get("content")) for m in messages if isinstance(m, dict))
    )
    if budget > 0 and _mark_last_user_message(messages):
        budget -= 1
    tools = payload.get("tools")
    if budget > 0 and isinstance(tools, list) and tools and isinstance(tools[-1], dict):
        tools[-1]["cache_control"] = _stable_cache_control(cfg)


ThinkingMode = Literal["adaptive", "budget", "none"]


@dataclass(frozen=True)
class ClaudeCapabilities:
    """What a Messages API model accepts for thinking, effort and sampling."""

    thinking_mode: ThinkingMode
    accepts_effort: bool
    accepts_xhigh_effort: bool
    accepts_sampling: bool


_ADAPTIVE_NEWEST = ClaudeCapabilities(
    thinking_mode="adaptive",
    accepts_effort=True,
    accepts_xhigh_effort=True,
    accepts_sampling=False,
)
_ADAPTIVE_4_6 = ClaudeCapabilities(
    thinking_mode="adaptive",
    accepts_effort=True,
    accepts_xhigh_effort=False,
    accepts_sampling=True,
)
_BUDGET = ClaudeCapabilities(
    thinking_mode="budget",
    accepts_effort=False,
    accepts_xhigh_effort=False,
    accepts_sampling=True,
)
_NO_THINKING = ClaudeCapabilities(
    thinking_mode="none",
    accepts_effort=False,
    accepts_xhigh_effort=False,
    accepts_sampling=True,
)

# ``claude-<family>-<major>[-<minor>]``; a minor is one or two digits so a date
# snapshot (``claude-sonnet-4-20250514``) is not read as one.
_FAMILY_ID_RE = re.compile(
    r"claude-(opus|sonnet|haiku|fable|mythos)(?:-(\d+)(?:-(\d{1,2})(?!\d))?)?"
)
# Claude 3 naming put the version first: ``claude-3-7-sonnet-20250219``.
_LEGACY_ID_RE = re.compile(r"claude-(\d+)(?:-(\d{1,2})(?!\d))?(?:-|$)")


def claude_capabilities(model: str) -> ClaudeCapabilities:
    """Classify a model id by the Messages API request surface it accepts.

    Rules (Anthropic API, checked 2026-10):

    - Opus 4.7 / 4.8, every 5.x (Opus 5 / 5.5, Sonnet 5 / 5.5), Fable and
      Mythos: ``thinking: {"type": "adaptive"}``; ``budget_tokens`` is a 400.
      Depth is ``output_config.effort`` (``low`` .. ``xhigh`` / ``max``).
      Non-default ``temperature`` / ``top_p`` / ``top_k`` are a 400. Thinking
      display defaults to ``omitted``, so ask for ``summarized`` explicitly.
    - Opus 4.6 / Sonnet 4.6: adaptive thinking (``budget_tokens`` deprecated),
      effort without ``xhigh``, sampling params allowed.
    - Haiku 4.5, Sonnet 4.5, Opus 4.5 and older 4.x, Claude 3.7 Sonnet:
      ``{"type": "enabled", "budget_tokens": N}`` with 1024 <= N < max_tokens;
      ``effort`` is not sent (Haiku 4.5 / Sonnet 4.5 reject it).
    - Other Claude 3 ids and Claude 2: no extended thinking at all.
    - An unknown ``claude-*`` id is assumed to be a newer model (adaptive, no
      sampling params). A non-Claude id served over an Anthropic-compatible
      endpoint (MiniMax, ...) keeps the legacy ``budget_tokens`` payload.

    Dotted ids from gateway catalogs (``claude-opus-5.5``) and provider
    prefixes (``anthropic/``, ``us.anthropic.``) are accepted.
    """
    normalized = model.lower().replace(".", "-").replace("_", "-")
    if "claude" not in normalized:
        return _BUDGET

    family_match = _FAMILY_ID_RE.search(normalized)
    if family_match is not None:
        family, major_text, minor_text = family_match.groups()
        if family in {"fable", "mythos"} or major_text is None:
            return _ADAPTIVE_NEWEST
        major = int(major_text)
        minor = int(minor_text) if minor_text is not None else 0
        if major >= 5 or (major == 4 and minor >= 7):
            return _ADAPTIVE_NEWEST
        if major == 4 and minor == 6:
            return _ADAPTIVE_4_6
        if major == 4:
            return _BUDGET
        return _NO_THINKING if major < 3 else _BUDGET

    legacy_match = _LEGACY_ID_RE.search(normalized)
    if legacy_match is not None:
        major = int(legacy_match.group(1))
        minor = int(legacy_match.group(2) or 0)
        if major == 3 and minor >= 7:
            return _BUDGET
        if major <= 3:
            return _NO_THINKING
    if "claude-instant" in normalized:
        return _NO_THINKING
    return _ADAPTIVE_NEWEST


_EFFORT_BY_LEVEL: dict[str, str] = {
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
}


def _effort_for_level(level: Any, caps: ClaudeCapabilities) -> str | None:
    """Map an AgentOS thinking level to ``output_config.effort``.

    ``adaptive`` / unset leaves effort to the model's default.
    """
    if level is None or not caps.accepts_effort:
        return None
    effort = _EFFORT_BY_LEVEL.get(str(level).strip().lower())
    if effort == "xhigh" and not caps.accepts_xhigh_effort:
        return "high"
    return effort


def _coerce_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _cache_creation_input_tokens(usage: dict[str, Any]) -> int:
    direct = _coerce_int(usage.get("cache_creation_input_tokens"))
    if direct:
        return direct

    creation = usage.get("cache_creation")
    if not isinstance(creation, dict):
        return 0
    return sum(_coerce_int(value) for value in creation.values())


def _anthropic_input_token_counts(usage: dict[str, Any]) -> tuple[int, int, int]:
    base_input_tokens = _coerce_int(usage.get("input_tokens"))
    cache_read_tokens = _coerce_int(usage.get("cache_read_input_tokens"))
    cache_creation_tokens = _cache_creation_input_tokens(usage)
    total_input_tokens = base_input_tokens + cache_read_tokens + cache_creation_tokens
    return total_input_tokens, cache_read_tokens, cache_creation_tokens


def _anthropic_iteration_token_counts(usage: dict[str, Any]) -> tuple[int, int]:
    iterations = usage.get("iterations")
    if not isinstance(iterations, list):
        return _coerce_int(usage.get("input_tokens")), _coerce_int(usage.get("output_tokens"))

    input_tokens = 0
    output_tokens = 0
    for iteration in iterations:
        if not isinstance(iteration, dict):
            continue
        input_tokens += _coerce_int(iteration.get("input_tokens"))
        output_tokens += _coerce_int(iteration.get("output_tokens"))
    return input_tokens, output_tokens


def _mid_stream_error_event(event: dict[str, Any]) -> ErrorEvent:
    """Translate a streaming ``{"type": "error", "error": {...}}`` event.

    ``code`` carries the upstream error type (``overloaded_error``,
    ``api_error``, ...) so ``classify_provider_error`` can route it exactly
    like the pre-stream HTTP-status path does; ``message`` keeps both so the
    surfaced text still names the cause when the type is unfamiliar.
    """
    raw = event.get("error")
    detail = raw if isinstance(raw, dict) else {}
    error_type = str(detail.get("type") or "") or "stream_error"
    message = str(detail.get("message") or "")
    if not message and isinstance(raw, str):
        message = raw
    return ErrorEvent(
        message=f"{error_type}: {message}" if message else error_type,
        code=error_type,
    )


def _thinking_block_at(blocks: dict[int, dict[str, Any]], event: dict[str, Any]) -> dict[str, Any]:
    """The thinking block a delta belongs to, created if the start was missing."""
    index = event.get("index", -1)
    index = index if isinstance(index, int) else -1
    block = blocks.get(index)
    if block is None or block.get("type") != "thinking":
        block = {"type": "thinking", "thinking": "", "signature": None}
        blocks[index] = block
    return block


def _finished_thinking_blocks(
    blocks: dict[int, dict[str, Any]],
) -> list[ContentBlockThinking | ContentBlockRedactedThinking]:
    finished: list[ContentBlockThinking | ContentBlockRedactedThinking] = []
    for index in sorted(blocks):
        block = blocks[index]
        if block["type"] == "redacted_thinking":
            if block["data"]:
                finished.append(ContentBlockRedactedThinking(data=block["data"]))
        elif block["thinking"] or block["signature"]:
            finished.append(
                ContentBlockThinking(thinking=block["thinking"], signature=block["signature"])
            )
    return finished


class AnthropicProvider:
    """Streams from Anthropic Messages API with SSE parsing."""

    provider_name = "anthropic"

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-6",
        base_url: str = _ANTHROPIC_API_BASE,
        proxy: str | None = None,
        betas: list[str] | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._proxy = proxy or None
        # ``anthropic-beta`` values sent on every request of this provider;
        # ``ChatConfig.anthropic_betas`` adds per-request ones.
        self._betas = list(betas or [])
        self._http = PooledHttpClient(proxy=self._proxy)

    async def aclose(self) -> None:
        """Close the pooled HTTP client (a later request opens a new one)."""
        await self._http.aclose()

    async def __aenter__(self) -> AnthropicProvider:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    def _beta_header(self, cfg: ChatConfig) -> str | None:
        values: list[str] = []
        for beta in [*self._betas, *cfg.anthropic_betas]:
            beta = beta.strip()
            if beta and beta not in values:
                values.append(beta)
        return ",".join(values) or None

    @property
    def model(self) -> str:
        """Model id this provider was configured with.

        Public so callers (e.g. derived-cache key construction) can identify
        the underlying model without prying at private state.
        """
        return self._model

    def _api_url(self, path: str) -> str:
        """Build an API URL without duplicating the version prefix."""
        if self._base_url.endswith("/v1") and path.startswith("/v1/"):
            return f"{self._base_url}{path[3:]}"
        return f"{self._base_url}{path}"

    def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[StreamEvent]:
        cfg = config or ChatConfig()
        return guard_tool_stream(
            lambda: self._stream(messages, tools, cfg),
            provider=self.provider_name,
            model=self._model,
        )

    async def _stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        cfg: ChatConfig,
    ) -> AsyncIterator[StreamEvent]:
        max_tokens = max(1, cfg.max_tokens)
        caps = claude_capabilities(self._model)
        thinking_payload: dict[str, Any] | None = None
        output_config: dict[str, Any] | None = None
        if cfg.thinking:
            if caps.thinking_mode == "adaptive":
                # 4.7+ / 5.x default thinking display to "omitted" (empty
                # thinking text); ask for summaries so ThinkingDeltaEvents flow.
                thinking_payload = {"type": "adaptive", "display": "summarized"}
                effort = _effort_for_level(cfg.thinking_level, caps)
                if effort is not None:
                    output_config = {"effort": effort}
            elif caps.thinking_mode == "budget":
                budget_tokens = max(1024, cfg.thinking_budget_tokens)
                if budget_tokens >= max_tokens:
                    max_tokens = budget_tokens + 4096
                thinking_payload = {
                    "type": "enabled",
                    "budget_tokens": budget_tokens,
                }

        # Messages another model produced lose their signed thinking and get
        # tool ids this API accepts; same-model history is sent unchanged.
        messages = prepare_replay(messages, provenance_for_provider(self))
        built_messages = [_build_message_payload(m, model=self._model) for m in messages]
        request_has_document = any(
            isinstance(m.get("content"), list)
            and any(isinstance(p, dict) and p.get("type") == "document" for p in m["content"])
            for m in built_messages
        )
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": built_messages,
            "stream": True,
        }
        system_payload = _build_system_payload(cfg)
        if system_payload:
            payload["system"] = system_payload
        # Thinking requires the default temperature, and 4.7+ / 5.x / Fable
        # reject sampling params outright, thinking or not.
        if cfg.temperature is not None and thinking_payload is None and caps.accepts_sampling:
            payload["temperature"] = cfg.temperature
        if cfg.stop_sequences:
            payload["stop_sequences"] = cfg.stop_sequences
        if tools:
            payload["tools"] = [_build_tool_payload(t) for t in tools]
            tool_choice_payload = _build_tool_choice_payload(
                cfg.tool_choice, cfg.parallel_tool_calls
            )
            if tool_choice_payload is not None:
                payload["tool_choice"] = tool_choice_payload
        if thinking_payload:
            payload["thinking"] = thinking_payload
        if output_config:
            payload["output_config"] = output_config
        _apply_cache_breakpoints(payload, cfg)

        from agentos.engine.context_budget import coordinate_provider_context_budget

        budget_decision = coordinate_provider_context_budget(
            payload,
            projection_adapter="anthropic",
            proof_budget=cfg.provider_request_max_chars,
            status_projection_mode="native_is_error",
        )
        if budget_decision.action == "budget_limited":
            proof = budget_decision.proof or {}
            log.warning("provider.request_budget_exhausted", **proof)
            yield ErrorEvent(
                message=json.dumps(proof, ensure_ascii=False, sort_keys=True),
                code="provider_request_budget_exhausted",
            )
            return
        payload = budget_decision.payload or payload
        if budget_decision.proof is not None:
            log.info("provider.request_proof", **budget_decision.proof)
        try:
            prove_provider_payload_from_env(
                payload,
                projection_adapter="anthropic",
                status_projection_mode="native_is_error",
            )
        except ProviderRequestBudgetExceededError as exc:
            log.warning("provider.request_budget_exhausted", **exc.proof)
            yield ErrorEvent(
                message=json.dumps(exc.proof, ensure_ascii=False, sort_keys=True),
                code="provider_request_budget_exhausted",
            )
            return

        headers = {
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
        if _uses_authorization_bearer(self._base_url):
            headers["Authorization"] = f"Bearer {self._api_key}"
        else:
            headers["x-api-key"] = self._api_key
        beta_header = self._beta_header(cfg)
        if beta_header:
            headers["anthropic-beta"] = beta_header

        # Per-tool state: id -> accumulated_json fragments
        tool_buffers: dict[str, list[str]] = {}
        tool_names: dict[str, str] = {}
        # Maps Anthropic's global content block index → tool_use_id
        index_to_tid: dict[int, str] = {}
        base_input_tokens = 0
        input_tokens = 0
        output_tokens = 0
        cached_tokens = 0
        cache_creation_tokens = 0
        thinking_parts: list[str] = []
        thinking_signature: str | None = None
        # Content-block index -> thinking / redacted_thinking block, so each
        # block keeps its own text and signature (replayed unmodified later).
        thinking_blocks: dict[int, dict[str, Any]] = {}
        stop_reason = "end_turn"
        stop_details: dict[str, Any] | None = None
        idle_timeout = resolve_stream_idle_timeout(cfg.stream_idle_timeout)

        try:
            client = self._http.get()
            async with client.stream(
                "POST",
                self._api_url("/v1/messages"),
                headers=headers,
                json=payload,
                timeout=stream_timeouts(cfg.timeout),
            ) as response:
                if response.status_code != 200:
                    # The body is read bounded and summarised: this message
                    # becomes an ErrorEvent, and an ErrorEvent reaches the
                    # agent's context. A gateway's HTML block page there
                    # costs tokens and tells the model nothing.
                    body = await read_bounded_body(response)
                    if request_has_document:
                        _increment_document_block_rejected(str(response.status_code))
                    detail = summarize_error_body(body) or "empty response body"
                    yield ErrorEvent(
                        message=f"HTTP {response.status_code}: {detail}",
                        code=str(response.status_code),
                        retry_after_seconds=retry_after_from_response(response),
                    )
                    return

                async for line in iter_lines_with_idle_timeout(
                    response.aiter_lines(), idle_timeout
                ):
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        event = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    etype = event.get("type", "")

                    if etype == "error":
                        # Anthropic can fail *after* the 200 and the first
                        # events: ``event: error`` (overloaded_error,
                        # api_error, ...) followed by a closed connection
                        # with no ``message_stop``. Without this branch the
                        # loop just ran out and the turn ended with
                        # neither an ErrorEvent nor a DoneEvent, so the
                        # caller saw a silently truncated reply and the
                        # circuit breaker never learned the provider was
                        # unhealthy (#2118).
                        yield _mid_stream_error_event(event)
                        return

                    if etype == "message_start":
                        usage = event.get("message", {}).get("usage", {})
                        base_input_tokens = _coerce_int(usage.get("input_tokens"))
                        (
                            input_tokens,
                            cached_tokens,
                            cache_creation_tokens,
                        ) = _anthropic_input_token_counts(usage)

                    elif etype == "content_block_start":
                        index = event.get("index", -1)
                        block = event.get("content_block", {})
                        btype = block.get("type")
                        if btype == "tool_use":
                            tid = block["id"]
                            tname = block["name"]
                            tool_buffers[tid] = []
                            tool_names[tid] = tname
                            index_to_tid[index] = tid
                            yield ToolUseStartEvent(tool_use_id=tid, tool_name=tname)
                        elif btype == "thinking":
                            thinking_blocks[index] = {
                                "type": "thinking",
                                "thinking": str(block.get("thinking") or ""),
                                "signature": block.get("signature") or None,
                            }
                            if block.get("thinking"):
                                thinking_parts.append(str(block["thinking"]))
                        elif btype == "redacted_thinking":
                            thinking_blocks[index] = {
                                "type": "redacted_thinking",
                                "data": str(block.get("data") or ""),
                            }

                    elif etype == "content_block_delta":
                        delta = event.get("delta", {})
                        dtype = delta.get("type")
                        if dtype == "text_delta":
                            yield TextDeltaEvent(text=delta.get("text", ""))
                        elif dtype == "input_json_delta":
                            index = event.get("index", 0)
                            fragment = delta.get("partial_json", "")
                            tid = index_to_tid.get(index)
                            if tid is not None:
                                tool_buffers[tid].append(fragment)
                                yield ToolUseDeltaEvent(tool_use_id=tid, json_fragment=fragment)
                            else:
                                log.debug("anthropic.unknown_delta_index", index=index)
                        elif dtype == "thinking_delta":
                            thinking_text = delta.get("thinking", "")
                            thinking_parts.append(thinking_text)
                            _thinking_block_at(thinking_blocks, event)["thinking"] += thinking_text
                            if thinking_text:
                                yield ThinkingDeltaEvent(text=thinking_text)
                        elif dtype == "signature_delta":
                            signature = delta.get("signature") or None
                            thinking_signature = signature or thinking_signature
                            if signature:
                                _thinking_block_at(thinking_blocks, event)["signature"] = signature

                    elif etype == "content_block_stop":
                        index = event.get("index", -1)
                        tid = index_to_tid.get(index)
                        if tid is not None:
                            full_json = "".join(tool_buffers[tid])
                            try:
                                args = json.loads(full_json) if full_json else {}
                            except json.JSONDecodeError:
                                args = {"_raw": full_json}
                            yield ToolUseEndEvent(
                                tool_use_id=tid,
                                tool_name=tool_names.get(tid, ""),
                                arguments=args,
                            )

                    elif etype == "message_delta":
                        # Last-write-wins guards: a delta that omits
                        # ``usage`` or ``delta.stop_reason`` (some
                        # Anthropic-compatible proxies emit trailing or
                        # empty deltas) must not zero the counts or reset
                        # ``tool_use`` back to ``end_turn``.
                        usage = event.get("usage") or {}
                        if isinstance(usage, dict) and usage:
                            (
                                iteration_input_tokens,
                                iteration_output_tokens,
                            ) = _anthropic_iteration_token_counts(usage)
                            output_tokens = iteration_output_tokens
                            cached_tokens = max(
                                cached_tokens,
                                _coerce_int(usage.get("cache_read_input_tokens")),
                            )
                            cache_creation_tokens = max(
                                cache_creation_tokens,
                                _cache_creation_input_tokens(usage),
                            )
                            if "input_tokens" in usage:
                                base_input_tokens = _coerce_int(usage.get("input_tokens"))
                            if isinstance(usage.get("iterations"), list):
                                input_tokens = iteration_input_tokens
                            else:
                                input_tokens = (
                                    base_input_tokens + cached_tokens + cache_creation_tokens
                                )
                        message_delta = event.get("delta") or {}
                        delta_stop_reason = message_delta.get("stop_reason")
                        if delta_stop_reason:
                            stop_reason = delta_stop_reason
                        # A safety-classifier decline (``refusal``) carries
                        # ``stop_details`` with a category and explanation.
                        delta_stop_details = message_delta.get("stop_details")
                        if not isinstance(delta_stop_details, dict):
                            delta_stop_details = event.get("stop_details")
                        if isinstance(delta_stop_details, dict) and delta_stop_details:
                            stop_details = dict(delta_stop_details)

                    elif etype == "message_stop":
                        reasoning_content = "".join(thinking_parts) or None
                        normalized_stop = normalize_stop_reason(stop_reason)
                        if normalized_stop is StopReason.UNKNOWN:
                            log.info("anthropic.unknown_stop_reason", stop_reason=stop_reason)
                        yield DoneEvent(
                            stop_reason=normalized_stop,
                            raw_stop_reason=stop_reason,
                            stop_details=(
                                stop_details if normalized_stop is StopReason.REFUSAL else None
                            ),
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            reasoning_content=reasoning_content,
                            thinking_signature=thinking_signature,
                            cached_tokens=cached_tokens,
                            cache_write_tokens=cache_creation_tokens,
                            thinking_blocks=_finished_thinking_blocks(thinking_blocks),
                        )

        except StreamIdleTimeoutError as exc:
            yield stream_idle_error_event(exc)
        except httpx.TimeoutException as exc:
            yield ErrorEvent(message=f"Request timed out: {exc}", code="timeout")
        except httpx.RequestError as exc:
            yield ErrorEvent(message=f"Request error: {exc}", code="request_error")

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(provider=self.provider_name, **m) for m in _KNOWN_MODELS]
