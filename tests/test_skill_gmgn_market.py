"""``gmgn-market``'s ``market signal`` chain list must agree with itself.

The Sub-commands table, the "Supported Chains" summary, and the sentence
directly above the `market signal` Parameters table all say signal supports
`sol` / `bsc` / `robinhood` / `arc` / `stable` -- but the `--chain` row inside
that same Parameters table said only `sol` / `bsc`, and the top-of-file
argument-hint said only `sol` / `bsc` / `robinhood`. The Parameters table is
what actually defines the CLI flag's accepted values, so an agent building a
`market signal --chain robinhood` (or arc / stable) call from that table
alone would believe the chain is rejected when the rest of the document says
it is supported.
"""

from __future__ import annotations

from pathlib import Path

SKILL_MD = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "gmgn-market"
    / "SKILL.md"
)

_SIGNAL_CHAINS = "`sol` / `bsc` / `robinhood` / `arc` / `stable`"


def test_market_signal_parameters_table_lists_every_supported_chain() -> None:
    text = SKILL_MD.read_text(encoding="utf-8")

    # The three places that already agreed before this fix.
    assert "sol / bsc / robinhood / arc / stable only. Max 50 results" in text
    assert f"signal: {_SIGNAL_CHAINS}" in text
    assert f"Chains: {_SIGNAL_CHAINS} only" in text

    # The Parameters table itself, previously narrower than all three above.
    assert f"| `--chain` | Yes | {_SIGNAL_CHAINS} |" in text


def test_argument_hint_offers_every_supported_signal_chain() -> None:
    text = SKILL_MD.read_text(encoding="utf-8")

    assert "signal --chain <sol|bsc|robinhood|arc|stable>" in text
