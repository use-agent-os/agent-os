"""``gmgn-portfolio`` must document the ``portfolio profits`` sub-command, and
describe its response the way the route actually returns it.

``gmgn-wallet-analysis/scripts/analyze.py`` shells out to
``gmgn-cli portfolio profits --chain <chain> --wallet <wallet> --period
<1d|all>`` as an input to its verdict, so the sub-command is real and
load-bearing -- yet ``gmgn-portfolio``, the skill that documents every other
``portfolio`` sub-command, never mentioned it.

The field reference has to match gmgn-cli 1.6.6's own ``gmgn-portfolio``
skill: the rows sit inside a ``{"list": [...]}`` envelope (read off the top
level, every field comes back missing and reads as ``0``), and every row
carries both the period-scoped fields and the all-time ``total_*`` fields
whatever ``--period`` is. ``--wallet`` is repeatable (1-100 wallets) and
``--period`` defaults to ``7d``.
"""

from __future__ import annotations

import re
from pathlib import Path

BUNDLED = Path(__file__).resolve().parent.parent / "src" / "agentos" / "skills" / "bundled"
SKILL_MD = BUNDLED / "gmgn-portfolio" / "SKILL.md"
WALLET_ANALYSIS_SCRIPT = BUNDLED / "gmgn-wallet-analysis" / "scripts" / "analyze.py"

PERIOD_FIELDS = ("realized_profit", "realized_profit_cost", "buy` / `sell")
ALL_TIME_FIELDS = (
    "total_realized_profit",
    "total_realized_profit_cost",
    "total_profit",
    "total_cost",
)


def _section(heading: str) -> str:
    """Body of the ``## ``/``### `` section titled ``heading``, up to the next heading."""
    text = SKILL_MD.read_text(encoding="utf-8")
    match = re.search(rf"^#+ {re.escape(heading)}\n(.*?)(?=^#+ |\Z)", text, re.M | re.S)
    assert match, f"missing section: {heading}"
    return match.group(1)


def test_profits_subcommand_is_used_by_the_real_script() -> None:
    """Sanity check on the premise: the dependency is real, not hypothetical."""
    script = WALLET_ANALYSIS_SCRIPT.read_text(encoding="utf-8")
    assert '"portfolio", "profits"' in script


def test_sub_commands_table_lists_profits() -> None:
    assert "| `portfolio profits` |" in _section("Sub-commands")


def test_profits_options_document_repeatable_wallet_and_period_default() -> None:
    options = _section("`portfolio profits` Options")

    wallet_row = next((line for line in options.splitlines() if "`--wallet" in line), "")
    assert "Repeatable" in wallet_row
    assert "1–100" in wallet_row

    period_row = next((line for line in options.splitlines() if "`--period" in line), "")
    assert "`1d` / `7d` / `30d` / `all`" in period_row
    assert "(default `7d`)" in period_row


def test_profits_response_envelope_is_documented() -> None:
    fields = _section("`portfolio profits` — Key Fields")
    assert '`{"list": [ {…} ]}`' in fields
    assert "`list[0]`" in fields
    assert "one row per `--wallet`" in fields


def test_every_response_carries_both_field_families() -> None:
    fields = _section("`portfolio profits` — Key Fields")

    # The reviewed-out claim: field names do not switch with --period.
    assert "differ by `--period`" not in fields
    assert "whatever `--period` is" in fields

    rows = {
        line.split("|")[1].strip(): line.split("|")[2].strip()
        for line in fields.splitlines()
        if line.startswith("| `")
    }
    for field in PERIOD_FIELDS:
        assert rows.get(f"`{field}`") == "selected period", field
    for field in ALL_TIME_FIELDS:
        assert rows.get(f"`{field}`") == "all-time", field
    assert "`unrealized_profit`" in rows


def test_response_fields_the_script_reads_are_documented() -> None:
    fields = _section("`portfolio profits` — Key Fields")
    for field in (
        "total_realized_profit",
        "total_realized_profit_cost",
        "unrealized_profit",
        "realized_profit",
        "realized_profit_cost",
    ):
        assert f"| `{field}` |" in fields, field
