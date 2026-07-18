"""Bounded, deterministic feature builder for the Pilot router (T1).

The binding feature contract (Pilot router spec, Rev 4, §6.5/§9):

* Input is bounded to the first ``MAX_INPUT_CHARS`` (8192) Unicode code
  points before any work: ``scan_text = message[:8192]``. Every scalar
  except ``char_count_full`` operates on ``scan_text``, and no tokenizer
  ever receives more than 8192 characters — so a pathological paste stays
  bounded.
* Embedding: MiniLM INT8 via ``LocalEmbeddingProvider`` (attention-mask mean
  pooling, 256-token right truncation, no text prefix — the explicit
  ``LOCAL_MODEL_SPECS`` entry), followed by explicit L2 normalization here.
  The provider returns raw vectors; the L2 step is owned by this builder.
* Eight scalars in exactly the order named in ``SCALAR_FEATURE_NAMES``.
* Output: the 384-dim L2 embedding concatenated with the 8 scalars → one
  ``float32`` vector of shape ``(392,)``.

The heavy embedding/tokeniser work goes through a small ``PilotEncoder``
protocol so the pure feature logic (bound + scalars + regexes) stays
testable offline without loading the ONNX model.
"""

from __future__ import annotations

import math
import re
from typing import Protocol

import numpy as np

# --- Binding constants ------------------------------------------------------

#: Owner-locked MiniLM backbone. Registered explicitly in
#: ``agentos.memory.embedding.LOCAL_MODEL_SPECS`` (mean pool, 256 tokens, no
#: prefix) so production never rides on the unknown-model default.
MINILM_MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"

#: Input bound (Unicode code points). No tokenizer ever sees more than this.
MAX_INPUT_CHARS = 8192

#: Embedding dimensionality of all-MiniLM-L6-v2.
EMBED_DIM = 384

#: Number of scalar features appended after the embedding.
NUM_SCALARS = 8

#: Full feature-vector width: embedding + scalars.
FEATURE_DIM = EMBED_DIM + NUM_SCALARS

#: The eight scalars, in exactly this order and naming. Contract — do not
#: reorder or rename; training and runtime must agree byte-for-byte.
SCALAR_FEATURE_NAMES: tuple[str, ...] = (
    "log1p_char_count_full",
    "log1p_token_count_pretrunc_8k",
    "log1p_line_count_8k",
    "has_code_fence_8k",
    "log1p_code_line_count_8k",
    "has_traceback_8k",
    "log1p_question_mark_count_8k",
    "has_file_or_url_reference_8k",
)

# --- Pinned reference regexes (compiled once) -------------------------------
# These exact strings are the contract; do not "improve" them.

URL_RE = re.compile(r"(?i)\bhttps?://[^\s<>()]+")
FILE_RE = re.compile(
    r"(?i)(?:\b[a-z]:[\\/]|(?:^|[\s(]))"
    r"(?:[\w.@+-]+[\\/])*[\w.@+-]+\."
    r"(?:py|pyi|js|jsx|ts|tsx|json|toml|ya?ml|md|txt|sh|sql|rs|go|java|c|cc|cpp|h|hpp)\b"
)

# A markdown code fence: three or more backticks at the start of a line.
_CODE_FENCE_RE = re.compile(r"^\s*```", re.MULTILINE)
# Python-style traceback header.
_TRACEBACK_RE = re.compile(r"(?m)^Traceback \(most recent call last\):")


class PilotEncoder(Protocol):
    """Minimal surface the feature builder needs from the MiniLM backbone.

    ``encode_sync`` returns raw (un-normalised) ``(N, EMBED_DIM)`` float32
    vectors — the same shape/contract as
    ``LocalEmbeddingProvider.encode_sync``. L2 normalization is applied by
    the feature builder, not the encoder.
    """

    def encode_sync(self, texts: list[str]) -> np.ndarray: ...


def _has_code_fence(scan_text: str) -> bool:
    return _CODE_FENCE_RE.search(scan_text) is not None


def _code_line_count(scan_text: str) -> int:
    """Number of content lines enclosed by markdown code fences in
    ``scan_text``. Fence delimiter lines themselves do not count; an
    unterminated final fence counts the lines that follow it."""
    inside = False
    count = 0
    for line in scan_text.splitlines():
        if _CODE_FENCE_RE.match(line):
            inside = not inside
            continue
        if inside:
            count += 1
    return count


def _has_traceback(scan_text: str) -> bool:
    return _TRACEBACK_RE.search(scan_text) is not None


def _has_file_or_url_reference(scan_text: str) -> bool:
    return URL_RE.search(scan_text) is not None or FILE_RE.search(scan_text) is not None


def extract_scalars(message: str, *, token_count_pretrunc_8k: int) -> np.ndarray:
    """Compute the eight scalar features as a ``float32 (8,)`` array.

    ``char_count_full`` uses the *full* ``message``; every other scalar
    operates on ``scan_text = message[:MAX_INPUT_CHARS]``. The pre-truncation
    token count is passed in by the caller (it needs the pinned tokenizer),
    so this function stays free of any model dependency.
    """
    scan_text = message[:MAX_INPUT_CHARS]

    char_count_full = len(message)
    line_count = len(scan_text.splitlines()) if scan_text else 0
    has_fence = _has_code_fence(scan_text)
    code_line_count = _code_line_count(scan_text) if has_fence else 0
    has_tb = _has_traceback(scan_text)
    question_mark_count = scan_text.count("?")
    has_ref = _has_file_or_url_reference(scan_text)

    return np.array(
        [
            math.log1p(char_count_full),
            math.log1p(token_count_pretrunc_8k),
            math.log1p(line_count),
            1.0 if has_fence else 0.0,
            math.log1p(code_line_count),
            1.0 if has_tb else 0.0,
            math.log1p(question_mark_count),
            1.0 if has_ref else 0.0,
        ],
        dtype=np.float32,
    )


def _l2_normalize(vec: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vec))
    if norm == 0.0:
        return vec.astype(np.float32)
    return (vec / norm).astype(np.float32)


def build_features(
    message: str,
    *,
    encoder: PilotEncoder,
    token_count_pretrunc_8k: int | None = None,
) -> np.ndarray:
    """Build the full ``float32 (392,)`` Pilot feature vector for ``message``.

    Steps, in order:

    1. Bound the input: ``scan_text = message[:MAX_INPUT_CHARS]``.
    2. Embed ``scan_text`` with the MiniLM encoder (mean pool, 256-token
       right truncation, no prefix), then L2-normalise the raw vector.
    3. Compute the eight scalars and append them.

    ``token_count_pretrunc_8k`` may be supplied by the caller (e.g. a
    ``MiniLMEncoder`` that already tokenised ``scan_text`` truncation-off);
    when ``None`` the encoder is asked for it via ``count_tokens_pretrunc``.
    """
    scan_text = message[:MAX_INPUT_CHARS]

    raw = encoder.encode_sync([scan_text])
    embedding = _l2_normalize(np.asarray(raw[0], dtype=np.float32))

    if token_count_pretrunc_8k is None:
        counter = getattr(encoder, "count_tokens_pretrunc", None)
        token_count_pretrunc_8k = counter(scan_text) if counter is not None else 0

    scalars = extract_scalars(message, token_count_pretrunc_8k=token_count_pretrunc_8k)
    return np.concatenate([embedding, scalars]).astype(np.float32)
