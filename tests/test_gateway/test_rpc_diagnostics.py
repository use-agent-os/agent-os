from __future__ import annotations

import pytest

from agentos.gateway.access import CONTROL_ONLY, ConnectionSurface
from agentos.gateway.auth import AccessContext
from agentos.gateway.config import GatewayConfig
from agentos.gateway.diagnostics import DiagnosticsState
from agentos.gateway.rpc import RpcContext, get_dispatcher


@pytest.mark.asyncio
async def test_diagnostics_status_is_read_scoped_and_reports_standard_default(
    monkeypatch,
) -> None:
    monkeypatch.delenv("AGENTOS_TURN_CALL_LOG", raising=False)
    state = DiagnosticsState.from_config(GatewayConfig(diagnostics_enabled=True))
    ctx = RpcContext(
        conn_id="test",
        config=GatewayConfig(diagnostics_enabled=True),
        diagnostics_state=state,
    )

    entry = get_dispatcher().get_entry("diagnostics.status")
    assert entry is not None
    assert entry.audiences == CONTROL_ONLY
    response = await get_dispatcher().dispatch("req-1", "diagnostics.status", {}, ctx)

    assert response.ok is True
    assert response.payload["enabled"] is True
    assert response.payload["detail"] == "standard"
    assert response.payload["raw_turn_call"]["enabled"] is False
    assert response.payload["raw_turn_call"]["source"] == "off"


@pytest.mark.asyncio
async def test_diagnostics_set_requires_admin_and_enables_runtime_raw(monkeypatch) -> None:
    monkeypatch.delenv("AGENTOS_TURN_CALL_LOG", raising=False)
    state = DiagnosticsState.from_config(GatewayConfig())
    ctx = RpcContext(conn_id="test", config=GatewayConfig(), diagnostics_state=state)

    entry = get_dispatcher().get_entry("diagnostics.set")
    assert entry is not None
    assert entry.audiences == CONTROL_ONLY
    response = await get_dispatcher().dispatch(
        "req-1",
        "diagnostics.set",
        {"enabled": True, "raw": True},
        ctx,
    )

    assert response.ok is True
    assert response.payload["enabled"] is True
    assert response.payload["detail"] == "raw"
    assert response.payload["raw_turn_call"] == {
        "enabled": True,
        "source": "runtime",
        "env_override": False,
    }
    assert response.payload["applies_to"] == "next_turn"


@pytest.mark.asyncio
async def test_diagnostics_methods_require_an_admitted_control_connection(
    monkeypatch,
) -> None:
    monkeypatch.delenv("AGENTOS_TURN_CALL_LOG", raising=False)
    ctx = RpcContext(
        conn_id="test",
        access=AccessContext(
            surface=ConnectionSurface.CONTROL,
            admitted=False,
            credential_verified=False,
        ),
        config=GatewayConfig(),
        diagnostics_state=DiagnosticsState.from_config(GatewayConfig()),
    )

    status = await get_dispatcher().dispatch("req-1", "diagnostics.status", {}, ctx)
    setter = await get_dispatcher().dispatch(
        "req-2",
        "diagnostics.set",
        {"enabled": True, "raw": True},
        ctx,
    )

    assert status.ok is False
    assert setter.ok is False
    assert status.error is not None
    assert status.error.code == "UNAUTHORIZED"
    assert setter.error is not None
    assert setter.error.code == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_diagnostics_set_attaches_fallback_state_to_context(monkeypatch) -> None:
    monkeypatch.delenv("AGENTOS_TURN_CALL_LOG", raising=False)
    ctx = RpcContext(conn_id="test", config=GatewayConfig())

    setter = await get_dispatcher().dispatch(
        "req-1",
        "diagnostics.set",
        {"enabled": True, "raw": True},
        ctx,
    )
    status = await get_dispatcher().dispatch("req-2", "diagnostics.status", {}, ctx)

    assert setter.ok is True
    assert status.ok is True
    assert status.payload["detail"] == "raw"
    assert status.payload["raw_turn_call"]["source"] == "runtime"


@pytest.mark.asyncio
async def test_diagnostics_off_clears_runtime_state_but_reports_env_forced_raw(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AGENTOS_TURN_CALL_LOG", "1")
    state = DiagnosticsState.from_config(GatewayConfig())
    state.set_runtime(enabled=True, raw=True)
    ctx = RpcContext(conn_id="test", config=GatewayConfig(), diagnostics_state=state)

    response = await get_dispatcher().dispatch(
        "req-1",
        "diagnostics.set",
        {"enabled": False},
        ctx,
    )

    assert response.ok is True
    assert response.payload["enabled"] is False
    assert response.payload["detail"] == "off"
    assert response.payload["runtime"]["enabled"] is False
    assert response.payload["runtime"]["raw"] is False
    assert response.payload["raw_turn_call"] == {
        "enabled": True,
        "source": "env",
        "env_override": True,
    }
    assert response.payload["warning"] == "AGENTOS_TURN_CALL_LOG still forces raw capture"

    second = await get_dispatcher().dispatch(
        "req-2",
        "diagnostics.set",
        {"enabled": False},
        ctx,
    )
    assert second.ok is True
    assert second.payload["runtime"]["raw"] is False
    assert second.payload["raw_turn_call"]["source"] == "env"


def test_doctor_status_is_control_only() -> None:
    entry = get_dispatcher().get_entry("doctor.status")
    assert entry is not None
    assert entry.audiences == CONTROL_ONLY


def test_diagnostics_state_reset_runtime() -> None:
    state = DiagnosticsState(configured_enabled=True)
    assert state.snapshot().effective_enabled is True
    assert state.snapshot().runtime_enabled is None

    state.set_runtime(enabled=False)
    assert state.snapshot().effective_enabled is False
    assert state.snapshot().runtime_enabled is False

    snap = state.reset_runtime()
    assert snap.effective_enabled is True
    assert snap.runtime_enabled is None
    assert snap.runtime_raw is False


@pytest.mark.asyncio
async def test_diagnostics_set_reset_reverts_to_configured(monkeypatch) -> None:
    monkeypatch.delenv("AGENTOS_TURN_CALL_LOG", raising=False)
    state = DiagnosticsState.from_config(GatewayConfig(diagnostics_enabled=True))
    state.set_runtime(enabled=False)
    assert state.snapshot().effective_enabled is False

    ctx = RpcContext(
        conn_id="test",
        config=GatewayConfig(diagnostics_enabled=True),
        diagnostics_state=state,
    )

    response = await get_dispatcher().dispatch(
        "req-reset",
        "diagnostics.set",
        {"reset": True},
        ctx,
    )
    assert response.ok is True
    assert response.payload["enabled"] is True
    assert response.payload["runtime"]["enabled"] is None

