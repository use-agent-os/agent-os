"""Mid-turn steering in the engine: skip checkpoint, injection, transcript record.

A message the user sends while a turn runs is taken by that turn at its next
checkpoint. These tests drive the tool batch and the agent loop with a fake
steering source; no gateway, no network, no provider.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from types import SimpleNamespace
from typing import Any

from agentos.engine import Agent, AgentConfig, ToolResult
from agentos.engine.steering import (
    SKIPPED_FOR_STEERING_REASON,
    BatchSteeringCheckpoint,
    SteeringMessage,
    bind_steering,
    is_skipped_for_steering,
    steering_for,
)
from agentos.engine.tool_batch import ToolBatchExecutor
from agentos.engine.transcript import (
    TranscriptRecord,
    build_history_from_transcript,
    decode_transcript_record,
    encode_transcript_record,
)
from agentos.engine.types import DoneEvent, SteeringEvent, ToolCall
from agentos.provider import ChatConfig, Message, ToolDefinition, ToolInputSchema
from agentos.provider import DoneEvent as ProviderDone
from agentos.provider import TextDeltaEvent as ProviderTextDelta
from agentos.provider import ToolUseEndEvent as ProviderToolUseEnd
from agentos.provider import ToolUseStartEvent as ProviderToolUseStart
from agentos.tools.batch_slot import wait_for_batch_turn, wait_outside_tool_slot
from agentos.tools.types import ToolSpec

SESSION = "agent:main:webchat:steer"


class FakeSteering:
    """A steering queue the test fills; ``take`` records what it handed out."""

    def __init__(self, session_key: str = SESSION) -> None:
        self._session_key = session_key
        self.queue: list[SteeringMessage] = []
        self.taken: list[SteeringMessage] = []
        self.attached = False

    @property
    def session_key(self) -> str:
        return self._session_key

    def attach(self) -> None:
        self.attached = True

    def has_pending(self) -> bool:
        return bool(self.queue)

    async def take(self) -> SteeringMessage | None:
        if not self.queue:
            return None
        message = self.queue.pop(0)
        self.taken.append(message)
        return message


def _spec(name: str, **kwargs: Any) -> ToolSpec:
    return ToolSpec(name=name, description=name, parameters={}, **kwargs)


def _call(index: int, name: str) -> ToolCall:
    return ToolCall(tool_use_id=f"t{index}", tool_name=name, arguments={})


def _ok(call: ToolCall) -> ToolResult:
    return ToolResult(tool_use_id=call.tool_use_id, tool_name=call.tool_name, content="ran")


async def _identity(result: ToolResult, *, tool_call: ToolCall | None = None) -> ToolResult:
    return result


def _executor(
    execute: Callable[[ToolCall], Awaitable[ToolResult]],
    specs: dict[str, ToolSpec],
    pending: Callable[[], bool],
) -> ToolBatchExecutor:
    loop = asyncio.get_running_loop()
    return ToolBatchExecutor(
        execute=execute,
        project=_identity,
        timeout_for=lambda _call: 5.0,
        spec_for=specs.get,
        deadline=loop.time() + 10.0,
        iteration_timeout=10.0,
        steering_pending=pending,
    )


async def _run(executor: ToolBatchExecutor, calls: list[ToolCall]) -> None:
    async for _event in executor.run(calls):
        pass


# ---------------------------------------------------------------------------
# Skip checkpoint
# ---------------------------------------------------------------------------


def test_checkpoint_never_cuts_before_a_call_started() -> None:
    checkpoint = BatchSteeringCheckpoint(lambda: True, [True, True])
    assert checkpoint.enter(0) is True
    assert checkpoint.enter(1) is False


def test_checkpoint_leaves_shared_calls_ahead_of_any_exclusive_one() -> None:
    checkpoint = BatchSteeringCheckpoint(lambda: True, [False, False, True])
    assert checkpoint.enter(0) is True
    assert checkpoint.enter(1) is True  # same shared group, already running
    assert checkpoint.enter(2) is False


async def test_pending_message_skips_the_rest_of_an_exclusive_sequence() -> None:
    pending: list[bool] = []
    ran: list[str] = []

    async def execute(call: ToolCall) -> ToolResult:
        ran.append(call.tool_use_id)
        pending.append(True)  # the user sends a message while t0 runs
        return _ok(call)

    executor = _executor(execute, {"write": _spec("write")}, lambda: bool(pending))
    await _run(executor, [_call(0, "write"), _call(1, "write"), _call(2, "write")])

    assert ran == ["t0"]
    results = executor.executed_results
    assert [r.content for r in results][0] == "ran"
    assert all(is_skipped_for_steering(r) for r in results[1:])
    assert results[1].execution_status["reason"] == SKIPPED_FOR_STEERING_REASON
    assert results[1].is_error is True
    # Pairing holds: one result per tool_use, in call order.
    assert [r.tool_use_id for r in results] == ["t0", "t1", "t2"]


async def test_shared_group_already_running_finishes() -> None:
    specs = {"write": _spec("write"), "read": _spec("read", concurrency="parallel")}
    pending: list[bool] = []
    ran: list[str] = []

    async def execute(call: ToolCall) -> ToolResult:
        ran.append(call.tool_use_id)
        if call.tool_use_id == "t1":
            pending.append(True)  # arrives while the shared group runs
        await asyncio.sleep(0)
        return _ok(call)

    executor = _executor(execute, specs, lambda: bool(pending))
    await _run(executor, [_call(0, "write"), _call(1, "read"), _call(2, "read")])

    assert ran == ["t0", "t1", "t2"]
    assert not any(is_skipped_for_steering(r) for r in executor.executed_results)


async def test_message_during_a_leading_shared_group_cuts_the_exclusive_call_after_it() -> None:
    specs = {"write": _spec("write"), "read": _spec("read", concurrency="parallel")}
    pending: list[bool] = []
    ran: list[str] = []

    async def execute(call: ToolCall) -> ToolResult:
        ran.append(call.tool_use_id)
        pending.append(True)
        await asyncio.sleep(0)
        return _ok(call)

    executor = _executor(execute, specs, lambda: bool(pending))
    await _run(executor, [_call(0, "read"), _call(1, "read"), _call(2, "write")])

    assert ran == ["t0", "t1"]
    assert [is_skipped_for_steering(r) for r in executor.executed_results] == [False, False, True]


async def test_shared_group_behind_an_exclusive_call_is_cut_when_nothing_of_it_started() -> None:
    specs = {"write": _spec("write"), "read": _spec("read", concurrency="parallel")}
    pending: list[bool] = []
    ran: list[str] = []

    async def execute(call: ToolCall) -> ToolResult:
        ran.append(call.tool_use_id)
        pending.append(True)
        return _ok(call)

    executor = _executor(execute, specs, lambda: bool(pending))
    await _run(executor, [_call(0, "write"), _call(1, "read"), _call(2, "read")])

    assert ran == ["t0"]
    assert [is_skipped_for_steering(r) for r in executor.executed_results] == [False, True, True]


async def test_parallel_batch_without_an_exclusive_call_runs_to_completion() -> None:
    specs = {"read": _spec("read", concurrency="parallel")}
    ran: list[str] = []

    async def execute(call: ToolCall) -> ToolResult:
        ran.append(call.tool_use_id)
        return _ok(call)

    executor = _executor(execute, specs, lambda: True)
    await _run(executor, [_call(0, "read"), _call(1, "read"), _call(2, "read")])

    assert sorted(ran) == ["t0", "t1", "t2"]
    assert not any(is_skipped_for_steering(r) for r in executor.executed_results)


async def test_early_approval_call_is_cut_when_its_turn_comes() -> None:
    """A call let in ahead of its turn passes the checkpoint in wait_for_batch_turn."""
    specs = {"exec": _spec("exec", early_approval=True)}
    pending: list[bool] = []
    first_running = asyncio.Event()
    release_first = asyncio.Event()
    bodies: list[str] = []

    async def execute(call: ToolCall) -> ToolResult:
        if call.tool_use_id == "t0":
            await wait_for_batch_turn()
            bodies.append("t0")
            first_running.set()
            await release_first.wait()
            return _ok(call)
        # t1 is started early (its approval check would run here) ...
        await wait_for_batch_turn()  # ... and must not act after a message came in
        bodies.append("t1")
        return _ok(call)

    executor = _executor(execute, specs, lambda: bool(pending))
    consumer = asyncio.create_task(_run(executor, [_call(0, "exec"), _call(1, "exec")]))
    await asyncio.wait_for(first_running.wait(), timeout=2.0)
    pending.append(True)
    release_first.set()
    await asyncio.wait_for(consumer, timeout=2.0)

    assert bodies == ["t0"]
    assert [is_skipped_for_steering(r) for r in executor.executed_results] == [False, True]


async def test_inline_approval_wait_passes_the_checkpoint_on_readmission() -> None:
    specs = {"exec": _spec("exec", early_approval=True)}
    loop = asyncio.get_running_loop()
    approvals: dict[str, asyncio.Future[bool]] = {}
    pending: list[bool] = []
    bodies: list[str] = []

    async def execute(call: ToolCall) -> ToolResult:
        approvals[call.tool_use_id] = loop.create_future()
        await wait_outside_tool_slot(approvals[call.tool_use_id])
        bodies.append(call.tool_use_id)
        pending.append(True)
        return _ok(call)

    executor = _executor(execute, specs, lambda: bool(pending))
    consumer = asyncio.create_task(_run(executor, [_call(0, "exec"), _call(1, "exec")]))
    while len(approvals) < 2:
        await asyncio.sleep(0)
    approvals["t1"].set_result(True)
    approvals["t0"].set_result(True)
    await asyncio.wait_for(consumer, timeout=2.0)

    assert bodies == ["t0"]
    assert [is_skipped_for_steering(r) for r in executor.executed_results] == [False, True]


# ---------------------------------------------------------------------------
# Agent loop: injection
# ---------------------------------------------------------------------------


def _tool_def(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name, description=name, input_schema=ToolInputSchema(properties={}, required=[])
    )


class ScriptedProvider:
    """Plays one scripted response per call; records every request."""

    provider_name = "fake"

    def __init__(self, script: list[list[Any]]) -> None:
        self._script = script
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(list(messages))
        events = self._script[min(len(self.calls), len(self._script)) - 1]
        return self._stream(events)

    async def _stream(self, events: list[Any]) -> AsyncIterator[Any]:
        for event in events:
            yield event

    async def list_models(self) -> list[Any]:
        return []


def _tool_use(index: int, name: str) -> list[Any]:
    tid = f"tool-{index}"
    return [
        ProviderToolUseStart(tool_use_id=tid, tool_name=name),
        ProviderToolUseEnd(tool_use_id=tid, tool_name=name, arguments={}),
    ]


def _text(text: str) -> list[Any]:
    return [
        ProviderTextDelta(text=text),
        ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1),
    ]


def _user_texts(messages: list[Message]) -> list[str]:
    return [m.content for m in messages if m.role == "user" and isinstance(m.content, str)]


async def test_message_sent_during_tools_is_injected_after_the_results() -> None:
    steering = FakeSteering()
    provider = ScriptedProvider(
        [
            [
                *_tool_use(0, "write"),
                *_tool_use(1, "write"),
                ProviderDone(stop_reason="tool_use", input_tokens=1, output_tokens=1),
            ],
            _text("switching to the new request"),
        ]
    )
    ran: list[str] = []

    async def handler(call: ToolCall) -> ToolResult:
        ran.append(call.tool_use_id)
        steering.queue.append(
            SteeringMessage(text="stop, do the other thing", message_id="row-2", task_id="t-2")
        )
        return _ok(call)

    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=5),
        tool_definitions=[_tool_def("write")],
        tool_handler=handler,
        session_key=SESSION,
    )
    with bind_steering(steering):
        events = [event async for event in agent.run_turn("first request")]

    assert steering.attached
    assert ran == ["tool-0"]
    second_request = provider.calls[1]
    tool_results = second_request[-2]
    assert tool_results.role == "user" and isinstance(tool_results.content, list)
    skipped = [block for block in tool_results.content if block.tool_use_id == "tool-1"]
    assert skipped and skipped[0].is_error
    assert second_request[-1] == Message(role="user", content="stop, do the other thing")
    steered = [e for e in events if isinstance(e, SteeringEvent)]
    assert [(e.text, e.message_id, e.task_id) for e in steered] == [
        ("stop, do the other thing", "row-2", "t-2")
    ]
    assert isinstance(events[-1], DoneEvent)
    record = decode_transcript_record(agent.transcript_record())
    assert record is not None
    assert record.steered_entry_ids == ("row-2",)
    assert "stop, do the other thing" in _user_texts(record.messages)


async def test_message_waiting_when_the_model_answers_continues_the_turn() -> None:
    steering = FakeSteering()
    provider = ScriptedProvider([_text("here is the plan"), _text("updated for the new ask")])
    steering.queue.append(SteeringMessage(text="also cover tests", message_id="row-9"))
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=5),
        session_key=SESSION,
    )
    with bind_steering(steering):
        events = [event async for event in agent.run_turn("make a plan")]

    assert len(provider.calls) == 2
    assert provider.calls[1][-1] == Message(role="user", content="also cover tests")
    assert provider.calls[1][-2].role == "assistant"
    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert "updated for the new ask" in done.text


async def test_no_bound_source_or_another_session_takes_nothing() -> None:
    other = FakeSteering(session_key="agent:main:webchat:someone-else")
    other.queue.append(SteeringMessage(text="not yours"))
    provider = ScriptedProvider([_text("answer")])
    agent = Agent(provider=provider, config=AgentConfig(max_iterations=3), session_key=SESSION)
    with bind_steering(other):
        assert steering_for(SESSION) is None
        _ = [event async for event in agent.run_turn("hi")]

    assert len(provider.calls) == 1
    assert other.queue and not other.taken and not other.attached


# ---------------------------------------------------------------------------
# Transcript record
# ---------------------------------------------------------------------------


def test_record_codec_keeps_steered_ids_and_omits_the_key_when_empty() -> None:
    plain = encode_transcript_record(TranscriptRecord(messages=[]))
    assert "steered_entry_ids" not in plain
    encoded = encode_transcript_record(
        TranscriptRecord(messages=[], input_entry_id="in", steered_entry_ids=("s1",))
    )
    decoded = decode_transcript_record(encoded)
    assert decoded is not None
    assert decoded.steered_entry_ids == ("s1",)


def test_load_replays_a_steered_row_once_from_the_record() -> None:
    record = encode_transcript_record(
        TranscriptRecord(
            messages=[
                Message(role="user", content="first"),
                Message(role="assistant", content="working"),
                Message(role="user", content="steer"),
                Message(role="assistant", content="done"),
            ],
            input_entry_id="in",
            steered_entry_ids=("steered",),
        )
    )
    entries = [
        SimpleNamespace(role="user", content="first", message_id="in", provider_messages=None),
        SimpleNamespace(role="user", content="steer", message_id="steered", provider_messages=None),
        SimpleNamespace(
            role="assistant", content="done", message_id="out", provider_messages=record
        ),
    ]
    loaded = build_history_from_transcript(entries)

    assert [(m.role, m.content) for m in loaded.messages] == [
        ("user", "first"),
        ("assistant", "working"),
        ("user", "steer"),
        ("assistant", "done"),
    ]


async def test_shared_group_finishes_and_the_exclusive_call_after_it_is_cut() -> None:
    specs = {"write": _spec("write"), "read": _spec("read", concurrency="parallel")}
    pending: list[bool] = []
    ran: list[str] = []

    async def execute(call: ToolCall) -> ToolResult:
        ran.append(call.tool_use_id)
        if call.tool_use_id == "t1":
            pending.append(True)
        await asyncio.sleep(0)
        return _ok(call)

    executor = _executor(execute, specs, lambda: bool(pending))
    calls = [_call(0, "write"), _call(1, "read"), _call(2, "read"), _call(3, "write")]
    await _run(executor, calls)

    assert ran == ["t0", "t1", "t2"]
    assert [is_skipped_for_steering(r) for r in executor.executed_results] == [
        False,
        False,
        False,
        True,
    ]
