"""Offline harness-plumbing tests for the T9 eval gate (Pilot vs v4).

Two layers, both fully offline / deterministic / network-free (AGENTS.md test
policy):

1. **Metric library** (``scripts/pilot_router/eval_lib``): every §6.4 metric —
   accuracy, under/over-routing, the per-transition severity-weighted
   under-routing table, per-class recall, macro-F1, confusion matrix, 15-bin
   ECE, NLL, and the paired bootstrap CI on the accuracy delta — pinned on tiny
   hand-computable fixtures.

2. **Replay driver** (``scripts/pilot_router/evaluate``): the conversation-
   ordered replay through the REAL ``apply_agentos_router`` step, driven by a
   stub strategy (no ONNX, no MiniLM) injected through the same ``_get_strategy``
   monkeypatch seam the engine tests use. This proves the driver (a) reads the
   engine's *final* decision (``routed_tier`` post-guards), (b) orders turns
   within a conversation by ``turn_id`` and accumulates history so the engine's
   anti-downgrade guard can fire, and (c) resets engine state between
   conversations.
"""

from __future__ import annotations

import math

import pytest

from scripts.pilot_router import eval_lib

# --------------------------------------------------------------------------- #
# Metric library
# --------------------------------------------------------------------------- #


def test_accuracy_and_routing_rates_on_hand_fixture() -> None:
    # gold, pred pairs: R2→R2 (correct), R3→R1 (under by 2), R0→R2 (over by 2),
    # R1→R1 (correct).
    gold = eval_lib.to_indices(["R2", "R3", "R0", "R1"])
    pred = eval_lib.to_indices(["R2", "R1", "R2", "R1"])
    assert eval_lib.accuracy(gold, pred) == 0.5
    assert eval_lib.under_routing_rate(gold, pred) == 0.25  # only R3→R1
    assert eval_lib.over_routing_rate(gold, pred) == 0.25  # only R0→R2


def test_severity_weighted_under_routing_uses_spec_table() -> None:
    # One of each penalised transition; correct + over-routing contribute 0.
    gold = eval_lib.to_indices(["R3", "R3", "R3", "R2", "R2", "R1", "R2", "R0"])
    pred = eval_lib.to_indices(["R0", "R1", "R2", "R0", "R1", "R0", "R2", "R3"])
    #                            3     2     1     2     1     1    (0)   (over)
    expected = (3 + 2 + 1 + 2 + 1 + 1) / 8
    assert eval_lib.severity_weighted_under_routing(gold, pred) == pytest.approx(expected)


def test_severity_table_adjacency_edge_cases() -> None:
    # R1→R0 is adjacent (=1), NOT the two-step R2→R0 (=2).
    assert eval_lib.SEVERITY_PENALTY[(1, 0)] == 1.0
    assert eval_lib.SEVERITY_PENALTY[(2, 0)] == 2.0
    assert eval_lib.SEVERITY_PENALTY[(3, 0)] == 3.0
    assert eval_lib.SEVERITY_PENALTY[(3, 2)] == 1.0  # adjacent from the top


def test_per_class_recall_and_macro_f1() -> None:
    gold = eval_lib.to_indices(["R0", "R0", "R1", "R2", "R2", "R3"])
    pred = eval_lib.to_indices(["R0", "R1", "R1", "R2", "R2", "R2"])
    rec = eval_lib.per_class_recall(gold, pred)
    assert rec["R0"] == pytest.approx(0.5)  # 1/2
    assert rec["R1"] == pytest.approx(1.0)  # 1/1
    assert rec["R2"] == pytest.approx(1.0)  # 2/2
    assert math.isnan(rec["R3"]) or rec["R3"] == 0.0  # 0/1 support → 0 recall
    assert rec["R3"] == 0.0
    # macro-F1 is finite and in [0,1].
    f1 = eval_lib.macro_f1(gold, pred)
    assert 0.0 <= f1 <= 1.0


def test_confusion_matrix_shape_and_counts() -> None:
    gold = eval_lib.to_indices(["R0", "R1", "R2", "R3", "R2"])
    pred = eval_lib.to_indices(["R0", "R2", "R2", "R3", "R1"])
    mat = eval_lib.confusion_matrix(gold, pred)
    assert len(mat) == 4 and all(len(r) == 4 for r in mat)
    assert mat[0][0] == 1  # R0→R0
    assert mat[1][2] == 1  # R1→R2
    assert mat[2][2] == 1 and mat[2][1] == 1  # R2 split
    assert mat[3][3] == 1  # R3→R3
    assert sum(sum(r) for r in mat) == 5


def test_ece_perfectly_calibrated_is_zero() -> None:
    # Confidence 1.0 and always correct → ECE 0.
    probs = [[0.0, 0.0, 1.0, 0.0], [1.0, 0.0, 0.0, 0.0]]
    gold = [2, 0]
    assert eval_lib.ece(probs, gold) == pytest.approx(0.0, abs=1e-9)


def test_ece_overconfident_wrong_is_one() -> None:
    # Confidence 1.0 but always wrong → ECE 1.0.
    probs = [[0.0, 0.0, 1.0, 0.0], [1.0, 0.0, 0.0, 0.0]]
    gold = [0, 2]
    assert eval_lib.ece(probs, gold) == pytest.approx(1.0, abs=1e-9)


def test_ece_and_nll_drop_missing_prob_rows() -> None:
    probs = [[0.0, 0.0, 1.0, 0.0], []]  # second row degraded (no vector)
    gold = [2, 1]
    # Only the first (correct, conf 1.0) row is scored → ECE 0, NLL ~0.
    assert eval_lib.ece(probs, gold) == pytest.approx(0.0, abs=1e-9)
    assert eval_lib.nll(probs, gold) == pytest.approx(0.0, abs=1e-6)


def test_nll_matches_hand_value() -> None:
    probs = [[0.5, 0.5, 0.0, 0.0]]
    gold = [0]
    assert eval_lib.nll(probs, gold) == pytest.approx(-math.log(0.5))


def test_bootstrap_ci_brackets_point_delta() -> None:
    # Pilot correct on 8/10, v4 on 6/10 → point delta +0.2; CI must bracket it.
    gold = [0] * 10
    pilot_pred = [0] * 8 + [1] * 2
    v4_pred = [0] * 6 + [1] * 4
    res = eval_lib.bootstrap_accuracy_delta(gold, pilot_pred, v4_pred, n_resamples=2000, seed=7)
    assert res.delta_point == pytest.approx(0.2)
    assert res.ci_low <= res.delta_point <= res.ci_high
    assert res.n_resamples == 2000


def test_bootstrap_is_deterministic_under_seed() -> None:
    gold = [0, 1, 2, 3, 0, 1, 2, 3]
    p = [0, 1, 2, 3, 0, 1, 1, 3]
    v = [0, 1, 1, 3, 0, 0, 2, 3]
    a = eval_lib.bootstrap_accuracy_delta(gold, p, v, n_resamples=500, seed=99)
    b = eval_lib.bootstrap_accuracy_delta(gold, p, v, n_resamples=500, seed=99)
    assert (a.ci_low, a.ci_high, a.delta_point) == (b.ci_low, b.ci_high, b.delta_point)


def test_compute_router_metrics_bundle() -> None:
    gold = eval_lib.to_indices(["R0", "R1", "R2", "R3"])
    pred = eval_lib.to_indices(["R0", "R1", "R2", "R2"])
    probs = [[1.0, 0, 0, 0], [0, 1.0, 0, 0], [0, 0, 1.0, 0], [0, 0, 1.0, 0]]
    m = eval_lib.compute_router_metrics(gold, pred, probs)
    assert m.n == 4
    assert m.accuracy == pytest.approx(0.75)
    assert m.under_routing_rate == pytest.approx(0.25)  # R3→R2
    d = m.to_dict()
    assert set(d) >= {
        "accuracy",
        "under_routing_rate",
        "over_routing_rate",
        "severity_weighted_under_routing",
        "per_class_recall",
        "macro_f1",
        "confusion_matrix",
        "ece",
        "nll",
    }


# --------------------------------------------------------------------------- #
# Replay driver plumbing (real apply_agentos_router, stub strategy)
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _restore_engine_seam() -> None:
    """Restore the engine's real ``_get_strategy`` + state after each test.

    ``replay_conversation`` injects a stub through the module-global seam; direct
    calls (not wrapped by ``replay_all``) would otherwise leak the stub into
    later tests that exercise the real dispatch (e.g. test_router_pilot_dispatch).
    """
    from agentos.engine.steps import agentos_router as step

    original = step._get_strategy
    yield
    step._get_strategy = original
    step._history_store.clear()
    step._strategy = None
    step._strategy_key = None


class _StubStrategy:
    """A history-aware stub returning a scripted route class per turn text.

    The text carries its intended route class as a ``[[RN]]`` marker so a
    conversation can drive a specific sequence of raw routes through the real
    engine step (and its history-dependent guards) with no ONNX/MiniLM.
    """

    requires_history = True
    source = "pilot_v1"

    async def classify(self, message, valid_tiers, routing_history=None, **kwargs):
        route_class = "R1"
        for marker in ("R0", "R1", "R2", "R3"):
            if f"[[{marker}]]" in message:
                route_class = marker
                break
        tier = {"R0": "c0", "R1": "c1", "R2": "c2", "R3": "c3"}[route_class]
        if tier not in valid_tiers:
            tier = valid_tiers[0]
        probs = {"R0": 0.0, "R1": 0.0, "R2": 0.0, "R3": 0.0}
        probs[route_class] = 1.0
        extra = {
            "route_class": route_class,
            "top1_label": route_class,
            "probabilities": probs,
            "thinking_mode": "T1",
            "prompt_policy": "P1",
            "flags": [],
            "safety_net_applied": False,
        }
        # Confidence 1.0 keeps the engine confidence gate inert so the raw route
        # reaches the final decision unless a *history* guard overrides it.
        return tier, 1.0, self.source, extra


@pytest.mark.asyncio
async def test_replay_reads_final_decision_and_accumulates_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.pilot_router import evaluate as ev

    strategy = _StubStrategy()
    result = await ev.replay_conversation(
        rows=[
            {"turn_id": "2", "text": "second [[R1]]"},  # deliberately out of order
            {"turn_id": "1", "text": "first [[R2]]"},
        ],
        strategy=strategy,
        session_key="conv-A",
    )
    # Ordered by turn_id: turn 1 (R2→c2) then turn 2 (raw R1→c1).
    assert [r["turn_id"] for r in result] == ["1", "2"]
    assert result[0]["final_tier"] == "c2"
    assert result[0]["final_route_class"] == "R2"
    # Turn 2's raw route is R1/c1, but the engine anti-downgrade guard holds the
    # prior c2 — proving history accumulated within the conversation and the
    # driver read the engine's FINAL (post-guard) decision, not the raw route.
    assert result[1]["base_route_class"] == "R1"
    assert result[1]["final_tier"] == "c2"
    assert result[1]["final_route_class"] == "R2"


@pytest.mark.asyncio
async def test_replay_resets_state_between_conversations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.pilot_router import evaluate as ev

    strategy = _StubStrategy()
    # Conversation A ends high (c2). Conversation B's first turn is a raw R1;
    # with state correctly reset it must NOT inherit A's c2.
    await ev.replay_conversation(
        rows=[{"turn_id": "1", "text": "hi [[R2]]"}],
        strategy=strategy,
        session_key="conv-A",
    )
    result_b = await ev.replay_conversation(
        rows=[{"turn_id": "1", "text": "hi [[R1]]"}],
        strategy=strategy,
        session_key="conv-B",
    )
    assert result_b[0]["final_tier"] == "c1"
    assert result_b[0]["final_route_class"] == "R1"


def test_order_conversation_turns_by_turn_id() -> None:
    from scripts.pilot_router import evaluate as ev

    rows = [
        {"turn_id": "101240", "text": "c"},
        {"turn_id": "101209", "text": "a"},
        {"turn_id": "101221", "text": "b"},
    ]
    ordered = ev.order_turns(rows)
    assert [r["text"] for r in ordered] == ["a", "b", "c"]
