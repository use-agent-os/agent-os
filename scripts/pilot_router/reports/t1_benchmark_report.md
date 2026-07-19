# T1 — MiniLM export + bounded feature builder + latency benchmark

Go/no-go stop-gate report for the Pilot router (spec
`docs/superpowers/specs/2026-07-17-pilot-router-design.md`, §6.5).

**Verdict: GO.** Warm p50 at the 2048-char case is **7.42 ms**, well under the
50 ms hard ceiling. INT8-vs-FP32 cosine is **0.9978** (≥ 0.99). The pathological
100k-char paste is bounded to 8192 chars at the tokenizer and completes without
crash or unbounded allocation.

*Re-measured after fixing a review finding: the pre-truncation counting
tokenizer (`MiniLMEncoder._count_tok`) now explicitly calls `.no_truncation()`
(the vendored `tokenizer.json` bakes in a 128-token truncation config that
otherwise silently capped `token_count_pretrunc_8k` at 128 for any input
tokenizing past that). Latency moved slightly (a true count on longer inputs
does marginally more tokenizer work than the old capped-at-128 count); the
verdict is unchanged.*

## 1. Export

- Backbone (owner-locked): `sentence-transformers/all-MiniLM-L6-v2`
  (Apache-2.0; 6 layers, 384 dims, ~22.7M params).
- HF revision: `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`.
- Path: `src/agentos/memory/models/embeddings/all-MiniLM-L6-v2-int8/`
  (resolved by `LocalEmbeddingProvider.resolve_onnx_dir` via the
  `models/embeddings/{short}-int8` convention).
- Quantization: onnxruntime **dynamic INT8**, `avx512_vnni`, **per-channel**.
  Per-tensor (`per_channel=False`) was tried first and measured cosine
  **0.9713** — below the 0.99 gate; per-channel weight scales recover fidelity
  to **0.9978** at the same on-disk size (~23 MB).
- **INT8-vs-FP32 mean cosine: 0.997833** (probe set of 10 fixed English texts).
  Acceptance ≥ 0.99 → **PASS**.
- Measured on-disk size of `model.onnx` (INT8): **22.97 MB** (22,966,011 bytes);
  FP32 reference was 90.36 MB.
- Full metadata (revision, sha256s, sizes, cosine) recorded in
  `export_meta.json` in the bundle dir.

Reproduce:
```sh
uv run --group pilot-train --extra recommended python scripts/pilot_router/export_embedder.py
```

## 2. Feature contract (as shipped)

- Input bound: `scan_text = message[:8192]` before any work. Every scalar except
  `char_count_full` operates on `scan_text`; no tokenizer receives > 8192 chars.
- Embedding: MiniLM INT8 (mean pool, 256-token right truncation, no prefix) via
  the explicit `LOCAL_MODEL_SPECS` entry, then **explicit L2 normalization** in
  the feature builder (provider returns raw vectors).
- Eight scalars, exact order: `log1p_char_count_full`,
  `log1p_token_count_pretrunc_8k`, `log1p_line_count_8k`, `has_code_fence_8k`,
  `log1p_code_line_count_8k`, `has_traceback_8k`, `log1p_question_mark_count_8k`,
  `has_file_or_url_reference_8k`.
- Output: `float32 [392]` = 384-dim L2 embedding ++ 8 scalars.

## 3. Latency benchmark (spec §6.5)

- **Hardware (reference laptop):** macOS-26.5.2-arm64 (Apple silicon).
- **Python:** 3.12.13.
- **Threads:** single thread — `intra_op_num_threads=1` enforced via
  `OMP/ORT/OPENBLAS/MKL_NUM_THREADS=1` set before session creation.
- **INT8 model.onnx size:** 22.97 MB.
- **Peak RSS after full run (incl. 100k paste):** ~171-176 MB.
- **Classify stage:** does not exist yet; it is measured at a later task
  (spec §11). Only feature-extraction and embed are measured here.

Warm = median over 50 iterations after a warmup encode. Cold = first call at
that length (includes any lazy per-length work).

| length (chars) | cold total ms | feat p50/p99 ms | embed p50/p99 ms | total p50/p99 ms | max tokenizer input chars |
|---|---|---|---|---|---|
| 32 | 1.34 | 0.014 / 0.027 | 0.66 / 0.76 | 0.68 / 0.84 | 32 |
| 256 | 4.01 | 0.065 / 0.072 | 3.46 / 3.69 | 3.54 / 3.90 | 256 |
| 2048 | 7.96 | 0.469 / 0.760 | 7.02 / 11.83 | **7.59 / 11.01** | 2048 |
| 100000 (pathological) | 10.83 | 1.897 / 1.952 | 8.54 / 8.93 | 10.62 / 11.17 | **8192** |

Notes:
- Numbers are stable across runs (2048-char warm p50 measured 7.42–7.59 ms
  across repeat runs after the counting-tokenizer truncation fix). Absolute
  values are hardware-specific; the slow test asserts only the invariants
  (8192 bound + 50 ms ceiling), not exact timings.
- Feature-extraction (the pure scalar/regex path) is sub-millisecond up to
  2048 chars; embed dominates end-to-end latency, as expected.
- The pathological 100k paste's cost is bounded because both the encoder and
  the pre-truncation token counter see at most `scan_text` (8192 chars). Its
  `max tokenizer input chars` is exactly 8192 — the bound holds.

## 4. Go/no-go

- **Hard ceiling (spec §6.5):** warm p50 ≤ 50 ms at 2048 chars on the reference
  laptop.
- **Measured:** 7.42 ms.
- **Verdict: GO (PASS)** — ~6.7x margin under the ceiling.

The ≤ 15 ms p50 figure from spec Rev 1 is a working hypothesis, not a criterion;
the measured budget (with owner-approved slack) is set from these numbers before
T7 (training) starts.

Reproduce:
```sh
uv run --extra recommended python scripts/pilot_router/benchmark_features.py
uv run --extra recommended pytest -m slow tests/test_pilot_benchmark.py
```

## 5. Concerns / follow-ups

- **Review fix round 1:** the pre-truncation counting tokenizer previously
  inherited the vendored `tokenizer.json`'s baked-in 128-token truncation
  config, silently capping `token_count_pretrunc_8k` at 128 for any input
  tokenizing past that (verified: 500-token input returned 128). Fixed by
  calling `.no_truncation()` / `.no_padding()` on `MiniLMEncoder._count_tok`
  in `scripts/pilot_router/benchmark_features.py`. All numbers in this report
  are re-measured post-fix.
- **Review fix round 1:** `build_features` previously defaulted
  `token_count_pretrunc_8k` to `0` when the encoder lacked
  `count_tokens_pretrunc` (`getattr(..., None)` fallback), which would have
  silently zeroed that scalar for a bare `LocalEmbeddingProvider`.
  `count_tokens_pretrunc` is now a required member of the `PilotEncoder`
  protocol; an encoder missing it raises `AttributeError` instead.
- Runtime inference deps (`onnxruntime`, `numpy`, `tokenizers`) intentionally
  stay in the `recommended`/`ml-router` extras; promoting them and the
  release-workflow/wheel guards for the new embeddings path are explicitly
  deferred to later tasks.
- `manifest.json` (superset of `export_meta.json`) is a later task.
