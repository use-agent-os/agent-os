"""Proactive recall through the real entry points that start a turn.

The policy in :func:`agentos.memory.active_recall.recall_skip_reason` keys on
``run_kind``, provenance and the prompt mode. Unit tests that pass those by
hand cannot notice when production labels a turn differently -- which is how
``sessions.send`` (``run_kind="session_turn"``, the WebChat and ``agentos
chat`` path) once skipped recall on every message. Here each turn is started
the way the gateway starts it -- the ``sessions.send`` RPC, the
``sessions_send`` / ``sessions_spawn`` tools, the cron ``agent_run`` handler,
the heartbeat service -- on a real ``TaskRuntime`` driving a real
``TurnRunner`` over SQLite sessions and a real FTS memory index. Only the
model is a stub, and it records what the provider was sent.

Fixtures are synthetic.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentos import run_kinds
from agentos.engine.runtime import TurnRunner
from agentos.gateway.boot import dispatch_task_runtime_turn
from agentos.gateway.config import (
    AgentOSRouterConfig,
    AttachmentsConfig,
    GatewayConfig,
    MemoryConfig,
    MemoryRecallConfig,
)
from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.gateway.task_runtime import TERMINAL_STATUSES, TaskRuntime
from agentos.memory.retrieval import MemoryRetriever
from agentos.memory.store import LongTermMemoryStore
from agentos.memory.sync_manager import MemorySyncManager
from agentos.provider import DoneEvent as ProviderDone
from agentos.provider import Message
from agentos.provider import TextDeltaEvent as ProviderText
from agentos.scheduler.delivery import DeliveryChain
from agentos.scheduler.handlers import make_agent_run_handler
from agentos.scheduler.heartbeat_service import HeartbeatService
from agentos.scheduler.payloads import AGENT_TURN_KIND
from agentos.scheduler.types import CronJob, SessionTarget
from agentos.session.manager import SessionManager
from agentos.session.models import AgentTaskStatus
from agentos.session.storage import SessionStorage
from agentos.tools.builtin import sessions as sessions_tools
from agentos.tools.registry import ToolRegistry
from agentos.tools.types import CallerKind, ToolContext, current_tool_context

KEY = "agent:main:webchat:direct:recall-e2e"
QUESTION = "Which billing database are we using, Postgres or MySQL?"
NOTES = {
    "decisions.md": (
        "# Decisions\n\n"
        "## Billing database\n"
        "On 2026-10-02 we decided to use Postgres instead of MySQL for the billing "
        "service, mainly for JSONB support and row-level security.\n"
    ),
}


class _Provider:
    """Answers every call in text and keeps what it was sent."""

    provider_name = "test"

    def __init__(self) -> None:
        self.model = "test/model"
        self.systems: list[str] = []
        self.requests: list[list[Message]] = []

    def chat(self, messages: list[Message], tools=None, config=None) -> AsyncIterator[Any]:
        self.requests.append(list(messages))
        self.systems.append(str(getattr(config, "system", None) or ""))
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderText(text="ok")
        yield ProviderDone(stop_reason="end_turn", input_tokens=10, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []

    def saw_recalled_memory(self) -> bool:
        sent = "\n".join(self.systems) + "\n".join(
            str(message.content) for request in self.requests for message in request
        )
        return "## Recalled Memory" in sent and "Postgres" in sent


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


class _Gateway:
    """The pieces ``build_services`` wires for a turn, minus the network."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.provider = _Provider()
        #: ``(session_key, run_kind, skip reason)`` per recall decision.
        self.decisions: list[tuple[str, str, str | None]] = []

    async def start(self) -> None:
        workspace = self.tmp_path / "workspace"
        memory_dir = workspace / "memory"
        memory_dir.mkdir(parents=True)
        for name, body in NOTES.items():
            (memory_dir / name).write_text(body, encoding="utf-8")
        self.store = LongTermMemoryStore(self.tmp_path / "memory.db")
        await self.store.initialize()
        sync = MemorySyncManager(store=self.store, workspace_dir=workspace, memory_dir=memory_dir)
        await sync.sync("test", force=True)
        retriever = MemoryRetriever(self.store, sync_manager=sync)

        self.storage = SessionStorage(str(self.tmp_path / "sessions.db"))
        await self.storage.connect()
        self.manager = SessionManager(
            self.storage, inject_time_prefix=False, checkpoint_workspace_dir=self.tmp_path
        )
        self.config = GatewayConfig(
            workspace_dir=str(workspace),
            attachments=AttachmentsConfig(media_root=str(self.tmp_path / "media")),
            agentos_router=AgentOSRouterConfig(enabled=False),
            # The trigger lane alone: no deep-lane model call to stub.
            memory=MemoryConfig(recall=MemoryRecallConfig(mode="off")),
        )
        self.runner = TurnRunner(
            provider_selector=_Selector(self.provider),
            tool_registry=ToolRegistry(),
            session_manager=self.manager,
            config=self.config,
            memory_retrievers={"main": retriever},
        )
        decide = self.runner._active_recall_skip_reason

        def _record(**kwargs: Any) -> str | None:
            reason = decide(**kwargs)
            self.decisions.append((kwargs["session_key"], kwargs["run_kind"], reason))
            return reason

        self.runner._active_recall_skip_reason = _record  # type: ignore[method-assign]

        async def _no_emit(*_args: Any, **_kwargs: Any) -> None:
            return None

        async def _handler(run: Any) -> None:
            await dispatch_task_runtime_turn(
                run,
                config=self.config,
                session_manager=self.manager,
                turn_runner=self.runner,
                event_emitter=_no_emit,
            )

        self.runtime = TaskRuntime(storage=self.storage, turn_handler=_handler)
        await self.manager.create(KEY)

    async def close(self) -> None:
        await self.runtime.shutdown()
        await self.storage.close()
        await self.store.close()

    def rpc_context(self) -> RpcContext:
        ctx = RpcContext(conn_id="conn-1", config=self.config)
        ctx.session_manager = self.manager
        ctx.task_runtime = self.runtime
        ctx.turn_runner = self.runner
        return ctx

    async def sessions_send(self, params: dict[str, Any]) -> None:
        res = await get_dispatcher().dispatch(
            "r1", "sessions.send", {"key": KEY, "message": QUESTION, **params}, self.rpc_context()
        )
        assert res.ok, res.error
        await self.wait_all()

    async def wait_all(self) -> None:
        for _ in range(200):
            rows = await self.storage.list_agent_tasks(limit=50)
            if rows and all(row.status in TERMINAL_STATUSES for row in rows):
                assert {row.status for row in rows} == {AgentTaskStatus.SUCCEEDED}
                return
            await asyncio.sleep(0.02)
        raise AssertionError("turns did not finish")

    def decision_for(self, session_key: str) -> tuple[str, str | None]:
        matches = [(kind, reason) for key, kind, reason in self.decisions if key == session_key]
        assert matches, f"no recall decision for {session_key}: {self.decisions}"
        return matches[-1]


@pytest.fixture
async def gateway(tmp_path: Path) -> AsyncIterator[_Gateway]:
    gw = _Gateway(tmp_path)
    await gw.start()
    try:
        yield gw
    finally:
        await gw.close()


@pytest.fixture
def tool_wiring(gateway: _Gateway) -> Iterator[None]:
    """What the gateway's boot hands the session tools."""
    sessions_tools.set_session_manager(gateway.manager)
    sessions_tools.set_task_runtime(gateway.runtime)
    sessions_tools.set_gateway_config(gateway.config)
    token = current_tool_context.set(
        ToolContext(
            caller_kind=CallerKind.WEB,
            session_key=KEY,
            agent_id="main",
            workspace_dir=str(gateway.tmp_path / "workspace"),
        )
    )
    try:
        yield
    finally:
        current_tool_context.reset(token)
        sessions_tools.set_session_manager(None)
        sessions_tools.set_task_runtime(None)
        sessions_tools.set_gateway_config(None)


# -- a person's message: recalled ------------------------------------------------


async def test_webchat_sessions_send_recalls_memory(gateway: _Gateway) -> None:
    await gateway.sessions_send({})

    assert gateway.decision_for(KEY) == (run_kinds.SESSION_TURN, None)
    assert gateway.provider.saw_recalled_memory()


async def test_cli_chat_sessions_send_recalls_memory(gateway: _Gateway) -> None:
    """``agentos chat`` against a gateway: the CLI route on ``sessions.send``."""
    await gateway.sessions_send(
        {"_source": {"caller_kind": "cli", "channel_kind": "cli", "source_name": "chat"}}
    )

    assert gateway.decision_for(KEY) == (run_kinds.SESSION_TURN, None)
    assert gateway.provider.saw_recalled_memory()


# -- machine-started or delivered turns: skipped --------------------------------


async def test_sessions_send_marked_inter_session_is_skipped(gateway: _Gateway) -> None:
    await gateway.sessions_send({"inputProvenanceKind": "inter_session"})

    assert gateway.decision_for(KEY) == (run_kinds.SESSION_TURN, "provenance:inter_session")
    assert not gateway.provider.saw_recalled_memory()


@pytest.mark.usefixtures("tool_wiring")
async def test_inter_session_send_tool_is_skipped(gateway: _Gateway) -> None:
    await sessions_tools.sessions_send(KEY, QUESTION)
    await gateway.wait_all()

    assert gateway.decision_for(KEY) == (run_kinds.DEFAULT, "provenance:inter_session")
    assert not gateway.provider.saw_recalled_memory()


@pytest.mark.usefixtures("tool_wiring")
async def test_subagent_spawn_is_skipped(gateway: _Gateway) -> None:
    spawned = json.loads(await sessions_tools.sessions_spawn(task=QUESTION))
    await gateway.wait_all()

    assert gateway.decision_for(spawned["session_key"]) == (
        run_kinds.SUBAGENT,
        f"run_kind:{run_kinds.SUBAGENT}",
    )
    assert not gateway.provider.saw_recalled_memory()


async def test_cron_job_on_the_users_session_is_skipped(gateway: _Gateway) -> None:
    """A ``current``-session cron job runs on the person's own session key."""
    job = CronJob(
        id="billing-check",
        name="Billing check",
        handler_key="agent_run",
        payload={"kind": AGENT_TURN_KIND, "task": QUESTION, "agent_id": "main"},
        session_target=SessionTarget.CURRENT,
        session_key=KEY,
        origin_session_key=KEY,
    )
    handler = make_agent_run_handler(
        DeliveryChain(),
        session_manager_ref=lambda: gateway.manager,
        task_runtime_ref=lambda: gateway.runtime,
    )

    await handler(job)

    assert gateway.decision_for(KEY) == (run_kinds.CRON_TURN, f"run_kind:{run_kinds.CRON_TURN}")
    assert not gateway.provider.saw_recalled_memory()


async def test_heartbeat_tick_is_skipped(gateway: _Gateway) -> None:
    service = HeartbeatService(
        turn_runner=gateway.runner,
        session_storage=gateway.storage,
        channel_manager_ref=lambda: None,
    )

    await service.run_once(
        reason="interval",
        agent_id="main",
        session_key=KEY,
        prompt=QUESTION,
        target="none",
        tool_context=ToolContext(caller_kind=CallerKind.CRON, session_key=KEY),
    )

    run_kind, reason = gateway.decision_for(KEY)
    assert run_kind == run_kinds.HEARTBEAT
    assert reason is not None
    assert not gateway.provider.saw_recalled_memory()
