"""Regression tests for the bundled Robinhood Agentic Trading skill."""

from pathlib import Path

from agentos.skills.loader import SkillLoader

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "src" / "agentos" / "skills" / "bundled"
SKILL_DIR = BUNDLED / "robinhood-agentic-trading"


def test_robinhood_agentic_trading_skill_loads() -> None:
    spec = SkillLoader(bundled_dir=BUNDLED).get_by_name("robinhood-agentic-trading")

    assert spec is not None
    assert "Robinhood Trading MCP" in spec.description
    assert "Use the live Robinhood Trading MCP schemas" in spec.content


def test_robinhood_agentic_trading_skill_has_execution_guardrails() -> None:
    skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    assert "Never invent a tool name" in skill
    assert "Label the preview **Not submitted**" in skill
    assert "Require explicit current-turn confirmation" in skill
    assert "Never retry a placement blindly" in skill
    assert "never expose account numbers" in skill


def test_robinhood_agentic_trading_skill_declares_mcp_dependency() -> None:
    metadata = (SKILL_DIR / "agents" / "openai.yaml").read_text(encoding="utf-8")
    skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    assert 'value: "robinhood-trading"' in metadata
    assert 'transport: "streamable_http"' in metadata
    assert 'url: "https://agent.robinhood.com/mcp/trading"' in metadata
    assert "live authenticated MCP schemas as authoritative" in skill
    assert "Trade placement is restricted to the dedicated Robinhood Agentic account" in skill
