"""Onboarding catalog for agentos-router tier profiles."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from agentos.gateway.config import (
    ROUTER_TIER_PROFILE_IDS,
    _router_tier_profile_defaults,
)
from agentos.router_tiers import DEFAULT_TEXT_TIER, TEXT_TIERS


@dataclass(frozen=True)
class RouterSetupProfile:
    profile_id: str
    provider_id: str
    label: str
    tiers: dict[str, dict[str, Any]]


_PROFILE_LABELS: dict[str, str] = {
    "openrouter": "OpenRouter mixed defaults",
    "dashscope": "Aliyun DashScope",
    "deepseek": "DeepSeek",
    "gemini": "Google Gemini",
    "volcengine": "Volcengine Ark",
    "openai": "OpenAI",
    "zhipu": "Zhipu",
    "moonshot": "Moonshot AI",
}


def _profile_to_setup(profile_id: str) -> RouterSetupProfile:
    normalized = profile_id.strip().lower()
    if normalized not in ROUTER_TIER_PROFILE_IDS:
        raise KeyError(f"unknown router profile: {profile_id!r}")
    raw_tiers = _router_tier_profile_defaults(normalized)
    exposed_tiers = {
        name: dict(value)
        for name, value in raw_tiers.items()
        if name in set(TEXT_TIERS) | {"image_model"}
    }
    return RouterSetupProfile(
        profile_id=normalized,
        provider_id=normalized,
        label=_PROFILE_LABELS.get(normalized, normalized),
        tiers=exposed_tiers,
    )


def list_router_setup_profiles() -> list[RouterSetupProfile]:
    return [_profile_to_setup(pid) for pid in sorted(ROUTER_TIER_PROFILE_IDS)]


def get_router_setup_profile(profile_id: str) -> RouterSetupProfile:
    return _profile_to_setup(profile_id)


def _tier_payload(tier: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": tier.get("provider", ""),
        "model": tier.get("model", ""),
        "description": tier.get("description", ""),
        "thinkingLevel": tier.get("thinking_level", ""),
        "supportsImage": bool(tier.get("supports_image", False)),
    }


def judge_models_for_profile(profile: RouterSetupProfile) -> list[str]:
    """Distinct pickable judge models for a profile (its text-tier models)."""
    models: list[str] = []
    for tier_name in TEXT_TIERS:
        tier = profile.tiers.get(tier_name)
        if not isinstance(tier, dict):
            continue
        model = str(tier.get("model", "") or "").strip()
        if model and model not in models:
            models.append(model)
    return models


def _judge_profile_payload(profile: RouterSetupProfile) -> dict[str, Any]:
    from agentos.agentos_router.llm_judge import resolve_judge_target

    target = resolve_judge_target(
        SimpleNamespace(judge_model=None, judge_provider=None, tiers=profile.tiers),
        SimpleNamespace(provider=profile.provider_id),
    )
    return {
        "autoProvider": target[0] if target else None,
        "autoModel": target[1] if target else None,
        "models": judge_models_for_profile(profile),
    }


def _judge_catalog_payload() -> dict[str, Any]:
    return {
        "modes": [
            {
                "mode": "auto",
                "label": "Auto (recommended)",
                "description": (
                    "Judge follows the tier profile's cheapest text tier (c0 first); "
                    "persists nothing, so profile switches auto-update the judge."
                ),
            },
            {
                "mode": "manual",
                "label": "Manual model",
                "description": (
                    "Pin an explicit judge model from the configured provider's "
                    "model catalog."
                ),
            },
            {
                "mode": "local",
                "label": "Local endpoint",
                "description": (
                    "Point the judge at a local OpenAI-compatible endpoint "
                    "(Ollama / LM Studio / llama.cpp / vLLM): supply the base URL "
                    "and model name. No cloud credentials are needed and zero "
                    "bytes are added to the package."
                ),
            },
        ],
        "profiles": {
            profile.profile_id: _judge_profile_payload(profile)
            for profile in list_router_setup_profiles()
        },
    }


def router_catalog_payload() -> dict[str, Any]:
    return {
        "judge": _judge_catalog_payload(),
        "defaultTier": DEFAULT_TEXT_TIER,
        "textTiers": list(TEXT_TIERS),
        "modes": [
            {
                "mode": "recommended",
                "label": "Recommended provider profile",
                "description": "Use the selected provider's default c0-c3 routing profile.",
            },
            {
                "mode": "openrouter-mix",
                "label": "OpenRouter mixed defaults",
                "description": "Keep the built-in OpenRouter mixed model routes.",
            },
            {
                "mode": "disabled",
                "label": "Disable router",
                "description": "Use the configured provider/model directly.",
            },
        ],
        "profiles": [
            {
                "profileId": profile.profile_id,
                "providerId": profile.provider_id,
                "label": profile.label,
                "tiers": {
                    name: _tier_payload(tier)
                    for name, tier in profile.tiers.items()
                },
            }
            for profile in list_router_setup_profiles()
        ],
    }
