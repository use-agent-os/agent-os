"""Unit tests for ``HistoryLoadStage`` driven directly (no full
TurnRunner stack).

The stage no longer compacts: the before-turn T3-upgrade / DB-preflight /
emergency paths were folded into the agent's single ``CompactionService``
(see ``tests/test_engine/test_compaction_service.py``). What is left is the
history load and the durable-summary prepend, driven here with recording
fakes for the two ports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.engine.turn_runner.history_load_stage import (
    HistoryLoadStage,
    HistoryLoadStageInput,
)
from agentos.engine.turn_runner.outcome import StageOutcome

# ---------------------------------------------------------------------------
# Recording fakes (one per port)
# ---------------------------------------------------------------------------


@dataclass
class _RecordingHistoryLoader:
    return_value: str | None = None
    raises: type[BaseException] | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def load(self, **kwargs: Any) -> str | None:
        self.calls.append(dict(kwargs))
        if self.raises is not None:
            raise self.raises("recording history loader boom")
        return self.return_value


@dataclass
class _RecordingPrepender:
    return_value: str | None | object = object()
    calls: list[dict[str, Any]] = field(default_factory=list)

    def prepend(self, **kwargs: Any) -> str | None:
        self.calls.append(dict(kwargs))
        if isinstance(self.return_value, object) and not isinstance(
            self.return_value, str | type(None)
        ):
            # Default: replicate the production helper output for assert parity
            existing = kwargs.get("existing")
            prepended = kwargs.get("prepended")
            if not prepended or not prepended.strip():
                return existing
            if not existing or not existing.strip():
                return prepended.strip()
            return f"{prepended.strip()}\n\n{existing.strip()}"
        return self.return_value  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _make_agent_stub(
    *,
    request_context_prompt: str | None = None,
) -> Any:
    """Build a minimal Agent-shape with the attributes the stage reads."""
    return SimpleNamespace(
        config=SimpleNamespace(request_context_prompt=request_context_prompt),
    )


def _make_input(
    *,
    agent: Any | None = None,
    request_context_prompt: str | None = None,
    session_key: str = "agent:main:s1",
    agent_id: str = "agent:main",
    history_has_persisted_user: bool = True,
) -> HistoryLoadStageInput:
    if agent is None:
        agent = _make_agent_stub(request_context_prompt=request_context_prompt)
    return HistoryLoadStageInput(
        agent=agent,
        session_key=session_key,
        agent_id=agent_id,
        history_has_persisted_user=history_has_persisted_user,
    )


def _make_stage(
    *,
    history: _RecordingHistoryLoader | None = None,
    prepender: _RecordingPrepender | None = None,
) -> tuple[HistoryLoadStage, _RecordingHistoryLoader, _RecordingPrepender]:
    history = history or _RecordingHistoryLoader()
    prepender = prepender or _RecordingPrepender()
    stage = HistoryLoadStage(
        history_loader=history,
        request_context_prepender=prepender,
    )
    return stage, history, prepender


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage_loads_history_then_prepends_once() -> None:
    stage, history, prepender = _make_stage(
        history=_RecordingHistoryLoader(return_value=None),
    )
    outcome = await stage.run(_make_input())

    assert isinstance(outcome, StageOutcome)
    assert outcome.terminate is False
    assert outcome.output is not None
    assert outcome.output.compaction_summary_context is None
    assert outcome.output.final_request_context_prompt is None
    assert len(history.calls) == 1
    assert len(prepender.calls) == 1


def test_stage_has_no_compaction_ports() -> None:
    """Before-turn compaction lives in the agent's CompactionService now."""
    stage, _, _ = _make_stage()
    assert not hasattr(stage, "_t3_upgrade")
    assert not hasattr(stage, "_preflight")


@pytest.mark.asyncio
async def test_history_loader_returns_summary_context() -> None:
    stage, _history, prepender = _make_stage(
        history=_RecordingHistoryLoader(return_value="SUMMARY1"),
    )
    outcome = await stage.run(_make_input(request_context_prompt="EXISTING"))

    assert outcome.output.compaction_summary_context == "SUMMARY1"
    # Default prepender replicates production: "<prepended>\n\n<existing>"
    assert outcome.output.final_request_context_prompt == "SUMMARY1\n\nEXISTING"
    assert prepender.calls[0]["existing"] == "EXISTING"
    assert prepender.calls[0]["prepended"] == "SUMMARY1"


@pytest.mark.asyncio
async def test_history_loader_called_with_trim_last_user_true() -> None:
    stage, history, _ = _make_stage()
    await stage.run(_make_input(history_has_persisted_user=True))
    assert history.calls[0]["trim_last_user"] is True


@pytest.mark.asyncio
async def test_history_loader_called_with_trim_last_user_false() -> None:
    stage, history, _ = _make_stage()
    await stage.run(_make_input(history_has_persisted_user=False))
    assert history.calls[0]["trim_last_user"] is False


@pytest.mark.asyncio
async def test_history_loader_receives_agent_and_session_key() -> None:
    agent = _make_agent_stub()
    stage, history, _ = _make_stage()
    await stage.run(_make_input(agent=agent, session_key="agent:main:s9"))
    assert history.calls[0]["agent"] is agent
    assert history.calls[0]["session_key"] == "agent:main:s9"


@pytest.mark.asyncio
async def test_history_loader_exception_propagates() -> None:
    """HistoryLoader exceptions propagate; the stage does not catch them."""
    stage, _, prepender = _make_stage(
        history=_RecordingHistoryLoader(raises=RuntimeError),
    )
    with pytest.raises(RuntimeError):
        await stage.run(_make_input())
    assert prepender.calls == []


@pytest.mark.asyncio
async def test_prepender_receives_existing_and_prepended() -> None:
    stage, _, prepender = _make_stage(
        history=_RecordingHistoryLoader(return_value="SUM"),
    )
    await stage.run(_make_input(request_context_prompt="EXIST"))
    assert prepender.calls == [{"existing": "EXIST", "prepended": "SUM"}]


@pytest.mark.asyncio
async def test_prepender_when_summary_is_none() -> None:
    stage, _, prepender = _make_stage(
        history=_RecordingHistoryLoader(return_value=None),
    )
    out = await stage.run(_make_input(request_context_prompt="EXIST"))
    assert prepender.calls == [{"existing": "EXIST", "prepended": None}]
    # The default prepender mirrors production: when prepended is None, the
    # existing string is returned unchanged.
    assert out.output.final_request_context_prompt == "EXIST"


@pytest.mark.asyncio
async def test_stage_does_not_mutate_agent_config() -> None:
    """Harness owns the agent.config.request_context_prompt mutation."""
    agent = _make_agent_stub(request_context_prompt="EXISTING")
    stage, _, _ = _make_stage(
        history=_RecordingHistoryLoader(return_value="SUMMARY"),
    )
    outcome = await stage.run(_make_input(agent=agent))
    # Agent.config.request_context_prompt unchanged by the stage; harness
    # applies output.final_request_context_prompt afterwards.
    assert agent.config.request_context_prompt == "EXISTING"
    assert outcome.output.final_request_context_prompt == "SUMMARY\n\nEXISTING"
