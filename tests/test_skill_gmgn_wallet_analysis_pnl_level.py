"""Issue #3378: gmgn-wallet-analysis labelled a wallet's 7d "style" from a
fabricated 0.0 ROI when the real 7d ROI is undefined.

``stats_roi`` (which produces ``m["roi_7d"]``) returns ``None`` when the API's 7-day
window has no cost basis -- ordinary for a wallet that sold a position it bought
earlier, without buying anything new this week. Its own docstring says this is
"unknown", never "zero return", and ``m["form"]`` (computed right next to it) already
reads the same condition as "cannot tell". ``pnl_level`` was the one place in the file
that defaulted a ``None`` ``roi_7d`` to ``0.0`` instead, which fed a confident P3-tier
label ("has not turned into anything") into the report's "style" line -- directly
contradicting the "cannot tell" gate a few lines below it in the same output, even when
the wallet's ``realized_profit`` for the window was a large positive number.

The script lives under a hyphenated directory (``gmgn-wallet-analysis``), so it is
loaded via ``importlib.util`` rather than a normal import; the CLI end-to-end test
below runs as a subprocess for the same reason and to exercise ``main()``'s
``--fixture`` path, which needs no network.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    REPO_ROOT
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "gmgn-wallet-analysis"
    / "scripts"
    / "analyze.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("gmgn_analyze_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _metrics(mod, **overrides):
    """A minimal, valid ``m`` dict for ``pnl_level`` -- enough fields, sane defaults."""
    base = {
        "roi_7d": 0.03,
        "winrate": 0.4,
        "lt50_share": 0.1,
        "token_num": 8,
        "realized_7d": 30.0,
    }
    base.update(overrides)
    return base


def test_a_none_roi_7d_returns_no_level_instead_of_a_fabricated_p3() -> None:
    mod = _load_module()

    plevel, basis = mod.pnl_level(_metrics(mod, roi_7d=None))

    assert plevel is None
    assert basis is None


def test_a_genuinely_flat_roi_still_gets_labelled_p3() -> None:
    """Guard: this fix must not swallow the real, small-but-known ROI case."""
    mod = _load_module()

    plevel, basis = mod.pnl_level(_metrics(mod, roi_7d=0.03))

    assert plevel == "P3"


def test_a_strongly_positive_roi_still_reaches_p5_with_a_corroborator() -> None:
    """Guard: the P5 path (winrate/heavy-loss corroborators) is unaffected."""
    mod = _load_module()

    plevel, basis = mod.pnl_level(
        _metrics(mod, roi_7d=0.6, winrate=0.7, lt50_share=0.05, token_num=10)
    )

    assert plevel == "P5"
    assert basis is not None


def test_style_title_returns_none_when_roi_7d_is_undefined() -> None:
    """style_title must not KeyError on TITLES[(freq, None)] and must not label."""
    mod = _load_module()

    m = {
        "trades": 10,
        "token_num": 8,
        "per_day": 10 / 7.0,
        "roi_7d": None,
        "winrate": 0.4,
        "lt50_share": 0.1,
        "realized_7d": 8000.0,
    }

    assert mod.style_title(m) is None


def test_style_title_still_labels_a_wallet_with_a_known_roi() -> None:
    mod = _load_module()

    m = {
        "trades": 10,
        "token_num": 8,
        "per_day": 10 / 7.0,
        "roi_7d": 0.03,
        "winrate": 0.4,
        "lt50_share": 0.1,
        "realized_7d": 30.0,
    }

    result = mod.style_title(m)

    assert result is not None
    emoji, name, gloss, cell = result
    assert cell == "L2×P3"


# ── end-to-end: the full report must stop contradicting itself ───────────────


_FIXTURE = {
    "_wallet": "TestWallet222",
    "_chain": "sol",
    "stats_7d": {
        "buy": 0,
        "sell": 10,
        "bought_cost": 0,
        "total_cost": 0,
        "realized_profit": 8000,
        "pnl_stat": {"token_num": 8, "winrate": 0.7, "avg_holding_period": 500000},
        "common": {
            "created_token_count": 0,
            "followers_count": 10,
            "created_at": 1700000000,
            "fund_from": "",
            "fund_from_address": "",
            "fund_amount": 0,
            "follow_count": 5,
            "tags": [],
        },
    },
    "stats_30d": {},
    "profits_1d": {},
    "profits_all": {"total_realized_profit_cost": 20000, "total_realized_profit": 6000},
    "holdings": [],
    "activity": [],
}


def test_the_3378_repro_no_longer_contradicts_the_cannot_tell_gate(tmp_path: Path) -> None:
    fixture_path = tmp_path / "wallet.json"
    fixture_path.write_text(json.dumps(_FIXTURE), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--fixture", str(fixture_path), "en"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "cannot tell" in out  # the gate's own read of the same undefined 7d ROI
    assert "has not turned into anything" not in out
    assert "lukewarm" not in out
    assert "**style**" not in out  # the report prints no style line at all now
