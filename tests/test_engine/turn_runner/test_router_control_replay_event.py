from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.engine.runtime import TurnRunner
from agentos.engine.steps import agentos_router as agentos_router_step
from agentos.engine.types import DoneEvent, RouterControlReplayEvent
from agentos.gateway.config import (
    AgentOSRouterConfig,
    GatewayConfig,
    _router_tier_profile_defaults,
)
from agentos.provider import (
    DoneEvent as ProviderDone,
)
from agentos.provider import (
    TextDeltaEvent as ProviderText,
)
from agentos.provider import ToolUseEndEvent as ProviderToolEnd
from agentos.provider import ToolUseStartEvent as ProviderToolStart
from agentos.tools import get_default_registry
from agentos.tools.types import CallerKind, ToolContext


class _Strategy:
    requires_history = True

    async def classify(
        self,
        message: str,
        valid_tiers: list[str],
        routing_history: list[dict] | None = None,
        **kwargs: object,
    ) -> tuple[str, float, str, dict]:
        return "c1", 0.9, "llm_judge", {"route_class": "R1"}


class _ReplayProvider:
    provider_name = "test"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.model = "base-model"

    def chat(self, messages: list[Any], tools=None, config=None) -> AsyncIterator[Any]:
        self.calls.append(self.model)
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            yield ProviderText(text="old partial")
            yield ProviderToolStart(tool_use_id="tool-1", tool_name="router_control")
            yield ProviderToolEnd(
                tool_use_id="tool-1",
                tool_name="router_control",
                arguments={
                    "action": "set_hold",
                    "target_id": "tier:c3",
                    "evidence": "use c3",
                },
            )
            yield ProviderDone(model=self.model)
            return
        yield ProviderText(text="new final")
        yield ProviderDone(model=self.model)

    async def list_models(self) -> list[Any]:
        return []


class _SelectorClone:
    def __init__(self, provider: _ReplayProvider) -> None:
        self.provider = provider
        self.current_config = SimpleNamespace(model=provider.model)

    def override_model(self, model: str) -> None:
        self.current_config = SimpleNamespace(model=model)
        self.provider.model = model

    def resolve(self) -> _ReplayProvider:
        return self.provider


class _Selector:
    def __init__(self, provider: _ReplayProvider) -> None:
        self.provider = provider

    def clone(self) -> _SelectorClone:
        return _SelectorClone(self.provider)


@pytest.mark.asyncio
async def test_router_control_replay_event_replays_turn_once(monkeypatch) -> None:
    monkeypatch.setattr(
        agentos_router_step, "_get_strategy", lambda _cfg, _llm_cfg=None: _Strategy()
    )
    provider = _ReplayProvider()
    cfg = GatewayConfig(
        agentos_router=AgentOSRouterConfig(
            enabled=True,
            rollout_phase="full",
            tiers=_router_tier_profile_defaults("openrouter"),
        )
    )
    runner = TurnRunner(
        provider_selector=_Selector(provider),
        tool_registry=get_default_registry(),
        config=cfg,
    )

    events = [
        event
        async for event in runner.run(
            "Use c3 for this",
            "agent:main:router-control-replay",
            tool_context=ToolContext(is_owner=True, caller_kind=CallerKind.CLI),
            history_has_persisted_user=False,
            no_memory_capture=True,
        )
    ]

    replay_events = [event for event in events if isinstance(event, RouterControlReplayEvent)]
    done_events = [event for event in events if isinstance(event, DoneEvent)]
    text = "".join(getattr(event, "text", "") for event in events if event.kind == "text_delta")

    assert len(replay_events) == 1
    assert replay_events[0].target_tier == "c3"
    assert provider.calls == ["minimax/minimax-m3", "anthropic/claude-opus-4.8"]
    assert done_events[-1].text == "new final"
    assert text.endswith("new final")
