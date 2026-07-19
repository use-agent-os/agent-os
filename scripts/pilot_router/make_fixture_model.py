#!/usr/bin/env python
"""Generate the Pilot router *fixture* model (T2).

The fixture is a tiny scikit-learn ``Pipeline(StandardScaler, MLPClassifier)``
trained on synthetic Gaussian blobs — 4 classes, 392-dim inputs, fixed seed —
and exported with the **exact production ONNX contract** so every contract test
in the suite runs against a real, loadable artifact pair long before the real
trained model (T7) exists.

The classifier is deliberately trivial; what must match production exactly is
the ONNX **IO contract**: a single ``input: float32 [N, 392]`` tensor and the
``skl2onnx`` outputs ``label: int64 [N]`` + ``probabilities: float32 [N, 4]``
(ZipMap disabled so probabilities are a plain tensor, not a list of dicts).

The artifact pair (``model.onnx`` + ``manifest.json``) is written to::

    tests/test_agentos_router/data/pilot_fixture/

and committed alongside this script. Regenerate with::

    uv run --group pilot-train python scripts/pilot_router/make_fixture_model.py

Dev-time only. Requires the ``pilot-train`` dependency group
(``scikit-learn`` + ``skl2onnx``).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np

from agentos.agentos_router.pilot.features import (
    FEATURE_DIM,
    FILE_RE,
    MINILM_MODEL_ID,
    SCALAR_FEATURE_NAMES,
    URL_RE,
)

# --- Binding fixture constants ----------------------------------------------

#: Pinned class order (Pilot spec, Rev 4). Index-aligned with the probability
#: tensor columns; the manifest and the loaded graph must agree.
CLASSES: list[str] = ["R0", "R1", "R2", "R3"]

#: Fixture temperature. T=1.0 makes the calibrated output equal the graph probs.
FIXTURE_TEMPERATURE = 1.0

#: Pinned ONNX IO names (skl2onnx defaults with zipmap disabled).
INPUT_NAME = "input"
OUTPUT_LABEL_NAME = "label"
OUTPUT_PROBA_NAME = "probabilities"

#: Deterministic training seed.
SEED = 20260717

#: Feature-schema version carried in the manifest.
FEATURE_SCHEMA_VERSION = "1"

#: Fixture pilot version — clearly marked as a fixture, not a trained model.
PILOT_VERSION = "fixture-0"

#: Where the export_meta.json for the locked MiniLM backbone lives, so the
#: fixture's encoder-contract fields stay consistent with the real embedder.
_EXPORT_META_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "agentos"
    / "memory"
    / "models"
    / "embeddings"
    / "all-MiniLM-L6-v2-int8"
    / "export_meta.json"
)

#: Fixture artifact output directory.
_FIXTURE_DIR = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "test_agentos_router"
    / "data"
    / "pilot_fixture"
)


def _make_blobs(n_per_class: int, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    """Synthetic Gaussian blobs — one well-separated centre per class.

    Labels are the integers ``0..3`` (index-aligned to :data:`CLASSES`), so the
    exported graph's ``label`` output stays ``int64`` — the production
    contract. The human-readable class names live in the manifest ``classes``
    field, aligned column-for-column with the probability tensor.
    """
    xs: list[np.ndarray] = []
    ys: list[int] = []
    for idx in range(len(CLASSES)):
        centre = np.zeros(FEATURE_DIM, dtype=np.float32)
        # Place each class centre on a distinct axis so the tiny MLP separates
        # them cleanly and deterministically.
        centre[idx] = 4.0
        pts = centre + rng.standard_normal((n_per_class, FEATURE_DIM)).astype(np.float32)
        xs.append(pts.astype(np.float32))
        ys.extend([idx] * n_per_class)
    x = np.concatenate(xs, axis=0).astype(np.float32)
    y = np.array(ys, dtype=np.int64)
    return x, y


def _build_manifest(model_onnx_bytes: bytes, export_meta: dict[str, Any]) -> dict[str, Any]:
    sha256 = hashlib.sha256(model_onnx_bytes).hexdigest()
    return {
        "pilot_version": PILOT_VERSION,
        "is_fixture": True,
        "classes": list(CLASSES),
        "temperature": FIXTURE_TEMPERATURE,
        "embedder_id": MINILM_MODEL_ID,
        "model_onnx_sha256": sha256,
        "io_contract": {
            "input": {
                "name": INPUT_NAME,
                "dtype": "float32",
                "shape": [None, FEATURE_DIM],
            },
            "outputs": [
                {"name": OUTPUT_LABEL_NAME, "dtype": "int64", "shape": [None]},
                {
                    "name": OUTPUT_PROBA_NAME,
                    "dtype": "float32",
                    "shape": [None, len(CLASSES)],
                },
            ],
        },
        "encoder_contract": {
            "pooling": "mean",
            "normalization": "l2",
            "max_input_chars": 8192,
            "max_tokens": 256,
            "truncation_side": "right",
            "model_revision": export_meta["hf_revision"],
            "tokenizer_sha256": export_meta["tokenizer_json_sha256"],
        },
        "feature_schema": {
            "version": FEATURE_SCHEMA_VERSION,
            "scalar_names": list(SCALAR_FEATURE_NAMES),
            "feature_dim": FEATURE_DIM,
            # Spec §6.5: pin the exact reference-regex strings (inline (?i)
            # flags included) so PilotModel can trip on train/serve drift.
            "url_regex": URL_RE.pattern,
            "file_regex": FILE_RE.pattern,
        },
        "training_stats": {
            "_note": "FIXTURE placeholder values — synthetic blobs, not real training.",
            "n_train": len(CLASSES) * 60,
            "seed": SEED,
            "class_balance": {label: 0.25 for label in CLASSES},
        },
    }


def build_fixture(out_dir: Path) -> tuple[Path, Path]:
    """Train + export the fixture pair into ``out_dir``. Returns (onnx, manifest)."""
    from skl2onnx import to_onnx
    from skl2onnx.common.data_types import FloatTensorType
    from sklearn.neural_network import MLPClassifier
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    rng = np.random.default_rng(SEED)
    x_train, y_train = _make_blobs(60, rng)

    clf = MLPClassifier(hidden_layer_sizes=(16,), max_iter=300, random_state=SEED)
    pipe = Pipeline([("scaler", StandardScaler()), ("clf", clf)])
    pipe.fit(x_train, y_train)

    # Integer labels 0..3 → class order is the pinned index order; the manifest
    # maps those columns to CLASSES.
    assert list(pipe.classes_) == list(range(len(CLASSES))), (
        f"unexpected class order: {list(pipe.classes_)}"
    )

    onnx_model = to_onnx(
        pipe,
        initial_types=[(INPUT_NAME, FloatTensorType([None, FEATURE_DIM]))],
        options={id(clf): {"zipmap": False}},
        target_opset=17,
    )
    model_bytes = onnx_model.SerializeToString()

    export_meta = json.loads(_EXPORT_META_PATH.read_text(encoding="utf-8"))
    manifest = _build_manifest(model_bytes, export_meta)

    out_dir.mkdir(parents=True, exist_ok=True)
    onnx_path = out_dir / "model.onnx"
    manifest_path = out_dir / "manifest.json"
    onnx_path.write_bytes(model_bytes)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return onnx_path, manifest_path


def main() -> None:
    onnx_path, manifest_path = build_fixture(_FIXTURE_DIR)
    print(f"wrote {onnx_path} ({onnx_path.stat().st_size} bytes)")
    print(f"wrote {manifest_path}")


if __name__ == "__main__":
    main()
