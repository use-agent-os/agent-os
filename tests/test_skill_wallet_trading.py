"""Regression tests for the bundled wallet-trading skill."""

from pathlib import Path

from agentos.skills.loader import SkillLoader

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "src" / "agentos" / "skills" / "bundled"
SKILL_DIR = BUNDLED / "wallet-trading"


def test_wallet_trading_description_says_it_cannot_bridge_up_front() -> None:
    # Asked to bridge ETH from Base to Robinhood Chain, the agent opened the
    # skill four times, read every `--help`, searched the web, and posted the
    # wallet's address to a bridge's quote API before saying no. The refusal
    # sits at the head of the description because a crowded skills block
    # shortens every description from the end.
    spec = SkillLoader(bundled_dir=BUNDLED).get_by_name("wallet-trading")

    assert spec is not None
    head = spec.description[:200]
    assert "it cannot bridge between chains" in head
    assert "answered as not supported and nothing is run" in head


def test_wallet_trading_skill_declines_a_bridge_and_runs_nothing() -> None:
    skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    assert "**There is no bridge.** No command moves funds from one chain to another" in skill
    assert "is not an order and has no recipient to ask for" in skill
    assert "bridging is not supported for the AgentOS wallets yet, and stop" in skill
    assert "Run\nnothing for it: no `agentos` command, no web search" in skill
    assert "hands the wallet's address to a stranger" in skill
    assert "Do not recommend,\nname or link a bridge." in skill
    assert "- Never bridge, and never stand in for one" in skill
