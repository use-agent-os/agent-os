"""chat.thinking RPC + has_thinking history flag + control_ui.show_thinking gate."""

from types import SimpleNamespace

import pytest

from agentos.gateway.rpc import RpcContext
from agentos.gateway.rpc_chat import _handle_chat_history, _handle_chat_thinking
from agentos.gateway.session_streams import SessionStreamRegistry
from agentos.session.models import TranscriptEntry

_SESSION_KEY = "agent:main:webchat:test"


class _FakeSessionManager:
    def __init__(self, entries):
        self._entries = entries

    async def get_transcript(self, session_key):
        return self._entries

    async def get_canonical_transcript(self, session_key):
        return self._entries

    async def get_summaries(self, session_key):
        return []


def _entry(idx: int, role: str = "assistant", reasoning: str | None = None) -> TranscriptEntry:
    return TranscriptEntry(
        id=idx,
        session_id="parent",
        session_key=_SESSION_KEY,
        role=role,
        content=f"message {idx}",
        created_at=idx,
        message_id=f"msg-{idx}",
        reasoning_content=reasoning,
    )


def _ctx(entries, *, show_thinking: bool | None = None) -> RpcContext:
    config = None
    if show_thinking is not None:
        config = SimpleNamespace(control_ui=SimpleNamespace(show_thinking=show_thinking))
    return RpcContext(
        conn_id="test",
        session_manager=_FakeSessionManager(entries),
        config=config,
    )


@pytest.mark.asyncio
async def test_chat_history_flags_messages_with_reasoning() -> None:
    entries = [
        _entry(1, role="user"),
        _entry(2, reasoning="chain of thought"),
        _entry(3),
    ]

    result = await _handle_chat_history({"sessionKey": _SESSION_KEY}, _ctx(entries))

    flags = [msg.get("has_thinking") for msg in result["messages"]]
    assert flags == [None, True, None]
    # The reasoning body itself must NOT ride along with history pages.
    assert all("reasoning" not in msg for msg in result["messages"])


@pytest.mark.asyncio
async def test_chat_history_strips_flag_when_show_thinking_disabled() -> None:
    entries = [_entry(1, reasoning="chain of thought")]

    result = await _handle_chat_history(
        {"sessionKey": _SESSION_KEY},
        _ctx(entries, show_thinking=False),
    )

    assert all("has_thinking" not in msg for msg in result["messages"])


@pytest.mark.asyncio
async def test_chat_thinking_returns_reasoning_for_message() -> None:
    entries = [_entry(1), _entry(2, reasoning="the hidden reasoning")]

    result = await _handle_chat_thinking(
        {"sessionKey": _SESSION_KEY, "messageId": "msg-2"},
        _ctx(entries),
    )

    assert result["reasoning"] == "the hidden reasoning"
    assert result["messageId"] == "msg-2"


@pytest.mark.asyncio
async def test_chat_thinking_returns_none_for_message_without_reasoning() -> None:
    entries = [_entry(1)]

    result = await _handle_chat_thinking(
        {"sessionKey": _SESSION_KEY, "messageId": "msg-1"},
        _ctx(entries),
    )

    assert result["reasoning"] is None


@pytest.mark.asyncio
async def test_chat_thinking_disabled_by_config() -> None:
    entries = [_entry(1, reasoning="should stay hidden")]

    result = await _handle_chat_thinking(
        {"sessionKey": _SESSION_KEY, "messageId": "msg-1"},
        _ctx(entries, show_thinking=False),
    )

    assert result["reasoning"] is None


@pytest.mark.asyncio
async def test_chat_thinking_requires_message_id() -> None:
    with pytest.raises(ValueError):
        await _handle_chat_thinking({"sessionKey": _SESSION_KEY}, _ctx([]))


def test_session_stream_thinking_events_are_replay_lossy() -> None:
    registry = SessionStreamRegistry(max_events_per_session=2)
    registry.record("s", "session.event.thinking", {"text": "a"})
    registry.record("s", "session.event.tool_use_start", {"tool_name": "t"})
    registry.record("s", "session.event.done", {})

    events = registry.replay("s", 0).events
    names = [event.event_name for event in events]
    # The thinking delta is trimmed first; durable events survive.
    assert "session.event.thinking" not in names
    assert "session.event.done" in names
