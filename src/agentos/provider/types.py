"""Provider type definitions: stream events, model info, config."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from agentos.execution_status import ExecutionStatus

# ---------------------------------------------------------------------------
# Stream event dataclasses
# ---------------------------------------------------------------------------


@dataclass
class TextDeltaEvent:
    """A chunk of assistant text."""

    kind: Literal["text_delta"] = field(default="text_delta", init=False)
    text: str = ""


@dataclass
class ThinkingDeltaEvent:
    """A chunk of model reasoning/thinking text (never user-facing reply text)."""

    kind: Literal["thinking_delta"] = field(default="thinking_delta", init=False)
    text: str = ""


@dataclass
class ToolUseStartEvent:
    """LLM begins a tool call."""

    kind: Literal["tool_use_start"] = field(default="tool_use_start", init=False)
    tool_use_id: str = ""
    tool_name: str = ""
    synthetic_from_text: bool = False


@dataclass
class ToolUseDeltaEvent:
    """Streaming fragment of tool call arguments (JSON)."""

    kind: Literal["tool_use_delta"] = field(default="tool_use_delta", init=False)
    tool_use_id: str = ""
    json_fragment: str = ""


@dataclass
class ToolUseEndEvent:
    """Tool call argument stream complete."""

    kind: Literal["tool_use_end"] = field(default="tool_use_end", init=False)
    tool_use_id: str = ""
    tool_name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    synthetic_from_text: bool = False
    thought_signature: str | None = None


class StopReason(StrEnum):
    """Provider-neutral reason a model stopped generating.

    Every provider maps its own wire vocabulary (Anthropic ``stop_reason``,
    OpenAI ``finish_reason``, Ollama ``done_reason``, Responses ``status``)
    onto these values so the engine compares against one set. The wire value
    is kept on :attr:`DoneEvent.raw_stop_reason` for logs.
    """

    END_TURN = "end_turn"
    MAX_TOKENS = "max_tokens"
    TOOL_USE = "tool_use"
    STOP_SEQUENCE = "stop_sequence"
    REFUSAL = "refusal"
    PAUSE_TURN = "pause_turn"
    UNKNOWN = "unknown"


# Wire values from every provider family. The vocabularies do not collide, so
# one table serves all of them; lookups are case-insensitive.
_STOP_REASON_ALIASES: dict[str, StopReason] = {
    # Anthropic Messages API
    "end_turn": StopReason.END_TURN,
    "max_tokens": StopReason.MAX_TOKENS,
    "tool_use": StopReason.TOOL_USE,
    "stop_sequence": StopReason.STOP_SEQUENCE,
    "refusal": StopReason.REFUSAL,
    "pause_turn": StopReason.PAUSE_TURN,
    # OpenAI-compatible chat completions and Ollama ``done_reason``
    "stop": StopReason.END_TURN,
    "length": StopReason.MAX_TOKENS,
    "tool_calls": StopReason.TOOL_USE,
    "function_call": StopReason.TOOL_USE,
    "content_filter": StopReason.REFUSAL,
    # OpenAI Responses API (``status`` / ``incomplete_details.reason``)
    "completed": StopReason.END_TURN,
    "max_output_tokens": StopReason.MAX_TOKENS,
    # Our own canonical value, so normalizing is idempotent
    "unknown": StopReason.UNKNOWN,
}


def normalize_stop_reason(raw: str | None, *, has_tool_calls: bool = False) -> StopReason:
    """Map a provider's wire stop reason onto :class:`StopReason`.

    ``has_tool_calls`` upgrades a plain end of turn to ``tool_use`` for
    providers that report ``stop`` even when they emitted tool calls. An
    unrecognized value becomes ``unknown``; callers keep the raw string.
    """

    if isinstance(raw, StopReason):
        normalized = raw
    elif not raw:
        normalized = StopReason.END_TURN
    else:
        normalized = _STOP_REASON_ALIASES.get(str(raw).strip().lower(), StopReason.UNKNOWN)
    if has_tool_calls and normalized is StopReason.END_TURN:
        return StopReason.TOOL_USE
    return normalized


@dataclass
class DoneEvent:
    """Stream finished successfully.

    ``stop_reason`` is always a normalized :class:`StopReason`; a raw wire value
    passed in (``"length"``, ``"stop"``...) is normalized on construction and
    remembered in ``raw_stop_reason``.
    """

    kind: Literal["done"] = field(default="done", init=False)
    stop_reason: StopReason = StopReason.END_TURN
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_content: str | None = None
    thinking_signature: str | None = None
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    billed_cost: float = 0.0
    model: str = ""
    # New fields appended at the end so positional construction in callers and
    # tests does not silently shift earlier args.
    cache_write_tokens: int = 0
    cost_source: str = "none"
    # The provider's own stop value before normalization, for logs.
    raw_stop_reason: str | None = None
    # Structured detail the provider attached to the stop, e.g. Anthropic's
    # refusal ``{"type": "refusal", "category": ..., "explanation": ...}``.
    stop_details: dict[str, Any] | None = None
    # Every ``thinking`` / ``redacted_thinking`` block of the response, in
    # stream order, each with its own ``signature`` / ``data``. This is the
    # replay-safe form: put these blocks, unmodified and in this order, at the
    # start of the assistant message's content. ``reasoning_content`` and
    # ``thinking_signature`` above stay as the joined text and the last
    # signature for older callers; they cannot round-trip more than one block.
    thinking_blocks: list[ContentBlockThinking | ContentBlockRedactedThinking] = field(
        default_factory=list
    )
    # OpenRouter ``reasoning_details`` of the response, fragments merged per
    # ``index``. Store them on :attr:`Message.reasoning_details`; the
    # OpenAI-compatible provider re-sends them verbatim to OpenRouter.
    reasoning_details: list[dict[str, Any]] | None = None
    # False when the provider's usage carried no cache-read figure at all
    # (unknown, not zero hits); the cache-break monitor skips such responses.
    cache_usage_reported: bool = True
    # True when ``input_tokens`` came only from a usage object the provider
    # marked ``"estimated": true`` (OpenCAP), not from a billed count. The
    # request-size calibration ignores such samples.
    usage_estimated: bool = False

    def __post_init__(self) -> None:
        given = self.stop_reason
        normalized = normalize_stop_reason(given)
        if self.raw_stop_reason is None and given and str(given) != normalized.value:
            self.raw_stop_reason = str(given)
        self.stop_reason = normalized

    @property
    def upstream_cost_usd(self) -> float:
        """Backward-compatible alias for earlier OpenRouter cost consumers."""
        return self.billed_cost


@dataclass
class ErrorEvent:
    """Stream error."""

    kind: Literal["error"] = field(default="error", init=False)
    message: str = ""
    #: The HTTP status as a string (``"429"``) for a non-200 response, else an
    #: upstream error token (``"timeout"``, ``"overloaded_error"``, ...).
    code: str = ""
    #: Seconds the provider asked us to wait before retrying, read from
    #: ``Retry-After`` (or a vendor rate-limit reset header) and clamped to
    #: ``[0, 300]``. ``None`` when the response carried no usable hint.
    retry_after_seconds: float | None = None


@dataclass
class ProviderHeartbeatEvent:
    """Provider-side liveness signal while no user-visible tokens are ready."""

    kind: Literal["provider_heartbeat"] = field(default="provider_heartbeat", init=False)
    phase: str = "provider"
    message: str = ""


@dataclass
class QuotaStatus:
    """Quota snapshot returned by ``quota_hook``.

    ``-1`` on either counter is the sentinel for "unlimited / not enforced";
    ``abort_reason`` is user-facing and surfaces verbatim in the graceful
    abort payload when the caller chooses to short-circuit the turn.
    """

    tokens_remaining: int = -1
    tool_calls_remaining: int = -1
    abort_reason: str | None = None


@dataclass(frozen=True)
class ModelCapabilities:
    """Per-model capability flags resolved from ModelCatalog."""

    supports_reasoning: bool = False
    supports_tools: bool = True
    supports_streaming: bool = True
    supports_vision: bool = False
    reasoning_format: str = "none"
    # "none" | "openrouter" | "deepseek" | "think_tags"


StreamEvent = (
    TextDeltaEvent
    | ThinkingDeltaEvent
    | ToolUseStartEvent
    | ToolUseDeltaEvent
    | ToolUseEndEvent
    | DoneEvent
    | ErrorEvent
    | ProviderHeartbeatEvent
)


# ---------------------------------------------------------------------------
# Tool definition (Pydantic BaseModel — external API boundary)
# ---------------------------------------------------------------------------

from pydantic import BaseModel  # noqa: E402


class ToolParam(BaseModel):
    """Single parameter in a tool schema."""

    type: str
    description: str = ""
    enum: list[str] | None = None


class ToolInputSchema(BaseModel):
    """JSON schema for tool inputs."""

    type: Literal["object"] = "object"
    properties: dict[str, Any] = {}
    required: list[str] = []
    # JSON Schema ``additionalProperties``. ``None`` leaves it out of the wire
    # schema (unchanged payloads); strict tools must set it to ``False``.
    additionalProperties: bool | dict[str, Any] | None = None  # noqa: N815


class ToolDefinition(BaseModel):
    """Tool definition passed to the LLM."""

    name: str
    description: str
    input_schema: ToolInputSchema
    # Opt-in constrained decoding of the tool input (Anthropic ``strict``,
    # OpenAI ``function.strict``). ``None`` sends nothing.
    strict: bool | None = None
    execution_timeout_seconds: float | None = None
    execution_timeout_argument: str | None = None
    execution_timeout_padding: float = 0.0


# ---------------------------------------------------------------------------
# Model info (Pydantic BaseModel — registry / API responses)
# ---------------------------------------------------------------------------


class ModelInfo(BaseModel):
    """Metadata about an available model."""

    provider: str
    model_id: str
    display_name: str = ""
    context_window: int = 0
    max_output_tokens: int = 0
    supports_reasoning: bool = False
    supports_tools: bool = True
    supports_streaming: bool = True
    supports_vision: bool = False
    input_cost_per_1k: float = 0.0
    output_cost_per_1k: float = 0.0


# ---------------------------------------------------------------------------
# Chat config (Pydantic BaseModel — call-time settings)
# ---------------------------------------------------------------------------


class ToolChoice(BaseModel):
    """Provider-neutral tool choice; each provider translates it.

    ``auto``: the model decides. ``any``: it must call some tool (OpenAI
    ``required``). ``none``: no tool calls. ``tool``: it must call ``name``.
    ``disable_parallel_tool_use`` asks for at most one tool call per response.
    """

    type: Literal["auto", "any", "none", "tool"] = "auto"
    name: str | None = None
    disable_parallel_tool_use: bool | None = None


class ChatConfig(BaseModel):
    """Runtime options for a single chat call."""

    max_tokens: int = 16384
    temperature: float | None = None
    system: str | None = None
    stop_sequences: list[str] = []
    thinking: bool = False
    thinking_budget_tokens: int = 5000
    timeout: float = 120.0
    # Prompt caching: when set, system prompt is split into cached/dynamic blocks
    cache_breakpoints: list[dict[str, str]] | None = None
    cache_mode: Literal["off", "auto", "on"] = "off"
    model_capabilities: ModelCapabilities | None = None
    thinking_level: Any | None = None
    provider_request_max_chars: int = 0
    # A :class:`ToolChoice`, an Anthropic-native dict, an OpenAI-style dict or a
    # bare string (``"auto"`` / ``"any"`` / ``"required"`` / ``"none"``).
    tool_choice: Any | None = None
    # Prompt-cache TTL for the stable prefix (system prompt and tools).
    # ``None`` / ``"5m"`` is the provider default; ``"1h"`` asks Anthropic for
    # the one-hour cache. The rolling conversation breakpoint stays at 5 min.
    cache_ttl: Literal["5m", "1h"] | None = None
    # Seconds without a streamed data event, once streaming has started,
    # before the provider abandons the stream with a retriable
    # ``stream_idle_timeout`` error. ``None`` reads
    # ``AGENTOS_LLM_STREAM_IDLE_TIMEOUT_SECONDS`` (default 120); ``0`` disables.
    stream_idle_timeout: float | None = None
    # ``False`` asks for at most one tool call per response (OpenAI
    # ``parallel_tool_calls``, Anthropic ``disable_parallel_tool_use``).
    # ``None`` sends nothing.
    parallel_tool_calls: bool | None = None
    # OpenAI ``prompt_cache_key``: requests sharing it are routed to the same
    # cache. Only sent to api.openai.com; when unset there, a key derived from
    # the model, system prompt and tools is used.
    prompt_cache_key: str | None = None
    # Extra ``anthropic-beta`` values for this request (merged, de-duplicated).
    anthropic_betas: list[str] = []
    # Context window the caller budgets for. Ollama sends it as
    # ``options.num_ctx`` (its own default is a few thousand tokens and
    # silently truncates a long prompt); other providers ignore it.
    context_window_tokens: int = 0


# ---------------------------------------------------------------------------
# Message types
# ---------------------------------------------------------------------------


class ContentBlockText(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ContentBlockToolUse(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any]
    thought_signature: str | None = None


class ContentBlockToolResult(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str | list[Any]
    is_error: bool = False
    execution_status: ExecutionStatus | None = None


class ContentBlockImage(BaseModel):
    type: Literal["image"] = "image"
    source_type: Literal["base64", "url"] = "base64"
    media_type: str  # "image/png", "image/jpeg", etc.
    data: str  # base64 data or URL


class ContentBlockDocument(BaseModel):
    type: Literal["document"] = "document"
    source_type: Literal["base64"] = "base64"
    media_type: Literal["application/pdf"]
    data: str
    title: str | None = None


class ContentBlockThinking(BaseModel):
    type: Literal["thinking"] = "thinking"
    thinking: str = ""
    signature: str | None = None


class ContentBlockRedactedThinking(BaseModel):
    """An encrypted Anthropic thinking block; replayed byte-for-byte."""

    type: Literal["redacted_thinking"] = "redacted_thinking"
    data: str


class ContentBlockCompaction(BaseModel):
    type: Literal["compaction"] = "compaction"
    content: str | None = None
    cache_control: dict[str, Any] | None = None


MessageContent = (
    str
    | list[
        ContentBlockText
        | ContentBlockToolUse
        | ContentBlockToolResult
        | ContentBlockImage
        | ContentBlockDocument
        | ContentBlockThinking
        | ContentBlockRedactedThinking
        | ContentBlockCompaction
    ]
)


class MessageProvenance(BaseModel):
    """Which provider and model produced an assistant message.

    ``provider`` is the provider kind the request went through (``anthropic``,
    ``openrouter``, an OpenAI-compatible gateway, ...), ``model`` the model id
    that was requested, and ``api`` the API family whose signed reasoning and
    tool-call ids the message carries (``anthropic``, ``openai``, ``google``,
    ``deepseek``, ...). The family comes from the model id, because one
    OpenAI-compatible gateway can serve several families behind one base URL;
    it is empty when neither the model nor the provider names a known family.
    See :mod:`agentos.provider.replay`.
    """

    provider: str | None = None
    model: str | None = None
    api: str | None = None


class Message(BaseModel):
    """A single conversation message."""

    role: Literal["user", "assistant"]
    content: MessageContent
    reasoning_content: str | None = None
    # OpenRouter ``reasoning_details`` from the response that produced this
    # assistant message, kept verbatim for replay (see DoneEvent).
    reasoning_details: list[dict[str, Any]] | None = None
    # Origin of an assistant message; ``None`` for user messages and for
    # messages persisted before provenance was recorded (treated as "same
    # model" on replay, so old sessions replay exactly as before).
    provenance: MessageProvenance | None = None
