"""Bankr LLM Gateway provider registration contract + OpenRouter defaults."""

from agentos.gateway.config import (
    ROUTER_TIER_PROFILE_IDS,
    LlmProviderConfig,
    _bankr_tiers,
    _default_tiers,
    _openrouter_tiers,
    _router_tier_profile_defaults,
)
from agentos.gateway.config_migration import migrate_config_payload
from agentos.onboarding.provider_specs import (
    get_provider_setup_spec,
    list_provider_setup_specs,
)
from agentos.provider.model_catalog import ModelCatalog
from agentos.provider.registry import get_provider_spec, list_provider_names


def test_bankr_gateway_is_registered() -> None:
    assert "bankr" in list_provider_names()


def test_opencap_gateway_is_removed() -> None:
    assert "opencap" not in list_provider_names()
    assert "opencap" not in ROUTER_TIER_PROFILE_IDS


def test_bankr_gateway_spec_contract() -> None:
    spec = get_provider_spec("bankr")
    assert spec.backend == "openai_compat"
    assert spec.env_key == "BANKR_API_KEY"
    assert spec.default_base_url == "https://llm.bankr.bot/v1"
    assert spec.runtime_supported is True
    assert spec.requires_api_key() is True
    assert spec.requires_base_url() is False
    assert spec.support_level == "compat_configured"


def test_bankr_gateway_onboarding_spec() -> None:
    spec = get_provider_setup_spec("bankr")
    assert spec.label == "Bankr LLM Gateway"
    assert spec.runtime_supported is True
    assert spec.deployment == "cloud"
    assert spec.requires_api_key is True
    assert any(f.name == "api_key" and f.required for f in spec.fields)


def test_openrouter_sorts_first_in_setup_list() -> None:
    specs = list_provider_setup_specs()
    assert specs[0].provider_id == "openrouter"


def test_llm_defaults_are_openrouter() -> None:
    cfg = LlmProviderConfig()
    assert cfg.provider == "openrouter"
    assert cfg.model == "deepseek/deepseek-v4-flash"
    assert cfg.base_url == "https://openrouter.ai/api/v1"


def test_default_tiers_route_through_openrouter() -> None:
    tiers = _default_tiers()
    assert tiers == _openrouter_tiers()
    assert tiers["c0"]["provider"] == "openrouter"
    assert tiers["c0"]["model"] == "deepseek/deepseek-v4-flash"
    assert tiers["c1"]["model"] == "deepseek/deepseek-v4-pro"
    assert tiers["c2"]["model"] == "z-ai/glm-5.1"
    assert tiers["c3"]["model"] == "anthropic/claude-opus-4.7"
    assert tiers["image_model"]["supports_image"] is True


def test_openrouter_is_the_default_tier_profile() -> None:
    assert "openrouter" in ROUTER_TIER_PROFILE_IDS
    assert _router_tier_profile_defaults(None) == _openrouter_tiers()
    assert _router_tier_profile_defaults("openrouter") == _openrouter_tiers()


def test_bankr_is_a_tier_profile() -> None:
    assert "bankr" in ROUTER_TIER_PROFILE_IDS
    tiers = _router_tier_profile_defaults("bankr")
    assert tiers == _bankr_tiers()
    assert tiers["c0"]["provider"] == "bankr"
    assert tiers["c0"]["model"] == "deepseek-v4-flash"
    assert tiers["c1"]["model"] == "minimax-m3"
    assert tiers["c2"]["model"] == "glm-5.2"
    assert tiers["c3"]["model"] == "claude-opus-4.8"
    assert tiers["image_model"]["model"] == "minimax-m3"
    assert tiers["image_model"]["supports_image"] is True


def test_bankr_gateway_capabilities_enable_vision_for_image_models() -> None:
    catalog = ModelCatalog()
    default_model = catalog.get_capabilities("minimax-m3", "bankr")
    assert default_model.supports_vision is True
    assert default_model.supports_tools is True
    for vision_id in ("claude-opus-4.8", "gemini-3.5-flash", "grok-4.3", "gpt-5.5"):
        assert catalog.get_capabilities(vision_id, "bankr").supports_vision is True
    for text_id in ("qwen3.7-max", "gpt-5.4-mini", "glm-5.2", "oc-uncensored-1.0"):
        assert catalog.get_capabilities(text_id, "bankr").supports_vision is False
    # Legacy namespaced ids from pre-migration configs keep working.
    legacy = catalog.get_capabilities("virtuals/kimi-k2.6", "bankr")
    assert legacy.supports_vision is True


def test_bankr_gateway_deepseek_flash_output_capped_below_direct_contract() -> None:
    # "deepseek-v4-flash" is shared: DeepSeek direct allows 393K output, but
    # the Bankr gateway caps the same id at 128K. The provider-aware fallback
    # must keep both contracts intact.
    catalog = ModelCatalog()
    assert (
        catalog.resolve_max_tokens("deepseek-v4-flash", provider_name="bankr")
        == 128_000
    )
    assert (
        catalog.resolve_context_window("deepseek-v4-flash", "bankr") == 1_000_000
    )
    # DeepSeek direct (and provider-less lookups) keep the direct contract.
    assert catalog.resolve_max_tokens("deepseek-v4-flash") == 393_216
    assert catalog.resolve_max_tokens("deepseek-v4-flash", provider_name="deepseek") == 393_216


def test_legacy_virtuals_model_ids_migrate_to_bare_catalog_ids() -> None:
    # The bankr gateway serves bare ids; a config that still carries the old
    # "virtuals/" namespace (with the current gateway provider) is upgraded.
    result = migrate_config_payload(
        {
            "llm": {"provider": "bankr", "model": "virtuals/minimax-m3"},
            "agentos_router": {
                "tiers": {
                    "c0": {"provider": "bankr", "model": "virtuals/deepseek-v4-flash"},
                    "c2": {"provider": "bankr", "model": "virtuals/qwen3.7-max"},
                    "image_model": {
                        "provider": "bankr",
                        "model": "virtuals/kimi-k2.6",
                    },
                }
            },
        }
    )
    assert result.changed is True
    assert result.payload["llm"]["model"] == "minimax-m3"
    tiers = result.payload["agentos_router"]["tiers"]
    assert tiers["c0"]["model"] == "deepseek-v4-flash"
    assert tiers["c2"]["model"] == "qwen3.7-max"
    assert tiers["image_model"]["model"] == "minimax-m3"


def test_legacy_migration_leaves_other_providers_untouched() -> None:
    result = migrate_config_payload(
        {
            "llm": {"provider": "openrouter", "model": "virtuals/minimax-m3"},
            "agentos_router": {
                "tiers": {"c0": {"provider": "openrouter", "model": "deepseek/deepseek-v4-flash"}}
            },
        }
    )
    assert result.payload["llm"]["model"] == "virtuals/minimax-m3"
    assert result.payload["agentos_router"]["tiers"]["c0"]["model"] == "deepseek/deepseek-v4-flash"
    assert result.changed is False


def test_retired_opencap_provider_ids_are_not_migrated() -> None:
    # The retired gateway provider ids are intentionally NOT migrated forward.
    # A config pinning them is left as-is so strict schema validation rejects it
    # and the operator re-selects a supported provider by hand.
    for legacy in ("capgateway", "opencap-gateway", "opencap"):
        result = migrate_config_payload(
            {
                "llm": {"provider": legacy, "model": "minimax-m3"},
                "agentos_router": {
                    "tiers": {"c0": {"provider": legacy, "model": "deepseek-v4-flash"}},
                },
            }
        )
        assert result.payload["llm"]["provider"] == legacy
        assert (
            result.payload["agentos_router"]["tiers"]["c0"]["provider"] == legacy
        )


def test_populate_from_bankr_parses_catalog_schema() -> None:
    catalog = ModelCatalog()
    catalog._populate_from_bankr(
        [
            {
                "id": "minimax-m3",
                "name": "MiniMax M3",
                "contextLength": 1_048_576,
                "maxOutput": 131_072,
                "modality": {"input": ["text", "image", "video"], "output": ["text"]},
            },
            {
                # maxOutput may be null in the live catalog.
                "id": "oc-uncensored-1.0",
                "name": "Uncensored 1.0",
                "contextLength": 262_144,
                "maxOutput": None,
                "modality": {"input": ["text"], "output": ["text"]},
            },
        ]
    )
    mm = catalog.get("minimax-m3")
    assert mm is not None
    assert mm.provider == "bankr"
    assert mm.context_window == 1_048_576
    assert mm.max_output_tokens == 131_072
    assert mm.supports_vision is True
    assert mm.supports_tools is True

    oc = catalog.get("oc-uncensored-1.0")
    assert oc is not None
    assert oc.max_output_tokens == 0  # null -> 0, resolved via fallback/default
    assert oc.supports_vision is False
    # A live text-only entry overrides the prefix heuristic in get_capabilities.
    assert catalog.get_capabilities("oc-uncensored-1.0", "bankr").supports_vision is False


def test_populate_from_bankr_reads_openai_style_field_names() -> None:
    # Bankr's /v1/models is OpenAI-compatible; the parser also accepts the
    # snake_case field names that a standard OpenAI-style list uses.
    catalog = ModelCatalog()
    catalog._populate_from_bankr(
        [
            {
                "id": "minimax-m3",
                "name": "MiniMax M3",
                "context_length": 1_000_000,
                "max_output": 64_000,
                "modality": {"input": ["text"]},
            },
        ]
    )
    mm = catalog.get("minimax-m3")
    assert mm is not None
    assert mm.context_window == 1_000_000
    assert mm.max_output_tokens == 64_000
