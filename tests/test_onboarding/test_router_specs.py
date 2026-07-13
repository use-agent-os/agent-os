"""Tests for router onboarding catalog."""

from agentos.onboarding.router_specs import (
    get_router_setup_profile,
    router_catalog_payload,
)


def test_router_catalog_exposes_supported_profiles_and_tiers():
    payload = router_catalog_payload()

    profiles = {p["profileId"]: p for p in payload["profiles"]}
    assert {"openrouter", "deepseek", "openai"} <= set(profiles)
    deepseek = profiles["deepseek"]
    assert deepseek["providerId"] == "deepseek"
    assert set(deepseek["tiers"]) == {"c0", "c1", "c2", "c3"}
    assert deepseek["tiers"]["c0"]["model"]
    assert deepseek["tiers"]["c0"]["provider"] == "deepseek"
    assert "description" in deepseek["tiers"]["c0"]
    assert "thinkingLevel" in deepseek["tiers"]["c0"]
    openrouter = profiles["openrouter"]
    assert "image_model" in openrouter["tiers"]
    assert openrouter["tiers"]["image_model"]["supportsImage"] is True
    assert payload["defaultTier"] == "c1"
    assert set(payload["textTiers"]) == {"c0", "c1", "c2", "c3"}


def test_router_catalog_exposes_judge_block_with_auto_resolution_per_profile():
    payload = router_catalog_payload()

    judge = payload["judge"]
    assert {m["mode"] for m in judge["modes"]} == {"auto", "manual", "local"}
    auto_mode = next(m for m in judge["modes"] if m["mode"] == "auto")
    assert "recommended" in auto_mode["label"].lower()
    local_mode = next(m for m in judge["modes"] if m["mode"] == "local")
    assert "local" in local_mode["label"].lower()

    profiles = judge["profiles"]
    deepseek = profiles["deepseek"]
    # AUTO resolves to the profile's c0 model; pickable models are the
    # distinct text-tier models of the profile.
    assert deepseek["autoProvider"] == "deepseek"
    assert deepseek["autoModel"] == "deepseek-v4-flash"
    assert deepseek["autoModel"] in deepseek["models"]
    assert len(deepseek["models"]) == len(set(deepseek["models"]))

    catalog_profiles = {p["profileId"] for p in payload["profiles"]}
    assert set(profiles) == catalog_profiles
    for entry in profiles.values():
        assert entry["autoModel"]
        assert entry["models"]


def test_get_router_setup_profile_rejects_unknown_profile():
    try:
        get_router_setup_profile("does-not-exist")
    except KeyError as exc:
        assert "unknown router profile" in str(exc)
    else:
        raise AssertionError("expected unknown router profile to fail")
