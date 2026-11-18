"""The no-model compaction prechecks agree with what compaction then does.

The pre-compaction memory review is skipped when the compaction it precedes
would summarise nothing; that decision is made up front by
``CompactionService.skip_reason`` (in-turn and proactive compaction) and
``session.compaction.compaction_skip_reason`` (manual compaction). Each must
answer what the real compaction decides.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from agentos.engine.compaction_service import CompactionService
from agentos.provider import Message
from agentos.session.compaction import (
    CompactionConfig,
    CompactionRequest,
    compact_context_new,
    compaction_skip_reason,
)


def _tokens(messages: Sequence[Message]) -> int:
    return sum(len(message.model_dump_json()) for message in messages) // 4


def _exchange(question: str, answer: str) -> list[Message]:
    return [Message(role="user", content=question), Message(role="assistant", content=answer)]


CASES = [
    pytest.param([Message(role="user", content="q")], 0, False, "under_threshold", id="under"),
    pytest.param(
        [Message(role="user", content="q" * 400)], 0, True, "nothing_to_compact", id="none"
    ),
    pytest.param(
        [*_exchange("a", "b"), Message(role="user", content="x" * 8000)],
        2,
        True,
        "not_worth_compacting",
        id="not-worth",
    ),
    pytest.param(
        [*_exchange("x" * 8000, "y" * 8000), Message(role="user", content="next?")],
        2,
        True,
        None,
        id="compacts",
    ),
]


@pytest.mark.parametrize(("messages", "turn_input_index", "proactive", "expected"), CASES)
async def test_skip_reason_is_what_compact_decides(
    messages: list[Message], turn_input_index: int, proactive: bool, expected: str | None
) -> None:
    calls: list[list[Message]] = []

    async def _summarizer(folded: list[Message], previous: str | None) -> tuple[str, str]:
        calls.append(folded)
        return "short summary", "llm"

    service = CompactionService(provider=None, summarizer=_summarizer)
    kwargs = {
        "turn_input_index": turn_input_index,
        "estimated_tokens": _tokens(messages),
        "window_tokens": 100_000 if proactive else 1_000_000,
        "message_tokens": _tokens,
        "proactive": proactive,
    }

    reason = service.skip_reason(messages, **kwargs)  # type: ignore[arg-type]
    result = await service.compact(messages, **kwargs)  # type: ignore[arg-type]

    assert reason == expected
    if expected is None:
        assert result.compacted and calls
    else:
        assert (result.compacted, result.reason, calls) == (False, expected, [])


@pytest.mark.parametrize(
    ("entries", "window", "expected"),
    [
        pytest.param([], 8000, "no_entries", id="empty"),
        pytest.param(
            [{"role": "user", "content": "short"}, {"role": "assistant", "content": "reply"}],
            8000,
            "within_compaction_budget",
            id="within-budget",
        ),
    ],
)
async def test_the_manual_precheck_is_what_session_compaction_decides(
    entries: list[dict], window: int, expected: str
) -> None:
    config = CompactionConfig()

    result = await compact_context_new(
        CompactionRequest(
            session_id="s", entries=entries, context_window_tokens=window, config=config
        )
    )

    assert compaction_skip_reason(entries, window, config) == expected == result.skip_reason


def test_the_manual_precheck_lets_a_long_session_through() -> None:
    entries = [
        {"role": role, "content": " ".join(f"{role}{i}word{n}" for n in range(600))}
        for i in range(6)
        for role in ("user", "assistant")
    ]

    assert compaction_skip_reason(entries, 8000, CompactionConfig()) is None
