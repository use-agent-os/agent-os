"""``TurnHarness`` terminal paths: late cancels, cancels without a progress
writer, and the router-control replay.

Each test drives a real ``TurnRunner`` over an in-memory session store with a
scripted provider, and swaps only the one port or stage the scenario needs.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.engine.runtime import TurnRunner
from agentos.engine.types import (
    AgentEvent,
    DoneEvent,
    RouterControlReplayEvent,
    TextDeltaEvent,
)
from agentos.gateway.config import AgentOSRouterConfig, AttachmentsConfig, GatewayConfig
from agentos.provider import Message
from agentos.session.manager import SessionManager
from agentos.session.storage import SessionStorage
from agentos.tools.registry import ToolRegistry
from agentos.tools.types import CallerKind, ToolContext, ToolSpec
from tests.fakes import ScriptedProvider, done, text, tool_call

KEY = "agent:main:webchat:harness-terminal"


class _Selector:
    def __init__(self, provider: ScriptedProvider) -> None:
        self.provider = provider
        self.current_config = SimpleNamespace(model="scripted/model")

    def clone(self) -> _Selector:
        return self

    def resolve(self) -> ScriptedProvider:
        return self.provider

    def override_model(self, model: str) -> None:
        self.current_config = SimpleNamespace(model=model)


@pytest.fixture(autouse=True)
def _state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path))


@pytest.fixture
async def storage() -> AsyncIterator[SessionStorage]:
    store = SessionStorage(":memory:")
    await store.connect()
    yield store
    await store.close()


@pytest.fixture
def manager(storage: SessionStorage, tmp_path: Path) -> SessionManager:
    return SessionManager(storage, inject_time_prefix=False, checkpoint_workspace_dir=tmp_path)


def _runner(
    manager: SessionManager,
    provider: ScriptedProvider,
    tmp_path: Path,
    registry: ToolRegistry | None = None,
) -> TurnRunner:
    return TurnRunner(
        provider_selector=_Selector(provider),
        tool_registry=registry or ToolRegistry(),
        session_manager=manager,
        config=GatewayConfig(
            attachments=AttachmentsConfig(media_root=str(tmp_path / "media")),
            agentos_router=AgentOSRouterConfig(enabled=False),
        ),
    )


async def _rows(storage: SessionStorage) -> list[tuple[str, str, bool]]:
    async with storage.conn.execute(
        "SELECT role, content, provider_messages IS NOT NULL FROM transcript_entries ORDER BY id"
    ) as cursor:
        return [(row[0], row[1], bool(row[2])) for row in await cursor.fetchall()]


def _texts(messages: list[Message], role: str) -> list[str]:
    out: list[str] = []
    for message in messages:
        if message.role != role:
            continue
        if isinstance(message.content, str):
            out.append(message.content)
            continue
        out.extend(
            str(getattr(block, "text", ""))
            for block in message.content
            if getattr(block, "type", None) == "text"
        )
    return out


async def _turn(runner: TurnRunner, manager: SessionManager, message: str) -> list[AgentEvent]:
    await manager.append_message(KEY, "user", message)
    ctx = ToolContext(caller_kind=CallerKind.WEB)
    events = runner.run(message, KEY, tool_context=ctx, history_has_persisted_user=True)
    return [event async for event in events]


# ---------------------------------------------------------------------------
# A cancel that lands after the finalizer persisted the reply
# ---------------------------------------------------------------------------


class _StallingCapture:
    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def capture_turn(self, **_kwargs: Any) -> None:
        self.entered.set()
        await asyncio.sleep(30)


class _StallingTotals:
    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def rollup(self, **_kwargs: Any) -> None:
        self.entered.set()
        await asyncio.sleep(30)


@pytest.mark.parametrize("stalled_port", ["memory_capture", "totals_rollup"])
async def test_a_cancel_after_the_reply_was_persisted_does_not_write_it_twice(
    storage: SessionStorage,
    manager: SessionManager,
    tmp_path: Path,
    stalled_port: str,
) -> None:
    provider = ScriptedProvider([[text("the full answer"), done()], [text("second"), done()]])
    runner = _runner(manager, provider, tmp_path)
    await manager.create(KEY)
    finalizer = runner._harness.stages.turn_finalizer
    stall: _StallingCapture | _StallingTotals
    if stalled_port == "memory_capture":
        stall = _StallingCapture()
        real_port: Any = finalizer._turn_memory_capture
        finalizer._turn_memory_capture = stall
    else:
        stall = _StallingTotals()
        real_port = finalizer._session_totals
        finalizer._session_totals = stall

    seen: list[AgentEvent] = []

    async def first_turn() -> None:
        await manager.append_message(KEY, "user", "question")
        ctx = ToolContext(caller_kind=CallerKind.WEB)
        async for event in runner.run(
            "question", KEY, tool_context=ctx, history_has_persisted_user=True
        ):
            seen.append(event)

    task = asyncio.create_task(first_turn())
    await asyncio.wait_for(stall.entered.wait(), 10)
    assert sum(isinstance(event, DoneEvent) for event in seen) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    rows = await _rows(storage)
    assistant = [(content, structured) for role, content, structured in rows if role == "assistant"]
    assert assistant == [("the full answer", True)]
    assert not any("[interrupted]" in content for _, content, _ in rows)

    # The next request carries the answer once.
    if stalled_port == "memory_capture":
        finalizer._turn_memory_capture = real_port
    else:
        finalizer._session_totals = real_port
    await _turn(runner, manager, "next")
    assert _texts(provider.calls[-1].messages, "assistant") == ["the full answer"]


# ---------------------------------------------------------------------------
# A cancel with no progress writer
# ---------------------------------------------------------------------------


async def test_a_cancel_without_a_progress_writer_still_persists_the_sealed_record(
    storage: SessionStorage,
    manager: SessionManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A session manager that cannot replace rows gets no progress writer; the
    # cancel path must still reach the agent and persist its sealed record.
    started = asyncio.Event()
    registry = ToolRegistry()

    async def slow(**_arguments: Any) -> str:
        started.set()
        await asyncio.sleep(30)
        return "never"

    registry.register(ToolSpec(name="slow", description="slow", parameters={}), slow)
    provider = ScriptedProvider(
        [
            [tool_call("slow", tool_use_id="T1"), done("tool_use")],
            [text("fresh start"), done()],
        ]
    )
    runner = _runner(manager, provider, tmp_path, registry)
    monkeypatch.setattr(runner, "_new_turn_progress_writer", lambda *_args: None)
    await manager.create(KEY)

    task = asyncio.create_task(_turn(runner, manager, "run the slow thing"))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    rows = await _rows(storage)
    assistant = [(content, structured) for role, content, structured in rows if role == "assistant"]
    assert len(assistant) == 1
    content, structured = assistant[0]
    assert "[interrupted]" in content
    assert structured, "the sealed provider record was not persisted"


# ---------------------------------------------------------------------------
# The router-control replay
# ---------------------------------------------------------------------------


class _ReplayingStream:
    """Stands in for the stream consumer: the first run asks for a replay."""

    def __init__(self, journal: list[str]) -> None:
        self.journal = journal
        self.runs = 0

    async def run(self, _inp: Any) -> AsyncGenerator[AgentEvent, None]:
        self.runs += 1
        run = self.runs
        try:
            if run == 1:
                yield TextDeltaEvent(text="switching models")
                yield RouterControlReplayEvent(action="pin", target_tier="c")
                # Never reached: the harness stops reading at the replay.
                yield DoneEvent(text="superseded")
            else:
                yield TextDeltaEvent(text="replayed answer")
                yield DoneEvent(text="replayed answer")
        finally:
            self.journal.append(f"stream{run}.closed")


async def test_a_router_control_replay_ends_with_one_done_and_releases_first(
    manager: SessionManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = ScriptedProvider([[text("unused"), done()]])
    runner = _runner(manager, provider, tmp_path)
    await manager.create(KEY)
    journal: list[str] = []
    stream = _ReplayingStream(journal)
    runner._harness.stages.stream_consumer = stream  # type: ignore[assignment]
    reservations = iter(["r1", "r2", "r3"])

    def reserve(_session_key: str) -> tuple[bool, str | None, str | None]:
        reservation = next(reservations)
        journal.append(f"reserve:{reservation}")
        return False, None, reservation

    def release(reservation: str | None) -> None:
        journal.append(f"release:{reservation}")

    monkeypatch.setattr(runner, "_reserve_spend_budget", reserve)
    monkeypatch.setattr(runner, "_release_spend_budget", release)

    events = await _turn(runner, manager, "use the big model")

    dones = [event for event in events if isinstance(event, DoneEvent)]
    assert [done_event.text for done_event in dones] == ["replayed answer"]
    assert sum(isinstance(event, RouterControlReplayEvent) for event in events) == 1
    assert stream.runs == 2
    # The first run's stream is closed and its reservation released before the
    # replay reserves again; each reservation is released exactly once.
    assert journal == [
        "reserve:r1",
        "stream1.closed",
        "release:r1",
        "reserve:r2",
        "stream2.closed",
        "release:r2",
    ]


# ---------------------------------------------------------------------------
# Abandoning ``TurnRunner.run``
# ---------------------------------------------------------------------------


class _EndlessStream:
    async def run(self, _inp: Any) -> AsyncGenerator[AgentEvent, None]:
        while True:
            yield TextDeltaEvent(text="more")
            await asyncio.sleep(0)


@pytest.mark.parametrize("caller_holds_lock", [False, True])
async def test_abandoning_run_closes_the_harness_run_before_its_own_cleanup(
    manager: SessionManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caller_holds_lock: bool,
) -> None:
    # The harness's ``finally`` (budget release, progress detach) must run
    # while ``run()`` still holds the session, not later from the asyncgen
    # finalizer after the lock is released and the turn state cleared.
    import agentos.engine.runtime as runtime_module

    provider = ScriptedProvider([[text("unused"), done()]])
    runner = _runner(manager, provider, tmp_path)
    await manager.create(KEY)
    runner._harness.stages.stream_consumer = _EndlessStream()  # type: ignore[assignment]
    journal: list[str] = []
    monkeypatch.setattr(runner, "_reserve_spend_budget", lambda _key: (False, None, "r1"))
    monkeypatch.setattr(runner, "_release_spend_budget", lambda r: journal.append(f"release:{r}"))
    real_clear = runner.clear_compaction_turn_state

    def clear(session_key: str) -> None:
        journal.append("clear_turn_state")
        real_clear(session_key)

    monkeypatch.setattr(runner, "clear_compaction_turn_state", clear)
    owner = runtime_module._SESSION_LOCK_OWNER
    token = None
    if caller_holds_lock:
        # The gateway path: the caller already serializes the session.
        lock = runner.get_session_lock(KEY)
        token = owner.set({id(lock): asyncio.current_task()})  # type: ignore[dict-item]
    try:
        await manager.append_message(KEY, "user", "go")
        events = runner.run("go", KEY, tool_context=ToolContext(caller_kind=CallerKind.WEB))
        first = await events.__anext__()  # type: ignore[attr-defined]
        assert isinstance(first, TextDeltaEvent)

        await events.aclose()  # type: ignore[attr-defined]
    finally:
        if token is not None:
            owner.reset(token)

    assert journal == ["release:r1", "clear_turn_state"]


# ---------------------------------------------------------------------------
# A stopped turn's display row keeps the arguments of its unanswered calls
# ---------------------------------------------------------------------------


async def _display_tool_uses(storage: SessionStorage) -> list[dict[str, Any]]:
    import json

    async with storage.conn.execute(
        "SELECT tool_calls FROM transcript_entries WHERE role = 'assistant' ORDER BY id"
    ) as cursor:
        rows = [row[0] for row in await cursor.fetchall()]
    return [
        segment
        for raw in rows
        if raw
        for segment in json.loads(raw)
        if segment.get("type") == "tool_use"
    ]


async def test_a_call_cancelled_while_it_runs_keeps_its_arguments_on_the_card(
    storage: SessionStorage, manager: SessionManager, tmp_path: Path
) -> None:
    started = asyncio.Event()
    registry = ToolRegistry()

    async def slow(**_arguments: Any) -> str:
        started.set()
        await asyncio.sleep(30)
        return "never"

    registry.register(
        ToolSpec(
            name="slow",
            description="slow",
            parameters={"path": {"type": "string"}},
            required=["path"],
        ),
        slow,
    )
    provider = ScriptedProvider(
        [[tool_call("slow", {"path": "notes.md"}, tool_use_id="T1"), done("tool_use")]]
    )
    runner = _runner(manager, provider, tmp_path, registry)
    await manager.create(KEY)

    task = asyncio.create_task(_turn(runner, manager, "run the slow thing"))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    uses = await _display_tool_uses(storage)
    assert [(u["tool_use_id"], u["input"]) for u in uses] == [("T1", {"path": "notes.md"})]


@pytest.mark.parametrize(
    ("stop_at", "expected"),
    [
        # Between the response and the tool batch: the call is in the record.
        ("tool_calling", [("D1", {"path": "old-build"})]),
        # While the call's arguments stream in: it never became a call, and
        # the row does not show one with no arguments.
        ("tool_use_start", []),
    ],
)
async def test_a_call_stopped_before_it_ran_shows_its_arguments_or_nothing(
    storage: SessionStorage,
    manager: SessionManager,
    tmp_path: Path,
    stop_at: str,
    expected: list[tuple[str, dict[str, str]]],
) -> None:
    from agentos.engine.turn_progress import TurnStopSignal, bind_turn_stop_signal
    from agentos.engine.types import StateChangeEvent, ToolUseStartEvent

    registry = ToolRegistry()
    ran: list[str] = []

    async def delete(**_arguments: Any) -> str:
        ran.append("delete")
        return "deleted"

    registry.register(
        ToolSpec(
            name="delete",
            description="delete",
            parameters={"path": {"type": "string"}},
            required=["path"],
        ),
        delete,
    )
    provider = ScriptedProvider(
        [[tool_call("delete", {"path": "old-build"}, tool_use_id="D1"), done("tool_use")]]
    )
    runner = _runner(manager, provider, tmp_path, registry)
    await manager.create(KEY)
    await manager.append_message(KEY, "user", "clean up")
    signal = TurnStopSignal()
    bind_turn_stop_signal(signal)
    events: list[AgentEvent] = []
    ctx = ToolContext(caller_kind=CallerKind.WEB)
    async for event in runner.run(
        "clean up", KEY, tool_context=ctx, history_has_persisted_user=True
    ):
        events.append(event)
        if (stop_at == "tool_use_start" and isinstance(event, ToolUseStartEvent)) or (
            stop_at == "tool_calling"
            and isinstance(event, StateChangeEvent)
            and str(event.to_state).endswith("tool_calling")
        ):
            signal.request_abort("cancelled")

    assert isinstance(events[-1], DoneEvent)
    assert events[-1].outcome.kind == "interrupted"
    assert ran == []
    # A stopped turn's partial reply is not a delivered answer.
    assert signal.reply_delivered is False
    uses = await _display_tool_uses(storage)
    assert [(u["tool_use_id"], u["input"]) for u in uses] == expected


# ---------------------------------------------------------------------------
# A stop after the whole reply was delivered: the run completed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("grace", [0.0, 0.2])
async def test_a_stop_after_the_reply_was_persisted_ends_the_run_completed(
    storage: SessionStorage, manager: SessionManager, tmp_path: Path, grace: float
) -> None:
    from agentos.gateway.routing import RouteEnvelope, SourceKind
    from agentos.gateway.task_runtime import TaskRuntime
    from agentos.session.models import AgentTaskStatus
    from tests.test_gateway.test_task_runtime_cooperative_cancel import _Storage

    provider = ScriptedProvider([[text("the full answer"), done()]])
    runner = _runner(manager, provider, tmp_path)
    await manager.create(KEY)
    stall = _StallingCapture()
    runner._harness.stages.turn_finalizer._turn_memory_capture = stall

    async def handler(run: Any) -> None:
        await manager.append_message(KEY, "user", run.message)
        ctx = ToolContext(caller_kind=CallerKind.WEB)
        async for _event in runner.run(
            run.message, KEY, tool_context=ctx, history_has_persisted_user=True
        ):
            pass

    tasks = _Storage({})
    runtime = TaskRuntime(storage=tasks, turn_handler=handler, cancel_grace_s=grace)
    handle = await runtime.enqueue(
        RouteEnvelope(
            source_kind=SourceKind.SYSTEM, source_name="test", agent_id="main", session_key=KEY
        ),
        "question",
        run_kind="default",
    )
    await asyncio.wait_for(stall.entered.wait(), 10)
    await runtime.cancel(task_id=handle.task_id, source="user")
    await runtime.wait(handle.task_id, timeout=5)

    record = tasks.records[handle.task_id]
    assert record.status == AgentTaskStatus.SUCCEEDED
    assert record.terminal_reason == "completed"
    rows = await _rows(storage)
    assert [content for role, content, _ in rows if role == "assistant"] == ["the full answer"]


# ---------------------------------------------------------------------------
# A stop that races a short reply which finishes anyway
# ---------------------------------------------------------------------------


def _user_texts(messages: list[Message]) -> list[str]:
    """The user's own words of each user message (the runtime context the
    turn appends to the latest one is dropped)."""
    return [text.split("\n\n[Runtime context", 1)[0] for text in _texts(messages, "user") if text]


async def _stop_on_done_through_runtime(
    runner: TurnRunner, manager: SessionManager, *, grace: float
) -> tuple[Any, list[AgentEvent]]:
    """Run one turn under a real ``TaskRuntime``; stop it as its ``done`` arrives.

    The stop is sent from the consumer while the turn is between its
    DoneEvent and the finalizer's transcript append: the race of a stop
    landing on the first text chunk of a one-chunk reply.
    """
    from agentos.gateway.routing import RouteEnvelope, SourceKind
    from agentos.gateway.task_runtime import TaskRuntime
    from tests.test_gateway.test_task_runtime_cooperative_cancel import _Storage

    seen: list[AgentEvent] = []
    task_id: list[str] = []
    runtime: TaskRuntime

    async def handler(run: Any) -> None:
        await manager.append_message(KEY, "user", run.message)
        ctx = ToolContext(caller_kind=CallerKind.WEB)
        async for event in runner.run(
            run.message, KEY, tool_context=ctx, history_has_persisted_user=True
        ):
            seen.append(event)
            if isinstance(event, DoneEvent):
                await runtime.cancel(task_id=task_id[0], source="user")

    tasks = _Storage({})
    runtime = TaskRuntime(storage=tasks, turn_handler=handler, cancel_grace_s=grace)
    handle = await runtime.enqueue(
        RouteEnvelope(
            source_kind=SourceKind.SYSTEM, source_name="test", agent_id="main", session_key=KEY
        ),
        "Say hello.",
        run_kind="default",
    )
    task_id.append(handle.task_id)
    await runtime.wait(handle.task_id, timeout=5)
    return tasks.records[handle.task_id], seen


@pytest.mark.parametrize("grace", [0.0, 5.0])
async def test_a_stop_racing_a_finished_short_reply_completes_and_persists_it_once(
    storage: SessionStorage, manager: SessionManager, tmp_path: Path, grace: float
) -> None:
    from agentos.session.models import AgentTaskStatus

    provider = ScriptedProvider([[text("Hello there."), done()], [text("Still here."), done()]])
    runner = _runner(manager, provider, tmp_path)
    await manager.create(KEY)

    record, seen = await _stop_on_done_through_runtime(runner, manager, grace=grace)

    dones = [event for event in seen if isinstance(event, DoneEvent)]
    assert len(dones) == 1
    assert dones[0].outcome is not None and dones[0].outcome.kind == "completed"
    # The done frame, the task and the session all say "completed".
    assert record.status == AgentTaskStatus.SUCCEEDED
    assert record.terminal_reason == "completed"
    rows = await _rows(storage)
    assert [(role, content, structured) for role, content, structured in rows] == [
        ("user", "Say hello.", False),
        ("assistant", "Hello there.", True),
    ]

    await _turn(runner, manager, "And now?")
    request = provider.calls[-1].messages
    assert _user_texts(request) == ["Say hello.", "And now?"]
    assert _texts(request, "assistant") == ["Hello there."]


class _CommitThenStall:
    """The store committed the assistant row, then the session touch stalls.

    Stands in for ``append_message``'s second await: the row is durable, the
    await has not returned when the cancel lands.
    """

    def __init__(self, manager: SessionManager) -> None:
        self._real = manager.append_message
        self.committed = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, session_key: str, role: str, content: str, **kwargs: Any) -> Any:
        entry = await self._real(session_key, role, content, **kwargs)
        if role == "assistant" and not self.committed.is_set():
            self.committed.set()
            await self.release.wait()
        return entry


async def test_a_cancel_after_the_row_committed_mid_append_does_not_write_it_twice(
    storage: SessionStorage,
    manager: SessionManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentos.engine.turn_progress import TurnStopSignal, bind_turn_stop_signal

    provider = ScriptedProvider([[text("Hello there."), done()], [text("Still here."), done()]])
    runner = _runner(manager, provider, tmp_path)
    await manager.create(KEY)
    stall = _CommitThenStall(manager)
    monkeypatch.setattr(manager, "append_message", stall)
    signal = TurnStopSignal()

    async def first_turn() -> None:
        bind_turn_stop_signal(signal)
        await _turn(runner, manager, "Say hello.")

    task = asyncio.create_task(first_turn())
    await asyncio.wait_for(stall.committed.wait(), 10)
    task.cancel()
    await asyncio.sleep(0)
    stall.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The reply is in once, as finished, and the gateway is told it was
    # delivered (so the run is recorded completed, not cancelled).
    rows = await _rows(storage)
    assert [content for role, content, _ in rows if role == "assistant"] == ["Hello there."]
    assert signal.reply_delivered is True

    await _turn(runner, manager, "And now?")
    request = provider.calls[-1].messages
    assert _user_texts(request) == ["Say hello.", "And now?"]
    assert _texts(request, "assistant") == ["Hello there."]


async def test_a_cancel_between_done_and_the_finalizer_persists_the_finished_reply(
    storage: SessionStorage, manager: SessionManager, tmp_path: Path
) -> None:
    from agentos.engine.turn_progress import TurnStopSignal, bind_turn_stop_signal

    provider = ScriptedProvider([[text("Hello there."), done()]])
    runner = _runner(manager, provider, tmp_path)
    await manager.create(KEY)
    stage = runner._harness.stages.stream_consumer
    real_run = stage.run
    closing = asyncio.Event()

    async def stall_after_done(inp: Any) -> AsyncGenerator[AgentEvent, None]:
        # The agent stream closes after its DoneEvent; a stop lands there.
        async for event in real_run(inp):
            yield event
            if isinstance(event, DoneEvent):
                closing.set()
                await asyncio.sleep(30)

    stage.run = stall_after_done  # type: ignore[method-assign]
    signal = TurnStopSignal()

    async def first_turn() -> None:
        bind_turn_stop_signal(signal)
        await _turn(runner, manager, "Say hello.")

    task = asyncio.create_task(first_turn())
    await asyncio.wait_for(closing.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    rows = await _rows(storage)
    assert [(content, structured) for role, content, structured in rows if role == "assistant"] == [
        ("Hello there.", True)
    ]
    assert signal.reply_delivered is True


async def test_a_stop_requested_while_a_reply_finished_anyway_counts_as_delivered(
    storage: SessionStorage, manager: SessionManager, tmp_path: Path
) -> None:
    # The stop asked the turn to abort, but the reply had already ended: the
    # turn's own outcome is "completed", so the run is not a cancelled one.
    from agentos.engine.turn_progress import TurnStopSignal, bind_turn_stop_signal

    provider = ScriptedProvider([[text("Hello there."), done()]])
    runner = _runner(manager, provider, tmp_path)
    await manager.create(KEY)
    await manager.append_message(KEY, "user", "Say hello.")
    signal = TurnStopSignal()
    bind_turn_stop_signal(signal)
    events: list[AgentEvent] = []
    ctx = ToolContext(caller_kind=CallerKind.WEB)
    async for event in runner.run(
        "Say hello.", KEY, tool_context=ctx, history_has_persisted_user=True
    ):
        events.append(event)
        if isinstance(event, DoneEvent):
            signal.request_abort("cancelled")

    done_event = events[-1]
    assert isinstance(done_event, DoneEvent)
    assert done_event.outcome is not None and done_event.outcome.kind == "completed"
    assert signal.reply_delivered is True
    rows = await _rows(storage)
    assert [content for role, content, _ in rows if role == "assistant"] == ["Hello there."]
