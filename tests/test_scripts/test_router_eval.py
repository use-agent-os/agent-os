"""Unit tests for the router eval harness KPI math and dataset floors.

The eval is the spec's gate (D7): its ``under_route_rate`` is the primary
KPI for the ML-vs-judge decision, so the pure rank/metric functions and the
committed dataset minimums must be pinned — a silent off-by-one or a dataset
trim would otherwise produce wrong ground-truth numbers with no CI signal.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "tests" / "data" / "router_eval"


def _load_router_eval():
    """Import scripts/router_eval.py by path (it is not an installed module)."""
    spec = importlib.util.spec_from_file_location(
        "router_eval_under_test", REPO_ROOT / "scripts" / "router_eval.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


router_eval = _load_router_eval()


# ---------------------------------------------------------------------------
# Rank tables
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("route_class", "expected"),
    [("R0", 0), ("R1", 1), ("R2", 2), ("R3", 3), ("R9", -1), (None, -1), ("", -1)],
)
def test_class_rank(route_class, expected) -> None:
    assert router_eval._class_rank(route_class) == expected


@pytest.mark.parametrize(
    ("tier", "expected"),
    [("c0", 0), ("c1", 1), ("c2", 2), ("c3", 3), ("c9", -1), (None, -1), ("", -1)],
)
def test_tier_rank(tier, expected) -> None:
    assert router_eval._tier_rank(tier) == expected


def test_class_and_tier_ranks_align() -> None:
    # R0->c0 ... R3->c3: the two rank tables must index identically so the
    # directional KPI compares like-for-like.
    for cls, tier in zip(["R0", "R1", "R2", "R3"], ["c0", "c1", "c2", "c3"], strict=True):
        assert router_eval._class_rank(cls) == router_eval._tier_rank(tier)


# ---------------------------------------------------------------------------
# Directional metrics (primary KPI: under_route_rate = P(pred < gold))
# ---------------------------------------------------------------------------


def test_directional_metrics_empty() -> None:
    metrics = router_eval._directional_metrics([])
    assert metrics == {
        "count": 0,
        "accuracy": None,
        "under_route_rate": None,
        "over_route_rate": None,
    }


def test_directional_metrics_under_over_exact() -> None:
    # pairs are (gold_rank, pred_rank).
    # (2,1) -> under (pred below gold); (1,1) -> exact; (0,2) -> over.
    metrics = router_eval._directional_metrics([(2, 1), (1, 1), (0, 2)])
    assert metrics["count"] == 3
    assert metrics["accuracy"] == round(1 / 3, 4)
    assert metrics["under_route_rate"] == round(1 / 3, 4)
    assert metrics["over_route_rate"] == round(1 / 3, 4)


def test_under_route_rate_is_strictly_pred_below_gold() -> None:
    # All exact -> zero under-route, and equality must NOT count as under.
    metrics = router_eval._directional_metrics([(3, 3), (2, 2), (0, 0)])
    assert metrics["under_route_rate"] == 0.0
    assert metrics["over_route_rate"] == 0.0
    assert metrics["accuracy"] == 1.0

    # Every prediction one tier below gold -> full under-route.
    metrics = router_eval._directional_metrics([(3, 2), (2, 1), (1, 0)])
    assert metrics["under_route_rate"] == 1.0
    assert metrics["over_route_rate"] == 0.0


def test_case_metrics_filters_invalid_ranks() -> None:
    rows = [
        {"gold_class": "R2", "pred_class": "R1", "final_tier": "c1"},  # under both views
        {"gold_class": "R1", "pred_class": "R1", "final_tier": "c1"},  # exact both views
        {"gold_class": "R0", "pred_class": "bogus", "final_tier": "c9"},  # dropped both views
        {"gold_class": "R3", "pred_class": "R3", "final_tier": None},  # dropped applied only
    ]
    metrics = router_eval._case_metrics(rows)
    # classification view: 3 valid pred classes (R1, R1, R3), bogus dropped.
    assert metrics["classification"]["count"] == 3
    # applied view: 3 valid tiers (c1, c1, c9-dropped, None-dropped) -> c1,c1 only.
    assert metrics["applied"]["count"] == 2
    assert metrics["classification"]["under_route_rate"] == round(1 / 3, 4)


# ---------------------------------------------------------------------------
# Dataset floors (spec D7 "minimal credible eval")
# ---------------------------------------------------------------------------


def _load_jsonl(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_cases_dataset_meets_d7_floors() -> None:
    cases = _load_jsonl(DATA_DIR / "cases.jsonl")
    assert len(cases) >= 120, "D7 requires >=120 labeled turns"

    vietnamese = [c for c in cases if c["lang"] == "vi"]
    assert len(vietnamese) >= 40, "D7 requires >=40 Vietnamese cases"

    # D7 explicitly requires the Vietnamese slice to include *short-but-hard*
    # prompts (short production/delete/debug asks whose difficulty the length
    # belies). This is the most spec-load-bearing slice — the whole ML-vs-judge
    # decision rests on the judge getting these right — so guard it directly.
    # Beyond length + hard gold class, require the risk/destructiveness
    # dimension D7 names via tags (high_risk/debug): without it a future edit
    # could swap these for short but merely mildly-nontrivial R2s (e.g. a short
    # opinion question) and the floor would still pass green, eroding the exact
    # slice while claiming to guard it.
    risk_tags = {"high_risk", "debug"}
    vietnamese_short_hard = [
        c
        for c in vietnamese
        if len(c["message"]) < 80
        and c["gold_class"] in {"R2", "R3"}
        and risk_tags & set(c.get("tags", []))
    ]
    assert len(vietnamese_short_hard) >= 5, (
        "D7 requires short-but-hard Vietnamese prompts (short production/delete/"
        "debug asks with gold R2/R3 carrying a high_risk or debug tag); found "
        f"{len(vietnamese_short_hard)}"
    )

    long_cases = [c for c in cases if len(c["message"]) > 6000]
    assert len(long_cases) >= 3, "D7 requires >=3 long-pasted-log cases (>6000 chars)"

    for case in cases:
        assert {"id", "message", "gold_class", "lang", "tags"} <= set(case)
        assert case["gold_class"] in {"R0", "R1", "R2", "R3"}


def test_sessions_dataset_meets_d7_floor() -> None:
    sessions = _load_jsonl(DATA_DIR / "sessions.jsonl")
    assert len(sessions) >= 15, "D7 requires >=15 simulated multi-turn sessions"
    for session in sessions:
        assert {"id", "turns"} <= set(session)
        assert len(session["turns"]) >= 2, "a session needs a hard turn + follow-up"
        for turn in session["turns"]:
            assert {"message", "gold_class"} <= set(turn)


# ---------------------------------------------------------------------------
# Stability KPI: _run_sessions downgrade-within-window math (finding #7)
# ---------------------------------------------------------------------------


def _script_route_turn(monkeypatch, tiers_by_message: dict[str, str]) -> None:
    """Replace _route_turn so _run_sessions sees deterministic tiers/classes.

    The stability math (downgrade_within_window_rate) lives entirely in
    _run_sessions, not in the tested pure helpers; scripting the router lets us
    pin its window flag and division without a live judge.
    """
    tier_to_class = {"c0": "R0", "c1": "R1", "c2": "R2", "c3": "R3"}

    async def _fake(config, message, session_key):  # noqa: ANN001, ARG001
        tier = tiers_by_message[message]
        return {
            "route_class": tier_to_class[tier],
            "final_route_class": tier_to_class[tier],
            "final_tier": tier,
            "base_tier": tier,
            "confidence": 1.0,
            "source": "llm_judge",
            "applied": True,
            "anti_downgrade_applied": False,
            "complaint_upgrade_applied": False,
            "confidence_gate_applied": False,
        }

    monkeypatch.setattr(router_eval, "_route_turn", _fake)
    monkeypatch.setattr(router_eval, "_reset_router_state", lambda *a, **k: None)


@pytest.mark.asyncio
async def test_run_sessions_flags_followup_downgrade_within_window(monkeypatch) -> None:
    # Hard turn (c2) then a follow-up that drops to c0 -> a within-window
    # downgrade. Turn 0 is never counted as a follow-up (index > 0 gate).
    _script_route_turn(monkeypatch, {"hard": "c2", "followup": "c0"})
    sessions = [
        {"id": "s1", "turns": [{"message": "hard", "gold_class": "R2"},
                               {"message": "followup", "gold_class": "R2"}]},
    ]
    report = await router_eval._run_sessions(_config_stub(), sessions)
    assert report["followup_turn_count"] == 1  # turn 0 excluded
    assert report["downgrade_count"] == 1
    assert report["downgrade_within_window_rate"] == 1.0
    assert report["sessions_with_downgrade"] == 1


@pytest.mark.asyncio
async def test_run_sessions_no_downgrade_when_tier_holds_or_rises(monkeypatch) -> None:
    # Follow-ups that hold (c2) or rise (c3) are NOT downgrades; equality must
    # not count (strict < against max prior tier rank).
    _script_route_turn(monkeypatch, {"t0": "c2", "hold": "c2", "rise": "c3"})
    sessions = [
        {"id": "s1", "turns": [{"message": "t0", "gold_class": "R2"},
                               {"message": "hold", "gold_class": "R2"},
                               {"message": "rise", "gold_class": "R3"}]},
    ]
    report = await router_eval._run_sessions(_config_stub(), sessions)
    assert report["followup_turn_count"] == 2
    assert report["downgrade_count"] == 0
    assert report["downgrade_within_window_rate"] == 0.0
    assert report["sessions_with_downgrade"] == 0


@pytest.mark.asyncio
async def test_run_sessions_window_is_versus_running_max_prior_tier(monkeypatch) -> None:
    # c3 then c0 (downgrade) then c1: the second follow-up compares against the
    # running max prior tier (c3), so c1 < c3 is also a downgrade -> 2/2.
    _script_route_turn(monkeypatch, {"peak": "c3", "drop": "c0", "partial": "c1"})
    sessions = [
        {"id": "s1", "turns": [{"message": "peak", "gold_class": "R3"},
                               {"message": "drop", "gold_class": "R3"},
                               {"message": "partial", "gold_class": "R3"}]},
    ]
    report = await router_eval._run_sessions(_config_stub(), sessions)
    assert report["followup_turn_count"] == 2
    assert report["downgrade_count"] == 2
    assert report["downgrade_within_window_rate"] == 1.0


@pytest.mark.asyncio
async def test_run_sessions_rate_is_none_without_followups(monkeypatch) -> None:
    _script_route_turn(monkeypatch, {"only": "c1"})
    sessions = [{"id": "s1", "turns": [{"message": "only", "gold_class": "R1"}]}]
    report = await router_eval._run_sessions(_config_stub(), sessions)
    assert report["followup_turn_count"] == 0
    assert report["downgrade_within_window_rate"] is None


def _config_stub():
    """_run_sessions only threads config into the (patched) _route_turn."""
    return object()


# ---------------------------------------------------------------------------
# Consistency KPI: _agreement class-agreement math (finding #7)
# ---------------------------------------------------------------------------


def test_agreement_all_runs_agree() -> None:
    runs = [
        [{"id": "a", "pred_class": "R1"}, {"id": "b", "pred_class": "R2"}],
        [{"id": "a", "pred_class": "R1"}, {"id": "b", "pred_class": "R2"}],
        [{"id": "a", "pred_class": "R1"}, {"id": "b", "pred_class": "R2"}],
    ]
    result = router_eval._agreement(runs)
    assert result["repeats"] == 3
    assert result["case_count"] == 2
    assert result["class_agreement_rate"] == 1.0
    assert result["disagreeing_case_ids"] == []


def test_agreement_flags_disagreeing_cases() -> None:
    # Case "a" agrees across runs; case "b" disagrees on one run.
    runs = [
        [{"id": "a", "pred_class": "R1"}, {"id": "b", "pred_class": "R2"}],
        [{"id": "a", "pred_class": "R1"}, {"id": "b", "pred_class": "R3"}],
    ]
    result = router_eval._agreement(runs)
    assert result["case_count"] == 2
    assert result["class_agreement_rate"] == 0.5
    assert result["disagreeing_case_ids"] == ["b"]


def test_agreement_none_pred_counts_as_a_distinct_value() -> None:
    # A None prediction differing from a real class is a disagreement.
    runs = [
        [{"id": "a", "pred_class": "R1"}],
        [{"id": "a", "pred_class": None}],
    ]
    result = router_eval._agreement(runs)
    assert result["disagreeing_case_ids"] == ["a"]
    assert result["class_agreement_rate"] == 0.0


def test_agreement_empty_runs_yields_none_rate() -> None:
    result = router_eval._agreement([])
    assert result["case_count"] == 0
    assert result["class_agreement_rate"] is None
