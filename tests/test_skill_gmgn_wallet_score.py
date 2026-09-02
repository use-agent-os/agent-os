"""Regression test for a `0.0 vs missing` bug in the GMGN wallet-score skill.

score.py is a linear, argv-driven CLI script (reads sys.argv at module level,
then shells out to `gmgn-cli`), not structured for import-based testing the
way chain_stocks.py is. The bug under test lives entirely in two small, pure
helper functions, so this test extracts just their source rather than
executing -- or mocking an entire CLI invocation for -- the whole script.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = (
    ROOT / "src" / "agentos" / "skills" / "bundled" / "gmgn-wallet-score" / "scripts" / "score.py"
)


def _load_helpers() -> dict[str, Callable[..., Any]]:
    """Exec just the `_clamp` and `_or_default` function defs from score.py.

    Avoids executing the rest of the script, which reads sys.argv and shells
    out to the gmgn-cli binary at module level.
    """
    tree = ast.parse(_SCRIPT.read_text())
    wanted = {"_clamp", "_or_default"}
    found = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    }
    assert found.keys() == wanted, f"expected {wanted}, found {found.keys()}"
    module = ast.Module(body=list(found.values()), type_ignores=[])
    namespace: dict[str, Any] = {}
    exec(compile(module, filename=str(_SCRIPT), mode="exec"), namespace)
    return {name: namespace[name] for name in wanted}


_helpers = _load_helpers()
_clamp = _helpers["_clamp"]
_or_default = _helpers["_or_default"]


def test_or_default_preserves_a_genuine_zero() -> None:
    # The bug: `x or default` also replaces a legitimate 0.0 with `default`,
    # not just None. A wallet at exactly break-even ROI (0.0) is a real,
    # meaningful value -- not missing data -- and must be preserved.
    assert _or_default(0.0, 0.0001) == 0.0


def test_or_default_still_substitutes_for_none() -> None:
    assert _or_default(None, 0.0001) == 0.0001


def test_or_default_passes_through_other_values() -> None:
    assert _or_default(0.42, 0.0001) == 0.42
    assert _or_default(-0.15, 0.0001) == -0.15


def test_wallet_pct_zero_no_longer_gets_amplified_into_a_huge_backtest_number() -> None:
    """End-to-end shape of the actual bug: a dev wallet with bought_cost <= 0
    and roi == 0.0 exactly must produce copy_7d == 0.0, not an absurdly
    amplified figure from dividing by a clamped-up 0.0001 in place of the
    real 0.0.
    """
    bought_cost = 0.0
    realized_profit = 800.0
    roi = 0.0

    wallet_pct = (realized_profit / bought_cost) if bought_cost > 0 else roi
    wallet_pct = _clamp(_or_default(wallet_pct, 0.0001), -0.9, 3.0)
    assert wallet_pct == 0.0

    drift, slip, gas_pct = 0.012, 0.006, 0.004
    copy_pct = wallet_pct - drift - slip - gas_pct
    copy_7d = realized_profit * (copy_pct / wallet_pct) if wallet_pct else 0.0
    assert copy_7d == 0.0
