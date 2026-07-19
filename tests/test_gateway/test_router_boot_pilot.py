"""Boot-preflight surface for the ``pilot-v1`` strategy (registry-driven).

``validate_agentos_router_runtime`` runs the registry ``asset_probe`` for a
strategy that ``requires_local_assets``. Missing Pilot artifacts warn (and
degrade at runtime) unless ``require_router_runtime`` is set, exactly as v4 does.
Pointing at the committed fixture bundle passes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentos.gateway.boot import validate_agentos_router_runtime
from agentos.gateway.config import GatewayConfig

FIXTURE_DIR = (
    Path(__file__).parent.parent
    / "test_agentos_router"
    / "data"
    / "pilot_fixture"
)


def test_pilot_boot_warns_on_missing_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    warnings: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "agentos.gateway.boot.log.warning",
        lambda event, **kwargs: warnings.append({"event": event, **kwargs}),
    )
    config = GatewayConfig()
    config.agentos_router.strategy = "pilot-v1"
    config.agentos_router.pilot.pilot_artifact_dir = str(tmp_path / "absent")

    validate_agentos_router_runtime(config)

    missing_events = [
        w for w in warnings if w["event"] == "build_services.agentos_router_bundle_missing"
    ]
    assert missing_events
    assert missing_events[0]["strategy"] == "pilot-v1"
    assert any("model.onnx" in m for m in missing_events[0]["missing"])


def test_pilot_boot_raises_when_required(tmp_path: Path) -> None:
    config = GatewayConfig()
    config.agentos_router.strategy = "pilot-v1"
    config.agentos_router.pilot.pilot_artifact_dir = str(tmp_path / "absent")
    config.agentos_router.require_router_runtime = True

    with pytest.raises(RuntimeError, match="pilot-v1 router assets"):
        validate_agentos_router_runtime(config)


def test_pilot_boot_passes_with_fixture_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    infos: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "agentos.gateway.boot.log.info",
        lambda event, **kwargs: infos.append({"event": event, **kwargs}),
    )
    from agentos.memory.embedding import LocalEmbeddingProvider

    if LocalEmbeddingProvider.resolve_onnx_dir(
        "sentence-transformers/all-MiniLM-L6-v2"
    ) is None:
        pytest.skip("MiniLM embedder dir not present in this checkout")

    config = GatewayConfig()
    config.agentos_router.strategy = "pilot-v1"
    config.agentos_router.pilot.pilot_artifact_dir = str(FIXTURE_DIR)
    # Fixture bundle + bundled MiniLM present → strict mode must not raise.
    config.agentos_router.require_router_runtime = True

    validate_agentos_router_runtime(config)

    ready = [i for i in infos if i["event"] == "build_services.agentos_router_bundle_ready"]
    assert ready
    assert ready[0]["strategy"] == "pilot-v1"
