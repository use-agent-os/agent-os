"""Offline CI smoke for the pilot-v1 R3-uplift config grid.

The owner-approved pilot-v1 R3-uplift spec amendment permits a small,
explicit config grid selected on VALIDATION ONLY. This test pins the exact grid
(``baseline`` / ``oversample`` / ``weights`` / ``both`` — no others) and drives
each config through the *real* train→export→load path on a synthetic corpus with
a deterministic stub encoder (no MiniLM weights, no network, fork-safe), the same
discipline as the v1 T7 smoke.

It also locks the two new plumbing levers the grid needs:

* per-class-multiplier oversampling (``resample_train`` ``"multiplier"`` mode),
* per-config sample weights (``sample_weights_for`` accepting a weights dict),

plus the val-only over-routing proxy (pred>gold rate) added to ``evaluate``.

Requires the ``pilot-train`` group; skipped when it is absent.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("skl2onnx", reason="pilot-train group (skl2onnx) not installed")
pytest.importorskip("sklearn", reason="pilot-train group (scikit-learn) not installed")

from agentos.agentos_router.pilot.features import FEATURE_DIM  # noqa: E402
from agentos.agentos_router.pilot.model import PilotModel  # noqa: E402
from scripts.pilot_router.export_model import export_artifact  # noqa: E402
from scripts.pilot_router.train_lib import (  # noqa: E402
    CLASS_TO_INT,
    CLASSES,
    V2_TIER1_CONFIGS,
    build_feature_matrix,
    evaluate,
    fit_temperature,
    load_split_rows,
    resample_train,
    sample_weights_for,
    train_pipeline,
)

# Reuse the v1 smoke's stub encoder + synthetic corpus writer.
from tests.test_agentos_router.test_pilot_train_smoke import (  # noqa: E402
    _STUB_EXPORT_META,
    _StubEncoder,
    _write_synthetic_corpus,
)


def test_grid_is_exactly_the_four_approved_configs() -> None:
    """The grid is frozen: exactly the four owner-approved names, no others."""
    assert set(V2_TIER1_CONFIGS) == {"baseline", "oversample", "weights", "both"}


def test_baseline_config_matches_shipped_v1_recipe() -> None:
    base = V2_TIER1_CONFIGS["baseline"]
    assert base.sample_weights == {"R0": 1.0, "R1": 1.0, "R2": 2.0, "R3": 3.0}
    assert base.oversample_multipliers is None  # no resampling in baseline


def test_config_grid_weights_and_multipliers() -> None:
    over = V2_TIER1_CONFIGS["oversample"]
    assert over.sample_weights == {"R0": 1.0, "R1": 1.0, "R2": 2.0, "R3": 3.0}
    assert over.oversample_multipliers == {"R0": 2.0, "R3": 3.0}

    weights = V2_TIER1_CONFIGS["weights"]
    assert weights.sample_weights == {"R0": 2.0, "R1": 1.0, "R2": 2.0, "R3": 6.0}
    assert weights.oversample_multipliers is None

    both = V2_TIER1_CONFIGS["both"]
    assert both.sample_weights == {"R0": 1.5, "R1": 1.0, "R2": 2.0, "R3": 4.0}
    assert both.oversample_multipliers == {"R3": 2.0}


def test_sample_weights_for_accepts_per_config_weights() -> None:
    y = np.array([CLASS_TO_INT[c] for c in ["R0", "R1", "R2", "R3"]], dtype=np.int64)
    w = sample_weights_for(y, weights={"R0": 2.0, "R1": 1.0, "R2": 2.0, "R3": 6.0})
    np.testing.assert_array_equal(w, np.array([2.0, 1.0, 2.0, 6.0]))
    # Default (no weights arg) keeps the shipped v1 policy.
    w_default = sample_weights_for(y)
    np.testing.assert_array_equal(w_default, np.array([1.0, 1.0, 2.0, 3.0]))


def test_resample_multiplier_grows_named_classes_only() -> None:
    # 10 R0, 100 R1, 100 R2, 5 R3 (R3-scarce, like the real corpus shape).
    y = np.array([0] * 10 + [1] * 100 + [2] * 100 + [3] * 5, dtype=np.int64)
    x = np.arange(len(y) * 3, dtype=np.float32).reshape(len(y), 3)
    xr, yr = resample_train(
        x, y, seed=42, strategy="multiplier", multipliers={"R0": 2.0, "R3": 3.0}
    )
    counts = {int(c): int((yr == c).sum()) for c in range(4)}
    assert counts[0] == 20  # R0 doubled
    assert counts[1] == 100  # R1 untouched (no multiplier)
    assert counts[2] == 100  # R2 untouched
    assert counts[3] == 15  # R3 tripled
    # Oversampled rows are with-replacement copies of real rows (no new vectors).
    assert set(map(tuple, xr.tolist())).issubset(set(map(tuple, x.tolist())))


def test_over_routing_proxy_counts_pred_above_gold() -> None:
    # gold R1, R1, R2 ; pred R2 (over), R0 (under), R2 (correct) -> 1/3 over.
    y = np.array([1, 1, 2], dtype=np.int64)
    probs = np.zeros((3, 4), dtype=np.float32)
    probs[0, 2] = 1.0  # pred R2 > gold R1 : over-route
    probs[1, 0] = 1.0  # pred R0 < gold R1 : under-route
    probs[2, 2] = 1.0  # pred R2 == gold R2
    m = evaluate(probs, y)
    assert m.over_routing_rate == pytest.approx(1.0 / 3.0)


@pytest.mark.parametrize("config_name", ["baseline", "oversample", "weights", "both"])
def test_each_config_trains_exports_loads(config_name: str, tmp_path: Path) -> None:
    """Every grid config drives the real train→export→PilotModel-load path."""
    corpus_path, labels_path = _write_synthetic_corpus(tmp_path)
    rows = load_split_rows(corpus_path, labels_path)
    encoder = _StubEncoder()
    x_train, y_train = build_feature_matrix(rows["train"], encoder)
    x_val, y_val = build_feature_matrix(rows["val"], encoder)
    assert x_train.shape == (40, FEATURE_DIM)

    cfg = V2_TIER1_CONFIGS[config_name]
    xt, yt = resample_train(
        x_train,
        y_train,
        seed=42,
        strategy="multiplier" if cfg.oversample_multipliers else "none",
        multipliers=cfg.oversample_multipliers,
    )
    # Oversampling only ever grows or preserves the row count.
    assert len(yt) >= len(y_train)
    pipe = train_pipeline(xt, yt, seed=42, max_iter=120, sample_weights=cfg.sample_weights)
    clf = pipe.named_steps["clf"]

    val_probs = pipe.predict_proba(x_val).astype(np.float32)
    temperature = fit_temperature(val_probs, y_val)
    metrics = evaluate(val_probs, y_val)
    assert 0.0 <= metrics.accuracy <= 1.0
    assert 0.0 <= metrics.over_routing_rate <= 1.0
    assert set(metrics.per_class_recall) == set(CLASSES)

    out_dir = tmp_path / f"artifact_{config_name}"
    onnx_path, manifest_path = export_artifact(
        pipe,
        clf,
        out_dir,
        temperature=temperature,
        training_stats={"_note": "R3-uplift grid smoke", "config": config_name},
        export_meta=_STUB_EXPORT_META,
    )
    model = PilotModel(out_dir)
    assert model.available, model.unavailable_reason
    probs = model.predict_proba(x_val)
    assert probs.shape == (10, 4)
    np.testing.assert_allclose(probs.sum(axis=1), np.ones(10), atol=1e-5)
    manifest = json.loads(manifest_path.read_text())
    assert manifest["pilot_version"] == "pilot-v1"


def test_unknown_multiplier_class_rejected() -> None:
    y = np.array([0, 1, 2, 3], dtype=np.int64)
    x = np.zeros((4, 2), dtype=np.float32)
    with pytest.raises((ValueError, KeyError)):
        resample_train(x, y, seed=1, strategy="multiplier", multipliers={"R9": 2.0})
