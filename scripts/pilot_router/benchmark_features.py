#!/usr/bin/env python
"""Latency benchmark for the Pilot feature pipeline (spec §6.5, T1).

Measures, on this machine, warm/cold p50/p99 for **feature extraction** and
**embed** separately, at ordinary input lengths 32 / 256 / 2048 chars plus a
pathological 100,000-char synthetic paste. Single thread,
``intra_op_num_threads=1``. The classify stage does not exist yet (later
task) and is not measured here.

Reusable as a library: ``run_benchmark()`` returns a structured result the
slow benchmark test asserts against (hard go/no-go ceiling: warm p50 ≤ 50 ms on
POSIX / 150 ms on Windows at the 2048-char case). Run standalone to print a table
and regenerate the report numbers::

    uv run --extra recommended python scripts/pilot_router/benchmark_features.py
"""

from __future__ import annotations

import gc
import os
import platform
import statistics
import time
from dataclasses import dataclass, field
from typing import Any

from agentos.agentos_router.pilot import features
from agentos.agentos_router.pilot.features import MAX_INPUT_CHARS, build_features

# Single-thread contract (spec §6.5, intra_op_num_threads=1). onnxruntime and
# its math backends read these at first session creation; MiniLMEncoder pins
# them there, before LocalEmbeddingProvider builds the session.
_THREAD_ENV_VARS = ("OMP_NUM_THREADS", "ORT_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")

# Ordinary input lengths (chars) plus the pathological paste.
ORDINARY_LENGTHS = (32, 256, 2048)
PATHOLOGICAL_LENGTH = 100_000

# Hard go/no-go ceiling (spec §6.5): warm p50 at the 2048-char case.
CEILING_MS = 50.0
CEILING_MS_WINDOWS = 150.0
CEILING_LENGTH = 2048


def active_ceiling_ms() -> float:
    """Return the active warm p50 ceiling (ms) for the current platform."""
    import os
    factor = float(os.environ.get("AGENTOS_BENCHMARK_FACTOR", "1.0"))
    base_ceiling = CEILING_MS_WINDOWS if os.name == "nt" else CEILING_MS
    return base_ceiling * factor

_WARM_ITERS = 50


def _make_text(n: int) -> str:
    """A synthetic message of ``n`` chars with some realistic structure
    (words, punctuation, a code fence, a path) so scalar extraction does
    real work rather than scanning a single repeated char."""
    unit = (
        "Refactor the auth module in src/app/auth.py? See https://ex.com/d.\n"
        "```\nprint('hi')\n```\n"
    )
    return (unit * (n // len(unit) + 1))[:n]


class MiniLMEncoder:
    """Feature-builder encoder backed by the MiniLM INT8 ONNX export.

    Wraps ``LocalEmbeddingProvider`` for the embedding and holds its own
    truncation-disabled tokenizer for the ``token_count_pretrunc_8k`` scalar
    (256-token truncation off, ``add_special_tokens=False``). The provider's
    own tokenizer keeps truncation/padding enabled for the embedding path;
    this second instance must not, so the two never share state.
    """

    def __init__(self) -> None:
        from tokenizers import Tokenizer

        from agentos.memory.embedding import LocalEmbeddingProvider

        # Pin single-thread execution before any ONNX session is built.
        for _var in _THREAD_ENV_VARS:
            os.environ.setdefault(_var, "1")

        onnx_dir = LocalEmbeddingProvider.resolve_onnx_dir(features.MINILM_MODEL_ID)
        if onnx_dir is None:
            raise RuntimeError(
                f"MiniLM export not found for {features.MINILM_MODEL_ID!r}; "
                "run scripts/pilot_router/export_embedder.py first."
            )
        self._provider = LocalEmbeddingProvider(features.MINILM_MODEL_ID, onnx_dir=onnx_dir)
        # Separate tokenizer for the pre-truncation count (no truncation),
        # kept distinct from the provider's own truncation/padding-enabled one.
        # The vendored tokenizer.json bakes in a 128-token truncation config;
        # without explicitly disabling it here, .encode() silently caps the
        # count at 128 regardless of the true token count.
        self._count_tok = Tokenizer.from_file(str(onnx_dir / "tokenizer.json"))
        self._count_tok.no_truncation()
        self._count_tok.no_padding()

    def encode_sync(self, texts: list[str]) -> Any:
        return self._provider.encode_sync(texts)

    def count_tokens_pretrunc(self, text: str) -> int:
        enc = self._count_tok.encode(text, add_special_tokens=False)
        return len(enc.ids)


@dataclass
class StageTiming:
    p50_ms: float
    p99_ms: float
    samples: int


@dataclass
class LengthResult:
    length: int
    cold_total_ms: float
    warm_feature: StageTiming
    warm_embed: StageTiming
    warm_total: StageTiming
    max_tokenizer_input_chars: int


@dataclass
class BenchmarkResult:
    hardware: str
    python: str
    ordinary: list[LengthResult] = field(default_factory=list)
    pathological: LengthResult | None = None
    rss_mb_after: float = 0.0
    int8_onnx_bytes: int = 0

    def ceiling_p50_ms(self) -> float:
        for r in self.ordinary:
            if r.length == CEILING_LENGTH:
                return r.warm_total.p50_ms
        raise LookupError(f"no result at length {CEILING_LENGTH}")

    def passes_ceiling(self) -> bool:
        return self.ceiling_p50_ms() <= active_ceiling_ms()


def _percentiles(samples_ms: list[float]) -> StageTiming:
    ordered = sorted(samples_ms)
    n = len(ordered)
    p50 = statistics.median(ordered)
    p99 = ordered[min(n - 1, int(round(0.99 * (n - 1))))]
    return StageTiming(p50_ms=p50, p99_ms=p99, samples=n)


def _rss_mb() -> float:
    try:
        import resource

        maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # macOS reports bytes; Linux reports kilobytes.
        return maxrss / 1e6 if platform.system() == "Darwin" else maxrss / 1e3
    except Exception:
        return 0.0


def _bench_length(encoder: MiniLMEncoder, length: int, *, iters: int) -> LengthResult:
    text = _make_text(length)
    scan_text = text[:MAX_INPUT_CHARS]

    # Track the largest string any tokenizer sees, to assert the 8192 bound.
    max_chars_seen = 0

    def _tracking_encode(texts: list[str]) -> Any:
        nonlocal max_chars_seen
        for t in texts:
            max_chars_seen = max(max_chars_seen, len(t))
        return encoder.encode_sync(texts)

    def _tracking_count(t: str) -> int:
        nonlocal max_chars_seen
        max_chars_seen = max(max_chars_seen, len(t))
        return encoder.count_tokens_pretrunc(t)

    class _Wrapped:
        encode_sync = staticmethod(_tracking_encode)
        count_tokens_pretrunc = staticmethod(_tracking_count)

    wrapped = _Wrapped()

    # Cold (first call, includes any lazy session/tokenizer init).
    t0 = time.perf_counter()
    build_features(text, encoder=wrapped)
    cold_total_ms = (time.perf_counter() - t0) * 1e3

    # Warm loop: time feature-extraction (scalars) and embed separately, plus
    # end-to-end total.
    feat_ms: list[float] = []
    embed_ms: list[float] = []
    total_ms: list[float] = []
    for _ in range(iters):
        t0 = time.perf_counter()
        tok_count = _tracking_count(scan_text)
        features.extract_scalars(text, token_count_pretrunc_8k=tok_count)
        feat_ms.append((time.perf_counter() - t0) * 1e3)

        t0 = time.perf_counter()
        _tracking_encode([scan_text])
        embed_ms.append((time.perf_counter() - t0) * 1e3)

        t0 = time.perf_counter()
        build_features(text, encoder=wrapped)
        total_ms.append((time.perf_counter() - t0) * 1e3)

    return LengthResult(
        length=length,
        cold_total_ms=cold_total_ms,
        warm_feature=_percentiles(feat_ms),
        warm_embed=_percentiles(embed_ms),
        warm_total=_percentiles(total_ms),
        max_tokenizer_input_chars=max_chars_seen,
    )


def run_benchmark(*, warm_iters: int = _WARM_ITERS) -> BenchmarkResult:
    encoder = MiniLMEncoder()
    # One warmup encode so the session/graph is fully initialised before the
    # per-length cold measurement isolates only that length's first call.
    encoder.encode_sync(["warmup"])

    result = BenchmarkResult(
        hardware=f"{platform.platform()} / {platform.processor() or platform.machine()}",
        python=platform.python_version(),
    )
    for length in ORDINARY_LENGTHS:
        result.ordinary.append(_bench_length(encoder, length, iters=warm_iters))

    result.pathological = _bench_length(
        encoder, PATHOLOGICAL_LENGTH, iters=max(5, warm_iters // 5)
    )
    gc.collect()
    result.rss_mb_after = _rss_mb()

    from agentos.memory.embedding import LocalEmbeddingProvider

    onnx_dir = LocalEmbeddingProvider.resolve_onnx_dir(features.MINILM_MODEL_ID)
    if onnx_dir is not None:
        model_onnx = onnx_dir / "model.onnx"
        if model_onnx.is_file():
            result.int8_onnx_bytes = model_onnx.stat().st_size
    return result


def _fmt_row(label: str, r: LengthResult) -> str:
    return (
        f"| {label} | {r.cold_total_ms:.2f} | "
        f"{r.warm_feature.p50_ms:.3f} / {r.warm_feature.p99_ms:.3f} | "
        f"{r.warm_embed.p50_ms:.2f} / {r.warm_embed.p99_ms:.2f} | "
        f"{r.warm_total.p50_ms:.2f} / {r.warm_total.p99_ms:.2f} | "
        f"{r.max_tokenizer_input_chars} |"
    )


def print_report(result: BenchmarkResult) -> None:
    print(f"Hardware: {result.hardware}")
    print(f"Python:   {result.python}")
    print(f"INT8 model.onnx size: {result.int8_onnx_bytes / 1e6:.2f} MB")
    print(f"Peak RSS after run:   {result.rss_mb_after:.1f} MB")
    print()
    print("| length | cold total ms | feat p50/p99 ms | embed p50/p99 ms "
          "| total p50/p99 ms | max tok chars |")
    print("|---|---|---|---|---|---|")
    for r in result.ordinary:
        print(_fmt_row(str(r.length), r))
    if result.pathological is not None:
        print(_fmt_row(f"{PATHOLOGICAL_LENGTH} (pathological)", result.pathological))
    print()
    p50 = result.ceiling_p50_ms()
    verdict = "GO (PASS)" if result.passes_ceiling() else "STOP (FAIL)"
    ceiling_val = active_ceiling_ms()
    print(f"Hard ceiling: warm p50 ≤ {ceiling_val:.1f} ms at {CEILING_LENGTH} chars")
    print(f"Measured warm p50 @ {CEILING_LENGTH}: {p50:.2f} ms  ->  {verdict}")


def main() -> int:
    result = run_benchmark()
    print_report(result)
    if result.pathological is not None:
        assert result.pathological.max_tokenizer_input_chars <= MAX_INPUT_CHARS, (
            "pathological paste leaked past the 8192-char bound"
        )
    return 0 if result.passes_ceiling() else 1


if __name__ == "__main__":
    raise SystemExit(main())
