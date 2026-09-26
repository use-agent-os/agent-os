"""``gmgn-cooking``'s chain list must agree with itself.

"Supported Chains" and "Supported Launchpads by Chain" both list `robinhood`
(launchpads `trench` / `pons`), but the `cooking create` `--chain` parameter
table and the Guided Launch Flow's chain-selection table only offered
`sol` / `bsc` / `base` -- an agent following either of those two sections
literally (the parameter table is explicitly marked authoritative: "Do NOT
guess field names or values") would refuse a valid Robinhood launch request.
"""

from __future__ import annotations

from pathlib import Path

SKILL_MD = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "gmgn-cooking"
    / "SKILL.md"
)


def test_chain_parameter_table_includes_every_supported_chain() -> None:
    text = SKILL_MD.read_text(encoding="utf-8")

    assert "`sol` / `bsc` / `base` / `robinhood`" in text  # Supported Chains
    assert "| `--chain` | Yes | Chain: `sol` / `bsc` / `base` / `robinhood` |" in text


def test_guided_launch_flow_offers_every_supported_launchpad() -> None:
    text = SKILL_MD.read_text(encoding="utf-8")

    assert "| `robinhood` | `trench`, `pons`" in text  # Supported Launchpads by Chain
    assert "`trench`" in text.split("### Step 1")[1].split("### Step 2")[0]
    assert "`pons`" in text.split("### Step 1")[1].split("### Step 2")[0]
