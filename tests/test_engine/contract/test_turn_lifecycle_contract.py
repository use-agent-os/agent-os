"""Contract: the observable lifecycle of one agent turn.

One test per scenario of the ``agent-turn-lifecycle`` spec (OpenSpec change
``refactor-agent-harness-core``), driven through ``Agent.run_turn`` with the
shared scripted provider and fake tools. Scenarios the harness does not meet
yet are ``xfail(strict=True)`` with the reason naming the phase that fixes
them: when that phase lands the test passes, the strict xfail fails, and the
marker must be removed.

The "router data delivered" scenario is a gateway-payload property and is
covered by the gateway serialization test of Phase 1, not here.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agentos.engine import Agent, AgentConfig
from agentos.engine.steering import SteeringMessage, bind_steering
from agentos.engine.transcript import decode_transcript_record
from agentos.engine.types import DoneEvent, ErrorEvent
from agentos.provider import Message
from tests.fakes import FakeTool, FakeToolbox, Hang, ScriptedProvider, done, error, text, tool_call

SESSION = "agent:main:webchat:contract"

PHASE1 = "Phase 1: one terminal done with an outcome on every exit path"
PHASE4 = "Phase 4: cooperative abort and durable cancelled-turn state"


# --------------------------------------------------------------------- helpers


def _agent(
    provider: ScriptedProvider,
    toolbox: FakeToolbox | None = None,
    *,
    dispatch: bool = False,
    **config: Any,
) -> Agent:
    config.setdefault("retry_base_backoff_ms", 0)
    config.setdefault("retry_max_backoff_ms", 0)
    config.setdefault("max_iterations", 8)
    return Agent(
        provider=provider,
        config=AgentConfig(**config),
        session_key=SESSION,
        **(toolbox.agent_kwargs(dispatch=dispatch) if toolbox else {}),
    )


async def _run(agent: Agent, message: str = "go") -> list[Any]:
    return [event async for event in agent.run_turn(message)]


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry backoff must not slow the suite; tool delays still use real time."""

    real_sleep = asyncio.sleep

    async def _fast(delay: float, *args: Any, **kwargs: Any) -> None:
        await real_sleep(0)

    monkeypatch.setattr("agentos.engine.agent.asyncio.sleep", _fast)


def _done_events(events: list[Any]) -> list[DoneEvent]:
    return [event for event in events if isinstance(event, DoneEvent)]


def _outcome(event: DoneEvent) -> str | None:
    outcome = getattr(event, "outcome", None)
    # ``engine.outcome.TurnOutcome``: the kind persisted as ``details.turn_outcome``.
    return None if outcome is None else str(getattr(outcome, "kind", outcome))


def _tool_blocks(messages: list[Message], kind: str) -> list[Any]:
    blocks: list[Any] = []
    for message in messages:
        if isinstance(message.content, list):
            blocks.extend(b for b in message.content if getattr(b, "type", None) == kind)
    return blocks


def _result_ids(messages: list[Message]) -> list[str]:
    return [block.tool_use_id for block in _tool_blocks(messages, "tool_result")]


def _unpaired_calls(messages: list[Message]) -> list[str]:
    results = set(_result_ids(messages))
    return [b.id for b in _tool_blocks(messages, "tool_use") if b.id not in results]


def _transcript_messages(agent: Agent) -> list[Message]:
    record = decode_transcript_record(agent.transcript_record())
    assert record is not None, "the turn left no transcript record"
    return list(record.messages)


async def _cancel_when(
    agent: Agent, ready: Any, message: str = "go", *, timeout: float = 2.0
) -> list[Any]:
    """Run a turn, cancel it once ``ready(events_so_far)`` is true; return the events."""

    events: list[Any] = []

    async def consume() -> None:
        async for event in agent.run_turn(message):
            events.append(event)

    task = asyncio.create_task(consume())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not ready(events):
        assert loop.time() < deadline, "cancel point never reached"
        await asyncio.sleep(0.002)
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout)
    except asyncio.CancelledError:
        pass
    return events


# ------------------------------------------- exactly one terminal outcome / turn


async def test_successful_turn_ends_with_exactly_one_done() -> None:
    events = await _run(_agent(ScriptedProvider([[text("hello"), done()]])))

    assert len(_done_events(events)) == 1
    assert isinstance(events[-1], DoneEvent)


@pytest.mark.xfail(strict=True, reason=PHASE1)
async def test_successful_turn_outcome_is_completed() -> None:
    events = await _run(_agent(ScriptedProvider([[text("hello"), done()]])))

    assert _outcome(events[-1]) == "completed"


@pytest.mark.xfail(strict=True, reason=PHASE1)
async def test_provider_failure_after_recovery_emits_error_then_done_failed() -> None:
    toolbox = FakeToolbox(FakeTool("read"))
    provider = ScriptedProvider(
        [
            [tool_call("read", tool_use_id="r1"), done("tool_use")],
            [error("500", "upstream exploded")],
        ]
    )

    events = await _run(_agent(provider, toolbox, max_provider_retries=1))

    errors = [i for i, e in enumerate(events) if isinstance(e, ErrorEvent)]
    assert errors, "the failure was not reported"
    assert isinstance(events[-1], DoneEvent)
    assert len(_done_events(events)) == 1
    assert errors[-1] < len(events) - 1
    assert _outcome(events[-1]) == "failed"


@pytest.mark.xfail(strict=True, reason=PHASE1)
async def test_failure_without_any_usage_still_emits_done_failed() -> None:
    provider = ScriptedProvider([[error("401", "invalid api key")]])

    events = await _run(_agent(provider))

    assert isinstance(events[-1], DoneEvent)
    assert _outcome(events[-1]) == "failed"


# ------------------------------------------- error events never end the stream


@pytest.mark.xfail(strict=True, reason=PHASE1)
async def test_consumer_waiting_for_done_sees_every_error_and_warning_first() -> None:
    provider = ScriptedProvider([[error("503", "busy")]])

    events = await _run(_agent(provider, max_provider_retries=2))

    kinds = [event.kind for event in events]
    assert "done" in kinds
    last_done = len(kinds) - 1 - kinds[::-1].index("done")
    assert last_done == len(kinds) - 1
    assert "error" in kinds[:last_done]


# ------------------------------------------- every tool call paired with one result


async def test_parallel_results_are_committed_in_call_order() -> None:
    toolbox = FakeToolbox(
        FakeTool("a", delay=0.03, concurrency="parallel"),
        FakeTool("b", delay=0.015, concurrency="parallel"),
        FakeTool("c", delay=0.0, concurrency="parallel"),
    )
    provider = ScriptedProvider(
        [
            [
                tool_call("a", tool_use_id="A"),
                tool_call("b", tool_use_id="B"),
                tool_call("c", tool_use_id="C"),
                done("tool_use"),
            ],
            [text("done"), done()],
        ]
    )

    await _run(_agent(provider, toolbox))

    finished = [call_id for event, call_id in toolbox.timeline if event == "finish"]
    assert finished[0] == "C", "the fake did not finish out of order"
    assert _result_ids(provider.calls[1].messages) == ["A", "B", "C"]


async def test_invalid_arguments_become_an_error_result_without_running_the_tool() -> None:
    toolbox = FakeToolbox(
        FakeTool("read", properties={"path": {"type": "string"}}, required=["path"])
    )
    provider = ScriptedProvider(
        [
            [tool_call("read", {"path": 42, "bogus": True}, tool_use_id="bad"), done("tool_use")],
            [text("ok"), done()],
        ]
    )

    # Argument admission lives in tools.dispatch, the path the gateway uses.
    await _run(_agent(provider, toolbox, dispatch=True))

    results = _tool_blocks(provider.calls[1].messages, "tool_result")
    assert [r.tool_use_id for r in results] == ["bad"]
    assert results[0].is_error
    assert toolbox.calls == []


@pytest.mark.xfail(strict=True, reason=PHASE4)
async def test_cancel_mid_batch_pairs_every_call_with_a_result() -> None:
    toolbox = FakeToolbox(
        FakeTool("fast", concurrency="parallel"),
        FakeTool("slow1", delay=5, concurrency="parallel"),
        FakeTool("slow2", delay=5, concurrency="parallel"),
    )
    provider = ScriptedProvider(
        [
            [
                tool_call("fast", tool_use_id="F"),
                tool_call("slow1", tool_use_id="S1"),
                tool_call("slow2", tool_use_id="S2"),
                done("tool_use"),
            ]
        ]
    )
    agent = _agent(provider, toolbox)

    await _cancel_when(
        agent,
        lambda _e: (
            toolbox.events_for("F") == ["start", "finish"] and "start" in toolbox.events_for("S2")
        ),
    )

    messages = _transcript_messages(agent)
    assert _result_ids(messages) == ["F", "S1", "S2"]
    cancelled = [r for r in _tool_blocks(messages, "tool_result") if r.tool_use_id != "F"]
    assert all(r.is_error for r in cancelled)


# ------------------------------------------- tool failures visible to the model


async def test_handler_exception_becomes_error_result_and_turn_continues() -> None:
    toolbox = FakeToolbox(FakeTool("boom", raises=RuntimeError("disk on fire")))
    provider = ScriptedProvider(
        [
            [tool_call("boom", tool_use_id="X"), done("tool_use")],
            [text("recovered"), done()],
        ]
    )

    events = await _run(_agent(provider, toolbox))

    assert len(provider.calls) == 2
    (result,) = _tool_blocks(provider.calls[1].messages, "tool_result")
    assert result.is_error
    assert "disk on fire" in str(result.content)
    assert isinstance(events[-1], DoneEvent)
    assert not any(isinstance(e, ErrorEvent) for e in events)


# ------------------------------------------- steering only at checkpoints


class _Steering:
    def __init__(self) -> None:
        self.queue: list[SteeringMessage] = []

    @property
    def session_key(self) -> str:
        return SESSION

    def has_pending(self) -> bool:
        return bool(self.queue)

    async def take(self) -> SteeringMessage | None:
        return self.queue.pop(0) if self.queue else None


def _last_user_text(messages: list[Message]) -> str:
    last = messages[-1]
    assert last.role == "user"
    return last.content if isinstance(last.content, str) else ""


async def test_steer_during_sequential_batch_skips_the_unstarted_tail() -> None:
    steering = _Steering()
    toolbox = FakeToolbox(FakeTool("w1", delay=0.02), FakeTool("w2"), FakeTool("w3"))
    provider = ScriptedProvider(
        [
            [
                tool_call("w1", tool_use_id="1"),
                tool_call("w2", tool_use_id="2"),
                tool_call("w3", tool_use_id="3"),
                done("tool_use"),
            ],
            [text("on it"), done()],
        ]
    )

    async def steer_soon() -> None:
        while "start" not in toolbox.events_for("1"):
            await asyncio.sleep(0.001)
        steering.queue.append(SteeringMessage(text="change of plan", message_id="m2"))

    with bind_steering(steering):
        steerer = asyncio.create_task(steer_soon())
        await _run(_agent(provider, toolbox))
        await steerer

    assert toolbox.events_for("1") == ["start", "finish"]
    assert toolbox.events_for("2") == [] and toolbox.events_for("3") == []
    second = provider.calls[1].messages
    results = _tool_blocks(second, "tool_result")
    assert [r.tool_use_id for r in results] == ["1", "2", "3"]
    assert results[1].is_error and results[2].is_error
    assert _last_user_text(second) == "change of plan"


async def test_steer_during_parallel_batch_never_skips_calls() -> None:
    steering = _Steering()
    toolbox = FakeToolbox(
        FakeTool("p1", delay=0.02, concurrency="parallel"),
        FakeTool("p2", delay=0.02, concurrency="parallel"),
    )
    provider = ScriptedProvider(
        [
            [tool_call("p1", tool_use_id="1"), tool_call("p2", tool_use_id="2"), done("tool_use")],
            [text("ok"), done()],
        ]
    )

    async def steer_soon() -> None:
        while "start" not in toolbox.events_for("2"):
            await asyncio.sleep(0.001)
        steering.queue.append(SteeringMessage(text="also do X", message_id="m3"))

    with bind_steering(steering):
        steerer = asyncio.create_task(steer_soon())
        await _run(_agent(provider, toolbox))
        await steerer

    assert toolbox.events_for("1") == ["start", "finish"]
    assert toolbox.events_for("2") == ["start", "finish"]
    second = provider.calls[1].messages
    assert [r.is_error for r in _tool_blocks(second, "tool_result")] == [False, False]
    assert _last_user_text(second) == "also do X"


# ------------------------------------------- cancellation is cooperative


@pytest.mark.xfail(strict=True, reason=PHASE4)
async def test_cancel_during_streaming_closes_stream_and_emits_done_cancelled() -> None:
    provider = ScriptedProvider([[text("partial answ"), Hang()]])
    agent = _agent(provider)
    events = await _cancel_when(agent, lambda seen: any(e.kind == "text_delta" for e in seen))

    assert provider.calls[0].closed_early
    assert isinstance(events[-1], DoneEvent)
    assert _outcome(events[-1]) == "interrupted"
    assert events[-1].outcome.reason == "cancelled"
    assert "partial answ" in str(_transcript_messages(agent))


@pytest.mark.xfail(strict=True, reason=PHASE4)
async def test_cancel_with_tool_ignoring_abort_still_ends_cancelled() -> None:
    toolbox = FakeToolbox(FakeTool("stubborn", delay=0.3, ignores_abort=True))
    provider = ScriptedProvider([[tool_call("stubborn", tool_use_id="S"), done("tool_use")]])
    agent = _agent(provider, toolbox)

    events = await _cancel_when(agent, lambda _e: "start" in toolbox.events_for("S"))

    assert isinstance(events[-1], DoneEvent)
    assert _outcome(events[-1]) == "interrupted"
    assert events[-1].outcome.reason == "cancelled"
    (result,) = _tool_blocks(_transcript_messages(agent), "tool_result")
    assert result.tool_use_id == "S" and result.is_error


# ------------------------------------------- cancelled turns leave durable state


@pytest.mark.xfail(strict=True, reason=PHASE4)
async def test_next_turn_after_cancel_sees_paired_results_and_interruption_note() -> None:
    toolbox = FakeToolbox(FakeTool("slow", delay=5))
    provider = ScriptedProvider(
        [
            [tool_call("slow", tool_use_id="T"), done("tool_use")],
            [text("fresh start"), done()],
        ]
    )
    agent = _agent(provider, toolbox)
    await _cancel_when(agent, lambda _e: "start" in toolbox.events_for("T"))

    agent.set_history(_transcript_messages(agent))
    await _run(agent, "new message")

    request = provider.calls[-1].messages
    assert _unpaired_calls(request) == []
    assert "T" in _result_ids(request)
    assert "interrupted" in str(request).lower()


@pytest.mark.xfail(strict=True, reason=PHASE4)
async def test_cancelled_transcript_is_valid_without_repair() -> None:
    toolbox = FakeToolbox(FakeTool("slow", delay=5))
    provider = ScriptedProvider([[tool_call("slow", tool_use_id="T"), done("tool_use")]])
    agent = _agent(provider, toolbox)

    await _cancel_when(agent, lambda _e: "start" in toolbox.events_for("T"))

    assert _unpaired_calls(_transcript_messages(agent)) == []
