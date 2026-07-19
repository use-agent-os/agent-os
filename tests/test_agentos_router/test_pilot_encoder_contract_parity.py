"""Train/serve parity tripwire for the Pilot ``encoder_contract`` (spec §4.3).

The manifest pins the encoder contract (pooling / normalization / max_input_chars
/ max_tokens / truncation-side / model-revision / tokenizer-sha256) and the model
loader load-validates the *presence and regex shape* of feature-schema fields
(``pilot.model``). But nothing load-validated that the pinned ``encoder_contract``
*values* actually agree with the runtime artifacts the encoder loads — a stale
manifest revision or a re-exported tokenizer would drift silently.

This is a default-suite test (not load-time validation — that would break the
fail-soft "degrade on a partial/absent bundle" contract): it cross-checks the
committed fixture manifest's ``encoder_contract`` against

* the sha256 of the bundled ``tokenizer.json`` the encoder actually loads,
* the export metadata's ``hf_revision``,
* the ``LOCAL_MODEL_SPECS`` MiniLM entry (pooling / max_tokens / no prefix),
* ``pilot.features.MAX_INPUT_CHARS``,
* the pinned right-truncation / l2 / mean contract.

Skips (does not fail) when the vendored MiniLM export is absent, so a minimal
checkout still runs green.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agentos.agentos_router.pilot.features import MAX_INPUT_CHARS, MINILM_MODEL_ID
from agentos.memory.embedding import LOCAL_MODEL_SPECS, LocalEmbeddingProvider

_FIXTURE_MANIFEST = (
    Path(__file__).parent / "data" / "pilot_fixture" / "manifest.json"
)
_EXPORT_DIR = LocalEmbeddingProvider.resolve_onnx_dir(MINILM_MODEL_ID)

pytestmark = pytest.mark.skipif(
    _EXPORT_DIR is None
    or not (_EXPORT_DIR / "tokenizer.json").is_file()
    or not (_EXPORT_DIR / "export_meta.json").is_file(),
    reason="vendored MiniLM export not present",
)


def _encoder_contract() -> dict:
    manifest = json.loads(_FIXTURE_MANIFEST.read_text(encoding="utf-8"))
    return manifest["encoder_contract"]


def _export_meta() -> dict:
    assert _EXPORT_DIR is not None  # narrowed by pytestmark
    return json.loads((_EXPORT_DIR / "export_meta.json").read_text(encoding="utf-8"))


def test_manifest_tokenizer_sha256_matches_bundled_tokenizer() -> None:
    assert _EXPORT_DIR is not None
    tok_bytes = (_EXPORT_DIR / "tokenizer.json").read_bytes()
    actual = hashlib.sha256(tok_bytes).hexdigest()
    assert _encoder_contract()["tokenizer_sha256"] == actual


def test_manifest_model_revision_matches_export_meta() -> None:
    assert _encoder_contract()["model_revision"] == _export_meta()["hf_revision"]


def test_manifest_pooling_and_max_tokens_match_local_model_spec() -> None:
    spec = LOCAL_MODEL_SPECS[MINILM_MODEL_ID]
    contract = _encoder_contract()
    assert contract["pooling"] == spec.pooling == "mean"
    assert contract["max_tokens"] == spec.max_tokens == 256
    # The MiniLM backbone rides with no text prefix (§4.4); the spec must agree.
    assert spec.query_prefix == ""
    assert spec.document_prefix == ""


def test_manifest_max_input_chars_matches_feature_builder() -> None:
    assert _encoder_contract()["max_input_chars"] == MAX_INPUT_CHARS == 8192


def test_manifest_truncation_side_and_normalization_pinned() -> None:
    contract = _encoder_contract()
    assert contract["truncation_side"] == "right"
    assert contract["normalization"] == "l2"
