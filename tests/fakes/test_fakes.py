"""Self-tests for the shared harness fakes: every script step type behaves."""

from __future__ import annotations

import asyncio

import pytest

from agentos.provider import StopReason
from agentos.tool_boundary import ToolCall
from tests.fakes import (
    ChainSelector,
    FakeTool,
    FakeToolbox,
    Hang,
    Raise,
    ScriptedProvider,
    done,
    error,
    text,
    thinking,
    tool_call,
)


async def _drain(provider: ScriptedProvider) -> list[object]:
    return [event async for event in provider.chat([], tools=None, config=None)]


async def test_plays_turns_in_order_and_records_calls() -> None:
    provider = ScriptedProvider([[text("one"), done()], [text("two"), done()]])

    first = await _drain(provider)
    second = await _drain(provider)

    assert [e.kind for e in first] == ["text_delta", "done"]
    assert first[0].text == "one"
    assert second[0].text == "two"
    assert len(provider.calls) == 2


async def test_replays_last_turn_past_end_of_script() -> None:
    provider = ScriptedProvider([[error("429")]])

    for _ in range(3):
        events = await _drain(provider)
        assert events[0].kind == "error"
        assert events[0].code == "429"
    assert len(provider.calls) == 3


async def test_thinking_tool_call_and_done_steps() -> None:
    provider = ScriptedProvider(
        [[thinking("hmm"), tool_call("read", {"p": 1}, tool_use_id="t1"), done("tool_use")]]
    )

    events = await _drain(provider)

    assert [e.kind for e in events] == ["thinking_delta", "tool_use_start", "tool_use_end", "done"]
    assert events[2].arguments == {"p": 1}
    assert events[2].tool_use_id == "t1"
    assert events[3].stop_reason is StopReason.TOOL_USE


async def test_error_carries_retry_after() -> None:
    provider = ScriptedProvider([[error("429", retry_after=2.5)]])

    (event,) = await _drain(provider)

    assert event.retry_after_seconds == 2.5


async def test_raise_step_raises_mid_stream() -> None:
    provider = ScriptedProvider([[text("partial"), Raise(RuntimeError("boom"))]])
    seen: list[str] = []

    with pytest.raises(RuntimeError, match="boom"):
        async for event in provider.chat([]):
            seen.append(event.kind)

    assert seen == ["text_delta"]
    assert provider.calls[0].closed_early is True


async def test_hang_with_seconds_then_continues() -> None:
    provider = ScriptedProvider([[Hang(0.01), text("after"), done()]])

    events = await _drain(provider)

    assert events[0].text == "after"
    assert provider.calls[0].closed_early is False


async def test_hang_forever_until_cancelled() -> None:
    provider = ScriptedProvider([[text("x"), Hang()]])

    task = asyncio.create_task(_drain(provider))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert provider.calls[0].closed_early is True


def test_empty_script_is_rejected() -> None:
    with pytest.raises(ValueError):
        ScriptedProvider([])


async def test_fallback_offer_is_recorded() -> None:
    provider = ScriptedProvider([[done()]], offers_fallback=True)

    assert provider.fallback_after_invalid_response("empty") is True
    assert provider.fallback_reasons == ["empty"]


# ----------------------------------------------------------------------- tools


def _call(name: str, call_id: str) -> ToolCall:
    return ToolCall(tool_use_id=call_id, tool_name=name, arguments={})


async def test_toolbox_ok_result_and_timeline() -> None:
    box = FakeToolbox(FakeTool("read", result="file body"))

    result = await box.handle(_call("read", "a"))

    assert result.content == "file body"
    assert result.is_error is False
    assert box.events_for("a") == ["start", "finish"]


async def test_toolbox_raises() -> None:
    box = FakeToolbox(FakeTool("bad", raises=ValueError("nope")))

    with pytest.raises(ValueError, match="nope"):
        await box.handle(_call("bad", "b"))

    assert box.events_for("b") == ["start", "raise"]


async def test_toolbox_slow_tool_records_cancel() -> None:
    box = FakeToolbox(FakeTool("slow", delay=10))

    task = asyncio.create_task(box.handle(_call("slow", "c")))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert box.events_for("c") == ["start", "cancelled"]


async def test_toolbox_ignores_abort_keeps_running_until_delay() -> None:
    box = FakeToolbox(FakeTool("stubborn", delay=0.05, ignores_abort=True))
    loop = asyncio.get_running_loop()

    started = loop.time()
    task = asyncio.create_task(box.handle(_call("stubborn", "d")))
    await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert loop.time() - started >= 0.04
    assert box.events_for("d") == ["start", "cancelled"]


def test_toolbox_exposes_definitions_and_concurrency_lookup() -> None:
    box = FakeToolbox(FakeTool("p", concurrency="parallel"), FakeTool("e"))

    kwargs = box.agent_kwargs()

    assert [d.name for d in kwargs["tool_definitions"]] == ["p", "e"]
    assert kwargs["tool_registry"].get("p").spec.concurrency == "parallel"
    assert kwargs["tool_registry"].get("e").spec.concurrency == "exclusive"
    assert kwargs["tool_registry"].get("missing") is None


# -------------------------------------------------------------------- selector


async def test_chain_selector_fails_over_through_the_real_wrapper() -> None:
    chain = ChainSelector(
        {
            "primary": ScriptedProvider([[error("429", "rate limited")]]),
            "backup": ScriptedProvider([[text("from backup"), done()]]),
        }
    )

    events = [e async for e in chain.wrapped().chat([], tools=None, config=None)]

    assert chain.failovers == ["backup"]
    assert [e.kind for e in events][-2:] == ["text_delta", "done"]
    assert events[-2].text == "from backup"


def test_chain_selector_rejects_empty_chain() -> None:
    with pytest.raises(ValueError):
        ChainSelector({})
