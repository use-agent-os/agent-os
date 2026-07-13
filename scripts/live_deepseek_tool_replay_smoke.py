#!/usr/bin/env python3
"""Live smoke DeepSeek thinking-mode tool replay requirements."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from agentos.provider.model_catalog import ModelCatalog
from agentos.provider.registry import get_provider_spec
from agentos.provider.selector import ProviderConfig, _build_provider
from agentos.provider.types import (
    ChatConfig,
    ContentBlockToolResult,
    ContentBlockToolUse,
    DoneEvent,
    ErrorEvent,
    Message,
    TextDeltaEvent,
    ToolDefinition,
    ToolInputSchema,
    ToolUseEndEvent,
    ToolUseStartEvent,
)


def _load_env_quietly(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _tool_def() -> ToolDefinition:
    return ToolDefinition(
        name="lookup_status",
        description="Return a deterministic status string for a service name.",
        input_schema=ToolInputSchema(
            type="object",
            properties={
                "service": {
                    "type": "string",
                    "description": "Service name to inspect.",
                }
            },
            required=["service"],
        ),
    )


async def _collect_call(
    provider: Any,
    messages: list[Message],
    *,
    tools: list[ToolDefinition],
    config: ChatConfig,
) -> dict[str, Any]:
    text_parts: list[str] = []
    tool_events: list[dict[str, Any]] = []
    done: DoneEvent | None = None
    error: str = ""
    start = time.perf_counter()
    async for event in provider.chat(messages, tools=tools, config=config):
        if isinstance(event, TextDeltaEvent):
            text_parts.append(event.text)
        elif isinstance(event, ToolUseStartEvent):
            tool_events.append(
                {
                    "event": "start",
                    "tool_use_id": event.tool_use_id,
                    "tool_name": event.tool_name,
                }
            )
        elif isinstance(event, ToolUseEndEvent):
            tool_events.append(
                {
                    "event": "end",
                    "tool_use_id": event.tool_use_id,
                    "tool_name": event.tool_name,
                    "arguments": event.arguments,
                }
            )
        elif isinstance(event, DoneEvent):
            done = event
        elif isinstance(event, ErrorEvent):
            error = event.message or event.code
            break
    latency_ms = int((time.perf_counter() - start) * 1000)
    return {
        "latency_ms": latency_ms,
        "text": "".join(text_parts),
        "tool_events": tool_events,
        "done": {
            "present": done is not None,
            "stop_reason": done.stop_reason if done else "",
            "input_tokens": done.input_tokens if done else 0,
            "output_tokens": done.output_tokens if done else 0,
            "reasoning_tokens": done.reasoning_tokens if done else 0,
            "reasoning_content_present": bool(done and done.reasoning_content),
            "reasoning_content_chars": len(done.reasoning_content or "") if done else 0,
            "model": done.model if done else "",
        },
        "_reasoning_content_raw": done.reasoning_content if done else None,
        "error": error,
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    _load_env_quietly()
    spec = get_provider_spec("deepseek")
    api_key = os.environ.get(spec.env_key, "").strip()
    model = args.model or os.environ.get("DEEPSEEK_MODEL", "").strip() or "deepseek-v4-pro"
    base_url = (
        args.base_url
        or os.environ.get("DEEPSEEK_BASE_URL", "").strip()
        or spec.default_base_url
    )
    marker = f"DEEPSEEK_TOOL_REPLAY_{int(time.time() * 1000)}"
    payload: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "provider": "deepseek",
        "model": model,
        "base_url": base_url,
        "env_key": spec.env_key,
        "key_present": bool(api_key),
        "marker": marker,
    }
    if not api_key:
        payload["ok"] = False
        payload["error"] = f"{spec.env_key} is empty"
    else:
        provider = _build_provider(
            ProviderConfig(provider="deepseek", model=model, api_key=api_key, base_url=base_url)
        )
        caps = ModelCatalog().get_capabilities(model, provider_name="deepseek", base_url=base_url)
        config = ChatConfig(
            max_tokens=args.max_tokens,
            temperature=None,
            thinking=True,
            thinking_budget_tokens=4096,
            timeout=60.0,
            model_capabilities=caps,
        )
        tools = [_tool_def()]
        first_messages = [
            Message(
                role="user",
                content=(
                    "You must call lookup_status exactly once before answering. "
                    "Use service='payments'. Do not provide a final answer yet."
                ),
            )
        ]
        first = await _collect_call(provider, first_messages, tools=tools, config=config)
        tool_end = next(
            (event for event in first["tool_events"] if event.get("event") == "end"),
            None,
        )
        second: dict[str, Any] | None = None
        replay_messages_without_secret: list[dict[str, Any]] = []
        if tool_end and first["done"]["reasoning_content_present"]:
            reasoning_placeholder = "<reasoning_content omitted; present in request object>"
            assistant_msg = Message(
                role="assistant",
                content=[
                    ContentBlockToolUse(
                        id=tool_end["tool_use_id"],
                        name=tool_end["tool_name"],
                        input=tool_end.get("arguments") or {},
                    )
                ],
                # The actual field is passed to the provider; the artifact only
                # records its presence/length, not the hidden reasoning text.
                reasoning_content=first.get("_reasoning_content_raw") or "",
            )
            tool_result_msg = Message(
                role="user",
                content=[
                    ContentBlockToolResult(
                        tool_use_id=tool_end["tool_use_id"],
                        content="payments status: healthy; queue depth: 0",
                    )
                ],
            )
            final_user = Message(
                role="user",
                content=f"Now answer exactly with {marker}.",
            )
            second_messages = [assistant_msg, tool_result_msg, final_user]
            replay_messages_without_secret = [
                {
                    "role": "assistant",
                    "tool_use_id": tool_end["tool_use_id"],
                    "tool_name": tool_end["tool_name"],
                    "reasoning_content": reasoning_placeholder,
                },
                {
                    "role": "tool/user",
                    "tool_use_id": tool_end["tool_use_id"],
                    "content": "payments status: healthy; queue depth: 0",
                },
                {"role": "user", "content": f"Now answer exactly with {marker}."},
            ]
            second = await _collect_call(provider, second_messages, tools=tools, config=config)
        first.pop("_reasoning_content_raw", None)
        if second:
            second.pop("_reasoning_content_raw", None)
        payload.update(
            {
                "ok": bool(
                    tool_end
                    and first["done"]["reasoning_content_present"]
                    and second
                    and not second.get("error")
                    and marker in str(second.get("text") or "")
                ),
                "first_call": first,
                "second_call": second,
                "replay_messages_without_secret": replay_messages_without_secret,
                "chat_config_without_secret": config.model_dump(mode="json"),
                "failure_reason": None,
            }
        )
        if not payload["ok"]:
            if not tool_end:
                payload["failure_reason"] = "first_call_did_not_emit_tool_call"
            elif not first["done"]["reasoning_content_present"]:
                payload["failure_reason"] = "first_call_missing_reasoning_content"
            elif not second:
                payload["failure_reason"] = "second_call_not_run"
            elif second.get("error"):
                payload["failure_reason"] = "second_call_error"
            else:
                payload["failure_reason"] = "second_call_marker_missing"
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
