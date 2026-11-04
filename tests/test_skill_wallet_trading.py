"""Regression tests for the bundled wallet-trading skill."""

from pathlib import Path

import pytest

from agentos.skills.loader import SkillLoader
from agentos.skills.outline import ALWAYS_MARKER

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "src" / "agentos" / "skills" / "bundled"
SKILL_MD = BUNDLED / "wallet-trading" / "SKILL.md"

# ``[skills].max_skill_view_chars`` defaults to 10,000: past that ``skill_view``
# hands the model the opening sections plus an index, so the 40 KB version of
# this skill never showed an agent its LP, DCA, trigger, bracket or bridge
# commands. The budget keeps the whole file within two views and the pinned
# Rules within every view.
MAX_SKILL_BYTES = 14_000

# One ``agentos trade <cmd>`` per capability the skill must spell out in the
# exact command vocabulary — a renamed or dropped row is a capability an
# ordinary chat agent can no longer reach.
CAPABILITY_COMMANDS = [
    "agentos trade status",
    "agentos trade quote",
    "agentos trade swap",
    "agentos trade send",
    "agentos trade allowances",
    "agentos trade revoke",
    "agentos trade decode",
    "agentos trade network",
    "agentos trade portfolio",
    "agentos trade history",
    "agentos trade lp pool",
    "agentos trade lp add",
    "agentos trade dca create",
    "agentos trade trigger create",
    "agentos trade protect",
    "agentos trade bridge quote",
    "agentos trade bridge create",
    "agentos trade bridge status",
]


@pytest.fixture(scope="module")
def skill() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


def test_wallet_trading_description_leads_with_execution_and_bridge() -> None:
    # A crowded skills block shortens every description from the end, so the
    # risk label, the bridge capability and its approval gate sit at the head.
    spec = SkillLoader(bundled_dir=BUNDLED).get_by_name("wallet-trading")

    assert spec is not None
    assert spec.description.startswith("[FINANCIAL EXECUTION]")
    head = spec.description[:200]
    assert "bridge ETH or USDC↔USDG" in head
    assert "waits for the user" in head
    assert "NOT for: GMGN meme-coin trading (gmgn-swap)" in spec.description
    assert "bridge" in spec.triggers


def test_wallet_trading_skill_fits_the_skill_view_budget(skill: str) -> None:
    # Measured on the text, not the file: a Windows checkout with autocrlf
    # adds a byte per line, and the view budget is counted on the text.
    assert len(skill.replace("\r\n", "\n").encode("utf-8")) <= MAX_SKILL_BYTES


def test_wallet_trading_rules_are_pinned(skill: str) -> None:
    lines = skill.splitlines()
    heading = lines.index("## Rules")
    assert lines[heading - 1] == ALWAYS_MARKER
    rules = skill[skill.index("## Rules") : skill.index("## Capabilities")]
    assert len(rules) <= 1_500


@pytest.mark.parametrize("command", CAPABILITY_COMMANDS)
def test_wallet_trading_skill_names_each_capability(skill: str, command: str) -> None:
    assert command in skill


def test_wallet_trading_skill_documents_the_bridge(skill: str) -> None:
    assert "## Bridge (Base ⇄ Robinhood)" in skill
    assert "`base` → `robinhood` | `ETH` / `USDC` | ETH / USDG" in skill
    assert "`robinhood` → `base` | `ETH` / `USDG` | ETH / USDC" in skill
    assert "ALWAYS `awaiting_approval`" in skill
    assert "at most\n   5 times, ≥ 10 s apart" in skill
    assert "trading.bridge.unsupported_route" in skill
    assert "trading.bridge.provider" in skill
    assert (
        "`trade bridge create` is the only way funds change chains: never send to a\n"
        "  bridge contract yourself, never swap into a wrapped stand-in, nothing but\n"
        "  ETH or USDC↔USDG."
    ) in skill
    assert "`token-burner` skill" in skill


def test_wallet_trading_skill_no_longer_forbids_bridging(skill: str) -> None:
    assert "There is no bridge" not in skill
    assert "cannot bridge" not in skill
    assert "Never bridge" not in skill
