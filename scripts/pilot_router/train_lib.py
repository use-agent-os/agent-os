#!/usr/bin/env python
"""Shared training/export library for the Pilot router `pilot-v1` model (T7).

This module holds the *pure, encoder-agnostic* half of the T7 pipeline so both
the real training entry point (``train.py``, real MiniLM encoder + on-disk
feature cache) and the offline CI smoke test (a stub encoder, no network, no
ONNX weights) drive **the same code paths**:

* :func:`load_split_rows` — inner-join corpus + labels on ``turn_id`` and split
  into train/val/test row lists (test is loaded for shape only; T7 never opens
  it — the caller must not pass ``"test"`` anywhere near training).
* :func:`build_feature_matrix` — turn a list of rows into a ``float32 [N, 392]``
  matrix via T1's ``build_features`` with a caller-supplied ``PilotEncoder``
  (the production ``_MiniLMEncoder`` in real runs, a deterministic stub in the
  smoke test). Exact 392-dim parity is asserted here.
* :func:`resample_train` — the spec §6.2 train-partition class-balance lever.
* :func:`train_pipeline` — the LOCKED architecture:
  ``Pipeline(StandardScaler(), MLPClassifier(hidden_layer_sizes=(256, 64)))``
  fit with GOLD-class sample weights ``R0=1, R1=1, R2=2, R3=3``. No search of
  any kind — architecture and hyperparameters are fixed by the contract.
* :func:`fit_temperature` — log-space temperature ``T`` fit on the VALIDATION
  split ONLY by minimizing NLL of ``softmax(log(clip(p,1e-7,1))/T)``.
* :func:`evaluate` — accuracy, per-class recall, severity-weighted
  under-routing, and 15-bin ECE, all on VALIDATION.

The locked class order is ``["R0", "R1", "R2", "R3"]`` with integer labels
``0..3`` (production ONNX contract). Sample weights and severity weights are
keyed by that order.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from agentos.agentos_router.pilot.features import (
    FEATURE_DIM,
    PilotEncoder,
    build_features,
)

# --- Locked contract constants ----------------------------------------------

#: Pinned class order (Pilot spec, Rev 4). Index == integer label.
CLASSES: list[str] = ["R0", "R1", "R2", "R3"]
CLASS_TO_INT: dict[str, int] = {c: i for i, c in enumerate(CLASSES)}

#: GOLD-class sample weights for ``MLPClassifier.fit`` (spec §6.6).
SAMPLE_WEIGHT_BY_CLASS: dict[str, float] = {"R0": 1.0, "R1": 1.0, "R2": 2.0, "R3": 3.0}

#: Severity weight per class for the severity-weighted under-routing metric.
#: Under-routing = predicting a strictly *lower* (cheaper) tier than gold; the
#: penalty scales with how far below and the gold class's severity.
SEVERITY_WEIGHT_BY_CLASS: dict[str, float] = {"R0": 1.0, "R1": 1.0, "R2": 2.0, "R3": 3.0}

#: The shipped seed; diagnostics replicate on these for a stability report.
SHIP_SEED = 42
DIAGNOSTIC_SEEDS: tuple[int, ...] = (7, 2026)


# --- pilot-v1 R3-uplift config grid (owner-approved amendment) ----------------
#
# The v1 discipline (no search) is amended for the R3-uplift round by an
# owner-approved spec amendment: a SMALL, EXPLICIT config grid selected on
# VALIDATION ONLY. The grid below is frozen — exactly these four configs, no
# hyperparameter search beyond them, all at seed 42 with the v1 fixed
# architecture and the same per-config temperature-refit procedure. The test
# split stays sealed.


@dataclass(frozen=True)
class TrainConfig:
    """One frozen point in the pilot-v1 R3-uplift config grid (VAL-only).

    * ``sample_weights`` — GOLD-class ``MLPClassifier.fit`` sample weights.
    * ``oversample_multipliers`` — per-class with-replacement TRAIN oversample
      multipliers (e.g. ``{"R3": 3.0}`` ~triples R3's real rows). ``None`` means
      no resampling; class balance is carried by ``sample_weights`` alone. Only
      TRAIN is ever resampled — val/test keep their natural distribution.
    """

    name: str
    sample_weights: dict[str, float]
    oversample_multipliers: dict[str, float] | None = None


#: The exact four owner-approved R3-uplift configs (no others). Multipliers are
#: chosen so the effective row counts match the amendment's targets on the real
#: corpus (train R3=168 → ~504 at 3×; R0=243 → ~486 at 2×).
V2_TIER1_CONFIGS: dict[str, TrainConfig] = {
    # Current shipped v1 settings — rerun for the comparison baseline.
    "baseline": TrainConfig(
        name="baseline",
        sample_weights={"R0": 1.0, "R1": 1.0, "R2": 2.0, "R3": 3.0},
        oversample_multipliers=None,
    ),
    # R3 oversampled ~3×, R0 ~2×; weights unchanged from baseline.
    "oversample": TrainConfig(
        name="oversample",
        sample_weights={"R0": 1.0, "R1": 1.0, "R2": 2.0, "R3": 3.0},
        oversample_multipliers={"R0": 2.0, "R3": 3.0},
    ),
    # No resampling; heavier GOLD-class sample weights on the rare classes.
    "weights": TrainConfig(
        name="weights",
        sample_weights={"R0": 2.0, "R1": 1.0, "R2": 2.0, "R3": 6.0},
        oversample_multipliers=None,
    ),
    # R3 oversample ~2× combined with moderate weight boosts.
    "both": TrainConfig(
        name="both",
        sample_weights={"R0": 1.5, "R1": 1.0, "R2": 2.0, "R3": 4.0},
        oversample_multipliers={"R3": 2.0},
    ),
}

#: Lower clip for log-space calibration (mirrors PilotModel._PROBA_CLIP_MIN).
_PROBA_CLIP_MIN = 1e-7

#: ECE bin count (spec §6.6).
ECE_BINS = 15


# --- Data loading -----------------------------------------------------------


@dataclass
class Row:
    """One joined corpus+label training row (current-turn text + gold class)."""

    turn_id: str
    text: str
    label: str
    split: str
    category: str
    boundary_set: bool


def load_split_rows(
    corpus_path: str | Path,
    labels_path: str | Path,
) -> dict[str, list[Row]]:
    """Inner-join corpus + labels on ``turn_id``; group by split.

    Rows present in ``labels.jsonl`` but absent from ``corpus.jsonl`` (or vice
    versa) are dropped — the 13 unlabeled turns fall out here naturally. The
    label's ``split`` is authoritative (corpus and labels agree by construction,
    but the label file is the gold source).

    Returns ``{"train": [...], "val": [...], "test": [...]}``. The ``test`` list
    is populated for completeness/shape, but T7 must never feed it to training
    or calibration — that partition is exclusively T9's.
    """
    corpus_by_id: dict[str, dict[str, Any]] = {}
    with open(corpus_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            corpus_by_id[str(rec["turn_id"])] = rec

    out: dict[str, list[Row]] = {"train": [], "val": [], "test": []}
    with open(labels_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            lab = json.loads(line)
            tid = str(lab["turn_id"])
            crec = corpus_by_id.get(tid)
            if crec is None:
                continue  # unlabeled/dropped turn — inner join excludes it
            split = str(lab["split"])
            if split not in out:
                continue
            row = Row(
                turn_id=tid,
                text=str(crec["text"]),
                label=str(lab["label"]),
                split=split,
                category=str(lab.get("category", crec.get("category", ""))),
                boundary_set=bool(lab.get("boundary_set", False)),
            )
            out[split].append(row)
    return out


# --- Feature building -------------------------------------------------------


def build_feature_matrix(
    rows: Sequence[Row],
    encoder: PilotEncoder,
) -> tuple[np.ndarray, np.ndarray]:
    """Build ``(X float32 [N, 392], y int64 [N])`` for ``rows``.

    Each row's text goes through T1's ``build_features`` with the supplied
    ``encoder`` — the SAME builder production uses, so train/serve features are
    byte-identical. Exact 392-dim parity is asserted. Labels are mapped to the
    pinned integer order ``0..3``.
    """
    n = len(rows)
    x = np.empty((n, FEATURE_DIM), dtype=np.float32)
    y = np.empty((n,), dtype=np.int64)
    for i, row in enumerate(rows):
        feat = build_features(row.text, encoder=encoder)
        if feat.shape != (FEATURE_DIM,):
            raise ValueError(
                f"feature parity violation at row {i} ({row.turn_id}): "
                f"expected ({FEATURE_DIM},), got {feat.shape}"
            )
        x[i] = feat
        y[i] = CLASS_TO_INT[row.label]
    return x, y


# --- Train-partition resampling (spec §6.2) ---------------------------------


def resample_train(
    x: np.ndarray,
    y: np.ndarray,
    *,
    seed: int,
    strategy: str = "none",
    multipliers: dict[str, float] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Optionally resample the TRAIN matrix for class balance (spec §6.2).

    ``strategy``:

    * ``"none"`` — return the rows unchanged (the shipped v1 decision; balance
      is carried entirely by the GOLD-class sample weights, so no rows are
      duplicated). This is the default.
    * ``"oversample"`` — with-replacement oversample every minority class up to
      the majority-class count, using ``seed`` for reproducibility. Provided so
      the lever is exercised/available; not used by the shipped artifact.
    * ``"multiplier"`` — with-replacement oversample each class named in
      ``multipliers`` to ``round(count * multiplier)`` rows (a class absent from
      ``multipliers`` is left at its natural count). This is the R3-uplift
      grid lever: e.g. ``{"R3": 3.0}`` ~triples R3's real rows without touching the
      others. Oversampled rows are with-replacement copies of real rows — no new
      vectors are synthesized. A multiplier of ``1.0`` is a no-op; ``<1.0`` (a
      downsample) is rejected to keep the lever monotone/explicit.

    Validation and test are NEVER passed here.
    """
    if strategy == "none":
        return x, y

    rng = np.random.default_rng(seed)

    if strategy == "multiplier":
        if not multipliers:
            return x, y
        mult_by_int: dict[int, float] = {}
        for cls_name, factor in multipliers.items():
            if cls_name not in CLASS_TO_INT:
                raise ValueError(f"unknown class in multipliers: {cls_name!r}")
            if factor < 1.0:
                raise ValueError(
                    f"multiplier for {cls_name} must be >= 1.0 (got {factor}); "
                    "downsampling is not an R3-uplift lever"
                )
            mult_by_int[CLASS_TO_INT[cls_name]] = float(factor)
        mult_idx_parts: list[np.ndarray] = []
        for cls_int in (int(c) for c in np.unique(y)):
            cls_idx = np.flatnonzero(y == cls_int)
            mult_idx_parts.append(cls_idx)
            m_factor = mult_by_int.get(cls_int, 1.0)
            m_target = int(round(len(cls_idx) * m_factor))
            if m_target > len(cls_idx):
                extra = rng.choice(cls_idx, size=m_target - len(cls_idx), replace=True)
                mult_idx_parts.append(extra)
        all_idx = np.concatenate(mult_idx_parts)
        rng.shuffle(all_idx)
        return x[all_idx], y[all_idx]

    if strategy != "oversample":
        raise ValueError(f"unknown resample strategy: {strategy!r}")

    counts = {int(c): int((y == c).sum()) for c in np.unique(y)}
    target = max(counts.values())
    idx_parts = []
    for cls, cnt in counts.items():
        cls_idx = np.flatnonzero(y == cls)
        idx_parts.append(cls_idx)
        if cnt < target:
            extra = rng.choice(cls_idx, size=target - cnt, replace=True)
            idx_parts.append(extra)
    all_idx = np.concatenate(idx_parts)
    rng.shuffle(all_idx)
    return x[all_idx], y[all_idx]


# --- Locked training pipeline -----------------------------------------------


def sample_weights_for(y: np.ndarray, weights: dict[str, float] | None = None) -> np.ndarray:
    """Per-row GOLD-class sample weights for labels ``y``.

    ``weights`` maps class name → weight; ``None`` uses the shipped v1 policy
    ``SAMPLE_WEIGHT_BY_CLASS`` (``R0=1,R1=1,R2=2,R3=3``). The R3-uplift config
    grid passes a per-config dict here.
    """
    policy = SAMPLE_WEIGHT_BY_CLASS if weights is None else weights
    w = np.ones(len(y), dtype=np.float64)
    for cls, weight in policy.items():
        w[y == CLASS_TO_INT[cls]] = weight
    return w


def train_pipeline(
    x_train: np.ndarray,
    y_train: np.ndarray,
    *,
    seed: int,
    max_iter: int = 400,
    sample_weights: dict[str, float] | None = None,
) -> Any:
    """Fit the LOCKED architecture (no search) and return it.

    Architecture (spec §6.6, fixed):
    ``Pipeline(StandardScaler(), MLPClassifier(hidden_layer_sizes=(256, 64)))``,
    fit with GOLD-class sample weights. ``random_state=seed`` is the only knob
    that varies between the shipped v1 artifact and the diagnostic replicas.

    ``sample_weights`` maps class name → weight; ``None`` uses the shipped v1
    policy. The R3-uplift config grid passes a per-config dict — the architecture
    itself stays fixed (that is not amended).

    ``max_iter`` is a convergence budget, not a searched hyperparameter; the
    default is generous enough for convergence on the real corpus while the
    smoke test lowers it for speed.
    """
    from sklearn.neural_network import MLPClassifier
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    clf = MLPClassifier(
        hidden_layer_sizes=(256, 64),
        random_state=seed,
        max_iter=max_iter,
    )
    pipe = Pipeline([("scaler", StandardScaler()), ("clf", clf)])
    pipe.fit(
        x_train,
        y_train,
        clf__sample_weight=sample_weights_for(y_train, sample_weights),
    )

    classes = list(pipe.named_steps["clf"].classes_)
    if classes != list(range(len(CLASSES))):
        raise ValueError(
            f"unexpected classifier class order {classes}; training data must contain all of 0..3"
        )
    return pipe


# --- Temperature calibration (spec §6.6) ------------------------------------


def _softmax_temp(probs: np.ndarray, temperature: float) -> np.ndarray:
    """``softmax(log(clip(p,1e-7,1)) / T)`` row-wise — matches PilotModel."""
    logits = np.log(np.clip(probs, _PROBA_CLIP_MIN, 1.0)) / temperature
    logits = logits - logits.max(axis=1, keepdims=True)
    ex = np.exp(logits)
    result: np.ndarray = ex / ex.sum(axis=1, keepdims=True)
    return result


def _nll(probs: np.ndarray, y: np.ndarray, temperature: float) -> float:
    q = _softmax_temp(probs, temperature)
    picked = q[np.arange(len(y)), y]
    return float(-np.log(np.clip(picked, _PROBA_CLIP_MIN, 1.0)).mean())


def fit_temperature(
    val_probs: np.ndarray,
    y_val: np.ndarray,
    *,
    lo: float = 0.05,
    hi: float = 10.0,
    iters: int = 60,
) -> float:
    """Fit log-space temperature ``T`` on VALIDATION by minimizing NLL.

    Golden-section search over ``[lo, hi]`` — deterministic, no random restarts,
    no test-split access. Returns the ``T`` that minimizes
    ``NLL(softmax(log(clip(p,1e-7,1))/T))`` on the validation set.
    """
    inv_phi = (math.sqrt(5.0) - 1.0) / 2.0  # 1/phi
    a, b = lo, hi
    c = b - inv_phi * (b - a)
    d = a + inv_phi * (b - a)
    fc = _nll(val_probs, y_val, c)
    fd = _nll(val_probs, y_val, d)
    for _ in range(iters):
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - inv_phi * (b - a)
            fc = _nll(val_probs, y_val, c)
        else:
            a, c, fc = c, d, fd
            d = a + inv_phi * (b - a)
            fd = _nll(val_probs, y_val, d)
    return float((a + b) / 2.0)


# --- Evaluation (VALIDATION only) -------------------------------------------


@dataclass
class EvalMetrics:
    accuracy: float
    per_class_recall: dict[str, float]
    severity_weighted_underrouting: float
    over_routing_rate: float
    ece: float
    nll: float
    n: int
    confusion: list[list[int]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accuracy": self.accuracy,
            "per_class_recall": self.per_class_recall,
            "severity_weighted_underrouting": self.severity_weighted_underrouting,
            "over_routing_rate": self.over_routing_rate,
            "ece": self.ece,
            "nll": self.nll,
            "n": self.n,
            "confusion": self.confusion,
        }


def _ece(probs: np.ndarray, y: np.ndarray, *, bins: int = ECE_BINS) -> float:
    """Expected Calibration Error over ``bins`` equal-width confidence bins."""
    conf = probs.max(axis=1)
    pred = probs.argmax(axis=1)
    correct = (pred == y).astype(np.float64)
    n = len(y)
    ece = 0.0
    edges = np.linspace(0.0, 1.0, bins + 1)
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        # Last bin is closed on the right so conf==1.0 lands somewhere.
        if hi >= 1.0:
            mask = (conf >= lo) & (conf <= hi)
        else:
            mask = (conf >= lo) & (conf < hi)
        m = int(mask.sum())
        if m == 0:
            continue
        acc = float(correct[mask].mean())
        avg_conf = float(conf[mask].mean())
        ece += (m / n) * abs(acc - avg_conf)
    return ece


def _severity_weighted_underrouting(pred: np.ndarray, y: np.ndarray) -> float:
    """Mean severity-weighted under-routing penalty.

    Under-routing = predicting a strictly lower (cheaper) tier than gold. The
    per-row penalty is ``severity[gold] * (gold_index - pred_index)`` when the
    prediction is below gold, else 0. Averaged over all rows — a directional,
    cost-aware companion to plain accuracy (over-routing is not penalized here).
    """
    sev = np.array([SEVERITY_WEIGHT_BY_CLASS[c] for c in CLASSES], dtype=np.float64)
    total = 0.0
    for p, g in zip(pred, y, strict=True):
        if p < g:
            total += sev[g] * (int(g) - int(p))
    return float(total / len(y)) if len(y) else 0.0


def _over_routing_rate(pred: np.ndarray, y: np.ndarray) -> float:
    """Over-routing proxy: fraction of rows whose prediction is above gold.

    Over-routing = predicting a strictly *higher* (more expensive) tier than
    gold. This is the val-only proxy the T9 gate's over-routing limit tracks
    (the real over-routing metric needs test-split traffic; on the sealed-test
    discipline this pred>gold rate stands in for it during VAL-only selection).
    """
    if not len(y):
        return 0.0
    return float((pred > y).mean())


def evaluate(
    calibrated_probs: np.ndarray,
    y: np.ndarray,
) -> EvalMetrics:
    """Compute VALIDATION metrics from calibrated probabilities."""
    pred = calibrated_probs.argmax(axis=1)
    acc = float((pred == y).mean()) if len(y) else 0.0

    per_class: dict[str, float] = {}
    for cls, idx in CLASS_TO_INT.items():
        mask = y == idx
        m = int(mask.sum())
        per_class[cls] = float((pred[mask] == idx).mean()) if m else float("nan")

    k = len(CLASSES)
    conf = [[0] * k for _ in range(k)]
    for p, g in zip(pred, y, strict=True):
        conf[int(g)][int(p)] += 1

    return EvalMetrics(
        accuracy=acc,
        per_class_recall=per_class,
        severity_weighted_underrouting=_severity_weighted_underrouting(pred, y),
        over_routing_rate=_over_routing_rate(pred, y),
        ece=_ece(calibrated_probs, y),
        nll=_nll(calibrated_probs, y, 1.0),
        n=len(y),
        confusion=conf,
    )
