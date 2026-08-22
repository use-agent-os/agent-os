from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime

import pytest

from agentos.engine.runtime import TurnRunner
from agentos.engine.types import ErrorEvent
from agentos.engine.usage import UsageTracker, parse_session_key_scope
from agentos.gateway.config import BudgetsConfig, GatewayConfig
from agentos.tools.types import ToolContext


def test_budgets_config_parsing() -> None:
    config = BudgetsConfig(
        session_limit=1.5,
        session_warn=0.5,
        daily_limit=10.0,
        daily_warn=5.0,
        agent_daily_limit={"main": 2.0},
        agent_daily_warn={"main": 1.0},
        channel_daily_limit={"telegram": 3.0},
        channel_daily_warn={"telegram": 1.5},
    )
    assert config.session_limit == 1.5
    assert config.session_warn == 0.5
    assert config.daily_limit == 10.0
    assert config.daily_warn == 5.0
    assert config.agent_daily_limit["main"] == 2.0
    assert config.agent_daily_warn["main"] == 1.0
    assert config.channel_daily_limit["telegram"] == 3.0
    assert config.channel_daily_warn["telegram"] == 1.5


def test_session_key_parsing() -> None:
    assert parse_session_key_scope("agent:main:main") == ("main", "system")
    assert parse_session_key_scope("agent:marketing:telegram:channel:123") == (
        "marketing",
        "telegram",
    )
    assert parse_session_key_scope("agent:foo:slack:direct:456") == ("foo", "slack")
    assert parse_session_key_scope("subagent:agent:bar:webchat:default") == ("bar", "webchat")
    assert parse_session_key_scope("invalid-key") == ("main", "system")


def test_daily_ledger_persistence() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_session.db")
        tracker = UsageTracker(default_provider_id="opencap", db_path=db_path)

        day = datetime.now(UTC).strftime("%Y-%m-%d")

        # Table should be created automatically
        assert tracker.get_spend(day, "gateway", "global") == 0.0

        # Record spend: model price lookup triggers cost calculation
        # Let's add tokens for a known model to record spend
        tracker.add(
            "agent:main:telegram:c1",
            input_tokens=10000,
            output_tokens=10000,
            model_id="oc-uncensored-1.0",
            provider_id="opencap",
        )

        cost = tracker.get_effective_session_cost("agent:main:telegram:c1")
        assert cost > 0.0

        # Ledger spend should match the calculated cost
        gateway_spend = tracker.get_spend(day, "gateway", "global")
        agent_spend = tracker.get_spend(day, "agent", "main")
        channel_spend = tracker.get_spend(day, "channel", "telegram")

        assert gateway_spend == pytest.approx(cost)
        assert agent_spend == pytest.approx(cost)
        assert channel_spend == pytest.approx(cost)

        # Persistence check: restart tracker and verify it reads back from the ledger
        tracker2 = UsageTracker(default_provider_id="opencap", db_path=db_path)
        assert tracker2.get_spend(day, "gateway", "global") == pytest.approx(cost)
        assert tracker2.get_spend(day, "agent", "main") == pytest.approx(cost)
        assert tracker2.get_spend(day, "channel", "telegram") == pytest.approx(cost)


def test_budget_limits_and_warnings() -> None:
    tracker = UsageTracker(default_provider_id="opencap")

    # Seed mock session cost
    tracker.add(
        "agent:main:slack:c1",
        input_tokens=1000000,
        output_tokens=1000000,
        model_id="oc-uncensored-1.0",
        provider_id="opencap",
    )
    cost = tracker.get_effective_session_cost("agent:main:slack:c1")

    # Case 1: Session warn threshold exceeded
    config = BudgetsConfig(session_warn=cost - 0.001)
    exceeded, msg = tracker.check_budget_limits("agent:main:slack:c1", config)
    assert exceeded is False
    assert "exceeds warning threshold" in msg

    # Repeat check: should not warn again (alert deduplication)
    exceeded, msg = tracker.check_budget_limits("agent:main:slack:c1", config)
    assert exceeded is False
    assert msg is None

    # Case 2: Session limit exceeded
    config = BudgetsConfig(session_limit=cost - 0.001)
    exceeded, msg = tracker.check_budget_limits("agent:main:slack:c1", config)
    assert exceeded is True
    assert "exceeds limit" in msg


@pytest.mark.asyncio
async def test_runner_budget_ceiling_fail_closed() -> None:
    # Set up a low budget configuration
    config = GatewayConfig()
    config.budgets = BudgetsConfig(session_limit=0.0001)

    tracker = UsageTracker(default_provider_id="opencap")
    tracker.add(
        "agent:main:main",
        input_tokens=100000,
        output_tokens=100000,
        model_id="oc-uncensored-1.0",
        provider_id="opencap",
    )

    runner = TurnRunner(
        provider_selector=None,
        session_manager=None,
        usage_tracker=tracker,
        config=config,
    )

    # Running a turn when session limit is already exceeded must fail closed immediately
    events: list[object] = []
    async for event in runner.run(
        message="hello",
        session_key="agent:main:main",
        tool_context=ToolContext(agent_id="main", session_key="agent:main:main"),
    ):
        events.append(event)

    assert len(events) == 1
    err = events[0]
    assert isinstance(err, ErrorEvent)
    assert err.code == "BUDGET_EXCEEDED"
    assert "exceeds limit" in err.message
