"""Request hooks can amend or veto a provider request (``tool-call-hooks`` spec)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.engine import Agent, AgentConfig
from agentos.engine.hooks import Amend, RequestHookCall, Veto
from agentos.engine.types import DoneEvent, ErrorEvent
from tests.fakes import ScriptedProvider, done, text


class _Hook:
    def __init__(self, decision: Any = None, *, raises: bool = False, fail_closed: bool = False):
        self.name = "test-hook"
        self._decision = decision
        self._raises = raises
        self.fail_closed = fail_closed
        self.calls: list[RequestHookCall] = []

    def before_request(self, call: RequestHookCall) -> Any:
        self.calls.append(call)
        if self._raises:
            raise RuntimeError("hook crashed")
        return self._decision


def _agent(provider: ScriptedProvider, hooks: list[Any]) -> Agent:
    return Agent(
        provider=provider,
        config=AgentConfig(system_prompt="You are a test agent."),
        session_key="agent:main:webchat:hooks",
        request_hooks=hooks,
    )


async def _run(agent: Agent) -> list[Any]:
    return [event async for event in agent.run_turn("hello")]


async def test_veto_skips_the_provider_and_fails_the_turn() -> None:
    provider = ScriptedProvider([[text("never"), done()]])

    events = await _run(_agent(provider, [_Hook(Veto("Daily spend limit reached."))]))

    assert provider.calls == []
    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert [(e.code, e.message) for e in errors] == [
        ("request_vetoed", "Daily spend limit reached.")
    ]
    assert isinstance(events[-1], DoneEvent)
    assert events[-1].outcome.kind == "failed"


async def test_amend_adds_a_trailing_note_and_keeps_the_prefix_identical() -> None:
    plain = ScriptedProvider([[text("ok"), done()]])
    amended = ScriptedProvider([[text("ok"), done()]])

    await _run(_agent(plain, []))
    await _run(_agent(amended, [_Hook(Amend("Note: the user is on mobile."))]))

    base = plain.calls[0]
    hooked = amended.calls[0]
    assert hooked.config.system == base.config.system
    assert hooked.messages[:-1] == base.messages
    assert hooked.messages[-1].role == "user"
    assert hooked.messages[-1].content == "Note: the user is on mobile."


async def test_amendment_is_not_persisted_to_history() -> None:
    provider = ScriptedProvider([[text("ok"), done()]])
    agent = _agent(provider, [_Hook(Amend("request-only note"))])

    await _run(agent)

    assert "request-only note" not in str(agent._history)


async def test_hook_that_raises_is_ignored() -> None:
    provider = ScriptedProvider([[text("ok"), done()]])

    events = await _run(_agent(provider, [_Hook(raises=True)]))

    assert len(provider.calls) == 1
    assert events[-1].outcome.kind == "completed"


async def test_fail_closed_hook_that_raises_vetoes() -> None:
    provider = ScriptedProvider([[text("ok"), done()]])

    events = await _run(_agent(provider, [_Hook(raises=True, fail_closed=True)]))

    assert provider.calls == []
    assert events[-1].outcome.kind == "failed"


async def test_turn_runner_passes_request_hooks_to_the_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentos.engine.runtime import TurnRunner

    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path))
    hook = _Hook(Veto("no"))
    runner = TurnRunner(provider_selector=SimpleNamespace(), request_hooks=[hook])

    agent = TurnRunner.build_agent(
        runner,
        provider=ScriptedProvider([[done()]]),
        config=AgentConfig(),
        tool_definitions=[],
        tool_handler=None,
        session_key="agent:main:webchat:hooks",
        turn_call_logger=None,
        memory_sync_manager=None,
        tool_context=None,
    )

    assert agent._request_hooks == (hook,)
