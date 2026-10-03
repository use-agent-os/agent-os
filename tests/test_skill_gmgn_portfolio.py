"""``gmgn-portfolio``'s Sub-commands table must list every sub-command the
GMGN CLI actually has.

``gmgn-wallet-analysis/scripts/analyze.py`` shells out to
``gmgn-cli portfolio profits --chain <chain> --wallet <wallet> --period
<1d|7d|all>`` as a "Fatal" input to its verdict, and reads
``total_realized_profit`` / ``total_realized_profit_cost`` /
``unrealized_profit`` / ``realized_profit`` off the response -- so the
sub-command is real, working, and load-bearing. ``gmgn-portfolio`` is the
skill that documents every other ``portfolio`` sub-command, but never
mentioned ``profits`` at all, so nothing pointed a reader there directly to
``gmgn-portfolio`` at it.
"""

from __future__ import annotations

from pathlib import Path

SKILL_MD = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "gmgn-portfolio"
    / "SKILL.md"
)

WALLET_ANALYSIS_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "gmgn-wallet-analysis"
    / "scripts"
    / "analyze.py"
)


def test_profits_subcommand_is_used_by_the_real_script() -> None:
    """Sanity check on the premise: the dependency is real, not hypothetical."""
    script = WALLET_ANALYSIS_SCRIPT.read_text(encoding="utf-8")
    assert '"portfolio", "profits"' in script


def test_sub_commands_table_lists_profits() -> None:
    text = SKILL_MD.read_text(encoding="utf-8")
    assert "| `portfolio profits` |" in text


def test_profits_period_values_match_what_the_script_relies_on() -> None:
    text = SKILL_MD.read_text(encoding="utf-8")
    assert "`1d` / `7d` / `30d` / `all`" in text


def test_profits_response_fields_the_script_reads_are_documented() -> None:
    text = SKILL_MD.read_text(encoding="utf-8")
    for field in (
        "total_realized_profit",
        "total_realized_profit_cost",
        "unrealized_profit",
        "realized_profit",
    ):
        assert f"`{field}`" in text
