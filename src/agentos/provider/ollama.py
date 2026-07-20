"""OllamaProvider — streams via Ollama local API using httpx."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from agentos.env import trust_env as _trust_env

from .types import (
    ChatConfig,
    DoneEvent,
    ErrorEvent,
    Message,
    ModelInfo,
    StreamEvent,
    TextDeltaEvent,
    ToolDefinition,
    ToolUseDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
)

_OLLAMA_DEFAULT_BASE = "http://localhost:11434"


def _build_ollama_tool(tool: ToolDefinition) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema.model_dump(exclude_none=True),
        },
    }


def _tool_result_content(content: Any) -> str:
    return content if isinstance(content, str) else json.dumps(content)


def _build_ollama_message(
    msg: Message,
    tool_names_by_id: dict[str, str] | None = None,
) -> dict[str, Any]:
    if isinstance(msg.content, str):
        return {"role": msg.role, "content": msg.content}

    tool_names = tool_names_by_id if tool_names_by_id is not None else {}
    parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in msg.content:
        if block.type == "text":
            parts.append(block.text)
        elif block.type == "tool_use":
            tool_names[block.id] = block.name
            tool_calls.append(
                {
                    "id": block.id,
                    "type": "function",
                    "function": {
                        "name": block.name,
                        "arguments": block.input,
                    },
                }
            )
        elif block.type == "tool_result":
            tool_result_message: dict[str, Any] = {
                "role": "tool",
                "content": _tool_result_content(block.content),
            }
            tool_name = tool_names.get(block.tool_use_id)
            if tool_name:
                tool_result_message["tool_name"] = tool_name
            return tool_result_message

    result: dict[str, Any] = {"role": msg.role, "content": " ".join(parts)}
    if tool_calls:
        result["tool_calls"] = tool_calls
    return result


def _build_ollama_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Translate canonical history without dropping Ollama's tool-call pairing."""

    result: list[dict[str, Any]] = []
    tool_names_by_id: dict[str, str] = {}
    for message in messages:
        if not isinstance(message.content, str):
            tool_results = [block for block in message.content if block.type == "tool_result"]
            if tool_results:
                for block in tool_results:
                    tool_result = {
                        "role": "tool",
                        "content": _tool_result_content(block.content),
                    }
                    tool_name = tool_names_by_id.get(block.tool_use_id)
                    if tool_name:
                        tool_result["tool_name"] = tool_name
                    result.append(tool_result)
                continue
        result.append(_build_ollama_message(message, tool_names_by_id))
    return result


def _normalize_tool_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return {"_raw": arguments}
        if isinstance(parsed, dict):
            return parsed
    return {"_raw": arguments}


class OllamaProvider:
    """Streams from a local Ollama instance using the /api/chat endpoint."""

    provider_name = "ollama"

    def __init__(
        self,
        model: str = "llama3",
        base_url: str = _OLLAMA_DEFAULT_BASE,
        proxy: str | None = None,
    ) -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._proxy = proxy or None

    @property
    def model(self) -> str:
        """Model id this provider was configured with.

        Public so callers (e.g. derived-cache key construction) can identify
        the underlying model without prying at private state.
        """
        return self._model

    def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[StreamEvent]:
        cfg = config or ChatConfig()
        return self._stream(messages, tools, cfg)

    async def _stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        cfg: ChatConfig,
    ) -> AsyncIterator[StreamEvent]:
        ollama_messages: list[dict[str, Any]] = []
        if cfg.system:
            ollama_messages.append({"role": "system", "content": cfg.system})
        ollama_messages.extend(_build_ollama_messages(messages))

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": ollama_messages,
            "stream": True,
            "options": {"num_predict": cfg.max_tokens},
        }
        if cfg.temperature is not None:
            payload["options"]["temperature"] = cfg.temperature
        if tools:
            payload["tools"] = [_build_ollama_tool(t) for t in tools]
            # Ollama's native /api/chat exposes no forced tool_choice parameter,
            # so cfg.tool_choice cannot be honored here. A caller that forces a
            # tool (e.g. the LLM router judge) degrades to its text-JSON parse
            # fallback rather than getting a guaranteed tool call.

        input_tokens = 0
        output_tokens = 0
        done_reason = "stop"
        response_model = self._model
        # Ollama tool calls accumulate in the full response (not streamed per-chunk)
        pending_tool_calls: list[dict[str, Any]] = []

        try:
            async with httpx.AsyncClient(
                timeout=cfg.timeout,
                trust_env=_trust_env(),
                proxy=self._proxy,
            ) as client:
                async with client.stream(
                    "POST",
                    f"{self._base_url}/api/chat",
                    json=payload,
                ) as response:
                    if response.status_code != 200:
                        body = await response.aread()
                        yield ErrorEvent(
                            message=f"HTTP {response.status_code}: {body.decode()}",
                            code=str(response.status_code),
                        )
                        return

                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        msg_chunk = chunk.get("message", {})
                        chunk_model = chunk.get("model")
                        if isinstance(chunk_model, str) and chunk_model:
                            response_model = chunk_model

                        # Text content
                        text = msg_chunk.get("content", "")
                        if text:
                            yield TextDeltaEvent(text=text)

                        # Ollama delivers tool_calls in a single chunk (non-streaming)
                        for tc in msg_chunk.get("tool_calls", []):
                            fn = tc.get("function", {})
                            pending_tool_calls.append(
                                {
                                    "id": tc.get("id", f"call_{len(pending_tool_calls)}"),
                                    "name": fn.get("name", ""),
                                    "arguments": _normalize_tool_arguments(
                                        fn.get("arguments", {})
                                    ),
                                }
                            )

                        # Final chunk carries usage stats
                        if chunk.get("done"):
                            input_tokens = chunk.get("prompt_eval_count", 0)
                            output_tokens = chunk.get("eval_count", 0)
                            raw_done_reason = chunk.get("done_reason")
                            if isinstance(raw_done_reason, str) and raw_done_reason:
                                done_reason = raw_done_reason

                    # Emit tool events after streaming completes
                    for call in pending_tool_calls:
                        yield ToolUseStartEvent(tool_use_id=call["id"], tool_name=call["name"])
                        args_json = json.dumps(call["arguments"])
                        yield ToolUseDeltaEvent(tool_use_id=call["id"], json_fragment=args_json)
                        yield ToolUseEndEvent(
                            tool_use_id=call["id"],
                            tool_name=call["name"],
                            arguments=call["arguments"],
                        )

                    yield DoneEvent(
                        stop_reason="tool_use" if pending_tool_calls else done_reason,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        model=response_model,
                    )

        except httpx.TimeoutException as exc:
            yield ErrorEvent(message=f"Request timed out: {exc}", code="timeout")
        except httpx.RequestError as exc:
            yield ErrorEvent(message=f"Request error: {exc}", code="request_error")

    async def list_models(self) -> list[ModelInfo]:
        try:
            async with httpx.AsyncClient(
                timeout=5.0,
                trust_env=_trust_env(),
                proxy=self._proxy,
            ) as client:
                resp = await client.get(f"{self._base_url}/api/tags")
                resp.raise_for_status()
                data = resp.json()
                return [
                    ModelInfo(
                        provider=self.provider_name,
                        model_id=m["name"],
                        display_name=m.get("name", ""),
                        context_window=m.get("details", {}).get("context_length", 0),
                    )
                    for m in data.get("models", [])
                ]
        except Exception:
            return []
