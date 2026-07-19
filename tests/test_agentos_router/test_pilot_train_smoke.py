"""Offline CI smoke test for the Pilot `pilot-v1` train→export→load path (T7).

This exercises the *real* T7 code paths — ``train_lib`` (join, feature matrix,
locked pipeline, temperature fit, evaluation) and ``export_model`` (skl2onnx
export + manifest) — end-to-end, but with:

* a synthetic 50-row corpus+labels pair written to a tmp dir (no real corpus),
* a deterministic **stub encoder** (no MiniLM weights, no network, fork-safe),
* a stub ``export_meta`` (no dependency on the committed MiniLM export),

then asserts the exported artifact loads through the production ``PilotModel``
and predicts a normalized ``[N, 4]`` distribution — with exact 392-dim parity.

Requires the ``pilot-train`` group (scikit-learn + skl2onnx). It is skipped when
those are absent so the default offline suite (which lacks the group) does not
error; CI that installs the group runs it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("skl2onnx", reason="pilot-train group (skl2onnx) not installed")
pytest.importorskip("sklearn", reason="pilot-train group (scikit-learn) not installed")

from agentos.agentos_router.pilot.features import EMBED_DIM, FEATURE_DIM  # noqa: E402
from agentos.agentos_router.pilot.model import PilotModel  # noqa: E402
from scripts.pilot_router.export_model import export_artifact  # noqa: E402
from scripts.pilot_router.train_lib import (  # noqa: E402
    CLASSES,
    build_feature_matrix,
    evaluate,
    fit_temperature,
    load_split_rows,
    train_pipeline,
)


class _StubEncoder:
    """Deterministic ``PilotEncoder`` — no weights, no network, fork-safe.

    ``encode_sync`` hashes each text to a stable 384-dim vector whose direction
    depends on the text, and it nudges the vector toward a class-signal axis
    keyed by a marker substring so the tiny MLP has a learnable signal. The
    feature builder L2-normalizes the raw vector, so magnitude is irrelevant.
    """

    _MARKERS = {"[[R0]]": 0, "[[R1]]": 1, "[[R2]]": 2, "[[R3]]": 3}

    def encode_sync(self, texts: list[str]) -> np.ndarray:
        out = np.empty((len(texts), EMBED_DIM), dtype=np.float32)
        for i, t in enumerate(texts):
            digest = hashlib.sha256(t.encode("utf-8")).digest()
            seed = int.from_bytes(digest[:8], "little")
            rng = np.random.default_rng(seed)
            vec = rng.standard_normal(EMBED_DIM).astype(np.float32) * 0.1
            for marker, idx in self._MARKERS.items():
                if marker in t:
                    vec[idx] += 5.0
            out[i] = vec
        return out

    def count_tokens_pretrunc(self, text: str) -> int:
        return len(text.split())


def _write_synthetic_corpus(dir_path: Path) -> tuple[Path, Path]:
    """Write a 50-row synthetic corpus+labels pair with all 4 classes present.

    Each row's text embeds a ``[[Rk]]`` marker so the stub encoder produces a
    separable signal; train/val splits both cover all four classes.
    """
    rng = np.random.default_rng(20260717)
    corpus_lines: list[str] = []
    label_lines: list[str] = []
    n = 0
    # 40 train + 10 val, cycling classes so every class appears in both splits.
    plan = [("train", 40), ("val", 10)]
    for split, count in plan:
        for k in range(count):
            cls = CLASSES[k % 4]
            tid = f"t{n:04d}"
            text = f"synthetic turn {n} {cls} marker [[{cls}]] extra words here"
            corpus_lines.append(
                json.dumps(
                    {
                        "turn_id": tid,
                        "conversation_id": f"c{n:04d}",
                        "text": text,
                        "category": "factual_qa",
                        "split": split,
                    }
                )
            )
            label_lines.append(
                json.dumps(
                    {
                        "turn_id": tid,
                        "conversation_id": f"c{n:04d}",
                        "split": split,
                        "category": "factual_qa",
                        "label": cls,
                        "why": "synthetic",
                        "agreement": True,
                        "adjudicated": False,
                        "boundary_set": False,
                    }
                )
            )
            n += 1
    _ = rng  # reserved for future jitter; determinism preserved above
    corpus_path = dir_path / "corpus.jsonl"
    labels_path = dir_path / "labels.jsonl"
    corpus_path.write_text("\n".join(corpus_lines) + "\n", encoding="utf-8")
    labels_path.write_text("\n".join(label_lines) + "\n", encoding="utf-8")
    return corpus_path, labels_path


_STUB_EXPORT_META = {
    "hf_revision": "stub-revision-0000",
    "tokenizer_json_sha256": "0" * 64,
}


def test_train_export_load_round_trip(tmp_path: Path) -> None:
    corpus_path, labels_path = _write_synthetic_corpus(tmp_path)
    rows = load_split_rows(corpus_path, labels_path)
    assert len(rows["train"]) == 40
    assert len(rows["val"]) == 10
    assert len(rows["test"]) == 0  # no test rows written; T7 never opens test

    encoder = _StubEncoder()
    x_train, y_train = build_feature_matrix(rows["train"], encoder)
    x_val, y_val = build_feature_matrix(rows["val"], encoder)

    # Exact 392-dim parity (the headline T7 assertion).
    assert x_train.shape == (40, FEATURE_DIM)
    assert x_val.shape == (10, FEATURE_DIM)
    assert x_train.dtype == np.float32

    pipe = train_pipeline(x_train, y_train, seed=42, max_iter=120)
    clf = pipe.named_steps["clf"]

    # Calibrate on the (synthetic) validation split only.
    val_probs = pipe.predict_proba(x_val).astype(np.float32)
    temperature = fit_temperature(val_probs, y_val)
    assert temperature > 0.0

    metrics = evaluate(val_probs, y_val)
    assert 0.0 <= metrics.accuracy <= 1.0
    assert set(metrics.per_class_recall) == set(CLASSES)

    training_stats = {
        "_note": "SMOKE synthetic — not real training",
        "seed": 42,
        "n_train": 40,
        "n_val": 10,
    }
    out_dir = tmp_path / "artifact"
    onnx_path, manifest_path = export_artifact(
        pipe,
        clf,
        out_dir,
        temperature=temperature,
        training_stats=training_stats,
        export_meta=_STUB_EXPORT_META,
    )
    assert onnx_path.is_file()
    assert manifest_path.is_file()

    # Round-trip: the exported pair must load through the production loader.
    model = PilotModel(out_dir)
    assert model.available, model.unavailable_reason
    assert model.classes == CLASSES
    assert model.temperature == pytest.approx(temperature)

    probs = model.predict_proba(x_val)
    assert probs.shape == (10, 4)
    assert probs.dtype == np.float32
    np.testing.assert_allclose(probs.sum(axis=1), np.ones(10), atol=1e-5)

    # sha256 in the manifest matches the bytes on disk.
    manifest = json.loads(manifest_path.read_text())
    assert manifest["model_onnx_sha256"] == hashlib.sha256(onnx_path.read_bytes()).hexdigest()
    assert manifest["pilot_version"] == "pilot-v1"
    assert manifest["is_fixture"] is False


def test_feature_matrix_rejects_dim_mismatch(tmp_path: Path) -> None:
    """A rogue encoder that returns the wrong embed width trips parity."""

    class _BadEncoder:
        def encode_sync(self, texts: list[str]) -> np.ndarray:
            return np.zeros((len(texts), EMBED_DIM - 1), dtype=np.float32)

        def count_tokens_pretrunc(self, text: str) -> int:
            return 1

    corpus_path, labels_path = _write_synthetic_corpus(tmp_path)
    rows = load_split_rows(corpus_path, labels_path)
    with pytest.raises((ValueError, IndexError)):
        build_feature_matrix(rows["train"][:1], _BadEncoder())
