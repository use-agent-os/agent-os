"""Cooperative cancellation (``TurnStopSignal.request_abort``), issue #3818.

A turn asked to stop closes the provider stream, sends no further request,
seals its record and ends on its own with one DoneEvent (outcome interrupted /
cancelled), so tools get a chance to stop before the task is hard-cancelled.
"""

from __future__ import annotations

import asyncio
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
from tests.fakes import FakeTool, FakeToolbox, Hang, ScriptedProvider, done, text, tool_call


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
