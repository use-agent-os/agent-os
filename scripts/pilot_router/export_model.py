#!/usr/bin/env python
"""Export a trained Pilot pipeline to the production ONNX artifact (T7).

Given a fitted ``Pipeline(StandardScaler, MLPClassifier)`` (the LOCKED
architecture from :mod:`train_lib`), the calibrated temperature ``T``, and the
training-stats block, this writes the shipped artifact pair::

    <out_dir>/model.onnx      # input: float32 [N, 392]
                              # outputs: label int64 [N], probabilities f32 [N,4]
    <out_dir>/manifest.json   # spec §6.3 full recording; loads via PilotModel

The ONNX export uses ``skl2onnx.to_onnx`` with ``options={id(clf):
{"zipmap": False}}`` so ``probabilities`` is a plain tensor (not a ZipMap list
of dicts) — the exact production contract T2's ``PilotModel`` validates. Integer
labels ``0..3`` map column-for-column to ``["R0","R1","R2","R3"]``.

The manifest's encoder-contract and embedder fields are copied verbatim from the
locked MiniLM ``export_meta.json`` (revision + tokenizer sha256), so a drifted
backbone or tokenizer trips ``PilotModel`` validation at load.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from agentos.agentos_router.pilot.features import (
    FEATURE_DIM,
    FILE_RE,
    MINILM_MODEL_ID,
    SCALAR_FEATURE_NAMES,
    URL_RE,
)
from scripts.pilot_router.train_lib import CLASSES

# Pinned ONNX IO names (skl2onnx defaults with zipmap disabled).
INPUT_NAME = "input"
OUTPUT_LABEL_NAME = "label"
OUTPUT_PROBA_NAME = "probabilities"

FEATURE_SCHEMA_VERSION = "1"
PILOT_VERSION = "pilot-v1"

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXPORT_META_PATH = (
    _REPO_ROOT
    / "src"
    / "agentos"
    / "memory"
    / "models"
    / "embeddings"
    / "all-MiniLM-L6-v2-int8"
    / "export_meta.json"
)


def pipeline_to_onnx_bytes(pipe: Any, clf: Any) -> bytes:
    """Serialize the fitted pipeline to ONNX bytes (production IO contract)."""
    from skl2onnx import to_onnx
    from skl2onnx.common.data_types import FloatTensorType

    onnx_model = to_onnx(
        pipe,
        initial_types=[(INPUT_NAME, FloatTensorType([None, FEATURE_DIM]))],
        options={id(clf): {"zipmap": False}},
        target_opset=17,
    )
    result: bytes = onnx_model.SerializeToString()
    return result


def build_manifest(
    model_onnx_bytes: bytes,
    *,
    temperature: float,
    training_stats: dict[str, Any],
    export_meta: dict[str, Any] | None = None,
    pilot_version: str = PILOT_VERSION,
) -> dict[str, Any]:
    """Assemble the spec §6.3 manifest for the trained artifact.

    ``training_stats`` is recorded verbatim (set sizes, per-split class balance,
    val metrics, 3-seed stability, resampling decision, sample-weight policy,
    git SHA, labeler pin, rubric sha256, dep pins). ``export_meta`` defaults to
    the locked MiniLM ``export_meta.json``; the smoke test passes a stub.
    """
    if export_meta is None:
        export_meta = json.loads(_EXPORT_META_PATH.read_text(encoding="utf-8"))

    sha256 = hashlib.sha256(model_onnx_bytes).hexdigest()
    return {
        "pilot_version": pilot_version,
        "is_fixture": False,
        "classes": list(CLASSES),
        "temperature": float(temperature),
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
            "url_regex": URL_RE.pattern,
            "file_regex": FILE_RE.pattern,
        },
        "training_stats": training_stats,
    }


def export_artifact(
    pipe: Any,
    clf: Any,
    out_dir: Path,
    *,
    temperature: float,
    training_stats: dict[str, Any],
    export_meta: dict[str, Any] | None = None,
    pilot_version: str = PILOT_VERSION,
) -> tuple[Path, Path]:
    """Export ``(model.onnx, manifest.json)`` into ``out_dir``. Returns paths."""
    model_bytes = pipeline_to_onnx_bytes(pipe, clf)
    manifest = build_manifest(
        model_bytes,
        temperature=temperature,
        training_stats=training_stats,
        export_meta=export_meta,
        pilot_version=pilot_version,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    onnx_path = out_dir / "model.onnx"
    manifest_path = out_dir / "manifest.json"
    onnx_path.write_bytes(model_bytes)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return onnx_path, manifest_path
