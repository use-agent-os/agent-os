"""Mutations for provider/channel onboarding configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from pydantic import ValidationError

from agentos.channels.registry import discover_all, parse_channel_entry
from agentos.gateway.config import (
    ROUTER_TIER_PROFILE_IDS,
    AgentOSRouterConfig,
    ChannelsConfig,
    GatewayConfig,
    LlmProviderConfig,
    MemoryEmbeddingConfig,
    _bankr_tiers,
    _openrouter_tiers,
    _router_tier_profile_defaults,
)
from agentos.onboarding.audio_specs import get_audio_provider_setup_spec
from agentos.onboarding.image_generation_specs import (
    get_image_generation_provider_setup_spec,
)
from agentos.onboarding.provider_specs import get_provider_setup_spec
from agentos.onboarding.redaction import (
    redact_audio_payload,
    redact_channel_entry,
    redact_image_generation_payload,
    redact_memory_embedding_payload,
    redact_provider_payload,
    redact_search_payload,
)
from agentos.onboarding.search_specs import get_search_provider_setup_spec
from agentos.provider.registry import is_local_provider
from agentos.router_tiers import (
    DEFAULT_TEXT_TIER,
    TEXT_TIERS,
    normalize_text_tier,
)
from agentos.secrets import clean_header_secret

SearchFallbackPolicy = Literal["off", "network"]
RouterMode = Literal["recommended", "openrouter-mix", "disabled"]
_TEXT_ROUTER_TIERS = TEXT_TIERS
_ROUTER_TIER_KEYS = set(_TEXT_ROUTER_TIERS) | {"image_model"}
_TIER_KEY_ALIASES = {
    "thinkingLevel": "thinking_level",
    "supportsImage": "supports_image",
    "imageOnly": "image_only",
}
_REMOTE_MEMORY_EMBEDDING_PROVIDERS = {"openai", "openai-compatible"}
_DEFAULT_REMOTE_EMBEDDING_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_OLLAMA_EMBEDDING_BASE_URL = "http://localhost:11434"


@dataclass(frozen=True)
class MutationResult:
    config: GatewayConfig
    changed: bool
    restart_required: bool
    warnings: list[str] = field(default_factory=list)
    public_payload: dict[str, Any] = field(default_factory=dict)


def _clone(cfg: GatewayConfig) -> GatewayConfig:
    new_cfg = cfg.model_copy(deep=True)
    new_cfg.inherit_runtime_secrets(cfg)
    return new_cfg


def _clean_optional_str(value: str | None) -> str:
    if value is None:
        return ""
    return value.strip()


def _positive_int(value: int | str, *, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be an integer >= 1") from None
    if parsed < 1:
        raise ValueError(f"{label} must be >= 1")
    return parsed


# Only the routing-relevant flags survive a local-tier rewrite. thinking_level
# and the image flags MUST be preserved so thinking tiering and image-aware
# routing keep working; the free-text description is dropped so the rewritten
# tiers stay minimal and round-trip cleanly.
_LOCAL_TIER_PRESERVED_KEYS = ("thinking_level", "supports_image", "image_only")


def _local_provider_tiers(
    base_tiers: dict[str, Any],
    provider_id: str,
    model: str,
) -> dict[str, Any]:
    """Rewrite every base tier to point at a local provider's single model.

    Local providers run one endpoint serving only locally-pulled models, so the
    router cannot fan out across per-tier cloud model ids. Pin all tiers
    (c0..c3 and image_model) to ``provider_id``/``model`` while preserving each
    tier's routing flags (thinking_level, supports_image, image_only).
    """
    rewritten: dict[str, Any] = {}
    for name, tier in base_tiers.items():
        entry: dict[str, Any] = {"provider": provider_id, "model": model}
        for key in _LOCAL_TIER_PRESERVED_KEYS:
            if key in tier:
                entry[key] = tier[key]
        rewritten[name] = entry
    return rewritten


def _tiers_are_machine_written_defaults(
    tiers: dict[str, Any],
    old_provider: str,
    old_model: str,
) -> bool:
    """True when ``tiers`` are safe to rewrite (not operator-customised).

    Two shapes count as machine-written:
      * the shipped default tier sets (openrouter or bankr), matched exactly,
        exactly as :meth:`GatewayConfig._default_agentos_router_profile_for_direct_provider`
        detects "not custom"; and
      * tiers this reconcile already local-pinned — every entry's provider equals
        the OLD llm provider AND every entry's model equals the OLD llm model
        (a local→local switch, e.g. ollama→vllm, must re-pin these).
    Anything else is treated as a custom, operator-authored tier set and left
    untouched.
    """
    if tiers in (_openrouter_tiers(), _bankr_tiers()):
        return True
    old_provider = str(old_provider or "").strip().lower()
    old_model = str(old_model or "").strip()
    if not old_provider or not old_model or not is_local_provider(old_provider):
        return False
    if not tiers:
        return False
    for tier in tiers.values():
        if not isinstance(tier, dict):
            return False
        if str(tier.get("provider") or "").strip().lower() != old_provider:
            return False
        if str(tier.get("model") or "").strip() != old_model:
            return False
    return True


def _reconcile_router_profile_for_provider(
    cfg: GatewayConfig,
    provider_id: str,
    *,
    model: str = "",
    old_provider: str = "",
    old_model: str = "",
) -> list[str]:
    current_profile = getattr(cfg.agentos_router, "tier_profile", None)
    if not getattr(cfg.agentos_router, "enabled", True):
        return []
    if current_profile and str(current_profile).strip().lower() == provider_id:
        return []
    if is_local_provider(provider_id) and not current_profile:
        # Local providers have no tier profile and build no per-tier client.
        # When the current tiers are the untouched shipped defaults OR tiers a
        # previous reconcile local-pinned, rewrite them to this provider+model so
        # the persisted config is self-consistent (the runtime degrade guard then
        # becomes a no-op).
        if _tiers_are_machine_written_defaults(
            cfg.agentos_router.tiers, old_provider, old_model
        ):
            router_payload = cfg.agentos_router.model_dump(mode="python")
            router_payload["tier_profile"] = None
            router_payload["tiers"] = _local_provider_tiers(
                cfg.agentos_router.tiers, provider_id, model
            )
            cfg.agentos_router = AgentOSRouterConfig(**router_payload)
            return []
        # Operator-customised tiers: leave the router exactly as the operator
        # authored it (enabled + custom tiers). The runtime degrade guard pins
        # any mismatched-provider tier to llm.model per turn, so custom local
        # tiers stay safe without being clobbered here.
        return []
    if (
        not current_profile
        and provider_id == "openrouter"
        and cfg.agentos_router.tiers.get("c0", {}).get("provider") == "openrouter"
    ):
        return []
    router_payload = cfg.agentos_router.model_dump(mode="python")
    router_payload.pop("tiers", None)
    if provider_id in ROUTER_TIER_PROFILE_IDS:
        router_payload["tier_profile"] = provider_id
    else:
        router_payload["enabled"] = False
        router_payload["tier_profile"] = None
    # A provider switch carries the OLD judge_model/judge_provider through
    # verbatim. An explicitly-pinned judge on the previous provider (e.g.
    # judge_provider='openrouter') no longer matches the new llm.provider and has
    # no credential source, so every judged turn would silently degrade to
    # judge_unavailable. Run the judge reconciliation in non-explicit mode so a
    # now-stale cross-provider judge resets to AUTO (judge_model=None keeps the
    # persisted-value path a no-op for the common AUTO / already-matching case),
    # and surface the reset as a warning at the step that introduced it.
    warnings: list[str] = []
    if router_payload.get("enabled"):
        warnings = _apply_router_judge_fields(
            router_payload,
            llm_provider=provider_id,
            judge_model=None,
            judge_provider=None,
        )
    cfg.agentos_router = AgentOSRouterConfig(**router_payload)
    return warnings


def _default_text_tier(default_tier: str | None) -> str:
    tier = normalize_text_tier(default_tier or DEFAULT_TEXT_TIER)
    return tier if tier in _TEXT_ROUTER_TIERS else DEFAULT_TEXT_TIER


def _normalize_explicit_text_tier(default_tier: str | None) -> str | None:
    if default_tier is None:
        return None
    if not str(default_tier).strip():
        return None
    tier = normalize_text_tier(default_tier)
    if not tier:
        raise ValueError("defaultTier must reference a text tier")
    if tier not in _TEXT_ROUTER_TIERS:
        raise ValueError("defaultTier must reference a text tier")
    return tier


def _router_default_model_for_provider(provider_id: str, default_tier: str | None) -> str:
    if provider_id not in ROUTER_TIER_PROFILE_IDS:
        return ""
    tiers = _router_tier_profile_defaults(provider_id)
    tier = tiers.get(_default_text_tier(default_tier)) or tiers.get("c1") or {}
    return str(tier.get("model") or "").strip()


def _normalize_tier_payload(name: str, payload: Any) -> dict[str, Any]:
    if name not in _ROUTER_TIER_KEYS:
        raise ValueError(f"unknown router tier {name!r}")
    if not isinstance(payload, dict):
        raise ValueError(f"router tier {name!r} must be an object")
    out: dict[str, Any] = {}
    for key, value in payload.items():
        out[_TIER_KEY_ALIASES.get(str(key), str(key))] = value
    return out


def _enforce_router_tier_role_invariants(name: str, tier: dict[str, Any]) -> dict[str, Any]:
    if name != "image_model":
        return tier
    out = dict(tier)
    out["supports_image"] = True
    out["image_only"] = True
    return out


def _merge_router_tiers(
    base: dict[str, Any],
    overrides: dict[str, Any] | None,
) -> dict[str, Any]:
    merged = {name: dict(value) for name, value in base.items()}
    if not overrides:
        return merged
    if not isinstance(overrides, dict):
        raise ValueError("router tiers must be an object")
    for name, raw_override in overrides.items():
        tier_name = normalize_text_tier(name) or str(name)
        override = _normalize_tier_payload(tier_name, raw_override)
        current = dict(merged.get(tier_name, {}))
        current.update(override)
        merged[tier_name] = _enforce_router_tier_role_invariants(tier_name, current)
    return merged


def _validate_judge_base_url(base_url: str) -> None:
    """Validate the *shape* of a local judge endpoint URL.

    This only rejects a malformed URL so a persisted config always has a scheme
    + host. Connectivity — that the endpoint actually answers a classification —
    is a separate check (:func:`_verify_local_judge_endpoint`) run by the
    callers that collect a local endpoint from an operator (CLI ``flow.py`` and
    the WebUI/RPC ``upsert_router`` path via ``verify_local_endpoint=True``).
    """
    from urllib.parse import urlparse

    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(
            "judge_base_url must be an absolute http(s) URL "
            "(e.g. http://localhost:11434/v1)"
        )


def _verify_local_judge_endpoint(base_url: str, model: str, api_key: str) -> None:
    """Probe a local judge endpoint with one test classification (spec D2).

    Raises ``ValueError`` when the endpoint is unreachable or returns no usable
    routing decision, so a programmatic/RPC caller cannot persist a local judge
    that would degrade to ``judge_unavailable`` on every turn. Shares the exact
    probe used by the interactive CLI flow.
    """
    from agentos.agentos_router.llm_judge import probe_local_judge

    error = probe_local_judge(base_url, model, api_key or None)
    if error is not None:
        raise ValueError(
            f"local judge endpoint {base_url!r} is not usable: {error}"
        )


def _validate_router_tiers(tiers: dict[str, Any], default_tier: str) -> None:
    if default_tier not in _TEXT_ROUTER_TIERS:
        raise ValueError("defaultTier must reference a text tier")
    for tier_name in _TEXT_ROUTER_TIERS:
        tier = tiers.get(tier_name)
        if not isinstance(tier, dict):
            raise ValueError(f"router tier {tier_name!r} must be an object")
        if not str(tier.get("provider") or "").strip():
            raise ValueError(f"router tier {tier_name!r} requires provider")
        if not str(tier.get("model") or "").strip():
            raise ValueError(f"router tier {tier_name!r} requires model")


def _sync_llm_model_to_router_default(cfg: GatewayConfig) -> None:
    router = cfg.agentos_router
    if not getattr(router, "enabled", True):
        return
    default_tier = _default_text_tier(getattr(router, "default_tier", DEFAULT_TEXT_TIER))
    _validate_router_tiers(router.tiers, default_tier)
    tier = router.tiers[default_tier]
    model = str(tier.get("model") or "").strip()
    if model:
        cfg.llm.model = model


def upsert_llm_provider(
    config: GatewayConfig,
    *,
    provider_id: str,
    model: str = "",
    api_key: str = "",
    api_key_env: str = "",
    base_url: str = "",
    proxy: str = "",
    provider_routing: dict[str, str] | None = None,
) -> MutationResult:
    spec = get_provider_setup_spec(provider_id)
    if not spec.runtime_supported:
        raise ValueError(
            f"provider {provider_id!r} is not runtime-supported and cannot be configured"
        )
    model_clean = _clean_optional_str(model)
    if not model_clean:
        model_clean = _router_default_model_for_provider(
            provider_id,
            getattr(config.agentos_router, "default_tier", "c1"),
        )
    if not model_clean:
        raise ValueError("model is required")
    # When the operator omits an api_key while reconfiguring the same
    # provider that already has one stored, treat that as "leave key
    # unchanged" — matches the WebUI's "leave blank to keep current"
    # password-field affordance.
    effective_api_key = clean_header_secret(api_key, label="LLM API key")
    if api_key and api_key_env.strip():
        raise ValueError("configure either api_key or api_key_env, not both")
    effective_api_key_env = "" if api_key else api_key_env.strip()
    if not api_key and not effective_api_key_env and config.llm.provider == provider_id:
        effective_api_key_env = getattr(config.llm, "api_key_env", "").strip()
    if (
        not effective_api_key
        and spec.requires_api_key
        and not api_key_env
        and config.llm.provider == provider_id
        and config.llm.api_key
    ):
        effective_api_key = config.llm.api_key
    if spec.requires_api_key and not effective_api_key and not effective_api_key_env:
        raise ValueError(f"provider {provider_id!r} requires an api_key")
    effective_base_url = base_url or spec.default_base_url
    if spec.requires_base_url and not effective_base_url:
        raise ValueError(f"provider {provider_id!r} requires a base_url")

    old_provider = str(config.llm.provider or "")
    old_model = str(config.llm.model or "")
    new_cfg = _clone(config)
    new_cfg.llm = LlmProviderConfig(
        provider=provider_id,
        model=model_clean,
        api_key=effective_api_key,
        api_key_env=effective_api_key_env,
        base_url=effective_base_url,
        proxy=proxy,
        provider_routing=dict(provider_routing or {}),
    )
    reconcile_warnings = _reconcile_router_profile_for_provider(
        new_cfg,
        provider_id,
        model=model_clean,
        old_provider=old_provider,
        old_model=old_model,
    )
    if api_key:
        new_cfg.clear_runtime_secret("llm.api_key")

    payload = {
        "provider": provider_id,
        "model": model_clean,
        "api_key": effective_api_key,
        "api_key_env": effective_api_key_env,
        "api_key_source": (
            "explicit" if effective_api_key else ("env" if effective_api_key_env else "none")
        ),
        "base_url": effective_base_url,
        "proxy": proxy,
        "provider_routing": dict(provider_routing or {}),
    }
    return MutationResult(
        config=new_cfg,
        changed=True,
        restart_required=False,
        warnings=reconcile_warnings,
        public_payload=redact_provider_payload(payload),
    )


def _apply_router_judge_fields(
    router_payload: dict[str, Any],
    *,
    llm_provider: str,
    judge_model: str | None,
    judge_provider: str | None,
    judge_base_url: str | None = None,
    judge_api_key: str | None = None,
    verify_local_endpoint: bool = False,
) -> list[str]:
    """Apply and validate judge params on an enabled router payload.

    Returns warnings. An explicitly requested cross-provider judge is
    rejected; a *preserved* one left stale by a provider switch is reset to
    AUTO with a warning instead of failing router setup.

    A local OpenAI-compatible endpoint (``judge_base_url`` set with an explicit
    ``judge_model``) carries its own ``judge_api_key`` and bypasses the
    provider-match constraint — it is NOT rejected as cross-provider. When
    ``verify_local_endpoint`` is set (the onboarding surfaces that actually
    collect a local endpoint from an operator), a newly-set local endpoint is
    probed with one test classification call (spec D2 connectivity check) and a
    failure is raised — so a programmatic/RPC caller cannot silently persist an
    unreachable or wrong-model endpoint that would degrade to ``judge_unavailable``
    on every turn.
    """
    explicit = judge_model is not None
    base_url_clean = str(judge_base_url).strip() if judge_base_url is not None else None
    if explicit:
        judge_model_clean = str(judge_model).strip()
        if judge_model_clean.lower() in {"", "auto"}:
            # AUTO is strictly judge_model=None: persist nothing so later
            # tier-profile switches auto-update the judge. Clearing to AUTO also
            # drops any local-endpoint fields.
            router_payload["judge_model"] = None
            router_payload["judge_provider"] = None
            router_payload["judge_base_url"] = None
            router_payload["judge_api_key"] = None
        else:
            router_payload["judge_model"] = judge_model_clean
            if base_url_clean:
                # Local OpenAI-compatible endpoint: validate URL shape, persist
                # base_url + api_key, and clear judge_provider (the target is the
                # local endpoint, not a cloud provider).
                _validate_judge_base_url(base_url_clean)
                if verify_local_endpoint:
                    _verify_local_judge_endpoint(
                        base_url_clean,
                        judge_model_clean,
                        str(judge_api_key).strip() if judge_api_key is not None else "",
                    )
                router_payload["judge_base_url"] = base_url_clean
                router_payload["judge_provider"] = None
                if judge_api_key is not None:
                    router_payload["judge_api_key"] = str(judge_api_key).strip() or None
            else:
                if judge_provider is not None:
                    router_payload["judge_provider"] = str(judge_provider).strip() or None
                # An explicit cloud pick clears any prior local endpoint. Clear
                # unconditionally — callers that don't surface the local-endpoint
                # fields (e.g. the WebUI Router step) pass ``judge_base_url=None``,
                # and a stale persisted base_url would otherwise silently retarget
                # a cloud model id at the old local endpoint (→ judge_unavailable).
                router_payload["judge_base_url"] = None
                router_payload["judge_api_key"] = None

    # A local-endpoint judge carries its own credentials — skip the
    # provider-match rejection entirely.
    if str(router_payload.get("judge_base_url") or "").strip():
        return []

    effective_provider = str(router_payload.get("judge_provider") or "").strip()
    if (
        router_payload.get("judge_model")
        and effective_provider
        and effective_provider.lower() != llm_provider
    ):
        # Tier entries carry no credentials — the judge inherits llm.*
        # credentials only when its provider matches llm.provider, so a
        # cross-provider judge has no credential source.
        if explicit:
            raise ValueError(
                f"judge_provider {effective_provider!r} does not match llm.provider "
                f"{llm_provider!r} and has no credential source; configure the judge "
                "on the active LLM provider or leave it on auto"
            )
        router_payload["judge_model"] = None
        router_payload["judge_provider"] = None
        return [
            f"router judge_provider {effective_provider!r} no longer matches "
            f"llm.provider {llm_provider!r}; judge reset to auto"
        ]
    return []


def _router_judge_public_payload(config: GatewayConfig) -> dict[str, Any]:
    from agentos.agentos_router.llm_judge import resolve_judge_target

    router = config.agentos_router
    target = resolve_judge_target(router, config.llm)
    return {
        "judge_model": router.judge_model,
        "judge_provider": router.judge_provider,
        # Local endpoint base URL (never the api key). None for cloud/auto judges.
        "judge_base_url": getattr(router, "judge_base_url", None),
        "resolved_provider": target[0] if target else None,
        "resolved_model": target[1] if target else None,
        "source": target[2] if target else None,
    }


def upsert_router(
    config: GatewayConfig,
    *,
    mode: str = "recommended",
    strategy: str | None = None,
    default_tier: str | None = None,
    tiers: dict[str, Any] | None = None,
    judge_model: str | None = None,
    judge_provider: str | None = None,
    judge_base_url: str | None = None,
    judge_api_key: str | None = None,
    safety_net_threshold: float | None = None,
    verify_local_endpoint: bool = False,
) -> MutationResult:
    """Upsert router config.

    ``strategy`` selects the routing engine: ``"v4_phase3"`` (the local ML
    router, the default), ``"llm_judge"`` (classify each turn via a small LLM
    call), or ``"pilot-v1"`` (local ONNX+MiniLM router, English-optimized).
    Valid ids come from the router strategy registry
    (``agentos.router_strategies``). ``None`` preserves the persisted strategy;
    any unknown value raises.
    The strategy is recorded whenever it is provided — even when ``mode`` is
    ``"disabled"`` — so a later re-enable keeps the operator's choice.

    Judge params matter only under ``strategy="llm_judge"`` and are applied only
    when the router ends up enabled: ``judge_model=None`` preserves the persisted
    value; ``""`` or ``"auto"`` clears to AUTO (persists nothing, so later profile
    switches auto-update the judge); anything else pins an explicit judge model.
    They are harmless (ignored at routing time) when the strategy is
    ``v4_phase3``.

    ``safety_net_threshold`` sets ``[agentos_router.pilot].safety_net_threshold``
    (only meaningful under ``strategy="pilot-v1"``): ``None`` preserves the
    persisted value; any provided value is range-validated (0.0–1.0) by
    ``PilotConfig`` and persisted.

    ``verify_local_endpoint`` runs a one-shot connectivity probe (spec D2) when a
    new local ``judge_base_url`` is being set, raising if it is unreachable or
    returns no usable routing decision. Onboarding surfaces that collect a local
    endpoint from an operator (WebUI/RPC) pass ``True``; the CLI probes it itself
    before calling here.
    """
    if mode not in {"recommended", "openrouter-mix", "disabled"}:
        raise ValueError("router mode must be recommended, openrouter-mix, or disabled")
    router_mode = cast(RouterMode, mode)
    provider = str(config.llm.provider or "").strip().lower()
    router_payload = config.agentos_router.model_dump(mode="python")
    router_payload.pop("tiers", None)

    strategy_clean: str | None = None
    if strategy is not None:
        from agentos.router_strategies import is_known_strategy, known_strategy_ids

        strategy_clean = str(strategy).strip()
        if not is_known_strategy(strategy_clean):
            allowed = ", ".join(repr(s) for s in sorted(known_strategy_ids()))
            raise ValueError(
                f"agentos_router.strategy must be one of {allowed}; got {strategy!r}"
            )
        router_payload["strategy"] = strategy_clean

    # Pilot safety-net threshold ([agentos_router.pilot].safety_net_threshold).
    # Optional: ``None`` preserves the persisted value (carried through by the
    # ``model_dump`` above); any provided value is written into the pilot
    # sub-table and range-validated (0.0–1.0) by ``PilotConfig`` when the router
    # payload is reconstructed below.
    if safety_net_threshold is not None:
        pilot_payload = dict(router_payload.get("pilot") or {})
        pilot_payload["safety_net_threshold"] = safety_net_threshold
        router_payload["pilot"] = pilot_payload

    default_tier_override = _normalize_explicit_text_tier(default_tier)
    default_tier_clean = default_tier_override or str(
        normalize_text_tier(router_payload.get("default_tier")) or DEFAULT_TEXT_TIER
    )
    if default_tier_override is not None:
        router_payload["default_tier"] = default_tier_clean

    public_payload: dict[str, Any] = {"mode": router_mode}
    if strategy_clean is not None:
        public_payload["strategy"] = strategy_clean
    if router_mode == "disabled":
        router_payload["enabled"] = False
        router_payload["tier_profile"] = None
        if is_local_provider(provider):
            # Keep a local pin in the persisted config while the router is off:
            # dropping the tiers here would resurrect the openrouter defaults
            # via the config default factory, leaving a local config that lies
            # about its providers.
            current_tiers = {
                name: dict(tier)
                for name, tier in (config.agentos_router.tiers or {}).items()
                if isinstance(tier, dict)
            }
            if current_tiers:
                router_payload["tiers"] = current_tiers
        public_payload.update({"enabled": False, "tier_profile": None})
    elif router_mode == "openrouter-mix":
        if provider != "openrouter":
            raise ValueError("openrouter-mix router mode is only valid for openrouter LLM provider")
        router_payload["enabled"] = True
        router_payload["tier_profile"] = None
        router_payload["tiers"] = _merge_router_tiers(
            _router_tier_profile_defaults("openrouter"),
            tiers,
        )
        public_payload.update({"enabled": True, "tier_profile": None})
    else:
        llm_model = str(config.llm.model or "").strip()
        if provider in ROUTER_TIER_PROFILE_IDS:
            router_payload["enabled"] = True
            router_payload["tier_profile"] = provider
            router_payload["tiers"] = _merge_router_tiers(
                _router_tier_profile_defaults(provider),
                tiers,
            )
            public_payload.update({"enabled": True, "tier_profile": provider})
        elif is_local_provider(provider) and llm_model:
            # Local providers have no tier profile, but "recommended" must not
            # disable the router and resurrect the openrouter default tiers
            # (clobbering the local pin the provider step just wrote). Keep an
            # operator-customised table verbatim; otherwise pin every tier to
            # the configured local model.
            router_payload["enabled"] = True
            router_payload["tier_profile"] = None
            current_tiers = {
                name: dict(tier)
                for name, tier in (config.agentos_router.tiers or {}).items()
                if isinstance(tier, dict)
            }
            if current_tiers and not _tiers_are_machine_written_defaults(
                current_tiers, provider, llm_model
            ):
                router_payload["tiers"] = _merge_router_tiers(current_tiers, tiers)
            else:
                router_payload["tiers"] = _merge_router_tiers(
                    _local_provider_tiers(
                        _router_tier_profile_defaults("openrouter"), provider, llm_model
                    ),
                    tiers,
                )
            public_payload.update({"enabled": True, "tier_profile": None})
        else:
            router_payload["enabled"] = False
            router_payload["tier_profile"] = None
            public_payload.update({"enabled": False, "tier_profile": None})
    warnings: list[str] = []
    if router_payload.get("enabled"):
        warnings.extend(
            _apply_router_judge_fields(
                router_payload,
                llm_provider=provider,
                judge_model=judge_model,
                judge_provider=judge_provider,
                judge_base_url=judge_base_url,
                judge_api_key=judge_api_key,
                verify_local_endpoint=verify_local_endpoint,
            )
        )
        _validate_router_tiers(
            cast(dict[str, Any], router_payload.get("tiers") or {}),
            default_tier_clean,
        )

    new_cfg = _clone(config)
    new_cfg.agentos_router = AgentOSRouterConfig(**router_payload)
    _sync_llm_model_to_router_default(new_cfg)
    public_payload["default_tier"] = new_cfg.agentos_router.default_tier
    public_payload["tiers"] = new_cfg.agentos_router.tiers
    public_payload["pilot"] = new_cfg.agentos_router.pilot.model_dump(mode="python")
    if new_cfg.agentos_router.enabled:
        public_payload["judge"] = _router_judge_public_payload(new_cfg)
    return MutationResult(
        config=new_cfg,
        changed=True,
        restart_required=False,
        warnings=warnings,
        public_payload=public_payload,
    )


def upsert_search_provider(
    config: GatewayConfig,
    *,
    provider_id: str,
    api_key: str = "",
    api_key_env: str = "",
    max_results: int | str = 5,
    proxy: str = "",
    use_env_proxy: bool = False,
    fallback_policy: str = "off",
    diagnostics: bool = False,
) -> MutationResult:
    spec = get_search_provider_setup_spec(provider_id)
    if not spec.runtime_supported:
        raise ValueError(
            f"search provider {provider_id!r} is not runtime-supported and cannot be configured"
        )
    effective_max_results = _positive_int(max_results, label="max_results")
    if fallback_policy not in {"off", "network"}:
        raise ValueError("fallback_policy must be 'off' or 'network'")
    fallback_policy_value = cast(SearchFallbackPolicy, fallback_policy)

    effective_api_key = (
        clean_header_secret(api_key, label="Search API key")
        if spec.requires_api_key
        else ""
    )
    effective_api_key_env = (
        ""
        if api_key or not spec.requires_api_key
        else api_key_env.strip()
    )
    if (
        not effective_api_key
        and not effective_api_key_env
        and spec.requires_api_key
        and config.search_provider == provider_id
        and config.search_api_key
    ):
        effective_api_key = config.search_api_key
    if spec.requires_api_key and not effective_api_key and not effective_api_key_env:
        raise ValueError(f"search provider {provider_id!r} requires an api_key")

    new_cfg = _clone(config)
    new_cfg.search_provider = provider_id
    new_cfg.search_api_key = effective_api_key
    new_cfg.search_api_key_env = effective_api_key_env
    new_cfg.search_max_results = effective_max_results
    new_cfg.search_proxy = proxy
    new_cfg.search_use_env_proxy = bool(use_env_proxy)
    new_cfg.search_fallback_policy = fallback_policy_value
    new_cfg.search_diagnostics = bool(diagnostics)
    if api_key:
        new_cfg.clear_runtime_secret("search_api_key")

    api_key_source = (
        "explicit" if effective_api_key else ("env" if effective_api_key_env else "none")
    )
    payload = {
        "provider": provider_id,
        "api_key": effective_api_key,
        "api_key_env": effective_api_key_env,
        "api_key_source": api_key_source,
        "max_results": effective_max_results,
        "proxy": proxy,
        "use_env_proxy": bool(use_env_proxy),
        "fallback_policy": fallback_policy_value,
        "diagnostics": bool(diagnostics),
    }
    return MutationResult(
        config=new_cfg,
        changed=True,
        restart_required=False,
        warnings=[],
        public_payload=redact_search_payload(payload),
    )


def _image_generation_provider_config(config: GatewayConfig, provider_id: str) -> Any:
    providers = config.image_generation.providers
    provider_config = getattr(providers, provider_id, None)
    if provider_config is None:
        raise KeyError(f"unknown image generation provider: {provider_id!r}")
    return provider_config


def _image_generation_api_key_source(
    config: GatewayConfig,
    *,
    provider_id: str,
    api_key: str,
    env_key: str,
) -> str:
    if api_key:
        return "explicit"
    if env_key and os.environ.get(env_key):
        return "env"
    if config.llm.provider == provider_id and config.llm.api_key:
        return "llm_fallback"
    return "none"


def upsert_image_generation_provider(
    config: GatewayConfig,
    *,
    provider_id: str,
    primary: str = "",
    api_key: str = "",
    api_key_env: str = "",
    base_url: str = "",
    enabled: bool = True,
) -> MutationResult:
    spec = get_image_generation_provider_setup_spec(provider_id)
    if not spec.runtime_supported:
        raise ValueError(
            f"image generation provider {provider_id!r} is not runtime-supported "
            "and cannot be configured"
        )
    primary_model = primary or spec.default_model
    primary_provider, sep, _model = primary_model.partition("/")
    if not sep or primary_provider != provider_id:
        raise ValueError(
            "primary must be a provider/model reference for "
            f"image generation provider {provider_id!r}"
    )

    current_provider_cfg = _image_generation_provider_config(config, provider_id)
    explicit_env_key = _clean_optional_str(api_key_env)
    if api_key and explicit_env_key:
        raise ValueError("configure either api_key or api_key_env, not both")
    effective_api_key = clean_header_secret(
        api_key or getattr(current_provider_cfg, "api_key", ""),
        label="Image API key",
    )
    current_env_key = getattr(current_provider_cfg, "api_key_env", spec.env_key) or ""
    if api_key:
        env_key = ""
    else:
        env_key = explicit_env_key or current_env_key or spec.env_key
    has_saved_env_reference = bool(
        explicit_env_key or (current_env_key and current_env_key != spec.env_key)
    )
    api_key_source = _image_generation_api_key_source(
        config,
        provider_id=provider_id,
        api_key=effective_api_key,
        env_key=env_key,
    )
    if (
        enabled
        and spec.requires_api_key
        and api_key_source == "none"
        and not has_saved_env_reference
    ):
        raise ValueError(
            f"image generation provider {provider_id!r} requires an api_key, "
            f"{spec.env_key}, or a matching configured LLM provider"
        )
    if api_key_source == "none" and has_saved_env_reference:
        api_key_source = "missing_env"

    effective_base_url = (
        base_url or getattr(current_provider_cfg, "base_url", "") or spec.default_base_url
    )

    new_cfg = _clone(config)
    new_cfg.image_generation.enabled = bool(enabled)
    new_cfg.image_generation.primary = primary_model
    next_provider_cfg = _image_generation_provider_config(new_cfg, provider_id)
    next_provider_cfg.api_key = effective_api_key
    next_provider_cfg.api_key_env = env_key
    next_provider_cfg.base_url = effective_base_url
    if api_key:
        new_cfg.clear_runtime_secret(f"image_generation.providers.{provider_id}.api_key")

    payload = {
        "provider": provider_id,
        "enabled": bool(enabled),
        "primary": primary_model,
        "api_key": effective_api_key,
        "api_key_env": env_key,
        "api_key_source": api_key_source,
        "base_url": effective_base_url,
    }
    return MutationResult(
        config=new_cfg,
        changed=True,
        restart_required=False,
        warnings=[],
        public_payload=redact_image_generation_payload(payload),
    )


def disable_image_generation(config: GatewayConfig) -> MutationResult:
    new_cfg = _clone(config)
    new_cfg.image_generation.enabled = False
    return MutationResult(
        config=new_cfg,
        changed=True,
        restart_required=False,
        warnings=[],
        public_payload={
            "enabled": False,
            "primary": new_cfg.image_generation.primary,
        },
    )


def _audio_provider_config(config: GatewayConfig, provider_id: str) -> Any:
    providers = config.audio.providers
    provider_config = getattr(providers, provider_id, None)
    if provider_config is None:
        raise KeyError(f"unknown audio provider: {provider_id!r}")
    return provider_config


def _audio_api_key_source(*, api_key: str, env_key: str) -> str:
    if api_key:
        return "explicit"
    if env_key and os.environ.get(env_key):
        return "env"
    if env_key:
        return "missing_env"
    return "none"


def upsert_audio_provider(
    config: GatewayConfig,
    *,
    provider_id: str,
    api_key: str = "",
    api_key_env: str = "",
    base_url: str = "",
    enabled: bool = True,
    tts_voice: str = "",
    tts_model: str = "",
    language_code: str = "",
) -> MutationResult:
    spec = get_audio_provider_setup_spec(provider_id)
    if not spec.runtime_supported:
        raise ValueError(
            f"audio provider {provider_id!r} is not runtime-supported and cannot be configured"
        )
    if provider_id != "elevenlabs":
        raise ValueError(f"audio provider {provider_id!r} is not supported")

    current_provider_cfg = _audio_provider_config(config, provider_id)
    explicit_env_key = _clean_optional_str(api_key_env)
    if api_key and explicit_env_key:
        raise ValueError("configure either api_key or api_key_env, not both")
    effective_api_key = clean_header_secret(
        api_key or getattr(current_provider_cfg, "api_key", ""),
        label="Audio API key",
    )
    current_env_key = getattr(current_provider_cfg, "api_key_env", spec.env_key) or ""
    env_key = "" if api_key else (explicit_env_key or current_env_key or spec.env_key)
    api_key_source = _audio_api_key_source(
        api_key=effective_api_key,
        env_key=env_key,
    )
    if enabled and spec.requires_api_key and api_key_source == "none":
        raise ValueError(
            f"audio provider {provider_id!r} requires an api_key or {spec.env_key}"
        )

    effective_base_url = (
        base_url or getattr(current_provider_cfg, "base_url", "") or spec.default_base_url
    )
    effective_tts_voice = tts_voice or config.audio.tts.voice or spec.default_tts_voice
    effective_tts_model = tts_model or config.audio.tts.model or spec.default_tts_model
    effective_language_code = language_code or config.audio.tts.language_code

    new_cfg = _clone(config)
    new_cfg.audio.enabled = bool(enabled)
    next_provider_cfg = _audio_provider_config(new_cfg, provider_id)
    next_provider_cfg.api_key = effective_api_key
    next_provider_cfg.api_key_env = env_key
    next_provider_cfg.base_url = effective_base_url
    new_cfg.audio.tts.voice = effective_tts_voice
    new_cfg.audio.tts.model = effective_tts_model
    new_cfg.audio.tts.language_code = effective_language_code
    if api_key:
        new_cfg.clear_runtime_secret(f"audio.providers.{provider_id}.api_key")

    payload = {
        "provider": provider_id,
        "enabled": bool(enabled),
        "api_key": effective_api_key,
        "api_key_env": env_key,
        "api_key_source": api_key_source,
        "base_url": effective_base_url,
        "tts_voice": effective_tts_voice,
        "tts_model": effective_tts_model,
        "language_code": effective_language_code,
    }
    return MutationResult(
        config=new_cfg,
        changed=True,
        restart_required=False,
        warnings=[],
        public_payload=redact_audio_payload(payload),
    )


def upsert_memory_embedding(
    config: GatewayConfig,
    *,
    provider: str,
    model: str | None = None,
    api_key: str | None = None,
    api_key_env: str | None = None,
    base_url: str | None = None,
    onnx_dir: str | None = None,
) -> MutationResult:
    if provider not in {"auto", "none", "local", "openai", "openai-compatible", "ollama"}:
        raise ValueError(f"unknown memory embedding provider: {provider!r}")

    new_cfg = _clone(config)
    old_memory = config.memory.model_dump(mode="python")
    current = config.memory.embedding
    model_value = _clean_optional_str(model)
    api_key_value = _clean_optional_str(api_key)
    api_key_env_value = _clean_optional_str(api_key_env)
    if api_key_value and api_key_env_value:
        raise ValueError("configure either api_key or api_key_env, not both")
    base_url_value = _clean_optional_str(base_url)
    onnx_dir_value = _clean_optional_str(onnx_dir)
    payload: dict[str, Any] = {"provider": provider}

    if provider in _REMOTE_MEMORY_EMBEDDING_PROVIDERS:
        current_api_key_env = _clean_optional_str(
            getattr(current.remote, "api_key_env", None)
        )
        effective_api_key_env = "" if api_key_value else (
            api_key_env_value or current_api_key_env or ""
        )
        effective_api_key = (
            api_key_value
            or ("" if effective_api_key_env else current.remote.api_key or current.api_key or "")
        )
        if not effective_api_key and not effective_api_key_env:
            raise ValueError(
                "remote memory embedding provider requires an api_key or api_key_env"
            )
        payload["remote"] = {
            "base_url": (
                base_url_value
                or current.remote.base_url
                or current.base_url
                or _DEFAULT_REMOTE_EMBEDDING_BASE_URL
            ),
        }
        if effective_api_key:
            payload["remote"]["api_key"] = effective_api_key
        if effective_api_key_env:
            payload["remote"]["api_key_env"] = effective_api_key_env
        remote_model = model_value or current.remote.model or current.model
        if remote_model:
            payload["remote"]["model"] = remote_model
    elif provider == "auto":
        remote_payload: dict[str, str] = {}
        current_api_key_env = _clean_optional_str(
            getattr(current.remote, "api_key_env", None)
        )
        effective_api_key_env = "" if api_key_value else (
            api_key_env_value or current_api_key_env or ""
        )
        effective_api_key = (
            api_key_value
            or ("" if effective_api_key_env else current.remote.api_key or current.api_key or "")
        )
        if effective_api_key:
            remote_payload["api_key"] = effective_api_key
        if effective_api_key_env:
            remote_payload["api_key_env"] = effective_api_key_env
        remote_base_url = base_url_value or current.remote.base_url or current.base_url
        if remote_base_url:
            remote_payload["base_url"] = remote_base_url
        remote_model = model_value or current.remote.model or (
            current.model if (effective_api_key or effective_api_key_env) else None
        )
        if remote_model:
            remote_payload["model"] = remote_model
        if remote_payload:
            payload["remote"] = remote_payload
    elif provider == "local":
        payload["local"] = {}
        local_onnx_dir = onnx_dir_value or (
            current.local.onnx_dir if current.requested_provider == "local" else ""
        )
        if local_onnx_dir:
            payload["local"]["onnx_dir"] = local_onnx_dir
    elif provider == "ollama":
        payload["ollama"] = {
            "base_url": (
                base_url_value
                or current.ollama.base_url
                or _DEFAULT_OLLAMA_EMBEDDING_BASE_URL
            ),
        }
        ollama_model = model_value or current.ollama.model
        if ollama_model:
            payload["ollama"]["model"] = ollama_model

    new_cfg.memory.embedding = MemoryEmbeddingConfig.model_validate(payload)
    changed = old_memory != new_cfg.memory.model_dump(mode="python")
    if api_key_value or api_key_env_value:
        new_cfg.clear_runtime_secret("memory.embedding.remote.api_key")
        new_cfg.clear_runtime_secret("memory.embedding.api_key")

    return MutationResult(
        config=new_cfg,
        changed=changed,
        restart_required=changed,
        warnings=[],
        public_payload=redact_memory_embedding_payload(payload),
    )


def _channel_entries_as_dicts(cfg: GatewayConfig) -> list[dict[str, Any]]:
    return [e.model_dump(mode="python") for e in cfg.channels.channels]


def list_channel_entries(config: GatewayConfig) -> list[dict[str, Any]]:
    return [redact_channel_entry(d.get("type", ""), d) for d in _channel_entries_as_dicts(config)]


def validate_channel_entry(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TypeError("channel entry payload must be a dict")
    type_name = payload.get("type")
    if not isinstance(type_name, str) or not type_name:
        raise ValueError("channel entry requires non-empty 'type'")
    if type_name not in discover_all():
        raise ValueError(f"unknown channel type: {type_name!r}")
    full = {"agent_id": "main", "enabled": True, **payload}
    try:
        entry = parse_channel_entry(full)
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc
    if (
        type_name == "slack"
        and getattr(entry, "connection_mode", "webhook") == "webhook"
        and not str(getattr(entry, "signing_secret", "") or "").strip()
    ):
        raise ValueError("slack webhook channels require signing_secret")
    return entry.model_dump(mode="python")


def upsert_channel(
    config: GatewayConfig,
    *,
    entry_payload: dict[str, Any],
) -> MutationResult:
    merged = _merge_with_existing_secrets(config, entry_payload)
    normalized = validate_channel_entry(merged)
    name = normalized["name"]
    new_cfg = _clone(config)
    raw = _channel_entries_as_dicts(new_cfg)
    replaced = False
    for idx, existing in enumerate(raw):
        if existing.get("name") == name:
            raw[idx] = normalized
            replaced = True
            break
    if not replaced:
        raw.append(normalized)
    new_cfg.channels = ChannelsConfig.model_validate({"channels": raw})

    return MutationResult(
        config=new_cfg,
        changed=True,
        restart_required=True,
        warnings=[],
        public_payload=redact_channel_entry(normalized["type"], normalized),
    )


def _merge_with_existing_secrets(
    config: GatewayConfig, payload: dict[str, Any]
) -> dict[str, Any]:
    """Mirror upsert_llm_provider: blank secret in payload = keep current.

    Only secret fields are auto-preserved here so that re-adding an entry
    by name does not require re-typing credentials. Non-secret partial
    updates belong to the edit path, which seeds the full existing entry
    in the CLI before calling upsert.
    """
    from agentos.onboarding.channel_specs import get_channel_setup_spec

    type_name = payload.get("type")
    name = payload.get("name")
    if not isinstance(type_name, str) or not isinstance(name, str):
        return dict(payload)
    try:
        spec = get_channel_setup_spec(type_name)
    except KeyError:
        return dict(payload)
    existing = next(
        (
            e.model_dump(mode="python")
            for e in config.channels.channels
            if e.name == name and e.type == type_name
        ),
        None,
    )
    if existing is None:
        return dict(payload)
    merged = dict(payload)
    # The legacy/static Telegram DM allowlist is intentionally absent from the
    # onboarding form (new approvals live in the pairing store). Preserve it
    # when an operator later edits transport or credential fields.
    if type_name == "telegram" and "approved_sender_ids" not in merged:
        merged["approved_sender_ids"] = list(existing.get("approved_sender_ids") or [])
    for f in spec.fields:
        if not f.secret:
            continue
        if merged.get(f.name) in ("", None) and existing.get(f.name):
            merged[f.name] = existing[f.name]
    return merged


def remove_channel(
    config: GatewayConfig,
    *,
    name: str,
) -> MutationResult:
    new_cfg = _clone(config)
    raw = _channel_entries_as_dicts(new_cfg)
    remaining = [e for e in raw if e.get("name") != name]
    if len(remaining) == len(raw):
        raise KeyError(f"no channel named {name!r}")
    new_cfg.channels = ChannelsConfig.model_validate({"channels": remaining})
    return MutationResult(
        config=new_cfg,
        changed=True,
        restart_required=True,
        public_payload={"name": name, "removed": True},
    )


def set_channel_enabled(
    config: GatewayConfig,
    *,
    name: str,
    enabled: bool,
) -> MutationResult:
    new_cfg = _clone(config)
    raw = _channel_entries_as_dicts(new_cfg)
    found = False
    for entry in raw:
        if entry.get("name") == name:
            entry["enabled"] = bool(enabled)
            found = True
            break
    if not found:
        raise KeyError(f"no channel named {name!r}")
    new_cfg.channels = ChannelsConfig.model_validate({"channels": raw})
    return MutationResult(
        config=new_cfg,
        changed=True,
        restart_required=True,
        public_payload={"name": name, "enabled": bool(enabled)},
    )
