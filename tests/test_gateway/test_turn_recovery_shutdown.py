"""A turn the gateway's own shutdown cancels stays recoverable.

A real ``TurnRunner`` inside a real ``TaskRuntime`` on a SQLite-backed
``SessionManager``: round 1 runs a tool, the second model call hangs, and the
runtime shuts down under it -- by a safe drain that times out (SIGTERM,
``gateway restart``) or at once (``restart --force``). The task must end
``abandoned/shutdown_interrupted`` and the transcript tail must still say
"interrupted after round 1", so restart recovery resumes it. A user cancel
keeps writing its ``[interrupted]`` reply. Fixtures are synthetic.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.engine.runtime import TurnRunner
from agentos.engine.turn_progress import classify_transcript_tail
from agentos.gateway.config import AgentOSRouterConfig, AttachmentsConfig, GatewayConfig
from agentos.gateway.routing import RouteEnvelope, SourceKind
from agentos.gateway.task_runtime import SHUTDOWN_INTERRUPTED_REASON, TaskRuntime
from agentos.gateway.turn_recovery import decide_recovery
from agentos.provider import DoneEvent as ProviderDone
from agentos.provider import Message
from agentos.provider import TextDeltaEvent as ProviderText
from agentos.provider import ToolUseEndEvent as ProviderToolUseEnd
from agentos.provider import ToolUseStartEvent as ProviderToolUseStart
from agentos.session.manager import SessionManager
from agentos.session.models import AgentTaskStatus
from agentos.session.storage import SessionStorage
from agentos.tools.registry import ToolRegistry, ToolSpec
from agentos.tools.types import CallerKind, ToolContext

KEY = "agent:main:webchat:direct:shutdown-recovery"


class _HangingProvider:
    """Round 1 calls ``act``; the second request streams a little, then hangs."""

    provider_name = "test"

    def __init__(self) -> None:
        self.model = "test/model"
        self.requests: list[list[Message]] = []
        self.hanging = asyncio.Event()

    def chat(self, messages: list[Message], tools=None, config=None) -> AsyncIterator[Any]:
        self.requests.append(list(messages))
        return self._stream(len(self.requests))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            yield ProviderText(text="step 1. ")
            yield ProviderToolUseStart(tool_use_id="act-1", tool_name="act")
            yield ProviderToolUseEnd(tool_use_id="act-1", tool_name="act", arguments={"n": 1})
            yield ProviderDone(stop_reason="tool_use", input_tokens=10, output_tokens=2)
            return
        yield ProviderText(text="writing the summary")
        self.hanging.set()
        await asyncio.Event().wait()  # a slow model call, cut short by the shutdown
        yield ProviderDone(stop_reason="end_turn", input_tokens=10, output_tokens=3)

    async def list_models(self) -> list[Any]:
        return []


class _Selector:
    def __init__(self, provider: _HangingProvider) -> None:
        self.provider = provider

    def clone(self) -> Any:
        provider = self.provider

        class _Clone:
            current_config = SimpleNamespace(model="test/model")

            def override_model(self, model: str) -> None:
                self.current_config = SimpleNamespace(model=model)

            def resolve(self) -> _HangingProvider:
                return provider

        return _Clone()


def _registry() -> ToolRegistry:
    registry = ToolRegistry()

    async def act(n: int) -> str:
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


async def _start_turn(tmp_path: Path):
    storage = SessionStorage(str(tmp_path / "sessions.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False, checkpoint_workspace_dir=tmp_path)
    provider = _HangingProvider()
    runner = TurnRunner(
        provider_selector=_Selector(provider),
        tool_registry=_registry(),
        session_manager=manager,
        config=GatewayConfig(
            attachments=AttachmentsConfig(media_root=str(tmp_path / "media")),
            agentos_router=AgentOSRouterConfig(enabled=False),
        ),
    )

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
    await manager.append_message(KEY, "user", "Act, then summarize.")
    envelope = RouteEnvelope(
        source_kind=SourceKind.WEB, source_name="test", agent_id="main", session_key=KEY
    )
    handle = await runtime.enqueue(envelope, "Act, then summarize.")
    await asyncio.wait_for(provider.hanging.wait(), timeout=10)
    return storage, manager, runtime, handle


@pytest.mark.parametrize("mode", ["safe_drain", "force"])
async def test_shutdown_mid_turn_leaves_a_resumable_turn(tmp_path: Path, mode: str) -> None:
    storage, manager, runtime, handle = await _start_turn(tmp_path)
    try:
        if mode == "safe_drain":
            # The drain deadline passes with the model call still running.
            await runtime.shutdown(graceful=True, graceful_timeout=0.2, timeout=2.0)
        else:
            await runtime.shutdown(cancel=True, timeout=2.0)

        record = await storage.get_agent_task(handle.task_id)
        assert record is not None
        assert (record.status, record.terminal_reason) == (
            AgentTaskStatus.ABANDONED,
            SHUTDOWN_INTERRUPTED_REASON,
        )
        rows = await manager.get_transcript(KEY)
        assert not any("[interrupted]" in str(row.content or "") for row in rows)
        tail = classify_transcript_tail(rows)
        assert (tail.status, tail.rounds_completed) == ("interrupted", 1)
        assert decide_recovery(tail) == ("resume", "round_complete")
    finally:
        await storage.close()


async def test_user_cancel_still_writes_the_interrupted_reply(tmp_path: Path) -> None:
    storage, manager, runtime, handle = await _start_turn(tmp_path)
    try:
        await runtime.cancel(task_id=handle.task_id, source="user", reason="stop")
        record = await runtime.wait(handle.task_id, timeout=5)
        assert record.status == AgentTaskStatus.CANCELLED
        rows = await manager.get_transcript(KEY)
        assert str(rows[-1].content).endswith("[interrupted]")
        assert classify_transcript_tail(rows).status == "finished"
    finally:
        await runtime.shutdown(cancel=True, timeout=1.0)
        await storage.close()


async def test_user_cancel_during_the_drain_is_still_a_cancel(tmp_path: Path) -> None:
    storage, manager, runtime, handle = await _start_turn(tmp_path)
    try:
        drain = asyncio.ensure_future(runtime.shutdown(graceful=True, graceful_timeout=5.0))
        await asyncio.sleep(0.05)
        await runtime.cancel(task_id=handle.task_id, source="user", reason="stop")
        await asyncio.wait_for(drain, timeout=5)
        record = await storage.get_agent_task(handle.task_id)
        assert record is not None and record.status == AgentTaskStatus.CANCELLED
        rows = await manager.get_transcript(KEY)
        assert str(rows[-1].content).endswith("[interrupted]")
    finally:
        await storage.close()
