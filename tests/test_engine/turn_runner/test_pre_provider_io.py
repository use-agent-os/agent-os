"""Pre-provider concurrency: ``PreProviderIO`` and the provider-recall deadline.

Unit level: the reads start together in concurrent mode, keep their old order
in serial mode, and are applied in the same order either way. ``RecallBudget``
turns a slow recall into "no recall this turn" without cancelling it.

Turn level: a real ``TurnRunner`` + ``SessionManager`` drives three turns with
project knowledge, an external memory provider, a memory sync manager and the
router context read all active, once with ``concurrent_turn_setup`` on and once
off, and the provider must receive byte-identical requests.

All waits are event driven; the only real timeouts are a few milliseconds.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any

import pytest
import structlog

import agentos.engine.agent as agent_module
from agentos.engine.runtime import TurnRunner
from agentos.engine.turn_runner.agent_bootstrap_stage import (
    AgentBootstrapStageInput,
)
from agentos.engine.turn_runner.pre_provider_io import (
    MEMORY_CONTEXT_KEY,
    PROJECT_KNOWLEDGE_KEY,
    PreProviderIO,
    RecallBudget,
)
from agentos.gateway.config import (
    AgentOSRouterConfig,
    AttachmentsConfig,
    GatewayConfig,
    MemoryConfig,
    TaskRuntimeConfig,
)
from agentos.memory.providers.base import MemoryProvider
from agentos.memory.providers.manager import MemoryProviderManager
from agentos.provider import DoneEvent as ProviderDone
from agentos.provider import Message
from agentos.provider import TextDeltaEvent as ProviderText
from agentos.session.manager import SessionManager
from agentos.session.storage import SessionStorage
from agentos.tools.types import CallerKind, ToolContext

# ---------------------------------------------------------------------------
# RecallBudget
# ---------------------------------------------------------------------------


async def test_recall_in_time_returns_the_block() -> None:
    budget = RecallBudget()

    async def fetch() -> str:
        return "fresh"

    assert await budget.recall("k", fetch, timeout=1.0) == "fresh"


async def test_late_recall_is_not_cancelled_and_stands_in_next_turn() -> None:
    budget = RecallBudget()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def slow() -> str:
        await release.wait()
        finished.set()
        return "late block"

    with structlog.testing.capture_logs() as logs:
        assert await budget.recall("k", slow, timeout=0.005) == ""
    assert [e["event"] for e in logs].count("memory.recall_timeout") == 1

    # The recall keeps running after the turn gave up on it.
    release.set()
    await asyncio.wait_for(finished.wait(), timeout=1.0)
    await asyncio.sleep(0)

    # Next turn: its own recall is late too, so the earlier block stands in.
    never = asyncio.Event()

    async def stuck() -> str:
        await never.wait()
        return "never"

    assert await budget.recall("k", stuck, timeout=0.005) == "late block"
    never.set()


async def test_fresh_recall_beats_the_late_one() -> None:
    budget = RecallBudget()
    release = asyncio.Event()

    async def slow() -> str:
        await release.wait()
        return "late block"

    assert await budget.recall("k", slow, timeout=0.005) == ""
    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    async def fast() -> str:
        return "fresh block"

    assert await budget.recall("k", fast, timeout=1.0) == "fresh block"
    # The stale block was dropped, not kept for later.
    never = asyncio.Event()

    async def stuck() -> str:
        await never.wait()
        return ""

    assert await budget.recall("k", stuck, timeout=0.005) == ""
    never.set()


async def test_running_recall_is_reused_instead_of_starting_another() -> None:
    budget = RecallBudget()
    release = asyncio.Event()
    calls: list[str] = []

    async def slow() -> str:
        calls.append("slow")
        await release.wait()
        return "block"

    async def other() -> str:
        calls.append("other")
        return "other"

    assert await budget.recall("k", slow, timeout=0.005) == ""
    waiter = asyncio.create_task(budget.recall("k", other, timeout=1.0))
    await asyncio.sleep(0)
    release.set()
    assert await waiter == "block"
    assert calls == ["slow"]

    # That turn consumed the block, so it is not offered again as stale.
    never = asyncio.Event()

    async def stuck() -> str:
        await never.wait()
        return ""

    assert await budget.recall("k", stuck, timeout=0.005) == ""
    never.set()


async def test_unscoped_recall_shares_nothing_between_calls() -> None:
    budget = RecallBudget()
    release = asyncio.Event()

    async def slow() -> str:
        await release.wait()
        return "someone else's block"

    assert await budget.recall(None, slow, timeout=0.005) == ""
    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    never = asyncio.Event()

    async def stuck() -> str:
        await never.wait()
        return ""

    assert await budget.recall(None, stuck, timeout=0.005) == ""
    never.set()


async def test_recall_failure_injects_nothing() -> None:
    budget = RecallBudget()

    async def boom() -> str:
        raise RuntimeError("provider down")

    assert await budget.recall("k", boom, timeout=1.0) == ""


# ---------------------------------------------------------------------------
# PreProviderIO
# ---------------------------------------------------------------------------


def _recording_io(*, concurrent: bool, started: list[str], gate: asyncio.Event | None = None):
    async def piece(name: str, value: Any) -> Any:
        started.append(name)
        if gate is not None:
            await gate.wait()
        return value

    return PreProviderIO(
        concurrent=concurrent,
        recall=lambda: piece("recall", "RECALL"),
        project_knowledge=lambda: piece("project_knowledge", "KNOWLEDGE"),
        hydrate_tools=lambda: piece("hydrate_tools", ["t"]),
        router_context=lambda: piece("router_context", {"prev_assistant_text": "x"}),
        memory_warm=lambda: piece("memory_warm", None),
    )


async def test_concurrent_mode_starts_every_read_before_any_is_awaited() -> None:
    started: list[str] = []
    gate = asyncio.Event()
    io = _recording_io(concurrent=True, started=started, gate=gate)
    await asyncio.sleep(0)
    assert sorted(started) == [
        "hydrate_tools",
        "memory_warm",
        "project_knowledge",
        "recall",
        "router_context",
    ]
    gate.set()
    extra = await io.extra_prompt_context({"Base": "b"})
    assert list(extra or {}) == ["Base", MEMORY_CONTEXT_KEY, PROJECT_KNOWLEDGE_KEY]
    await io.hydrate_tools()
    router = io.router_context()
    assert router is not None
    assert await router == {"prev_assistant_text": "x"}


async def test_serial_mode_keeps_the_old_order_and_fetches_router_context_in_stage() -> None:
    started: list[str] = []
    io = _recording_io(concurrent=False, started=started)
    await asyncio.sleep(0)
    assert started == []
    await io.extra_prompt_context(None)
    await io.hydrate_tools()
    assert started == ["recall", "project_knowledge", "hydrate_tools"]
    assert io.router_context() is None
    assert io.memory_warm() is None
    assert "memory_warm" not in started


async def test_extra_context_is_identical_in_both_modes() -> None:
    results = []
    for concurrent in (True, False):
        io = _recording_io(concurrent=concurrent, started=[])
        results.append(await io.extra_prompt_context({"Internal Event Mode": "m"}))
    assert results[0] == results[1]
    assert list(results[0] or {}) == list(results[1] or {})


async def test_empty_blocks_leave_the_context_object_untouched() -> None:
    async def empty() -> str:
        return ""

    async def none() -> None:
        return None

    for concurrent in (True, False):
        io = PreProviderIO(
            concurrent=concurrent,
            recall=empty,
            project_knowledge=none,
            hydrate_tools=none,
        )
        base = {"Base": "b"}
        assert await io.extra_prompt_context(base) is base
        await io.hydrate_tools()


# ---------------------------------------------------------------------------
# AgentBootstrapStage: an early memory warm is awaited, not repeated
# ---------------------------------------------------------------------------


async def test_bootstrap_awaits_the_early_warm_and_skips_its_own() -> None:
    from agentos.engine.turn_runner.agent_bootstrap_stage import (
        AgentBootstrapStage,
        _AgentConfigAuxiliaries,
        _MemorySnapshotResult,
        _ResolvedBudgets,
        _ResolvedCatalog,
    )

    order: list[str] = []
    warm_done = asyncio.Event()

    async def early_warm() -> None:
        await asyncio.sleep(0)
        order.append("early_warm")
        warm_done.set()

    class _Memory:
        async def warm_and_capture(self, *, agent_id, session_key, warmed=False):
            order.append(f"capture(warmed={warmed})")
            return _MemorySnapshotResult(sync_manager=None, private_memory_allowed=True)

    class _Budgets:
        def resolve_budgets(self, **_kw):
            return _ResolvedBudgets(60.0, 10, "default", 30.0, 30.0, 30.0, 1)

    class _Catalog:
        def lookup(self, model_id):
            return _ResolvedCatalog(1024, 8192, None)

    class _Aux:
        def build_auxiliaries(self, **_kw):
            return _AgentConfigAuxiliaries(False, "", "", 0, 0, 0, 0)

    class _Factory:
        def build_agent(self, **kwargs):
            return object()

    class _Turn:
        metadata: dict[str, Any] = {}
        tool_defs: list[Any] = []

    stage = AgentBootstrapStage(
        timeout_budget=_Budgets(),
        model_catalog=_Catalog(),
        agent_config_builder=_Aux(),
        memory_snapshot=_Memory(),
        agent_factory=_Factory(),
    )
    task = asyncio.create_task(early_warm())
    await stage.run(
        AgentBootstrapStageInput(
            provider=None,
            cloned_selector=None,
            turn=_Turn(),
            final_prompt="p",
            cache_breakpoints=None,
            request_context_prompt=None,
            resolved_model="m",
            session_id_for_log=None,
            tool_handler=None,
            turn_call_logger=None,
            tool_context=None,
            session_key="agent:main:s",
            agent_id="main",
            timeout=None,
            max_iterations=None,
            iteration_timeout=None,
            tool_timeout=None,
            request_timeout=None,
            max_provider_retries=None,
            length_capped_continuations=None,
            pending_memory_warm=task,
        )
    )
    assert order == ["early_warm", "capture(warmed=True)"]


# ---------------------------------------------------------------------------
# Whole turns: concurrent and serial setup send byte-identical requests
# ---------------------------------------------------------------------------

KEY = "agent:main:webchat:preprovider"


class _RecordingProvider:
    provider_name = "test"

    def __init__(self) -> None:
        self.model = "test/model"
        self.requests: list[dict[str, Any]] = []

    def chat(self, messages: list[Message], tools=None, config=None) -> AsyncIterator[Any]:
        self.requests.append(
            {
                "system": getattr(config, "system", None),
                "tools": [tool.model_dump(mode="json") for tool in tools or []],
                "messages": [
                    json.dumps(message.model_dump(mode="json"), sort_keys=True)
                    for message in messages
                ],
            }
        )
        return self._stream(len(self.requests))

    async def _stream(self, n: int) -> AsyncIterator[Any]:
        yield ProviderText(text=f"answer {n}")
        yield ProviderDone(stop_reason="end_turn", input_tokens=10, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


class _Clone:
    def __init__(self, provider: _RecordingProvider) -> None:
        self.provider = provider
        self.current_config = type("C", (), {"model": "test/model"})()

    def override_model(self, model: str, **_kw: Any) -> None:
        self.current_config = type("C", (), {"model": model})()

    def resolve(self) -> _RecordingProvider:
        return self.provider


class _Selector:
    def __init__(self, provider: _RecordingProvider) -> None:
        self.provider = provider

    def clone(self) -> _Clone:
        return _Clone(self.provider)


class _RecallProvider(MemoryProvider):
    """Recall that echoes the query, so a mixed-up turn would show."""

    def __init__(self, gate: asyncio.Event | None = None) -> None:
        self.gate = gate
        self.queries: list[str] = []

    @property
    def name(self) -> str:
        return "fake"

    def is_available(self) -> bool:
        return True

    async def initialize(self, session_id: str, **kwargs: Any) -> None:
        return None

    async def prefetch(self, query: str, *, session_id: str = "") -> str:
        self.queries.append(query)
        if self.gate is not None:
            await self.gate.wait()
        return f"recalled for: {query}"

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return []

    async def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kw: Any) -> str:
        return "{}"

    async def shutdown(self) -> None:
        return None


class _SyncManager:
    def __init__(self) -> None:
        self.warmed: list[str] = []

    async def warm_session(self, session_key: str) -> None:
        await asyncio.sleep(0)
        self.warmed.append(session_key)

    def notify_message(self, byte_count: int) -> None:
        return None


async def _drive_three_turns(
    tmp_path: Path, *, concurrent: bool
) -> tuple[_RecordingProvider, _SyncManager]:
    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        manager = SessionManager(
            storage, inject_time_prefix=False, checkpoint_workspace_dir=tmp_path
        )
        project = await manager.create_project("main", "Research", knowledge="Alpha facts.")
        await manager.create(KEY, agent_id="main", project_id=project["project_id"])
        provider = _RecordingProvider()
        memory_manager = MemoryProviderManager()
        assert memory_manager.add_provider(_RecallProvider())
        sync_manager = _SyncManager()
        runner = TurnRunner(
            provider_selector=_Selector(provider),
            session_manager=manager,
            memory_provider_managers={"main": memory_manager},
            memory_sync_managers={"main": sync_manager},
            config=GatewayConfig(
                workspace_dir=str(tmp_path),
                attachments=AttachmentsConfig(media_root=str(tmp_path / "media")),
                agentos_router=AgentOSRouterConfig(enabled=False),
                task_runtime=TaskRuntimeConfig(concurrent_turn_setup=concurrent),
            ),
        )
        for text in ("first question", "second question", "third question"):
            await manager.append_message(KEY, "user", text)
            async for _ in runner.run(
                text,
                KEY,
                agent_id="main",
                tool_context=ToolContext(caller_kind=CallerKind.WEB, workspace_dir=str(tmp_path)),
                history_has_persisted_user=True,
                no_memory_capture=True,
            ):
                pass
        return provider, sync_manager
    finally:
        await storage.close()


_PINNED_NOW = datetime(2026, 11, 3, 6, 44, 30, tzinfo=UTC)


class _PinnedClock:
    """Stands in for ``datetime`` in ``agentos.engine.agent``: ``now()`` never moves.

    The runtime context block the agent prepends to the user message carries
    ``Current local date/time`` at minute resolution. The two setups below run
    one after the other on the wall clock, so a run that straddled a minute
    boundary (06:44 vs 06:45) made the requests differ by that one line.
    """

    @staticmethod
    def now(tz: tzinfo | None = None) -> datetime:
        return _PINNED_NOW if tz is None else _PINNED_NOW.astimezone(tz)


async def test_concurrent_setup_sends_byte_identical_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_module, "datetime", _PinnedClock)
    concurrent_dir = tmp_path / "concurrent"
    serial_dir = tmp_path / "serial"
    concurrent_dir.mkdir()
    serial_dir.mkdir()
    concurrent, concurrent_sync = await _drive_three_turns(concurrent_dir, concurrent=True)
    serial, serial_sync = await _drive_three_turns(serial_dir, concurrent=False)

    def _normalised(provider: _RecordingProvider, root: Path) -> list[str]:
        # json.dumps doubles a Windows path's backslashes, so match the escaped form.
        root_text = json.dumps(str(root))[1:-1]
        return [json.dumps(r, sort_keys=True).replace(root_text, "<ws>") for r in provider.requests]

    assert len(concurrent.requests) == 3
    assert _normalised(concurrent, concurrent_dir) == _normalised(serial, serial_dir)
    # The pieces that were overlapped really reached the prompt.
    last = json.dumps(concurrent.requests[-1])
    assert "recalled for: third question" in last
    assert "Alpha facts." in last
    # One warm per turn either way (the second and later are no-ops in the
    # real manager); never a doubled call from the early start.
    assert concurrent_sync.warmed == serial_sync.warmed == [KEY, KEY, KEY]


async def test_slow_recall_does_not_hold_the_turn(tmp_path: Path) -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    gate = asyncio.Event()
    try:
        manager = SessionManager(storage, inject_time_prefix=False)
        await manager.create(KEY, agent_id="main")
        provider = _RecordingProvider()
        memory_manager = MemoryProviderManager()
        assert memory_manager.add_provider(_RecallProvider(gate=gate))
        runner = TurnRunner(
            provider_selector=_Selector(provider),
            session_manager=manager,
            memory_provider_managers={"main": memory_manager},
            config=GatewayConfig(
                workspace_dir=str(tmp_path),
                attachments=AttachmentsConfig(media_root=str(tmp_path / "media")),
                agentos_router=AgentOSRouterConfig(enabled=False),
                memory=MemoryConfig(recall_timeout_seconds=0.01),
            ),
        )
        await manager.append_message(KEY, "user", "hello")
        with structlog.testing.capture_logs() as logs:
            async for _ in runner.run(
                "hello",
                KEY,
                agent_id="main",
                tool_context=ToolContext(caller_kind=CallerKind.WEB, workspace_dir=str(tmp_path)),
                history_has_persisted_user=True,
                no_memory_capture=True,
            ):
                pass
        assert len(provider.requests) == 1
        assert "recalled for" not in json.dumps(provider.requests[0])
        assert [e["event"] for e in logs].count("memory.recall_timeout") == 1
    finally:
        gate.set()
        await asyncio.sleep(0)
        await storage.close()


def test_config_defaults() -> None:
    assert MemoryConfig().recall_timeout_seconds == 1.5
    assert TaskRuntimeConfig().concurrent_turn_setup is True
    with pytest.raises(ValueError):
        MemoryConfig(recall_timeout_seconds=0)


# ---------------------------------------------------------------------------
# Blocking work kept off the event loop
# ---------------------------------------------------------------------------


async def test_attachment_build_runs_off_the_event_loop() -> None:
    import threading

    from agentos.engine.turn_runner.attachment_stage import (
        AttachmentStage,
        AttachmentStageInput,
    )

    threads: list[int] = []

    class _Builder:
        def build(self, message: str, attachments: list[dict]) -> list[Any] | None:
            threads.append(threading.get_ident())
            return ["msg"] if attachments else None

    stage = AttachmentStage(builder=_Builder())
    out = (
        await stage.run(AttachmentStageInput(effective_runtime_message="m", attachments=[{"x": 1}]))
    ).require_output()
    assert out.extra_messages == ["msg"]
    assert out.turn_input == ""
    assert threads[-1] != threading.get_ident()

    # Nothing to build: stays inline, no thread hop.
    out = (
        await stage.run(AttachmentStageInput(effective_runtime_message="m", attachments=None))
    ).require_output()
    assert out.turn_input == "m"
    assert threads[-1] == threading.get_ident()


async def test_memory_sync_scans_and_reads_files_off_the_event_loop(tmp_path: Path) -> None:
    import threading

    from agentos.memory.sync_manager import MemorySyncManager

    workspace = tmp_path / "ws"
    (workspace / "memory").mkdir(parents=True)
    (workspace / "MEMORY.md").write_text("root\n", encoding="utf-8")

    class _Store:
        def __init__(self) -> None:
            self.indexed: dict[str, str] = {}

        async def index_file(self, *, path: str, content: str, source: object, mtime=None) -> int:
            self.indexed[path] = content
            return 1

        async def remove_file(self, path: str) -> None:
            return None

    store = _Store()
    manager = MemorySyncManager(
        store=store, workspace_dir=workspace, memory_dir=workspace / "memory"
    )
    loop_thread = threading.get_ident()
    scan_threads: list[int] = []
    real_scan = manager._scan_files

    def _scan() -> dict[str, float]:
        scan_threads.append(threading.get_ident())
        return real_scan()

    manager._scan_files = _scan  # type: ignore[method-assign]
    await manager.warm_session("agent:main:s")
    assert scan_threads and all(t != loop_thread for t in scan_threads)
    assert store.indexed == {"MEMORY.md": "root\n"}


def test_system_prompt_template_is_compiled_once_per_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jinja2

    from agentos.identity.prompt import _make_env, assemble_system_prompt
    from agentos.identity.types import AgentProfile

    profile = AgentProfile(agent_id="main")
    first = assemble_system_prompt(profile)
    compiles: list[str] = []
    real_compile = jinja2.Environment.compile

    def _counting_compile(self, *args, **kwargs):
        compiles.append("compile")
        return real_compile(self, *args, **kwargs)

    monkeypatch.setattr(jinja2.Environment, "compile", _counting_compile)
    assert assemble_system_prompt(profile) == first
    assert assemble_system_prompt(profile) == first
    assert compiles == []
    assert _make_env() is _make_env()
