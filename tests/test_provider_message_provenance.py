"""Cross-model replay: signed reasoning and tool-call ids follow the producing model.

An assistant message records which provider / model / API family produced it.
When the next request goes to the same model the history is sent unchanged
(signatures kept, so prompt caching and preserved thinking keep working).
When it goes to another model, signed or encrypted reasoning is dropped and
tool-call ids are rewritten to the target's format, with the matching tool
results rewritten too. Messages without provenance replay as before.

Fixtures are synthetic.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from agentos.engine.history import repair_tool_pairing
from agentos.provider import (
    ChatConfig,
    ContentBlockRedactedThinking,
    ContentBlockText,
    ContentBlockThinking,
    ContentBlockToolResult,
    ContentBlockToolUse,
    DoneEvent,
    Message,
    MessageProvenance,
    ModelCapabilities,
)
from agentos.provider.anthropic import AnthropicProvider
from agentos.provider.openai import OpenAIProvider
from agentos.provider.openai_responses import _responses_input
from agentos.provider.replay import (
    ANTHROPIC_TOOL_IDS,
    MISTRAL_TOOL_IDS,
    OPENAI_TOOL_IDS,
    model_api_family,
    prepare_replay,
    provenance_for_provider,
    same_origin,
)

CLAUDE = MessageProvenance(provider="anthropic", model="claude-opus-5-5", api="anthropic")
SONNET = MessageProvenance(provider="anthropic", model="claude-sonnet-4-6", api="anthropic")
KIMI = MessageProvenance(provider="opencap", model="kimi-k2.5", api="moonshot")
FOREIGN_ID = "functions.lookup:0"


def _history(source: MessageProvenance | None, tool_id: str = "toolu_01AbC") -> list[Message]:
    return [
        Message(role="user", content="look it up"),
        Message(
            role="assistant",
            content=[
                ContentBlockThinking(thinking="plan the lookup", signature="sig-claude"),
                ContentBlockRedactedThinking(data="enc-claude"),
                ContentBlockText(text="Looking it up."),
                ContentBlockToolUse(
                    id=tool_id, name="lookup", input={"q": "x"}, thought_signature="ts-1"
                ),
            ],
            reasoning_content="plan the lookup",
            reasoning_details=[{"type": "reasoning.text", "text": "plan", "signature": "s"}],
            provenance=source,
        ),
        Message(
            role="user",
            content=[ContentBlockToolResult(tool_use_id=tool_id, content="found it")],
        ),
        Message(role="assistant", content="Done.", provenance=source),
        Message(role="user", content="thanks"),
    ]


def _blocks(message: Message) -> list[Any]:
    assert isinstance(message.content, list)
    return list(message.content)


# ---------------------------------------------------------------------------
# Family and origin rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "provider_kind", "family"),
    [
        ("claude-opus-5-5", "opencap", "anthropic"),
        ("anthropic/claude-sonnet-4.6", "openrouter", "anthropic"),
        ("gpt-5.1", "opencap", "openai"),
        ("o3-mini", "openai", "openai"),
        ("openai/gpt-oss-120b", "openrouter", "openai"),
        ("deepseek-v4-pro", "opencap", "deepseek"),
        ("MiniMax-M2.5", "opencap", "minimax"),
        ("gemini-3-pro-preview", "gemini", "google"),
        ("mistral-large-latest", "mistral", "mistral"),
        ("kimi-k2.5", "opencap", "moonshot"),
        ("~anthropic/claude-opus-latest", "openrouter", "anthropic"),
        # A gateway's routing id names no family; its kind says nothing either.
        ("openrouter/auto", "openrouter", ""),
        ("house-model", "opencap", ""),
        # A single-family provider fills in for an id that names none.
        ("house-model", "anthropic", "anthropic"),
    ],
)
def test_api_family_comes_from_the_model_first(model: str, provider_kind: str, family: str) -> None:
    assert model_api_family(model, provider_kind) == family


def test_unknown_provenance_counts_as_same_model() -> None:
    assert same_origin(None, CLAUDE)
    assert same_origin(MessageProvenance(), CLAUDE)
    assert same_origin(CLAUDE, None)
    assert same_origin(CLAUDE, CLAUDE.model_copy(update={"provider": "opencap"}))
    assert not same_origin(SONNET, CLAUDE)
    assert not same_origin(CLAUDE, CLAUDE.model_copy(update={"api": "openai"}))


def test_provenance_for_provider_reads_public_metadata() -> None:
    gateway = OpenAIProvider(
        api_key="k", model="claude-opus-5-5", base_url="https://gw.test/v1", provider_kind="opencap"
    )
    assert provenance_for_provider(gateway) == MessageProvenance(
        provider="opencap", model="claude-opus-5-5", api="anthropic"
    )
    minimax = AnthropicProvider(api_key="k", model="MiniMax-M2.5", base_url="https://mm.test")
    assert provenance_for_provider(minimax) == MessageProvenance(
        provider="anthropic", model="MiniMax-M2.5", api="minimax"
    )
    assert provenance_for_provider(object()) is None


# ---------------------------------------------------------------------------
# prepare_replay
# ---------------------------------------------------------------------------


def test_same_model_replay_is_the_identical_list() -> None:
    history = _history(CLAUDE)
    assert prepare_replay(history, CLAUDE) is history


def test_legacy_messages_without_provenance_are_unchanged() -> None:
    history = _history(None, tool_id=FOREIGN_ID)
    assert prepare_replay(history, CLAUDE) is history


def test_cross_model_replay_drops_signed_reasoning_and_keeps_plain_text() -> None:
    history = _history(SONNET)
    replayed = prepare_replay(history, CLAUDE)

    assert replayed is not history
    assistant = replayed[1]
    assert [type(block) for block in _blocks(assistant)] == [
        ContentBlockText,
        ContentBlockToolUse,
    ]
    assert _blocks(assistant)[0].text == "Looking it up."
    tool_use = _blocks(assistant)[1]
    assert tool_use.thought_signature is None
    assert tool_use.id == "toolu_01AbC"  # already valid for the target: kept
    assert assistant.reasoning_details is None
    assert assistant.reasoning_content == "plan the lookup"
    # The stored history is never edited.
    assert isinstance(_blocks(history[1])[0], ContentBlockThinking)
    assert _blocks(history[1])[0].signature == "sig-claude"
    assert history[1].reasoning_details is not None


@pytest.mark.parametrize(
    ("target", "id_format"),
    [
        (CLAUDE, ANTHROPIC_TOOL_IDS),
        (MessageProvenance(provider="openai", model="gpt-5.1", api="openai"), OPENAI_TOOL_IDS),
        (
            MessageProvenance(provider="mistral", model="mistral-large", api="mistral"),
            MISTRAL_TOOL_IDS,
        ),
    ],
)
def test_tool_id_normalization_keeps_call_and_result_paired(
    target: MessageProvenance, id_format: Any
) -> None:
    long_id = "call_" + "x" * 60
    history = [
        *_history(KIMI, tool_id=FOREIGN_ID),
        Message(
            role="assistant",
            content=[ContentBlockToolUse(id=long_id, name="lookup", input={})],
            provenance=KIMI,
        ),
        Message(role="user", content=[ContentBlockToolResult(tool_use_id=long_id, content="r")]),
    ]

    replayed = prepare_replay(history, target)

    uses = [
        block
        for message in replayed
        for block in (message.content if isinstance(message.content, list) else [])
        if isinstance(block, ContentBlockToolUse)
    ]
    results = [
        block
        for message in replayed
        for block in (message.content if isinstance(message.content, list) else [])
        if isinstance(block, ContentBlockToolResult)
    ]
    assert [use.id for use in uses] == [result.tool_use_id for result in results]
    assert all(id_format.accepts(use.id) for use in uses)
    assert FOREIGN_ID not in {use.id for use in uses}
    assert len({use.id for use in uses}) == 2
    # Pairing survives: nothing is pruned as an orphan.
    assert repair_tool_pairing(replayed) is replayed
    # Deterministic: a request built twice is byte-identical.
    assert [m.model_dump(mode="json") for m in prepare_replay(history, target)] == [
        m.model_dump(mode="json") for m in replayed
    ]


def test_same_model_call_reusing_a_foreign_id_keeps_its_own_results() -> None:
    history = [
        *_history(KIMI, tool_id=FOREIGN_ID),
        Message(
            role="assistant",
            content=[ContentBlockToolUse(id=FOREIGN_ID, name="lookup", input={})],
            provenance=CLAUDE,
        ),
        Message(role="user", content=[ContentBlockToolResult(tool_use_id=FOREIGN_ID, content="r")]),
    ]
    replayed = prepare_replay(history, CLAUDE)
    assert _blocks(replayed[2])[0].tool_use_id != FOREIGN_ID
    assert _blocks(replayed[5])[0].id == FOREIGN_ID
    assert _blocks(replayed[6])[0].tool_use_id == FOREIGN_ID


def test_reasoning_only_reply_from_another_model_keeps_its_text() -> None:
    history = [
        Message(role="user", content="hi"),
        Message(
            role="assistant",
            content=[ContentBlockThinking(thinking="just thinking", signature="sig")],
            provenance=SONNET,
        ),
        Message(
            role="assistant",
            content=[ContentBlockRedactedThinking(data="enc")],
            provenance=SONNET,
        ),
        Message(role="user", content="again"),
    ]
    replayed = prepare_replay(history, CLAUDE)
    assert len(replayed) == 3
    assert _blocks(replayed[1]) == [ContentBlockText(text="just thinking")]


# ---------------------------------------------------------------------------
# Adapters: what actually goes on the wire
# ---------------------------------------------------------------------------

_ANTHROPIC_SSE = b"".join(
    f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n".encode()
    for ev in (
        {"type": "message_start", "message": {"usage": {"input_tokens": 3}}},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 1},
        },
        {"type": "message_stop"},
    )
)

_OPENAI_SSE = (
    b"".join(
        f"data: {json.dumps(chunk)}\n\n".encode()
        for chunk in (
            {"choices": [{"delta": {"content": "ok"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1}},
        )
    )
    + b"data: [DONE]\n\n"
)


def _capture(monkeypatch: pytest.MonkeyPatch, module: str, body: bytes) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    transport = httpx.MockTransport(handler)

    def patched(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(f"{module}.httpx.AsyncClient", patched)
    return captured


def _run(provider: Any, messages: list[Message], cfg: ChatConfig | None = None) -> None:
    async def _go() -> list[Any]:
        return [ev async for ev in provider.chat(messages, config=cfg or ChatConfig())]

    assert any(isinstance(ev, DoneEvent) for ev in asyncio.run(_go()))


def _anthropic_assistant_blocks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        block
        for message in payload["messages"]
        if message["role"] == "assistant" and isinstance(message["content"], list)
        for block in message["content"]
    ]


def test_anthropic_same_model_keeps_signatures(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture(monkeypatch, "agentos.provider.anthropic", _ANTHROPIC_SSE)
    provider = AnthropicProvider(api_key="k", model="claude-opus-5-5")
    _run(provider, _history(provenance_for_provider(provider)))

    blocks = _anthropic_assistant_blocks(captured[0])
    assert {"type": "thinking", "thinking": "plan the lookup", "signature": "sig-claude"} in blocks
    assert {"type": "redacted_thinking", "data": "enc-claude"} in blocks


def test_anthropic_cross_model_drops_signatures_and_fixes_tool_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture(monkeypatch, "agentos.provider.anthropic", _ANTHROPIC_SSE)
    provider = AnthropicProvider(api_key="k", model="claude-opus-5-5")
    _run(provider, _history(KIMI, tool_id=FOREIGN_ID))

    payload = captured[0]
    blocks = _anthropic_assistant_blocks(payload)
    assert not [b for b in blocks if b["type"] in {"thinking", "redacted_thinking"}]
    tool_use = next(b for b in blocks if b["type"] == "tool_use")
    assert ANTHROPIC_TOOL_IDS.accepts(tool_use["id"])
    result = next(
        b
        for message in payload["messages"]
        if message["role"] == "user" and isinstance(message["content"], list)
        for b in message["content"]
        if b["type"] == "tool_result"
    )
    assert result["tool_use_id"] == tool_use["id"]


def test_anthropic_legacy_history_replays_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture(monkeypatch, "agentos.provider.anthropic", _ANTHROPIC_SSE)
    _run(AnthropicProvider(api_key="k", model="claude-opus-5-5"), _history(None))
    blocks = _anthropic_assistant_blocks(captured[0])
    assert any(b.get("signature") == "sig-claude" for b in blocks)


_OPENROUTER_CAPS = ModelCapabilities(
    supports_reasoning=True, supports_tools=True, reasoning_format="openrouter"
)


def _openrouter(model: str) -> OpenAIProvider:
    return OpenAIProvider(
        api_key="k",
        model=model,
        base_url="https://openrouter.ai/api/v1",
        provider_kind="openrouter",
    )


def _assistant_payloads(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [m for m in payload["messages"] if m["role"] == "assistant"]


def test_openrouter_reasoning_details_follow_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture(monkeypatch, "agentos.provider.openai", _OPENAI_SSE)
    cfg = ChatConfig(model_capabilities=_OPENROUTER_CAPS)
    target = _openrouter("anthropic/claude-opus-5.5")
    same = provenance_for_provider(target)
    sibling = MessageProvenance(
        provider="openrouter", model="anthropic/claude-sonnet-4.6", api="anthropic"
    )
    for source in (same, sibling, None):
        history = _history(source)
        history[1].reasoning_details = [
            {"type": "reasoning.text", "text": "plan", "signature": "s", "format": "anthropic-v1"}
        ]
        _run(target, history, cfg)

    same_payload, sibling_payload, legacy_payload = captured
    assert "reasoning_details" in _assistant_payloads(same_payload)[0]
    # Same family, other model: the family check alone would have replayed it.
    assert "reasoning_details" not in _assistant_payloads(sibling_payload)[0]
    assert "reasoning_details" in _assistant_payloads(legacy_payload)[0]


def test_gateway_cross_family_drops_thought_signature_and_fixes_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture(monkeypatch, "agentos.provider.openai", _OPENAI_SSE)
    gateway = OpenAIProvider(
        api_key="k", model="gpt-5.1", base_url="https://gw.test/v1", provider_kind="opencap"
    )
    long_id = "toolu_" + "Z" * 50
    _run(gateway, _history(CLAUDE, tool_id=long_id))

    payload = captured[0]
    call = _assistant_payloads(payload)[0]["tool_calls"][0]
    assert "thought_signature" not in call
    assert OPENAI_TOOL_IDS.accepts(call["id"])
    tool_message = next(m for m in payload["messages"] if m["role"] == "tool")
    assert tool_message["tool_call_id"] == call["id"]


def test_responses_input_uses_normalized_call_ids() -> None:
    target = MessageProvenance(provider="openai_responses", model="gpt-5.1", api="openai")
    long_id = "toolu_" + "Q" * 50
    items = _responses_input(prepare_replay(_history(CLAUDE, tool_id=long_id), target))
    call = next(item for item in items if item.get("type") == "function_call")
    output = next(item for item in items if item.get("type") == "function_call_output")
    assert OPENAI_TOOL_IDS.accepts(call["call_id"])
    assert output["call_id"] == call["call_id"]
