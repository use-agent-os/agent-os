"""The v4_phase3 bundle must actually load and route.

Every other v4 test constructs the strategy with ``bundle_dir="/nonexistent"``
and asserts the degradation path (tier c1, confidence 0.0, source
"v4_unavailable"). That is worth keeping, but on its own it let the bundle go
missing from the wheel while ``strategy="v4_phase3"`` stayed the default: the
router loaded, reported no error beyond one boot warning, and pinned every turn
to c1. These tests exercise the real artifacts so that regression cannot repeat
silently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentos.agentos_router.v4_phase3 import V4Phase3Strategy, default_bundle_dir

_LFS_POINTER = b"version https://git-lfs.github.com/spec/v1"

_WEIGHTS = (
    "lgbm_main.bin",
    "lgbm_aux.bin",
    "mlp/model.onnx",
    "mlp/scaler.joblib",
    "features/tfidf.pkl",
    "features/svd.pkl",
    "features/bge_pca.joblib",
)

TIERS = ["c0", "c1", "c2", "c3"]


def _require_bundle() -> Path:
    """Skip when the ML extras or hydrated weights are unavailable.

    Both are present in CI (``uv sync --extra recommended`` plus an
    ``lfs: true`` checkout) and in any wheel install, but a bare ``--extra dev``
    venv or a source clone without ``git lfs pull`` has neither.
    """
    for module in ("numpy", "lightgbm", "onnxruntime", "sklearn", "joblib", "tokenizers"):
        pytest.importorskip(module)

    bundle = default_bundle_dir()
    if not bundle.is_dir():
        pytest.skip(f"v4 bundle not present at {bundle}")
    for name in _WEIGHTS:
        path = bundle / name
        if not path.is_file():
            pytest.skip(f"v4 bundle weight missing: {name}")
        if path.read_bytes()[:64].startswith(_LFS_POINTER):
            pytest.skip(f"v4 bundle weight is an unhydrated LFS pointer: {name}")
    return bundle


def test_bundle_loads_from_the_shipped_assets() -> None:
    _require_bundle()

    # require_router_runtime=True turns the silent degradation into a raise, so
    # a broken bundle fails here as an error rather than a passing assertion
    # about c1.
    strategy = V4Phase3Strategy(require_router_runtime=True)

    assert strategy._available is True
    assert strategy._model_version == "v4"


def test_bundle_does_not_duplicate_the_shared_bge_export() -> None:
    bundle = _require_bundle()

    # ~23MB of the wheel. memory/models/bge_onnx is the single copy; the router
    # resolves it through LocalEmbeddingProvider rather than shipping its own.
    assert not (bundle / "bge_onnx").exists()

    strategy = V4Phase3Strategy(require_router_runtime=True)
    extractor = strategy._core.bge_extractor
    resolved = Path(extractor.onnx_model_dir)

    assert extractor.backend == "onnx"
    assert resolved.is_dir()
    assert resolved.parts[-3:] == ("memory", "models", "bge_onnx")
    assert (resolved / "model.onnx").is_file()
    assert (resolved / "tokenizer.json").is_file()


async def test_classify_returns_a_real_prediction() -> None:
    _require_bundle()
    strategy = V4Phase3Strategy(require_router_runtime=True)

    tier, confidence, source, meta = await strategy.classify("hi", list(TIERS))

    assert source == "v4_phase3"  # not "v4_unavailable"
    assert tier in TIERS
    assert confidence > 0.0
    assert meta["route_class"] in {"R0", "R1", "R2", "R3"}
    assert meta["model_version"] == "v4"


async def test_classify_separates_trivial_from_hard_turns() -> None:
    """The whole point of the router. A constant classifier passes every
    assertion above but fails this one."""
    _require_bundle()
    strategy = V4Phase3Strategy(require_router_runtime=True)

    trivial_tier, _, trivial_source, _ = await strategy.classify("hi", list(TIERS))
    hard_tier, _, hard_source, _ = await strategy.classify(
        "My deploy is failing. Traceback (most recent call last):\n"
        '  File "app/main.py", line 42, in handler\n'
        "    result = client.fetch(url)\n"
        "ConnectionResetError: [Errno 104] Connection reset by peer\n"
        "This only reproduces in production under load. What is the root cause?",
        list(TIERS),
    )

    assert trivial_source == hard_source == "v4_phase3"
    assert TIERS.index(hard_tier) > TIERS.index(trivial_tier)


async def test_classify_honours_the_valid_tier_allowlist() -> None:
    _require_bundle()
    strategy = V4Phase3Strategy(require_router_runtime=True)

    for _ in range(2):
        tier, _, _, _ = await strategy.classify("hi", ["c2", "c3"])
        assert tier in {"c2", "c3"}
