"""Surplus Intelligence marketplace provider contracts."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from agentos.gateway.config import (
    ROUTER_TIER_PROFILE_IDS,
    GatewayConfig,
    _opencap_tiers,
    _router_tier_profile_defaults,
)
from agentos.onboarding.provider_specs import get_provider_setup_spec
from agentos.provider import model_catalog as model_catalog_module
from agentos.provider.failures import ProviderFailureKind, classify_provider_error
from agentos.provider.model_catalog import ModelCatalog
from agentos.provider.openai import OpenAIProvider
from agentos.provider.registry import get_provider_spec, list_provider_names
from agentos.provider.selector import ProviderConfig, _build_provider
from agentos.provider.types import ChatConfig, Message


def test_surplus_is_registered() -> None:
    assert "surplus" in list_provider_names()


def test_surplus_spec_contract() -> None:
    spec = get_provider_spec("surplus")

    assert spec.backend == "openai_compat"
    assert spec.provider_kind == "surplus"
    assert spec.env_key == "SURPLUS_API_KEY"
    assert spec.default_base_url == "https://api.surplusintelligence.ai/v1"
    assert spec.model_catalog_url == "https://api.surplusintelligence.ai/v1/models"
    assert spec.runtime_supported is True
    assert spec.requires_api_key() is True
    assert spec.requires_base_url() is False
    assert spec.support_level == "compat_mock_verified"


def test_surplus_onboarding_spec() -> None:
    spec = get_provider_setup_spec("surplus")

    assert spec.label == "Surplus Intelligence"
    assert spec.runtime_supported is True
    assert spec.deployment == "cloud"
    assert spec.requires_api_key is True
    assert spec.default_base_url == "https://api.surplusintelligence.ai/v1"
    assert spec.default_direct_model == "gpt-5.6-luna"
    assert any(field.name == "api_key" and field.required for field in spec.fields)


def test_surplus_builds_shared_openai_compatible_transport() -> None:
    provider = _build_provider(
        ProviderConfig(provider="surplus", model="gpt-5.6-luna", api_key="inf_test")
    )

    assert isinstance(provider, OpenAIProvider)
    metadata = provider.provider_metadata()
    assert metadata.provider_kind == "surplus"
    assert metadata.model == "gpt-5.6-luna"
    assert metadata.base_url == "https://api.surplusintelligence.ai/v1"


def test_surplus_uses_openai_compatible_failure_classification() -> None:
    assert (
        classify_provider_error("surplus", 400, "unsupported_provider", "unsupported")
        is ProviderFailureKind.UNSUPPORTED_FEATURE
    )


def test_surplus_router_profile_contract() -> None:
    assert "surplus" in ROUTER_TIER_PROFILE_IDS
    tiers = _router_tier_profile_defaults("surplus")

    assert {tier["provider"] for tier in tiers.values()} == {"surplus"}
    assert tiers["c0"]["model"] == "deepseek-v4.1-flash"
    assert tiers["c1"]["model"] == "gpt-5.6-luna"
    assert tiers["c2"]["model"] == "glm-5.3"
    assert tiers["c3"]["model"] == "claude-opus-5"
    assert tiers["image_model"]["model"] == "glm-5.3-flash"
    assert tiers["image_model"]["supports_image"] is True
    assert tiers["image_model"]["image_only"] is True


def test_surplus_image_tier_diverges_from_opencap() -> None:
    """Surplus publishes minimax-m3 without vision, so the image tier cannot be
    cloned from the OpenCAP profile the way the text tiers can."""
    surplus = _router_tier_profile_defaults("surplus")
    opencap = _opencap_tiers()

    assert opencap["image_model"]["model"] == "minimax-m3"
    assert surplus["image_model"]["model"] != opencap["image_model"]["model"]


def test_surplus_tier_models_are_all_served_by_the_live_catalog() -> None:
    """Every default is a bare id the marketplace actually publishes.

    Pinned to a catalog snapshot rather than fetched, so the test stays offline;
    a default that drifts off this list is a default Surplus can no longer
    resolve to a seller.
    """
    published = {
        "deepseek-v4-flash",
        "deepseek-v4.1-flash",
        "deepseek-v4-pro",
        "gpt-5.6-luna",
        "gpt-5.6-terra",
        "gpt-5.6-sol",
        "glm-5.3",
        "glm-5.3-flash",
        "claude-opus-5",
        "claude-sonnet-5",
        "gemini-3.1-pro",
        "grok-4.6",
        "kimi-k3",
    }

    for name, tier in _router_tier_profile_defaults("surplus").items():
        assert tier["model"] in published, f"{name} pins an id Surplus no longer publishes"


def test_surplus_direct_provider_auto_selects_router_profile() -> None:
    cfg = GatewayConfig(llm={"provider": "surplus", "model": "gpt-5.6-luna"})

    assert cfg.agentos_router.tier_profile == "surplus"
    assert cfg.agentos_router.tiers == _router_tier_profile_defaults("surplus")


def test_surplus_router_profile_loads_from_disk_on_gateway_restart(tmp_path: Path) -> None:
    config_path = tmp_path / "agentos.toml"
    config_path.write_text(
        """
[llm]
provider = "surplus"
model = "gpt-5.6-luna"

[agentos_router]
enabled = true
tier_profile = "surplus"
""".strip(),
        encoding="utf-8",
    )

    cfg = GatewayConfig.load(config_path)

    assert cfg.config_path == str(config_path)
    assert cfg.llm.provider == "surplus"
    assert cfg.agentos_router.tier_profile == "surplus"
    assert cfg.agentos_router.tiers == _router_tier_profile_defaults("surplus")


def test_populate_from_surplus_parses_per_token_pricing_and_features() -> None:
    catalog = ModelCatalog()
    catalog._populate_from_surplus(
        [
            {
                "id": "glm-5.3-flash",
                "name": "GLM 5.3 Flash",
                "context_length": 1_048_576,
                "top_provider": {"max_completion_tokens": 131_072},
                "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
                "pricing": {"prompt": "0.0000000700", "completion": "0.0000002500"},
                "supported_parameters": ["temperature", "top_p"],
                "supported_features": ["streaming", "vision", "tools", "reasoning"],
            },
            {
                "id": "minimax-m3",
                "name": "MiniMax M3",
                "context_length": 200_000,
                "top_provider": {"max_completion_tokens": 32_768},
                "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
                "pricing": {"prompt": "0.0000004000", "completion": "0.0000016000"},
                "supported_features": ["streaming", "tools"],
            },
        ]
    )

    flash = catalog.get("glm-5.3-flash")
    assert flash is not None
    assert flash.provider == "surplus"
    assert flash.context_window == 1_048_576
    assert flash.max_output_tokens == 131_072
    # supported_features names vision/reasoning directly; supported_parameters
    # carries neither, so the feature array has to be read for these to be true.
    assert flash.supports_vision is True
    assert flash.supports_reasoning is True
    assert flash.supports_tools is True
    # Per-token USD -> per-1K USD.
    assert flash.input_cost_per_1k == pytest.approx(0.00007)
    assert flash.output_cost_per_1k == pytest.approx(0.00025)

    minimax = catalog.get("minimax-m3")
    assert minimax is not None
    assert minimax.supports_vision is False
    assert minimax.supports_reasoning is False
    assert minimax.supports_tools is True


def test_populate_from_surplus_defaults_missing_or_malformed_pricing_to_zero() -> None:
    catalog = ModelCatalog()
    catalog._populate_from_surplus(
        [
            {"id": "missing-price"},
            {"id": "malformed-price", "pricing": {"prompt": -1, "completion": "not-a-number"}},
            {"id": "non-finite-price", "pricing": {"prompt": "nan", "completion": "inf"}},
        ]
    )

    for model_id in ("missing-price", "malformed-price", "non-finite-price"):
        model = catalog.get(model_id)
        assert model is not None
        assert model.input_cost_per_1k == 0.0
        assert model.output_cost_per_1k == 0.0


def test_openrouter_catalog_parsing_is_unchanged_by_the_shared_parser() -> None:
    """Surplus reuses _populate_from_data; OpenRouter must keep its old shape --
    same provider id, and pricing still resolved by engine.pricing, not here."""
    catalog = ModelCatalog()
    catalog._populate_from_data(
        [
            {
                "id": "openai/gpt-5.6-luna",
                "name": "GPT 5.6 Luna",
                "context_length": 1_050_000,
                "top_provider": {"max_completion_tokens": 128_000},
                "architecture": {"input_modalities": ["text", "image"]},
                "supported_parameters": ["tools", "reasoning"],
                "pricing": {"prompt": "0.0000002", "completion": "0.0000012"},
            }
        ]
    )

    model = catalog.get("openai/gpt-5.6-luna")
    assert model is not None
    assert model.provider == "openrouter"
    assert model.supports_vision is True
    assert model.supports_reasoning is True
    assert model.input_cost_per_1k == 0.0
    assert model.output_cost_per_1k == 0.0


def test_surplus_capabilities_prefer_live_catalog_flags() -> None:
    catalog = ModelCatalog()
    catalog._populate_from_surplus(
        [
            {
                "id": "minimax-m3",
                "supported_features": ["streaming", "tools"],
                "architecture": {"input_modalities": ["text"]},
            }
        ]
    )

    caps = catalog.get_capabilities("minimax-m3", provider_name="surplus")

    # The offline prefix table would guess reasoning for a minimax-m2 id; the
    # live catalog says this one has neither reasoning nor vision, and wins.
    assert caps.supports_reasoning is False
    assert caps.supports_vision is False
    assert caps.supports_tools is True
    assert caps.reasoning_format == "none"


def test_surplus_tier_defaults_resolve_reasoning_without_a_live_catalog() -> None:
    """A boot whose catalog fetch failed still has to route the shipped tiers:
    without a reasoning format their thinking_level silently no-ops."""
    catalog = ModelCatalog()

    for model in (
        "deepseek-v4.1-flash",
        "deepseek-v4-flash",
        "gpt-5.6-luna",
        "glm-5.3",
        "claude-opus-5",
        "claude-haiku-4.5",
    ):
        caps = catalog.get_capabilities(model, provider_name="surplus")
        assert caps.supports_reasoning is True, model
        assert caps.reasoning_format == "openrouter", model
        assert caps.supports_tools is True, model

    image = catalog.get_capabilities("glm-5.3-flash", provider_name="surplus")
    assert image.supports_vision is True


def test_surplus_offline_reasoning_prefixes_cover_every_vision_prefix_claude_id() -> None:
    """`_SURPLUS_VISION_PREFIXES` lists claude-haiku-4.5 as a Surplus model, so
    the reasoning table -- consulted by the same offline fallback -- must know
    about it too, the way it already does for claude-opus-/claude-sonnet-.
    Missing here means a real extended-thinking model silently answers
    supports_reasoning=False whenever the catalog fetch has failed."""
    catalog = ModelCatalog()

    caps = catalog.get_capabilities("claude-haiku-4.5", provider_name="surplus")

    assert caps.supports_vision is True
    assert caps.supports_reasoning is True
    assert caps.reasoning_format == "openrouter"


def test_surplus_normalizes_reasoning_instead_of_using_vendor_native_switches() -> None:
    """Surplus advertises `reasoning`/`include_reasoning` for these ids, so the
    OpenRouter shape is correct -- the zai/deepseek switches the OpenCAP gateway
    takes would be rejected here."""
    catalog = ModelCatalog()

    for model in ("glm-5.3", "deepseek-v4-flash"):
        assert catalog.get_capabilities(model, provider_name="surplus").reasoning_format == (
            "openrouter"
        )

    # The OpenCAP gateway keeps its vendor-native shapes.
    assert catalog.get_capabilities("glm-5.3", provider_name="opencap").reasoning_format == "zai"
    assert (
        catalog.get_capabilities("deepseek-v4-flash", provider_name="opencap").reasoning_format
        == "deepseek"
    )


def test_surplus_capability_branch_does_not_leak_into_other_gateways() -> None:
    catalog = ModelCatalog()

    bankr = catalog.get_capabilities("glm-5.2", provider_name="bankr")
    assert bankr.supports_reasoning is False
    assert bankr.reasoning_format == "none"


def test_surplus_uses_conservative_shared_id_limits() -> None:
    """Shared bare ids resolve the gateway caps, not DeepSeek's direct ones."""
    catalog = ModelCatalog()

    assert catalog.resolve_max_tokens("deepseek-v4-flash", provider_name="surplus") == 128_000
    assert catalog.resolve_context_window("deepseek-v4-flash", provider_name="surplus") == 1_000_000


@pytest.mark.asyncio
async def test_fetch_surplus_uses_public_unauthenticated_catalog() -> None:
    captured: dict[str, object] = {}
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"data": [{"id": "glm-5.3", "name": "GLM 5.3"}]}

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
        payload = await catalog.fetch_surplus()

    assert captured["url"] == "https://api.surplusintelligence.ai/v1/models"
    assert captured["headers"] == {"Accept": "application/json"}
    assert payload == {"data": [{"id": "glm-5.3", "name": "GLM 5.3"}]}
    model = catalog.get("glm-5.3")
    assert model is not None
    assert model.provider == "surplus"


@pytest.mark.asyncio
async def test_slow_surplus_catalog_refresh_does_not_block_event_loop() -> None:
    request_started = asyncio.Event()
    release_request = asyncio.Event()
    ticker_ran = asyncio.Event()
    ticks = 0
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"data": []}

    async def slow_get(*_args, **_kwargs):
        request_started.set()
        await release_request.wait()
        return response

    async def ticker() -> None:
        nonlocal ticks
        while not release_request.is_set():
            ticks += 1
            ticker_ran.set()
            await asyncio.sleep(0)

    with patch.object(model_catalog_module.httpx, "AsyncClient") as client_cls:
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get = AsyncMock(side_effect=slow_get)
        client_cls.return_value = client

        refresh_task = asyncio.create_task(ModelCatalog().fetch_surplus())
        ticker_task = asyncio.create_task(ticker())
        await asyncio.wait_for(request_started.wait(), timeout=0.1)
        await asyncio.wait_for(ticker_ran.wait(), timeout=0.1)
        assert refresh_task.done() is False
        release_request.set()
        await refresh_task
        await ticker_task

    assert ticks > 0


@pytest.mark.asyncio
async def test_surplus_outbound_stream_request_includes_api_key_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'data: {"choices": [{"delta": {"content": "hello"}}]}\n\ndata: [DONE]\n\n',
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("agentos.provider.openai.httpx.AsyncClient", patched_async_client)
    provider = OpenAIProvider(
        api_key="inf_secret_key_123",
        model="gpt-5.6-luna",
        base_url="https://api.surplusintelligence.ai/v1",
        provider_kind="surplus",
    )

    async for _event in provider.chat([Message(role="user", content="hi")], config=ChatConfig()):
        pass

    headers = captured.get("headers", {})
    assert headers.get("authorization") == "Bearer inf_secret_key_123"
    assert headers.get("x-api-key") == "inf_secret_key_123"


@pytest.mark.asyncio
async def test_surplus_list_models_includes_api_key_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"data": [{"id": "gpt-5.6-luna", "name": "GPT 5.6 Luna"}]})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("agentos.provider.openai.httpx.AsyncClient", patched_async_client)
    provider = OpenAIProvider(
        api_key="inf_secret_key_123",
        model="gpt-5.6-luna",
        base_url="https://api.surplusintelligence.ai/v1",
        provider_kind="surplus",
    )

    models = await provider.list_models()
    assert len(models) == 1
    assert models[0].model_id == "gpt-5.6-luna"

    headers = captured.get("headers", {})
    assert headers.get("authorization") == "Bearer inf_secret_key_123"
    assert headers.get("x-api-key") == "inf_secret_key_123"
