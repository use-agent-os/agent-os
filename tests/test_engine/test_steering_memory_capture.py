"""A message steered in with ``noMemoryCapture`` keeps the whole turn out of memory.

Drives the real ``TurnRunner`` (in-memory ``SessionManager``) with a bound
steering source; the memory capture port records what it was asked to do.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.engine.runtime import TurnRunner
from agentos.engine.steering import SteeringMessage, bind_steering
from agentos.engine.types import SteeringEvent
from agentos.gateway.config import AgentOSRouterConfig, AttachmentsConfig, GatewayConfig
from agentos.provider import DoneEvent as ProviderDone
from agentos.provider import TextDeltaEvent as ProviderText
from agentos.session.manager import SessionManager
from agentos.session.storage import SessionStorage
from agentos.tools.types import CallerKind, ToolContext

KEY = "agent:main:webchat:steer-no-capture"


class _ReplyProvider:
    provider_name = "test"

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, messages: Any, tools: Any = None, config: Any = None) -> AsyncIterator[Any]:
        self.calls += 1
        return self._stream(self.calls)

    async def _stream(self, n: int) -> AsyncIterator[Any]:
        yield ProviderText(text=f"answer {n}")
        yield ProviderDone(stop_reason="end_turn", input_tokens=5, output_tokens=2)

    async def list_models(self) -> list[Any]:
        return []


class _Selector:
    current_config = SimpleNamespace(model="test/model")

    def __init__(self, provider: _ReplyProvider) -> None:
        self.provider = provider

    def clone(self) -> _Selector:
        return self

    def resolve(self) -> _ReplyProvider:
        return self.provider

    def override_model(self, model: str) -> None:
        self.current_config = SimpleNamespace(model=model)


class _Steering:
    def __init__(self, manager: SessionManager, *, no_memory_capture: bool) -> None:
        self._manager = manager
        self._no_capture = no_memory_capture
        self.waiting = True

    @property
    def session_key(self) -> str:
        return KEY

    def has_pending(self) -> bool:
        return self.waiting

    async def take(self) -> SteeringMessage | None:
        if not self.waiting:
            return None
        self.waiting = False
        row = await self._manager.append_message(KEY, "user", "keep this private")
        return SteeringMessage(
            text="keep this private",
            message_id=row.message_id,
            task_id="t-2",
            no_memory_capture=self._no_capture,
        )


@pytest.fixture(autouse=True)
def _state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path))


@pytest.fixture
async def manager(tmp_path: Path) -> AsyncIterator[SessionManager]:
    storage = SessionStorage(":memory:")
    await storage.connect()
    yield SessionManager(storage, inject_time_prefix=False, checkpoint_workspace_dir=tmp_path)
    await storage.close()


async def _run_with(manager: SessionManager, tmp_path: Path, *, no_capture: bool) -> list[Any]:
    provider = _ReplyProvider()
    runner = TurnRunner(
        provider_selector=_Selector(provider),
        session_manager=manager,
        config=GatewayConfig(
            attachments=AttachmentsConfig(media_root=str(tmp_path / "media")),
            agentos_router=AgentOSRouterConfig(enabled=False),
        ),
    )
    captured: list[dict[str, Any]] = []

    async def capture_turn(**kwargs: Any) -> None:
        captured.append(kwargs)

    runner._turn_finalizer_stage._turn_memory_capture.capture_turn = capture_turn  # type: ignore[method-assign]
    await manager.create(KEY)
    await manager.append_message(KEY, "user", "hello")
    events: list[Any] = []
    with bind_steering(_Steering(manager, no_memory_capture=no_capture)):
        async for event in runner.run(
            "hello",
            KEY,
            tool_context=ToolContext(caller_kind=CallerKind.WEB),
            history_has_persisted_user=True,
        ):
            events.append(event)
    assert provider.calls == 2
    assert any(isinstance(e, SteeringEvent) for e in events)
    return captured


@pytest.mark.asyncio
async def test_no_memory_capture_steer_turns_capture_off_for_the_turn(
    manager: SessionManager, tmp_path: Path
) -> None:
    captured = await _run_with(manager, tmp_path, no_capture=True)
    assert [call["no_memory_capture"] for call in captured] == [True]


@pytest.mark.asyncio
async def test_plain_steer_leaves_capture_on(manager: SessionManager, tmp_path: Path) -> None:
    captured = await _run_with(manager, tmp_path, no_capture=False)
    assert [call["no_memory_capture"] for call in captured] == [False]
