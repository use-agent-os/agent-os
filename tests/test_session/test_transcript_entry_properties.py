from __future__ import annotations

from agentos.session.models import TranscriptEntry


def test_transcript_entry_role_properties() -> None:
    user_entry = TranscriptEntry(
        session_id="s1",
        session_key="k1",
        role="user",
        content="hello",
    )
    assert user_entry.is_user is True
    assert user_entry.is_assistant is False
    assert user_entry.has_tool_calls is False

    asst_entry = TranscriptEntry(
        session_id="s1",
        session_key="k1",
        role="assistant",
        content="hi",
    )
    assert asst_entry.is_user is False
    assert asst_entry.is_assistant is True
    assert asst_entry.has_tool_calls is False


def test_transcript_entry_has_tool_calls() -> None:
    entry_none = TranscriptEntry(
        session_id="s1",
        session_key="k1",
        role="assistant",
        tool_calls=None,
    )
    assert entry_none.has_tool_calls is False

    entry_empty = TranscriptEntry(
        session_id="s1",
        session_key="k1",
        role="assistant",
        tool_calls=[],
    )
    assert entry_empty.has_tool_calls is False

    entry_with_calls = TranscriptEntry(
        session_id="s1",
        session_key="k1",
        role="assistant",
        tool_calls=[{"id": "call_1", "name": "weather", "arguments": {}}],
    )
    assert entry_with_calls.has_tool_calls is True
