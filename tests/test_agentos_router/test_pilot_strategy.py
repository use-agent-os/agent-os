"""Contract suite for ``PilotStrategy`` (Pilot router spec, Rev 4, §4/§9).

These tests drive the strategy through a deterministic stub encoder and the
committed fixture model artifact (``tests/test_agentos_router/data/
pilot_fixture/``). They pin:

* ``requires_history is True`` (engine guard gating) while the classifier input
  stays current-turn-only (history-invariance of raw probabilities);
* the two-case confidence contract (§4.1: not-fired → P(top-1); fired → m);
* ``extra`` completeness (every consumer field present, ``final_*`` absent);
* fail-soft degrade semantics (no artifacts → DEFAULT_TEXT_TIER / 0.0 /
  ``pilot_unavailable`` / degraded extra), and the ``require_router_runtime``
  escape hatch that turns a load failure into a raise.

Engine-interaction tests live in ``tests/test_engine/test_router_pilot_guards.py``.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from agentos.agentos_router.pilot import PilotStrategy
from agentos.agentos_router.pilot.features import EMBED_DIM
from agentos.router_tiers import DEFAULT_TEXT_TIER

FIXTURE_DIR = Path(__file__).parent / "data" / "pilot_fixture"


class _StubEncoder:
    """Deterministic ``PilotEncoder`` returning a fixed vector per text.

    The raw vector depends only on the text length so identical messages map to
    identical embeddings regardless of the surrounding call — the property the
    history-invariance test relies on. Non-unit magnitude keeps the builder's L2
    step observable.
    """

    def __init__(self) -> None:
        self.seen: list[list[str]] = []

    def encode_sync(self, texts: list[str]) -> np.ndarray:
        self.seen.append(list(texts))
        rows = []
        for text in texts:
            seed = (len(text) % 97) + 1
            rows.append(np.full(EMBED_DIM, float(seed), dtype=np.float32))
        return np.asarray(rows, dtype=np.float32)

    def count_tokens_pretrunc(self, text: str) -> int:
        return len(text.split())


class _ProbEncoder(_StubEncoder):
    """Encoder that drives the fixture model toward a chosen argmax class.

    The fixture ONNX is a real trained-on-synthetic-blobs model, so we can't
    dictate its output directly; instead we scan a handful of RNG seeds offline
    (see the module tests) and pick embeddings that land on the class we need.
    """

    def __init__(self, vector: np.ndarray) -> None:
        super().__init__()
        self._vector = vector

    def encode_sync(self, texts: list[str]) -> np.ndarray:
        self.seen.append(list(texts))
        return np.asarray([self._vector for _ in texts], dtype=np.float32)


def _make_strategy(
    *,
    artifact_dir: Path | str | None = FIXTURE_DIR,
    encoder: object | None = None,
    safety_net_threshold: float = 0.5,
    confidence_threshold: float = 0.5,
    require_router_runtime: bool = False,
) -> PilotStrategy:
    return PilotStrategy(
        artifact_dir=artifact_dir,
        encoder=encoder if encoder is not None else _StubEncoder(),
        safety_net_threshold=safety_net_threshold,
        confidence_threshold=confidence_threshold,
        require_router_runtime=require_router_runtime,
    )


def _seed_vector_for_argmax(target_argmax: int) -> np.ndarray:
    """Find a 384-d embedding whose fixture prediction argmaxes to ``target``.

    Only R1 (1) and R2 (2) are reachable through the L2-normalised
    ``build_features`` path against this synthetic fixture model — R0/R3 argmax
    is not producible, so the confidence tests are built around R1/R2, the
    classes the fixture can actually emit.
    """
    from agentos.agentos_router.pilot.features import build_features
    from agentos.agentos_router.pilot.model import PilotModel

    model = PilotModel(FIXTURE_DIR)
    assert model.available
    for seed in range(400):
        rng = np.random.default_rng(seed)
        vector = rng.standard_normal(EMBED_DIM).astype(np.float32)
        encoder = _ProbEncoder(vector)
        feats = build_features(
            "probe message", encoder=encoder, token_count_pretrunc_8k=3
        )
        probs = model.predict_proba(feats.reshape(1, -1))[0]
        if int(np.argmax(probs)) == target_argmax:
            return vector
    raise AssertionError(f"no seed produced argmax {target_argmax}")


# --- requires_history / plumbing ------------------------------------------


def test_artifact_dir_tilde_is_expanded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured ``~/...`` artifact dir must expand at dispatch, not only in
    the boot/doctor asset probe — otherwise preflight reports the router ready
    while every turn degrades to ``pilot_unavailable``."""
    import shutil

    home = tmp_path / "home"
    shutil.copytree(FIXTURE_DIR, home / "pilot_bundle")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # expanduser on Windows

    strategy = _make_strategy(artifact_dir="~/pilot_bundle")
    assert strategy.artifact_dir == home / "pilot_bundle"
    assert strategy._available is True


def test_requires_history_is_true() -> None:
    strategy = _make_strategy()
    assert strategy.requires_history is True


def test_source_tag_healthy_is_pilot_v1() -> None:
    strategy = _make_strategy()
    assert strategy.source == "pilot_v1"


# --- classify success path -------------------------------------------------


@pytest.mark.asyncio
async def test_classify_returns_tier_confidence_source_extra() -> None:
    strategy = _make_strategy()
    tier, confidence, source, extra = await strategy.classify(
        "please refactor this parser", ["c0", "c1", "c2", "c3"]
    )
    assert tier in {"c0", "c1", "c2", "c3"}
    assert 0.0 <= confidence <= 1.0
    assert source == "pilot_v1"
    assert isinstance(extra, dict)


@pytest.mark.asyncio
async def test_extra_completeness_and_no_final_fields() -> None:
    strategy = _make_strategy()
    _tier, _conf, _source, extra = await strategy.classify(
        "explain this traceback", ["c0", "c1", "c2", "c3"]
    )
    expected = {
        "route_class",
        "top1_label",
        "probabilities",
        "difficulty",
        "margin",
        "thinking_mode",
        "prompt_policy",
        "flags",
        "model_version",
        "safety_net_applied",
    }
    assert expected <= set(extra), f"missing: {expected - set(extra)}"
    # Engine-owned fields must never be set by the strategy.
    assert not any(key.startswith("final_") for key in extra), extra
    assert extra["top1_label"] == extra["route_class"]
    assert extra["flags"] == []
    assert isinstance(extra["probabilities"], dict)
    assert set(extra["probabilities"]) == {"R0", "R1", "R2", "R3"}


@pytest.mark.asyncio
async def test_difficulty_is_expected_weighted_sum() -> None:
    strategy = _make_strategy()
    _tier, _conf, _source, extra = await strategy.classify(
        "hello there", ["c0", "c1", "c2", "c3"]
    )
    probs = extra["probabilities"]
    expected = sum(i * probs[cls] for i, cls in enumerate(["R0", "R1", "R2", "R3"]))
    assert extra["difficulty"] == pytest.approx(expected)


# --- valid_tiers honouring -------------------------------------------------


@pytest.mark.asyncio
async def test_honours_valid_tiers_walks_upward_when_preferred_excluded() -> None:
    # Force the model onto R2 (→ c2) with a high safety-net threshold so the net
    # cannot fire and re-target the class, then exclude c2 so the strategy must
    # walk upward to the next configured tier (c3), never down to c0/c1.
    vector = _seed_vector_for_argmax(2)
    strategy = _make_strategy(
        encoder=_ProbEncoder(vector),
        safety_net_threshold=1.0,
        confidence_threshold=1.0,
    )
    tier, _conf, _source, extra = await strategy.classify(
        "probe message", ["c0", "c1", "c3"]
    )
    assert extra["route_class"] == "R2"
    assert extra["safety_net_applied"] is False
    assert tier == "c3", "excluded preferred tier must walk upward, not down"


# --- confidence contract (§4.1) --------------------------------------------


@pytest.mark.asyncio
async def test_confidence_not_fired_is_top1_probability() -> None:
    # An R2 argmax with a high t_eff so the safety net cannot re-target: the net
    # does not fire (argmax is already R2), so confidence == P(top-1).
    vector = _seed_vector_for_argmax(2)
    strategy = _make_strategy(
        encoder=_ProbEncoder(vector),
        safety_net_threshold=1.0,
        confidence_threshold=1.0,
    )
    _tier, confidence, _source, extra = await strategy.classify(
        "probe message", ["c0", "c1", "c2", "c3"]
    )
    assert extra["safety_net_applied"] is False
    top1 = extra["probabilities"][extra["route_class"]]
    assert confidence == pytest.approx(top1)


@pytest.mark.asyncio
async def test_confidence_fired_is_escalation_mass() -> None:
    # An R1 argmax whose combined R2+R3 mass exceeds the default t_eff (0.5): the
    # safety net fires, the route is bumped to R2 (→ c2), and confidence is the
    # escalation mass m (not the R1 argmax probability).
    vector = _seed_vector_for_argmax(1)
    strategy = _make_strategy(encoder=_ProbEncoder(vector))
    tier, confidence, _source, extra = await strategy.classify(
        "probe message", ["c0", "c1", "c2", "c3"]
    )
    probs = extra["probabilities"]
    mass = probs["R2"] + probs["R3"]
    assert mass > 0.5, "fixture R1 seed must carry escalation mass above t_eff"
    assert extra["safety_net_applied"] is True
    assert extra["route_class"] == "R2"
    assert tier == "c2"
    assert confidence == pytest.approx(mass)


# --- history invariance ----------------------------------------------------


@pytest.mark.asyncio
async def test_history_invariance_identical_probabilities() -> None:
    """Identical message + different routing_history → identical raw probs."""
    strategy = _make_strategy()
    message = "diagnose the failing integration test"
    _t1, _c1, _s1, extra_a = await strategy.classify(
        message, ["c0", "c1", "c2", "c3"], routing_history=None
    )
    _t2, _c2, _s2, extra_b = await strategy.classify(
        message,
        ["c0", "c1", "c2", "c3"],
        routing_history=[{"route_class": "R3", "final_tier": "c3", "_ts": 1.0}],
    )
    assert extra_a["probabilities"] == extra_b["probabilities"]
    assert extra_a["route_class"] == extra_b["route_class"]


@pytest.mark.asyncio
async def test_over_bound_message_classifies_without_error() -> None:
    """A message far past MAX_INPUT_CHARS classifies cleanly (bounding is T1's).

    Guards the strategy against pre-slicing the message (which would cap
    char_count_full and corrupt log1p_char_count_full); the strategy passes the
    full message to build_features and lets it bound the tokenizer/embed inputs.
    """
    strategy = _make_strategy()
    long_message = "word " * 5000  # ~25k chars, well past the 8192 bound
    tier, confidence, source, extra = await strategy.classify(
        long_message, ["c0", "c1", "c2", "c3"]
    )
    assert source == "pilot_v1"
    assert tier in {"c0", "c1", "c2", "c3"}
    assert 0.0 <= confidence <= 1.0
    assert set(extra["probabilities"]) == {"R0", "R1", "R2", "R3"}


@pytest.mark.asyncio
async def test_history_kwargs_ignored_for_feature_building() -> None:
    """prev_assistant_text / history_user_texts must not alter the features."""
    strategy = _make_strategy()
    message = "summarise this module"
    _t1, _c1, _s1, extra_a = await strategy.classify(
        message, ["c0", "c1", "c2", "c3"]
    )
    _t2, _c2, _s2, extra_b = await strategy.classify(
        message,
        ["c0", "c1", "c2", "c3"],
        prev_assistant_text="a long previous assistant turn",
        history_user_texts=["earlier user turn one", "earlier user turn two"],
        prev_assistant_usage={"input_tokens": 5000},
    )
    assert extra_a["probabilities"] == extra_b["probabilities"]


# --- degrade semantics -----------------------------------------------------


@pytest.mark.asyncio
async def test_degrades_without_artifacts() -> None:
    strategy = _make_strategy(artifact_dir="/nonexistent/pilot/artifacts")
    tier, confidence, source, extra = await strategy.classify(
        "anything at all", ["c0", "c1", "c2", "c3"]
    )
    assert tier == DEFAULT_TEXT_TIER
    assert confidence == 0.0
    assert source == "pilot_unavailable"
    # Degraded extra still carries the consumer-read shape.
    assert extra["route_class"]
    assert extra["thinking_mode"]
    assert extra["prompt_policy"]
    assert not any(key.startswith("final_") for key in extra)


@pytest.mark.asyncio
async def test_degrade_honours_valid_tiers() -> None:
    strategy = _make_strategy(artifact_dir="/nonexistent/pilot/artifacts")
    tier, _confidence, source, _extra = await strategy.classify(
        "anything", ["c2", "c3"]
    )
    assert source == "pilot_unavailable"
    # DEFAULT_TEXT_TIER (c1) is excluded → walk upward to c2.
    assert tier == "c2"


@pytest.mark.asyncio
async def test_never_raises_at_classify_by_default() -> None:
    strategy = _make_strategy(artifact_dir="/nonexistent/pilot/artifacts")
    # Must not raise even with empty valid_tiers.
    tier, confidence, source, _extra = await strategy.classify("x", [])
    assert source == "pilot_unavailable"
    assert confidence == 0.0
    assert tier == DEFAULT_TEXT_TIER


def test_require_router_runtime_raises_on_load_failure() -> None:
    with pytest.raises(RuntimeError, match="[Pp]ilot"):
        PilotStrategy(
            artifact_dir="/nonexistent/pilot/artifacts",
            encoder=_StubEncoder(),
            require_router_runtime=True,
        )


class _BoomSession:
    """ONNX-session stand-in whose ``run`` always faults."""

    def run(self, *_args: object, **_kwargs: object) -> object:
        raise RuntimeError("onnx predict boom")


@pytest.mark.asyncio
async def test_require_router_runtime_raises_on_predict_fault() -> None:
    """Mirrors v4: with the flag set, an ONNX predict-time fault must surface.

    ``PilotModel._run`` swallows predict exceptions internally (fail-soft,
    flipping ``available``), so the strategy has to consult the flag on the
    availability-flip path too — not only when ``build_features`` raises.
    """
    strategy = _make_strategy(require_router_runtime=True)
    assert strategy._model is not None
    strategy._model._session = _BoomSession()
    with pytest.raises(RuntimeError, match="[Pp]ilot"):
        await strategy.classify("anything at all", ["c0", "c1", "c2", "c3"])


@pytest.mark.asyncio
async def test_predict_fault_degrades_without_flag() -> None:
    """Without the flag the same predict fault degrades silently (fail-soft)."""
    strategy = _make_strategy()
    assert strategy._model is not None
    strategy._model._session = _BoomSession()
    tier, confidence, source, _extra = await strategy.classify(
        "anything at all", ["c0", "c1", "c2", "c3"]
    )
    assert source == "pilot_unavailable"
    assert confidence == 0.0
    assert tier == DEFAULT_TEXT_TIER
