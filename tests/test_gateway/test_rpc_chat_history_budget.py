"""``chat.history`` is byte-bounded, resumable from a cursor and display-ready.

One 10 MB tool result used to blow the frame, a reconnect refetched a full
page, and each client stripped protocol markers its own way.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agentos.gateway import rpc_chat
from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.gateway.rpc_chat import _handle_chat_entry, _handle_chat_history
from agentos.session.models import TranscriptEntry

KEY = "agent:main:webchat:budget"


class _Manager:
    def __init__(self, entries: list[TranscriptEntry]) -> None:
        self.entries = entries

    async def get_canonical_transcript(self, session_key: str) -> list[TranscriptEntry]:
        return self.entries

    async def get_transcript(self, session_key: str) -> list[TranscriptEntry]:
        return self.entries

    async def get_summaries(self, session_key: str) -> list[Any]:
        return []


def _entry(idx: int, content: str = "", role: str = "user", **fields: Any) -> TranscriptEntry:
    return TranscriptEntry(
        id=idx,
        session_id="s",
        session_key=KEY,
        role=role,
        content=content or f"message {idx}",
        created_at=idx,
        message_id=f"msg-{idx}",
        **fields,
    )


def _ctx(entries: list[TranscriptEntry]) -> RpcContext:
    return RpcContext(conn_id="test", session_manager=_Manager(entries))


def _size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())


async def test_an_oversized_message_becomes_a_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rpc_chat, "CHAT_HISTORY_MESSAGE_BUDGET_BYTES", 4_000)
    huge = _entry(
        2,
        "x" * 50_000,
        role="assistant",
        tool_calls=[
            {"type": "tool_use", "tool_use_id": "t1", "name": "bash", "input": "y" * 9_000},
            {"type": "tool_result", "tool_use_id": "t1", "content": "z" * 9_000},
        ],
    )
    result = await _handle_chat_history({"sessionKey": KEY}, _ctx([_entry(1), huge, _entry(3)]))

    messages = result["messages"]
    assert [m["message_id"] for m in messages] == ["msg-1", "msg-2", "msg-3"]
    placeholder = messages[1]
    assert placeholder["placeholder"] is True
    assert placeholder["role"] == "assistant"
    assert placeholder["entry_id"] == 2
    assert placeholder["byte_size"] > 50_000
    # Tool identity survives, the bodies do not.
    assert placeholder["tool_calls"] == [
        {"type": "tool_use", "name": "bash", "tool_use_id": "t1"},
        {"type": "tool_result", "tool_use_id": "t1"},
    ]
    assert len(placeholder["text"]) < 4_000
    assert _size(placeholder) < 4_000
    assert messages[0].get("placeholder") is None


async def test_the_response_budget_keeps_the_newest_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rpc_chat, "CHAT_HISTORY_RESPONSE_BUDGET_BYTES", 3_000)
    entries = [_entry(i, "w" * 900) for i in range(1, 11)]

    result = await _handle_chat_history({"sessionKey": KEY, "limit": 10}, _ctx(entries))

    ids = [m["message_id"] for m in result["messages"]]
    assert ids and ids[-1] == "msg-10"
    assert len(ids) < 10
    assert sum(_size(m) for m in result["messages"]) <= 3_000
    # The rows left out are one ``before`` page away.
    assert result["has_more"] is True
    assert result["oldest_cursor"] == f"{ids[0][4:]}|{ids[0][4:]}"


async def test_the_real_budgets_bound_a_ten_megabyte_tool_result() -> None:
    big = _entry(2, "r" * (10 * 1024 * 1024), role="tool")
    result = await _handle_chat_history({"sessionKey": KEY}, _ctx([_entry(1), big]))

    assert _size(result) < rpc_chat.CHAT_HISTORY_RESPONSE_BUDGET_BYTES
    assert result["messages"][1]["placeholder"] is True


async def test_chat_entry_returns_the_full_row() -> None:
    huge = _entry(2, "x" * 300_000, role="assistant")
    result = await _handle_chat_entry({"sessionKey": KEY, "entryId": 2}, _ctx([_entry(1), huge]))

    assert result["message"]["text"] == "x" * 300_000
    assert result["byte_size"] > 300_000
    with pytest.raises(KeyError):
        await _handle_chat_entry({"sessionKey": KEY, "entryId": 99}, _ctx([huge]))


async def test_chat_entry_is_not_offered_to_channels() -> None:
    from agentos.gateway.access import ConnectionSurface
    from agentos.gateway.auth import AccessContext

    ctx = RpcContext(
        conn_id="c",
        access=AccessContext(
            surface=ConnectionSurface.CHANNEL, admitted=True, credential_verified=True
        ),
        session_manager=_Manager([_entry(1)]),
    )
    res = await get_dispatcher().dispatch("1", "chat.entry", {"sessionKey": KEY, "entryId": 1}, ctx)
    assert res.ok is False


# ── delta cursor ─────────────────────────────────────────────────────────


async def test_delta_cursor_returns_only_the_rows_after_it() -> None:
    entries = [_entry(i) for i in range(1, 6)]

    result = await _handle_chat_history({"sessionKey": KEY, "delta_cursor": "3|3"}, _ctx(entries))

    assert result["kind"] == "delta"
    assert [m["message_id"] for m in result["messages"]] == ["msg-4", "msg-5"]
    assert result["newest_cursor"] == "5|5"
    assert result["has_more"] is False


async def test_delta_cursor_at_the_tail_is_an_empty_delta() -> None:
    entries = [_entry(i) for i in range(1, 4)]

    result = await _handle_chat_history({"sessionKey": KEY, "deltaCursor": "3|3"}, _ctx(entries))

    assert result["kind"] == "delta"
    assert result["messages"] == []
    assert result["newest_cursor"] == "3|3"


async def test_delta_is_capped_and_resumable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rpc_chat, "CHAT_HISTORY_DELTA_MAX_ROWS", 2)
    entries = [_entry(i) for i in range(1, 7)]

    first = await _handle_chat_history({"sessionKey": KEY, "delta_cursor": "1|1"}, _ctx(entries))
    second = await _handle_chat_history(
        {"sessionKey": KEY, "delta_cursor": first["newest_cursor"]}, _ctx(entries)
    )

    assert [m["message_id"] for m in first["messages"]] == ["msg-2", "msg-3"]
    assert first["has_more"] is True
    assert [m["message_id"] for m in second["messages"]] == ["msg-4", "msg-5"]


async def test_delta_byte_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rpc_chat, "CHAT_HISTORY_DELTA_MAX_BYTES", 2_500)
    entries = [_entry(1)] + [_entry(i, "d" * 1_000) for i in range(2, 8)]

    result = await _handle_chat_history({"sessionKey": KEY, "delta_cursor": "1|1"}, _ctx(entries))

    assert [m["message_id"] for m in result["messages"]] == ["msg-2", "msg-3"]
    assert result["has_more"] is True


async def test_an_unusable_cursor_resets_to_the_latest_page() -> None:
    entries = [_entry(i) for i in range(1, 4)]

    result = await _handle_chat_history(
        {"sessionKey": KEY, "delta_cursor": "999|gone"}, _ctx(entries)
    )

    assert result["kind"] == "reset"
    assert [m["message_id"] for m in result["messages"]] == ["msg-1", "msg-2", "msg-3"]
    assert result["oldest_cursor"] == "1|1"


# ── display projection ───────────────────────────────────────────────────


async def test_assistant_rows_arrive_display_clean() -> None:
    leaked = (
        "[[reply_to_current]] Useful answer [generated artifact omitted: chart.png (image/png)]\n"
        "<angle brackets> leaked"
    )
    entries = [
        _entry(1, "[[reply_to_current]] keep my words", role="user"),
        _entry(
            2,
            leaked,
            role="assistant",
            tool_calls=[{"type": "text", "text": "[[reply_to: 7]]Step one"}],
        ),
    ]

    result = await _handle_chat_history({"sessionKey": KEY}, _ctx(entries))

    user, assistant = result["messages"]
    # Only assistant text is a display concern; what the user typed is theirs.
    assert user["text"] == "[[reply_to_current]] keep my words"
    assert assistant["text"] == "Useful answer"
    assert assistant["tool_calls"] == [{"type": "text", "text": "Step one"}]
