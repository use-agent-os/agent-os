"""The attempt policy is the only bound on provider retries.

The retry loop used to stop on its own counter, which the empty-response retry
after a tool round bumped without telling the policy. A transient failure after
that retry then left the loop while the policy still answered ``Retry``: the
real error was replaced by ``provider_stream_incomplete`` and failover was
never asked.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agentos.engine import Agent, AgentConfig
from agentos.engine.types import DoneEvent, ErrorEvent, WarningEvent
from tests.fakes import FakeTool, FakeToolbox, Hang, ScriptedProvider, done, error, text, tool_call

_503 = "HTTP 503: upstream unavailable"


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    real_sleep = asyncio.sleep

    async def fast(*args: Any, **kwargs: Any) -> None:
        await real_sleep(0)

    monkeypatch.setattr("agentos.engine.agent.asyncio.sleep", fast)


def _after_tools_then_503(*, fallback: bool) -> tuple[ScriptedProvider, FakeToolbox]:
    toolbox = FakeToolbox(FakeTool("probe"))
    provider = ScriptedProvider(
        [
            [tool_call("probe"), done("tool_use")],
            [error("empty_response", "empty response")],
            [error("503", _503)],
        ],
        offers_fallback=fallback,
    )
    return provider, toolbox


async def _run(agent: Agent) -> list[Any]:
    return [event async for event in agent.run_turn("go")]


@pytest.mark.parametrize("max_retries", [1, 6])
async def test_transient_failure_after_the_empty_response_retry_fails_over(
    no_sleep: None, max_retries: int
) -> None:
    provider, toolbox = _after_tools_then_503(fallback=True)
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_provider_retries=max_retries),
        session_key="agent:main:webchat:bounds",
        **toolbox.agent_kwargs(),
    )

    events = await _run(agent)

    warnings = [e.code for e in events if isinstance(e, WarningEvent)]
    assert "provider_empty_retry" in warnings
    assert "provider_recovery_fallback" in warnings
    assert provider.fallback_reasons == [_503]
    # The empty-response retry is one of the same-model retries: tool round,
    # empty response, then the 503 on the remaining retries, then failover.
    assert len(provider.calls) == 1 + 1 + max_retries + 1
    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert [e.code for e in errors] == ["503"]
    assert isinstance(events[-1], DoneEvent)
    assert events[-1].outcome.reason == "503"


async def test_transient_failure_after_the_empty_response_retry_reports_the_real_error(
    no_sleep: None,
) -> None:
    provider, toolbox = _after_tools_then_503(fallback=False)
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_provider_retries=1),
        session_key="agent:main:webchat:bounds",
        **toolbox.agent_kwargs(),
    )

    events = await _run(agent)

    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert [e.code for e in errors] == ["503"]
    assert "provider_stream_incomplete" not in {e.code for e in errors}


async def test_a_slow_failed_attempt_does_not_spend_the_whole_retry_budget() -> None:
    """A response that streams past the budget, then drops, is retried once."""
    provider = ScriptedProvider(
        [[Hang(0.3), error("503", _503)], [text("ok"), done()]],
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            retry_budget_seconds=0.2, retry_base_backoff_ms=10, retry_max_backoff_ms=20
        ),
        session_key="agent:main:webchat:bounds",
    )

    events = await _run(agent)

    assert len(provider.calls) == 2
    assert not [e for e in events if isinstance(e, ErrorEvent)]
    assert events[-1].outcome.kind == "completed"
