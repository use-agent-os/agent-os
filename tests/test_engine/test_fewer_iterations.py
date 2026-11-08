"""Fewer provider round trips: parallel tool calls, batching nudge, effort by tier."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
import structlog

from agentos.engine import Agent, AgentConfig
from agentos.engine.runtime import TurnRunner
from agentos.engine.steps.agentos_router import (
    _record_controller_thinking_metadata,
    _record_thinking_metadata,
)
from agentos.engine.types import ThinkingLevel
from agentos.gateway.config import AgentOSRouterConfig, ToolsConfig
from agentos.identity.prompt import assemble_system_prompt
from agentos.identity.types import AgentIdentity, AgentProfile
from agentos.provider import ChatConfig, Message
from agentos.provider import DoneEvent as ProviderDone
from agentos.provider import TextDeltaEvent as ProviderText
from agentos.router_tiers import effort_for_tier, tier_effort_ceiling

# ---------------------------------------------------------------------------
# parallel tool calls on the request the engine builds
# ---------------------------------------------------------------------------


class _ConfigCapturingProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.configs: list[ChatConfig | None] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.configs.append(config)
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderText(text="ok")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


@pytest.mark.asyncio
async def test_agent_requests_parallel_tool_calls_by_default() -> None:
    provider = _ConfigCapturingProvider()
    agent = Agent(provider=provider, config=AgentConfig())

    _ = [event async for event in agent.run_turn("hello")]

    assert provider.configs[0] is not None
    assert provider.configs[0].parallel_tool_calls is True


@pytest.mark.asyncio
async def test_agent_sends_nothing_when_parallel_calls_disabled() -> None:
    provider = _ConfigCapturingProvider()
    agent = Agent(provider=provider, config=AgentConfig(parallel_tool_calls=False))

    _ = [event async for event in agent.run_turn("hello")]

    assert provider.configs[0] is not None
    # None, not False: switching it off restores the old request exactly.
    assert provider.configs[0].parallel_tool_calls is None


def test_tools_parallel_calls_config_reaches_agent_config() -> None:
    assert ToolsConfig().parallel_calls is True
    for value in (True, False):
        runner = TurnRunner(
            provider_selector=None,
            config=SimpleNamespace(tools=ToolsConfig(parallel_calls=value)),
        )
        aux = TurnRunner.build_auxiliaries(
            runner,
            agent_id="main",
            session_key="agent:main:webchat:t",
            session_id_for_log=None,
            turn=SimpleNamespace(metadata={}),
        )
        assert aux.parallel_tool_calls is value


# ---------------------------------------------------------------------------
# batching nudge in the system prompt
# ---------------------------------------------------------------------------

_NUDGE = (
    "- When several tool calls are independent of each other, issue them together in one "
    "batch; the runtime can execute them concurrently.\n"
)


def test_batching_nudge_is_in_the_tool_section_once_and_stable() -> None:
    first = assemble_system_prompt(
        AgentProfile(agent_id="main", identity=AgentIdentity(name="A")),
        tools=["read_file", "exec"],
    )
    second = assemble_system_prompt(
        AgentProfile(agent_id="other", identity=AgentIdentity(name="B")),
        tools=["read_file", "exec", "web_search"],
    )
    for prompt in (first, second):
        assert prompt.count(_NUDGE) == 1
        tool_section = prompt.split("## Tool Call Style", 1)[1].split("\n## ", 1)[0]
        assert _NUDGE in tool_section
        assert _NUDGE.isascii()
    # No tools, no nudge.
    bare = assemble_system_prompt(AgentProfile(agent_id="main"), tools=[])
    assert _NUDGE not in bare


# ---------------------------------------------------------------------------
# effort by router tier
# ---------------------------------------------------------------------------


def test_tier_effort_table() -> None:
    assert tier_effort_ceiling("c0") == "low"
    assert tier_effort_ceiling("t0") == "low"  # legacy alias
    assert tier_effort_ceiling("c1") == "medium"
    assert tier_effort_ceiling("c2") is None
    assert tier_effort_ceiling("c3") is None
    assert tier_effort_ceiling("image_model") is None
    assert tier_effort_ceiling(None) is None

    assert effort_for_tier("c0", "high") == "low"
    assert effort_for_tier("c0", "minimal") == "minimal"  # never raised
    assert effort_for_tier("c1", "xhigh") == "medium"
    assert effort_for_tier("c1", "low") == "low"
    assert effort_for_tier("c3", "high") == "high"
    assert effort_for_tier("c0", "adaptive") == "adaptive"


def _runner(*, effort_by_tier: bool = True, llm_thinking: Any = None) -> TurnRunner:
    return TurnRunner(
        provider_selector=None,
        config=SimpleNamespace(
            llm=SimpleNamespace(thinking=llm_thinking),
            agentos_router=AgentOSRouterConfig(effort_by_tier=effort_by_tier),
        ),
    )


def _turn(tier: str, level: str = "high", **extra: Any) -> SimpleNamespace:
    metadata = {
        "thinking_requested": True,
        "thinking_level": level,
        "thinking_level_source": "tier_default",
        "routed_tier": tier,
        "routing_applied": True,
    }
    metadata.update(extra)
    return SimpleNamespace(metadata=metadata)


@pytest.mark.parametrize(
    ("tier", "expected"),
    [
        ("c0", ThinkingLevel.LOW),
        ("c1", ThinkingLevel.MEDIUM),
        ("c2", ThinkingLevel.HIGH),
        ("c3", ThinkingLevel.HIGH),
        ("image_model", ThinkingLevel.HIGH),
    ],
)
def test_router_tier_caps_tier_thinking_level(tier: str, expected: ThinkingLevel) -> None:
    assert _runner()._resolve_turn_thinking(_turn(tier)) == expected


def test_effort_by_tier_logs_the_lowered_level() -> None:
    with structlog.testing.capture_logs() as logs:
        _runner()._resolve_turn_thinking(_turn("c0"))
        _runner()._resolve_turn_thinking(_turn("c3"))
    events = [e for e in logs if e["event"] == "turn_runner.effort_by_tier"]
    assert len(events) == 1
    assert events[0]["tier"] == "c0"
    # ``thinking_level`` on purpose: ``level`` is structlog's own key and is
    # overwritten by the log level, which hid the value in the gateway log.
    assert events[0]["thinking_level"] == "low"
    assert "level" not in events[0] or events[0]["level"] != "low"
    assert events[0]["tier_level"] == "high"
    assert events[0]["source"] == "tier_default"


def test_effort_by_tier_switch_off_keeps_tier_level() -> None:
    assert AgentOSRouterConfig().effort_by_tier is True
    runner = _runner(effort_by_tier=False)
    assert runner._resolve_turn_thinking(_turn("c0")) == ThinkingLevel.HIGH


def test_effort_by_tier_leaves_explicit_levels_alone() -> None:
    # [llm] thinking is explicit and wins before the router is consulted.
    assert _runner(llm_thinking="xhigh")._resolve_turn_thinking(_turn("c0")) == (
        ThinkingLevel.XHIGH
    )
    # A controller-chosen level is router-owned too and is capped the same way
    # (pilot-v1 sets it on every routed turn, so this is the default path).
    controller = _turn("c0", thinking_level_source="controller")
    assert _runner()._resolve_turn_thinking(controller) == ThinkingLevel.LOW
    # An explicit model overrode the route: the tier says nothing about it.
    overridden = _turn("c0", routing_applied=False)
    assert _runner()._resolve_turn_thinking(overridden) == ThinkingLevel.HIGH
    # An operator-configured tier level is respected.
    configured = _turn("c1", thinking_level_source="tier_config")
    assert _runner()._resolve_turn_thinking(configured) == ThinkingLevel.HIGH
    # The pre-fix "tier" tag is no longer capped: only a shipped level is.
    assert _runner()._resolve_turn_thinking(_turn("c0", thinking_level_source="tier")) == (
        ThinkingLevel.HIGH
    )
    # Metadata from before this change (no source) is untouched.
    legacy = _turn("c0")
    del legacy.metadata["thinking_level_source"]
    assert _runner()._resolve_turn_thinking(legacy) == ThinkingLevel.HIGH
    # No thinking requested: still off.
    assert _runner()._resolve_turn_thinking(SimpleNamespace(metadata={})) is False


def test_router_records_where_the_thinking_level_came_from() -> None:
    router_cfg = SimpleNamespace(auto_thinking=True)
    tier_ctx = SimpleNamespace(metadata={})
    _record_thinking_metadata(tier_ctx, router_cfg, {"thinking_level": "high"}, tier_name="c1")
    assert tier_ctx.metadata["thinking_level_source"] == "tier_default"

    ctrl_ctx = SimpleNamespace(metadata={})
    _record_controller_thinking_metadata(ctrl_ctx, router_cfg, {"thinking_level": "high"}, "T3")
    if "thinking_level" in ctrl_ctx.metadata:
        assert ctrl_ctx.metadata["thinking_level_source"] == "controller"

    fallback_ctx = SimpleNamespace(metadata={})
    _record_controller_thinking_metadata(
        fallback_ctx, router_cfg, {"thinking_level": "high"}, None, tier_name="c0"
    )
    assert fallback_ctx.metadata["thinking_level_source"] == "tier_default"


def test_tier_thinking_source_tells_shipped_levels_from_operator_levels() -> None:
    # openai ships c1 at "low": an operator who raises it to "high" set it.
    openai = AgentOSRouterConfig(tier_profile="openai", tiers={"c1": {"thinking_level": "high"}})
    ctx = SimpleNamespace(metadata={})
    _record_thinking_metadata(ctx, openai, openai.tiers["c1"], tier_name="c1")
    assert ctx.metadata["thinking_level_source"] == "tier_config"
    assert (
        _runner()._resolve_turn_thinking(
            _turn("c1", thinking_level_source=ctx.metadata["thinking_level_source"])
        )
        == ThinkingLevel.HIGH
    )

    # The shipped level on the same profile is still a default.
    shipped = AgentOSRouterConfig(tier_profile="openai")
    ctx = SimpleNamespace(metadata={})
    _record_thinking_metadata(ctx, shipped, shipped.tiers["c2"], tier_name="c2")
    assert ctx.metadata["thinking_level_source"] == "tier_default"

    # The default table ships c1 at "high": that is the level effort by tier caps.
    default = AgentOSRouterConfig()
    ctx = SimpleNamespace(metadata={})
    _record_thinking_metadata(ctx, default, default.tiers["c1"], tier_name="c1")
    assert ctx.metadata["thinking_level_source"] == "tier_default"
    # A level that differs from the shipped one is the operator's.
    lowered = AgentOSRouterConfig(tiers={**default.tiers, "c1": {"thinking_level": "xhigh"}})
    ctx = SimpleNamespace(metadata={})
    _record_thinking_metadata(ctx, lowered, lowered.tiers["c1"], tier_name="c1")
    assert ctx.metadata["thinking_level_source"] == "tier_config"


def test_router_control_hold_keeps_the_tier_thinking_level() -> None:
    held = _turn("c1", router_control_hold_applied=True)
    assert _runner()._resolve_turn_thinking(held) == ThinkingLevel.HIGH


@pytest.mark.asyncio
async def test_a_model_hold_is_not_capped_by_effort_by_tier() -> None:
    """The user hand-picked a model: its host tier level must not be lowered."""
    from agentos.engine.pipeline import TurnContext
    from agentos.engine.steps.agentos_router import apply_agentos_router
    from agentos.router_control import (
        RouterControlHoldStore,
        resolve_router_control_model_target,
    )

    cfg = AgentOSRouterConfig(enabled=True, rollout_phase="full", auto_thinking=True)
    target = resolve_router_control_model_target(cfg, "anthropic/claude-opus-5.5")
    store = RouterControlHoldStore()
    store.set_hold("agent:main:effort-hold", target, evidence="use opus")
    ctx = TurnContext(
        message="review this",
        session_key="agent:main:effort-hold",
        config=SimpleNamespace(agentos_router=cfg),
        provider=None,
        model="default-model",
        tool_defs=[],
        system_prompt="system",
        metadata={"router_control_hold_store": store},
    )
    out = await apply_agentos_router(ctx)
    assert out.metadata["router_control_hold_applied"] is True
    assert out.metadata["thinking_level_source"] == "router_control"

    runner = TurnRunner(
        provider_selector=None,
        config=SimpleNamespace(llm=SimpleNamespace(thinking=None), agentos_router=cfg),
    )
    assert runner._resolve_turn_thinking(SimpleNamespace(metadata=out.metadata)) == (
        ThinkingLevel.HIGH
    )
