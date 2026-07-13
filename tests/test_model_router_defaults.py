import tomllib
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

from agentos.gateway.config import ROUTER_TIER_PROFILE_IDS

REPO_ROOT = Path(__file__).resolve().parents[1]
# openrouter is the baked-in default tier set, so it never auto-selects a
# tier_profile when chosen as the direct provider. It is excluded from the
# "non-default direct provider auto-selects its matching profile"
# parametrization; bankr and the rest still auto-select.
DIRECT_ROUTER_PROFILE_IDS = sorted(
    ROUTER_TIER_PROFILE_IDS - {"openrouter"}
)


def _agentos_router_config_cls():
    config_path = REPO_ROOT / "src" / "agentos" / "gateway" / "config.py"
    spec = spec_from_file_location("agentos_gateway_config_under_test", config_path)
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.AgentOSRouterConfig


def _gateway_config_cls():
    from agentos.gateway.config import GatewayConfig

    return GatewayConfig


def test_agentos_router_defaults_match_runtime_router_config() -> None:
    agentos_router_config_cls = _agentos_router_config_cls()
    cfg = agentos_router_config_cls()

    assert cfg.enabled is True
    assert cfg.auto_thinking is True
    assert cfg.rollout_phase == "full"
    assert cfg.strategy == "v4_phase3"
    assert cfg.judge_model is None
    assert cfg.judge_provider is None
    assert cfg.judge_input_max_chars == 4000
    assert cfg.judge_short_circuit_enabled is True
    assert cfg.default_tier == "c1"
    assert cfg.confidence_threshold == 0.5
    assert cfg.kv_cache_anti_downgrade_enabled is True
    assert cfg.kv_cache_anti_downgrade_window_seconds == 600
    assert cfg.complaint_upgrade_enabled is True
    assert cfg.complaint_upgrade_steps == 1
    assert cfg.complaint_upgrade_max_chars == 160
    # Restored with the reintegrated v4_phase3 ML router; real fields with
    # defaults.
    assert cfg.v4_bundle_dir is None
    assert cfg.v4_use_aux_head is True
    assert cfg.require_router_runtime is False

    assert cfg.tiers["c0"]["model"] == "deepseek/deepseek-v4-flash"
    assert cfg.tiers["c0"]["thinking_level"] == "high"
    assert cfg.tiers["c1"]["model"] == "deepseek/deepseek-v4-pro"
    assert cfg.tiers["c1"]["thinking_level"] == "high"
    assert cfg.tiers["c2"]["model"] == "z-ai/glm-5.1"
    assert cfg.tiers["c2"]["thinking_level"] == "high"
    assert cfg.tiers["c3"]["model"] == "anthropic/claude-opus-4.7"
    assert cfg.tiers["c3"]["thinking_level"] == "high"
    assert cfg.tiers["image_model"]["model"] == "moonshotai/kimi-k2.6"
    assert cfg.tiers["image_model"]["supports_image"] is True
    assert cfg.tiers["image_model"]["image_only"] is True


def test_confidence_threshold_is_bounded_to_unit_interval() -> None:
    """Finding: confidence_threshold must be bounded to [0.0, 1.0]. The judge
    pins confidence=1.0 to keep the confidence gate inert (spec D3); that only
    holds while threshold <= 1.0. A value >1.0 would silently downgrade every
    non-default judged turn to the default tier, disabling the router. The
    field must reject out-of-range values instead of accepting a kill-switch."""
    agentos_router_config_cls = _agentos_router_config_cls()

    # Boundary values are accepted.
    assert agentos_router_config_cls(confidence_threshold=0.0).confidence_threshold == 0.0
    assert agentos_router_config_cls(confidence_threshold=1.0).confidence_threshold == 1.0

    # A value >1.0 (the kill-switch) must be rejected.
    with pytest.raises(ValueError, match="confidence_threshold"):
        agentos_router_config_cls(confidence_threshold=2.0)

    # A negative value is also rejected.
    with pytest.raises(ValueError, match="confidence_threshold"):
        agentos_router_config_cls(confidence_threshold=-0.1)


def test_agentos_router_strategy_accepts_v4_phase3() -> None:
    # v4_phase3 is the reintegrated default local ML router strategy.
    agentos_router_config_cls = _agentos_router_config_cls()

    cfg = agentos_router_config_cls(strategy="v4_phase3")

    assert cfg.strategy == "v4_phase3"


def test_agentos_router_strategy_rejects_unknown_value() -> None:
    agentos_router_config_cls = _agentos_router_config_cls()

    with pytest.raises(ValueError, match="strategy"):
        agentos_router_config_cls(strategy="llm_jugde")


def test_agentos_router_default_profile_is_openrouter() -> None:
    from agentos.gateway.config import _bankr_tiers, _openrouter_tiers

    agentos_router_config_cls = _agentos_router_config_cls()

    default_cfg = agentos_router_config_cls()
    explicit_cfg = agentos_router_config_cls(tier_profile="openrouter")
    bankr_cfg = agentos_router_config_cls(tier_profile="bankr")

    # openrouter is now the baked-in default, so the explicit openrouter profile
    # matches the default tiers; the bankr gateway profile is distinct.
    assert explicit_cfg.tiers == default_cfg.tiers
    assert explicit_cfg.tiers == _openrouter_tiers()
    assert explicit_cfg.tier_profile == "openrouter"
    assert bankr_cfg.tiers == _bankr_tiers()
    assert bankr_cfg.tiers != default_cfg.tiers


def test_agentos_router_canonical_tier_wins_over_legacy_alias() -> None:
    agentos_router_config_cls = _agentos_router_config_cls()

    cfg = agentos_router_config_cls(
        tiers={
            "c1": {"provider": "openrouter", "model": "canonical-model"},
            "t1": {"provider": "openrouter", "model": "legacy-model"},
        },
        default_tier="t1",
    )

    assert cfg.default_tier == "c1"
    assert cfg.tiers["c1"]["model"] == "canonical-model"


def test_provider_profile_requires_matching_llm_provider() -> None:
    gateway_config_cls = _gateway_config_cls()

    try:
        gateway_config_cls(
            llm={"provider": "openrouter"},
            agentos_router={"tier_profile": "dashscope"},
        )
    except ValueError as exc:
        assert "agentos_router.tier_profile requires llm.provider" in str(exc)
    else:
        raise AssertionError("expected provider/profile mismatch to fail")


def test_explicit_openrouter_profile_requires_openrouter_provider() -> None:
    gateway_config_cls = _gateway_config_cls()

    try:
        gateway_config_cls(
            llm={"provider": "deepseek"},
            agentos_router={"tier_profile": "openrouter"},
        )
    except ValueError as exc:
        assert "agentos_router.tier_profile requires llm.provider" in str(exc)
    else:
        raise AssertionError("expected explicit openrouter profile mismatch to fail")


def test_provider_profile_accepts_matching_llm_provider() -> None:
    gateway_config_cls = _gateway_config_cls()

    cfg = gateway_config_cls(
        llm={"provider": "dashscope"},
        agentos_router={"tier_profile": "dashscope"},
    )

    assert cfg.llm.provider == "dashscope"
    assert cfg.agentos_router.tier_profile == "dashscope"
    assert cfg.agentos_router.tiers["c0"]["provider"] == "dashscope"
    assert cfg.agentos_router.tiers["c0"]["model"] == "qwen3.6-flash"


def test_cross_provider_judge_is_reset_to_auto_at_config_load() -> None:
    """Finding #10: a hand-edited TOML with judge_provider != llm.provider has no
    credential source (tier entries carry no creds) and would degrade to
    judge_unavailable on every turn. A GatewayConfig validator must reset it to
    AUTO at load, mirroring the onboarding mutation, so config load stays valid
    but the stale cross-provider judge does not silently break routing."""
    gateway_config_cls = _gateway_config_cls()

    cfg = gateway_config_cls(
        llm={"provider": "deepseek", "model": "deepseek-chat"},
        agentos_router={
            "judge_model": "gpt-5.4-mini",
            "judge_provider": "openai",
        },
    )

    assert cfg.agentos_router.judge_model is None
    assert cfg.agentos_router.judge_provider is None


def test_matching_provider_judge_is_preserved_at_config_load() -> None:
    gateway_config_cls = _gateway_config_cls()

    cfg = gateway_config_cls(
        llm={"provider": "deepseek", "model": "deepseek-chat"},
        agentos_router={
            "judge_model": "deepseek-v4-pro",
            "judge_provider": "deepseek",
        },
    )

    assert cfg.agentos_router.judge_model == "deepseek-v4-pro"
    assert cfg.agentos_router.judge_provider == "deepseek"


def test_local_endpoint_judge_is_preserved_at_config_load() -> None:
    """A local-endpoint judge (judge_base_url set) carries its own credentials
    via judge_api_key and bypasses the provider-match constraint, so the
    cross-provider reset validator must NOT clear it even though it has no
    judge_provider matching llm.provider."""
    gateway_config_cls = _gateway_config_cls()

    cfg = gateway_config_cls(
        llm={"provider": "deepseek", "model": "deepseek-chat"},
        agentos_router={
            "judge_model": "llama3",
            "judge_base_url": "http://localhost:11434/v1",
            "judge_api_key": "sk-local",
        },
    )

    assert cfg.agentos_router.judge_model == "llama3"
    assert cfg.agentos_router.judge_base_url == "http://localhost:11434/v1"
    assert cfg.agentos_router.judge_api_key == "sk-local"


@pytest.mark.parametrize("provider_id", DIRECT_ROUTER_PROFILE_IDS)
def test_unset_tier_profile_uses_matching_direct_provider_profile(provider_id: str) -> None:
    from agentos.gateway.config import _router_tier_profile_defaults

    gateway_config_cls = _gateway_config_cls()

    cfg = gateway_config_cls(llm={"provider": provider_id})

    expected = _router_tier_profile_defaults(provider_id)
    assert cfg.agentos_router.tier_profile == provider_id
    for tier in ("c0", "c1", "c2", "c3"):
        assert cfg.agentos_router.tiers[tier]["provider"] == provider_id
        assert cfg.agentos_router.tiers[tier]["model"] == expected[tier]["model"]


@pytest.mark.parametrize("provider_id", DIRECT_ROUTER_PROFILE_IDS)
def test_direct_legacy_openrouter_router_defaults_are_migrated(provider_id: str) -> None:
    from agentos.gateway.config import _router_tier_profile_defaults

    gateway_config_cls = _gateway_config_cls()

    cfg = gateway_config_cls(
        llm={"provider": provider_id},
        agentos_router={"enabled": True, "tiers": _router_tier_profile_defaults("openrouter")},
    )

    expected = _router_tier_profile_defaults(provider_id)
    assert cfg.agentos_router.tier_profile == provider_id
    for tier in ("c0", "c1", "c2", "c3"):
        assert cfg.agentos_router.tiers[tier]["provider"] == provider_id
        assert cfg.agentos_router.tiers[tier]["model"] == expected[tier]["model"]


def test_deepseek_direct_legacy_openrouter_model_default_is_normalized() -> None:
    gateway_config_cls = _gateway_config_cls()

    cfg = gateway_config_cls(
        llm={
            "provider": "deepseek",
            "model": "deepseek/deepseek-v4-pro",
        }
    )

    assert cfg.llm.model == "deepseek-v4-pro"


def test_each_provider_profile_has_four_text_tiers_without_default_image_model() -> None:
    agentos_router_config_cls = _agentos_router_config_cls()

    for profile in ("dashscope", "deepseek", "gemini", "volcengine"):
        cfg = agentos_router_config_cls(tier_profile=profile)
        assert {"c0", "c1", "c2", "c3"}.issubset(cfg.tiers)
        assert "image_model" not in cfg.tiers
        assert {cfg.tiers[tier]["provider"] for tier in ("c0", "c1", "c2", "c3")} == {
            profile
        }


def test_direct_provider_profiles_have_four_text_tiers_without_default_image_model() -> None:
    agentos_router_config_cls = _agentos_router_config_cls()

    for profile in ("openai", "zhipu", "moonshot"):
        cfg = agentos_router_config_cls(tier_profile=profile)
        assert {"c0", "c1", "c2", "c3"}.issubset(cfg.tiers)
        assert "image_model" not in cfg.tiers
        assert {cfg.tiers[tier]["provider"] for tier in ("c0", "c1", "c2", "c3")} == {
            profile
        }


def test_openai_profile_uses_streaming_compatible_models() -> None:
    agentos_router_config_cls = _agentos_router_config_cls()

    cfg = agentos_router_config_cls(tier_profile="openai")

    assert cfg.tiers["c0"]["model"] == "gpt-5.4-nano"
    assert cfg.tiers["c1"]["model"] == "gpt-5.4-mini"
    assert cfg.tiers["c2"]["model"] == "gpt-5.5"
    assert cfg.tiers["c3"]["model"] == "gpt-5.5"
    assert cfg.tiers["c3"]["thinking_level"] == "high"
    assert all(
        cfg.tiers[tier]["model"] != "gpt-5.5-pro" for tier in ("c0", "c1", "c2", "c3")
    )


def test_zhipu_profile_uses_glm_5_1_for_strong_tiers() -> None:
    agentos_router_config_cls = _agentos_router_config_cls()

    cfg = agentos_router_config_cls(tier_profile="zhipu")

    assert cfg.tiers["c0"]["model"] == "glm-4.7-flashx"
    assert cfg.tiers["c1"]["model"] == "glm-5"
    assert cfg.tiers["c2"]["model"] == "glm-5.1"
    assert cfg.tiers["c3"]["model"] == "glm-5.1"
    assert cfg.tiers["c3"]["thinking_level"] == "high"


def test_moonshot_profile_uses_kimi_for_strong_tiers() -> None:
    agentos_router_config_cls = _agentos_router_config_cls()

    cfg = agentos_router_config_cls(tier_profile="moonshot")

    assert cfg.tiers["c0"]["model"] == "kimi-k2.5"
    assert cfg.tiers["c1"]["model"] == "kimi-k2.5"
    assert cfg.tiers["c2"]["model"] == "kimi-k2.6"
    assert cfg.tiers["c3"]["model"] == "kimi-k2.6"
    assert all(
        cfg.tiers[tier]["supports_image"] is True for tier in ("c0", "c1", "c2", "c3")
    )


def test_volcengine_profile_uses_seed_2_capability_ladder() -> None:
    agentos_router_config_cls = _agentos_router_config_cls()

    cfg = agentos_router_config_cls(tier_profile="volcengine")

    assert cfg.tiers["c0"]["model"] == "doubao-seed-2-0-mini-260215"
    assert cfg.tiers["c0"]["thinking_level"] == "off"
    assert cfg.tiers["c1"]["model"] == "doubao-seed-2-0-lite-260215"
    assert cfg.tiers["c1"]["thinking_level"] == "low"
    assert cfg.tiers["c2"]["model"] == "doubao-seed-2-0-pro-260215"
    assert cfg.tiers["c2"]["thinking_level"] == "medium"
    assert cfg.tiers["c3"]["model"] == "doubao-seed-2-0-code-preview-260215"
    assert cfg.tiers["c3"]["thinking_level"] == "high"


def test_profile_tier_override_merges_keys_inside_tier() -> None:
    agentos_router_config_cls = _agentos_router_config_cls()

    cfg = agentos_router_config_cls(
        tier_profile="gemini",
        tiers={"c2": {"thinking_level": "high"}},
    )

    assert cfg.tiers["c2"]["provider"] == "gemini"
    assert cfg.tiers["c2"]["model"] == "gemini-2.5-pro"
    assert cfg.tiers["c2"]["thinking_level"] == "high"


def test_profile_rejects_non_dict_tier_override() -> None:
    agentos_router_config_cls = _agentos_router_config_cls()

    with pytest.raises((ValueError, TypeError)) as excinfo:
        agentos_router_config_cls(
            tier_profile="gemini",
            tiers=[],
        )

    assert "tiers" in str(excinfo.value)


def test_profile_preserves_explicit_provider_compatible_image_model() -> None:
    agentos_router_config_cls = _agentos_router_config_cls()

    cfg = agentos_router_config_cls(
        tier_profile="gemini",
        tiers={
            "image_model": {
                "provider": "gemini",
                "model": "gemini-2.5-flash",
                "supports_image": True,
                "image_only": True,
            }
        },
    )

    assert cfg.tiers["image_model"]["provider"] == "gemini"
    assert cfg.tiers["image_model"]["supports_image"] is True
    assert cfg.tiers["c0"]["provider"] == "gemini"


def test_example_toml_enables_runtime_router_defaults() -> None:
    example = REPO_ROOT / "agentos.toml.example"

    data = tomllib.loads(example.read_text(encoding="utf-8"))
    agentos_router = data["agentos_router"]

    assert data["llm"]["provider"] == "openrouter"
    assert data["llm"]["model"] == "deepseek/deepseek-v4-flash"
    assert agentos_router["enabled"] is True
    assert agentos_router["auto_thinking"] is True
    assert agentos_router["rollout_phase"] == "full"
    assert agentos_router["strategy"] == "llm_judge"
    assert "judge_model" not in agentos_router  # unset = AUTO judge resolution
    assert agentos_router["judge_input_max_chars"] == 4000
    assert agentos_router["judge_short_circuit_enabled"] is True
    assert "cache_ttl_seconds" not in agentos_router
    assert agentos_router["default_tier"] == "c1"
    assert agentos_router["confidence_threshold"] == 0.5
    assert agentos_router["kv_cache_anti_downgrade_enabled"] is True
    assert agentos_router["kv_cache_anti_downgrade_window_seconds"] == 600
    assert agentos_router["complaint_upgrade_enabled"] is True
    assert agentos_router["complaint_upgrade_steps"] == 1
    assert agentos_router["complaint_upgrade_max_chars"] == 160
    # Removed with the v4_phase3 ML bundle.
    assert "v4_use_aux_head" not in agentos_router
    assert "v4_bundle_dir" not in agentos_router
    assert "require_router_runtime" not in agentos_router

    tiers = agentos_router["tiers"]
    assert tiers["c0"]["model"] == "deepseek/deepseek-v4-flash"
    assert tiers["c0"]["thinking_level"] == "high"
    assert tiers["c1"]["model"] == "deepseek/deepseek-v4-pro"
    assert tiers["c1"]["thinking_level"] == "high"
    assert tiers["c2"]["model"] == "z-ai/glm-5.1"
    assert tiers["c2"]["thinking_level"] == "high"
    assert tiers["c3"]["model"] == "anthropic/claude-opus-4.7"
    assert tiers["c3"]["thinking_level"] == "high"
    assert tiers["image_model"]["model"] == "moonshotai/kimi-k2.6"
    assert tiers["image_model"]["supports_image"] is True
    assert tiers["image_model"]["image_only"] is True


def test_v4_phase3_module_is_present() -> None:
    # The v4_phase3 ML router was reintegrated as the default strategy, so its
    # tracked module ships in the package again. The 75MB model bundle under
    # agentos_router/models/ is git-ignored (present locally, absent in CI /
    # public checkouts), so this test deliberately asserts nothing about that
    # directory's presence — only the tracked source module and the relocated
    # BGE ONNX export (memory embedding).
    assert (REPO_ROOT / "src" / "agentos" / "agentos_router" / "v4_phase3.py").is_file()
    assert (REPO_ROOT / "src" / "agentos" / "memory" / "models" / "bge_onnx").is_dir()
