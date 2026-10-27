"""The one CompactionService: trigger, shape, summarizer, safety rules.

Coverage moved here from the deleted before-turn paths (T3-upgrade,
DB preflight, emergency request-scoped trim): the threshold rules, the
cron/subagent session handling and the summarizer now live in one service
the agent calls before every provider request.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest

from agentos.engine.compaction_service import (
    CONTEXT_SUMMARY_HEADER,
    SUMMARY_ACK,
    CompactionService,
    ends_mid_tool_round,
    fallback_max_chars,
    fallback_summary,
    is_ephemeral_session,
    should_compact,
    summarize_messages,
)
from agentos.provider import (
    ChatConfig,
    ContentBlockRedactedThinking,
    ContentBlockText,
    ContentBlockThinking,
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
)
from agentos.provider import DoneEvent as ProviderDone
from agentos.provider import ErrorEvent as ProviderError
from agentos.provider import TextDeltaEvent as ProviderText
from agentos.provider.types import ContentBlockImage


def _tokens(messages: Sequence[Message]) -> int:
    return sum(len(message.model_dump_json()) for message in messages) // 4


def _round(tool_id: str, result: str) -> list[Message]:
    return [
        Message(
            role="assistant",
            content=[
                ContentBlockThinking(thinking=f"think {tool_id}", signature=f"sig-{tool_id}"),
                ContentBlockToolUse(id=tool_id, name="read_file", input={"path": tool_id}),
            ],
        ),
        Message(
            role="user",
            content=[ContentBlockToolResult(tool_use_id=tool_id, content=result)],
        ),
    ]


class _SummarizingProvider:
    """A minimal LLMProvider: the summarizer must call it, never raw HTTP."""

    provider_name = "fake"

    def __init__(self, *, text: str = "provider summary", error: bool = False) -> None:
        self.text = text
        self.error = error
        self.calls: list[tuple[list[Message], ChatConfig | None]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append((messages, config))
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        if self.error:
            yield ProviderError(message="upstream down", code="503")
            return
        yield ProviderText(text=self.text)
        yield ProviderDone(stop_reason="end_turn")


# ---------------------------------------------------------------------------
# Trigger
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("estimated", "window", "ratio", "expected"),
    [
        (849, 1000, 0.85, False),
        (850, 1000, 0.85, False),  # exactly at the threshold does not compact
        (851, 1000, 0.85, True),
        (601, 1000, 0.6, True),  # configured ratio is honoured
        (10_000, 0, 0.85, False),  # unknown window never triggers
    ],
)
def test_single_trigger_is_the_live_estimate_against_window_times_ratio(
    estimated: int, window: int, ratio: float, expected: bool
) -> None:
    assert should_compact(estimated, window, ratio) is expected
    assert CompactionService(provider=None, ratio=ratio).should_compact(estimated, window) is (
        expected
    )


@pytest.mark.asyncio
async def test_under_threshold_is_a_noop_without_summarizing() -> None:
    calls: list[Any] = []

    async def _summarizer(messages: list[Message], previous: str | None) -> tuple[str, str]:
        calls.append(messages)
        return "s", "llm"

    messages = [Message(role="user", content="q"), Message(role="assistant", content="a")]
    result = await CompactionService(provider=None, summarizer=_summarizer).compact(
        [*messages, Message(role="user", content="now")],
        turn_input_index=2,
        estimated_tokens=100,
        window_tokens=1000,
        message_tokens=_tokens,
    )

    assert result.status == "skipped"
    assert result.reason == "under_threshold"
    assert calls == []


@pytest.mark.parametrize(
    ("key", "ephemeral"),
    [
        ("subagent:agent:main:main", True),
        ("agent:main:subagent:run-7", True),  # canonical key a prefix test missed
        ("AGENT:Main:SUBAGENT:Run-7", True),
        ("cron:daily-digest", True),
        ("agent:main:main", False),
        ("agent:main:webchat:abc", False),
        ("", False),
        (None, False),
    ],
)
def test_ephemeral_sessions_use_is_subagent_key(key: str | None, ephemeral: bool) -> None:
    assert is_ephemeral_session(key) is ephemeral


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shape_is_one_summary_exchange_plus_the_current_turn_verbatim() -> None:
    seen: list[list[Message]] = []

    async def _summarizer(messages: list[Message], previous: str | None) -> tuple[str, str]:
        seen.append(messages)
        return "condensed", "llm"

    image = ContentBlockImage(media_type="image/png", data="aGVsbG8=")
    history = [
        Message(role="user", content="old question " + "h" * 4000),
        Message(role="assistant", content="old answer " + "a" * 4000),
    ]
    current = [
        Message(role="user", content=[ContentBlockText(text="look at this"), image]),
        *_round("t1", "result one"),
    ]
    messages = [*history, *current]

    result = await CompactionService(provider=None, summarizer=_summarizer).compact(
        messages,
        turn_input_index=2,
        estimated_tokens=_tokens(messages),
        window_tokens=1_000,
        message_tokens=_tokens,
        input_entry={"role": "user", "content": "look at this"},
    )

    assert result.compacted
    assert result.messages[0] == Message(
        role="user", content=f"{CONTEXT_SUMMARY_HEADER}\ncondensed"
    )
    assert result.messages[1] == Message(role="assistant", content=SUMMARY_ACK)
    # The current turn is kept object-for-object: image, thinking signature,
    # tool pair -- nothing flattened or re-projected.
    assert result.messages[2:] == current
    assert all(a is b for a, b in zip(result.messages[2:], current, strict=True))
    assert result.turn_input_index == 2
    assert result.removed_count == 2
    assert seen == [history]
    assert result.kept_entries == [{"role": "user", "content": "look at this"}]


@pytest.mark.asyncio
async def test_previous_summary_is_folded_into_the_next_one() -> None:
    previous: list[str | None] = []

    async def _summarizer(messages: list[Message], prev: str | None) -> tuple[str, str]:
        previous.append(prev)
        return "merged summary", "llm"

    messages = [
        Message(role="user", content=f"{CONTEXT_SUMMARY_HEADER}\nfirst summary"),
        Message(role="assistant", content=SUMMARY_ACK),
        Message(role="user", content="q " + "x" * 4000),
        Message(role="assistant", content="a " + "y" * 4000),
        Message(role="user", content="now"),
    ]

    result = await CompactionService(provider=None, summarizer=_summarizer).compact(
        messages,
        turn_input_index=4,
        estimated_tokens=_tokens(messages),
        window_tokens=1_000,
        message_tokens=_tokens,
    )

    assert previous == ["first summary"]
    assert result.messages[0].content == f"{CONTEXT_SUMMARY_HEADER}\nmerged summary"
    assert [m.content for m in result.messages[2:]] == ["now"]


@pytest.mark.asyncio
async def test_oversized_current_turn_folds_oldest_complete_rounds_only() -> None:
    folded: list[list[Message]] = []

    async def _summarizer(messages: list[Message], previous: str | None) -> tuple[str, str]:
        folded.append(messages)
        return "s", "llm"

    current = [
        Message(role="user", content="do the big task"),
        *_round("r1", "a" * 20_000),
        *_round("r2", "b" * 20_000),
        *_round("r3", "c" * 2_000),
    ]
    messages = [Message(role="user", content="old"), Message(role="assistant", content="old a")]

    result = await CompactionService(provider=None, summarizer=_summarizer).compact(
        [*messages, *current],
        turn_input_index=2,
        estimated_tokens=_tokens([*messages, *current]),
        window_tokens=10_000,
        message_tokens=_tokens,
    )

    assert result.compacted
    kept = result.messages[2:]
    # Input message and the latest round survive; r1/r2 were summarized.
    assert kept[0] == current[0]
    assert kept[1:] == current[-2:]
    assert result.folded_rounds == 2
    assert folded[0][2:] == current[1:5]
    # Never a tool_result without its tool_use.
    kept_use_ids = {
        block.id
        for message in kept
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockToolUse)
    }
    kept_result_ids = {
        block.tool_use_id
        for message in kept
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockToolResult)
    }
    assert kept_result_ids == kept_use_ids == {"r3"}


@pytest.mark.asyncio
async def test_never_compacts_mid_tool_round() -> None:
    calls: list[Any] = []

    async def _summarizer(messages: list[Message], previous: str | None) -> tuple[str, str]:
        calls.append(messages)
        return "s", "llm"

    messages = [
        Message(role="user", content="old " + "x" * 8000),
        Message(role="assistant", content="answer"),
        Message(role="user", content="now"),
        _round("pending", "unused")[0],  # tool_use whose result is not appended yet
    ]
    assert ends_mid_tool_round(messages)

    result = await CompactionService(provider=None, summarizer=_summarizer).compact(
        messages,
        turn_input_index=2,
        estimated_tokens=50_000,
        window_tokens=1_000,
        message_tokens=_tokens,
        force=True,
    )

    assert result.status == "skipped"
    assert result.reason == "mid_tool_round"
    assert result.messages is messages
    assert calls == []


@pytest.mark.asyncio
async def test_nothing_before_the_current_turn_is_a_noop() -> None:
    result = await CompactionService(provider=None).compact(
        [Message(role="user", content="x" * 40_000)],
        turn_input_index=0,
        estimated_tokens=50_000,
        window_tokens=1_000,
        message_tokens=_tokens,
        force=True,
    )

    assert result.status == "skipped"
    assert result.reason == "nothing_to_compact"


# ---------------------------------------------------------------------------
# Summarizer goes through LLMProvider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summarizer_streams_through_the_provider_abstraction() -> None:
    provider = _SummarizingProvider(text="what happened so far")
    history = [
        Message(role="user", content="deploy to host-17 at 10.0.0.7:8443"),
        Message(
            role="assistant",
            content=[
                ContentBlockThinking(thinking="secret reasoning", signature="s1"),
                ContentBlockRedactedThinking(data="opaque"),
                ContentBlockText(text="deploying"),
            ],
        ),
    ]

    result = await CompactionService(provider=provider).compact(
        [*history, Message(role="user", content="status?")],
        turn_input_index=2,
        estimated_tokens=_tokens(history) + 10,
        window_tokens=30,
        message_tokens=_tokens,
    )

    assert result.compacted
    assert result.summary == "what happened so far"
    assert result.summary_source == "llm"
    assert len(provider.calls) == 1
    sent, config = provider.calls[0]
    assert "10.0.0.7:8443" in str(sent[0].content)
    # Reasoning is not summarized; thinking is off for the summarizer call.
    assert "secret reasoning" not in str(sent[0].content)
    assert config is not None and config.thinking is False
    assert "identifiers" in str(config.system)


@pytest.mark.asyncio
async def test_provider_failure_falls_back_to_a_deterministic_digest() -> None:
    provider = _SummarizingProvider(error=True)
    history = [
        Message(role="user", content="please remember order 4711"),
        Message(role="assistant", content="noted order 4711"),
    ]

    summary, source = await summarize_messages(provider, history, None, window_tokens=1_000)

    assert source == "fallback"
    assert "order 4711" in summary
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_summarizer_exception_falls_back_and_still_shrinks() -> None:
    async def _boom(messages: list[Message], previous: str | None) -> tuple[str, str]:
        raise RuntimeError("summarizer unavailable")

    messages = [
        Message(role="user", content="old " + "x" * 8000),
        Message(role="assistant", content="answer"),
        Message(role="user", content="now"),
    ]
    result = await CompactionService(provider=None, summarizer=_boom).compact(
        messages,
        turn_input_index=2,
        estimated_tokens=_tokens(messages),
        window_tokens=1_000,
        message_tokens=_tokens,
    )

    assert result.status == "compacted"
    assert result.summary_source == "fallback"
    assert result.tokens_after < result.tokens_before
    assert result.messages[-1].content == "now"


@pytest.mark.asyncio
async def test_hung_summarizer_times_out_into_the_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentos.engine import compaction_service

    monkeypatch.setattr(compaction_service, "_SUMMARY_TIMEOUT_GRACE_SECONDS", 0.0)

    async def _hang(messages: list[Message], previous: str | None) -> tuple[str, str]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    messages = [
        Message(role="user", content="old " + "x" * 8000),
        Message(role="assistant", content="answer"),
        Message(role="user", content="now"),
    ]
    result = await CompactionService(provider=None, summarizer=_hang, timeout=0.01).compact(
        messages,
        turn_input_index=2,
        estimated_tokens=_tokens(messages),
        window_tokens=1_000,
        message_tokens=_tokens,
    )

    assert result.status == "compacted"
    assert result.summary_source == "fallback"
    assert result.tokens_after < result.tokens_before


@pytest.mark.asyncio
async def test_fallback_over_many_chunks_fits_the_window() -> None:
    """Many short messages over many chunks: the digest still shrinks the
    history well under the trigger (it used to merge one 12k digest per
    chunk and come out larger than the window)."""

    async def _down(text: str, timeout: float) -> str | None:
        return None

    messages: list[Message] = []
    for index in range(300):
        messages.append(Message(role="user", content=f"q{index} " + "x" * 150))
        messages.append(Message(role="assistant", content=f"a{index} " + "y" * 150))
    messages.append(Message(role="user", content="now"))
    window = 8_000
    result = await CompactionService(provider=None, chunk_summarizer=_down).compact(
        messages,
        turn_input_index=len(messages) - 1,
        estimated_tokens=_tokens(messages),
        window_tokens=window,
        message_tokens=_tokens,
    )

    assert result.status == "compacted"
    assert result.summary_source == "fallback"
    assert len(result.summary) <= fallback_max_chars(window) + 64
    assert result.tokens_after < window * 0.85


def test_repeated_fallbacks_do_not_grow_without_bound() -> None:
    previous: str | None = None
    batch = [Message(role="user", content="z" * 5_000)] * 30
    sizes = []
    for _ in range(5):
        previous = fallback_summary(batch, previous, max_chars=6_000)
        sizes.append(len(previous))
    assert max(sizes) <= 6_000
    assert sizes[-1] <= sizes[1] + 64


@pytest.mark.asyncio
async def test_compaction_lifecycle_events_keep_their_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "agentos.engine.compaction_service.notify_compaction",
        lambda session_key, **payload: events.append((session_key, payload)),
    )

    async def _summarizer(messages: list[Message], previous: str | None) -> tuple[str, str]:
        return "s", "llm"

    lifecycle_messages = [
        Message(role="user", content="old " + "x" * 8000),
        Message(role="assistant", content="answer"),
        Message(role="user", content="now"),
    ]
    await CompactionService(
        provider=None, summarizer=_summarizer, session_key="agent:main:main"
    ).compact(
        lifecycle_messages,
        turn_input_index=2,
        estimated_tokens=_tokens(lifecycle_messages),
        window_tokens=1_000,
        message_tokens=_tokens,
    )

    assert [payload["status"] for _, payload in events] == ["started", "observed", "observed"]
    assert {payload["phase"] for _, payload in events} == {"agent_inline_overflow"}
    assert [payload["event"] for _, payload in events] == [
        "compaction.triggered",
        "compaction.chunk_summarized",
        "compaction.summary_verified",
    ]
    assert len({payload["compaction_id"] for _, payload in events}) == 1


@pytest.mark.asyncio
async def test_preflight_refuses_when_the_current_turn_alone_is_too_large() -> None:
    """Never summarize (and durably rewrite) history for a turn that still fails."""
    calls: list[Any] = []

    async def _summarizer(messages: list[Message], previous: str | None) -> tuple[str, str]:
        calls.append(messages)
        return "s", "llm"

    messages = [
        Message(role="user", content="old " + "x" * 8000),
        Message(role="assistant", content="answer"),
        Message(role="user", content="huge paste " + "p" * 8000),
    ]
    result = await CompactionService(provider=None, summarizer=_summarizer).compact(
        messages,
        turn_input_index=2,
        estimated_tokens=_tokens(messages),
        window_tokens=1_000,
        message_tokens=_tokens,
    )

    assert result.status == "skipped"
    assert result.reason == "current_turn_too_large"
    assert result.messages is messages
    assert calls == []


@pytest.mark.parametrize("window", [128_000, 200_000])
def test_trigger_agrees_with_the_provider_request_budget(window: int) -> None:
    """The engine compacts under the line above which providers rewrite the
    payload on every request (``provider_request_max_chars`` - headroom)."""
    from agentos.engine import Agent, AgentConfig
    from agentos.engine.compaction_service import trigger_limit_tokens
    from agentos.provider.request_proof import effective_proof_budget

    agent = Agent(
        provider=_SummarizingProvider(),
        config=AgentConfig(context_window_tokens=window, max_tokens=16_384),
    )
    proof_chars = agent._provider_request_proof_max_chars()
    provider_line = effective_proof_budget(proof_chars) // 4
    limit = trigger_limit_tokens(window, 0.85, agent._compaction_budget_tokens())

    assert limit == provider_line
    assert limit < window * 0.85
    # ~74k on a 128k window, ~135k on 200k (16k output reserve).
    expected = {128_000: 73_777, 200_000: 134_977}[window]
    assert abs(limit - expected) <= 2


# ---------------------------------------------------------------------------
# Structured summary: fixed sections, previous summary as the base, file lists
# ---------------------------------------------------------------------------


def _tool_round(tool_id: str, name: str, arguments: dict[str, Any]) -> list[Message]:
    return [
        Message(
            role="assistant",
            content=[ContentBlockToolUse(id=tool_id, name=name, input=arguments)],
        ),
        Message(
            role="user",
            content=[ContentBlockToolResult(tool_use_id=tool_id, content="ok " + "r" * 3000)],
        ),
    ]


def test_summary_prompt_asks_for_the_fixed_sections_in_order() -> None:
    from agentos.engine.compaction_service import _SUMMARY_SYSTEM_PROMPT, SUMMARY_SECTIONS

    names = [name for name, _ in SUMMARY_SECTIONS]
    assert names == [
        "Goal",
        "Constraints and preferences",
        "Done",
        "In progress",
        "Blocked",
        "Key decisions",
        "Next steps",
        "Critical context",
    ]
    positions = [_SUMMARY_SYSTEM_PROMPT.index(f"- {name}:") for name in names]
    assert positions == sorted(positions)
    assert "## <section name>" in _SUMMARY_SYSTEM_PROMPT
    assert "earlier summary" in _SUMMARY_SYSTEM_PROMPT
    assert "identifiers" in _SUMMARY_SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_summarizer_call_uses_the_structured_prompt_within_the_budget() -> None:
    from agentos.engine.compaction_files import FILE_ACTIVITY_MAX_TOKENS
    from agentos.engine.compaction_service import SUMMARY_MAX_TOKENS

    provider = _SummarizingProvider(text="## Goal\n- ship")
    history = [
        Message(role="user", content="please ship v2"),
        Message(role="assistant", content="ok"),
    ]

    summary, source = await summarize_messages(provider, history, None, window_tokens=10_000)

    assert (summary, source) == ("## Goal\n- ship", "llm")
    sent, config = provider.calls[0]
    assert config is not None
    assert "- Critical context:" in str(config.system)
    # The model's text and the engine's file block share the summary budget.
    assert config.max_tokens == SUMMARY_MAX_TOKENS - FILE_ACTIVITY_MAX_TOKENS
    request = str(sent[0].content)
    assert request.startswith("Write the summary for the conversation below.")
    assert "please ship v2" in request


@pytest.mark.asyncio
async def test_previous_summary_is_the_base_of_the_next_request() -> None:
    provider = _SummarizingProvider(text="## Goal\n- updated")
    history = [Message(role="user", content="next step"), Message(role="assistant", content="ok")]

    await summarize_messages(provider, history, "## Goal\n- original", window_tokens=10_000)

    request = str(provider.calls[0][0][0].content)
    assert request.startswith("Revise the earlier summary")
    assert "[Earlier summary]\n## Goal\n- original\n[End of earlier summary]" in request
    assert request.index("[Earlier summary]") < request.index("[Newer conversation]")
    assert "next step" in request


@pytest.mark.asyncio
async def test_chunks_revise_one_summary_in_order_instead_of_merging_parts() -> None:
    requests: list[str] = []

    async def _chunk(text: str, timeout: float) -> str | None:
        requests.append(text)
        return f"## Goal\n- after chunk {len(requests)}"

    history: list[Message] = []
    for index in range(6):
        history.append(Message(role="user", content=f"q{index} " + "x" * 4000))
        history.append(Message(role="assistant", content=f"a{index} " + "y" * 4000))

    summary, source = await summarize_messages(_chunk, history, "start", window_tokens=2_000)

    assert len(requests) > 1
    assert source == "llm"
    assert summary == f"## Goal\n- after chunk {len(requests)}"
    assert "[Earlier summary]\nstart\n" in requests[0]
    for index, request in enumerate(requests[1:], start=1):
        assert f"[Earlier summary]\n## Goal\n- after chunk {index}\n" in request


@pytest.mark.asyncio
async def test_file_lists_are_merged_across_two_compactions_outside_the_model_text() -> None:
    from agentos.engine.compaction_files import FILE_ACTIVITY_HEADER, split_file_activity

    previous_seen: list[str | None] = []

    async def _summarizer(messages: list[Message], previous: str | None) -> tuple[str, str]:
        previous_seen.append(previous)
        # A model that makes up its own file list: the engine drops it.
        invented = f"{FILE_ACTIVITY_HEADER}\nModified:\n- invented.py"
        return f"## Goal\n- summary {len(previous_seen)}\n\n{invented}", "llm"

    service = CompactionService(provider=None, summarizer=_summarizer)
    first_history = [
        Message(role="user", content="refactor the parser " + "x" * 2000),
        *_tool_round("t1", "read_file", {"path": "src/parser.py"}),
        *_tool_round(
            "t2", "edit_file", {"path": "src/parser.py", "old_text": "a", "new_text": "b"}
        ),
        *_tool_round("t3", "read_file", {"path": "docs/grammar.md"}),
        Message(role="assistant", content="parser refactored"),
    ]
    first_turn = [Message(role="user", content="now the lexer")]
    messages = [*first_history, *first_turn]

    first = await service.compact(
        messages,
        turn_input_index=len(first_history),
        estimated_tokens=_tokens(messages),
        window_tokens=1_000,
        message_tokens=_tokens,
    )

    assert first.compacted
    text, files = split_file_activity(first.summary)
    assert text == "## Goal\n- summary 1"
    assert files is not None
    assert files.modified == ("src/parser.py",)
    assert files.read == ("docs/grammar.md",)
    assert first.file_activity == files
    assert "invented.py" not in first.summary
    assert first.messages[0].content == f"{CONTEXT_SUMMARY_HEADER}\n{first.summary}"

    second_history = [
        *first.messages,
        *_tool_round("t4", "read_file", {"path": "src/lexer.py"}),
        *_tool_round("t5", "write_file", {"path": "src/tokens.py", "content": "T = 1"}),
        *_tool_round("t6", "read_file", {"path": "docs/grammar.md"}),
        Message(role="assistant", content="lexer done"),
    ]
    second_turn = [Message(role="user", content="run the tests")]
    messages = [*second_history, *second_turn]

    second = await service.compact(
        messages,
        turn_input_index=len(second_history),
        estimated_tokens=_tokens(messages),
        window_tokens=1_000,
        message_tokens=_tokens,
    )

    assert second.compacted
    # The model revised the earlier text only; the lists never reached it.
    assert previous_seen == [None, "## Goal\n- summary 1"]
    text, files = split_file_activity(second.summary)
    assert text == "## Goal\n- summary 2"
    assert files is not None
    assert files.modified == ("src/parser.py", "src/tokens.py")
    assert files.read == ("src/lexer.py", "docs/grammar.md")
    assert second.summary.count(FILE_ACTIVITY_HEADER) == 1


@pytest.mark.asyncio
async def test_fallback_digest_also_carries_the_file_lists() -> None:
    from agentos.engine.compaction_files import split_file_activity

    async def _down(text: str, timeout: float) -> str | None:
        return None

    history = [
        Message(role="user", content="update config " + "x" * 4000),
        *_tool_round(
            "t1", "edit_file", {"path": "conf/app.toml", "old_text": "a", "new_text": "b"}
        ),
    ]
    messages = [*history, Message(role="user", content="next")]

    result = await CompactionService(provider=None, chunk_summarizer=_down).compact(
        messages,
        turn_input_index=len(history),
        estimated_tokens=_tokens(messages),
        window_tokens=2_000,
        message_tokens=_tokens,
    )

    assert result.compacted and result.summary_source == "fallback"
    _, files = split_file_activity(result.summary)
    assert files is not None and files.modified == ("conf/app.toml",)
