"""Slow latency/pathological benchmark for the Pilot feature pipeline (§6.5).

Marked ``slow`` (opt-in; excluded from the default run via ``-m "not slow"``)
because it loads the real MiniLM INT8 ONNX export and runs many encodes. It
asserts the two invariants that are contractual rather than machine-specific:

  * the pathological 100k-char paste never lets a tokenizer see more than
    8192 characters (the input bound), and
  * the hard go/no-go ceiling: warm p50 ≤ 50 ms at the 2048-char case.

The absolute p50 depends on hardware; the ceiling is generous (50 ms) and the
reference laptop measures well under it, so this is a genuine regression guard,
not a flaky micro-timing assertion.

Run explicitly::

    uv run --extra recommended pytest -m slow tests/test_pilot_benchmark.py
"""

from __future__ import annotations

import pytest

pytest.importorskip("onnxruntime")
pytest.importorskip("tokenizers")

from agentos.agentos_router.pilot.features import MAX_INPUT_CHARS  # noqa: E402
from agentos.memory.embedding import LocalEmbeddingProvider  # noqa: E402

_EXPORT_DIR = LocalEmbeddingProvider.resolve_onnx_dir("sentence-transformers/all-MiniLM-L6-v2")
_EXPORT_PRESENT = _EXPORT_DIR is not None and (_EXPORT_DIR / "model.onnx").is_file()

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not _EXPORT_PRESENT,
        reason="MiniLM INT8 export not present; run scripts/pilot_router/export_embedder.py",
    ),
]


@pytest.fixture(scope="module")
def benchmark_result():
    from scripts.pilot_router.benchmark_features import run_benchmark

    # Fewer warm iterations than the standalone runner: enough for a stable
    # p50 while keeping the opt-in test quick.
    return run_benchmark(warm_iters=15)


def test_pathological_paste_stays_within_8192_char_bound(benchmark_result):
    patho = benchmark_result.pathological
    assert patho is not None
    assert patho.length == 100_000
    # No tokenizer ever saw more than the 8192-char bound.
    assert patho.max_tokenizer_input_chars <= MAX_INPUT_CHARS
    assert patho.max_tokenizer_input_chars == MAX_INPUT_CHARS


def test_ordinary_lengths_respect_the_char_bound(benchmark_result):
    for r in benchmark_result.ordinary:
        assert r.max_tokenizer_input_chars <= MAX_INPUT_CHARS
        # For inputs shorter than the bound, the tokenizer sees the whole input.
        assert r.max_tokenizer_input_chars == r.length


def test_warm_p50_meets_hard_ceiling_at_2048(benchmark_result):
    p50 = benchmark_result.ceiling_p50_ms()
    assert benchmark_result.passes_ceiling(), (
        f"warm p50 at 2048 chars = {p50:.2f} ms exceeds the 50 ms hard ceiling"
    )


def test_count_tokens_pretrunc_is_not_capped_by_baked_in_truncation():
    """The vendored tokenizer.json bakes in a 128-token truncation config.

    ``MiniLMEncoder`` keeps a *separate* tokenizer instance for the
    pre-truncation count and must disable truncation on it explicitly
    (``.no_truncation()``); otherwise ``count_tokens_pretrunc`` silently
    caps at 128 regardless of how many tokens the text actually contains,
    corrupting ``token_count_pretrunc_8k`` for anything over that length.
    """
    from scripts.pilot_router.benchmark_features import MiniLMEncoder

    encoder = MiniLMEncoder()
    # ~500 distinct words -> well over 128 tokens, comfortably under 8192 chars.
    text = " ".join(f"word{i}" for i in range(500))
    assert len(text) < 8192
    count = encoder.count_tokens_pretrunc(text)
    assert count > 128, (
        f"count_tokens_pretrunc returned {count}, expected > 128 "
        "(truncation must be disabled on the counting tokenizer)"
    )
