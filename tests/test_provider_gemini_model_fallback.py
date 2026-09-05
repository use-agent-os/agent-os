from __future__ import annotations

import pytest

from agentos.gateway.config import GatewayConfig, LlmProviderConfig, _router_tier_profile_defaults
from agentos.onboarding.mutations import upsert_llm_provider
from agentos.onboarding.provider_specs import get_provider_setup_spec
from agentos.provider.openai import OpenAIProvider
from agentos.provider.selector import ModelSelector, ProviderConfig, SelectorConfig


def test_llm_provider_config_gemini_defaults_to_gemini_3_1_pro_preview() -> None:
    cfg = LlmProviderConfig(provider="gemini", model="")
    assert cfg.model == "gemini-3.1-pro-preview"


def test_llm_provider_config_gemini_upgrades_legacy_2_5_pro() -> None:
    cfg = LlmProviderConfig(provider="gemini", model="gemini-2.5-pro")
    assert cfg.model == "gemini-3.1-pro-preview"


def test_llm_provider_config_gemini_respects_explicit_model() -> None:
    cfg = LlmProviderConfig(provider="gemini", model="gemini-3.5-flash")
    assert cfg.model == "gemini-3.5-flash"


def test_llm_provider_config_gemini_respects_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_MODEL", "gemini-custom-override")
    cfg = LlmProviderConfig(provider="gemini", model="")
    assert cfg.model == "gemini-custom-override"


def test_gemini_router_tiers_use_gemini_3_1_pro_preview() -> None:
    tiers = _router_tier_profile_defaults("gemini")
    assert tiers["c2"]["model"] == "gemini-3.1-pro-preview"
    assert tiers["c3"]["model"] == "gemini-3.1-pro-preview"


def test_openai_provider_gemini_defaults_to_3_1_pro_preview() -> None:
    provider = OpenAIProvider(api_key="test-key", model="gemini-2.5-pro", provider_kind="gemini")
    assert provider.model == "gemini-3.1-pro-preview"


def test_openai_provider_gemini_respects_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_MODEL", "gemini-env-model")
    provider = OpenAIProvider(api_key="test-key", model="gemini-2.5-pro", provider_kind="gemini")
    assert provider.model == "gemini-env-model"


def test_model_selector_gemini_build() -> None:
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(provider="gemini", model="gemini-2.5-pro", api_key="test-key")
        )
    )
    provider = selector.resolve()
    assert getattr(provider, "model", None) == "gemini-3.1-pro-preview"


def test_onboarding_gemini_provider_spec() -> None:
    spec = get_provider_setup_spec("gemini")
    assert spec is not None
    assert spec.default_direct_model == "gemini-3.1-pro-preview"


def test_onboarding_mutation_gemini_setup() -> None:
    initial_cfg = GatewayConfig()
    result = upsert_llm_provider(
        initial_cfg,
        provider_id="gemini",
        api_key="test-gemini-key",
        model="",
    )
    assert result.config.llm.provider == "gemini"
    assert result.config.llm.model == "gemini-3.1-pro-preview"
