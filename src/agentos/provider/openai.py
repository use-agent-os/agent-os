"""OpenAIProvider — streams via OpenAI Chat Completions API using httpx."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

if TYPE_CHECKING:
    from agentos.engine.types import ThinkingLevel

import httpx
import structlog

from agentos.execution_status import compact_provider_status, derive_is_error
from agentos.secrets import clean_header_secret

from .context_capabilities import supports_openrouter_explicit_prompt_cache
from .error_body import read_bounded_body, summarize_error_body
from .openrouter_attribution import openrouter_app_headers
from .protocol import ProviderConnectionConfig, ProviderMetadata
from .reasoning import ThinkTagStreamSplitter
from .replay import prepare_replay, provenance_for_provider
from .request_proof import (
    ProviderRequestBudgetExceededError,
    prove_provider_payload_from_env,
)
from .retry_after import retry_after_from_response
from .text_tool_protocol import promote_text_tool_calls
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
    DoneEvent,
    ErrorEvent,
    Message,
    ModelCapabilities,
    ModelInfo,
    ProviderHeartbeatEvent,
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

_OPENAI_API_BASE = "https://api.openai.com"
log = structlog.get_logger(__name__)
_OPENAI_TOOL_STATUS_OUTPUT_MAX_CHARS = 4000


def _openai_tool_result_content(block: Any) -> str:
    content = block.content if isinstance(block.content, str) else json.dumps(block.content)
    status = getattr(block, "execution_status", None)
    if status is None or not derive_is_error(status):
        return content
    output = content
    if len(output) > _OPENAI_TOOL_STATUS_OUTPUT_MAX_CHARS:
        output = output[:_OPENAI_TOOL_STATUS_OUTPUT_MAX_CHARS]
    return json.dumps(
        {
            "execution_status": compact_provider_status(status),
            "output": output,
        },
        ensure_ascii=False,
    )


def _provider_display_name(provider_kind: str) -> str:
    return {
        "openai": "OpenAI",
        "openrouter": "OpenRouter",
        "opencap": "OpenCAP",
        "surplus": "Surplus Intelligence",
        "deepseek": "DeepSeek",
        "moonshot": "Moonshot",
        "dashscope": "DashScope",
        "gemini": "Gemini",
        "zhipu": "Zhipu",
        "qianfan": "Qianfan",
        "volcengine": "Volcengine",
        "azure": "Azure OpenAI",
        "bailian_coding": "Bailian Coding",
        "mistral": "Mistral",
        "groq": "Groq",
        "siliconflow": "SiliconFlow",
        "aihubmix": "AIHubMix",
        "minimax": "MiniMax",
        "minimax_openai": "MiniMax",
        "byteplus": "BytePlus",
        "bankr": "Bankr",
        "vllm": "vLLM",
        "lm_studio": "LM Studio",
        "ovms": "OVMS",
    }.get(provider_kind, "Provider")


def _http_error_body_text(body: bytes | str) -> str:
    """Extract the useful part of an error body, bounded.

    Kept as a name because `openai_responses` imports it; the logic lives in
    `provider.error_body` so the Anthropic path shares it.
    """

    return summarize_error_body(body)


def _format_chat_http_error(provider_kind: str, status_code: int, body: bytes | str) -> str:
    body_text = _http_error_body_text(body) or "empty response body"
    return (
        f"{_provider_display_name(provider_kind)} chat request failed "
        f"(HTTP {status_code}): {body_text}"
    )


def _resolve_reasoning_effort(level: ThinkingLevel | None, budget: int) -> str:
    """Map ThinkingLevel to OpenRouter/DeepSeek effort string."""
    from agentos.engine.types import (
        ThinkingLevel,  # local: avoids circular import at module load
    )

    _level_map = {
        ThinkingLevel.MINIMAL: "minimal",
        ThinkingLevel.LOW: "low",
        ThinkingLevel.MEDIUM: "medium",
        ThinkingLevel.HIGH: "high",
        ThinkingLevel.XHIGH: "xhigh",
    }
    if level and level in _level_map:
        return _level_map[level]
    if budget <= 1024:
        return "low"
    elif budget <= 10000:
        return "medium"
    else:
        return "high"


def _resolve_deepseek_reasoning_effort(level: ThinkingLevel | None) -> str:
    """Map AgentOS thinking levels to DeepSeek V4's documented effort values."""
    from agentos.engine.types import (
        ThinkingLevel,  # local: avoids circular import at module load
    )

    if level == ThinkingLevel.XHIGH:
        return "max"
    return "high"


def _gemini_supports_reasoning_none(model: str) -> bool:
    """Return True for Gemini OpenAI-compatible models with documented off control."""
    model_name = model.rsplit("/", 1)[-1].strip().lower()
    return model_name.startswith(("gemini-2.5-flash", "gemini-3.1-flash", "gemini-3.5-flash"))


def _extract_think_tags(text: str) -> str:
    """Extract content from <think> tags. Returns empty string if none found."""
    matches = re.findall(r"<think>([\s\S]*?)</think>", text)
    return "\n".join(matches) if matches else ""


def _strip_think_tags(text: str) -> str:
    """Remove <think> tags from text, including unclosed trailing tags."""
    result = re.sub(r"<think>[\s\S]*?</think>", "", text)
    result = re.sub(r"<think>[\s\S]*$", "", result)
    return result.strip()


def _should_disable_openrouter_reasoning_by_default(model: str) -> bool:
    """Return True for OpenRouter models known to default into reasoning streams.

    OpenRouter's reasoning controls are model/provider-specific: GLM 4.5 can be
    stabilized by explicitly disabling reasoning when AgentOS has not requested
    thinking, while MiniMax reasoning endpoints reject that same payload.
    """
    return model.strip().lower() in {
        "z-ai/glm-4.5",
        "z-ai/glm-4.5-air",
        "z-ai/glm-5",
        "z-ai/glm-5.1",
        "z-ai/glm-5.2",
        "z-ai/glm-5.3",
    }


def _direct_openai_uses_max_completion_tokens(
    provider_kind: str,
    base_url: str,
    model: str,
) -> bool:
    if provider_kind != "openai" or "api.openai.com" not in base_url.lower():
        return False
    model_name = model.rsplit("/", 1)[-1].strip().lower()
    return model_name.startswith(("gpt-5", "o1", "o3", "o4"))


def _is_kimi_fixed_sampling_model(provider_kind: str, model: str) -> bool:
    if provider_kind != "moonshot":
        return False
    model_name = model.rsplit("/", 1)[-1].strip().lower()
    return model_name.startswith(("kimi-k2.5", "kimi-k2.6"))


def _should_send_temperature(
    provider_kind: str,
    base_url: str,
    model: str,
    cfg: ChatConfig,
    caps: Any,
) -> bool:
    if cfg.temperature is None:
        return False
    if _is_kimi_fixed_sampling_model(provider_kind, model) and cfg.temperature != 1.0:
        return False
    if (
        provider_kind == "openai"
        and "api.openai.com" in base_url.lower()
        and cfg.thinking
        and bool(caps and caps.supports_reasoning)
    ):
        model_name = model.rsplit("/", 1)[-1].strip().lower()
        if model_name.startswith(("gpt-5.4", "gpt-5.5", "gpt-5.6")):
            return False
    return True


def _resolve_llm_proxy(proxy: str | None) -> str | None:
    if proxy is None:
        return os.environ.get("AGENTOS_LLM_PROXY", "").strip() or None
    return proxy.strip() or None


def _extract_thought_signature(tc: Mapping[str, Any]) -> str | None:
    raw_fn = tc.get("function")
    function: Mapping[str, Any] = raw_fn if isinstance(raw_fn, Mapping) else {}
    raw_extra = tc.get("extra_content")
    extra_content: Mapping[str, Any] = raw_extra if isinstance(raw_extra, Mapping) else {}
    sig = (
        tc.get("thought_signature")
        or tc.get("thoughtSignature")
        or function.get("thought_signature")
        or function.get("thoughtSignature")
        or extra_content.get("thought_signature")
        or extra_content.get("thoughtSignature")
    )
    return str(sig) if sig else None


def _coerce_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _coerce_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _first_present(*sources: tuple[Mapping[str, Any], str]) -> int:
    """Return the first source[key] that is actually present (key in dict).

    Truthiness chains via ``or`` would skip an explicit ``0`` from the canonical
    field and silently fall through to a less-canonical one — e.g. an
    ``cache_creation_input_tokens=0`` getting overwritten by a non-zero
    ``prompt_cache_miss_tokens``. Use ``in`` instead so a real zero wins.
    """
    for src, key in sources:
        if isinstance(src, Mapping) and key in src:
            return _coerce_int(src[key])
    return 0


def _usage_fields(usage: Mapping[str, Any] | None) -> tuple[int, int, int, int, int, float]:
    if not usage:
        return 0, 0, 0, 0, 0, 0.0

    input_tokens = _coerce_int(usage.get("prompt_tokens"))
    output_tokens = _coerce_int(usage.get("completion_tokens"))
    completion_details = usage.get("completion_tokens_details") or {}
    reasoning_tokens = _coerce_int(completion_details.get("reasoning_tokens"))
    prompt_details = usage.get("prompt_tokens_details") or {}

    # Cache reads: keys we accept, in priority order.
    #   - prompt_tokens_details.cached_tokens  — OpenAI native + most OpenRouter
    #     proxies.
    #   - usage.prompt_cache_hit_tokens        — DeepSeek native shape.
    cached_tokens = _first_present(
        (prompt_details, "cached_tokens"),
        (usage, "prompt_cache_hit_tokens"),
    )

    # Cache writes: keys we accept, in priority order.
    #   - usage.cache_creation_input_tokens          — Anthropic-via-OpenRouter passthrough.
    #   - prompt_tokens_details.cache_write_tokens   — OpenRouter documented field.
    #   - usage.cache_write_tokens                   — top-level alias some proxies use.
    #   - usage.prompt_cache_miss_tokens             — DeepSeek (miss == write under their
    #     prompt-cache pricing model).
    #   - prompt_tokens_details.cache_creation_tokens — defensive fallback.
    cache_write_tokens = _first_present(
        (usage, "cache_creation_input_tokens"),
        (prompt_details, "cache_write_tokens"),
        (usage, "cache_write_tokens"),
        (usage, "prompt_cache_miss_tokens"),
        (prompt_details, "cache_creation_tokens"),
    )

    billed_cost = _coerce_float(usage.get("cost", usage.get("total_cost")))
    return (
        input_tokens,
        output_tokens,
        reasoning_tokens,
        cached_tokens,
        cache_write_tokens,
        billed_cost,
    )


def _usage_reports_cache_reads(usage: Mapping[str, Any]) -> bool:
    prompt_details = usage.get("prompt_tokens_details")
    if isinstance(prompt_details, Mapping) and "cached_tokens" in prompt_details:
        return True
    return "prompt_cache_hit_tokens" in usage


class _UsageAccumulator:
    """Merge every usage object a stream carries into one set of counts.

    Gateways split usage across chunks: OpenCAP puts the cache detail
    (``prompt_tokens_details.cached_tokens`` / ``cache_write_tokens``) on the
    finish chunk and then sends a trailing billing chunk with only
    ``prompt_tokens`` / ``completion_tokens`` / ``cost``, sometimes all zeros,
    and sometimes leads with an ``"estimated": true`` chunk. Keeping only the
    last chunk recorded cached=0 (or input=0) for a fully cached request.

    Per field, a later non-zero value wins; a zero or missing one never
    overwrites a non-zero value; a value from an ``estimated`` chunk never
    overwrites a real one.
    """

    def __init__(self) -> None:
        self._values: list[float] = [0, 0, 0, 0, 0, 0.0]
        self._estimated = [False] * 6
        #: Whether any real usage object reported cache reads at all; ``False``
        #: means "unknown", not "zero cache hits".
        self.cache_reads_reported = False

    def add(self, usage: Mapping[str, Any] | None) -> None:
        if not isinstance(usage, Mapping) or not usage:
            return
        estimated = bool(usage.get("estimated"))
        if not estimated and _usage_reports_cache_reads(usage):
            self.cache_reads_reported = True
        for index, value in enumerate(_usage_fields(usage)):
            if not value:
                continue
            if estimated and self._values[index] and not self._estimated[index]:
                continue
            self._values[index] = value
            self._estimated[index] = estimated

    @property
    def input_estimated(self) -> bool:
        """Whether the input count came only from an ``estimated`` usage chunk."""
        return bool(self._values[0]) and self._estimated[0]

    def fields(self) -> tuple[int, int, int, int, int, float]:
        values = self._values
        return (
            int(values[0]),
            int(values[1]),
            int(values[2]),
            int(values[3]),
            int(values[4]),
            float(values[5]),
        )


def _provider_billed_cost(provider_kind: str, raw_billed_cost: float) -> tuple[float, str]:
    """Return trusted provider-billed cost and its source marker."""
    if provider_kind == "openrouter" and raw_billed_cost > 0.0:
        return raw_billed_cost, "provider_billed"
    return 0.0, "none"


def _resolve_tool_call_index(
    tc: Mapping[str, Any],
    pending_calls: Mapping[int, dict[str, Any]],
    *,
    provider_kind: str,
) -> int:
    if "index" in tc:
        return _coerce_int(tc["index"])
    if provider_kind != "gemini":
        raise KeyError("index")
    tool_call_id = tc.get("id")
    if isinstance(tool_call_id, str) and tool_call_id:
        for idx, call in pending_calls.items():
            if call.get("id") == tool_call_id:
                return idx
        return max(pending_calls.keys(), default=-1) + 1
    if len(pending_calls) == 1:
        return cast(int, next(iter(pending_calls)))
    return max(pending_calls.keys(), default=-1) + 1


def _mid_stream_error_event(raw: Any) -> ErrorEvent:
    """Translate a streamed ``{"error": ...}`` chunk into an ErrorEvent.

    ``code`` prefers the upstream ``code`` then ``type`` so that
    ``classify_provider_error`` sees the same tokens it would from the
    pre-stream HTTP-status branch (``502`` -> PROVIDER_OVERLOADED,
    ``rate_limit_exceeded`` -> RATE_LIMITED, ...).
    """
    if isinstance(raw, dict):
        code = raw.get("code")
        if code in (None, ""):
            code = raw.get("type")
        message = raw.get("message")
        if not isinstance(message, str) or not message:
            message = json.dumps(raw, ensure_ascii=False)
    else:
        code = None
        message = str(raw)
    code_text = str(code) if code not in (None, "") else "stream_error"
    return ErrorEvent(message=f"{code_text}: {message}", code=code_text)


def _stream_timeout(timeout: float) -> httpx.Timeout:
    return stream_timeouts(timeout)


def _merge_reasoning_detail(details: list[dict[str, Any]], fragment: dict[str, Any]) -> None:
    """Fold one streamed ``reasoning_details`` fragment into ``details``.

    OpenRouter streams each reasoning block as fragments that share an
    ``index``: the text / summary / encrypted data arrive in pieces and the
    signature on a later piece. Replay needs one entry per block, so fragments
    of the same ``index`` and ``type`` are concatenated; every other field
    keeps its first non-empty value, except ``signature``, where the last one
    wins.
    """
    index = fragment.get("index")
    kind = fragment.get("type")
    target: dict[str, Any] | None = None
    for existing in reversed(details):
        if existing.get("type") != kind:
            continue
        if index is not None and existing.get("index") == index:
            target = existing
        elif index is None and existing.get("index") is None and existing is details[-1]:
            target = existing
        break
    if target is None:
        details.append(dict(fragment))
        return
    for key, value in fragment.items():
        if key in {"text", "summary", "data"} and isinstance(value, str):
            previous = target.get(key)
            target[key] = (previous if isinstance(previous, str) else "") + value
        elif key == "signature":
            if value:
                target[key] = value
        elif target.get(key) in (None, "") and value not in (None, ""):
            target[key] = value


def _openai_supports_prompt_cache_key(provider_kind: str, base_url: str) -> bool:
    """``prompt_cache_key`` is an api.openai.com field.

    OpenAI-compatible gateways and local servers either ignore it or reject an
    unknown field with a 400, so it is only sent to OpenAI itself.
    """
    if provider_kind != "openai":
        return False
    return httpx.URL(base_url).host == "api.openai.com"


def _prompt_cache_key(
    cfg: ChatConfig,
    *,
    model: str,
    openai_messages: list[dict[str, Any]],
    tools_payload: list[dict[str, Any]] | None,
) -> str | None:
    if cfg.prompt_cache_key:
        return cfg.prompt_cache_key[:64]
    if cfg.cache_mode == "off":
        return None
    # Requests that share the system prompt and tools share a cache-routing
    # key, so OpenAI sends them to the machines already holding that prefix.
    system = openai_messages[0] if openai_messages and openai_messages[0].get("role") else None
    stable_prefix = {
        "model": model,
        "system": system if system and system.get("role") == "system" else None,
        "tools": tools_payload or [],
    }
    return f"agentos-{_stable_json_hash(stable_prefix)}"


def _build_openai_tool_choice(tool_choice: Any) -> Any:
    """Translate ``ChatConfig.tool_choice`` into the Chat Completions shape.

    OpenAI-style values (``"auto"``, ``"required"``, ``"none"``,
    ``{"type": "function", ...}``) pass through unchanged; a :class:`ToolChoice`
    and Anthropic-native dicts (``{"type": "tool", "name": ...}``,
    ``{"type": "any"}``) are translated instead of forwarded verbatim.
    """
    if tool_choice is None:
        return None
    if isinstance(tool_choice, ToolChoice):
        if tool_choice.type == "tool":
            if not tool_choice.name:
                return "required"
            return {"type": "function", "function": {"name": tool_choice.name}}
        return {"auto": "auto", "any": "required", "none": "none"}[tool_choice.type]
    if isinstance(tool_choice, str):
        if tool_choice == "any":
            return "required"
        return tool_choice if tool_choice in {"auto", "none", "required"} else "auto"
    if isinstance(tool_choice, dict):
        choice_type = tool_choice.get("type")
        if choice_type == "tool":
            name = tool_choice.get("name")
            return {"type": "function", "function": {"name": name}} if name else "required"
        if choice_type == "any":
            return "required"
        if choice_type in {"auto", "none"}:
            return choice_type
    return tool_choice


def _accepts_parallel_tool_calls_opt_in(provider_kind: str, base_url: str, model: str) -> bool:
    """Whether ``parallel_tool_calls: true`` may be sent to this endpoint.

    The engine asks for parallel tool calls on every request, so the opt-in
    only goes where the field is documented: api.openai.com (minus the o-series
    reasoning models, which reject it) and OpenRouter. Other OpenAI-compatible
    servers (DeepSeek, Zhipu, Gemini-compatible, Ollama, gateways, local
    servers) either reject unknown fields or are unverified, so they get none.
    An explicit ``False`` is a correctness request and is not gated.
    """
    if provider_kind == "openrouter":
        return True
    if provider_kind != "openai" or not base_url:
        return False
    if httpx.URL(base_url).host != "api.openai.com":
        return False
    model_name = model.rsplit("/", 1)[-1].strip().lower()
    return not model_name.startswith(("o1", "o3", "o4"))


def _parallel_tool_calls(cfg: ChatConfig, *, accepts_opt_in: bool = True) -> bool | None:
    if cfg.parallel_tool_calls is False or (cfg.parallel_tool_calls and accepts_opt_in):
        return cfg.parallel_tool_calls
    choice = cfg.tool_choice
    if isinstance(choice, ToolChoice) and choice.disable_parallel_tool_use is not None:
        return not choice.disable_parallel_tool_use
    if isinstance(choice, dict) and isinstance(choice.get("disable_parallel_tool_use"), bool):
        return not choice["disable_parallel_tool_use"]
    return None


def _synthesize_text_tool_events(
    full_text: str,
    tools: list[ToolDefinition] | None,
) -> list[ToolUseStartEvent | ToolUseEndEvent]:
    """Tool-use events for a reply that wrote its tool calls as text.

    The format table and the promotion rules live in ``text_tool_protocol``;
    ``engine.tool_text_compat`` hides exactly what is promoted here, so
    markup is either run or left visible, never silently dropped.
    """
    if not tools or not full_text:
        return []
    promotion = promote_text_tool_calls(
        full_text,
        {tool.name for tool in tools},
        schemas={tool.name: tool.input_schema.properties for tool in tools},
    )
    if promotion is None:
        return []
    prefix = "text_protocol" if promotion.source == "markup" else "text_compat"
    events: list[ToolUseStartEvent | ToolUseEndEvent] = []
    for call in promotion.calls:
        tool_use_id = f"{prefix}_{uuid4().hex[:12]}"
        events.append(
            ToolUseStartEvent(
                tool_use_id=tool_use_id,
                tool_name=call.name,
                synthetic_from_text=True,
            )
        )
        events.append(
            ToolUseEndEvent(
                tool_use_id=tool_use_id,
                tool_name=call.name,
                arguments=dict(call.arguments),
                synthetic_from_text=True,
            )
        )
    return events


def _is_truncated_json(parts: list[str]) -> bool:
    text = "".join(parts)
    if not text.strip():
        return False
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return True
    return False


def _build_openai_tool(tool: ToolDefinition) -> dict[str, Any]:
    function: dict[str, Any] = {
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.input_schema.model_dump(exclude_none=True),
    }
    if tool.strict:
        function["strict"] = True
    return {"type": "function", "function": function}


def _openrouter_model_likely_supports_explicit_prompt_cache(model: str) -> bool:
    return supports_openrouter_explicit_prompt_cache(model)


def _openrouter_model_is_anthropic(model: str) -> bool:
    return model.strip().lower().startswith("anthropic/")


@dataclass(frozen=True)
class _AnthropicCacheGateway:
    """An OpenAI-compatible gateway that forwards cache hints to Anthropic.

    ``style`` says which hint it understands for Claude models:

    - ``"top_level"``: a request-level ``cache_control`` that the gateway turns
      into Anthropic's automatic caching, whose breakpoint advances with the
      conversation (OpenRouter).
    - ``"rolling_block"``: an explicit ``cache_control`` on the last content
      part of the last user / tool message, moved forward every request, the
      same rolling marker the Anthropic provider places. For a gateway that
      caches the system prompt and tools on its own, so only the conversation
      tail needs a marker and the gateway's markers plus ours stay within
      Anthropic's limit of four. No row uses it today; add one only for a
      gateway measured to forward the marker.
    """

    name: str
    provider_kinds: frozenset[str]
    hosts: frozenset[str]
    model_prefixes: tuple[str, ...]
    model_substrings: tuple[str, ...]
    style: str

    def serves(self, provider_kind: str, base_url: str, model: str) -> bool:
        host = (httpx.URL(base_url).host or "").lower() if base_url else ""
        if provider_kind not in self.provider_kinds and host not in self.hosts:
            return False
        model_l = model.strip().lower()
        return model_l.startswith(self.model_prefixes) or any(
            part in model_l for part in self.model_substrings
        )


#: Gateways known to forward cache hints to Anthropic. Add a row to support
#: another one; every upstream not listed here (api.openai.com, DeepSeek,
#: Gemini-compatible, Zhipu, local servers) gets no Anthropic-style hints.
_ANTHROPIC_CACHE_GATEWAYS: tuple[_AnthropicCacheGateway, ...] = (
    _AnthropicCacheGateway(
        name="openrouter",
        provider_kinds=frozenset({"openrouter"}),
        hosts=frozenset({"openrouter.ai"}),
        model_prefixes=("anthropic/", "~anthropic/"),
        model_substrings=(),
        style="top_level",
    ),
    # No OpenCAP row: measured 2026-10-13, it ignores per-message cache_control markers.
)


def _anthropic_cache_gateway(
    *, provider_kind: str, base_url: str, model: str, cfg: ChatConfig
) -> _AnthropicCacheGateway | None:
    if cfg.cache_mode not in {"auto", "on"}:
        return None
    for gateway in _ANTHROPIC_CACHE_GATEWAYS:
        if gateway.serves(provider_kind, base_url, model):
            return gateway
    return None


def _openrouter_anthropic_should_use_top_level_cache(
    *,
    provider_kind: str,
    model: str,
    cfg: ChatConfig,
    base_url: str = "",
) -> bool:
    gateway = _anthropic_cache_gateway(
        provider_kind=provider_kind, base_url=base_url, model=model, cfg=cfg
    )
    return gateway is not None and gateway.style == "top_level"


#: Anthropic accepts at most this many ``cache_control`` markers per request.
_MAX_CACHE_BREAKPOINTS = 4


def _count_cache_markers(value: Any) -> int:
    if isinstance(value, list):
        return sum(_count_cache_markers(item) for item in value)
    if isinstance(value, dict):
        own = 1 if value.get("cache_control") else 0
        return own + sum(_count_cache_markers(v) for k, v in value.items() if k != "cache_control")
    return 0


def _mark_rolling_cache_breakpoint(openai_messages: list[dict[str, Any]]) -> bool:
    """Put ``cache_control`` on the last text part of the last user/tool message.

    A string content becomes one text part carrying the marker (the same
    prompt for the gateway's Anthropic translation). Returns whether a marker
    was placed; nothing is placed past Anthropic's limit of four.
    """
    if _count_cache_markers(openai_messages) >= _MAX_CACHE_BREAKPOINTS:
        return False
    for message in reversed(openai_messages):
        role = message.get("role")
        if role == "assistant":
            # Cache the prefix up to the newest *input*; an assistant tail
            # (prefill) is not something the next request repeats.
            continue
        if role not in {"user", "tool"}:
            return False
        content = message.get("content")
        if isinstance(content, str):
            if not content:
                return False
            message["content"] = [
                {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
            ]
            return True
        if isinstance(content, list):
            for part in reversed(content):
                if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                    part["cache_control"] = {"type": "ephemeral"}
                    return True
        return False
    return False


def _stable_json_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _openrouter_non_system_prefix_item_hashes(
    messages: list[dict[str, Any]], *, max_items: int = 3
) -> list[str]:
    hashes: list[str] = []
    for message in messages:
        if message.get("role") == "system":
            continue
        hashes.append(_stable_json_hash(message))
        if len(hashes) >= max_items:
            break
    return hashes


#: ``reasoning_details[].format`` prefix -> model family that produced it.
_REASONING_FORMAT_FAMILIES: tuple[tuple[str, str], ...] = (
    ("anthropic", "anthropic"),
    ("openai", "openai"),
    ("azure-openai", "openai"),
    ("google", "google"),
    ("xai", "xai"),
)
#: OpenRouter model-id vendor prefix -> model family.
_OPENROUTER_VENDOR_FAMILIES: dict[str, str] = {
    "anthropic": "anthropic",
    "openai": "openai",
    "google": "google",
    "x-ai": "xai",
}


def _reasoning_detail_family(detail: Mapping[str, Any]) -> str:
    fmt = str(detail.get("format") or "").strip().lower()
    for prefix, family in _REASONING_FORMAT_FAMILIES:
        if fmt.startswith(prefix):
            return family
    return "unknown"


def _openrouter_model_family(model: str | None) -> str:
    vendor = (model or "").strip().lower().lstrip("~").split("/", 1)[0]
    return _OPENROUTER_VENDOR_FAMILIES.get(vendor, "unknown")


def _replayable_reasoning_details(
    details: list[dict[str, Any]], model: str | None
) -> list[dict[str, Any]] | None:
    """``details`` when every block was produced by ``model``'s family, else None.

    A router or ``/model`` switch can put a different family on the next
    request. Messages that record their origin are already stripped of
    another model's reasoning by :func:`agentos.provider.replay.prepare_replay`;
    this check covers what that cannot see: messages persisted before
    provenance was recorded, and a routing model id (``openrouter/auto``)
    that answers with a different family from one request to the next.
    Claude signatures, OpenAI / Gemini encrypted reasoning are only valid for
    the family that issued them, so the blocks are replayed all-or-nothing:
    the sequence may not be reordered or partially edited. Details whose
    ``format`` names no known family (plain-text reasoning from GLM, DeepSeek,
    ...) are only replayed to a model that is also outside the known families.
    """
    family = _openrouter_model_family(model)
    if all(_reasoning_detail_family(detail) == family for detail in details):
        return details
    return None


def _attach_reasoning_content(
    msg: Message,
    payload: dict[str, Any],
    *,
    include_reasoning_content: bool = True,
    require_assistant_reasoning_content: bool = False,
    provider_kind: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    if (
        provider_kind == "openrouter"
        and include_reasoning_content
        and msg.role == "assistant"
        and msg.reasoning_details
    ):
        # OpenRouter's replay form: the response's ``reasoning_details``
        # exactly as received (order and content unmodified). It carries the
        # signatures / encrypted blobs that let Claude, GPT, Gemini and GLM keep
        # their reasoning across tool calls, and supersedes plain text. Only
        # replayed to the family that produced it (see the helper).
        replayable = _replayable_reasoning_details(msg.reasoning_details, model)
        if replayable is not None:
            payload["reasoning_details"] = replayable
            return payload
    if include_reasoning_content and msg.role == "assistant" and msg.reasoning_content:
        payload["reasoning_content"] = msg.reasoning_content
    elif (
        include_reasoning_content
        and require_assistant_reasoning_content
        and msg.role == "assistant"
    ):
        payload["reasoning_content"] = ""
    return payload


def _is_direct_deepseek_v4_model_id(model: str) -> bool:
    return model.strip().lower() in {"deepseek-v4-flash", "deepseek-v4-pro"}


def _is_direct_deepseek_v4_request(provider_kind: str, model: str) -> bool:
    return provider_kind == "deepseek" and _is_direct_deepseek_v4_model_id(model)


def _should_replay_reasoning_content(
    *,
    provider_kind: str,
    model: str,
    caps: ModelCapabilities | None,
) -> bool:
    if provider_kind == "openrouter":
        if not caps or not caps.supports_reasoning:
            return False
        return caps.reasoning_format == "openrouter"
    if _is_direct_deepseek_v4_request(provider_kind, model):
        return True
    if not caps or not caps.supports_reasoning:
        return False
    return (
        provider_kind == "deepseek"
        and caps.reasoning_format == "deepseek"
        and _is_direct_deepseek_v4_model_id(model)
    )


def _build_openai_messages(
    msg: Message,
    *,
    include_reasoning_content: bool = True,
    require_assistant_reasoning_content: bool = False,
    provider_kind: str | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """Convert a agentos Message into one or more OpenAI-format message dicts.

    Returns a list because OpenAI requires one ``{"role": "tool"}`` message
    per tool result, while agentos packs multiple tool results into a single
    Message.

    Invariant: tool_result blocks never coexist with text/image blocks in the
    same Message (agent.py always packs tool results into a dedicated message).
    """
    if isinstance(msg.content, str):
        return [
            _attach_reasoning_content(
                msg,
                {"role": msg.role, "content": msg.content},
                include_reasoning_content=include_reasoning_content,
                require_assistant_reasoning_content=require_assistant_reasoning_content,
                provider_kind=provider_kind,
                model=model,
            )
        ]

    parts: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []

    for block in msg.content:
        if block.type == "text":
            parts.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            tool_call_dict: dict[str, Any] = {
                "id": block.id,
                "type": "function",
                "function": {
                    "name": block.name,
                    "arguments": json.dumps(block.input),
                },
            }
            thought_sig = getattr(block, "thought_signature", None)
            if thought_sig:
                tool_call_dict["thought_signature"] = thought_sig
            elif provider_kind == "gemini":
                tool_call_dict["thought_signature"] = "skip_thought_signature_validator"
            tool_calls.append(tool_call_dict)
        elif block.type == "image":
            if block.source_type == "url":
                parts.append({"type": "image_url", "image_url": {"url": block.data}})
            else:
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{block.media_type};base64,{block.data}"},
                    }
                )
        elif block.type == "tool_result":
            tool_results.append(
                {
                    "role": "tool",
                    "tool_call_id": block.tool_use_id,
                    "content": _openai_tool_result_content(block),
                }
            )

    # Tool results → one message per result (OpenAI requirement)
    if tool_results:
        return tool_results

    # Assistant message with tool_calls (preserve text alongside calls)
    if tool_calls:
        result: dict[str, Any] = {"role": msg.role, "tool_calls": tool_calls}
        text_content = " ".join(p["text"] for p in parts if p.get("type") == "text")
        if text_content:
            result["content"] = text_content
        return [
            _attach_reasoning_content(
                msg,
                result,
                include_reasoning_content=include_reasoning_content,
                require_assistant_reasoning_content=require_assistant_reasoning_content,
                provider_kind=provider_kind,
                model=model,
            )
        ]

    # If parts contain mixed content (text + images), return as list for multimodal
    has_non_text = any(p["type"] != "text" for p in parts)
    if has_non_text:
        return [
            _attach_reasoning_content(
                msg,
                {"role": msg.role, "content": parts},
                include_reasoning_content=include_reasoning_content,
                require_assistant_reasoning_content=require_assistant_reasoning_content,
                provider_kind=provider_kind,
                model=model,
            )
        ]
    content_text = " ".join(p["text"] for p in parts if p["type"] == "text")
    return [
        _attach_reasoning_content(
            msg,
            {"role": msg.role, "content": content_text},
            include_reasoning_content=include_reasoning_content,
            require_assistant_reasoning_content=require_assistant_reasoning_content,
            provider_kind=provider_kind,
            model=model,
        )
    ]


class OpenAIProvider:
    """Streams from OpenAI-compatible Chat Completions API (SSE)."""

    provider_name = "openai"

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o",
        base_url: str = _OPENAI_API_BASE,
        org_id: str | None = None,
        proxy: str | None = None,
        provider_kind: str | None = None,
        provider_routing: Mapping[str, str] | None = None,
    ) -> None:
        self._api_key = clean_header_secret(api_key, label="LLM API key")
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._proxy = _resolve_llm_proxy(proxy)
        self._org_id = org_id
        inferred_kind = "openrouter" if "openrouter.ai" in self._base_url else "openai"
        self._provider_kind = provider_kind or inferred_kind
        self._provider_routing: Mapping[str, str] = provider_routing or {}
        self._http = PooledHttpClient(proxy=self._proxy)

    async def aclose(self) -> None:
        """Close the pooled HTTP client (a later request opens a new one)."""
        await self._http.aclose()

    async def __aenter__(self) -> OpenAIProvider:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    @property
    def model(self) -> str:
        """Model id this provider was configured with.

        Public so callers (e.g. derived-cache key construction) can identify
        the underlying model without prying at private state.
        """
        return self._model

    def provider_metadata(self) -> ProviderMetadata:
        """Return read-only non-secret provider metadata for consumers."""
        return ProviderMetadata(
            provider_name=self.provider_name,
            provider_kind=self._provider_kind,
            model=self._model,
            base_url=self._base_url,
        )

    def provider_connection_config(self) -> ProviderConnectionConfig:
        """Return provider-owned connection fields for internal runtime calls."""
        return ProviderConnectionConfig(
            provider_kind=self._provider_kind,
            model=self._model,
            api_key=self._api_key,
            base_url=self._base_url,
        )

    def _api_url(self, path: str) -> str:
        """Build an API URL without duplicating the version prefix."""
        if self._base_url.endswith("/v1") and path.startswith("/v1/"):
            return f"{self._base_url}{path[3:]}"
        if self._base_url.endswith("/v2") and path.startswith("/v1/"):
            return f"{self._base_url}{path[3:]}"
        if self._base_url.endswith("/v3") and path.startswith("/v1/"):
            return f"{self._base_url}{path[3:]}"
        if self._base_url.endswith("/v4") and path.startswith("/v1/"):
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
            provider=self._provider_kind,
            model=self._model,
        )

    async def _stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        cfg: ChatConfig,
    ) -> AsyncIterator[StreamEvent]:
        openai_messages: list[dict[str, Any]] = []
        caps = cfg.model_capabilities
        include_reasoning_content = _should_replay_reasoning_content(
            provider_kind=self._provider_kind,
            model=self._model,
            caps=caps,
        )
        if cfg.system:
            explicit_cache_supported = self._provider_kind == "openrouter" and (
                cfg.cache_mode == "on"
                or (
                    cfg.cache_mode == "auto"
                    and _openrouter_model_likely_supports_explicit_prompt_cache(self._model)
                )
            )
            if cfg.cache_breakpoints and explicit_cache_supported:
                # Split system prompt into cached base + dynamic parts
                content_blocks = []
                for bp in cfg.cache_breakpoints:
                    block: dict[str, Any] = {"type": "text", "text": bp["text"]}
                    if bp.get("cache"):
                        block["cache_control"] = {"type": "ephemeral"}
                    content_blocks.append(block)
                openai_messages.append({"role": "system", "content": content_blocks})
            else:
                openai_messages.append({"role": "system", "content": cfg.system})
        # Messages another model produced lose their signed reasoning and get
        # tool ids this API accepts; same-model history is sent unchanged.
        messages = prepare_replay(messages, provenance_for_provider(self))
        for m in messages:
            openai_messages.extend(
                _build_openai_messages(
                    m,
                    include_reasoning_content=include_reasoning_content,
                    require_assistant_reasoning_content=(
                        _is_direct_deepseek_v4_request(self._provider_kind, self._model)
                    ),
                    provider_kind=self._provider_kind,
                    model=self._model,
                )
            )

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": openai_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if _direct_openai_uses_max_completion_tokens(
            self._provider_kind,
            self._base_url,
            self._model,
        ):
            payload["max_completion_tokens"] = cfg.max_tokens
        else:
            payload["max_tokens"] = cfg.max_tokens
        if self._provider_kind == "openrouter":
            payload["usage"] = {"include": True}
            if _openrouter_anthropic_should_use_top_level_cache(
                provider_kind=self._provider_kind,
                model=self._model,
                cfg=cfg,
                base_url=self._base_url,
            ):
                payload["cache_control"] = {"type": "ephemeral"}
        cache_gateway = _anthropic_cache_gateway(
            provider_kind=self._provider_kind,
            base_url=self._base_url,
            model=self._model,
            cfg=cfg,
        )
        if cache_gateway is not None and cache_gateway.style == "rolling_block":
            _mark_rolling_cache_breakpoint(openai_messages)
        if _should_send_temperature(
            self._provider_kind,
            self._base_url,
            self._model,
            cfg,
            caps,
        ):
            payload["temperature"] = cfg.temperature
        if cfg.stop_sequences:
            payload["stop"] = cfg.stop_sequences
        if tools:
            payload["tools"] = [_build_openai_tool(t) for t in tools]
            tool_choice_payload = _build_openai_tool_choice(cfg.tool_choice)
            if tool_choice_payload is not None:
                payload["tool_choice"] = tool_choice_payload
            parallel_tool_calls = _parallel_tool_calls(
                cfg,
                accepts_opt_in=_accepts_parallel_tool_calls_opt_in(
                    self._provider_kind, self._base_url, self._model
                ),
            )
            if parallel_tool_calls is not None:
                payload["parallel_tool_calls"] = parallel_tool_calls
        if _openai_supports_prompt_cache_key(self._provider_kind, self._base_url):
            cache_key = _prompt_cache_key(
                cfg,
                model=self._model,
                openai_messages=openai_messages,
                tools_payload=payload.get("tools"),
            )
            if cache_key:
                payload["prompt_cache_key"] = cache_key
        pinned_provider = self._provider_routing.get(self._model)
        if pinned_provider:
            if self._provider_kind == "openrouter":
                payload["provider"] = {
                    "order": [pinned_provider],
                    "allow_fallbacks": True,
                }
            elif self._provider_kind in {"opencap", "surplus"}:
                payload["provider"] = [pinned_provider]

        # Reasoning injection (gated on thinking being enabled)
        direct_deepseek_v4 = _is_direct_deepseek_v4_request(self._provider_kind, self._model)
        if (caps and caps.supports_reasoning and cfg.thinking) or (
            direct_deepseek_v4 and cfg.thinking
        ):
            effort = _resolve_reasoning_effort(cfg.thinking_level, cfg.thinking_budget_tokens)
            reasoning_format = caps.reasoning_format if caps is not None else "deepseek"
            if reasoning_format == "openrouter":
                payload["reasoning"] = {"effort": effort}
            elif reasoning_format == "openai":
                payload["reasoning_effort"] = effort
            elif reasoning_format == "deepseek":
                payload["thinking"] = {"type": "enabled"}
                payload["reasoning_effort"] = _resolve_deepseek_reasoning_effort(cfg.thinking_level)
            elif reasoning_format == "gemini":
                payload["reasoning_effort"] = effort
            elif reasoning_format == "zai":
                payload["thinking"] = {"type": "enabled"}
            elif reasoning_format == "dashscope":
                payload["enable_thinking"] = True
                payload["thinking_budget"] = cfg.thinking_budget_tokens
            elif reasoning_format in {"moonshot", "volcengine"}:
                payload["thinking"] = {"type": "enabled"}
        elif direct_deepseek_v4 or (
            caps and caps.supports_reasoning and caps.reasoning_format == "deepseek"
        ):
            payload["thinking"] = {"type": "disabled"}
        elif (
            caps
            and caps.supports_reasoning
            and caps.reasoning_format == "gemini"
            and _gemini_supports_reasoning_none(self._model)
        ):
            payload["reasoning_effort"] = "none"
        elif caps and caps.supports_reasoning and caps.reasoning_format == "zai":
            payload["thinking"] = {"type": "disabled"}
        elif caps and caps.supports_reasoning and caps.reasoning_format == "dashscope":
            payload["enable_thinking"] = False
        elif (
            caps
            and caps.supports_reasoning
            and caps.reasoning_format
            in {
                "moonshot",
                "volcengine",
            }
        ):
            payload["thinking"] = {"type": "disabled"}
        elif (
            self._provider_kind == "openrouter"
            and caps
            and caps.supports_reasoning
            and caps.reasoning_format == "openrouter"
            and _should_disable_openrouter_reasoning_by_default(self._model)
        ):
            payload["reasoning"] = {"enabled": False}

        fallback_reason = (
            "native_is_error_unavailable"
            if any(message.get("role") == "tool" for message in openai_messages)
            else None
        )
        from agentos.engine.context_budget import coordinate_provider_context_budget

        budget_decision = coordinate_provider_context_budget(
            payload,
            projection_adapter=self._provider_kind,
            proof_budget=cfg.provider_request_max_chars,
            status_projection_mode="content_envelope",
            fallback_reason=fallback_reason,
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
                projection_adapter=self._provider_kind,
                status_projection_mode="content_envelope",
                fallback_reason=fallback_reason,
            )
        except ProviderRequestBudgetExceededError as exc:
            log.warning("provider.request_budget_exhausted", **exc.proof)
            yield ErrorEvent(
                message=json.dumps(exc.proof, ensure_ascii=False, sort_keys=True),
                code="provider_request_budget_exhausted",
            )
            return

        headers: dict[str, str] = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        if self._provider_kind in {"opencap", "bankr", "surplus"}:
            headers["x-api-key"] = self._api_key
        headers.update(openrouter_app_headers(self._base_url))
        if self._org_id:
            headers["OpenAI-Organization"] = self._org_id

        # tool_call accumulator: index -> {id, name, json_parts}
        pending_calls: dict[int, dict[str, Any]] = {}
        reasoning_parts: list[str] = []
        reasoning_details: list[dict[str, Any]] = []
        usage_acc = _UsageAccumulator()
        saw_done_marker = False
        idle_timeout = resolve_stream_idle_timeout(cfg.stream_idle_timeout)
        assistant_text_parts: list[str] = []
        # think_tags models interleave reasoning into the content stream; the
        # splitter routes it to reasoning_parts so it never reaches user text.
        think_splitter = (
            ThinkTagStreamSplitter()
            if caps is not None and caps.reasoning_format == "think_tags"
            else None
        )
        input_tokens = 0
        output_tokens = 0
        reasoning_tokens = 0
        cached_tokens = 0
        cache_write_tokens = 0
        billed_cost = 0.0
        cost_source = "none"
        actual_model = self._model
        stop_reason: str | None = None
        emitted_stream_event = False

        if os.environ.get("AGENTOS_TRACE_ROUTING"):
            print(
                f"[CALLED] base={self._base_url} model={self._model} "
                f"n_messages={len(openai_messages)}",
                file=sys.stderr,
                flush=True,
            )
        if self._provider_kind == "openrouter":
            system_payload = (
                openai_messages[0]
                if openai_messages and openai_messages[0].get("role") == "system"
                else None
            )
            non_system_prefix_item_hashes = _openrouter_non_system_prefix_item_hashes(
                openai_messages
            )
            log.debug(
                "openrouter.payload_cache_shape",
                model=self._model,
                top_level_cache_control=bool(payload.get("cache_control")),
                system_hash=_stable_json_hash(system_payload) if system_payload else "",
                tools_hash=_stable_json_hash(payload.get("tools", [])) if tools else "",
                messages_prefix_hash=_stable_json_hash(openai_messages[:-1]),
                first_non_system_hash=(
                    non_system_prefix_item_hashes[0] if non_system_prefix_item_hashes else ""
                ),
                non_system_prefix_item_hashes=non_system_prefix_item_hashes,
                message_count=len(openai_messages),
            )

        try:
            client = self._http.get()
            async with client.stream(
                "POST",
                self._api_url("/v1/chat/completions"),
                headers=headers,
                json=payload,
                timeout=stream_timeouts(cfg.timeout),
            ) as response:
                if response.status_code != 200:
                    body = await read_bounded_body(response)
                    message = _format_chat_http_error(
                        self._provider_kind,
                        response.status_code,
                        body,
                    )
                    # Diagnostic: dump payload head (no auth headers)
                    # so 400s from picky upstreams are debuggable. Truncated
                    # to keep memory low.
                    _body_text = (
                        body.decode("utf-8", errors="replace")
                        if isinstance(body, bytes)
                        else str(body)
                    )
                    try:
                        _payload_head = json.dumps(
                            payload,
                            ensure_ascii=False,
                        )[:4000]
                    except Exception:  # noqa: BLE001
                        _payload_head = repr(payload)[:4000]
                    log.warning(
                        "provider.chat_http_error",
                        provider=self._provider_kind,
                        model=self._model,
                        status_code=response.status_code,
                        message=message,
                        response_body=_body_text[:2000],
                        request_payload_head=_payload_head,
                    )
                    yield ErrorEvent(
                        message=message,
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
                        saw_done_marker = True
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    if not isinstance(chunk, dict):
                        continue

                    # OpenAI-compatible gateways report a failure that
                    # happens after the 200 as a chunk carrying ``error``
                    # (OpenRouter: ``{"error": {"code": 502, ...}}``)
                    # and then close the stream, usually without
                    # ``[DONE]``. That chunk has no ``choices`` so it
                    # used to fall straight through to the DoneEvent
                    # below, which the runtime counted as a success
                    # against the circuit breaker (#2118, #2214).
                    if chunk.get("error"):
                        yield _mid_stream_error_event(chunk["error"])
                        return

                    chunk_model = chunk.get("model")
                    if chunk_model:
                        actual_model = chunk_model

                    # Usage may appear in the final chunk, or be split across
                    # several (see _UsageAccumulator).
                    if chunk.get("usage"):
                        usage_acc.add(chunk["usage"])
                        (
                            input_tokens,
                            output_tokens,
                            reasoning_tokens,
                            cached_tokens,
                            cache_write_tokens,
                            raw_billed_cost,
                        ) = usage_acc.fields()
                        billed_cost, cost_source = _provider_billed_cost(
                            self._provider_kind,
                            raw_billed_cost,
                        )

                    # `or []` / `or {}` throughout, not a get() default: several
                    # OpenAI-compatible gateways send these keys with an explicit
                    # null rather than omitting them, and a get() default only
                    # covers the missing case.
                    for choice in chunk.get("choices") or []:
                        finish = choice.get("finish_reason")
                        if finish:
                            stop_reason = finish

                        delta = choice.get("delta") or {}

                        # Text content
                        text = delta.get("content")
                        if text:
                            emitted_stream_event = True
                            if think_splitter is not None:
                                visible, thinking = think_splitter.feed(text)
                                if visible:
                                    yield TextDeltaEvent(text=visible)
                                    assistant_text_parts.append(visible)
                                if thinking:
                                    reasoning_parts.append(thinking)
                                    yield ThinkingDeltaEvent(text=thinking)
                            else:
                                yield TextDeltaEvent(text=text)
                                assistant_text_parts.append(text)

                        # Reasoning content (always parsed, not gated on thinking)
                        delta_reasoning_details = delta.get("reasoning_details")
                        if delta_reasoning_details:
                            for detail in delta_reasoning_details:
                                if isinstance(detail, dict):
                                    _merge_reasoning_detail(reasoning_details, detail)
                                    text_val = detail.get("text", "")
                                    if text_val:
                                        reasoning_parts.append(text_val)
                                        yield ThinkingDeltaEvent(text=text_val)
                        reasoning_str = delta.get("reasoning_content")
                        if reasoning_str:
                            reasoning_parts.append(reasoning_str)
                            yield ThinkingDeltaEvent(text=reasoning_str)

                        # Tool calls (may stream over multiple chunks)
                        for tc in delta.get("tool_calls") or []:
                            idx = _resolve_tool_call_index(
                                tc,
                                pending_calls,
                                provider_kind=self._provider_kind,
                            )
                            function = tc.get("function") or {}
                            thought_sig = _extract_thought_signature(tc)
                            if idx not in pending_calls:
                                pending_calls[idx] = {
                                    "id": tc.get("id") or "",
                                    "name": function.get("name") or "",
                                    "parts": [],
                                    "thought_signature": thought_sig,
                                    "started": False,
                                }
                            else:
                                # id/name may arrive in later chunks. Once the start
                                # event has published an id it is kept: the later
                                # delta/end events must name that same id.
                                if tc.get("id") and not pending_calls[idx]["started"]:
                                    pending_calls[idx]["id"] = tc["id"]
                                fname = function.get("name") or ""
                                if fname:
                                    pending_calls[idx]["name"] = fname
                                if thought_sig and not pending_calls[idx].get("thought_signature"):
                                    pending_calls[idx]["thought_signature"] = thought_sig

                            fragment = function.get("arguments") or ""
                            # ToolUseStartEvent publishes the id, so it waits for the
                            # real one; a synthetic id is minted only once an argument
                            # fragment (or the end of the stream) forces the start.
                            call = pending_calls[idx]
                            if not call["started"] and (call["id"] or fragment):
                                call["id"] = call["id"] or f"call_{uuid4().hex[:12]}"
                                call["started"] = True
                                emitted_stream_event = True
                                yield ToolUseStartEvent(
                                    tool_use_id=call["id"],
                                    tool_name=call["name"],
                                )
                            if fragment:
                                pending_calls[idx]["parts"].append(fragment)
                                emitted_stream_event = True
                                yield ToolUseDeltaEvent(
                                    tool_use_id=pending_calls[idx]["id"],
                                    json_fragment=fragment,
                                )

                # Resolve any partial <think> tag held back at end of stream.
                if think_splitter is not None:
                    visible, thinking = think_splitter.flush()
                    if visible:
                        emitted_stream_event = True
                        yield TextDeltaEvent(text=visible)
                        assistant_text_parts.append(visible)
                    if thinking:
                        reasoning_parts.append(thinking)
                        yield ThinkingDeltaEvent(text=thinking)

                # A stream that closed with neither ``[DONE]`` nor a
                # finish_reason was cut; if a tool call's arguments are not
                # valid JSON, it was cut inside that call. End here without
                # ToolUseEnd/Done so the stream guard can retry the request
                # instead of running a call with truncated arguments.
                if (
                    pending_calls
                    and not saw_done_marker
                    and stop_reason is None
                    and any(_is_truncated_json(call["parts"]) for call in pending_calls.values())
                ):
                    log.warning(
                        "provider.tool_stream_truncated",
                        provider=self._provider_kind,
                        model=self._model,
                        open_tools=[call["name"] for call in pending_calls.values()],
                    )
                    return

                # Emit ToolUseEnd for each completed call
                for call in pending_calls.values():
                    if not call["started"]:
                        call["id"] = call["id"] or f"call_{uuid4().hex[:12]}"
                        call["started"] = True
                        yield ToolUseStartEvent(tool_use_id=call["id"], tool_name=call["name"])
                    full_json = "".join(call["parts"])
                    try:
                        args = json.loads(full_json) if full_json else {}
                    except json.JSONDecodeError:
                        args = {"_raw": full_json}
                    emitted_stream_event = True
                    yield ToolUseEndEvent(
                        tool_use_id=call["id"],
                        tool_name=call["name"],
                        arguments=args,
                        thought_signature=call.get("thought_signature"),
                    )

                # Last-resort text-protocol compatibility: some upstreams
                # leak a native XML tool call as text instead of structured
                # tool_calls. Only synthesize calls when no structured calls
                # arrived, tools were offered, and the parsed tool name is
                # explicitly allowed by this turn.
                synthesized_tool_call = False
                if not pending_calls and tools and assistant_text_parts:
                    full_text = "".join(assistant_text_parts)
                    for event in _synthesize_text_tool_events(full_text, tools):
                        emitted_stream_event = True
                        if isinstance(event, ToolUseEndEvent):
                            synthesized_tool_call = True
                        yield event

                # Assemble reasoning from structured fields
                reasoning_text = "".join(reasoning_parts)

                # Fallback: <think> tag extraction from accumulated text
                caps = cfg.model_capabilities
                if not reasoning_text and caps and caps.reasoning_format == "think_tags":
                    full_text = "".join(assistant_text_parts)
                    reasoning_text = _extract_think_tags(full_text)

                yield DoneEvent(
                    stop_reason=normalize_stop_reason(
                        stop_reason,
                        has_tool_calls=bool(pending_calls) or synthesized_tool_call,
                    ),
                    raw_stop_reason=stop_reason,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    reasoning_content=reasoning_text or None,
                    reasoning_tokens=reasoning_tokens,
                    cached_tokens=cached_tokens,
                    cache_write_tokens=cache_write_tokens,
                    billed_cost=billed_cost,
                    model=actual_model,
                    cost_source=cost_source,
                    reasoning_details=reasoning_details or None,
                    cache_usage_reported=usage_acc.cache_reads_reported,
                    usage_estimated=usage_acc.input_estimated,
                )

        except StreamIdleTimeoutError as exc:
            yield stream_idle_error_event(exc)
        except httpx.TimeoutException as exc:
            if self._provider_kind == "openrouter" and not emitted_stream_event:
                log.warning(
                    "openrouter.stream_timeout_fallback_started",
                    model=self._model,
                    timeout_seconds=cfg.timeout,
                    error=str(exc),
                )
                yield ProviderHeartbeatEvent(
                    phase="llm_fallback",
                    message="OpenRouter stream timed out; retrying without streaming.",
                )
                async for fallback_event in self._complete_non_stream(
                    payload=payload,
                    headers=headers,
                    cfg=cfg,
                    tools=tools,
                    timeout_exc=exc,
                ):
                    yield fallback_event
                return
            yield ErrorEvent(message=f"Request timed out: {exc}", code="timeout")
        except httpx.RequestError as exc:
            yield ErrorEvent(message=f"Request error: {exc}", code="request_error")

    async def _complete_non_stream(
        self,
        *,
        payload: dict[str, Any],
        headers: dict[str, str],
        cfg: ChatConfig,
        tools: list[ToolDefinition] | None,
        timeout_exc: httpx.TimeoutException,
    ) -> AsyncIterator[StreamEvent]:
        fallback_payload = dict(payload)
        fallback_payload["stream"] = False
        fallback_payload.pop("stream_options", None)
        fallback_headers = dict(headers)
        fallback_headers["Accept"] = "application/json"

        try:
            response = await self._http.get().post(
                self._api_url("/v1/chat/completions"),
                headers=fallback_headers,
                json=fallback_payload,
                timeout=stream_timeouts(cfg.timeout),
            )
        except httpx.TimeoutException:
            log.warning(
                "openrouter.non_stream_fallback_timeout",
                model=self._model,
                timeout_seconds=cfg.timeout,
                stream_error=str(timeout_exc),
            )
            yield ErrorEvent(message=f"Request timed out: {timeout_exc}", code="timeout")
            return
        except httpx.RequestError as exc:
            yield ErrorEvent(message=f"Request error: {exc}", code="request_error")
            return

        if response.status_code != 200:
            yield ErrorEvent(
                message=_format_chat_http_error(
                    self._provider_kind,
                    response.status_code,
                    response.text,
                ),
                code=str(response.status_code),
                retry_after_seconds=retry_after_from_response(response),
            )
            return

        try:
            data = response.json()
        except json.JSONDecodeError:
            yield ErrorEvent(message="Invalid JSON response from provider", code="invalid_json")
            return

        actual_model = data.get("model") or self._model
        (
            input_tokens,
            output_tokens,
            reasoning_tokens,
            cached_tokens,
            cache_write_tokens,
            raw_billed_cost,
        ) = _usage_fields(data.get("usage"))
        raw_usage = data.get("usage")
        cache_usage_reported = isinstance(raw_usage, Mapping) and _usage_reports_cache_reads(
            raw_usage
        )
        billed_cost, cost_source = _provider_billed_cost(self._provider_kind, raw_billed_cost)
        stop_reason: str | None = None
        assistant_text_parts: list[str] = []
        reasoning_parts: list[str] = []
        message_reasoning_details: list[dict[str, Any]] = []
        emitted_structured_tool = False

        for choice in data.get("choices") or []:
            if choice.get("finish_reason"):
                stop_reason = choice["finish_reason"]
            message = choice.get("message") or {}

            text = message.get("content")
            if isinstance(text, str) and text:
                assistant_text_parts.append(text)
                yield TextDeltaEvent(text=text)

            reasoning_details = message.get("reasoning_details")
            if reasoning_details:
                for detail in reasoning_details:
                    if isinstance(detail, dict):
                        message_reasoning_details.append(detail)
                        text_val = detail.get("text", "")
                        if text_val:
                            reasoning_parts.append(text_val)
            for key in ("reasoning_content", "reasoning"):
                reasoning_str = message.get(key)
                if isinstance(reasoning_str, str) and reasoning_str:
                    reasoning_parts.append(reasoning_str)

            for tc in message.get("tool_calls") or []:
                function = tc.get("function") or {}
                tool_use_id = tc.get("id") or f"call_{uuid4().hex[:12]}"
                tool_name = function.get("name") or ""
                arguments_text = function.get("arguments") or ""
                thought_sig = _extract_thought_signature(tc)
                emitted_structured_tool = True
                yield ToolUseStartEvent(tool_use_id=tool_use_id, tool_name=tool_name)
                if arguments_text:
                    yield ToolUseDeltaEvent(
                        tool_use_id=tool_use_id,
                        json_fragment=arguments_text,
                    )
                try:
                    arguments = json.loads(arguments_text) if arguments_text else {}
                except json.JSONDecodeError:
                    arguments = {"_raw": arguments_text}
                yield ToolUseEndEvent(
                    tool_use_id=tool_use_id,
                    tool_name=tool_name,
                    arguments=arguments,
                    thought_signature=thought_sig,
                )

        if not emitted_structured_tool and tools and assistant_text_parts:
            for event in _synthesize_text_tool_events("".join(assistant_text_parts), tools):
                if isinstance(event, ToolUseEndEvent):
                    emitted_structured_tool = True
                yield event

        reasoning_text = "".join(reasoning_parts)
        if (
            not reasoning_text
            and cfg.model_capabilities
            and cfg.model_capabilities.reasoning_format == "think_tags"
        ):
            reasoning_text = _extract_think_tags("".join(assistant_text_parts))

        yield DoneEvent(
            stop_reason=normalize_stop_reason(stop_reason, has_tool_calls=emitted_structured_tool),
            raw_stop_reason=stop_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_content=reasoning_text or None,
            reasoning_tokens=reasoning_tokens,
            cached_tokens=cached_tokens,
            cache_write_tokens=cache_write_tokens,
            billed_cost=billed_cost,
            model=actual_model,
            cost_source=cost_source,
            reasoning_details=message_reasoning_details or None,
            cache_usage_reported=cache_usage_reported,
            usage_estimated=isinstance(raw_usage, Mapping) and bool(raw_usage.get("estimated")),
        )

    async def list_models(self) -> list[ModelInfo]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        if self._provider_kind in {"opencap", "bankr", "surplus"}:
            headers["x-api-key"] = self._api_key
        headers.update(openrouter_app_headers(self._base_url))
        try:
            client = self._http.get()
            resp = await client.get(self._api_url("/v1/models"), headers=headers, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
            return [
                ModelInfo(
                    provider=self._provider_kind,
                    model_id=m["id"],
                    display_name=m.get("name", m.get("id", "")),
                    context_window=m.get("context_length", 0),
                    max_output_tokens=(m.get("top_provider") or {}).get("max_completion_tokens")
                    or 0,
                )
                for m in data.get("data", [])
            ]
        except Exception:
            return []
