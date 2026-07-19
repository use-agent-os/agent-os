"""Canonical router-strategy registry — the single source of truth for the
strategy id → integration-surface behavior mapping.

The strategy name was historically branched across eight integration surfaces
(dispatch, cache key, boot preflight, doctor payload, onboarding mutations, the
CLI selector, and the ``RoutingSource`` telemetry literal). Adding a bare
string like ``pilot-v1`` to that scatter silently mis-handles it — e.g.
``gateway/boot.py`` treated every non-``v4_phase3`` strategy as the LLM judge.

This module replaces the scatter with a small typed registry. Every surface
consults :func:`get_strategy_info` (or the derived helpers) instead of comparing
against literal strategy ids, so a new strategy is wired in exactly one place.

The registry lives beside :mod:`agentos.router_tiers` (which owns
``DEFAULT_ROUTER_STRATEGY``) rather than inside it: the asset probes need lazy
imports of the embedding / bundle plumbing, which has no business being an
import-time dependency of the tier-id helpers.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agentos.router_tiers import DEFAULT_ROUTER_STRATEGY

__all__ = [
    "RouterStrategyInfo",
    "get_strategy_info",
    "resolve_strategy_id",
    "is_known_strategy",
    "known_strategy_ids",
    "pilot_asset_probe",
    "v4_asset_probe",
    "PILOT_STRATEGY_ID",
    "V4_STRATEGY_ID",
    "LLM_JUDGE_STRATEGY_ID",
]

V4_STRATEGY_ID = "v4_phase3"
LLM_JUDGE_STRATEGY_ID = "llm_judge"
PILOT_STRATEGY_ID = "pilot-v1"

#: MiniLM embedder id that the Pilot feature builder depends on. Kept as a bare
#: literal (not imported) so the registry stays import-light; the value is
#: asserted against the source of truth in the registry test.
_MINILM_MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"

#: Files the Pilot production bundle must carry alongside the MiniLM dir.
_PILOT_REQUIRED_FILES = ("model.onnx", "manifest.json")

#: Files the ``_MiniLMEncoder`` needs inside the resolved MiniLM ONNX dir:
#: ``model.onnx`` for the embedding provider and ``tokenizer.json`` for the
#: pre-truncation token counter. A partial dir (e.g. an un-smudged LFS
#: checkout missing one of these) must be reported the same as a missing dir,
#: or boot/doctor report ready while every turn degrades.
_MINILM_REQUIRED_FILES = ("model.onnx", "tokenizer.json")

#: Files the v4 bundle must carry.
_V4_REQUIRED_FILES = ("runtime_src", "router.runtime.yaml")


@dataclass(frozen=True)
class RouterStrategyInfo:
    """Immutable descriptor for one router strategy.

    Attributes:
        strategy_id: canonical id (``"v4_phase3"`` | ``"pilot-v1"`` |
            ``"llm_judge"``).
        source: healthy telemetry ``routing_source`` tag.
        degraded_source: telemetry tag emitted when the strategy degrades.
        requires_local_assets: whether boot preflight must probe on-disk assets.
        uses_judge: whether the ``judge_*`` config fields are relevant.
        asset_probe: callable returning the list of missing asset paths (empty
            when everything the strategy needs is present); ``None`` for
            strategies with no local assets.
    """

    strategy_id: str
    source: str
    degraded_source: str
    requires_local_assets: bool
    uses_judge: bool
    asset_probe: Callable[..., list[str]] | None


def _pilot_default_artifact_dir() -> Path:
    from agentos.agentos_router.pilot.strategy import default_artifact_dir

    return default_artifact_dir()


def _minilm_onnx_dir() -> Path | None:
    from agentos.memory.embedding import LocalEmbeddingProvider

    return LocalEmbeddingProvider.resolve_onnx_dir(_MINILM_MODEL_ID)


def pilot_asset_probe(config: object | None = None) -> list[str]:
    """Return the Pilot bundle files (and the MiniLM dir) that are missing.

    Checks ``models/pilot_v1/`` for ``model.onnx`` + ``manifest.json`` (honoring
    a ``pilot_artifact_dir`` config override) AND the presence of the MiniLM
    embedder directory the feature builder needs. An empty list means the Pilot
    runtime is fully present.
    """
    artifact_dir = _resolve_pilot_artifact_dir(config)
    missing = [
        str(artifact_dir / name)
        for name in _PILOT_REQUIRED_FILES
        if not (artifact_dir / name).exists()
    ]
    minilm_dir = _minilm_onnx_dir()
    if minilm_dir is None or not Path(minilm_dir).is_dir():
        missing.append(f"MiniLM embedder dir ({_MINILM_MODEL_ID})")
    else:
        # A present dir is not enough: a partial checkout (missing model.onnx or
        # an un-smudged LFS tokenizer.json) makes the encoder degrade at every
        # turn. Probe per-file so boot/doctor report the same paths the pilot
        # bundle files are reported with.
        minilm_path = Path(minilm_dir)
        missing.extend(
            str(minilm_path / name)
            for name in _MINILM_REQUIRED_FILES
            if not (minilm_path / name).exists()
        )
    return missing


def _resolve_pilot_artifact_dir(config: object | None) -> Path:
    """Resolve the Pilot artifact dir from a router config or pilot sub-config.

    Accepts either the ``AgentOSRouterConfig`` (reads ``config.pilot
    .pilot_artifact_dir``) or a bare ``PilotConfig`` / namespace exposing
    ``pilot_artifact_dir`` directly.
    """
    configured = None
    if config is not None:
        pilot_cfg = getattr(config, "pilot", None)
        if pilot_cfg is not None:
            configured = getattr(pilot_cfg, "pilot_artifact_dir", None)
        if not configured:
            configured = getattr(config, "pilot_artifact_dir", None)
    if configured:
        return Path(configured).expanduser()
    return _pilot_default_artifact_dir()


def _v4_bundle_dir(config: object | None) -> Path:
    configured = getattr(config, "v4_bundle_dir", None) if config is not None else None
    if configured:
        return Path(configured).expanduser()
    return (
        Path(__file__).resolve().parent
        / "agentos_router"
        / "models"
        / "v4.2_phase3_inference"
    )


def v4_asset_probe(config: object | None = None) -> list[str]:
    """Return the v4 bundle files that are missing (empty when present)."""
    bundle_dir = _v4_bundle_dir(config)
    return [
        str(bundle_dir / name)
        for name in _V4_REQUIRED_FILES
        if not (bundle_dir / name).exists()
    ]


_REGISTRY: dict[str, RouterStrategyInfo] = {
    V4_STRATEGY_ID: RouterStrategyInfo(
        strategy_id=V4_STRATEGY_ID,
        source="v4_phase3",
        degraded_source="v4_unavailable",
        requires_local_assets=True,
        uses_judge=False,
        asset_probe=v4_asset_probe,
    ),
    LLM_JUDGE_STRATEGY_ID: RouterStrategyInfo(
        strategy_id=LLM_JUDGE_STRATEGY_ID,
        source="llm_judge",
        degraded_source="judge_unavailable",
        requires_local_assets=False,
        uses_judge=True,
        asset_probe=None,
    ),
    PILOT_STRATEGY_ID: RouterStrategyInfo(
        strategy_id=PILOT_STRATEGY_ID,
        source="pilot_v1",
        degraded_source="pilot_unavailable",
        requires_local_assets=True,
        uses_judge=False,
        asset_probe=pilot_asset_probe,
    ),
}


def known_strategy_ids() -> frozenset[str]:
    """Return the set of registered strategy ids."""
    return frozenset(_REGISTRY)


def is_known_strategy(strategy_id: object) -> bool:
    """Return whether ``strategy_id`` names a registered strategy."""
    return str(strategy_id or "").strip() in _REGISTRY


def get_strategy_info(strategy_id: object) -> RouterStrategyInfo | None:
    """Return the :class:`RouterStrategyInfo` for ``strategy_id`` or ``None``."""
    return _REGISTRY.get(str(strategy_id or "").strip())


def resolve_strategy_id(value: object) -> str:
    """Return a registered strategy id, falling back to the default.

    Mirrors the historical ``_strategy_name`` behavior: an unknown/blank id
    resolves to :data:`DEFAULT_ROUTER_STRATEGY` (the caller decides whether to
    warn).
    """
    candidate = str(value or "").strip() or DEFAULT_ROUTER_STRATEGY
    if candidate in _REGISTRY:
        return candidate
    return DEFAULT_ROUTER_STRATEGY
