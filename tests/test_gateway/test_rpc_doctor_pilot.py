"""Doctor-payload surface for the ``pilot-v1`` strategy (registry-driven).

The doctor reports Pilot runtime validity from the asset probe (via
``validate_agentos_router_runtime``) and short-circuits the judge-resolution
block, exactly as it does for the local ML v4 strategy.
"""

from __future__ import annotations

from pathlib import Path

from agentos.gateway.config import GatewayConfig
from agentos.gateway.rpc import RpcContext

FIXTURE_DIR = (
    Path(__file__).parent.parent
    / "test_agentos_router"
    / "data"
    / "pilot_fixture"
)


def test_doctor_reports_pilot_runtime_invalid_when_assets_missing_and_required(
    tmp_path: Path,
) -> None:
    """Mirrors the v4 contract: the doctor turns a *raising* runtime validation
    into runtimeValid=False with reason="assets". A raise only happens under
    require_router_runtime (else the missing bundle just warns and degrades)."""
    import agentos.gateway.rpc_doctor as rpc_doctor

    config = GatewayConfig(
        agentos_router={
            "strategy": "pilot-v1",
            "require_router_runtime": True,
            "pilot": {"pilot_artifact_dir": str(tmp_path / "absent")},
        }
    )
    ctx = RpcContext(conn_id="test", config=config)

    payload = rpc_doctor._router_payload(ctx)

    assert payload["strategy"] == "pilot-v1"
    assert payload["runtimeValid"] is False
    assert payload["runtimeInvalidReason"] == "assets"
    # A local-asset strategy never resolves a judge target.
    assert payload["judgeProvider"] is None
    assert payload["judgeModel"] is None


def test_doctor_pilot_skips_judge_resolution(tmp_path: Path) -> None:
    """Pilot short-circuits before judge resolution — all judge fields None,
    regardless of asset presence (require_router_runtime unset → only warns)."""
    import agentos.gateway.rpc_doctor as rpc_doctor

    config = GatewayConfig(
        agentos_router={
            "strategy": "pilot-v1",
            "pilot": {"pilot_artifact_dir": str(tmp_path / "absent")},
        }
    )
    ctx = RpcContext(conn_id="test", config=config)

    payload = rpc_doctor._router_payload(ctx)

    assert payload["strategy"] == "pilot-v1"
    assert payload["judgeProvider"] is None
    assert payload["judgeModel"] is None
    assert payload["judgeSource"] is None
    assert payload["judgeBaseUrl"] is None


def test_doctor_reports_pilot_runtime_valid_with_fixture_bundle() -> None:
    import agentos.gateway.rpc_doctor as rpc_doctor
    from agentos.memory.embedding import LocalEmbeddingProvider

    if LocalEmbeddingProvider.resolve_onnx_dir(
        "sentence-transformers/all-MiniLM-L6-v2"
    ) is None:
        import pytest

        pytest.skip("MiniLM embedder dir not present in this checkout")

    config = GatewayConfig(
        agentos_router={
            "strategy": "pilot-v1",
            "pilot": {"pilot_artifact_dir": str(FIXTURE_DIR)},
        }
    )
    ctx = RpcContext(conn_id="test", config=config)

    payload = rpc_doctor._router_payload(ctx)

    assert payload["strategy"] == "pilot-v1"
    assert payload["runtimeValid"] is True
