"""Regression test: the "coins in profit" count must agree with the win rate
printed right next to it.

``compute()``'s own comment documents a real observed wallet: the 0-200%
multiplier bucket held 188 of 209 tokens while the win rate was 23.9% (which
implies about 50 winners). The bucket absorbs every token bought but not yet
sold -- realized ROI 0, sitting on the band's lower edge -- so its size is not
a count of wins. ``unsettled``/``dist_gap`` were added to state that
discrepancy in the chart caveat, but ``m["winners"]`` -- the number shown
directly to the user as "N of M coins in profit" -- kept summing the raw
bucket in, silently overcounting by exactly the unsettled amount.

``analyze.py`` does its work at import time only under ``__name__ ==
"__main__"``, so ``compute()`` can be exercised directly by loading the
module through ``importlib``, matching the sibling gmgn-wallet-score test's
approach for these hyphenated-directory scripts.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "gmgn-wallet-analysis"
    / "scripts"
    / "analyze.py"
)


def _load_analyze() -> Any:
    spec = importlib.util.spec_from_file_location("gmgn_analyze_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _wallet_data(*, token_num: int, winrate: float, buckets: dict[str, int]) -> dict[str, Any]:
    return {
        "stats_7d": {
            "buy": 20,
            "sell": 15,
            "bought_cost": 1000.0,
            "realized_profit": 100.0,
            "pnl_stat": {
                "token_num": token_num,
                "winrate": winrate,
                "avg_holding_period": 3600,
                "pnl_gt_5x_num": buckets["gt5"],
                "pnl_2x_5x_num": buckets["x2_5"],
                "pnl_0x_2x_num": buckets["x0_2"],
                "pnl_nd5_0x_num": buckets["n50_0"],
                "pnl_lt_nd5_num": buckets["lt_n50"],
            },
            "common": {},
        },
    }


def test_winners_matches_the_winrate_not_the_raw_0_to_200_percent_bucket() -> None:
    """The exact scenario compute()'s own comment describes: 188 of 209 tokens
    in the 0-200% band next to a 23.9% win rate implies ~50 winners, not 203."""
    analyze = _load_analyze()
    d = _wallet_data(
        token_num=209,
        winrate=0.239,
        buckets={"gt5": 5, "x2_5": 10, "x0_2": 188, "n50_0": 4, "lt_n50": 2},
    )

    m = analyze.compute(d, latency_s=1.0, my_size=0)

    assert m["winners"] == m["implied_winners"] == 50


def test_winners_is_exact_when_the_0_to_200_percent_bucket_has_no_unsettled_tokens() -> None:
    """When every token in the wallet has already been sold, the 0-200% bucket
    genuinely is a win count and the fix must not under-report it."""
    analyze = _load_analyze()
    d = _wallet_data(
        token_num=20,
        winrate=0.5,
        buckets={"gt5": 2, "x2_5": 3, "x0_2": 5, "n50_0": 6, "lt_n50": 4},
    )

    m = analyze.compute(d, latency_s=1.0, my_size=0)

    assert m["unsettled"] == 0
    assert m["winners"] == 10
