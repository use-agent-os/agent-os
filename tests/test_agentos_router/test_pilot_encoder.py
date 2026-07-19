"""Contract tests for the production ``_MiniLMEncoder`` (strategy.py).

These pin the encoder actually used by real pilot-v1 dispatch — not the
benchmark-script twin in ``scripts/pilot_router/benchmark_features.py`` — to
the spec's ``token_count_pretrunc_8k`` contract (Pilot router spec, Rev 4,
§4.4): pinned MiniLM tokenizer, truncation disabled, and
``add_special_tokens=False`` for the count. Every other Pilot test injects a
stub encoder, so without this file the production adapter has zero coverage
and can silently drift from the trained/benchmarked contract (train/serve
feature skew).
"""

from __future__ import annotations

import pytest

pytest.importorskip("tokenizers")

from agentos.agentos_router.pilot.features import EMBED_DIM  # noqa: E402
from agentos.agentos_router.pilot.strategy import _MiniLMEncoder  # noqa: E402
from agentos.memory.embedding import LocalEmbeddingProvider  # noqa: E402

_EXPORT_DIR = LocalEmbeddingProvider.resolve_onnx_dir("sentence-transformers/all-MiniLM-L6-v2")

pytestmark = pytest.mark.skipif(
    _EXPORT_DIR is None or not (_EXPORT_DIR / "tokenizer.json").is_file(),
    reason="vendored MiniLM export not present",
)


def test_count_tokens_pretrunc_excludes_special_tokens() -> None:
    """Spec §4.4: the count uses ``add_special_tokens=False`` — no [CLS]/[SEP].

    The tokenizers-library default (``add_special_tokens=True``) would inflate
    every count by 2, skewing ``log1p_token_count_pretrunc_8k`` at serve time
    relative to the training/benchmark pipeline.
    """
    encoder = _MiniLMEncoder()
    assert encoder.count_tokens_pretrunc("") == 0
    assert encoder.count_tokens_pretrunc("hello world") == 2


def test_count_tokens_pretrunc_matches_spec_reference_tokenizer() -> None:
    """The production count equals a from-scratch spec-configured tokenizer."""
    from pathlib import Path

    from tokenizers import Tokenizer

    assert _EXPORT_DIR is not None
    reference = Tokenizer.from_file(str(Path(_EXPORT_DIR) / "tokenizer.json"))
    reference.no_truncation()
    reference.no_padding()

    encoder = _MiniLMEncoder()
    for text in (
        "hello world",
        "please refactor src/agentos/agentos_router/pilot/strategy.py?",
        "Traceback (most recent call last):\n  ValueError: boom",
        "```python\nprint('hi')\n```",
    ):
        expected = len(reference.encode(text, add_special_tokens=False).ids)
        assert encoder.count_tokens_pretrunc(text) == expected


def test_count_tokens_pretrunc_is_not_capped_by_baked_in_truncation() -> None:
    """The vendored tokenizer.json bakes in truncation; the production adapter
    must disable it (``.no_truncation()``) or counts silently cap, corrupting
    ``token_count_pretrunc_8k`` for long messages. Mirrors the benchmark-twin
    guard in ``tests/test_pilot_benchmark.py`` for the class real dispatch uses.
    """
    encoder = _MiniLMEncoder()
    text = " ".join(f"word{i}" for i in range(500))
    count = encoder.count_tokens_pretrunc(text)
    assert count > 128, (
        f"count_tokens_pretrunc returned {count}, expected > 128 "
        "(truncation must be disabled on the counting tokenizer)"
    )


def test_encode_sync_returns_embed_dim_float32_rows() -> None:
    """``encode_sync`` yields raw ``(N, EMBED_DIM)`` float32 vectors."""
    pytest.importorskip("onnxruntime")
    import numpy as np

    assert _EXPORT_DIR is not None
    if not (_EXPORT_DIR / "model.onnx").is_file():
        pytest.skip("MiniLM model.onnx not present")

    encoder = _MiniLMEncoder()
    out = encoder.encode_sync(["hello world", "explain this traceback"])
    arr = np.asarray(out)
    assert arr.shape == (2, EMBED_DIM)
    assert arr.dtype == np.float32


def test_encode_sync_right_truncates_at_256_tokens() -> None:
    """Spec §9: the encode path applies 256-token RIGHT truncation.

    Observed behaviorally in the default suite (not just as a config-value
    pin): the base text tokenizes far past the cap AND past the model's
    512-position limit, so if ``enable_truncation`` regressed this would
    fault the ONNX session rather than pass. Texts differing only beyond the
    cap must embed identically; a changed PREFIX must not (right side is the
    truncated side).
    """
    pytest.importorskip("onnxruntime")
    import numpy as np

    assert _EXPORT_DIR is not None
    if not (_EXPORT_DIR / "model.onnx").is_file():
        pytest.skip("MiniLM model.onnx not present")

    encoder = _MiniLMEncoder()
    base = "hello " * 600  # ~600 tokens: > 256 cap and > 512 position limit
    longer = "hello " * 700
    suffix_changed = base + "zebra unicorn"
    prefix_changed = "zebra unicorn " + base

    out = np.asarray(
        encoder.encode_sync([base, longer, suffix_changed, prefix_changed])
    )
    assert np.isfinite(out).all()
    # Content beyond the 256-token cap is ignored → identical embeddings.
    np.testing.assert_allclose(out[1], out[0], atol=1e-6)
    np.testing.assert_allclose(out[2], out[0], atol=1e-6)
    # Leading tokens survive truncation → a prefix edit must be visible.
    assert not np.allclose(out[3], out[0], atol=1e-6)
