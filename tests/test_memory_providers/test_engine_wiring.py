"""Engine wiring tests for the memory provider layer (Task B4).

Deterministic and offline: a fake async provider is attached to a real-ish
``MemoryProviderManager`` and wired into ``TurnRunner`` / the ``memory`` tool.
Background work is flushed with ``flush_pending`` (a barrier) rather than
sleeps, so ordering assertions are stable.

Covered wiring points:
1. Prompt — provider STATIC block joins the system prompt; PREFETCHED fenced
   context lands in the per-turn volatile suffix.
2. Turn end — a completed turn enqueues ``sync_all`` + ``queue_prefetch_all``.
3. Write mirror — a successful curated ``memory`` write mirrors to the provider.
4. Disabled default — no provider manager means zero provider calls.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from agentos.engine.runtime import TurnRunner
from agentos.memory.providers.base import MemoryProvider
from agentos.memory.providers.manager import MemoryProviderManager

OPEN = "<memory-context>"
CLOSE = "</memory-context>"


class _FakeProvider(MemoryProvider):
    """Recording fake provider with configurable prefetch + static block."""

    def __init__(
        self,
        name: str = "fake",
        *,
        prefetch_result: str = "",
        static_block: str = "",
    ) -> None:
        self._name = name
        self._prefetch_result = prefetch_result
        self._static_block = static_block
        self.sync_calls: list[tuple[str, str, str]] = []
        self.queue_prefetch_calls: list[str] = []
        self.memory_writes: list[tuple[str, str, str, dict[str, Any] | None]] = []
        self.session_ended: list[list[dict[str, Any]]] = []
        self.session_switched: list[str] = []

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return True

    def system_prompt_block(self) -> str:
        return self._static_block

    async def initialize(self, session_id: str, **kwargs: Any) -> None:
        return None

    async def prefetch(self, query: str, *, session_id: str = "") -> str:
        return self._prefetch_result

    async def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        self.queue_prefetch_calls.append(query)

    async def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        self.sync_calls.append((user_content, assistant_content, session_id))

    async def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.memory_writes.append((action, target, content, metadata))

    async def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        self.session_ended.append(messages)

    async def on_session_switch(self, new_session_id: str, **kwargs: Any) -> None:
        self.session_switched.append(new_session_id)

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return []

    async def handle_tool_call(
        self, tool_name: str, args: dict[str, Any], **kwargs: Any
    ) -> str:
        return "{}"

    async def shutdown(self) -> None:
        return None


def _manager_with(provider: _FakeProvider) -> MemoryProviderManager:
    mgr = MemoryProviderManager()
    assert mgr.add_provider(provider)
    return mgr


def _runner(tmp_path, provider_managers=None, **memory_kwargs) -> TurnRunner:
    return TurnRunner(
        provider_selector=None,
        config=SimpleNamespace(
            workspace_dir=str(tmp_path),
            memory=SimpleNamespace(source="workspace", **memory_kwargs),
            tools=SimpleNamespace(profile=None),
        ),
        memory_provider_managers=provider_managers,
    )


def _prompt_text(assembled) -> str:
    if isinstance(assembled, tuple):
        return "\n\n".join(part for part in assembled if part)
    return assembled or ""


# -- Requirement 1a: STATIC provider block joins the system prompt ------------


def test_provider_static_block_lands_in_system_prompt(tmp_path):
    (tmp_path / "AGENTS.md").write_text("agents\n", encoding="utf-8")
    provider = _FakeProvider(static_block="## Long-Term Memory\n\nProvider is live.")
    runner = _runner(tmp_path, provider_managers={"main": _manager_with(provider)})

    assembled = runner._assemble_prompt("main", [], session_key="agent:main:auto")

    prompt = _prompt_text(assembled)
    assert "Provider is live." in prompt


def test_no_provider_static_block_when_manager_absent(tmp_path):
    (tmp_path / "AGENTS.md").write_text("agents\n", encoding="utf-8")
    runner = _runner(tmp_path)  # no provider managers

    assembled = runner._assemble_prompt("main", [], session_key="agent:main:auto")

    prompt = _prompt_text(assembled)
    assert "Provider is live." not in prompt


# -- Requirement 1b: PREFETCHED fenced context lands per-turn -----------------


async def test_prefetch_block_injected_into_extra_context(tmp_path):
    provider = _FakeProvider(prefetch_result="user prefers dark mode")
    runner = _runner(tmp_path, provider_managers={"main": _manager_with(provider)})

    extra = await runner._augment_extra_context_with_prefetch(
        agent_id="main",
        session_id="sess-1",
        message="what theme?",
        extra_context=None,
    )

    assert extra is not None
    joined = "\n".join(extra.values())
    assert OPEN in joined and CLOSE in joined
    assert "user prefers dark mode" in joined


async def test_prefetch_empty_result_leaves_context_untouched(tmp_path):
    provider = _FakeProvider(prefetch_result="")
    runner = _runner(tmp_path, provider_managers={"main": _manager_with(provider)})

    extra = await runner._augment_extra_context_with_prefetch(
        agent_id="main",
        session_id="sess-1",
        message="q",
        extra_context=None,
    )
    assert extra is None


async def test_prefetch_no_provider_is_noop(tmp_path):
    runner = _runner(tmp_path)  # disabled default
    extra = await runner._augment_extra_context_with_prefetch(
        agent_id="main",
        session_id="sess-1",
        message="q",
        extra_context={"Existing": "keep me"},
    )
    # Unchanged object returned; no provider work.
    assert extra == {"Existing": "keep me"}


async def test_prefetch_slow_provider_times_out_and_injects_nothing(tmp_path):
    class _SlowProvider(_FakeProvider):
        async def prefetch(self, query: str, *, session_id: str = "") -> str:
            import asyncio

            await asyncio.sleep(10)
            return "too late"

    provider = _SlowProvider(prefetch_result="ignored")
    runner = _runner(tmp_path, provider_managers={"main": _manager_with(provider)})

    extra = await runner._augment_extra_context_with_prefetch(
        agent_id="main",
        session_id="sess-1",
        message="q",
        extra_context=None,
        timeout=0.05,
    )
    assert extra is None


# -- Requirement 2: turn end enqueues sync + queue_prefetch -------------------


class _FakeSession:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id


class _FakeSessionManager:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def get_session(self, session_key: str) -> _FakeSession:
        return self._session


class _FakeCaptureService:
    def __init__(self) -> None:
        self.captured: list[dict[str, Any]] = []

    async def capture_turn(self, **kwargs: Any) -> str | None:
        self.captured.append(kwargs)
        return "turns/agent-main-main/2026-05-14.md"


async def test_turn_end_enqueues_sync_and_prefetch(tmp_path):
    provider = _FakeProvider()
    mgr = _manager_with(provider)
    runner = _runner(tmp_path, provider_managers={"main": mgr})
    runner._session_manager = _FakeSessionManager(_FakeSession("sess-9"))
    runner._turn_capture_services = {"main": _FakeCaptureService()}

    await runner._capture_turn_memory(
        agent_id="main",
        session_key="agent:main:main",
        runtime_message="hello",
        final_text="world",
        input_mode="user",
        tool_context=None,
        input_provenance=None,
    )

    # flush_pending is the barrier — enqueued bg work runs before assertions.
    await mgr.flush_pending()

    assert provider.sync_calls == [("hello", "world", "sess-9")]
    assert provider.queue_prefetch_calls == ["hello"]


async def test_turn_end_no_provider_is_noop(tmp_path):
    runner = _runner(tmp_path)  # no provider managers
    runner._session_manager = _FakeSessionManager(_FakeSession("sess-9"))
    capture = _FakeCaptureService()
    runner._turn_capture_services = {"main": capture}

    await runner._capture_turn_memory(
        agent_id="main",
        session_key="agent:main:main",
        runtime_message="hello",
        final_text="world",
        input_mode="user",
        tool_context=None,
        input_provenance=None,
    )
    # Capture still fired; nothing else to assert (no provider present).
    assert capture.captured


# -- Requirement 4: write mirror ---------------------------------------------


async def _make_memory_tool(tmp_path, provider_manager):
    from agentos.memory import LongTermMemoryStore, MemoryRetriever
    from agentos.tools.builtin.memory_tools import create_memory_tools
    from agentos.tools.registry import ToolRegistry
    from agentos.tools.types import CallerKind, ToolContext, current_tool_context

    store = LongTermMemoryStore(db_path=str(tmp_path / "memory.db"))
    await store.initialize()
    retriever = MemoryRetriever(store)
    registry = ToolRegistry()
    create_memory_tools(
        {"main": store},
        {"main": retriever},
        memory_dir=str(tmp_path),
        registry=registry,
        memory_source="workspace",
        workspace_base=str(tmp_path),
        provider_managers={"main": provider_manager} if provider_manager else None,
    )
    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.AGENT,
        agent_id="main",
        workspace_dir=str(tmp_path / "agents" / "main" / "workspace"),
    )
    return registry, ctx, current_tool_context, store


async def test_successful_single_write_mirrors_to_provider(tmp_path):
    provider = _FakeProvider()
    mgr = _manager_with(provider)
    registry, ctx, cv, _store = await _make_memory_tool(tmp_path, mgr)

    memory = registry.get("memory").handler
    token = cv.set(ctx)
    try:
        result = await memory(action="add", target="memory", content="prefers dark mode")
    finally:
        cv.reset(token)
    assert json.loads(result)["success"] is True

    await mgr.flush_pending()
    assert provider.memory_writes == [
        ("add", "memory", "prefers dark mode", {}),
    ]
    await _store.close()


async def test_batch_write_mirrors_each_op(tmp_path):
    provider = _FakeProvider()
    mgr = _manager_with(provider)
    registry, ctx, cv, _store = await _make_memory_tool(tmp_path, mgr)

    memory = registry.get("memory").handler
    token = cv.set(ctx)
    try:
        result = await memory(
            target="memory",
            operations=[
                {"action": "add", "content": "fact one"},
                {"action": "add", "content": "fact two"},
            ],
        )
    finally:
        cv.reset(token)
    assert json.loads(result)["success"] is True

    await mgr.flush_pending()
    actions = [(a, t, c) for (a, t, c, _m) in provider.memory_writes]
    assert ("add", "memory", "fact one") in actions
    assert ("add", "memory", "fact two") in actions
    assert len(provider.memory_writes) == 2
    await _store.close()


async def test_failed_write_does_not_mirror(tmp_path):
    provider = _FakeProvider()
    mgr = _manager_with(provider)
    registry, ctx, cv, _store = await _make_memory_tool(tmp_path, mgr)

    memory = registry.get("memory").handler
    token = cv.set(ctx)
    try:
        # 'add' with no content fails validation -> no mirror.
        result = await memory(action="add", target="memory", content="")
    finally:
        cv.reset(token)
    assert json.loads(result)["success"] is False

    await mgr.flush_pending()
    assert provider.memory_writes == []
    await _store.close()


async def test_no_provider_write_is_noop(tmp_path):
    registry, ctx, cv, _store = await _make_memory_tool(tmp_path, None)
    memory = registry.get("memory").handler
    token = cv.set(ctx)
    try:
        result = await memory(action="add", target="memory", content="ok")
    finally:
        cv.reset(token)
    assert json.loads(result)["success"] is True  # write still works
    await _store.close()


# -- Requirement 3: session end + switch on reset -----------------------------


async def test_session_boundary_notifies_end_then_switch(tmp_path):
    from agentos.gateway.rpc_sessions import _notify_provider_session_boundary

    provider = _FakeProvider()
    mgr = _manager_with(provider)
    runner = _runner(tmp_path, provider_managers={"main": mgr})
    ctx = SimpleNamespace(turn_runner=runner)
    transcript = [
        SimpleNamespace(role="user", content="hi"),
        SimpleNamespace(role="assistant", content="hello"),
        SimpleNamespace(role="system", content="ignored"),
    ]

    await _notify_provider_session_boundary(
        ctx,
        agent_id="main",
        transcript=transcript,
        new_session_id="sess-new",
    )

    assert provider.session_ended == [
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    ]
    assert provider.session_switched == ["sess-new"]


async def test_session_boundary_no_provider_is_noop(tmp_path):
    from agentos.gateway.rpc_sessions import _notify_provider_session_boundary

    runner = _runner(tmp_path)  # no provider managers
    ctx = SimpleNamespace(turn_runner=runner)

    # Must not raise and must be a clean no-op.
    await _notify_provider_session_boundary(
        ctx,
        agent_id="main",
        transcript=[SimpleNamespace(role="user", content="hi")],
        new_session_id="sess-new",
    )
