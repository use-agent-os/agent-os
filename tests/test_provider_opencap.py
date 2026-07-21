"""OpenCAP gateway provider contracts."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentos.gateway.config import (
    ROUTER_TIER_PROFILE_IDS,
    GatewayConfig,
    _router_tier_profile_defaults,
)
from agentos.onboarding.provider_specs import get_provider_setup_spec
from agentos.provider.failures import ProviderFailureKind, classify_provider_error
from agentos.provider.model_catalog import ModelCatalog
from agentos.provider.openai import OpenAIProvider
from agentos.provider.registry import ProviderSpec, get_provider_spec, list_provider_names
from agentos.provider.selector import ProviderConfig, _build_provider


def test_opencap_gateway_is_registered() -> None:
    assert "opencap" in list_provider_names()


def test_opencap_gateway_spec_contract() -> None:
    spec = get_provider_spec("opencap")

    assert spec.backend == "openai_compat"
    assert spec.provider_kind == "opencap"
    assert spec.env_key == "OPENCAP_API_KEY"
    assert spec.default_base_url == "https://gw.capminal.ai/api/inference/v1"
    assert spec.model_catalog_url == "https://gw.capminal.ai/api/public/models"
    assert spec.runtime_supported is True
    assert spec.requires_api_key() is True
    assert spec.requires_base_url() is False
    assert spec.support_level == "compat_mock_verified"


def test_model_catalog_url_preserves_provider_spec_positional_contract() -> None:
    spec = ProviderSpec(
        "legacy-compatible",
        "openai_compat",
        "legacy-compatible",
        "LEGACY_API_KEY",
        "https://legacy.example/v1",
        "compat_configured",
    )

    assert spec.support_level == "compat_configured"
    assert spec.model_catalog_url == ""


def test_opencap_gateway_onboarding_spec() -> None:
    spec = get_provider_setup_spec("opencap")

    assert spec.label == "OpenCAP"
    assert spec.runtime_supported is True
    assert spec.deployment == "cloud"
    assert spec.requires_api_key is True
    assert spec.default_base_url == "https://gw.capminal.ai/api/inference/v1"
    assert spec.default_direct_model == "oc-uncensored-1.0"
    model_field = next(field for field in spec.fields if field.name == "model")
    assert model_field.default == "oc-uncensored-1.0"
    assert any(field.name == "api_key" and field.required for field in spec.fields)


def test_opencap_builds_shared_openai_compatible_transport() -> None:
    provider = _build_provider(
        ProviderConfig(provider="opencap", model="minimax-m3", api_key="test-key")
    )

    assert isinstance(provider, OpenAIProvider)
    metadata = provider.provider_metadata()
    assert metadata.provider_kind == "opencap"
    assert metadata.model == "minimax-m3"
    assert metadata.base_url == "https://gw.capminal.ai/api/inference/v1"


def test_opencap_uses_openai_compatible_failure_classification() -> None:
    assert (
        classify_provider_error("opencap", 400, "unsupported_provider", "unsupported")
        is ProviderFailureKind.UNSUPPORTED_FEATURE
    )


def test_opencap_router_profile_contract() -> None:
    assert "opencap" in ROUTER_TIER_PROFILE_IDS
    tiers = _router_tier_profile_defaults("opencap")

    assert {tier["provider"] for tier in tiers.values()} == {"opencap"}
    assert tiers["c0"]["model"] == "oc-uncensored-1.0"
    assert tiers["c1"]["model"] == "minimax-m3"
    assert tiers["c2"]["model"] == "glm-5.2"
    assert tiers["c3"]["model"] == "claude-opus-4.8"
    assert tiers["image_model"]["model"] == "minimax-m3"
    assert tiers["image_model"]["supports_image"] is True
    assert tiers["image_model"]["image_only"] is True


def test_opencap_direct_provider_auto_selects_router_profile() -> None:
    cfg = GatewayConfig(llm={"provider": "opencap", "model": "minimax-m3"})

    assert cfg.agentos_router.tier_profile == "opencap"
    assert cfg.agentos_router.tiers == _router_tier_profile_defaults("opencap")


def test_populate_from_opencap_parses_gateway_catalog() -> None:
    catalog = ModelCatalog()
    catalog._populate_from_opencap(
        [
            {
                "id": "minimax-m3",
                "name": "MiniMax M3",
                "contextLength": 1_048_576,
                "maxOutput": 131_072,
                "modality": {"input": ["text", "image"], "output": ["text"]},
            },
            {
                "id": "oc-uncensored-1.0",
                "name": "OpenCAP Uncensored 1.0",
                "context_length": 262_144,
                "max_output": None,
                "modality": {"input": ["text"], "output": ["text"]},
            },
        ]
    )

    minimax = catalog.get("minimax-m3")
    assert minimax is not None
    assert minimax.provider == "opencap"
    assert minimax.context_window == 1_048_576
    assert minimax.max_output_tokens == 131_072
    assert minimax.supports_vision is True
    assert minimax.supports_tools is True

    uncensored = catalog.get("oc-uncensored-1.0")
    assert uncensored is not None
    assert uncensored.provider == "opencap"
    assert uncensored.max_output_tokens == 0
    assert catalog.get_capabilities("oc-uncensored-1.0", "opencap").supports_vision is False


@pytest.mark.asyncio
async def test_fetch_opencap_uses_public_unauthenticated_catalog() -> None:
    captured: dict[str, object] = {}
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"data": [{"id": "glm-5.2", "name": "GLM-5.2"}]}

    with patch("agentos.provider.model_catalog.httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        async def capture_get(url, *, headers):
            captured["url"] = url
            captured["headers"] = headers
            return response

        client.get = AsyncMock(side_effect=capture_get)
        client_cls.return_value = client

        catalog = ModelCatalog()
        payload = await catalog.fetch_opencap()

    assert captured["url"] == "https://gw.capminal.ai/api/public/models"
    assert captured["headers"] == {"Accept": "application/json"}
    assert payload == {"data": [{"id": "glm-5.2", "name": "GLM-5.2"}]}
    model = catalog.get("glm-5.2")
    assert model is not None
    assert model.provider == "opencap"


def test_opencap_gateway_uses_conservative_shared_id_limits() -> None:
    catalog = ModelCatalog()

    assert catalog.resolve_max_tokens("deepseek-v4-flash", provider_name="opencap") == 128_000
    assert catalog.resolve_context_window("deepseek-v4-flash", provider_name="opencap") == 1_000_000
