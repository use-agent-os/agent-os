"""A turn cancelled mid-tool is persisted so the next turn replays it cleanly.

The agent seals a cancelled turn (every tool call paired with a cancelled
result, an interruption note at the end) and the runner persists that record
with the partial row. The next turn's request must then carry the pairs and
the note without the history loader having to repair anything.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.engine.runtime import TurnRunner
from agentos.gateway.config import AgentOSRouterConfig, AttachmentsConfig, GatewayConfig
from agentos.provider import Message
from agentos.session.manager import SessionManager
from agentos.session.storage import SessionStorage
from agentos.tools.registry import ToolRegistry
from agentos.tools.types import CallerKind, ToolContext, ToolSpec
from tests.fakes import ScriptedProvider, done, text, tool_call

KEY = "agent:main:webchat:cancel-replay"


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
async def manager(tmp_path: Path) -> AsyncIterator[SessionManager]:
    storage = SessionStorage(":memory:")
    await storage.connect()
    yield SessionManager(storage, inject_time_prefix=False, checkpoint_workspace_dir=tmp_path)
    await storage.close()


def _blocks(messages: list[Message], kind: str) -> list[Any]:
    return [
        block
        for message in messages
        if isinstance(message.content, list)
        for block in message.content
        if getattr(block, "type", None) == kind
    ]


async def test_cancelled_tool_turn_replays_with_pairs_and_note(
    manager: SessionManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agentos.engine.agent as agent_module
    import agentos.engine.transcript as transcript_module

    repairs: list[int] = []
    real_repair = transcript_module.repair_tool_pairing

    def spy_repair(messages: list[Message]) -> list[Message]:
        repaired = real_repair(messages)
        if repaired != messages:
            repairs.append(len(messages))
        return repaired

    monkeypatch.setattr(transcript_module, "repair_tool_pairing", spy_repair)
    monkeypatch.setattr(agent_module, "repair_tool_pairing", spy_repair)

    started = asyncio.Event()
    registry = ToolRegistry()

    async def slow(**arguments: Any) -> str:
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
    runner = TurnRunner(
        provider_selector=_Selector(provider),
        tool_registry=registry,
        session_manager=manager,
        config=GatewayConfig(
            attachments=AttachmentsConfig(media_root=str(tmp_path / "media")),
            agentos_router=AgentOSRouterConfig(enabled=False),
        ),
    )
    await manager.create(KEY)
    ctx = ToolContext(caller_kind=CallerKind.WEB)

    async def first_turn() -> None:
        await manager.append_message(KEY, "user", "run the slow thing")
        async for _ in runner.run(
            "run the slow thing", KEY, tool_context=ctx, history_has_persisted_user=True
        ):
            pass

    task = asyncio.create_task(first_turn())
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await manager.append_message(KEY, "user", "never mind, say hi")
    [
        _
        async for _ in runner.run(
            "never mind, say hi", KEY, tool_context=ctx, history_has_persisted_user=True
        )
    ]

    request = provider.calls[-1].messages
    uses = [block.id for block in _blocks(request, "tool_use")]
    results = {block.tool_use_id: block for block in _blocks(request, "tool_result")}
    assert uses == ["T1"]
    assert "T1" in results and results["T1"].is_error
    assert "interrupted" in str(request).lower()
    # The persisted record was already valid: no history load had to repair it.
    assert repairs == []
