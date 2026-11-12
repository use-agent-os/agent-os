"""Config migration: agentos_router.strategy handling and threshold clamp."""

import pytest

from agentos.gateway.config_migration import migrate_config_payload

# ---------------------------------------------------------------------------
# Force default-flip migration: v4_phase3 -> pilot-v1 (unconditional).
#
# The default strategy flipped v4_phase3 -> pilot-v1, but historical onboarding
# persisted `strategy = "v4_phase3"` explicitly, so an upgraded install would
# silently stay on the legacy router. This migration force-rewrites every such
# config to pilot-v1 on load — there is no supported way to persist v4_phase3
# (the legacy engine remains in-tree only as an evaluation baseline). The flip
# is idempotent: once rewritten, the strategy is pilot-v1 and re-running is a
# no-op.
#
# Semantics table (each case below is a test):
#   (a) no strategy key      -> untouched (default already applies)
#   (b) v4_phase3            -> pilot-v1, changed=True
#   (c) pilot-v1 / llm_judge -> untouched
#   idempotency: migrating an already-migrated payload -> changed=False
# ---------------------------------------------------------------------------


def _router(result) -> dict:
    return result.payload["agentos_router"]


def test_case_a_missing_strategy_key_is_untouched() -> None:
    # (a) A config without an explicit strategy already gets the new default;
    # do NOT touch it.
    result = migrate_config_payload({"agentos_router": {"enabled": True}})

    assert result.changed is False
    assert "strategy" not in _router(result)

    # An empty payload has no router section at all.
    assert migrate_config_payload({}).changed is False


def test_case_b_v4_phase3_is_force_migrated_to_pilot() -> None:
    # (b) The core migration: a persisted v4_phase3 is unconditionally rewritten
    # to pilot-v1 and changed=True so the loader backs up and rewrites the file.
    result = migrate_config_payload({"agentos_router": {"enabled": True, "strategy": "v4_phase3"}})

    assert result.changed is True
    assert _router(result)["strategy"] == "pilot-v1"
    assert any("strategy" in change and "pilot-v1" in change for change in result.changes)


def test_case_c_pilot_and_judge_strategies_are_untouched() -> None:
    # (c) A config already on pilot-v1 or llm_judge is left entirely alone.
    for strategy in ("pilot-v1", "llm_judge"):
        result = migrate_config_payload({"agentos_router": {"enabled": True, "strategy": strategy}})
        assert result.changed is False
        assert _router(result)["strategy"] == strategy


def test_migration_is_idempotent_on_already_migrated_payload() -> None:
    # Running the migration on its own output is a no-op: the strategy is now
    # pilot-v1, so no further rewrite (changed=False).
    once = migrate_config_payload({"agentos_router": {"enabled": True, "strategy": "v4_phase3"}})
    assert once.changed is True

    twice = migrate_config_payload(once.payload)
    assert twice.changed is False
    assert _router(twice)["strategy"] == "pilot-v1"


def test_migrated_payload_boots_the_router_config() -> None:
    # The migrated payload must construct AgentOSRouterConfig without raising.
    from agentos.gateway.config import AgentOSRouterConfig

    result = migrate_config_payload({"agentos_router": {"enabled": True, "strategy": "v4_phase3"}})
    cfg = AgentOSRouterConfig(**_router(result))
    assert cfg.strategy == "pilot-v1"


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (2.0, 1.0),  # legacy "always gate to default" > 1.0
        (1.5, 1.0),
        (-0.5, 0.0),  # negative
        (1, 1.0),  # int in range is coerced to float but not otherwise changed
    ],
)
def test_out_of_range_confidence_threshold_is_clamped(stored, expected) -> None:
    """Finding (round 9): confidence_threshold gained a strict [0.0, 1.0] bound.
    A legacy TOML with an out-of-range value (a functioning knob under the old v4
    confidence gate) would now fail schema validation and crash the gateway on
    boot. Migration must clamp it into range so old configs boot cleanly."""
    result = migrate_config_payload(
        {
            "agentos_router": {
                "enabled": True,
                "strategy": "v4_phase3",
                "confidence_threshold": stored,
            }
        }
    )

    clamped = result.payload["agentos_router"]["confidence_threshold"]
    assert clamped == expected


def test_out_of_range_confidence_threshold_records_change() -> None:
    result = migrate_config_payload(
        {"agentos_router": {"enabled": True, "confidence_threshold": 2.0}}
    )
    assert result.changed is True
    assert any(
        "confidence_threshold" in change and "clamped" in change for change in result.changes
    )


def test_in_range_confidence_threshold_is_untouched() -> None:
    result = migrate_config_payload(
        {"agentos_router": {"enabled": True, "confidence_threshold": 0.5}}
    )
    assert result.payload["agentos_router"]["confidence_threshold"] == 0.5
    assert not any("confidence_threshold" in change for change in result.changes)


def test_non_numeric_confidence_threshold_is_left_for_schema() -> None:
    # A bool or string is not clamped here (bool is intentionally excluded so
    # True/False aren't coerced to 1.0/0.0); the strict schema rejects it, which
    # is the correct loud failure for a genuinely malformed value.
    for bad in (True, "high"):
        result = migrate_config_payload(
            {"agentos_router": {"enabled": True, "confidence_threshold": bad}}
        )
        assert result.payload["agentos_router"]["confidence_threshold"] == bad
        assert not any("confidence_threshold" in change for change in result.changes)


def test_clamped_confidence_threshold_boots_the_router_config() -> None:
    """The migrated payload must construct AgentOSRouterConfig without raising —
    proving the boot crash is actually resolved end-to-end, not just that the
    number changed."""
    from agentos.gateway.config import AgentOSRouterConfig

    # Pre-migration: the raw stale value hard-fails validation.
    with pytest.raises(Exception):
        AgentOSRouterConfig(confidence_threshold=2.0)

    result = migrate_config_payload(
        {"agentos_router": {"enabled": True, "confidence_threshold": 2.0}}
    )
    cfg = AgentOSRouterConfig(**result.payload["agentos_router"])
    assert cfg.confidence_threshold == 1.0


def test_legacy_openrouter_default_models_are_migrated_forward() -> None:
    result = migrate_config_payload(
        {
            "llm": {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro"},
            "agentos_router": {
                "enabled": True,
                "tiers": {
                    "c1": {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro"},
                    "c2": {"provider": "openrouter", "model": "z-ai/glm-5.1"},
                    "c3": {"provider": "openrouter", "model": "anthropic/claude-opus-4.7"},
                    "image_model": {
                        "provider": "openrouter",
                        "model": "moonshotai/kimi-k2.6",
                    },
                },
            },
        }
    )

    assert result.changed is True
    assert result.payload["llm"]["model"] == "anthropic/claude-haiku-5.5"
    tiers = result.payload["agentos_router"]["tiers"]
    assert tiers["c1"]["model"] == "anthropic/claude-haiku-5.5"
    assert tiers["c2"]["model"] == "anthropic/claude-sonnet-5.5"
    assert tiers["c3"]["model"] == "anthropic/claude-opus-5.5"
    assert tiers["image_model"]["model"] == "anthropic/claude-haiku-5.5"


def test_superseded_opus_c3_default_is_migrated_to_current_opus() -> None:
    # claude-opus-4.8 still resolves upstream, but it is the previous C3 default,
    # so configs carrying it forward are refreshed — the same treatment
    # claude-opus-4.7 got when 4.8 replaced it. Both the namespaced OpenRouter id
    # and the bare gateway id are covered.
    result = migrate_config_payload(
        {
            "llm": {"provider": "bankr", "model": "claude-opus-4.8"},
            "agentos_router": {
                "enabled": True,
                "tiers": {
                    "c3": {"provider": "openrouter", "model": "anthropic/claude-opus-4.8"},
                    "image_model": {"provider": "openrouter", "model": "minimax/minimax-m3"},
                },
            },
        }
    )

    assert result.changed is True
    assert result.payload["llm"]["model"] == "claude-opus-5.5"
    tiers = result.payload["agentos_router"]["tiers"]
    assert tiers["c3"]["model"] == "anthropic/claude-opus-5.5"
    # The vision route still runs on MiniMax, so it must survive the migration.
    assert tiers["image_model"]["model"] == "minimax/minimax-m3"


def test_non_default_openrouter_models_are_left_untouched() -> None:
    result = migrate_config_payload(
        {
            "llm": {"provider": "openrouter", "model": "qwen/qwen3.7-max"},
            "agentos_router": {
                "enabled": True,
                "tiers": {
                    "c2": {"provider": "openrouter", "model": "mistralai/mistral-large"},
                },
            },
        }
    )

    assert result.changed is False
    assert result.payload["llm"]["model"] == "qwen/qwen3.7-max"
    tiers = result.payload["agentos_router"]["tiers"]
    assert tiers["c2"]["model"] == "mistralai/mistral-large"


def test_legacy_openrouter_models_untouched_for_other_providers() -> None:
    # The same id string under a non-openrouter provider must not be rewritten.
    result = migrate_config_payload(
        {"llm": {"provider": "anthropic", "model": "anthropic/claude-opus-4.7"}}
    )

    assert result.changed is False
    assert result.payload["llm"]["model"] == "anthropic/claude-opus-4.7"


# ---------------------------------------------------------------------------
# Legacy model rewrites vs the shipped tier defaults (issue #140).
#
# The rewrite maps are a fifth place a model id is written down, and the only
# one nothing checked. Both directions can rot silently, and both produce a
# working-looking config pointed at the wrong model.
# ---------------------------------------------------------------------------


def _legacy_rewrites() -> dict[str, str]:
    from agentos.gateway.config_migration import (
        LEGACY_GATEWAY_MODEL_IDS,
        LEGACY_OPENROUTER_MODEL_IDS,
    )

    return {**LEGACY_GATEWAY_MODEL_IDS, **LEGACY_OPENROUTER_MODEL_IDS}


def _shipped_tier_models() -> set[str]:
    from agentos.gateway.config import (
        ROUTER_TIER_PROFILE_IDS,
        _router_tier_profile_defaults,
    )

    return {
        str(tier["model"])
        for profile in ROUTER_TIER_PROFILE_IDS
        for tier in _router_tier_profile_defaults(profile).values()
        if tier.get("model")
    }


def test_every_legacy_rewrite_target_is_a_model_we_still_ship() -> None:
    """Retiring a default must not leave a migration aiming at a dead id.

    A rewrite target that is no longer a tier default still gets written into
    the user's config on upgrade, and neither pricing nor the catalog would
    complain -- they would just quote numbers for a model nobody routes to.
    """
    from agentos import model_registry

    shipped = _shipped_tier_models()

    for legacy_id, target in _legacy_rewrites().items():
        assert model_registry.by_id(target) is not None, (
            f"{legacy_id} is rewritten to {target}, which is not declared in the model registry"
        )
        assert target in shipped, (
            f"{legacy_id} is rewritten to {target}, which is no longer a shipped tier default"
        )


def test_no_legacy_rewrite_key_is_still_a_shipped_tier_default() -> None:
    """The inverse, and the one that will break first when defaults rotate.

    If a retired id is brought back as a default, the migration would keep
    rewriting it away on every load -- the user's chosen model silently
    replaced by whatever the map points at.
    """
    resurrected = sorted(_shipped_tier_models() & set(_legacy_rewrites()))

    assert not resurrected, f"tier defaults that a migration would rewrite away: {resurrected}"
