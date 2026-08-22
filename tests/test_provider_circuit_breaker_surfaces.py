"""Breaker state is visible to operators: doctor, providers.status, /api/system/status."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient

from agentos.cli.providers_cmd import _circuit_cell
from agentos.gateway.app import create_gateway_app
from agentos.gateway.config import AuthConfig, GatewayConfig, LlmProviderConfig
from agentos.gateway.rpc_tools import _circuit_breaker_row
from agentos.health.evaluator import evaluate_provider
from agentos.provider.circuit_breaker import (
    BreakerSettings,
    ProviderCircuitBreaker,
)
from agentos.provider.failures import ProviderFailureKind
from agentos.provider.selector import ModelSelector, ProviderConfig, SelectorConfig


def _selector(threshold: int = 1) -> ModelSelector:
    return ModelSelector(
        SelectorConfig(primary=ProviderConfig("openrouter", "m", api_key="k")),
        breaker=ProviderCircuitBreaker(
            BreakerSettings(failure_threshold=threshold, cooldown_seconds=60.0)
        ),
    )


def _provider_row(breaker: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "activeProvider": "openrouter",
        "providers": [
            {
                "providerId": "openrouter",
                "active": True,
                "configured": True,
                "buildable": True,
                "model": "m",
                "circuitBreaker": breaker,
            }
        ],
    }


# ── doctor / health evaluator ────────────────────────────────────────


def test_closed_breaker_adds_no_doctor_finding() -> None:
    findings = evaluate_provider(_provider_row({"state": "closed"}))
    assert [f.id for f in findings] == ["provider.active.ready"]


def test_missing_breaker_field_adds_no_doctor_finding() -> None:
    """Older gateways (or a selector without a breaker) must not break doctor."""
    findings = evaluate_provider(_provider_row(None))
    assert [f.id for f in findings] == ["provider.active.ready"]


def test_open_breaker_degrades_readiness_with_cooldown_detail() -> None:
    findings = evaluate_provider(
        _provider_row(
            {
                "state": "open",
                "consecutiveFailures": 3,
                "failureThreshold": 3,
                "cooldownRemainingSeconds": 42.4,
                "lastFailureKind": "provider_overloaded",
                "lastFailureReason": "upstream 503",
            }
        )
    )
    ids = [f.id for f in findings]
    assert "provider.circuit.open" in ids

    finding = next(f for f in findings if f.id == "provider.circuit.open")
    assert finding.severity == "warn"
    assert finding.readiness_impact == "degrades"
    assert "42s" in finding.detail
    assert finding.evidence["lastFailureKind"] == "provider_overloaded"
    # Recoverable on its own — never tell the operator to restart for this.
    assert finding.restart_required is False


def test_half_open_breaker_is_informational_only() -> None:
    findings = evaluate_provider(
        _provider_row({"state": "half_open", "consecutiveFailures": 3, "failureThreshold": 3})
    )
    finding = next(f for f in findings if f.id == "provider.circuit.half_open")
    assert finding.severity == "info"
    assert finding.readiness_impact == "optional"


# ── providers.status row ─────────────────────────────────────────────


class _Ctx:
    def __init__(self, provider_selector: Any) -> None:
        self.provider_selector = provider_selector


def test_providers_status_row_reports_breaker_state() -> None:
    selector = _selector()
    selector.record_provider_failure(ProviderFailureKind.PROVIDER_OVERLOADED, "upstream 503")

    row = _circuit_breaker_row("openrouter", _Ctx(selector))
    assert row is not None
    assert row["state"] == "open"
    assert row["consecutiveFailures"] == 1


def test_providers_status_row_is_none_without_a_selector() -> None:
    assert _circuit_breaker_row("openrouter", _Ctx(None)) is None


def test_providers_status_row_is_side_effect_free() -> None:
    """Rendering status must not consume the half-open probe slot."""
    selector = _selector()
    selector.record_provider_failure(ProviderFailureKind.PROVIDER_OVERLOADED, "503")

    for _ in range(3):
        _circuit_breaker_row("openrouter", _Ctx(selector))

    assert selector.circuit_breaker.state("openrouter") == "open"


@pytest.mark.parametrize(
    ("breaker", "expected"),
    [
        (None, "-"),
        ({"state": "closed"}, "closed"),
        ({"state": "half_open"}, "half_open"),
        ({"state": "open", "cooldownRemainingSeconds": 41.6}, "open (42s)"),
        ({"state": "open", "cooldownRemainingSeconds": 0}, "open"),
    ],
)
def test_cli_circuit_cell(breaker: dict[str, Any] | None, expected: str) -> None:
    assert _circuit_cell(breaker) == expected


# ── /api/system/status ───────────────────────────────────────────────


def _client(selector: Any) -> TestClient:
    config = GatewayConfig(
        host="127.0.0.1",
        auth=AuthConfig(mode="none"),
        llm=LlmProviderConfig(provider="openrouter", model="m"),
    )
    return TestClient(
        create_gateway_app(config, provider_selector=selector),
        base_url="http://127.0.0.1",
    )


def test_system_status_exposes_breaker_state() -> None:
    selector = _selector()
    selector.record_provider_failure(ProviderFailureKind.RATE_LIMITED, "429 slow down")

    payload = _client(selector).get("/api/system/status").json()

    assert payload["provider"] == "openrouter"
    assert payload["circuitBreaker"]["state"] == "open"
    assert payload["circuitBreaker"]["provider"] == "openrouter"
    assert [row["provider"] for row in payload["circuitBreakers"]] == ["openrouter"]


def test_system_status_is_quiet_while_every_provider_is_healthy() -> None:
    payload = _client(_selector()).get("/api/system/status").json()

    assert payload["circuitBreaker"] is None
    assert payload["circuitBreakers"] == []


def test_system_status_without_a_selector() -> None:
    payload = _client(None).get("/api/system/status").json()

    assert payload["circuitBreaker"] is None
    assert payload["circuitBreakers"] == []
