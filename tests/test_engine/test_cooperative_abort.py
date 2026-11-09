"""Cooperative cancellation (``TurnStopSignal.request_abort``), issue #3818.

A turn asked to stop closes the provider stream, sends no further request,
seals its record and ends on its own with one DoneEvent (outcome interrupted /
cancelled), so tools get a chance to stop before the task is hard-cancelled.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from agentos.engine import Agent, AgentConfig
from agentos.engine.transcript import decode_transcript_record
from agentos.engine.turn_progress import (
    TurnStopSignal,
    bind_turn_stop_signal,
    turn_abort_requested,
)
from agentos.engine.types import DoneEvent, ErrorEvent
from agentos.gateway.terminal_events import mark_aborted_done, terminal_state
from tests.fakes import (
    FakeTool,
    FakeToolbox,
    Hang,
    ScriptedProvider,
    done,
    error,
    text,
    tool_call,
)


def _agent(provider: ScriptedProvider, toolbox: FakeToolbox | None = None) -> Agent:
    return Agent(
        provider=provider,
        config=AgentConfig(max_iterations=8),
        session_key="agent:main:webchat:abort",
        **(toolbox.agent_kwargs() if toolbox else {}),
    )


async def _run_with_signal(agent: Agent, signal: TurnStopSignal, ready: Any) -> list[Any]:
    events: list[Any] = []

    async def consume() -> None:
        bind_turn_stop_signal(signal)
        async for event in agent.run_turn("go"):
            events.append(event)

    task = asyncio.create_task(consume())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 3
    while not ready(events):
        assert loop.time() < deadline, "abort point never reached"
        await asyncio.sleep(0.002)
    signal.request_abort("cancelled")
    await asyncio.wait_for(task, timeout=3)  # ends on its own: no CancelledError
    return events


def _record(agent: Agent) -> str:
    record = decode_transcript_record(agent.transcript_record())
    assert record is not None
    return str(record.messages)


async def test_abort_while_streaming_closes_the_stream_and_ends_with_one_done() -> None:
    provider = ScriptedProvider([[text("partial answ"), Hang()]])
    agent = _agent(provider)

    events = await _run_with_signal(
        agent, TurnStopSignal(), lambda seen: any(e.kind == "text_delta" for e in seen)
    )

    assert provider.calls[0].closed_early
    assert not any(isinstance(e, ErrorEvent) for e in events)
    assert isinstance(events[-1], DoneEvent)
    assert sum(isinstance(e, DoneEvent) for e in events) == 1
    assert events[-1].outcome.kind == "interrupted"
    assert events[-1].outcome.reason == "cancelled"
    assert "partial answ" in events[-1].text
    record = _record(agent)
    assert "partial answ" in record and "interrupted" in record


async def test_abort_during_a_tool_lets_it_finish_and_sends_no_further_request() -> None:
    toolbox = FakeToolbox(FakeTool("slow", delay=0.05, result="slow result"))
    provider = ScriptedProvider(
        [[tool_call("slow", tool_use_id="T"), done("tool_use")], [text("never sent"), done()]]
    )
    agent = _agent(provider, toolbox)

    events = await _run_with_signal(
        agent, TurnStopSignal(), lambda _seen: "start" in toolbox.events_for("T")
    )

    assert toolbox.events_for("T") == ["start", "finish"]
    assert len(provider.calls) == 1
    assert events[-1].outcome.reason == "cancelled"
    assert "slow result" in _record(agent)


async def test_a_tool_can_see_the_abort() -> None:
    seen: list[bool] = []
    signal = TurnStopSignal()

    async def check() -> None:
        bind_turn_stop_signal(signal)
        seen.append(turn_abort_requested())
        signal.request_abort()
        seen.append(turn_abort_requested())
        await asyncio.wait_for(signal.wait_abort(), timeout=1)

    await check()
    assert seen == [False, True]


def test_an_aborted_done_frame_reports_state_aborted() -> None:
    payload = {
        "text": "partial",
        "outcome": {"kind": "interrupted", "reason": "cancelled"},
    }

    mark_aborted_done(payload)

    assert payload["reason"] == "aborted"
    assert payload["partial_text"] == "partial"
    assert terminal_state("session.event.done", payload) == "aborted"
    completed = mark_aborted_done({"outcome": {"kind": "completed", "reason": "done"}})
    assert terminal_state("session.event.done", completed) == "done"


def _texts(messages: Any) -> list[str]:
    """Every text in *messages*: plain string contents and text blocks."""
    out: list[str] = []
    for message in messages:
        content = message.content
        if isinstance(content, str):
            out.append(content)
            continue
        for block in content or []:
            value = getattr(block, "text", None)
            if isinstance(value, str):
                out.append(value)
    return out


async def test_abort_after_a_committed_round_does_not_repeat_its_text() -> None:
    # "Checking now." is already on record with its tool call; the abort that
    # ends the turn after the tool must not seal it again as a partial reply.
    toolbox = FakeToolbox(FakeTool("slow", delay=0.05, result="slow result"))
    provider = ScriptedProvider(
        [
            [text("Checking now."), tool_call("slow", tool_use_id="T"), done("tool_use")],
            [text("never sent"), done()],
        ]
    )
    agent = _agent(provider, toolbox)

    events = await _run_with_signal(
        agent, TurnStopSignal(), lambda _seen: "start" in toolbox.events_for("T")
    )

    assert events[-1].outcome.kind == "interrupted"
    assert events[-1].text.count("Checking now.") == 1
    assert _record(agent).count("Checking now.") == 1
    assert sum(item.count("Checking now.") for item in _texts(agent._history)) == 1


async def test_hard_cancel_during_a_tool_batch_does_not_repeat_committed_text() -> None:
    toolbox = FakeToolbox(FakeTool("slow", delay=5.0, result="never"))
    provider = ScriptedProvider(
        [[text("Checking now."), tool_call("slow", tool_use_id="T"), done("tool_use")]]
    )
    agent = _agent(provider, toolbox)

    async def consume() -> None:
        async for _event in agent.run_turn("go"):
            pass

    task = asyncio.create_task(consume())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 3
    while "start" not in toolbox.events_for("T"):
        assert loop.time() < deadline, "tool never started"
        await asyncio.sleep(0.002)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert sum(item.count("Checking now.") for item in _texts(agent._history)) == 1


async def test_abort_wins_over_buffered_events_and_no_tool_runs() -> None:
    # The rest of the response (a tool call and done) is already buffered when
    # the stop lands: the stream is closed and the tool never starts.
    toolbox = FakeToolbox(FakeTool("delete_everything", result="deleted"))
    provider = ScriptedProvider(
        [
            [
                text("On it."),
                tool_call("delete_everything", tool_use_id="D"),
                done("tool_use"),
            ]
        ]
    )
    agent = _agent(provider, toolbox)
    signal = TurnStopSignal()
    events: list[Any] = []

    async def consume() -> None:
        bind_turn_stop_signal(signal)
        async for event in agent.run_turn("go"):
            events.append(event)
            if event.kind == "text_delta":
                signal.request_abort("cancelled")

    await asyncio.wait_for(consume(), timeout=3)

    assert toolbox.events_for("D") == []
    assert provider.calls[0].closed_early
    assert sum(isinstance(e, DoneEvent) for e in events) == 1
    assert events[-1].outcome.kind == "interrupted"


async def test_abort_during_a_retry_wait_sends_no_further_request() -> None:
    provider = ScriptedProvider(
        [[error("429", "rate limited", retry_after=5.0)], [text("second request"), done()]]
    )
    agent = _agent(provider)
    signal = TurnStopSignal()
    events: list[Any] = []
    loop = asyncio.get_running_loop()

    async def consume() -> None:
        bind_turn_stop_signal(signal)
        async for event in agent.run_turn("go"):
            events.append(event)
            if getattr(event, "code", None) == "provider_retry_after_wait":
                loop.call_later(0.05, signal.request_abort, "cancelled")

    began = loop.time()
    await asyncio.wait_for(consume(), timeout=3)

    assert loop.time() - began < 2  # the 5 s wait was cut short
    assert len(provider.calls) == 1
    assert sum(isinstance(e, DoneEvent) for e in events) == 1
    assert events[-1].outcome.kind == "interrupted"
