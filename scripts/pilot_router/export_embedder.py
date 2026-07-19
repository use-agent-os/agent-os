#!/usr/bin/env python
"""Export the locked Pilot backbone to an INT8 ONNX bundle.

Downloads ``sentence-transformers/all-MiniLM-L6-v2`` (Apache-2.0; 6 layers,
384 dims, ~22.7M params), exports it to ONNX, applies INT8 **dynamic**
quantization via optimum/onnxruntime, and writes the bundle to::

    src/agentos/memory/models/embeddings/all-MiniLM-L6-v2-int8/

The output directory mirrors the bundled BGE export
(``memory/models/bge_onnx/``): ``model.onnx`` plus the tokenizer files and
configs, so ``LocalEmbeddingProvider.resolve_onnx_dir`` discovers it via the
``models/embeddings/{short}-int8`` convention.

An ``export_meta.json`` records the HF revision, file sha256s, quantization
method, measured INT8-vs-FP32 cosine similarity, and file sizes — the input
to the (later-task) ``manifest.json``.

Dev-time only. Requires the ``pilot-train`` dependency group::

    uv run --group pilot-train python scripts/pilot_router/export_embedder.py

Acceptance: mean INT8-vs-FP32 cosine similarity ≥ 0.99 on a fixed English
probe set. The script prints the value and exits non-zero if it is not met.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
EMBED_DIM = 384
MAX_TOKENS = 256  # matches the Pilot LOCAL_MODEL_SPECS entry
COSINE_ACCEPTANCE = 0.99

# Fixed English probe set for the INT8-vs-FP32 cosine check. Small, varied,
# and stable so the acceptance number is reproducible.
PROBE_TEXTS = [
    "How do I reset my password?",
    "Traceback (most recent call last): ValueError raised in main.py",
    "Please summarize this quarterly financial report in three bullet points.",
    "The quick brown fox jumps over the lazy dog.",
    "def add(a, b):\n    return a + b",
    "What is the capital of France?",
    "Refactor the authentication module to use dependency injection.",
    "See https://example.com/docs for the full API reference.",
    "I need help debugging a segmentation fault in my C++ program.",
    "Write a haiku about the ocean at dawn.",
]

OUTPUT_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "agentos"
    / "memory"
    / "models"
    / "embeddings"
    / "all-MiniLM-L6-v2-int8"
)

# Files the runtime bundle ships (mirrors memory/models/bge_onnx/).
BUNDLE_FILES = (
    "model.onnx",
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "vocab.txt",
)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _mean_pool(last_hidden: Any, attention_mask: Any) -> Any:
    import numpy as np

    mask = attention_mask.astype(np.float32)[..., None]
    summed = np.sum(last_hidden * mask, axis=1)
    counts = np.clip(mask.sum(axis=1), a_min=1.0, a_max=None)
    return summed / counts


def _embed(session: Any, tokenizer: Any, texts: list[str]) -> Any:
    """Encode ``texts`` through an ONNX session with mean pooling + L2."""
    import numpy as np

    enc = tokenizer(
        texts,
        padding=True,
        truncation=True,
        max_length=MAX_TOKENS,
        return_tensors="np",
    )
    input_names = {i.name for i in session.get_inputs()}
    feed = {k: v for k, v in enc.items() if k in input_names}
    last_hidden = session.run(None, feed)[0]
    pooled = _mean_pool(last_hidden, enc["attention_mask"])
    norms = np.linalg.norm(pooled, axis=1, keepdims=True)
    norms = np.clip(norms, a_min=1e-12, a_max=None)
    return pooled / norms


def _cosine(a: Any, b: Any) -> float:
    import numpy as np

    # a, b are already L2-normalised rows; cosine is the row-wise dot product.
    return float(np.mean(np.sum(a * b, axis=1)))


def export(output_dir: Path, *, keep_fp32: bool = False) -> dict[str, Any]:
    import onnxruntime as ort
    from optimum.onnxruntime import ORTModelForFeatureExtraction, ORTQuantizer
    from optimum.onnxruntime.configuration import AutoQuantizationConfig
    from transformers import AutoConfig, AutoTokenizer

    work = Path(tempfile.mkdtemp(prefix="minilm-export-"))
    fp32_dir = work / "fp32"
    int8_dir = work / "int8"

    print(f"[1/6] Downloading + exporting {MODEL_ID} to ONNX (FP32)...")
    model = ORTModelForFeatureExtraction.from_pretrained(MODEL_ID, export=True)
    model.save_pretrained(fp32_dir)
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
    tokenizer.save_pretrained(fp32_dir)

    hf_config = AutoConfig.from_pretrained(MODEL_ID)
    hf_revision = getattr(hf_config, "_commit_hash", None) or "unknown"

    print("[2/6] Applying INT8 dynamic quantization...")
    quantizer = ORTQuantizer.from_pretrained(fp32_dir)
    # Per-channel dynamic quantization. Per-tensor (per_channel=False) drops
    # MiniLM's INT8-vs-FP32 cosine to ~0.97 (below the 0.99 gate); per-channel
    # weight scales recover it to ≥ 0.99 at the same on-disk size.
    qconfig = AutoQuantizationConfig.avx512_vnni(is_static=False, per_channel=True)
    quantizer.quantize(save_dir=int8_dir, quantization_config=qconfig)
    tokenizer.save_pretrained(int8_dir)

    # optimum names the quantized file model_quantized.onnx; normalise it.
    fp32_onnx = next(fp32_dir.glob("model.onnx"))
    int8_candidates = sorted(int8_dir.glob("*quantized*.onnx")) or sorted(int8_dir.glob("*.onnx"))
    int8_onnx = int8_candidates[0]

    print("[3/6] Loading FP32 and INT8 sessions for the cosine check...")
    so = ort.SessionOptions()
    so.intra_op_num_threads = 1
    fp32_sess = ort.InferenceSession(
        str(fp32_onnx), sess_options=so, providers=["CPUExecutionProvider"]
    )
    int8_sess = ort.InferenceSession(
        str(int8_onnx), sess_options=so, providers=["CPUExecutionProvider"]
    )

    print("[4/6] Measuring INT8-vs-FP32 cosine similarity on the probe set...")
    fp32_emb = _embed(fp32_sess, tokenizer, PROBE_TEXTS)
    int8_emb = _embed(int8_sess, tokenizer, PROBE_TEXTS)
    cosine = _cosine(fp32_emb, int8_emb)
    print(f"       mean cosine(INT8, FP32) = {cosine:.6f} (acceptance ≥ {COSINE_ACCEPTANCE})")

    print(f"[5/6] Writing bundle to {output_dir} ...")
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)

    # Canonicalise the quantized model to model.onnx, copy sidecars.
    shutil.copy2(int8_onnx, output_dir / "model.onnx")
    for name in BUNDLE_FILES:
        if name == "model.onnx":
            continue
        src = int8_dir / name
        if not src.is_file():
            src = fp32_dir / name
        if src.is_file():
            shutil.copy2(src, output_dir / name)

    fp32_size = fp32_onnx.stat().st_size
    int8_size = (output_dir / "model.onnx").stat().st_size

    print("[6/6] Writing export_meta.json ...")
    file_sizes = {
        p.name: p.stat().st_size for p in sorted(output_dir.iterdir()) if p.is_file()
    }
    meta: dict[str, Any] = {
        "model_id": MODEL_ID,
        "hf_revision": hf_revision,
        "embed_dim": EMBED_DIM,
        "max_tokens": MAX_TOKENS,
        "pooling": "mean",
        "l2_normalized_by": "pilot.features.build_features",
        "quantization": {
            "method": "onnxruntime dynamic (avx512_vnni, per_channel)",
            "is_static": False,
            "per_channel": True,
        },
        "int8_vs_fp32_cosine": round(cosine, 6),
        "cosine_acceptance": COSINE_ACCEPTANCE,
        "fp32_onnx_bytes": fp32_size,
        "int8_onnx_bytes": int8_size,
        "model_onnx_sha256": _sha256(output_dir / "model.onnx"),
        "tokenizer_json_sha256": _sha256(output_dir / "tokenizer.json"),
        "file_sizes": file_sizes,
    }
    (output_dir / "export_meta.json").write_text(json.dumps(meta, indent=2) + "\n")

    if keep_fp32:
        shutil.copy2(fp32_onnx, output_dir / "model_fp32.onnx")

    return meta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=OUTPUT_DIR,
        help="destination bundle directory (default: the committed embeddings path)",
    )
    parser.add_argument(
        "--keep-fp32",
        action="store_true",
        help="also copy the FP32 onnx into the output dir (for local A/B; not committed)",
    )
    args = parser.parse_args(argv)

    meta = export(args.output_dir, keep_fp32=args.keep_fp32)

    cosine = meta["int8_vs_fp32_cosine"]
    print()
    print(f"INT8 on-disk size: {meta['int8_onnx_bytes'] / 1e6:.2f} MB")
    print(f"mean INT8-vs-FP32 cosine: {cosine:.6f}")
    if cosine < COSINE_ACCEPTANCE:
        print(
            f"FAIL: cosine {cosine:.6f} < acceptance {COSINE_ACCEPTANCE}",
            file=sys.stderr,
        )
        return 1
    print(f"PASS: cosine ≥ {COSINE_ACCEPTANCE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
