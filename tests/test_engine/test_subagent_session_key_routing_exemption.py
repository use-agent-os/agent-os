"""Test subagent session key routing and metadata exemptions across key shapes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentos.engine.pipeline import TurnContext
from agentos.engine.steps.agentos_router import apply_agentos_router
from agentos.gateway.rpc_sessions import _derive_source_metadata


@pytest.mark.parametrize(
    "subagent_key",
    [
        "subagent:agent:main:main:coder:1:abcd1234",
        "subagent:agent:ops:direct:user1",
        "subagent:worker-1",
        "agent:main:subagent:run-123",
        "agent:coder:subagent:task-99",
    ],
)
async def test_apply_agentos_router_skips_all_subagent_key_shapes(subagent_key: str) -> None:
    """All subagent session key formats must be exempted from router model override."""
    cfg = SimpleNamespace(
        agentos_router=SimpleNamespace(
            enabled=True,
            tiers={"c0": {"model": "cheap-model"}, "c1": {"model": "expensive-model"}},
            rollout_phase="enforce",
        )
    )
    ctx = TurnContext(
        message="Translate this text or write complex code",
        session_key=subagent_key,
        config=cfg,
        provider=None,
        model="custom-subagent-model",
        tool_defs=[],
        system_prompt="You are a helpful assistant.",
    )

    result_ctx = await apply_agentos_router(ctx)

    assert result_ctx.model == "custom-subagent-model"
    assert "routed_tier" not in result_ctx.metadata
    assert "routed_model" not in result_ctx.metadata


@pytest.mark.parametrize(
    "subagent_key",
    [
        "subagent:agent:main:main:coder:1:abcd1234",
        "subagent:worker-1",
        "agent:main:subagent:run-123",
    ],
)
def test_session_source_metadata_identifies_subagents(subagent_key: str) -> None:
    """Both prefix and infix subagent keys must report source_kind and channel_kind as subagent."""
    session = SimpleNamespace(
        session_key=subagent_key,
        origin=None,
        last_channel=None,
        channel=None,
        last_to=None,
    )

    kinds = _derive_source_metadata(session)

    assert kinds["source_kind"] == "subagent"
    assert kinds["sourceKind"] == "subagent"
    assert kinds["channel_kind"] == "subagent"
    assert kinds["channelKind"] == "subagent"
