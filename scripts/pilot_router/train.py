#!/usr/bin/env python
"""Train + calibrate + export the real Pilot `pilot-v1` model (T7).

One command builds the shipped artifact plus its diagnostics:

    uv run --group pilot-train --extra recommended \\
        python scripts/pilot_router/train.py

Pipeline (all values locked by spec §6.6 — NO search of any kind):

1. Load corpus + labels, inner-join on ``turn_id``, keep train/val only. The
   test split is NEVER read for training or calibration (T9 owns it).
2. Build ``float32 [N, 392]`` features through T1's ``build_features`` with the
   PRODUCTION ``_MiniLMEncoder`` (imported from ``pilot.strategy`` — not a third
   encoder). Features are cached to a git-ignored ``.feature_cache/`` keyed by a
   fingerprint of (corpus sha, feature contract) so retrains are instant.
3. Train the fixed ``Pipeline(StandardScaler, MLPClassifier(256, 64))`` with
   GOLD-class sample weights (R0=1,R1=1,R2=2,R3=3) at seed 42 (the shipped
   artifact) plus diagnostic replicas at seeds 7 and 2026.
4. Fit log-space temperature ``T`` on the VALIDATION split only.
5. Evaluate on validation (accuracy, per-class recall, severity-weighted
   under-routing, 15-bin ECE) for the shipped seed and every diagnostic; emit a
   3-seed mean±std stability table.
6. Export seed 42 → ``scripts/pilot_router/artifacts/pilot_v1/`` (git-ignored
   STAGING). The artifact lands in the shipped location only after T9 passes.
7. Write ``training_meta.json`` (git-tracked) with all metrics + provenance.

Diagnostics never touch the test split; no seed is selected by score — seed 42
ships regardless of the stability numbers.
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

# Running as a script (``python scripts/pilot_router/train.py``) does not put the
# repo root on sys.path, so the ``scripts`` package is not importable. Add it so
# the sibling ``scripts.pilot_router.*`` imports below resolve either way (script
# invocation or ``python -m``).
_REPO_ROOT_FOR_PATH = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT_FOR_PATH) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT_FOR_PATH))

from agentos.agentos_router.pilot.features import FEATURE_DIM  # noqa: E402
from scripts.pilot_router.export_model import (  # noqa: E402
    PILOT_VERSION,
    export_artifact,
)
from scripts.pilot_router.train_lib import (  # noqa: E402
    CLASSES,
    DIAGNOSTIC_SEEDS,
    SAMPLE_WEIGHT_BY_CLASS,
    SHIP_SEED,
    V2_TIER1_CONFIGS,
    Row,
    TrainConfig,
    build_feature_matrix,
    evaluate,
    fit_temperature,
    load_split_rows,
    resample_train,
    train_pipeline,
)

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[1]

CORPUS_PATH = _HERE / "data" / "corpus.jsonl"
LABELS_PATH = _HERE / "data" / "labels.jsonl"
LABELS_META_PATH = _HERE / "labels_meta.json"
CACHE_DIR = _HERE / ".feature_cache"
STAGING_DIR = _HERE / "artifacts" / "pilot_v1"
#: SEPARATE staging dir for the pilot-v1 R3-uplift config-grid selected config.
#: NEVER overwrite the v1 staging dir (that is the committed T9 record).
V2_TIER1_STAGING_DIR = _HERE / "artifacts" / "pilot_v1_uplift_grid"
TRAINING_META_PATH = _HERE / "training_meta.json"

#: Selection-rule thresholds (owner amendment "R3 uplift"), applied on VAL only.
V2_ACC_WITHIN_PP = 0.02  # accuracy within 2pp of the best config's accuracy
V2_OVERROUTE_SLACK_PP = 0.05  # over-routing proxy ≤ baseline + 5pp
V2_MEANINGFUL_R3_UPLIFT_PP = 0.05  # <+5pp over baseline R3 recall = "not meaningful"

#: Shipped resampling decision (spec §6.2). "none": balance is carried entirely
#: by GOLD-class sample weights; no TRAIN rows are duplicated. Val/test natural.
RESAMPLE_STRATEGY = "none"

#: Pinned pilot-train dep versions recorded in the manifest (contract, spec §6.6).
_PINNED_DEPS = (
    "scikit-learn>=1.8,<1.9",
    "skl2onnx>=1.17",
    "onnxruntime>=1.17",
)


# --- Provenance helpers ------------------------------------------------------


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()
    except Exception:
        return "unknown"


def _installed_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for mod in ("sklearn", "skl2onnx", "onnxruntime", "numpy"):
        try:
            m = __import__(mod)
            versions[mod] = getattr(m, "__version__", "unknown")
        except Exception:
            versions[mod] = "unavailable"
    return versions


def _corpus_fingerprint() -> str:
    """sha256 over the corpus + labels bytes + the feature contract width.

    Keys the feature cache — any change to the data or the 392-dim contract
    invalidates it, so a stale cache can never silently poison a retrain.
    """
    h = hashlib.sha256()
    h.update(CORPUS_PATH.read_bytes())
    h.update(LABELS_PATH.read_bytes())
    h.update(f"dim={FEATURE_DIM}".encode())
    return h.hexdigest()[:16]


# --- Feature cache -----------------------------------------------------------


def _build_or_load_features(
    split: str,
    rows: list[Row],
    encoder: Any,
    fingerprint: str,
) -> tuple[np.ndarray, np.ndarray]:
    """Build features for ``rows``, caching to a git-ignored .npz on disk."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"{split}_{fingerprint}.npz"
    if cache_path.is_file():
        data = np.load(cache_path)
        x, y = data["x"].astype(np.float32), data["y"].astype(np.int64)
        if x.shape == (len(rows), FEATURE_DIM) and len(y) == len(rows):
            print(f"  [{split}] loaded cached features {x.shape}")
            return x, y
        print(f"  [{split}] cache shape stale ({x.shape}); rebuilding")
    t0 = time.perf_counter()
    x, y = build_feature_matrix(rows, encoder)
    dt = time.perf_counter() - t0
    print(f"  [{split}] built features {x.shape} in {dt:.1f}s")
    np.savez(cache_path, x=x, y=y)
    return x, y


# --- Stability aggregation ---------------------------------------------------


@dataclass
class SeedResult:
    seed: int
    temperature: float
    accuracy: float
    per_class_recall: dict[str, float]
    severity_weighted_underrouting: float
    over_routing_rate: float
    ece_before: float
    ece_after: float
    nll_before: float
    nll_after: float
    confusion: list[list[int]]


def _mean_std(values: list[float]) -> dict[str, float]:
    arr = np.asarray(values, dtype=np.float64)
    return {"mean": float(arr.mean()), "std": float(arr.std(ddof=0))}


def _stability_table(results: list[SeedResult]) -> dict[str, Any]:
    table: dict[str, Any] = {
        "seeds": [r.seed for r in results],
        "accuracy": _mean_std([r.accuracy for r in results]),
        "severity_weighted_underrouting": _mean_std(
            [r.severity_weighted_underrouting for r in results]
        ),
        "ece_after": _mean_std([r.ece_after for r in results]),
        "temperature": _mean_std([r.temperature for r in results]),
        "per_class_recall": {},
    }
    for cls in CLASSES:
        table["per_class_recall"][cls] = _mean_std([r.per_class_recall[cls] for r in results])
    return table


# --- Train one seed ----------------------------------------------------------


def _train_one_seed(
    seed: int,
    x_train: np.ndarray,
    y_train: np.ndarray,
    x_val: np.ndarray,
    y_val: np.ndarray,
    config: TrainConfig,
) -> tuple[SeedResult, Any, Any]:
    strategy = "multiplier" if config.oversample_multipliers else "none"
    xt, yt = resample_train(
        x_train,
        y_train,
        seed=seed,
        strategy=strategy,
        multipliers=config.oversample_multipliers,
    )
    pipe = train_pipeline(xt, yt, seed=seed, sample_weights=config.sample_weights)
    clf = pipe.named_steps["clf"]

    val_probs = pipe.predict_proba(x_val).astype(np.float32)
    before = evaluate(val_probs, y_val)

    temperature = fit_temperature(val_probs, y_val)
    # Apply the fitted T the same way PilotModel does, then re-evaluate.
    from scripts.pilot_router.train_lib import _softmax_temp

    cal_probs = _softmax_temp(val_probs, temperature).astype(np.float32)
    after = evaluate(cal_probs, y_val)

    result = SeedResult(
        seed=seed,
        temperature=temperature,
        accuracy=after.accuracy,
        per_class_recall=after.per_class_recall,
        severity_weighted_underrouting=after.severity_weighted_underrouting,
        over_routing_rate=after.over_routing_rate,
        ece_before=before.ece,
        ece_after=after.ece,
        nll_before=before.nll,
        nll_after=after.nll,
        confusion=after.confusion,
    )
    return result, pipe, clf


# --- Data loading (shared by the v1 path and the v2 grid) --------------------


@dataclass
class LoadedData:
    train_rows: list[Row]
    val_rows: list[Row]
    x_train: np.ndarray
    y_train: np.ndarray
    x_val: np.ndarray
    y_val: np.ndarray
    fingerprint: str


def _load_features() -> LoadedData:
    """Load train+val rows and features (test never opened). Shared setup."""
    rows = load_split_rows(CORPUS_PATH, LABELS_PATH)
    train_rows, val_rows = rows["train"], rows["val"]
    print(f"  rows: train={len(train_rows)} val={len(val_rows)} (test untouched)")

    # Import the PRODUCTION encoder — not a third implementation (spec §6.6).
    from agentos.agentos_router.pilot.strategy import _MiniLMEncoder

    encoder = _MiniLMEncoder()
    fingerprint = _corpus_fingerprint()
    print(f"  feature fingerprint: {fingerprint}")

    x_train, y_train = _build_or_load_features("train", train_rows, encoder, fingerprint)
    x_val, y_val = _build_or_load_features("val", val_rows, encoder, fingerprint)

    if x_train.shape[1] != FEATURE_DIM or x_val.shape[1] != FEATURE_DIM:
        raise SystemExit(f"392-dim parity violation: {x_train.shape} / {x_val.shape}")
    return LoadedData(
        train_rows=train_rows,
        val_rows=val_rows,
        x_train=x_train,
        y_train=y_train,
        x_val=x_val,
        y_val=y_val,
        fingerprint=fingerprint,
    )


# --- Main (v1 baseline artifact) --------------------------------------------


def main() -> int:
    print("Pilot pilot-v1 training (T7)")
    print(f"  corpus:  {CORPUS_PATH}")
    print(f"  labels:  {LABELS_PATH}")
    print(f"  staging: {STAGING_DIR}")

    data = _load_features()
    train_rows, val_rows = data.train_rows, data.val_rows
    x_train, y_train = data.x_train, data.y_train
    x_val, y_val = data.x_val, data.y_val
    fingerprint = data.fingerprint

    baseline_cfg = V2_TIER1_CONFIGS["baseline"]

    # Seed 42 first (the shipped artifact), then diagnostics.
    print(f"\nTraining seed {SHIP_SEED} (SHIPPED) ...")
    ship_result, ship_pipe, ship_clf = _train_one_seed(
        SHIP_SEED, x_train, y_train, x_val, y_val, baseline_cfg
    )
    _print_seed(ship_result)

    diag_results: list[SeedResult] = []
    for seed in DIAGNOSTIC_SEEDS:
        print(f"\nTraining diagnostic seed {seed} ...")
        res, _, _ = _train_one_seed(seed, x_train, y_train, x_val, y_val, baseline_cfg)
        _print_seed(res)
        diag_results.append(res)

    all_results = [ship_result, *diag_results]
    stability = _stability_table(all_results)
    _print_stability(stability)

    # --- Assemble training stats + provenance for the manifest ---
    labels_meta = json.loads(LABELS_META_PATH.read_text(encoding="utf-8"))
    split_class_counts = {
        split: dict(Counter(r.label for r in split_rows))
        for split, split_rows in (("train", train_rows), ("val", val_rows))
    }
    training_stats = {
        "pilot_version": PILOT_VERSION,
        "ship_seed": SHIP_SEED,
        "diagnostic_seeds": list(DIAGNOSTIC_SEEDS),
        "architecture": "Pipeline(StandardScaler, MLPClassifier(hidden_layer_sizes=(256, 64)))",
        "sample_weight_policy": dict(SAMPLE_WEIGHT_BY_CLASS),
        "resample_strategy": RESAMPLE_STRATEGY,
        "resample_note": (
            "TRAIN not resampled; class balance carried by GOLD-class sample "
            "weights only. Validation/test kept at natural distribution (spec §6.2)."
        ),
        "set_sizes": {"train": len(train_rows), "val": len(val_rows)},
        "class_balance_per_split": split_class_counts,
        "val_metrics_seed42": {
            "accuracy": ship_result.accuracy,
            "per_class_recall": ship_result.per_class_recall,
            "severity_weighted_underrouting": ship_result.severity_weighted_underrouting,
            "over_routing_rate": ship_result.over_routing_rate,
            "ece_before_calibration": ship_result.ece_before,
            "ece_after_calibration": ship_result.ece_after,
            "nll_before_calibration": ship_result.nll_before,
            "nll_after_calibration": ship_result.nll_after,
            "fitted_temperature": ship_result.temperature,
            "confusion_gold_by_pred": ship_result.confusion,
        },
        "seed_stability": stability,
        "labeling": {
            "labeler_pin": labels_meta.get("labeler_pin"),
            "label_model": labels_meta.get("label_model"),
            "rubric_sha256": labels_meta.get("rubric_sha256"),
            "labels_file_sha256": labels_meta.get("labels_file", {}).get("labels.jsonl"),
        },
        "git_sha": _git_sha(),
        "training_scripts": [
            "scripts/pilot_router/train.py",
            "scripts/pilot_router/train_lib.py",
            "scripts/pilot_router/export_model.py",
        ],
        "pinned_deps": list(_PINNED_DEPS),
        "installed_versions": _installed_versions(),
        "feature_fingerprint": fingerprint,
        "hardware": platform.platform(),
        "python": platform.python_version(),
    }

    # --- Export the SHIPPED seed-42 artifact to staging ---
    print(f"\nExporting seed-{SHIP_SEED} artifact → {STAGING_DIR}")
    onnx_path, manifest_path = export_artifact(
        ship_pipe,
        ship_clf,
        STAGING_DIR,
        temperature=ship_result.temperature,
        training_stats=training_stats,
    )
    print(f"  wrote {onnx_path} ({onnx_path.stat().st_size} bytes)")
    print(f"  wrote {manifest_path}")

    # --- training_meta.json (git-tracked, no artifacts) ---
    TRAINING_META_PATH.write_text(json.dumps(training_stats, indent=2) + "\n", encoding="utf-8")
    print(f"  wrote {TRAINING_META_PATH}")

    # --- Loadability check via the production loader ---
    _verify_load(STAGING_DIR, x_val)

    print("\nDONE — seed-42 artifact staged; T9 owns the eval gate.")
    return 0


def _verify_load(staging_dir: Path, x_val: np.ndarray) -> None:
    from agentos.agentos_router.pilot.model import PilotModel

    model = PilotModel(staging_dir)
    if not model.available:
        raise SystemExit(f"staged artifact failed to load: {model.unavailable_reason}")
    probs = model.predict_proba(x_val[:8])
    assert probs.shape == (min(8, len(x_val)), 4), probs.shape
    row_sums = probs.sum(axis=1)
    assert np.allclose(row_sums, 1.0, atol=1e-4), row_sums
    print(f"  PilotModel load OK; smoke predict {probs.shape}, rows sum to 1")


def _print_seed(r: SeedResult) -> None:
    recall = " ".join(f"{c}={r.per_class_recall[c]:.3f}" for c in CLASSES)
    print(
        f"  seed {r.seed}: acc={r.accuracy:.4f}  recall[{recall}]  "
        f"sev_under={r.severity_weighted_underrouting:.4f}  "
        f"ECE {r.ece_before:.4f}->{r.ece_after:.4f}  T={r.temperature:.4f}"
    )


def _print_stability(s: dict[str, Any]) -> None:
    print(f"\n3-seed stability (mean ± std over seeds {s['seeds']}):")
    print(f"  accuracy:  {s['accuracy']['mean']:.4f} ± {s['accuracy']['std']:.4f}")
    print(
        "  sev_under: "
        f"{s['severity_weighted_underrouting']['mean']:.4f} ± "
        f"{s['severity_weighted_underrouting']['std']:.4f}"
    )
    print(f"  ECE(cal):  {s['ece_after']['mean']:.4f} ± {s['ece_after']['std']:.4f}")
    print(f"  T:         {s['temperature']['mean']:.4f} ± {s['temperature']['std']:.4f}")
    for cls in CLASSES:
        m = s["per_class_recall"][cls]
        print(f"  recall {cls}: {m['mean']:.4f} ± {m['std']:.4f}")


# --- pilot-v1 R3-uplift config grid (owner-approved amendment, VAL-only) ------


def _select_config(
    results: dict[str, SeedResult],
) -> tuple[str, str]:
    """Apply the amendment's mechanical selection rule. Returns (name, why).

    Rule: among configs whose val accuracy is within 2pp of the best config's
    accuracy AND whose over-routing proxy does not exceed baseline's by more
    than 5pp, choose the one with the highest val R3 recall. If no eligible
    config beats baseline's R3 recall by ≥5pp, baseline is kept (a valid
    outcome — targeted supplemental data is the real lever).
    """
    baseline = results["baseline"]
    best_acc = max(r.accuracy for r in results.values())
    acc_floor = best_acc - V2_ACC_WITHIN_PP
    over_ceiling = baseline.over_routing_rate + V2_OVERROUTE_SLACK_PP

    eligible = {
        name: r
        for name, r in results.items()
        if r.accuracy >= acc_floor and r.over_routing_rate <= over_ceiling
    }
    # Baseline is always a valid fallback even if the filters exclude it.
    winner = max(
        eligible.values(),
        key=lambda r: r.per_class_recall["R3"],
        default=baseline,
    )
    winner_name = next(n for n, r in results.items() if r is winner)

    baseline_r3 = baseline.per_class_recall["R3"]
    uplift = winner.per_class_recall["R3"] - baseline_r3
    if winner_name == "baseline" or uplift < V2_MEANINGFUL_R3_UPLIFT_PP:
        why = (
            f"No eligible config beat baseline R3 recall by ≥"
            f"{V2_MEANINGFUL_R3_UPLIFT_PP:.2f} "
            f"(best eligible R3 uplift {uplift:+.3f}); KEEPING baseline. "
            "Targeted supplemental data is the real lever."
        )
        return "baseline", why
    why = (
        f"Highest val R3 recall ({winner.per_class_recall['R3']:.3f}, "
        f"{uplift:+.3f} vs baseline) among configs within {V2_ACC_WITHIN_PP:.2f} "
        f"acc of best ({best_acc:.4f}) and over-routing ≤ baseline+"
        f"{V2_OVERROUTE_SLACK_PP:.2f} ({over_ceiling:.4f})."
    )
    return winner_name, why


def _print_grid_table(results: dict[str, SeedResult], baseline_over: float) -> None:
    print("\n=== pilot-v1 R3-uplift config-grid val comparison (seed 42) ===")
    header = (
        f"{'config':<11} {'acc':>7} {'R0':>6} {'R1':>6} {'R2':>6} {'R3':>6} "
        f"{'sevUnd':>7} {'overRt':>7} {'ECE':>6} {'T':>6}"
    )
    print(header)
    for name in V2_TIER1_CONFIGS:
        r = results[name]
        pc = r.per_class_recall
        over_delta = r.over_routing_rate - baseline_over
        print(
            f"{name:<11} {r.accuracy:>7.4f} {pc['R0']:>6.3f} {pc['R1']:>6.3f} "
            f"{pc['R2']:>6.3f} {pc['R3']:>6.3f} "
            f"{r.severity_weighted_underrouting:>7.4f} "
            f"{r.over_routing_rate:>7.4f} {r.ece_after:>6.4f} {r.temperature:>6.3f}"
            f"  (over Δ{over_delta:+.4f})"
        )


def run_v2_tier1_grid() -> int:
    """Run the four-config R3-uplift grid on VAL only; select + export winner."""
    print("Pilot pilot-v1 R3-uplift config-grid round (owner-approved amendment)")
    print(f"  corpus:  {CORPUS_PATH}")
    print(f"  labels:  {LABELS_PATH}")
    print(f"  staging: {V2_TIER1_STAGING_DIR}")

    data = _load_features()
    train_rows, val_rows = data.train_rows, data.val_rows
    x_train, y_train = data.x_train, data.y_train
    x_val, y_val = data.x_val, data.y_val

    results: dict[str, SeedResult] = {}
    pipes: dict[str, tuple[Any, Any]] = {}
    for name, cfg in V2_TIER1_CONFIGS.items():
        print(f"\nTraining config '{name}' (seed {SHIP_SEED}) ...")
        res, pipe, clf = _train_one_seed(SHIP_SEED, x_train, y_train, x_val, y_val, cfg)
        _print_seed(res)
        print(f"    over-routing proxy (pred>gold rate): {res.over_routing_rate:.4f}")
        results[name] = res
        pipes[name] = (pipe, clf)

    baseline_over = results["baseline"].over_routing_rate
    _print_grid_table(results, baseline_over)

    selected_name, why = _select_config(results)
    print(f"\nSELECTED CONFIG: {selected_name}\n  {why}")

    sel_res = results[selected_name]
    sel_pipe, sel_clf = pipes[selected_name]
    sel_cfg = V2_TIER1_CONFIGS[selected_name]

    # --- Assemble training stats for the selected config's manifest ---
    labels_meta = json.loads(LABELS_META_PATH.read_text(encoding="utf-8"))
    split_class_counts = {
        split: dict(Counter(r.label for r in split_rows))
        for split, split_rows in (("train", train_rows), ("val", val_rows))
    }
    grid_val_table = {
        name: {
            "accuracy": r.accuracy,
            "per_class_recall": r.per_class_recall,
            "severity_weighted_underrouting": r.severity_weighted_underrouting,
            "over_routing_rate": r.over_routing_rate,
            "ece_after_calibration": r.ece_after,
            "fitted_temperature": r.temperature,
        }
        for name, r in results.items()
    }
    training_stats = {
        "pilot_version": PILOT_VERSION,
        "pilot_round": "r3_uplift_config_grid",
        "amendment": "pilot-v1 R3 uplift spec amendment (owner-approved 2026-07-19)",
        "ship_seed": SHIP_SEED,
        "architecture": "Pipeline(StandardScaler, MLPClassifier(hidden_layer_sizes=(256, 64)))",
        "selected_config": selected_name,
        "selection_rationale": why,
        "selection_rule": {
            "acc_within_pp_of_best": V2_ACC_WITHIN_PP,
            "over_routing_slack_pp_over_baseline": V2_OVERROUTE_SLACK_PP,
            "meaningful_r3_uplift_pp": V2_MEANINGFUL_R3_UPLIFT_PP,
        },
        "config_grid": {
            name: {
                "sample_weights": cfg.sample_weights,
                "oversample_multipliers": cfg.oversample_multipliers,
            }
            for name, cfg in V2_TIER1_CONFIGS.items()
        },
        "sample_weight_policy": dict(sel_cfg.sample_weights),
        "oversample_multipliers": sel_cfg.oversample_multipliers,
        "resample_strategy": ("multiplier" if sel_cfg.oversample_multipliers else "none"),
        "resample_note": (
            "VAL-only config selection per the owner amendment; TRAIN-partition "
            "oversampling (with-replacement copies of real rows) applied only for "
            "configs that declare multipliers. Validation/test kept at natural "
            "distribution; test split never opened (T9 owns the gate)."
        ),
        "set_sizes": {"train": len(train_rows), "val": len(val_rows)},
        "class_balance_per_split": split_class_counts,
        "val_grid_seed42": grid_val_table,
        "val_metrics_selected_seed42": {
            "config": selected_name,
            "accuracy": sel_res.accuracy,
            "per_class_recall": sel_res.per_class_recall,
            "severity_weighted_underrouting": sel_res.severity_weighted_underrouting,
            "over_routing_rate": sel_res.over_routing_rate,
            "ece_before_calibration": sel_res.ece_before,
            "ece_after_calibration": sel_res.ece_after,
            "nll_before_calibration": sel_res.nll_before,
            "nll_after_calibration": sel_res.nll_after,
            "fitted_temperature": sel_res.temperature,
            "confusion_gold_by_pred": sel_res.confusion,
        },
        "labeling": {
            "labeler_pin": labels_meta.get("labeler_pin"),
            "label_model": labels_meta.get("label_model"),
            "rubric_sha256": labels_meta.get("rubric_sha256"),
            "labels_file_sha256": labels_meta.get("labels_file", {}).get("labels.jsonl"),
        },
        "git_sha": _git_sha(),
        "training_scripts": [
            "scripts/pilot_router/train.py",
            "scripts/pilot_router/train_lib.py",
            "scripts/pilot_router/export_model.py",
        ],
        "pinned_deps": list(_PINNED_DEPS),
        "installed_versions": _installed_versions(),
        "feature_fingerprint": data.fingerprint,
        "hardware": platform.platform(),
        "python": platform.python_version(),
    }

    print(f"\nExporting selected config '{selected_name}' → {V2_TIER1_STAGING_DIR}")
    onnx_path, manifest_path = export_artifact(
        sel_pipe,
        sel_clf,
        V2_TIER1_STAGING_DIR,
        temperature=sel_res.temperature,
        training_stats=training_stats,
    )
    print(f"  wrote {onnx_path} ({onnx_path.stat().st_size} bytes)")
    print(f"  wrote {manifest_path}")

    _verify_load(V2_TIER1_STAGING_DIR, x_val)

    # Emit the grid table as JSON to the staging dir for the report/record.
    (V2_TIER1_STAGING_DIR / "grid_val_table.json").write_text(
        json.dumps(
            {"selected_config": selected_name, "rationale": why, "grid": grid_val_table},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(
        f"\nDONE — R3-uplift grid evaluated on val; selected '{selected_name}' "
        f"staged in {V2_TIER1_STAGING_DIR.name}. Test split remains sealed."
    )
    return 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--r3-uplift-grid",
        "--v2-tier1-grid",  # retained alias (deprecated spelling)
        dest="r3_uplift_grid",
        action="store_true",
        help="Run the pilot-v1 R3-uplift config grid (VAL-only selection) "
        "instead of the v1 baseline artifact build.",
    )
    args = parser.parse_args()
    if args.r3_uplift_grid:
        sys.exit(run_v2_tier1_grid())
    sys.exit(main())
