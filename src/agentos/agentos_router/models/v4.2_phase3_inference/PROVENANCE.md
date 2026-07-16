# AgentOSRouter V4 Phase 3 Bundle Provenance

This directory contains the local inference bundle used by
`agentos.agentos_router.v4_phase3.V4Phase3Strategy`.

## Purpose

The bundle provides the V4 Phase 3 local model router used to classify a turn
into route classes `R0` through `R3`, which are then mapped to configured model
tiers by AgentOS gateway configuration. This provenance file does not change
runtime behavior; it records the assets that the existing runtime loads.

## Bundled Asset Groups

- `lgbm_main.bin` and `lgbm_aux.bin`: LightGBM booster files for router heads.
- `mlp/model.onnx` and `mlp/scaler.joblib`: MLP head model and scaler.
- `features/tfidf.pkl`, `features/svd.pkl`, `features/config.pkl`, and
  `features/bge_pca.joblib`: scikit-learn/joblib feature extraction artifacts.
- The BGE ONNX export and tokenizer files derived from `BAAI/bge-small-zh-v1.5`
  are **not** stored in this directory. They ship once under
  `src/agentos/memory/models/bge_onnx` and are shared with memory's local
  embedder; `inference/artifacts.py::_resolve_bge_onnx_dir` resolves them via
  `agentos.memory.embedding.LocalEmbeddingProvider.resolve_onnx_dir`.
- `router.runtime.yaml`, `version.json`, and `inference_manifest.json`: router
  runtime configuration and inference metadata.

## Upstream Bundle Origin

This bundle — the trained model artifacts **and** the `runtime_src/` inference
code — originates from OpenSquilla and is **not** trained or authored by the
AgentOS contributors:

- Upstream project: https://github.com/opensquilla/opensquilla
- Upstream path: `src/opensquilla/squilla_router/models/v4.2_phase3_inference/`
- License: Apache License 2.0
- Copyright notice: OpenSquilla contributors (the upstream project ships the
  stock Apache-2.0 text without a filled-in copyright line and no NOTICE file).

The LightGBM boosters, the PyTorch-exported MLP head, and the fitted
scikit-learn feature artifacts are OpenSquilla's trained weights, used here
byte-for-byte unmodified — `lgbm_main.bin` carries the same Git LFS object
(`sha256:5f312db09577bbaf30f87358941974eef6edce7f1424d0e9de21cbd38a646d53`,
39684725 bytes) as upstream, and the `runtime_src/` modules are byte-identical
apart from the modifications recorded below.

In accordance with Section 4(b) of the Apache License 2.0, this notice records
that the following files have been modified by the AgentOS contributors
relative to upstream:

- Namespace/branding renames throughout (`opensquilla` → `agentos`,
  `squilla_router` → `agentos_router`).
- `runtime_src/src/router/inference/artifacts.py` — `_resolve_bge_onnx_dir`
  falls back to the shared BGE export under `src/agentos/memory/models/bge_onnx`
  so the ~23MB export is shipped once rather than duplicated in this bundle.
- `artifact_manifest.json` and this file — `bge_onnx/*` entries removed to match
  that deduplication.

The whole AgentOS repository is licensed under the Apache License 2.0 (see
`LICENSE`), so the upstream license terms apply uniformly. The repository-root
`THIRD_PARTY_NOTICES.md` records this bundle under its OpenSquilla section.

## Upstream Model Attribution

The BGE assets this bundle consumes (from `src/agentos/memory/models/bge_onnx`)
are derived from `BAAI/bge-small-zh-v1.5`:

- Hugging Face model: https://huggingface.co/BAAI/bge-small-zh-v1.5
- Upstream project: https://github.com/FlagOpen/FlagEmbedding
- License: MIT

The upstream MIT notice is recorded in the repository root
`THIRD_PARTY_NOTICES.md`.

Note that the BGE model is a dependency of the router's `bge_x3` feature
channel, not its base: the routing decision comes from OpenSquilla's LightGBM +
MLP heads, which consume BGE embeddings as one of several input channels.

## Conversion Notes

The repository contains runtime router metadata including feature dimensions,
route classes, and the BGE model name. Runtime behavior is defined by the
checked-in artifacts and configuration listed below.

Current known metadata:

- `version.json` records the router version, feature channels,
  `BAAI/bge-small-zh-v1.5`, and backend `onnx`.
- `inference_manifest.json` records feature dimensions, route classes,
  temperature, class alpha values, BGE backend, and BGE ONNX directory.

## Safety Notes

The current runtime deserializes `.pkl` and `.joblib` artifacts through
`joblib.load`. Treat those files as executable-code-equivalent inputs. Only use
assets shipped with a trusted AgentOS release or assets whose size and
sha256 match `artifact_manifest.json`.

## Update Procedure

When any router asset changes:

1. Regenerate the `sha256`/`size_bytes` entries in `artifact_manifest.json` for
   every file in this directory except `bge_onnx/*` (shared, see above).
   Upstream's `scripts/update_router_artifact_manifest.py` and
   `tests/test_ci/test_router_artifact_manifest.py` do this, but neither was
   carried over into AgentOS — regenerate by hand or port them back.
2. Review the changed `artifact_manifest.json` entries.
3. Run `uv run pytest tests/test_agentos_router/test_v4_phase3_bundle.py -q` to
   confirm the bundle still loads and produces real (non-degraded) predictions.
4. Include any required notice or provenance changes in the same commit.
