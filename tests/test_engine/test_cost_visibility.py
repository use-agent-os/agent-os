import sqlite3
from pathlib import Path

import pytest

from agentos.engine.steps.agentos_router import _get_cheapest_compatible_tier
from agentos.engine.usage import UsageTracker, _current_skill_name, _current_tool_name
from agentos.gateway.rpc_usage import _handle_usage_cost


def test_usage_records_creation_and_query(tmp_path: Path):
    db_file = tmp_path / "test_sessions.db"
    tracker = UsageTracker(default_provider_id="openrouter", db_path=str(db_file))

    # Check that table is created
    conn = sqlite3.connect(str(db_file))
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='usage_records';")
    assert cursor.fetchone() is not None
    conn.close()

    # Record a usage event
    session_key = "agent:agent-1:telegram:session-1"
    tracker.add(
        session_key=session_key,
        model_id="anthropic/claude-3-haiku",
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=20,
        cache_write_tokens=10,
        billed_cost=0.0005,
        provider_id="openrouter",
    )

    # Let's query it
    rows = tracker.query_usage(agent_id="agent-1")
    assert len(rows) == 1
    row = rows[0]
    assert row["sessionKey"] == session_key
    assert row["agentId"] == "agent-1"
    assert row["channelType"] == "telegram"
    assert row["model"] == "anthropic/claude-3-haiku"
    assert row["provider"] == "openrouter"
    assert row["inputTokens"] == 100
    assert row["outputTokens"] == 50
    assert row["cacheReadTokens"] == 20
    assert row["cacheWriteTokens"] == 10
    assert row["billedCostUsd"] == 0.0005

    # Filter with mismatching fields
    assert len(tracker.query_usage(agent_id="agent-2")) == 0
    assert len(tracker.query_usage(channel_type="slack")) == 0


def test_context_attribution_in_usage_tracker(tmp_path: Path):
    db_file = tmp_path / "test_sessions.db"
    tracker = UsageTracker(default_provider_id="openrouter", db_path=str(db_file))

    # Set context vars
    tool_token = _current_tool_name.set("custom_tool")
    skill_token = _current_skill_name.set("custom_skill")

    try:
        session_key = "agent:myagent:slack:session-abc"
        tracker.add(
            session_key=session_key,
            model_id="google/gemini-pro",
            input_tokens=10,
            output_tokens=5,
        )
    finally:
        _current_tool_name.reset(tool_token)
        _current_skill_name.reset(skill_token)

    rows = tracker.query_usage(session_key=session_key)
    assert len(rows) == 1
    row = rows[0]
    assert row["toolName"] == "custom_tool"
    assert row["skill"] == "custom_skill"


def test_session_active_skill_attribution(tmp_path: Path):
    db_file = tmp_path / "test_sessions.db"
    tracker = UsageTracker(default_provider_id="openrouter", db_path=str(db_file))

    session_key = "agent:myagent:slack:session-abc"
    tracker._session_active_skill[session_key] = "git-skills"

    tracker.add(
        session_key=session_key,
        model_id="google/gemini-pro",
        input_tokens=10,
        output_tokens=5,
    )

    rows = tracker.query_usage(session_key=session_key)
    assert len(rows) == 1
    assert rows[0]["skill"] == "git-skills"


def test_cheapest_compatible_tier(monkeypatch):
    import agentos.engine.steps.agentos_router as ar
    from agentos.engine import pricing

    # Mock lookup_price using PriceEntry
    prices = {
        ("model-a", "prov"): pricing.PriceEntry(1.0, 2.0),
        ("model-b", "prov"): pricing.PriceEntry(10.0, 20.0),
        ("model-c", "prov"): pricing.PriceEntry(2.0, 4.0),
        ("model-d", "prov"): pricing.PriceEntry(50.0, 100.0),
    }

    def mock_lookup_price(model_id, provider_id=""):
        return prices.get((model_id, provider_id), pricing.PriceEntry(0.0, 0.0))

    monkeypatch.setattr(ar, "lookup_price", mock_lookup_price)

    tiers = {
        "c0": {"model": "model-a", "provider": "prov"},
        "c1": {"model": "model-b", "provider": "prov"},
        "c2": {"model": "model-c", "provider": "prov"},
        "c3": {"model": "model-d", "provider": "prov"},
    }
    valid_tiers = ["c0", "c1", "c2", "c3"]

    cheapest = _get_cheapest_compatible_tier("c1", tiers, valid_tiers)
    # Target is c1 (model-b, cost=30.0).
    # Compatibles are c1, c2, c3.
    # Prices: c1=30.0, c2=6.0, c3=150.0.
    # Cheapest compatible is c2 (6.0).
    assert cheapest == "c2"


@pytest.mark.asyncio
async def test_rpc_handle_usage_cost(tmp_path: Path):
    db_file = tmp_path / "test_sessions.db"
    tracker = UsageTracker(default_provider_id="openrouter", db_path=str(db_file))

    # Record some usage
    tracker.add(
        session_key="agent:agent-1:telegram:session-1",
        model_id="anthropic/claude-3-haiku",
        input_tokens=100,
        output_tokens=50,
        billed_cost=0.0005,
    )

    class MockRpcContext:
        usage_tracker = tracker
        session_manager = None

    ctx = MockRpcContext()
    res = await _handle_usage_cost({"startDate": "2020-01-01"}, ctx)
    assert "breakdown" in res
    assert "totalCostUsd" in res
    assert len(res["breakdown"]) == 1
    assert res["breakdown"][0]["agentId"] == "agent-1"
