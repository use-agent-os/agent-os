"""Pilot router strategy — assembles T1/T2/T3 into a ``RouterStrategy`` (T4).

``PilotStrategy`` implements the same ``classify`` contract as
``V4Phase3Strategy`` / ``LLMJudgeStrategy`` (Pilot router spec, Rev 4, §4):

    build_features (T1)  →  PilotModel.predict_proba (T2, calibrated)
        →  apply_safety_net (T3)  →  route class + confidence

**History handling.** The strategy declares ``requires_history = True`` so the
engine (``engine/steps/agentos_router.py::_finalize_decision``) runs its
deterministic guards (confidence gate, complaint upgrade, KV-cache
anti-downgrade) and accumulates per-session routing history. But the classifier
input is **current-turn-only**: ``routing_history`` and the history/context
kwargs (``prev_assistant_text``, ``history_user_texts``, …) are accepted and
deliberately ignored when building features, so identical current-turn text
yields identical raw probabilities regardless of session history. All history
policy is the engine's; the safety net (T3) is Pilot's only probability-space
adjustment — no sticky tier, no history-based policy, no other adjustment.

**Fail-soft.** Mirrors ``V4Phase3Strategy``: a missing artifact directory, a
missing/corrupt file, or any exception at load or predict time degrades to
``DEFAULT_TEXT_TIER`` (via the shared ``_find_valid_tier``) with confidence
``0.0``, source ``"pilot_unavailable"``, and a degraded ``extra`` dict — never
raising at classify time. The ``require_router_runtime`` escape hatch turns a
construction-time load failure — or any classify-time fault, including a
predict fault that ``PilotModel`` absorbed fail-soft — into a ``RuntimeError``
instead of degrading, matching how v4 honours the same flag.

**Encoder adapter.** T1's ``build_features`` needs a ``PilotEncoder`` (both
``encode_sync`` and ``count_tokens_pretrunc``). A bare ``LocalEmbeddingProvider``
supplies only ``encode_sync``; this module owns ``_MiniLMEncoder``, the adapter
that pairs the provider with a pinned MiniLM tokenizer (truncation disabled) to
add the pre-truncation token count.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import structlog

from agentos.agentos_router.controller import (
    compute_difficulty,
    compute_margin,
    derive_prompt_policy,
    derive_thinking_mode,
    normalize_decisions,
)
from agentos.agentos_router.pilot.features import (
    MINILM_MODEL_ID,
    PilotEncoder,
    build_features,
)
from agentos.agentos_router.pilot.model import PILOT_CLASSES, PilotModel
from agentos.agentos_router.pilot.postprocess import apply_safety_net
from agentos.agentos_router.tiers_util import _find_valid_tier
from agentos.router_tiers import DEFAULT_TEXT_TIER, ROUTE_CLASS_TO_TIER

log = structlog.get_logger(__name__)

_ROUTE_CLASS_TO_TIER: dict[str, str] = dict(ROUTE_CLASS_TO_TIER)

#: Healthy / degraded source tags surfaced as ``routing_source`` (spec §4.5).
SOURCE_HEALTHY = "pilot_v1"
SOURCE_UNAVAILABLE = "pilot_unavailable"


def default_artifact_dir() -> Path:
    """Repository default for the production Pilot artifact directory.

    The production bundle (``models/pilot_v1/``) does not exist yet; the
    strategy degrades cleanly when it is absent, exactly as v4 does for its own
    missing bundle.
    """
    return Path(__file__).resolve().parent.parent / "models" / "pilot_v1"


class _MiniLMEncoder:
    """``PilotEncoder`` adapter around ``LocalEmbeddingProvider``.

    ``encode_sync`` delegates to the provider (raw, un-normalised vectors — the
    feature builder owns L2). ``count_tokens_pretrunc`` uses a second copy of the
    pinned MiniLM tokenizer with truncation and padding disabled, counting with
    ``add_special_tokens=False`` (spec §4.4), so it reports the true
    pre-truncation token count that ``log1p_token_count_pretrunc_8k`` demands
    (the provider's own tokenizer has 256-token truncation enabled).

    Both the provider weights and the counting tokenizer load lazily on first
    use; construction is side-effect-free.
    """

    def __init__(self, model_id: str = MINILM_MODEL_ID) -> None:
        self._model_id = model_id
        self._provider: Any | None = None
        self._counter: Any | None = None

    def _ensure_provider(self) -> Any:
        if self._provider is None:
            from agentos.memory.embedding import LocalEmbeddingProvider

            self._provider = LocalEmbeddingProvider(self._model_id)
        return self._provider

    def _ensure_counter(self) -> Any:
        if self._counter is None:
            from tokenizers import Tokenizer

            from agentos.memory.embedding import LocalEmbeddingProvider

            onnx_dir = LocalEmbeddingProvider.resolve_onnx_dir(self._model_id)
            if onnx_dir is None:
                raise RuntimeError(
                    f"could not resolve MiniLM ONNX dir for {self._model_id!r}"
                )
            tokenizer = Tokenizer.from_file(str(Path(onnx_dir) / "tokenizer.json"))
            # Pre-truncation count: no truncation, no padding.
            tokenizer.no_truncation()
            tokenizer.no_padding()
            self._counter = tokenizer
        return self._counter

    def encode_sync(self, texts: list[str]) -> np.ndarray:
        raw = self._ensure_provider().encode_sync(list(texts))
        return np.asarray(raw, dtype=np.float32)

    def count_tokens_pretrunc(self, text: str) -> int:
        # add_special_tokens=False per the spec-pinned count contract (§4.4):
        # [CLS]/[SEP] must not inflate log1p_token_count_pretrunc_8k.
        return len(self._ensure_counter().encode(text, add_special_tokens=False).ids)


class PilotStrategy:
    """History-aware Pilot router strategy (fixture- or production-backed)."""

    requires_history = True
    source = SOURCE_HEALTHY

    def __init__(
        self,
        artifact_dir: str | Path | None = None,
        *,
        encoder: PilotEncoder | None = None,
        safety_net_threshold: float = 0.5,
        confidence_threshold: float = 0.5,
        require_router_runtime: bool = False,
    ) -> None:
        # expanduser: a configured "~/..." dir must resolve the same way the
        # boot/doctor asset probe resolves it (router_strategies.py), or
        # preflight reports ready while every turn degrades.
        self.artifact_dir = (
            Path(artifact_dir).expanduser() if artifact_dir else default_artifact_dir()
        )
        self._safety_net_threshold = safety_net_threshold
        self._confidence_threshold = confidence_threshold
        self._require_router_runtime = require_router_runtime
        self._encoder: PilotEncoder = encoder if encoder is not None else _MiniLMEncoder()
        self._model: PilotModel | None = None
        self._model_version = "unknown"
        self._available = False

        try:
            self._init_model()
        except Exception as exc:  # noqa: BLE001 - fail-soft by contract
            log.warning(
                "pilot.init_failed",
                artifact_dir=str(self.artifact_dir),
                error=str(exc),
            )
            if require_router_runtime:
                raise RuntimeError(f"failed to initialize Pilot router: {exc}") from exc

    def _init_model(self) -> None:
        model = PilotModel(self.artifact_dir)
        if not model.available:
            raise RuntimeError(
                f"pilot model unavailable: {model.unavailable_reason}"
            )
        self._model = model
        self._model_version = str(model.manifest.get("pilot_version", "unknown"))
        self._available = True

    async def classify(
        self,
        message: str,
        valid_tiers: list[str],
        routing_history: list[dict] | None = None,
        prev_assistant_text: str | None = None,
        prev_assistant_usage: dict | None = None,
        history_user_texts: list[str] | None = None,
        flags_text_override: str | None = None,
        **_ignored: object,
    ) -> tuple[str, float, str, dict]:
        """Classify the CURRENT turn into AgentOS tier format.

        The history/context kwargs are accepted for protocol compatibility with
        ``V4Phase3Strategy`` / ``LLMJudgeStrategy`` but are deliberately unused:
        Pilot classifies the current turn only. History policy is the engine's.
        """
        if not self._available or self._model is None:
            return self._unavailable_classify(valid_tiers)

        try:
            # Pass the full message: build_features bounds every tokenizer/embed
            # input to MAX_INPUT_CHARS itself, while char_count_full must reflect
            # the untruncated length (T1 contract). Pre-slicing here would corrupt
            # log1p_char_count_full for any message over the bound.
            features = build_features(message, encoder=self._encoder)
            probs = self._model.predict_proba(features.reshape(1, -1))
            if not self._model.available:
                # A predict-time fault flipped the model unavailable (fail-soft).
                # PilotModel swallows the exception internally, so honour the
                # escape hatch here too — mirroring v4, where a predict fault
                # propagates and classify re-raises under the flag.
                if self._require_router_runtime:
                    raise RuntimeError(
                        f"pilot predict failed: {self._model.unavailable_reason}"
                    )
                return self._unavailable_classify(valid_tiers)
            return self._map_result(probs[0], valid_tiers)
        except Exception as exc:  # noqa: BLE001 - fail-soft by contract
            log.warning("pilot.predict_failed", error=str(exc), exc_info=True)
            if self._require_router_runtime:
                raise
            return self._unavailable_classify(valid_tiers)

    def _map_result(
        self,
        probs: np.ndarray,
        valid_tiers: list[str],
    ) -> tuple[str, float, str, dict]:
        prob_list = [float(p) for p in probs]
        result = apply_safety_net(
            prob_list,
            safety_net_threshold=self._safety_net_threshold,
            confidence_threshold=self._confidence_threshold,
        )
        route_class = result.route_class
        tier = _ROUTE_CLASS_TO_TIER.get(route_class, DEFAULT_TEXT_TIER)
        if tier not in valid_tiers:
            tier = _find_valid_tier(tier, valid_tiers)

        # flags is an empty list by contract (§4.2); the controller helpers treat
        # a non-mapping/empty flags value as "no flags set".
        flags: list[str] = []
        thinking_mode = derive_thinking_mode(prob_list, None)
        prompt_policy = derive_prompt_policy(prob_list, None)
        thinking_mode, prompt_policy = normalize_decisions(thinking_mode, prompt_policy)

        probabilities = {cls: float(p) for cls, p in zip(PILOT_CLASSES, prob_list, strict=True)}
        extra: dict[str, Any] = {
            "route_class": route_class,
            "top1_label": route_class,
            "probabilities": probabilities,
            "difficulty": compute_difficulty(prob_list),
            "margin": compute_margin(prob_list),
            "thinking_mode": thinking_mode,
            "prompt_policy": prompt_policy,
            "flags": flags,
            "model_version": self._model_version,
            "safety_net_applied": result.safety_net_applied,
        }
        return tier, result.confidence, self.source, extra

    def _unavailable_classify(
        self,
        valid_tiers: list[str],
    ) -> tuple[str, float, str, dict]:
        tier = _find_valid_tier(DEFAULT_TEXT_TIER, valid_tiers)
        route_class = next(
            (key for key, value in _ROUTE_CLASS_TO_TIER.items() if value == tier),
            "R1",
        )
        extra: dict[str, Any] = {
            "route_class": route_class,
            "top1_label": route_class,
            "probabilities": None,
            "difficulty": None,
            "margin": None,
            "thinking_mode": "T1",
            "prompt_policy": "P1",
            "flags": [],
            "model_version": self._model_version,
            "safety_net_applied": False,
        }
        return tier, 0.0, SOURCE_UNAVAILABLE, extra
