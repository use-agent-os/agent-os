"""Gateway-backed implementation for the inbound AgentOS MCP bridge."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from typing import Any, Protocol

from agentos.gateway_client import normalize_gateway_url


class GatewayClientLike(Protocol):
    async def connect(self, url: str) -> None: ...

    async def close(self) -> None: ...

    async def list_sessions(self, limit: int = 50) -> dict[str, Any]: ...

    async def resolve_session(self, key: str) -> dict[str, Any]: ...

    async def session_history(self, session_key: str, limit: int = 1000) -> dict[str, Any]: ...

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any: ...

    async def recv_event(self, timeout: float | None = None) -> dict[str, Any]: ...


# Upper bounds for the bridge arguments an MCP client supplies. The client is the
# operator's own MCP host rather than a remote attacker, but every one of these
# arguments is chosen by a model on each call, so a bad pick has to degrade
# rather than hang the tool call: a `timeout_ms=3_600_000` holds the call open
# for an hour, which the user cannot tell apart from a stuck gateway.
#
# The ceilings sit above this bridge's own defaults (50 sessions, 1000 messages)
# so ordinary calls are untouched. That is why they are not the 200/100 used by
# the `sessions_list` / `sessions_history` builtins, whose defaults are 50 and
# 20. The gateway applies its own, tighter cap to `chat.history`, so the history
# ceiling here is defence in depth rather than the only guard.
_MAX_EVENTS_WAIT_TIMEOUT_MS = 300_000  # 5 minutes
_MAX_EVENTS_WAIT_EVENTS = 10_000
_MAX_HISTORY_LIMIT = 5_000
_MAX_SESSION_LIST_LIMIT = 5_000


def _clamp_limit(limit: int, upper: int) -> int:
    return min(max(1, limit), upper)


def _default_gateway_client() -> GatewayClientLike:
    from agentos.gateway_client import GatewayRPCClient

    return GatewayRPCClient()


class AgentOSMCPBridge:
    """Small product bridge from MCP tools/resources to existing gateway RPCs."""

    def __init__(
        self,
        *,
        gateway_url: str | None = None,
        gateway_client_factory: Callable[[], GatewayClientLike] = _default_gateway_client,
    ) -> None:
        raw_url = (
            gateway_url
            or os.environ.get("AGENTOS_GATEWAY_URL")
            or "ws://localhost:18791/ws"
        )
        self.gateway_url = normalize_gateway_url(raw_url)
        self._gateway_client_factory = gateway_client_factory
        self._client: GatewayClientLike | None = None

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def _ensure_client(self) -> GatewayClientLike:
        if self._client is None:
            client = self._gateway_client_factory()
            await client.connect(self.gateway_url)
            self._client = client
        return self._client

    async def conversations_list(self, limit: int = 50) -> dict[str, Any]:
        client = await self._ensure_client()
        # The lower bound matters as much as the ceiling here: `sessions.list`
        # passes the limit into a SQL `LIMIT ?`, and SQLite reads a negative
        # limit as no limit at all, so a negative argument loaded every row.
        return await client.list_sessions(limit=_clamp_limit(limit, _MAX_SESSION_LIST_LIMIT))

    async def session_resolve(self, key: str) -> dict[str, Any]:
        client = await self._ensure_client()
        return await client.resolve_session(key)

    async def messages_read(self, key: str, limit: int = 1000) -> dict[str, Any]:
        client = await self._ensure_client()
        return await client.session_history(key, limit=_clamp_limit(limit, _MAX_HISTORY_LIMIT))

    async def messages_send(
        self,
        key: str,
        message: str,
        *,
        intent: str = "continue",
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        client = self._gateway_client_factory()
        await client.connect(self.gateway_url)
        try:
            subscription = await self._subscribe_messages(client, key, since_stream_seq=None)
            result = await client.call(
                "sessions.send",
                {
                    "key": key,
                    "message": message,
                    "attachments": attachments or [],
                    "intent": intent,
                    "_source": {
                        "caller_kind": "cli",
                        "channel_kind": "cli",
                        "channel_id": "mcp:bridge",
                        "source_kind": "mcp",
                        "source_name": "mcp_server",
                    },
                },
            )
            response = result if isinstance(result, dict) else {"result": result}
            response["current_stream_seq"] = subscription.get("current_stream_seq")
            response["replay_complete"] = subscription.get("replay_complete")
            response["replay_gap_reason"] = subscription.get("replay_gap_reason")
            return response
        finally:
            await client.close()

    async def events_wait(
        self,
        key: str,
        *,
        since_stream_seq: int | None = None,
        timeout_ms: int = 30_000,
        max_events: int = 100,
        terminal_only: bool = False,
    ) -> dict[str, Any]:
        client = self._gateway_client_factory()
        await client.connect(self.gateway_url)
        try:
            subscription = await self._subscribe_messages(
                client, key, since_stream_seq=since_stream_seq
            )

            events: list[dict[str, Any]] = []
            current_stream_seq = int(
                subscription.get("current_stream_seq") or since_stream_seq or 0
            )
            max_events = _clamp_limit(max_events, _MAX_EVENTS_WAIT_EVENTS)
            timeout_ms = min(max(0, timeout_ms), _MAX_EVENTS_WAIT_TIMEOUT_MS)
            timeout_s = timeout_ms / 1000
            deadline = time.monotonic() + timeout_s

            timed_out = False
            while len(events) < max_events:
                # Re-clamp: on coarse clocks ``deadline - now`` can round a hair
                # above ``timeout_s``, handing ``recv_event`` more than the cap.
                remaining = min(deadline - time.monotonic(), timeout_s)
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    frame = await client.recv_event(timeout=remaining)
                except TimeoutError:
                    timed_out = True
                    break
                normalized = _normalize_event_frame(frame)
                payload = normalized.get("payload")
                if not isinstance(payload, dict) or payload.get("session_key") != key:
                    continue
                event_name = str(normalized.get("event") or "")
                stream_seq = payload.get("stream_seq")
                if isinstance(stream_seq, int):
                    current_stream_seq = max(current_stream_seq, stream_seq)
                is_terminal = event_name in _TERMINAL_EVENTS
                if not terminal_only or is_terminal:
                    events.append({"event": event_name, "payload": payload})
                if is_terminal:
                    break

            return {
                "key": key,
                "events": events,
                "current_stream_seq": current_stream_seq,
                "replay_complete": subscription.get("replay_complete"),
                "replay_gap_reason": subscription.get("replay_gap_reason"),
                # Deadline expiry (or a hard recv_event TimeoutError) is the only
                # thing this reports -- not "no events collected" and not "the
                # max_events cap was reached", both of which are simply the
                # loop's other, non-terminal exit path and say nothing about
                # whether more events were still coming.
                "timed_out": timed_out,
            }
        finally:
            await client.close()

    async def transcript_jsonl(self, key: str, limit: int = 1000) -> str:
        history = await self.messages_read(key, limit=limit)
        messages = history.get("messages", []) if isinstance(history, dict) else []
        rows = [_message_to_event(row) for row in messages if isinstance(row, dict)]
        flattened: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, list):
                flattened.extend(row)
            else:
                flattened.append(row)
        return "\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in flattened)

    async def _subscribe_messages(
        self,
        client: GatewayClientLike,
        key: str,
        *,
        since_stream_seq: int | None,
    ) -> dict[str, Any]:
        result = await client.call(
            "sessions.messages.subscribe",
            {"key": key, "since_stream_seq": since_stream_seq},
        )
        return result if isinstance(result, dict) else {}


_TERMINAL_EVENTS = {
    "session.event.done",
    "session.event.error",
    "task.cancelled",
    "task.failed",
    "task.timeout",
    "task.abandoned",
}


def _normalize_event_frame(frame: dict[str, Any]) -> dict[str, Any]:
    if "event" in frame and "payload" in frame:
        return frame
    return {"event": frame.get("event"), "payload": frame.get("payload") or frame}


def _message_to_event(message: dict[str, Any]) -> dict[str, Any] | list[dict[str, Any]]:
    role = str(message.get("role") or "unknown")
    timestamp = message.get("timestamp")
    tool_calls = message.get("tool_calls") or []
    if role == "assistant" and isinstance(tool_calls, list) and tool_calls:
        return _assistant_tool_events(tool_calls, timestamp=timestamp)
    return _message_event(
        role,
        [{"type": "text", "text": str(message.get("text") or "")}] if message.get("text") else [],
        timestamp=timestamp,
    )


def _assistant_tool_events(
    tool_calls: list[Any],
    *,
    timestamp: Any,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    assistant_blocks: list[dict[str, Any]] = []
    for segment in tool_calls:
        if not isinstance(segment, dict):
            continue
        segment_type = segment.get("type")
        if segment_type == "text":
            text = segment.get("text")
            if text:
                assistant_blocks.append({"type": "text", "text": str(text)})
        elif segment_type == "tool_use":
            assistant_blocks.append(
                {
                    "type": "toolCall",
                    "name": str(segment.get("name") or ""),
                    "id": str(segment.get("tool_use_id") or ""),
                    "arguments": segment.get("input") or {},
                }
            )
        elif segment_type == "tool_result":
            if assistant_blocks:
                output.append(_message_event("assistant", assistant_blocks, timestamp=timestamp))
                assistant_blocks = []
            output.append(
                _message_event(
                    "toolResult",
                    [{"type": "text", "text": str(segment.get("result") or "")}],
                    timestamp=timestamp,
                    tool_call_id=str(segment.get("tool_use_id") or ""),
                    tool_name=str(segment.get("name") or ""),
                    is_error=bool(segment.get("is_error", False)),
                    execution_status=(
                        segment.get("execution_status")
                        if isinstance(segment.get("execution_status"), dict)
                        else None
                    ),
                )
            )
    if assistant_blocks:
        output.append(_message_event("assistant", assistant_blocks, timestamp=timestamp))
    return output


def _message_event(
    role: str,
    content: list[dict[str, Any]],
    *,
    timestamp: Any = None,
    tool_call_id: str | None = None,
    tool_name: str | None = None,
    is_error: bool | None = None,
    execution_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {"type": "message", "message": {"role": role, "content": content}}
    if timestamp is not None:
        event["timestamp"] = timestamp
    if tool_call_id is not None:
        event["message"]["toolCallId"] = tool_call_id
    if tool_name is not None:
        event["message"]["toolName"] = tool_name
    if is_error is not None:
        event["message"]["isError"] = is_error
    if execution_status is not None:
        event["message"]["executionStatus"] = execution_status
    return event
