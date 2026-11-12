"""OpenAI Responses API provider path.

This provider intentionally stays separate from the OpenAI-compatible Chat
Completions provider because Responses uses item-shaped input/output and native
state protocols that should evolve independently.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import Any
from uuid import uuid4

import httpx

from agentos.env import trust_env as _trust_env
from agentos.secrets import clean_header_secret

from .error_body import read_bounded_body
from .openai import _http_error_body_text, _openai_tool_result_content, _resolve_llm_proxy
from .protocol import ProviderConnectionConfig, ProviderMetadata
from .replay import prepare_replay, provenance_for_provider
from .retry_after import retry_after_from_response
from .transport import (
    PooledHttpClient,
    StreamIdleTimeoutError,
    iter_lines_with_idle_timeout,
    resolve_stream_idle_timeout,
    stream_idle_error_event,
    stream_timeouts,
)
from .types import (
    ChatConfig,
    ContentBlockImage,
    ContentBlockText,
    ContentBlockToolResult,
    ContentBlockToolUse,
    DoneEvent,
    ErrorEvent,
    Message,
    ModelInfo,
    StopReason,
    StreamEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolDefinition,
    ToolUseDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    normalize_stop_reason,
)

_OPENAI_RESPONSES_BASE = "https://api.openai.com/v1"

#: ``format`` marker on a Responses ``reasoning`` output item kept in
#: :attr:`Message.reasoning_details`. The item (id, encrypted_content,
#: summary) is replayed verbatim before the function calls it preceded, so a
#: ``store: false`` tool loop keeps its reasoning across calls.
RESPONSES_REASONING_FORMAT = "openai-responses-native"

#: Responses rejects ``max_output_tokens`` below 16 (checked live 2026-11-12).
_MIN_MAX_OUTPUT_TOKENS = 16


def _responses_stop_reason(
    data: dict[str, Any],
    *,
    emitted_tool: bool,
    refusal_text: str | None,
) -> tuple[StopReason, str | None, dict[str, Any] | None]:
    """Normalize a Responses ``status`` / ``incomplete_details`` pair.

    ``incomplete`` carries its cause in ``incomplete_details.reason``
    (``max_output_tokens`` or ``content_filter``); ``completed`` is an end of
    turn, or ``tool_use`` when function calls were emitted. A ``refusal``
    content part is a model decline even on a completed response.
    """

    raw_status = data.get("status")
    status = raw_status if isinstance(raw_status, str) and raw_status else None
    details = data.get("incomplete_details")
    reason = details.get("reason") if isinstance(details, dict) else None
    raw: str | None = status
    if status == "incomplete":
        raw = f"incomplete:{reason}" if isinstance(reason, str) and reason else status
        normalized = (
            normalize_stop_reason(reason)
            if isinstance(reason, str) and reason
            else StopReason.UNKNOWN
        )
    elif status is None or status == "completed":
        normalized = StopReason.TOOL_USE if emitted_tool else StopReason.END_TURN
    else:
        normalized = StopReason.UNKNOWN
    if refusal_text is not None:
        return StopReason.REFUSAL, raw, {"type": "refusal", "explanation": refusal_text}
    if normalized is StopReason.REFUSAL:
        return normalized, raw, {"type": "refusal", "category": reason}
    return normalized, raw, None


def _responses_tool(tool: ToolDefinition) -> dict[str, Any]:
    # ``strict`` defaults to true on /v1/responses, which rewrites the schema
    # (every property required, additionalProperties false) and so turns
    # optional arguments into mandatory ones. Chat Completions defaults to
    # false; send it explicitly to keep the same contract on both APIs.
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.input_schema.model_dump(exclude_none=True),
        "strict": bool(tool.strict),
    }


def _responses_tool_from_chat(tool: Mapping[str, Any]) -> dict[str, Any]:
    """A Chat Completions ``tools[]`` entry in the flat Responses shape."""
    function = tool.get("function")
    if tool.get("type") != "function" or not isinstance(function, Mapping):
        return dict(tool)
    converted: dict[str, Any] = {
        "type": "function",
        "name": function.get("name") or "",
        "parameters": function.get("parameters") or {"type": "object", "properties": {}},
        "strict": bool(function.get("strict")),
    }
    if function.get("description") is not None:
        converted["description"] = function.get("description")
    return converted


def _build_tool_choice_payload(tool_choice: Any) -> dict[str, Any] | str:
    """Translate a ChatConfig.tool_choice into the Responses API format.

    Callers pass either a bare string (``"auto"``/``"none"``/``"required"``)
    or the OpenAI Chat-Completions-style forced-tool dict
    (``{"type": "function", "function": {"name": ...}}``). The Responses API
    expects the FLAT forced-function shape ``{"type": "function", "name": ...}``
    (matching ``_responses_tool``), so the nested Chat-Completions form must be
    flattened here rather than forwarded verbatim.
    """
    if tool_choice is None:
        return "auto"
    if isinstance(tool_choice, str):
        return tool_choice
    if not isinstance(tool_choice, dict):
        return "auto"
    if tool_choice.get("type") == "function":
        # Accept both the nested Chat-Completions form and the already-flat form.
        name = tool_choice.get("name") or (tool_choice.get("function") or {}).get("name")
        return {"type": "function", "name": name} if name else "auto"
    return dict(tool_choice)


def _responses_message_item(role: str, content: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "message", "role": role, "content": content}


def _is_replayable_reasoning_item(detail: Any) -> bool:
    return (
        isinstance(detail, Mapping)
        and detail.get("format") == RESPONSES_REASONING_FORMAT
        and detail.get("type") == "reasoning"
        and isinstance(detail.get("encrypted_content"), str)
        and bool(detail.get("encrypted_content"))
    )


def _replay_reasoning_item(detail: Mapping[str, Any]) -> dict[str, Any]:
    """A stored reasoning item in the shape /v1/responses takes as input."""
    item: dict[str, Any] = {
        "type": "reasoning",
        "summary": list(detail.get("summary") or []),
        "encrypted_content": detail["encrypted_content"],
    }
    if isinstance(detail.get("id"), str) and detail["id"]:
        item["id"] = detail["id"]
    return item


def _stored_reasoning_item(item: Mapping[str, Any]) -> dict[str, Any] | None:
    """A ``reasoning`` output item as kept on ``Message.reasoning_details``."""
    encrypted = item.get("encrypted_content")
    if not isinstance(encrypted, str) or not encrypted:
        return None
    stored: dict[str, Any] = {
        "type": "reasoning",
        "format": RESPONSES_REASONING_FORMAT,
        "encrypted_content": encrypted,
        "summary": [
            {"type": "summary_text", "text": part.get("text") or ""}
            for part in item.get("summary") or []
            if isinstance(part, Mapping) and part.get("type") == "summary_text"
        ],
    }
    if isinstance(item.get("id"), str) and item["id"]:
        stored["id"] = item["id"]
    return stored


def reasoning_items_by_tool_call(messages: list[Message]) -> dict[str, list[dict[str, Any]]]:
    """Replayable reasoning items keyed by the first tool-call id they preceded.

    Only reasoning that led to tool calls is worth replaying: OpenAI drops
    reasoning items from before the latest user message anyway, while inside
    a tool loop they let the model continue from its own reasoning instead of
    re-deriving it. ``prepare_replay`` has already removed another model's
    ``reasoning_details`` from *messages*.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for message in messages:
        if message.role != "assistant" or not message.reasoning_details:
            continue
        if isinstance(message.content, str):
            continue
        first_call = next(
            (block.id for block in message.content if isinstance(block, ContentBlockToolUse)),
            None,
        )
        if not first_call:
            continue
        items = [
            _replay_reasoning_item(detail)
            for detail in message.reasoning_details
            if _is_replayable_reasoning_item(detail)
        ]
        if items:
            out[first_call] = items
    return out


def _responses_image(block: ContentBlockImage) -> dict[str, Any]:
    url = (
        block.data if block.source_type == "url" else f"data:{block.media_type};base64,{block.data}"
    )
    return {"type": "input_image", "image_url": url}


def _responses_input(messages: list[Message]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message.content, str):
            items.append({"role": message.role, "content": message.content})
            continue

        pending_content: list[dict[str, Any]] = []
        if message.role == "assistant" and message.reasoning_details:
            items.extend(
                _replay_reasoning_item(detail)
                for detail in message.reasoning_details
                if _is_replayable_reasoning_item(detail)
            )

        def flush_pending_message() -> None:
            if pending_content:
                items.append(_responses_message_item(message.role, list(pending_content)))
                pending_content.clear()

        for block in message.content:
            if isinstance(block, ContentBlockText):
                content_type = "output_text" if message.role == "assistant" else "input_text"
                pending_content.append({"type": content_type, "text": block.text})
            elif isinstance(block, ContentBlockImage) and message.role != "assistant":
                pending_content.append(_responses_image(block))
            elif isinstance(block, ContentBlockToolUse):
                flush_pending_message()
                items.append(
                    {
                        "type": "function_call",
                        "call_id": block.id,
                        "name": block.name,
                        "arguments": json.dumps(block.input, ensure_ascii=False),
                    }
                )
            elif isinstance(block, ContentBlockToolResult):
                flush_pending_message()
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": block.tool_use_id,
                        "output": _openai_tool_result_content(block),
                    }
                )
        flush_pending_message()
    return items


def _usage_fields(usage: Any) -> tuple[int, int, int, int, int]:
    """``(input, output, reasoning, cached, cache_write)`` of a Responses usage.

    As on Chat Completions, ``input_tokens`` includes the cached part and
    ``output_tokens`` includes the reasoning part.
    """
    if not isinstance(usage, dict):
        return 0, 0, 0, 0, 0
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    input_details = usage.get("input_tokens_details")
    cached_tokens = (
        int(input_details.get("cached_tokens") or 0) if isinstance(input_details, dict) else 0
    )
    cache_write_tokens = (
        int(input_details.get("cache_write_tokens") or 0) if isinstance(input_details, dict) else 0
    )
    output_details = usage.get("output_tokens_details")
    reasoning_tokens = (
        int(output_details.get("reasoning_tokens") or 0) if isinstance(output_details, dict) else 0
    )
    return input_tokens, output_tokens, reasoning_tokens, cached_tokens, cache_write_tokens


def _usage_reports_cache_reads(usage: Any) -> bool:
    details = usage.get("input_tokens_details") if isinstance(usage, dict) else None
    return isinstance(details, dict) and "cached_tokens" in details


def _chat_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, Mapping) and part.get("type") == "text"
        )
    return ""


def _chat_user_content(content: Any) -> str | list[dict[str, Any]]:
    if not isinstance(content, list):
        return content if isinstance(content, str) else ""
    parts: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, Mapping):
            continue
        if part.get("type") == "text":
            parts.append({"type": "input_text", "text": str(part.get("text") or "")})
        elif part.get("type") == "image_url":
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, Mapping) else image
            if isinstance(url, str) and url:
                converted: dict[str, Any] = {"type": "input_image", "image_url": url}
                if isinstance(image, Mapping) and image.get("detail"):
                    converted["detail"] = image["detail"]
                parts.append(converted)
    return parts


def responses_input_from_chat_messages(
    chat_messages: list[dict[str, Any]],
    *,
    reasoning_by_call: Mapping[str, list[dict[str, Any]]] | None = None,
) -> tuple[str | None, list[dict[str, Any]]]:
    """``(instructions, input)`` for a Chat Completions ``messages`` list.

    The Chat Completions provider builds (and budget-checks) its usual payload
    first; this turns that payload's messages into Responses input items.
    *reasoning_by_call* (see :func:`reasoning_items_by_tool_call`) puts each
    stored reasoning item back in front of the tool calls it preceded.
    """
    instructions: list[str] = []
    items: list[dict[str, Any]] = []
    reasoning = reasoning_by_call or {}
    for message in chat_messages:
        role = message.get("role")
        if role in {"system", "developer"}:
            text = _chat_text(message.get("content"))
            if text:
                instructions.append(text)
        elif role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": str(message.get("tool_call_id") or ""),
                    "output": _chat_text(message.get("content")),
                }
            )
        elif role == "assistant":
            tool_calls = [
                call for call in message.get("tool_calls") or [] if isinstance(call, Mapping)
            ]
            if tool_calls:
                items.extend(reasoning.get(str(tool_calls[0].get("id") or ""), []))
            text = _chat_text(message.get("content"))
            if text:
                items.append(
                    _responses_message_item("assistant", [{"type": "output_text", "text": text}])
                )
            for call in tool_calls:
                function = call.get("function")
                function = function if isinstance(function, Mapping) else {}
                items.append(
                    {
                        "type": "function_call",
                        "call_id": str(call.get("id") or ""),
                        "name": str(function.get("name") or ""),
                        "arguments": str(function.get("arguments") or "{}"),
                    }
                )
        elif role == "user":
            content = _chat_user_content(message.get("content"))
            if isinstance(content, str):
                items.append({"role": "user", "content": content})
            elif content:
                items.append(_responses_message_item("user", content))
    return ("\n\n".join(instructions) or None), items


def responses_payload_from_chat(
    payload: Mapping[str, Any],
    *,
    reasoning_effort: str | None,
    reasoning_by_call: Mapping[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """A /v1/responses request equivalent to a Chat Completions *payload*.

    Carries over what the Responses API accepts (tools, tool choice, parallel
    calls, prompt cache key, output budget) and drops what it refuses for the
    reasoning models this path serves: ``stop`` is an unknown parameter and
    ``temperature`` is unsupported. With *reasoning_effort* set, the request
    asks for a reasoning summary (streamed as thinking) and the encrypted
    reasoning items, which ``store: false`` needs to replay them.
    """
    instructions, input_items = responses_input_from_chat_messages(
        list(payload.get("messages") or []),
        reasoning_by_call=reasoning_by_call,
    )
    max_tokens = payload.get("max_completion_tokens") or payload.get("max_tokens")
    out: dict[str, Any] = {
        "model": payload.get("model"),
        "input": input_items,
        "store": False,
        "stream": True,
    }
    if instructions:
        out["instructions"] = instructions
    if isinstance(max_tokens, int):
        out["max_output_tokens"] = max(max_tokens, _MIN_MAX_OUTPUT_TOKENS)
    tools = payload.get("tools")
    if tools:
        out["tools"] = [_responses_tool_from_chat(tool) for tool in tools]
        if "tool_choice" in payload:
            out["tool_choice"] = _build_tool_choice_payload(payload["tool_choice"])
        if "parallel_tool_calls" in payload:
            out["parallel_tool_calls"] = payload["parallel_tool_calls"]
    if payload.get("prompt_cache_key"):
        out["prompt_cache_key"] = payload["prompt_cache_key"]
    if reasoning_effort:
        out["reasoning"] = {"effort": reasoning_effort, "summary": "auto"}
        if reasoning_effort != "none":
            out["include"] = ["reasoning.encrypted_content"]
    return out


class _ResponsesEventMapper:
    """Turn Responses output items / stream events into AgentOS stream events."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.emitted_tool = False
        self.refusal_text: str | None = None
        self.reasoning_parts: list[str] = []
        self.reasoning_details: list[dict[str, Any]] = []
        self.final: dict[str, Any] | None = None
        # item id -> call_id / name of a function call still streaming
        self._open_calls: dict[str, tuple[str, str]] = {}
        self._finished_calls: set[str] = set()

    # -- whole items (non-streamed responses and ``output_item.done``) -------

    def _function_call_events(
        self,
        item: Mapping[str, Any],
        *,
        started: bool,
    ) -> list[StreamEvent]:
        call_id = str(item.get("call_id") or item.get("id") or f"call_{uuid4().hex[:12]}")
        if call_id in self._finished_calls:
            return []
        self._finished_calls.add(call_id)
        self.emitted_tool = True
        tool_name = str(item.get("name") or "")
        arguments_text = str(item.get("arguments") or "")
        events: list[StreamEvent] = []
        if not started:
            events.append(ToolUseStartEvent(tool_use_id=call_id, tool_name=tool_name))
            if arguments_text:
                events.append(ToolUseDeltaEvent(tool_use_id=call_id, json_fragment=arguments_text))
        try:
            arguments = json.loads(arguments_text) if arguments_text else {}
        except json.JSONDecodeError:
            arguments = {"_raw": arguments_text}
        events.append(
            ToolUseEndEvent(tool_use_id=call_id, tool_name=tool_name, arguments=arguments)
        )
        return events

    def _record_message_item(
        self, item: Mapping[str, Any], *, emit_text: bool
    ) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        for part in item.get("content") or []:
            if not isinstance(part, Mapping):
                continue
            if part.get("type") == "output_text" and emit_text:
                text = part.get("text")
                if isinstance(text, str) and text:
                    events.append(TextDeltaEvent(text=text))
            elif part.get("type") == "refusal":
                refusal = part.get("refusal")
                self.refusal_text = refusal if isinstance(refusal, str) else ""
        return events

    def _record_reasoning_item(
        self, item: Mapping[str, Any], *, emit_text: bool
    ) -> list[StreamEvent]:
        stored = _stored_reasoning_item(item)
        if stored is not None:
            self.reasoning_details.append(stored)
        if not emit_text:
            return []
        events: list[StreamEvent] = []
        for part in item.get("summary") or []:
            text = part.get("text") if isinstance(part, Mapping) else None
            if isinstance(text, str) and text:
                if self.reasoning_parts:
                    self.reasoning_parts.append("\n\n")
                self.reasoning_parts.append(text)
                events.append(ThinkingDeltaEvent(text=text))
        return events

    def output_item(self, item: Any, *, streamed: bool) -> list[StreamEvent]:
        """Events for one complete output item.

        *streamed* means its text and argument deltas were already emitted, so
        only the parts deltas do not carry (tool-call end, encrypted reasoning,
        refusals) are produced here.
        """
        if not isinstance(item, Mapping):
            return []
        item_type = item.get("type")
        if item_type == "message":
            return self._record_message_item(item, emit_text=not streamed)
        if item_type == "function_call":
            item_id = str(item.get("id") or "")
            started = streamed and item_id in self._open_calls
            self._open_calls.pop(item_id, None)
            return self._function_call_events(item, started=started)
        if item_type == "reasoning":
            return self._record_reasoning_item(item, emit_text=not streamed)
        return []

    # -- stream events ---------------------------------------------------------

    def stream_event(self, event: Mapping[str, Any]) -> list[StreamEvent]:
        event_type = event.get("type")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            return [TextDeltaEvent(text=delta)] if isinstance(delta, str) and delta else []
        if event_type == "response.reasoning_summary_part.added":
            if self.reasoning_parts:
                self.reasoning_parts.append("\n\n")
                return [ThinkingDeltaEvent(text="\n\n")]
            return []
        if event_type == "response.reasoning_summary_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                self.reasoning_parts.append(delta)
                return [ThinkingDeltaEvent(text=delta)]
            return []
        if event_type == "response.output_item.added":
            item = event.get("item")
            if isinstance(item, Mapping) and item.get("type") == "function_call":
                call_id = str(item.get("call_id") or item.get("id") or "")
                if call_id:
                    self._open_calls[str(item.get("id") or call_id)] = (
                        call_id,
                        str(item.get("name") or ""),
                    )
                    return [
                        ToolUseStartEvent(
                            tool_use_id=call_id, tool_name=str(item.get("name") or "")
                        )
                    ]
            return []
        if event_type == "response.function_call_arguments.delta":
            open_call = self._open_calls.get(str(event.get("item_id") or ""))
            delta = event.get("delta")
            if open_call and isinstance(delta, str) and delta:
                return [ToolUseDeltaEvent(tool_use_id=open_call[0], json_fragment=delta)]
            return []
        if event_type == "response.output_item.done":
            return self.output_item(event.get("item"), streamed=True)
        if event_type in {"response.completed", "response.incomplete"}:
            response = event.get("response")
            if isinstance(response, dict):
                self.final = response
        return []

    @property
    def has_open_tool_call(self) -> bool:
        return bool(self._open_calls)

    def done_event(self, data: Mapping[str, Any]) -> DoneEvent:
        usage = data.get("usage")
        input_tokens, output_tokens, reasoning_tokens, cached_tokens, cache_write_tokens = (
            _usage_fields(usage)
        )
        stop_reason, raw_stop_reason, stop_details = _responses_stop_reason(
            dict(data),
            emitted_tool=self.emitted_tool,
            refusal_text=self.refusal_text,
        )
        return DoneEvent(
            stop_reason=stop_reason,
            raw_stop_reason=raw_stop_reason,
            stop_details=stop_details,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_content="".join(self.reasoning_parts) or None,
            reasoning_tokens=reasoning_tokens,
            cached_tokens=cached_tokens,
            cache_write_tokens=cache_write_tokens,
            reasoning_details=self.reasoning_details or None,
            cache_usage_reported=_usage_reports_cache_reads(usage),
            model=str(data.get("model") or self.model),
        )


def _response_failed_event(event: Mapping[str, Any]) -> ErrorEvent:
    """An ``error`` / ``response.failed`` stream event as an ErrorEvent."""
    raw: Any = event.get("error")
    if raw is None:
        response = event.get("response")
        raw = response.get("error") if isinstance(response, Mapping) else None
    if raw is None:
        raw = {"message": event.get("message")} if event.get("message") else event
    if isinstance(raw, Mapping):
        code = raw.get("code") or raw.get("type")
        message = raw.get("message")
        if not isinstance(message, str) or not message:
            message = json.dumps(raw, ensure_ascii=False)
    else:
        code, message = None, str(raw)
    code_text = str(code) if code not in (None, "") else "stream_error"
    return ErrorEvent(message=f"{code_text}: {message}", code=code_text)


def _http_error_event(status_code: int, body: bytes | str, response: httpx.Response) -> ErrorEvent:
    detail = _http_error_body_text(body)
    message = f"OpenAI Responses API error {status_code}"
    if detail:
        message = f"{message}: {detail}"
    return ErrorEvent(
        message=message,
        code=str(status_code),
        retry_after_seconds=retry_after_from_response(response),
    )


async def stream_responses(
    *,
    http: PooledHttpClient,
    url: str,
    headers: Mapping[str, str],
    payload: dict[str, Any],
    config: ChatConfig,
    model: str,
) -> AsyncIterator[StreamEvent]:
    """POST a streaming /v1/responses request and map its events.

    A JSON (non-SSE) body is accepted too and mapped from its ``output``. A
    stream that closes before ``response.completed`` / ``incomplete`` ends
    without a DoneEvent, so the tool-stream guard can retry a cut tool call.
    """
    request_headers = {**headers, "Accept": "text/event-stream"}
    mapper = _ResponsesEventMapper(model)
    idle_timeout = resolve_stream_idle_timeout(config.stream_idle_timeout)
    try:
        async with http.get().stream(
            "POST",
            url,
            headers=request_headers,
            json=payload,
            timeout=stream_timeouts(config.timeout),
        ) as response:
            if response.status_code != 200:
                body = await read_bounded_body(response)
                yield _http_error_event(response.status_code, body, response)
                return
            content_type = response.headers.get("content-type", "").lower()
            if "text/event-stream" not in content_type:
                raw = await response.aread()
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    yield ErrorEvent(
                        message="Invalid JSON response from OpenAI Responses API",
                        code="invalid_json",
                    )
                    return
                if not isinstance(data, dict):
                    yield ErrorEvent(
                        message="Invalid response shape from OpenAI Responses API",
                        code="invalid_json",
                    )
                    return
                for item in data.get("output") or []:
                    for mapped in mapper.output_item(item, streamed=False):
                        yield mapped
                yield mapper.done_event(data)
                return

            async for line in iter_lines_with_idle_timeout(response.aiter_lines(), idle_timeout):
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str or data_str == "[DONE]":
                    continue
                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") in {"error", "response.failed"}:
                    yield _response_failed_event(event)
                    return
                for mapped in mapper.stream_event(event):
                    yield mapped
                if mapper.final is not None:
                    break
    except StreamIdleTimeoutError as exc:
        yield stream_idle_error_event(exc)
        return
    except httpx.TimeoutException as exc:
        yield ErrorEvent(message=f"Request timed out: {exc}", code="timeout")
        return
    except httpx.RequestError as exc:
        yield ErrorEvent(message=f"Request error: {exc}", code="request_error")
        return

    if mapper.final is None:
        if mapper.has_open_tool_call:
            # Cut inside a tool call: end without Done so the guard retries.
            return
        yield ErrorEvent(
            message="OpenAI Responses stream ended before the response completed",
            code="stream_incomplete",
        )
        return
    yield mapper.done_event(mapper.final)


class OpenAIResponsesProvider:
    """OpenAI native Responses API provider.

    The initial implementation supports text and function-call event mapping
    with stateless requests (`store: false`). Provider-native compaction/item
    replay is added in later continuity work.
    """

    provider_name = "openai_responses"

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-5.4",
        base_url: str = _OPENAI_RESPONSES_BASE,
        org_id: str | None = None,
        proxy: str | None = None,
    ) -> None:
        self._api_key = clean_header_secret(api_key, label="LLM API key")
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._org_id = org_id
        self._proxy = _resolve_llm_proxy(proxy)
        self._http = PooledHttpClient(proxy=self._proxy)

    async def aclose(self) -> None:
        """Close the pooled HTTP client (a later request opens a new one)."""
        await self._http.aclose()

    @property
    def model(self) -> str:
        return self._model

    def provider_metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider_name=self.provider_name,
            provider_kind="openai_responses",
            model=self._model,
            base_url=self._base_url,
        )

    def provider_connection_config(self) -> ProviderConnectionConfig:
        return ProviderConnectionConfig(
            provider_kind="openai_responses",
            model=self._model,
            api_key=self._api_key,
            base_url=self._base_url,
        )

    def _api_url(self, path: str) -> str:
        if self._base_url.endswith("/v1") and path.startswith("/v1/"):
            return f"{self._base_url}{path[3:]}"
        return f"{self._base_url}{path}"

    def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[StreamEvent]:
        return self.chat_items(
            _responses_input(prepare_replay(messages, provenance_for_provider(self))),
            tools=tools,
            config=config or ChatConfig(),
        )

    def chat_items(
        self,
        input_items: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a Responses request from canonical Responses input items."""

        return self._complete_items(input_items, tools=tools, config=config or ChatConfig())

    async def _complete_items(
        self,
        input_items: list[dict[str, Any]],
        *,
        tools: list[ToolDefinition] | None,
        config: ChatConfig,
    ) -> AsyncIterator[StreamEvent]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._org_id:
            headers["OpenAI-Organization"] = self._org_id

        payload: dict[str, Any] = {
            "model": self._model,
            "input": input_items,
            "max_output_tokens": config.max_tokens,
            "store": False,
        }
        if config.system:
            payload["instructions"] = config.system
        if config.temperature is not None:
            payload["temperature"] = config.temperature
        # No ``stop``: /v1/responses answers it with "Unknown parameter".
        if tools:
            payload["tools"] = [_responses_tool(tool) for tool in tools]
            payload["tool_choice"] = _build_tool_choice_payload(config.tool_choice)

        try:
            response = await self._http.get().post(
                self._api_url("/v1/responses"),
                headers=headers,
                json=payload,
                timeout=stream_timeouts(config.timeout),
            )
        except httpx.TimeoutException as exc:
            yield ErrorEvent(message=f"Request timed out: {exc}", code="timeout")
            return
        except httpx.RequestError as exc:
            yield ErrorEvent(message=f"Request error: {exc}", code="request_error")
            return

        if response.status_code != 200:
            detail = _http_error_body_text(response.text)
            message = f"OpenAI Responses API error {response.status_code}"
            if detail:
                message = f"{message}: {detail}"
            yield ErrorEvent(
                message=message,
                code=str(response.status_code),
                retry_after_seconds=retry_after_from_response(response),
            )
            return

        try:
            data = response.json()
        except json.JSONDecodeError:
            yield ErrorEvent(
                message="Invalid JSON response from OpenAI Responses API",
                code="invalid_json",
            )
            return

        if not isinstance(data, dict):
            yield ErrorEvent(
                message="Invalid response shape from OpenAI Responses API",
                code="invalid_json",
            )
            return
        mapper = _ResponsesEventMapper(self._model)
        for item in data.get("output") or []:
            for event in mapper.output_item(item, streamed=False):
                yield event
        yield mapper.done_event(data)

    async def list_models(self) -> list[ModelInfo]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            async with httpx.AsyncClient(
                timeout=30,
                trust_env=_trust_env(),
                proxy=self._proxy,
            ) as client:
                response = await client.get(self._api_url("/v1/models"), headers=headers)
        except httpx.HTTPError:
            return []

        if response.status_code != 200:
            return []
        try:
            data = response.json()
        except json.JSONDecodeError:
            return []

        models: list[ModelInfo] = []
        for raw in data.get("data", []):
            model_id = raw.get("id") if isinstance(raw, dict) else None
            if isinstance(model_id, str):
                models.append(
                    ModelInfo(
                        provider=self.provider_name,
                        model_id=model_id,
                        display_name=raw.get("name") or model_id,
                    )
                )
        return models

    async def compact_window(
        self,
        input_items: list[dict[str, Any]],
        *,
        config: ChatConfig | None = None,
    ) -> dict[str, Any]:
        """Call `/responses/compact` and return the raw compact response.

        The returned `output` is an opaque canonical context window. Callers
        must store and later replay it without pruning or inspecting internals.
        """

        cfg = config or ChatConfig()
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._org_id:
            headers["OpenAI-Organization"] = self._org_id

        async with httpx.AsyncClient(
            timeout=cfg.timeout,
            trust_env=_trust_env(),
            proxy=self._proxy,
        ) as client:
            response = await client.post(
                self._api_url("/v1/responses/compact"),
                headers=headers,
                json={"model": self._model, "input": input_items},
            )

        if response.status_code != 200:
            detail = _http_error_body_text(response.text)
            message = f"OpenAI Responses compact API error {response.status_code}"
            if detail:
                message = f"{message}: {detail}"
            raise RuntimeError(message)

        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError("Invalid JSON response from OpenAI Responses compact API") from exc
        if not isinstance(data, dict):
            raise RuntimeError("Invalid response shape from OpenAI Responses compact API")
        return data
