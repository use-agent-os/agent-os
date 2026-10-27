"""Tests for context window compaction logic."""

import pytest

from agentos.session.compaction import (
    CompactionConfig,
    CompactionRequest,
    _find_turn_boundary_cut,
    call_compaction_llm,
    compact_context,
    estimate_entry_replay_tokens,
)
from agentos.session.compaction_lifecycle import compaction_effect_payload


def _make_entries(n: int, tokens_each: int = 100) -> list[dict]:
    return [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"message {i} " + "x" * 50,
            "token_count": tokens_each,
        }
        for i in range(n)
    ]


def test_compaction_effect_payload_marks_automatic_noop_not_user_visible():
    payload = compaction_effect_payload(
        status="skipped",
        source="automatic",
        reason="within_compaction_budget",
    )

    assert payload == {
        "applied": False,
        "durability": "none",
        "skip_reason": "within_compaction_budget",
        "user_visible": False,
    }


def test_compaction_effect_payload_surfaces_non_benign_skip_reasons():
    for reason in ("coverage_blocked", "empty_summary", "no_safe_turn_boundary"):
        payload = compaction_effect_payload(
            status="skipped",
            source="automatic",
            reason=reason,
        )

        assert payload["applied"] is False
        assert payload["durability"] == "none"
        assert payload["skip_reason"] == reason
        assert payload["user_visible"] is True


def test_compaction_effect_payload_marks_durable_completion_applied():
    payload = compaction_effect_payload(status="completed", source="automatic")

    assert payload["applied"] is True
    assert payload["durability"] == "durable"
    assert payload["user_visible"] is True


@pytest.mark.asyncio
async def test_no_compaction_needed_small_context():
    entries = _make_entries(5, tokens_each=10)  # 50 tokens total
    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=10_000,  # huge window
        )
    )
    assert result.removed_count == 0
    assert result.kept_entries == entries
    assert result.summary_source == "skipped"
    assert result.skip_reason == "within_compaction_budget"


@pytest.mark.asyncio
async def test_compaction_occurs_when_over_budget():
    entries = _make_entries(20, tokens_each=200)  # 4000 tokens
    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=1000,  # tight window
        )
    )
    assert result.removed_count > 0
    assert result.summary != ""
    assert result.chunks_processed >= 1
    assert result.summary_source == "fallback"
    assert result.tokens_before == 4000
    assert result.tokens_after < result.tokens_before
    assert result.remaining_budget_tokens >= 0


def test_replay_token_estimate_uses_tool_payload_summary_not_raw_arguments():
    large_content = "x" * 80_000
    entry = {
        "role": "assistant",
        "content": "wrote file",
        "token_count": 1,
        "tool_calls": [
            {
                "type": "tool_use",
                "tool_use_id": "write-large",
                "name": "write_file",
                "input": {"path": "index.html", "content": large_content},
            }
        ],
        "reasoning_content": "private reasoning " + ("r" * 20_000),
    }

    tokens = estimate_entry_replay_tokens(entry)

    assert tokens < 500


@pytest.mark.asyncio
async def test_compaction_source_is_llm_when_all_chunks_use_llm(monkeypatch):
    calls: list[str] = []

    async def fake_llm(**kwargs):
        calls.append(kwargs["chunk_text"])
        return "LLM summary"

    monkeypatch.setattr("agentos.session.compaction.call_compaction_llm", fake_llm)
    entries = _make_entries(12, tokens_each=200)

    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=500,
            config=CompactionConfig(model="test/model", api_key="test-key"),
        )
    )

    assert calls
    assert result.removed_count > 0
    assert result.summary_source == "llm"


@pytest.mark.asyncio
async def test_compaction_source_is_mixed_when_llm_partly_falls_back(monkeypatch):
    responses = ["LLM summary", None]

    async def fake_llm(**kwargs):
        return responses.pop(0) if responses else "LLM summary"

    monkeypatch.setattr("agentos.session.compaction.call_compaction_llm", fake_llm)
    entries = _make_entries(12, tokens_each=200)

    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=500,
            config=CompactionConfig(model="test/model", api_key="test-key"),
        )
    )

    assert result.removed_count > 0
    assert result.summary_source == "mixed"


@pytest.mark.asyncio
async def test_compaction_keeps_recent_entries():
    entries = _make_entries(20, tokens_each=200)
    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=1000,
        )
    )
    # kept entries should be a tail of the original
    if result.kept_entries:
        last_kept = result.kept_entries[-1]
        assert last_kept in entries[-len(result.kept_entries) :]


def test_find_turn_boundary_cut_does_not_orphan_second_parallel_tool_result():
    # A single assistant turn with two parallel tool calls. The naive
    # token-budget cut lands between the two tool results.
    entries = [
        {"role": "user", "content": "analyze files"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_1", "function": {"name": "read_file"}},
                {"id": "call_2", "function": {"name": "read_file"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "file 1 data " * 50},
        {"role": "tool", "tool_call_id": "call_2", "content": "file 2 data"},
        {"role": "assistant", "content": "Analysis complete."},
    ]

    cut = _find_turn_boundary_cut(entries, keep_budget=30)

    first_kept = entries[cut] if cut < len(entries) else None
    assert first_kept is None or first_kept.get("role") != "tool"


def test_find_turn_boundary_cut_falls_back_before_the_whole_parallel_turn():
    # Same shape, but the only budget-driven cut lands on the first of the
    # two tool results — the cut must still skip back past the assistant
    # message that issued both calls, not just past the immediate pair.
    entries = [
        {"role": "user", "content": "analyze files"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_1", "function": {"name": "read_file"}},
                {"id": "call_2", "function": {"name": "read_file"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "x"},
        {"role": "tool", "tool_call_id": "call_2", "content": "file 2 data " * 50},
        {"role": "assistant", "content": "Analysis complete."},
    ]

    cut = _find_turn_boundary_cut(entries, keep_budget=30)

    first_kept = entries[cut] if cut < len(entries) else None
    assert first_kept is None or first_kept.get("role") != "tool"


def test_find_turn_boundary_cut_does_not_walk_back_past_a_tool_call_with_no_result_yet():
    # An assistant tool call that never got a result recorded (the turn was
    # cut short) followed by later, unrelated turns. Cutting right after the
    # pending call orphans nothing -- there is no tool result anywhere in the
    # kept set for it to leave stranded -- so this is already a clean
    # boundary and must not be walked back past. A fix that also checks
    # "was the last removed entry an assistant tool call" (in addition to
    # "is the first kept entry a tool result") re-triggers here even though
    # nothing is at risk, discarding an extra turn it didn't need to.
    entries = [
        {"role": "user", "content": "long earlier context " * 20},
        {"role": "assistant", "content": "ok"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_1", "function": {"name": "read_file"}}],
        },
        {"role": "user", "content": "unrelated later turn"},
        {"role": "assistant", "content": "unrelated reply"},
    ]

    cut = _find_turn_boundary_cut(entries, keep_budget=8)

    assert entries[cut]["content"] == "unrelated later turn"


@pytest.mark.asyncio
async def test_empty_entries():
    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=[],
            context_window_tokens=1000,
        )
    )
    assert result.removed_count == 0
    assert result.kept_entries == []
    assert result.summary == ""
    assert result.skip_reason == "no_entries"


@pytest.mark.asyncio
async def test_custom_config():
    entries = _make_entries(20, tokens_each=200)
    cfg = CompactionConfig(
        base_chunk_ratio=0.3,
        min_chunk_ratio=0.1,
        safety_margin=1.0,
        default_parts=3,
        identifier_policy="off",
    )
    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=500,
            config=cfg,
        )
    )
    assert result.removed_count > 0


@pytest.mark.asyncio
async def test_strict_identifier_policy_in_summary():
    entries = _make_entries(10, tokens_each=200)
    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=500,
            config=CompactionConfig(identifier_policy="strict"),
        )
    )
    if result.summary:
        assert "identifier" in result.summary.lower() or "IMPORTANT" in result.summary


@pytest.mark.asyncio
async def test_chunks_processed_count():
    entries = _make_entries(30, tokens_each=200)
    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=500,
        )
    )
    assert result.chunks_processed >= 1


def _install_sse_provider_transport(
    monkeypatch,
    captured: dict[str, object],
    *,
    error: Exception | None = None,
) -> None:
    """Serve the summarizer's provider request from an in-memory SSE stream.

    ``call_compaction_llm`` goes through the ``LLMProvider`` abstraction (an
    OpenAI-compatible provider when no session provider is passed), so the
    fake sits at the provider's HTTP transport, not in the compaction module.
    """
    import json as _json

    import httpx

    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        if error is not None:
            raise error
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["json"] = _json.loads(request.content)
        body = (
            'data: {"choices":[{"delta":{"content":"summary"}}]}\n\n'
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body.encode("utf-8"),
        )

    monkeypatch.setattr(
        "agentos.provider.openai.httpx.AsyncClient",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(handler),
            timeout=kwargs.get("timeout"),
        ),
    )


@pytest.mark.asyncio
async def test_call_compaction_llm_adds_openrouter_app_attribution(monkeypatch) -> None:
    captured: dict[str, object] = {}
    _install_sse_provider_transport(monkeypatch, captured)

    result = await call_compaction_llm(
        chunk_text="old conversation",
        identifier_instruction="",
        model="openai/gpt-4o-mini",
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        timeout=10.0,
    )

    assert result == "summary"
    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["authorization"] == "Bearer test-key"
    assert headers["http-referer"] == "https://useagentos.dev"
    assert headers["x-openrouter-title"] == "AgentOS"
    assert headers["x-openrouter-categories"] == "cli-agent,personal-agent"
    payload = captured["json"]
    assert isinstance(payload, dict)
    assert payload["model"] == "openai/gpt-4o-mini"


@pytest.mark.asyncio
async def test_call_compaction_llm_timeout_returns_none(monkeypatch) -> None:
    _install_sse_provider_transport(monkeypatch, {}, error=TimeoutError("summary timed out"))

    result = await call_compaction_llm(
        chunk_text="old conversation",
        identifier_instruction="",
        model="openai/gpt-4o-mini",
        api_key="test-key",
        timeout=0.01,
    )

    assert result is None


@pytest.mark.asyncio
async def test_call_compaction_llm_streams_through_a_session_provider() -> None:
    """A resolved session provider is used as is -- no raw HTTP, no key needed."""
    from agentos.provider import DoneEvent, Message, TextDeltaEvent

    calls: list[tuple[list[Message], object]] = []

    class _Provider:
        provider_name = "fake"

        def chat(self, messages, tools=None, config=None):
            calls.append((messages, config))

            async def _stream():
                yield TextDeltaEvent(text="provider ")
                yield TextDeltaEvent(text="summary")
                yield DoneEvent()

            return _stream()

    result = await call_compaction_llm(
        chunk_text="old conversation",
        identifier_instruction="Preserve exact IDs.",
        model="any-model",
        api_key="",
        provider=_Provider(),
    )

    assert result == "provider summary"
    assert len(calls) == 1
    sent, config = calls[0]
    assert "old conversation" in str(sent[0].content)
    assert "Preserve exact IDs." in str(getattr(config, "system", ""))


@pytest.mark.asyncio
async def test_custom_instructions_are_user_scoped_and_identifier_policy_stays_system(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}
    _install_sse_provider_transport(monkeypatch, captured)

    await call_compaction_llm(
        chunk_text="old conversation",
        identifier_instruction="Preserve exact IDs.",
        model="openai/gpt-4o-mini",
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        timeout=10.0,
        custom_instructions="Focus on deployment decisions.",
    )

    messages = captured["json"]["messages"]
    assert messages[0]["role"] == "system"
    assert "Preserve exact IDs." in messages[0]["content"]
    assert "Focus on deployment decisions." not in messages[0]["content"]
    assert messages[1]["role"] == "user"
    assert "Focus on deployment decisions." in messages[1]["content"]


# ---------------------------------------------------------------------------
# Session-level /compact uses the engine's structured summary and file lists
# ---------------------------------------------------------------------------


def _tool_entry(index: int, name: str, arguments: dict, *, error: bool = False) -> list[dict]:
    call_id = f"call-{index}"
    return [
        {"role": "user", "content": f"step {index} " + "x" * 50, "token_count": 200},
        {
            "role": "assistant",
            "content": f"did step {index}",
            "token_count": 200,
            "tool_calls": [
                {"type": "tool_use", "tool_use_id": call_id, "name": name, "input": arguments},
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "name": name,
                    "result": "error" if error else "ok",
                    "is_error": error,
                },
            ],
        },
    ]


@pytest.mark.asyncio
async def test_call_compaction_llm_sends_the_structured_summary_request() -> None:
    from agentos.engine.compaction_service import SUMMARY_SECTIONS, SUMMARY_TEXT_MAX_TOKENS
    from agentos.provider import DoneEvent, TextDeltaEvent

    calls: list[tuple[list, object]] = []

    class _Provider:
        provider_name = "fake"

        def chat(self, messages, tools=None, config=None):
            calls.append((messages, config))

            async def _stream():
                yield TextDeltaEvent(text="## Goal\n- revised")
                yield DoneEvent()

            return _stream()

    result = await call_compaction_llm(
        chunk_text="[user]: next step",
        identifier_instruction="Preserve exact IDs.",
        model="any-model",
        api_key="",
        provider=_Provider(),
        earlier_summary="## Goal\n- original",
        custom_instructions="Focus on the parser.",
    )

    assert result == "## Goal\n- revised"
    sent, config = calls[0]
    system = str(getattr(config, "system", ""))
    for name, _ in SUMMARY_SECTIONS:
        assert f"- {name}:" in system
    assert system.rstrip().endswith("Preserve exact IDs.")
    assert getattr(config, "max_tokens", None) == SUMMARY_TEXT_MAX_TOKENS
    request = str(sent[0].content)
    assert request.startswith("Additional summary instructions.")
    assert "Focus on the parser." in request
    assert "[Earlier summary]\n## Goal\n- original\n[End of earlier summary]" in request
    assert "[Newer conversation]\n[user]: next step" in request


@pytest.mark.asyncio
async def test_compact_context_rolls_one_summary_and_appends_the_file_lists(monkeypatch):
    from agentos.engine.compaction_files import (
        FILE_ACTIVITY_HEADER,
        FileActivity,
        attach_file_activity,
        split_file_activity,
    )

    earlier: list[str | None] = []

    async def fake_llm(**kwargs):
        earlier.append(kwargs.get("earlier_summary"))
        # The model tries to write its own file list; it is dropped.
        return f"## Goal\n- after chunk {len(earlier)}\n\n{FILE_ACTIVITY_HEADER}\nRead:\n- fake.py"

    monkeypatch.setattr("agentos.session.compaction.call_compaction_llm", fake_llm)
    entries = [
        *_tool_entry(0, "read_file", {"path": "src/parser.py"}),
        *_tool_entry(1, "edit_file", {"path": "src/parser.py", "old_text": "a", "new_text": "b"}),
        *_tool_entry(2, "write_file", {"path": "src/broken.py"}, error=True),
        *_tool_entry(3, "read_file", {"path": "docs/grammar.md"}),
        *_tool_entry(4, "exec_command", {"command": "touch src/shell_only.py"}),
        *_tool_entry(5, "read_file", {"path": "src/lexer.py"}),
    ]
    previous = attach_file_activity(
        "## Goal\n- before", FileActivity(read=("README.md",), modified=("setup.py",))
    )

    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=1_000,
            config=CompactionConfig(model="test/model", api_key="test-key"),
            previous_summary=previous,
        )
    )

    assert result.removed_count > 0
    assert len(earlier) > 1
    # The first chunk revises the previous summary; every later one revises
    # the summary so far.
    assert earlier[0] == "## Goal\n- before"
    for index, base in enumerate(earlier[1:], start=1):
        assert base == f"## Goal\n- after chunk {index}"
    text, files = split_file_activity(result.summary)
    assert text == f"## Goal\n- after chunk {len(earlier)}"
    assert files is not None
    assert "fake.py" not in result.summary
    assert "src/shell_only.py" not in result.summary
    assert "src/broken.py" not in result.summary
    assert "src/parser.py" in files.modified
    assert "src/parser.py" not in files.read
    # The structured payload replays the summary text, file lists included.
    assert result.summary_payload is not None
    assert "src/parser.py" in result.summary_payload["current_status"]


@pytest.mark.asyncio
async def test_compact_context_merges_the_previous_file_lists(monkeypatch):
    from agentos.engine.compaction_files import (
        FileActivity,
        attach_file_activity,
        split_file_activity,
    )

    async def fake_llm(**kwargs):
        return "## Goal\n- next"

    monkeypatch.setattr("agentos.session.compaction.call_compaction_llm", fake_llm)
    previous = attach_file_activity(
        "## Goal\n- before", FileActivity(read=("README.md",), modified=("setup.py",))
    )
    entries = [
        *_tool_entry(0, "edit_file", {"path": "src/a.py", "old_text": "a", "new_text": "b"}),
        *_tool_entry(1, "read_file", {"path": "README.md"}),
        *_tool_entry(2, "read_file", {"path": "src/z.py"}),
    ]

    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=400,
            config=CompactionConfig(model="test/model", api_key="test-key"),
            previous_summary=previous,
        )
    )

    assert result.removed_count > 0
    _, files = split_file_activity(result.summary)
    assert files is not None
    assert files.modified[:2] == ("setup.py", "src/a.py")
    assert files.read[0] == "README.md"


@pytest.mark.asyncio
async def test_a_revised_durable_summary_is_marked_and_keeps_its_predecessors_facts(monkeypatch):
    from agentos.engine.compaction_files import (
        FileActivity,
        attach_file_activity,
        split_file_activity,
    )

    bases: list[str | None] = []

    async def fake_llm(**kwargs):
        bases.append(kwargs.get("earlier_summary"))
        return "## Goal\n- revised"

    monkeypatch.setattr("agentos.session.compaction.call_compaction_llm", fake_llm)
    previous = attach_file_activity("## Goal\n- before", FileActivity(modified=("setup.py",)))
    entries = [
        *_tool_entry(0, "edit_file", {"path": "src/a.py", "old_text": "a", "new_text": "b"}),
        *_tool_entry(1, "read_file", {"path": "src/z.py"}),
    ]

    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=400,
            config=CompactionConfig(model="test/model", api_key="test-key"),
            previous_summary=previous,
            previous_compaction_id="cmp_prev",
            previous_summary_payload={
                "current_status": previous,
                "important_identifiers": ["host-17"],
                "critical_carry_forward": ["order 4711"],
            },
        )
    )

    assert bases[0] == "## Goal\n- before"
    payload = result.summary_payload
    assert payload is not None
    assert payload["revises_compaction_id"] == "cmp_prev"
    assert "host-17" in payload["important_identifiers"]
    assert "order 4711" in payload["critical_carry_forward"]
    _, files = split_file_activity(result.summary)
    assert files is not None and files.modified == ("setup.py", "src/a.py")


@pytest.mark.asyncio
async def test_without_a_model_summary_the_durable_predecessor_is_left_alone():
    from agentos.engine.compaction_files import split_file_activity

    entries = [
        *_tool_entry(0, "edit_file", {"path": "src/a.py", "old_text": "a", "new_text": "b"}),
        *_tool_entry(1, "read_file", {"path": "src/z.py"}),
    ]

    result = await compact_context(
        CompactionRequest(
            session_id="s1",
            entries=entries,
            context_window_tokens=400,
            config=CompactionConfig(),  # no model: deterministic digest only
            previous_summary="## Goal\n- before",
            previous_compaction_id="cmp_prev",
        )
    )

    assert result.removed_count > 0
    assert result.summary_payload is not None
    # Not a revision: the previous summary keeps replaying on its own, and
    # the digest does not copy it.
    assert "revises_compaction_id" not in result.summary_payload
    assert "- before" not in result.summary
    _, files = split_file_activity(result.summary)
    assert files is not None and files.modified == ("src/a.py",)


def test_merge_structured_payloads_keeps_previous_facts_newest_last_and_capped() -> None:
    from agentos.session.compaction_state import merge_structured_payloads

    merged = merge_structured_payloads(
        {"user_goal": "ship v2", "important_identifiers": ["a", "b"], "current_status": "old"},
        {"user_goal": "", "important_identifiers": ["b", "c"], "current_status": "new"},
        max_items=2,
    )

    assert merged["current_status"] == "new"
    assert merged["user_goal"] == "ship v2"
    assert merged["important_identifiers"] == ["b", "c"]
