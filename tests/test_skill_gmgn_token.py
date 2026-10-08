"""Every ``--order-by`` value ``gmgn-token`` recommends must be one the API accepts.

The ``token traders`` Combination Guide recommended ``--order-by
last_active_timestamp`` for "KOLs recently active". ``last_active_timestamp``
is a response field, not a sort key: the GMGN API answers
``HTTP 400 invalid order_by`` and lists the same five values as the skill's
own "`--order-by` Values" table, which is the one ``gmgn-cli`` documents too.
"""

from __future__ import annotations

import re
from pathlib import Path

SKILL_MD = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "gmgn-token"
    / "SKILL.md"
)

ACCEPTED_ORDER_BY = {
    "amount_percentage",
    "profit",
    "unrealized_profit",
    "buy_volume_cur",
    "sell_volume_cur",
}

_BACKTICKED_RE = re.compile(r"`(\w+)`")
_VALUE_ROW_RE = re.compile(r"^\| `(\w+)` \|", re.MULTILINE)
_ORDER_BY_FLAG_RE = re.compile(r"--order-by (\w+)")


def _section(text: str, heading: str, next_heading: str) -> str:
    return text.split(heading, 1)[1].split(next_heading, 1)[0]


def _recommended_order_by(guide: str) -> list[set[str]]:
    """The backticked values in the last cell of each data row of the guide table."""
    rows = [line for line in guide.splitlines() if line.startswith("|")]
    return [set(_BACKTICKED_RE.findall(row.strip().strip("|").split("|")[-1])) for row in rows[2:]]


def test_order_by_values_table_lists_exactly_the_accepted_values() -> None:
    text = SKILL_MD.read_text(encoding="utf-8")
    table = _section(text, "### `--order-by` Values", "### `--tag` Values")

    assert set(_VALUE_ROW_RE.findall(table)) == ACCEPTED_ORDER_BY


def test_every_combination_guide_order_by_value_is_accepted() -> None:
    text = SKILL_MD.read_text(encoding="utf-8")
    holders_guide = _section(
        text, "### `--tag` + `--order-by` Combination Guide", "## Response Field Reference"
    )
    traders_guide = _section(
        text,
        "### `token traders` — `--tag` + `--order-by` Combination Guide",
        "### `token traders` — Find Active Traders",
    )

    for guide in (holders_guide, traders_guide):
        rows = _recommended_order_by(guide)
        assert rows, "the guide table was not found"
        for values in rows:
            assert values, "a guide row names no --order-by value"
            assert values <= ACCEPTED_ORDER_BY, values - ACCEPTED_ORDER_BY


def test_every_order_by_example_uses_an_accepted_value() -> None:
    text = SKILL_MD.read_text(encoding="utf-8")

    used = set(_ORDER_BY_FLAG_RE.findall(text))
    assert used, "no --order-by examples found"
    assert used <= ACCEPTED_ORDER_BY, used - ACCEPTED_ORDER_BY


def test_recently_active_is_sorted_client_side() -> None:
    text = SKILL_MD.read_text(encoding="utf-8")
    traders_guide = _section(
        text,
        "### `token traders` — `--tag` + `--order-by` Combination Guide",
        "### `token traders` — Find Active Traders",
    )

    assert "| `last_active_timestamp` |" not in traders_guide
    assert "sort the returned rows by their `last_active_timestamp` field yourself" in (
        " ".join(traders_guide.split())
    )
