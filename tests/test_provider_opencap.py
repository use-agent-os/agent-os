"""OpenCAP gateway provider contracts."""

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
    _router_tier_profile_defaults,
)
from agentos.onboarding.provider_specs import get_provider_setup_spec
from agentos.provider import model_catalog as model_catalog_module
from agentos.provider.failures import ProviderFailureKind, classify_provider_error
from agentos.provider.model_catalog import ModelCatalog
from agentos.provider.openai import OpenAIProvider
from agentos.provider.registry import ProviderSpec, get_provider_spec, list_provider_names
from agentos.provider.selector import ProviderConfig, _build_provider
from agentos.provider.types import ChatConfig, Message


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
    assert spec.default_direct_model == "claude-haiku-5.5"
    model_field = next(field for field in spec.fields if field.name == "model")
    assert model_field.default == "claude-haiku-5.5"
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
    assert tiers["c0"]["model"] == "claude-haiku-5.5"
    assert tiers["c1"]["model"] == "claude-haiku-5.5"
    assert tiers["c2"]["model"] == "claude-sonnet-5.5"
    assert tiers["c3"]["model"] == "claude-opus-5.5"
    assert tiers["image_model"]["model"] == "claude-haiku-5.5"
    assert tiers["image_model"]["supports_image"] is True
    assert tiers["image_model"]["image_only"] is True
    assert all(tier["model"] != "oc-uncensored-1.0" for tier in tiers.values())


def test_opencap_router_profile_is_not_a_clone_of_bankr() -> None:
    """The two gateways publish different catalogs, so the tables must be free
    to diverge. They pick the same models today, but each is declared on its
    own and routes only through its own gateway."""
    from agentos.gateway.config import _bankr_tiers

    opencap = _router_tier_profile_defaults("opencap")
    bankr = _bankr_tiers()

    assert opencap is not bankr
    assert {tier["provider"] for tier in opencap.values()} == {"opencap"}
    assert {tier["provider"] for tier in bankr.values()} == {"bankr"}


def test_opencap_tier_models_are_all_served_by_the_live_catalog() -> None:
    """Every default is a bare id the gateway actually publishes.

    Pinned to the catalog snapshot rather than fetched, so the test stays
    offline; a default that drifts off this list is a default the gateway can
    no longer resolve.
    """
    published = {
        "deepseek-v4-flash",
        "deepseek-v4.1-flash",
        "gpt-5.6-luna",
        "gpt-6-luna",
        "glm-5.3",
        "glm-5.3-flash",
        "claude-opus-5",
        "claude-opus-5.5",
        "claude-sonnet-5.5",
        "claude-haiku-5.5",
        "minimax-m3",
        "grok-4.6",
        "kimi-k3",
        "muse-spark-1.2",
    }

    for name, tier in _router_tier_profile_defaults("opencap").items():
        assert tier["model"] in published, f"{name} pins an id OpenCAP no longer publishes"


def test_opencap_direct_provider_auto_selects_router_profile() -> None:
    cfg = GatewayConfig(llm={"provider": "opencap", "model": "minimax-m3"})

    assert cfg.agentos_router.tier_profile == "opencap"
    assert cfg.agentos_router.tiers == _router_tier_profile_defaults("opencap")


def test_opencap_router_profile_loads_from_disk_on_gateway_restart(tmp_path: Path) -> None:
    config_path = tmp_path / "agentos.toml"
    config_path.write_text(
        """
[llm]
provider = "opencap"
model = "minimax-m3"

[agentos_router]
enabled = true
tier_profile = "opencap"
""".strip(),
        encoding="utf-8",
    )

    cfg = GatewayConfig.load(config_path)

    assert cfg.config_path == str(config_path)
    assert cfg.llm.provider == "opencap"
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
                "pricing": {"input": 0.2541, "output": "1.0164"},
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
    assert minimax.input_cost_per_1k == pytest.approx(0.0002541)
    assert minimax.output_cost_per_1k == pytest.approx(0.0010164)

    uncensored = catalog.get("oc-uncensored-1.0")
    assert uncensored is not None
    assert uncensored.provider == "opencap"
    assert uncensored.max_output_tokens == 0
    assert catalog.get_capabilities("oc-uncensored-1.0", "opencap").supports_vision is False


def test_opencap_glm_models_resolve_zai_reasoning() -> None:
    """The c2 default declares thinking_level="high"; without a reasoning format
    that setting is silently dropped on the way to the gateway."""
    catalog = ModelCatalog()

    for model in ("glm-5.3", "glm-5.2", "glm-5.3-flash"):
        caps = catalog.get_capabilities(model, provider_name="opencap")
        assert caps.supports_reasoning is True, model
        assert caps.reasoning_format == "zai", model
        assert caps.supports_tools is True, model


def test_opencap_glm_reasoning_does_not_leak_into_the_bankr_gateway() -> None:
    """Separate deployment, unverified switch -- Bankr must keep its old shape."""
    caps = ModelCatalog().get_capabilities("glm-5.2", provider_name="bankr")

    assert caps.supports_reasoning is False
    assert caps.reasoning_format == "none"


def test_opencap_non_reasoning_models_keep_reporting_no_reasoning() -> None:
    catalog = ModelCatalog()

    for model in ("minimax-m3", "oc-uncensored-1.0", "gpt-5.6-luna"):
        caps = catalog.get_capabilities(model, provider_name="opencap")
        assert caps.supports_reasoning is False, model
        assert caps.reasoning_format == "none", model


def test_populate_from_opencap_defaults_missing_or_malformed_pricing_to_zero() -> None:
    catalog = ModelCatalog()
    catalog._populate_from_opencap(
        [
            {"id": "missing-price"},
            {
                "id": "malformed-price",
                "pricing": {"input": -1, "output": "not-a-number"},
            },
            {
                "id": "non-finite-price",
                "pricing": {"input": "nan", "output": "inf"},
            },
        ]
    )

    for model_id in ("missing-price", "malformed-price", "non-finite-price"):
        model = catalog.get(model_id)
        assert model is not None
        assert model.input_cost_per_1k == 0.0
        assert model.output_cost_per_1k == 0.0


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


@pytest.mark.asyncio
async def test_slow_opencap_catalog_refresh_does_not_block_event_loop() -> None:
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

        refresh_task = asyncio.create_task(ModelCatalog().fetch_opencap())
        ticker_task = asyncio.create_task(ticker())
        await asyncio.wait_for(request_started.wait(), timeout=0.1)
        await asyncio.wait_for(ticker_ran.wait(), timeout=0.1)
        assert refresh_task.done() is False
        release_request.set()
        await refresh_task
        await ticker_task

    assert ticks > 0


def test_opencap_gateway_uses_conservative_shared_id_limits() -> None:
    catalog = ModelCatalog()

    assert catalog.resolve_max_tokens("deepseek-v4-flash", provider_name="opencap") == 128_000
    assert catalog.resolve_context_window("deepseek-v4-flash", provider_name="opencap") == 1_000_000

    # The V4.1 default is published at the same 384K output / 1M context on the
    # gateway as everywhere else, so it carries no gateway override.
    assert catalog.resolve_max_tokens("deepseek-v4.1-flash", provider_name="opencap") == 384_000
    assert (
        catalog.resolve_context_window("deepseek-v4.1-flash", provider_name="opencap") == 1_048_576
    )


def test_opencap_deepseek_v4_models_resolve_deepseek_reasoning() -> None:
    # DeepSeek V4 via the OpenCAP/Bankr gateways honors the DeepSeek-native
    # thinking payload and streams reasoning_content (verified live); the
    # capability gate must say so or tier thinking_level silently no-ops.
    catalog = ModelCatalog()

    # deepseek-v4.1-flash is the shipped c0 on both gateways, so the offline
    # prefix heuristic has to cover the dotted id too, not just deepseek-v4-*.
    for provider in ("opencap", "bankr"):
        for model in ("deepseek-v4-flash", "deepseek-v4.1-flash"):
            caps = catalog.get_capabilities(model, provider_name=provider)
            assert caps.supports_reasoning is True, (provider, model)
            assert caps.reasoning_format == "deepseek", (provider, model)

    pro = catalog.get_capabilities("deepseek-v4-pro", provider_name="opencap")
    assert pro.supports_reasoning is True

    # Non-DeepSeek gateway ids stay reasoning-off: OpenAI/Anthropic never
    # expose chain-of-thought through these OpenAI-compat gateways.
    for model in ("gpt-5.6-terra", "gpt-5.6-luna", "claude-opus-5"):
        caps = catalog.get_capabilities(model, provider_name="opencap")
        assert caps.supports_reasoning is False
        assert caps.reasoning_format == "none"


@pytest.mark.asyncio
async def test_opencap_outbound_stream_request_includes_api_key_headers(
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
        api_key="oc_secret_key_123",
        model="gpt-5.6-luna",
        base_url="https://gw.capminal.ai/api/inference/v1",
        provider_kind="opencap",
    )

    events = []
    async for event in provider.chat([Message(role="user", content="hi")], config=ChatConfig()):
        events.append(event)

    headers = captured.get("headers", {})
    assert headers.get("authorization") == "Bearer oc_secret_key_123"
    assert headers.get("x-api-key") == "oc_secret_key_123"


@pytest.mark.asyncio
async def test_opencap_list_models_includes_api_key_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            json={"data": [{"id": "gpt-5.6-luna", "name": "GPT 5.6 Luna"}]},
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("agentos.provider.openai.httpx.AsyncClient", patched_async_client)
    provider = OpenAIProvider(
        api_key="oc_secret_key_123",
        model="gpt-5.6-luna",
        base_url="https://gw.capminal.ai/api/inference/v1",
        provider_kind="opencap",
    )

    models = await provider.list_models()
    assert len(models) == 1
    assert models[0].model_id == "gpt-5.6-luna"

    headers = captured.get("headers", {})
    assert headers.get("authorization") == "Bearer oc_secret_key_123"
    assert headers.get("x-api-key") == "oc_secret_key_123"


@pytest.mark.asyncio
async def test_openai_compat_outbound_stream_request_omits_x_api_key_header(
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
        api_key="sk-openai-compat-key",
        model="custom-model",
        base_url="https://api.example.com/v1",
        provider_kind="openai_compat",
    )

    events = []
    async for event in provider.chat([Message(role="user", content="hi")], config=ChatConfig()):
        events.append(event)

    headers = captured.get("headers", {})
    assert headers.get("authorization") == "Bearer sk-openai-compat-key"
    assert "x-api-key" not in headers
