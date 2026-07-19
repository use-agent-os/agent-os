#!/usr/bin/env python
"""Pure, offline-testable metric library for the Pilot-vs-v4 eval gate (T9).

This module holds the *pure* half of ``evaluate.py`` — every §6.4 gate metric as
a side-effect-free function over gold/pred label arrays and probability
matrices, plus the bootstrap CI on the accuracy delta. It imports nothing from
the gateway config or the engine, so the harness plumbing can be unit-tested on
tiny synthetic fixtures with no network, no ONNX weights, and no MiniLM encoder
(spec §11 T9: "CI smoke: harness plumbing on a tiny synthetic fixture").

The locked class order is ``["R0", "R1", "R2", "R3"]`` with integer labels
``0..3`` — the production ONNX contract, shared with ``train_lib``. Both the
Pilot and the v4 final decisions are mapped back onto this 4-class space (the
tier ``cN`` maps to class ``RN``) before any metric is computed, so the two
routers are scored by identical code.

Severity-weighted under-routing (spec §6.4 gate row) uses the **per-transition
penalty table** written in the spec:

    "R3→R0=3, R3→R1/R2→R0=2, adjacent=1"

interpreted as a penalty that grows with the drop distance AND the gold class's
stakes (see :data:`SEVERITY_PENALTY`). Only under-routing (pred strictly below
gold) is penalised; over-routing and correct routing score 0. The gate is
relative (Pilot ≤ v4), so the two routers are compared under this identical
table.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

CLASSES: tuple[str, str, str, str] = ("R0", "R1", "R2", "R3")
CLASS_TO_INT: dict[str, int] = {c: i for i, c in enumerate(CLASSES)}
ECE_BINS = 15

#: Per-transition under-routing severity penalty, keyed ``(gold_idx, pred_idx)``
#: with ``pred_idx < gold_idx``. Encodes the spec §6.4 table verbatim:
#:
#:   * ``R3→R0`` (3→0)                       → 3   (top class dropped to the floor)
#:   * ``R3→R1``, ``R3→R2``, ``R2→R0``       → 2   (a serious drop: from R3, or all
#:                                                  the way down to R0)
#:   * every *adjacent* drop (Δ = 1)         → 1   (R1→R0, R2→R1, R3→R2)
#:
#: ``R1→R0`` is adjacent (Δ=1) so it is 1, NOT 2 — the "…/R2→R0" clause in the
#: spec names the two-step ``R2→R0``, not the one-step ``R1→R0``.
SEVERITY_PENALTY: dict[tuple[int, int], float] = {
    (3, 0): 3.0,
    (3, 1): 2.0,
    (3, 2): 1.0,  # adjacent
    (2, 0): 2.0,
    (2, 1): 1.0,  # adjacent
    (1, 0): 1.0,  # adjacent
}

#: Legacy train-side severity weights (``train_lib.SEVERITY_WEIGHT_BY_CLASS``),
#: kept so the report can also show the continuity number alongside the gate's
#: per-transition table.
SEVERITY_WEIGHT_BY_CLASS: dict[str, float] = {"R0": 1.0, "R1": 1.0, "R2": 2.0, "R3": 3.0}


def _as_int(label: Any) -> int:
    """Map an ``R0..R3`` label (or an already-int 0..3) to its class index."""
    if isinstance(label, str):
        return CLASS_TO_INT[label]
    idx = int(label)
    if idx not in (0, 1, 2, 3):
        raise ValueError(f"class index out of range: {label!r}")
    return idx


def to_indices(labels: list[Any]) -> list[int]:
    return [_as_int(x) for x in labels]


def accuracy(gold: list[int], pred: list[int]) -> float:
    if not gold:
        return 0.0
    return sum(1 for g, p in zip(gold, pred, strict=True) if g == p) / len(gold)


def under_routing_rate(gold: list[int], pred: list[int]) -> float:
    """Fraction of turns where the router picked a strictly cheaper tier."""
    if not gold:
        return 0.0
    return sum(1 for g, p in zip(gold, pred, strict=True) if p < g) / len(gold)


def over_routing_rate(gold: list[int], pred: list[int]) -> float:
    """Fraction of turns where the router picked a strictly costlier tier."""
    if not gold:
        return 0.0
    return sum(1 for g, p in zip(gold, pred, strict=True) if p > g) / len(gold)


def severity_weighted_under_routing(gold: list[int], pred: list[int]) -> float:
    """Mean per-transition under-routing penalty (spec §6.4 table).

    Sums :data:`SEVERITY_PENALTY` over every under-routed turn and divides by the
    turn count. Correct and over-routed turns contribute 0.
    """
    if not gold:
        return 0.0
    total = 0.0
    for g, p in zip(gold, pred, strict=True):
        if p < g:
            total += SEVERITY_PENALTY[(g, p)]
    return total / len(gold)


def per_class_recall(gold: list[int], pred: list[int]) -> dict[str, float]:
    out: dict[str, float] = {}
    for cls, idx in CLASS_TO_INT.items():
        support = sum(1 for g in gold if g == idx)
        if support == 0:
            out[cls] = float("nan")
            continue
        hit = sum(1 for g, p in zip(gold, pred, strict=True) if g == idx and p == idx)
        out[cls] = hit / support
    return out


def per_class_precision(gold: list[int], pred: list[int]) -> dict[str, float]:
    out: dict[str, float] = {}
    for cls, idx in CLASS_TO_INT.items():
        selected = sum(1 for p in pred if p == idx)
        if selected == 0:
            out[cls] = float("nan")
            continue
        hit = sum(1 for g, p in zip(gold, pred, strict=True) if g == idx and p == idx)
        out[cls] = hit / selected
    return out


def macro_f1(gold: list[int], pred: list[int]) -> float:
    rec = per_class_recall(gold, pred)
    prec = per_class_precision(gold, pred)
    f1s: list[float] = []
    for cls in CLASSES:
        r = rec[cls]
        p = prec[cls]
        if r != r or p != p:  # NaN (no support / none selected)
            f1s.append(0.0)
            continue
        f1s.append(0.0 if (p + r) == 0 else 2 * p * r / (p + r))
    return sum(f1s) / len(f1s) if f1s else 0.0


def confusion_matrix(gold: list[int], pred: list[int]) -> list[list[int]]:
    """Gold-by-pred 4x4 confusion matrix (rows = gold class, cols = pred)."""
    k = len(CLASSES)
    mat = [[0] * k for _ in range(k)]
    for g, p in zip(gold, pred, strict=True):
        mat[g][p] += 1
    return mat


def ece(probs: list[list[float]], gold: list[int], *, bins: int = ECE_BINS) -> float:
    """Expected Calibration Error over ``bins`` equal-width top-1 confidence bins.

    Mirrors ``train_lib._ece`` exactly: the last bin is right-closed so a
    confidence of exactly 1.0 always lands somewhere. Rows whose ``probs`` is
    ``None``/empty are dropped (a degraded router turn with no probability
    vector cannot be calibration-scored); ``n`` is the number of scored rows.
    """
    scored = [(pr, g) for pr, g in zip(probs, gold, strict=True) if pr]
    n = len(scored)
    if n == 0:
        return float("nan")
    edges = [i / bins for i in range(bins + 1)]
    total = 0.0
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        bucket = []
        for pr, g in scored:
            conf = max(pr)
            in_bin = (conf >= lo and conf <= hi) if hi >= 1.0 else (conf >= lo and conf < hi)
            if in_bin:
                pred = max(range(len(pr)), key=lambda i: pr[i])
                bucket.append((conf, 1.0 if pred == g else 0.0))
        if not bucket:
            continue
        m = len(bucket)
        acc = sum(c for _, c in bucket) / m
        avg_conf = sum(cf for cf, _ in bucket) / m
        total += (m / n) * abs(acc - avg_conf)
    return total


def nll(probs: list[list[float]], gold: list[int], *, eps: float = 1e-7) -> float:
    """Mean negative log-likelihood of the gold class under ``probs``.

    Rows with no probability vector are dropped (same rule as :func:`ece`).
    """
    import math

    scored = [(pr, g) for pr, g in zip(probs, gold, strict=True) if pr]
    if not scored:
        return float("nan")
    total = 0.0
    for pr, g in scored:
        p = min(max(pr[g], eps), 1.0)
        total += -math.log(p)
    return total / len(scored)


@dataclass
class BootstrapResult:
    delta_point: float
    ci_low: float
    ci_high: float
    n_resamples: int
    confidence: float = 0.95
    seed: int = 42

    def to_dict(self) -> dict[str, Any]:
        return {
            "delta_point": self.delta_point,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "n_resamples": self.n_resamples,
            "confidence": self.confidence,
            "seed": self.seed,
        }


def bootstrap_accuracy_delta(
    gold: list[int],
    pilot_pred: list[int],
    v4_pred: list[int],
    *,
    n_resamples: int = 10_000,
    confidence: float = 0.95,
    seed: int = 42,
) -> BootstrapResult:
    """Paired bootstrap 95% CI on ``acc(pilot) - acc(v4)``.

    Resamples turn indices *with replacement* (paired: the same resampled index
    scores both routers, so the shared test-set noise cancels), recomputing the
    accuracy delta each time, and returns the empirical ``[2.5%, 97.5%]``
    percentile interval. The gate (spec §6.4) passes only if ``ci_low > -0.01``.
    """
    n = len(gold)
    correct_p = [1 if g == p else 0 for g, p in zip(gold, pilot_pred, strict=True)]
    correct_v = [1 if g == p else 0 for g, p in zip(gold, v4_pred, strict=True)]
    point = (sum(correct_p) - sum(correct_v)) / n if n else 0.0

    rng = random.Random(seed)
    deltas: list[float] = []
    for _ in range(n_resamples):
        sp = 0
        sv = 0
        for _ in range(n):
            j = rng.randrange(n)
            sp += correct_p[j]
            sv += correct_v[j]
        deltas.append((sp - sv) / n)
    deltas.sort()
    lo_idx = int((1 - confidence) / 2 * n_resamples)
    hi_idx = int((1 + confidence) / 2 * n_resamples) - 1
    lo_idx = max(0, min(lo_idx, n_resamples - 1))
    hi_idx = max(0, min(hi_idx, n_resamples - 1))
    return BootstrapResult(
        delta_point=point,
        ci_low=deltas[lo_idx],
        ci_high=deltas[hi_idx],
        n_resamples=n_resamples,
        confidence=confidence,
        seed=seed,
    )


@dataclass
class RouterMetrics:
    """All §6.4 metrics for one router on one label set."""

    n: int
    accuracy: float
    under_routing_rate: float
    over_routing_rate: float
    severity_weighted_under_routing: float
    per_class_recall: dict[str, float]
    per_class_precision: dict[str, float]
    macro_f1: float
    confusion_matrix: list[list[int]]
    ece: float
    nll: float
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "accuracy": self.accuracy,
            "under_routing_rate": self.under_routing_rate,
            "over_routing_rate": self.over_routing_rate,
            "severity_weighted_under_routing": self.severity_weighted_under_routing,
            "per_class_recall": self.per_class_recall,
            "per_class_precision": self.per_class_precision,
            "macro_f1": self.macro_f1,
            "confusion_matrix": self.confusion_matrix,
            "ece": self.ece,
            "nll": self.nll,
            **({"extra": self.extra} if self.extra else {}),
        }


def compute_router_metrics(
    gold: list[int],
    pred: list[int],
    probs: list[list[float]] | None = None,
) -> RouterMetrics:
    """Bundle every §6.4 metric for one router.

    ``probs`` (calibrated top-1 probability vectors per turn, ``None`` where the
    router degraded) feeds ECE/NLL only; the discrete metrics use ``pred``. When
    ``probs`` is omitted, ECE/NLL are ``NaN``.
    """
    prob_rows = probs if probs is not None else [[] for _ in gold]
    return RouterMetrics(
        n=len(gold),
        accuracy=accuracy(gold, pred),
        under_routing_rate=under_routing_rate(gold, pred),
        over_routing_rate=over_routing_rate(gold, pred),
        severity_weighted_under_routing=severity_weighted_under_routing(gold, pred),
        per_class_recall=per_class_recall(gold, pred),
        per_class_precision=per_class_precision(gold, pred),
        macro_f1=macro_f1(gold, pred),
        confusion_matrix=confusion_matrix(gold, pred),
        ece=ece(prob_rows, gold),
        nll=nll(prob_rows, gold),
    )
