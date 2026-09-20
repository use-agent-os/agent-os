"""``gmgn-token``'s canonical ``--order-by`` values must cover every value its
own Combination Guide tables recommend.

The ``token traders`` "`--tag` + `--order-by` Combination Guide" table
recommends ``--order-by last_active_timestamp`` for "KOLs recently active",
but the dedicated "`--order-by` Values" table -- the one this file's own
"Do NOT guess field names or values" rule points an agent at -- only listed
five values and did not include it.
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

_COMBO_ORDER_BY_RE = re.compile(r"\| `(\w+)` \|\s*$", re.MULTILINE)


def _order_by_values_table(text: str) -> str:
    start = text.index("### `--order-by` Values")
    end = text.index("### `--tag` Values")
    return text[start:end]


def test_every_combination_guide_order_by_value_is_in_the_canonical_table() -> None:
    text = SKILL_MD.read_text(encoding="utf-8")
    canonical = _order_by_values_table(text)

    holders_guide = text.split("### `--tag` + `--order-by` Combination Guide")[1].split(
        "## Response Field Reference"
    )[0]
    traders_guide = text.split("### `token traders` — `--tag` + `--order-by` Combination Guide")[
        1
    ].split("### `token traders` — Find Active Traders")[0]

    used_values = set(_COMBO_ORDER_BY_RE.findall(holders_guide)) | set(
        _COMBO_ORDER_BY_RE.findall(traders_guide)
    )

    assert "last_active_timestamp" in used_values  # sanity: the guide still recommends it
    missing = {v for v in used_values if f"`{v}`" not in canonical}
    assert not missing, (
        f"Combination Guide uses --order-by values missing from the table: {missing}"
    )
