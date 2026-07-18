"""Pilot router feature builder — bounded, deterministic feature contract.

Covers the binding contract from the Pilot router spec (§6.5/§9, T1):
the 8192-char input bound, the exact eight-scalar schema and order, the
pinned file/URL regexes, MiniLM INT8 → mean-pool → L2 embedding, and the
392-dim float32 output. The embedding path is exercised against a stub
encoder so these tests stay offline and deterministic; a separate slow
benchmark test drives the real ONNX export.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from agentos.agentos_router.pilot import features
from agentos.agentos_router.pilot.features import (
    MAX_INPUT_CHARS,
    SCALAR_FEATURE_NAMES,
    build_features,
    extract_scalars,
)
from agentos.memory.embedding import LOCAL_MODEL_SPECS


class _StubEncoder:
    """Records the texts it is asked to encode and returns a fixed raw vector.

    Mimics ``LocalEmbeddingProvider.encode_sync``: takes a list of texts and
    returns a raw (un-normalised) ``(N, 384)`` float32 array. The returned
    vector is deliberately un-normalised so the feature builder's L2 step is
    observable.
    """

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim
        self.seen: list[list[str]] = []

    def encode_sync(self, texts, **_kwargs):
        self.seen.append(list(texts))
        # Constant non-unit vector: L2 norm is sqrt(dim) * 3, clearly != 1.
        return np.full((len(texts), self.dim), 3.0, dtype=np.float32)


# --- Model spec: no unknown-model default -----------------------------------


def test_minilm_resolves_to_explicit_spec():
    """MiniLM must have an explicit LOCAL_MODEL_SPECS entry — production
    must never rely on the unknown-model (CLS/no-prefix) default."""
    assert features.MINILM_MODEL_ID == "sentence-transformers/all-MiniLM-L6-v2"
    assert features.MINILM_MODEL_ID in LOCAL_MODEL_SPECS
    spec = LOCAL_MODEL_SPECS[features.MINILM_MODEL_ID]
    assert spec.pooling == "mean"
    assert spec.max_tokens == 256
    assert spec.query_prefix == ""
    assert spec.document_prefix == ""
    assert spec.dims == 384


# --- Scalar schema / order --------------------------------------------------


def test_scalar_feature_names_exact_order():
    assert SCALAR_FEATURE_NAMES == (
        "log1p_char_count_full",
        "log1p_token_count_pretrunc_8k",
        "log1p_line_count_8k",
        "has_code_fence_8k",
        "log1p_code_line_count_8k",
        "has_traceback_8k",
        "log1p_question_mark_count_8k",
        "has_file_or_url_reference_8k",
    )


def test_extract_scalars_returns_eight_in_order():
    scalars = extract_scalars("hello world?", token_count_pretrunc_8k=2)
    assert scalars.shape == (8,)
    assert scalars.dtype == np.float32
    # char_count_full = len("hello world?") = 12
    assert scalars[0] == pytest.approx(math.log1p(12))
    # token_count passed in explicitly = 2
    assert scalars[1] == pytest.approx(math.log1p(2))
    # one line
    assert scalars[2] == pytest.approx(math.log1p(1))
    # no code fence
    assert scalars[3] == 0.0
    # question-mark count = 1
    assert scalars[6] == pytest.approx(math.log1p(1))


def test_char_count_full_uses_full_message_not_scan_text():
    """Scalar 1 (char_count_full) counts the full message; everything else
    operates on the 8192-char scan_text."""
    long_msg = "a" * (MAX_INPUT_CHARS + 500)
    scalars = extract_scalars(long_msg, token_count_pretrunc_8k=0)
    # char_count_full is the full length, not the bound
    assert scalars[0] == pytest.approx(math.log1p(MAX_INPUT_CHARS + 500))


# --- Code fence / traceback / lines -----------------------------------------


def test_code_fence_and_code_line_count():
    msg = "intro\n```\nline1\nline2\n```\nouter"
    scalars = extract_scalars(msg, token_count_pretrunc_8k=0)
    assert scalars[3] == 1.0  # has_code_fence_8k
    # two lines inside the fence
    assert scalars[4] == pytest.approx(math.log1p(2))


def test_no_code_fence():
    scalars = extract_scalars("just prose\nmore prose", token_count_pretrunc_8k=0)
    assert scalars[3] == 0.0
    assert scalars[4] == pytest.approx(math.log1p(0))


def test_traceback_detection():
    tb = 'Traceback (most recent call last):\n  File "x.py", line 3\nValueError'
    scalars = extract_scalars(tb, token_count_pretrunc_8k=0)
    assert scalars[5] == 1.0


def test_no_traceback():
    scalars = extract_scalars("no error here", token_count_pretrunc_8k=0)
    assert scalars[5] == 0.0


# --- File / URL reference regex ---------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "see https://example.com/path?q=1 for details",
        "http://localhost:8080/foo",
        "edit src/agentos/main.py please",
        "open ./relative/path/module.ts",
        "check config.toml",
        "the file data.yaml is broken",
        r"look at C:\Users\me\script.py",
        r"windows path D:/proj/app.rs here",
        "README.md has the answer",
    ],
)
def test_file_or_url_reference_positive(text):
    scalars = extract_scalars(text, token_count_pretrunc_8k=0)
    assert scalars[7] == 1.0, f"expected file/url match in {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        "just some plain english prose with no references",
        "the word python is not a file",
        "version 3.12 of the runtime",
        "a sentence ending in a period.",
        "ftp://not-http-or-https.example",
    ],
)
def test_file_or_url_reference_negative(text):
    scalars = extract_scalars(text, token_count_pretrunc_8k=0)
    assert scalars[7] == 0.0, f"expected NO file/url match in {text!r}"


# --- Full build: shape, dtype, L2, bound ------------------------------------


def test_build_features_shape_and_dtype():
    encoder = _StubEncoder()
    vec = build_features("hello world", encoder=encoder)
    assert vec.shape == (392,)
    assert vec.dtype == np.float32


def test_embedding_is_l2_normalized():
    encoder = _StubEncoder()
    vec = build_features("hello", encoder=encoder)
    embedding = vec[:384]
    norm = float(np.linalg.norm(embedding))
    assert norm == pytest.approx(1.0, abs=1e-5)


def test_scalars_appended_after_embedding():
    encoder = _StubEncoder()
    vec = build_features("hi?", encoder=encoder)
    scalars = vec[384:]
    assert scalars.shape == (8,)
    # question-mark count = 1
    assert scalars[6] == pytest.approx(math.log1p(1))


def test_input_bounded_to_8192_chars_before_encoding():
    """The encoder must never receive more than 8192 characters."""
    encoder = _StubEncoder()
    long_msg = "x" * (MAX_INPUT_CHARS + 10_000)
    build_features(long_msg, encoder=encoder)
    assert len(encoder.seen) == 1
    (encoded_text,) = encoder.seen[0]
    assert len(encoded_text) == MAX_INPUT_CHARS


def test_char_count_full_survives_bound_in_full_vector():
    encoder = _StubEncoder()
    long_msg = "x" * (MAX_INPUT_CHARS + 123)
    vec = build_features(long_msg, encoder=encoder)
    assert vec[384] == pytest.approx(math.log1p(MAX_INPUT_CHARS + 123))
