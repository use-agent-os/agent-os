"""Restart recovery after a crash in a turn that a message was steered into.

A real ``TurnRunner`` inside a real ``TaskRuntime`` on a SQLite-backed
``SessionManager``. Round 1 runs a tool; while it runs the user sends a
``steer`` message; the next model call hangs and the process "dies" (the
database is copied as it is on disk). The restarted gateway runs
``TurnRecovery`` with the gateway's own task handler and a fresh model:

* the steer was taken -- its task ended ``succeeded/steered`` after the
  interrupted turn started. It joined that turn and must not count as newer
  work: the turn is resumed and the model sees the steered message once,
  after the tool result it arrived during.
* the steer was still waiting -- its text lived only in memory and on its
  task's ledger row. It must not vanish: it is written after the interrupted
  turn and answered by the resumed one.

Fixtures are synthetic.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.engine.runtime import TurnRunner
from agentos.gateway.boot import dispatch_task_runtime_turn
from agentos.gateway.config import AgentOSRouterConfig, AttachmentsConfig, GatewayConfig
from agentos.gateway.routing import RouteEnvelope, SourceKind
from agentos.gateway.task_runtime import DEFERRED_USER_TEXT_KEY, TaskRuntime
from agentos.gateway.turn_recovery import RECOVERY_KEY, TurnRecovery
from agentos.provider import ContentBlockText, ContentBlockToolResult, Message
from agentos.provider import DoneEvent as ProviderDone
from agentos.provider import TextDeltaEvent as ProviderText
from agentos.provider import ToolUseEndEvent as ProviderToolUseEnd
from agentos.provider import ToolUseStartEvent as ProviderToolUseStart
from agentos.session.manager import SessionManager
from agentos.session.models import AgentTaskStatus
from agentos.session.storage import SessionStorage
from agentos.tools.registry import ToolRegistry, ToolSpec
from agentos.tools.types import CallerKind, ToolContext

KEY = "agent:main:webchat:direct:steer-recovery"
STEER = "use the blue one"


class _Provider:
    """Before the crash: ``act`` in round 1, then a model call that hangs.

    After the restart (``hang=False``): answers in text.
    """

    provider_name = "test"

    def __init__(self, *, hang: bool) -> None:
        self.model = "test/model"
        self.hang = hang
        self.requests: list[list[Message]] = []
        self.hanging = asyncio.Event()

    def chat(self, messages: list[Message], tools=None, config=None) -> AsyncIterator[Any]:
        self.requests.append(list(messages))
        return self._stream(len(self.requests))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if not self.hang:
            yield ProviderText(text="Picked it back up.")
            yield ProviderDone(stop_reason="end_turn", input_tokens=10, output_tokens=3)
            return
        if call_number == 1:
            yield ProviderText(text="step 1. ")
            yield ProviderToolUseStart(tool_use_id="act-1", tool_name="act")
            yield ProviderToolUseEnd(tool_use_id="act-1", tool_name="act", arguments={"n": 1})
            yield ProviderDone(stop_reason="tool_use", input_tokens=10, output_tokens=2)
            return
        yield ProviderText(text="switching")
        self.hanging.set()
        await asyncio.Event().wait()  # the process dies during this call
        yield ProviderDone(stop_reason="end_turn", input_tokens=10, output_tokens=3)

    async def list_models(self) -> list[Any]:
        return []


class _Selector:
    def __init__(self, provider: _Provider) -> None:
        self.provider = provider

    def clone(self) -> Any:
        provider = self.provider

        class _Clone:
            current_config = SimpleNamespace(model="test/model")

            def override_model(self, model: str) -> None:
                self.current_config = SimpleNamespace(model=model)

            def resolve(self) -> _Provider:
                return provider

        return _Clone()


def _registry(act_started: asyncio.Event, act_release: asyncio.Event) -> ToolRegistry:
    registry = ToolRegistry()

    async def act(n: int) -> str:
        act_started.set()
        await act_release.wait()
        return f"acted {n}"

    registry.register(
        ToolSpec(
            name="act",
            description="Do something with side effects",
            parameters={
                "type": "object",
                "properties": {"n": {"type": "integer"}},
                "required": ["n"],
            },
        ),
        act,
    )
    return registry


def _runner(provider: _Provider, manager: SessionManager, tmp_path: Path, registry: ToolRegistry):
    return TurnRunner(
        provider_selector=_Selector(provider),
        tool_registry=registry,
        session_manager=manager,
        config=GatewayConfig(
            attachments=AttachmentsConfig(media_root=str(tmp_path / "media")),
            agentos_router=AgentOSRouterConfig(enabled=False),
        ),
    )


def _envelope() -> RouteEnvelope:
    return RouteEnvelope(
        source_kind=SourceKind.WEB, source_name="test", agent_id="main", session_key=KEY
    )


async def _open(db: Path, tmp_path: Path) -> tuple[SessionStorage, SessionManager]:
    storage = SessionStorage(str(db))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False, checkpoint_workspace_dir=tmp_path)
    return storage, manager


def _snapshot(db: Path, target: Path) -> None:
    source = sqlite3.connect(str(db))
    dest = sqlite3.connect(str(target))
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()


async def _crash(tmp_path: Path, *, taken: bool) -> tuple[Path, str, str]:
    """Run the turn, steer into it, and copy the database mid-call.

    ``taken``: the steer is sent while round 1's tool runs (the turn takes
    it); otherwise it is sent during the hanging model call (nothing takes
    it). Returns the snapshot path and the turn's and the steer's task ids.
    """
    db = tmp_path / "live.db"
    storage, manager = await _open(db, tmp_path)
    provider = _Provider(hang=True)
    act_started, act_release = asyncio.Event(), asyncio.Event()
    runner = _runner(provider, manager, tmp_path, _registry(act_started, act_release))

    async def _handler(run: Any) -> None:
        async for _event in runner.run(
            run.message,
            run.session_key,
            tool_context=ToolContext(caller_kind=CallerKind.WEB, workspace_dir=str(tmp_path)),
            history_has_persisted_user=True,
            no_memory_capture=True,
        ):
            pass

    runtime = TaskRuntime(storage=storage, turn_handler=_handler)
    await manager.create(KEY)
    await manager.append_message(KEY, "user", "Paint it red, then report.")
    turn = await runtime.enqueue(_envelope(), "Paint it red, then report.")

    async def _write_steer() -> tuple[str, str | None]:
        entry = await manager.append_message(KEY, "user", STEER)
        return STEER, entry.message_id

    async def _send_steer() -> str:
        handle = await runtime.enqueue(
            _envelope(), STEER, mode="steer", deferred_input=_write_steer
        )
        return handle.task_id

    try:
        if taken:
            await asyncio.wait_for(act_started.wait(), timeout=10)
            steer_id = await _send_steer()
            act_release.set()
            await asyncio.wait_for(provider.hanging.wait(), timeout=10)
            steer_record = await storage.get_agent_task(steer_id)
            assert steer_record is not None
            assert (steer_record.status, steer_record.terminal_reason) == (
                AgentTaskStatus.SUCCEEDED,
                "steered",
            )
            assert steer_record.details["steered_into"] == turn.task_id
            assert DEFERRED_USER_TEXT_KEY not in steer_record.details
        else:
            act_release.set()
            await asyncio.wait_for(provider.hanging.wait(), timeout=10)
            steer_id = await _send_steer()
            steer_record = await storage.get_agent_task(steer_id)
            assert steer_record is not None and steer_record.status == AgentTaskStatus.QUEUED
            assert steer_record.details[DEFERRED_USER_TEXT_KEY] == STEER

        snapshot = tmp_path / "after-kill.db"
        _snapshot(db, snapshot)
    finally:
        act_release.set()
        await runtime.shutdown(cancel=True, timeout=2.0)
        await storage.close()
    return snapshot, turn.task_id, steer_id


async def _restart_and_recover(tmp_path: Path, snapshot: Path):
    """Boot on the snapshot and let recovery resume what it finds."""
    storage, manager = await _open(snapshot, tmp_path)
    provider = _Provider(hang=False)
    runner = _runner(provider, manager, tmp_path, ToolRegistry())
    config = GatewayConfig(
        workspace_dir=str(tmp_path / "ws"),
        agent_stream_heartbeat_interval_seconds=0.0,
        agent_stream_idle_timeout_seconds=5.0,
        attachments=AttachmentsConfig(media_root=str(tmp_path / "media")),
        agentos_router=AgentOSRouterConfig(enabled=False),
    )

    async def _no_emit(session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        return None

    async def _handler(run: Any) -> None:
        await dispatch_task_runtime_turn(
            run,
            config=config,
            session_manager=manager,
            turn_runner=runner,
            event_emitter=_no_emit,
        )

    runtime = TaskRuntime(storage=storage, turn_handler=_handler)
    recovery = TurnRecovery(
        storage=storage, session_manager=manager, task_runtime=runtime, backoff_s=(0.0,)
    )
    await recovery.scan()
    await recovery.wait_idle(timeout=10)
    return storage, manager, runtime, provider, recovery.recoveries()


def _texts(messages: list[Message]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for message in messages:
        if isinstance(message.content, str):
            out.append((message.role, message.content))
            continue
        for block in message.content:
            if isinstance(block, ContentBlockText):
                out.append((message.role, block.text))
            elif isinstance(block, ContentBlockToolResult):
                out.append((message.role, f"tool_result:{block.tool_use_id}"))
    return out


@pytest.mark.asyncio
async def test_crash_after_a_taken_steer_resumes_the_turn_it_joined(tmp_path: Path) -> None:
    snapshot, turn_id, _steer_id = await _crash(tmp_path, taken=True)
    storage, manager, runtime, provider, found = await _restart_and_recover(tmp_path, snapshot)
    try:
        assert [item["run_id"] for item in found] == [turn_id]
        origin = await storage.get_agent_task(turn_id)
        assert origin is not None
        assert origin.details[RECOVERY_KEY]["state"] == "resumed"

        # The resumed turn's model call: the steered message once, right
        # after the tool result it arrived during.
        assert len(provider.requests) == 1
        sent = [t for t in _texts(provider.requests[0]) if not t[1].startswith("Current local")]
        steers = [index for index, (_, text) in enumerate(sent) if STEER in text]
        assert len(steers) == 1
        assert sent[steers[0] - 1] == ("user", "tool_result:act-1")
        assert "[Restart recovery]" in sent[-1][1]

        rows = await manager.get_transcript(KEY)
        assert [row.content for row in rows if row.role == "user"].count(STEER) == 1
    finally:
        await runtime.shutdown(cancel=True, timeout=1.0)
        await storage.close()


@pytest.mark.asyncio
async def test_crash_with_an_untaken_steer_writes_and_answers_it(tmp_path: Path) -> None:
    snapshot, turn_id, steer_id = await _crash(tmp_path, taken=False)
    storage, manager, runtime, provider, found = await _restart_and_recover(tmp_path, snapshot)
    try:
        assert len(found) == 1
        assert found[0]["state"] == "resumed"

        rows = await manager.get_transcript(KEY)
        users = [row.content for row in rows if row.role == "user"]
        assert users.count(STEER) == 1
        steer_record = await storage.get_agent_task(steer_id)
        assert steer_record is not None
        assert DEFERRED_USER_TEXT_KEY not in steer_record.details
        assert steer_record.details["persisted_user_message_id"]

        # The resumed turn sees the message after the interrupted turn's work.
        sent = [t for t in _texts(provider.requests[0]) if not t[1].startswith("Current local")]
        steers = [index for index, (_, text) in enumerate(sent) if STEER in text]
        assert len(steers) == 1
        assert ("user", "tool_result:act-1") in sent[: steers[0]]
        assert "[Restart recovery]" in sent[-1][1]
        assert turn_id != steer_id

        # A second boot does not write it again.
        recovery = TurnRecovery(storage=storage, session_manager=manager, task_runtime=runtime)
        await recovery.scan()
        rows = await manager.get_transcript(KEY)
        assert [row.content for row in rows if row.role == "user"].count(STEER) == 1
    finally:
        await runtime.shutdown(cancel=True, timeout=1.0)
        await storage.close()
