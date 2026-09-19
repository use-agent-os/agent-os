"""gmgn-wallet-analysis ``compute()`` -- the "N of M coins in profit" count (#2797).

The 0-200% bucket also holds every token bought and not yet sold (realized ROI 0 sits on its
lower edge), so summing it in raw printed 203 winners next to a 23.9% win rate. Only that band
is ambiguous: a token past 2x is a realized win whatever the win rate says, so the count keeps
those and caps the band at the wins the win rate leaves for it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src/agentos/skills/bundled/gmgn-wallet-analysis/scripts/analyze.py"


@pytest.fixture(scope="module")
def analyze() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_gmgn_wallet_analysis_analyze", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _metrics(analyze: ModuleType, *, winrate: float | None, gt5: int, x2_5: int, x0_2: int) -> dict:
    pnl: dict = {
        "token_num": gt5 + x2_5 + x0_2 + 6,
        "avg_holding_period": 3600,
        "pnl_gt_5x_num": gt5,
        "pnl_2x_5x_num": x2_5,
        "pnl_0x_2x_num": x0_2,
        "pnl_nd5_0x_num": 4,
        "pnl_lt_nd5_num": 2,
    }
    if winrate is not None:
        pnl["winrate"] = winrate
    data = {
        "stats_7d": {
            "buy": 20,
            "sell": 15,
            "bought_cost": 1000.0,
            "realized_profit": 100.0,
            "pnl_stat": pnl,
            "common": {},
        }
    }
    return analyze.compute(data, latency_s=1.0, my_size=0)


def test_winners_agrees_with_the_win_rate_not_the_raw_band(analyze: ModuleType) -> None:
    """The issue's wallet: 188 of 209 tokens in the band beside a 23.9% win rate."""
    m = _metrics(analyze, winrate=0.239, gt5=5, x2_5=10, x0_2=188)

    assert m["token_num"] == 209
    assert m["winners"] == m["implied_winners"] == 50


def test_winners_is_the_raw_sum_when_the_band_holds_no_unsettled_tokens(
    analyze: ModuleType,
) -> None:
    m = _metrics(analyze, winrate=30 / 36, gt5=5, x2_5=10, x0_2=15)

    assert m["winners"] == 30


def test_winners_never_drops_below_the_tokens_past_2x_when_the_win_rate_is_missing(
    analyze: ModuleType,
) -> None:
    """An absent win rate reads as 0; "0 coins in profit" beside 15 tokens past 2x is wrong."""
    m = _metrics(analyze, winrate=None, gt5=5, x2_5=10, x0_2=188)

    assert m["implied_winners"] == 0
    assert m["winners"] == 15


def test_winners_never_drops_below_the_tokens_past_2x_when_the_win_rate_lags(
    analyze: ModuleType,
) -> None:
    m = _metrics(analyze, winrate=0.02, gt5=5, x2_5=10, x0_2=188)

    assert m["implied_winners"] == 4
    assert m["winners"] == 15


def test_winners_never_exceeds_the_three_profit_buckets(analyze: ModuleType) -> None:
    m = _metrics(analyze, winrate=1.0, gt5=5, x2_5=10, x0_2=20)

    assert m["winners"] == 35
