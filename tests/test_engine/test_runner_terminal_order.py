"""Every ``TurnRunner`` turn ends with exactly one DoneEvent, after its error.

The runner holds a turn's ErrorEvent back until the transcript is persisted;
the DoneEvent of a failed turn is held with it so the order a client sees is
always ``error`` then ``done``. Turns the runner itself ends (a refused spend
budget, an exception) get their DoneEvent from the runner.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.engine.runtime import TurnRunner
from agentos.engine.types import DoneEvent, ErrorEvent
from agentos.gateway.config import AgentOSRouterConfig, AttachmentsConfig, GatewayConfig
from agentos.session.manager import SessionManager
from agentos.session.storage import SessionStorage
from agentos.tools.registry import ToolRegistry
from agentos.tools.types import CallerKind, ToolContext, ToolSpec
from tests.fakes import ScriptedProvider, done, error, text, tool_call

KEY = "agent:main:webchat:terminal-order"


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

    async def _no_sleep(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr("agentos.engine.agent.asyncio.sleep", _no_sleep)


@pytest.fixture
async def manager(tmp_path: Path) -> AsyncIterator[SessionManager]:
    storage = SessionStorage(":memory:")
    await storage.connect()
    yield SessionManager(storage, inject_time_prefix=False, checkpoint_workspace_dir=tmp_path)
    await storage.close()


def _runner(provider: ScriptedProvider, manager: SessionManager, tmp_path: Path) -> TurnRunner:
    registry = ToolRegistry()

    async def lookup(**arguments: Any) -> str:
        return "found it"

    registry.register(
        ToolSpec(name="lookup", description="lookup", parameters={}, read_only=True), lookup
    )
    return TurnRunner(
        provider_selector=_Selector(provider),
        tool_registry=registry,
        session_manager=manager,
        config=GatewayConfig(
            attachments=AttachmentsConfig(media_root=str(tmp_path / "media")),
            agentos_router=AgentOSRouterConfig(enabled=False),
        ),
    )


async def _run(runner: TurnRunner, manager: SessionManager) -> list[Any]:
    await manager.create(KEY)
    await manager.append_message(KEY, "user", "go")
    return [
        event
        async for event in runner.run(
            "go",
            KEY,
            tool_context=ToolContext(caller_kind=CallerKind.WEB),
            history_has_persisted_user=True,
            max_provider_retries=0,
        )
    ]


def _terminal_kinds(events: list[Any]) -> list[str]:
    return [e.kind for e in events if isinstance(e, ErrorEvent | DoneEvent)]


async def test_failure_after_usage_ends_with_error_then_one_done(
    manager: SessionManager, tmp_path: Path
) -> None:
    provider = ScriptedProvider(
        [
            [tool_call("lookup", tool_use_id="L"), done("tool_use", input_tokens=50)],
            [error("500", "upstream exploded")],
        ]
    )

    events = await _run(_runner(provider, manager, tmp_path), manager)

    assert _terminal_kinds(events) == ["error", "done"]
    assert events[-1].kind == "done"
    assert events[-1].outcome.kind == "failed"
    assert events[-1].input_tokens > 0


async def test_success_ends_with_one_completed_done(
    manager: SessionManager, tmp_path: Path
) -> None:
    provider = ScriptedProvider([[text("hello"), done()]])

    events = await _run(_runner(provider, manager, tmp_path), manager)

    assert _terminal_kinds(events) == ["done"]
    assert events[-1].outcome.kind == "completed"


async def test_refused_budget_ends_with_error_then_done(
    manager: SessionManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider([[text("never sent"), done()]])
    runner = _runner(provider, manager, tmp_path)
    monkeypatch.setattr(
        runner, "_reserve_spend_budget", lambda session_key: (True, "Daily budget hit.", None)
    )

    events = await _run(runner, manager)

    assert provider.calls == []
    assert _terminal_kinds(events) == ["error", "done"]
    assert events[-1].outcome.kind == "failed"
    assert events[-1].outcome.reason == "budget_exceeded"


async def test_runner_exception_before_done_ends_with_error_then_done(
    manager: SessionManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider([[text("hello"), done()]])
    runner = _runner(provider, manager, tmp_path)

    async def _explode(inp: Any) -> Any:
        raise RuntimeError("consumer broke")
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(runner._stream_consumer_stage, "run", _explode)

    events = await _run(runner, manager)

    assert _terminal_kinds(events) == ["error", "done"]
    assert events[-1].outcome.kind == "failed"


async def test_failure_after_done_emits_nothing_more(
    manager: SessionManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider([[text("hello"), done()]])
    runner = _runner(provider, manager, tmp_path)

    async def _explode(inp: Any) -> Any:
        raise RuntimeError("finalizer broke")

    monkeypatch.setattr(runner._turn_finalizer_stage, "run", _explode)

    events = await _run(runner, manager)

    assert _terminal_kinds(events) == ["done"]
    assert events[-1].kind == "done"
