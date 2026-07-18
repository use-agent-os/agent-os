"""Unified readiness doctor RPC."""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any, cast

from agentos.gateway.config import GatewayConfig
from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.gateway.rpc_channels import _handle_channels_status
from agentos.gateway.rpc_logs import _build_logs_status
from agentos.gateway.rpc_system import _handle_doctor_memory_status
from agentos.gateway.rpc_tools import _handle_providers_status, _handle_search_status
from agentos.health.evaluator import (
    evaluate_channels,
    evaluate_image_generation,
    evaluate_logs,
    evaluate_memory,
    evaluate_memory_embedding,
    evaluate_provider,
    evaluate_router,
    evaluate_sandbox,
    evaluate_search,
)
from agentos.health.model import FixStep, HealthFinding, HealthSeverity, build_report
from agentos.health.recovery_commands import command_with_config as _command_with_config
from agentos.sandbox.status import status_payload as _sandbox_status_payload
from agentos.session.keys import normalize_agent_id

_d = get_dispatcher()

Collector = Callable[[], dict[str, Any] | Awaitable[dict[str, Any]]]
Evaluator = Callable[[dict[str, Any]], list[HealthFinding]]

_COLLECTION_INSPECT_COMMANDS = {
    "provider": "agentos providers status --json",
    "logs": "agentos diagnostics status",
    "memory": "agentos memory status --deep --json",
    "channels": "agentos channels status --json",
    "sandbox": "agentos sandbox status --json",
    "router": "agentos diagnostics status",
    "memory_embedding": "agentos memory status --deep --json",
    "search": "agentos search status --json",
    "image_generation": "agentos onboard status --json",
}
_READINESS_CRITICAL_COLLECTIONS = {"provider"}
_UNKNOWN_SEARCH_PROVIDER_RE = re.compile(
    r"Unknown search provider ['\"]([^'\"]+)['\"]"
    r"|unknown search provider: ['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)


def _collection_error(surface: str, exc: Exception) -> HealthFinding:
    inspect_command = _COLLECTION_INSPECT_COMMANDS.get(surface)
    fix_steps = []
    if inspect_command:
        fix_steps.append(FixStep(label=f"Inspect {surface}", command=inspect_command))
    if inspect_command != "agentos diagnostics status":
        fix_steps.append(
            FixStep(label="Inspect diagnostics", command="agentos diagnostics status")
        )
    fix_steps.append(FixStep(label="Restart gateway", command="agentos gateway restart"))
    severity: HealthSeverity = (
        "error" if surface in _READINESS_CRITICAL_COLLECTIONS else "warn"
    )
    return HealthFinding(
        id=f"{surface}.diagnostic.unavailable",
        severity=severity,
        surface=surface,
        title=f"{surface.title()} diagnostics unavailable",
        detail=f"{type(exc).__name__}: {exc}",
        evidence={"errorType": type(exc).__name__},
        fix_steps=fix_steps,
        restart_required=True,
    )


def _config_path(ctx: RpcContext) -> str | None:
    config = getattr(ctx, "config", None)
    value = getattr(config, "config_path", None)
    return str(value) if value else None


def _with_config_recovery_steps(
    findings: list[HealthFinding],
    config_path: str | None,
) -> list[HealthFinding]:
    if not config_path:
        return findings
    adjusted: list[HealthFinding] = []
    for finding in findings:
        fix_steps = [
            replace(step, command=_command_with_config(step.command, config_path))
            if step.command
            else step
            for step in finding.fix_steps
        ]
        adjusted.append(replace(finding, fix_steps=fix_steps))
    return adjusted


def _unknown_search_provider(exc: Exception) -> str:
    message = str(exc)
    match = _UNKNOWN_SEARCH_PROVIDER_RE.search(message)
    if not match:
        return "unknown"
    return next(group for group in match.groups() if group) or "unknown"


def _search_api_key_env(ctx: RpcContext, payload: dict[str, Any]) -> str:
    config = getattr(ctx, "config", None)
    configured_env = str(getattr(config, "search_api_key_env", "") or "")
    if configured_env:
        return configured_env
    provider = str(payload.get("provider") or payload.get("activeProvider") or "")
    if not provider:
        return ""
    try:
        from agentos.search.registry import get_provider_spec

        return str(get_provider_spec(provider).env_key or "")
    except Exception:  # noqa: BLE001 - unknown search providers are reported separately.
        return ""


async def _search_payload(ctx: RpcContext) -> dict[str, Any]:
    try:
        payload = cast(dict[str, Any], await _handle_search_status({}, ctx))
        payload.setdefault("apiKeyEnv", _search_api_key_env(ctx, payload))
        return payload
    except (KeyError, ValueError) as exc:
        provider = _unknown_search_provider(exc)
        return {
            "activeProvider": provider,
            "provider": provider,
            "apiKeyEnv": "",
            "unknownProvider": True,
            "configured": False,
            "runtimeSupported": False,
            "requiresApiKey": False,
            "apiKeyConfigured": False,
            "buildable": False,
            "error": str(exc),
        }


def _sandbox_payload(ctx: RpcContext) -> dict[str, Any]:
    config = getattr(ctx, "config", None)
    if config is None:
        return {
            "posture": "unknown",
            "sandbox": {"sandbox": False, "security_grading": False},
            "permissions": {"default_mode": "unknown"},
            "restart_required": False,
        }
    return _sandbox_status_payload(config, restart_required=False)


def _image_generation_payload(ctx: RpcContext) -> dict[str, Any]:
    config = getattr(ctx, "config", None)
    if config is None:
        return {
            "enabled": False,
            "configured": False,
            "status": "optional",
            "provider": "",
            "primary": "",
            "source": "none",
            "apiKeyEnv": "",
            "configPath": None,
        }

    from agentos.onboarding.status import get_onboarding_status

    status = get_onboarding_status(config)
    section_status = status.sections.get("image_generation")
    status_value = getattr(section_status, "value", str(section_status or "unknown"))
    provider = status.image_generation_provider
    primary = status.image_generation_primary
    if not provider and "/" in primary:
        provider = primary.split("/", 1)[0]
    return {
        "enabled": status.image_generation_enabled,
        "configured": status.image_generation_configured,
        "status": status_value,
        "provider": status.image_generation_provider,
        "primary": primary,
        "source": status.image_generation_source,
        "apiKeyEnv": _image_generation_api_key_env(config, provider),
        "configPath": status.config_path,
    }


def _image_generation_api_key_env(config: Any, provider: str) -> str:
    if not provider:
        return ""
    provider_id = provider.strip().lower()
    try:
        from agentos.onboarding.image_generation_specs import (
            get_image_generation_provider_setup_spec,
        )

        spec = get_image_generation_provider_setup_spec(provider_id)
    except KeyError:
        return ""
    providers = getattr(getattr(config, "image_generation", None), "providers", None)
    provider_cfg = getattr(providers, provider_id, None) if providers is not None else None
    configured_env = str(getattr(provider_cfg, "api_key_env", "") or "")
    return configured_env or str(spec.env_key or "")


def _router_payload(ctx: RpcContext) -> dict[str, Any]:
    config = cast(GatewayConfig | None, getattr(ctx, "config", None))
    if config is None:
        return {
            "enabled": False,
            "rolloutPhase": "unknown",
            "strategy": "unknown",
            "tierProfile": "custom",
            "defaultTier": None,
            "runtimeValid": True,
        }

    router = config.agentos_router
    if router is None:
        return {
            "enabled": False,
            "rolloutPhase": "unknown",
            "strategy": "unknown",
            "tierProfile": "custom",
            "defaultTier": None,
            "runtimeValid": True,
        }

    runtime_valid = True
    error: str | None = None
    # Distinguishes WHY the router runtime is invalid so health/evaluator can
    # pick an accurate finding taxonomy (title/id/fix_steps). "assets" = missing
    # local runtime files; the judge-era reasons below have nothing to do with
    # files and must not surface under the asset-centric "missing assets" title.
    runtime_invalid_reason: str | None = None
    try:
        from agentos.gateway.boot import validate_agentos_router_runtime

        validate_agentos_router_runtime(config)
    except Exception as exc:  # noqa: BLE001 - doctor turns runtime validation into guidance.
        runtime_valid = False
        runtime_invalid_reason = "assets"
        error = str(exc)

    judge_provider: str | None = None
    judge_model: str | None = None
    judge_source: str | None = None
    judge_base_url: str | None = None
    strategy = getattr(router, "strategy", None)

    # Local ML routers (strategy="v4_phase3" default, or "pilot-v1") need no
    # judge and no cloud credentials — their health is purely local-asset
    # presence, which validate_agentos_router_runtime already checked above
    # (runtime_invalid_reason == "assets" when an asset is missing). Short-
    # circuit before the judge resolution block: resolve_judge_target ignores
    # strategy and would otherwise resolve a phantom judge target and run
    # credential checks that are meaningless for a local-asset strategy.
    from agentos.router_strategies import get_strategy_info

    _info = get_strategy_info(strategy)

    # A strategy that requires local assets degrades silently to the default
    # tier when its bundle is missing but require_router_runtime is unset (the
    # normal Phase-A state): validate_agentos_router_runtime above only warns,
    # so runtime_valid is still True here. Run the registry asset_probe to
    # surface that degradation as a WARNING (not "Router ready") — registry-
    # driven, so this covers pilot-v1 and v4_phase3 alike. A raise already
    # flipped runtime_valid=False with reason="assets"; only probe otherwise.
    if (
        _info is not None
        and _info.requires_local_assets
        and _info.asset_probe is not None
        and runtime_valid
    ):
        missing_assets = _info.asset_probe(router)
        if missing_assets:
            runtime_valid = False
            runtime_invalid_reason = "assets_degraded"
            default_tier = getattr(router, "default_tier", None) or "default"
            error = (
                f"The {strategy} router is selected but its local model bundle is "
                f"missing ({', '.join(missing_assets)}), so every request routes to "
                f"the {default_tier} tier. Set agentos_router.require_router_runtime = "
                "true to make this fatal at boot, or reinstall the router bundle to "
                "restore local routing."
            )

    if _info is not None and not _info.uses_judge:
        return {
            "enabled": bool(getattr(router, "enabled", False)),
            "rolloutPhase": getattr(router, "rollout_phase", None),
            "strategy": strategy,
            "tierProfile": getattr(router, "tier_profile", None),
            "defaultTier": getattr(router, "default_tier", None),
            "runtimeValid": runtime_valid,
            "runtimeInvalidReason": runtime_invalid_reason,
            "judgeProvider": None,
            "judgeModel": None,
            "judgeSource": None,
            "judgeBaseUrl": None,
            "error": error,
        }

    judge_resolved = True
    judge_no_credentials = False
    judge_base_url_ignored = False
    try:
        from agentos.agentos_router.llm_judge import (
            judge_provider_has_credentials,
            resolve_judge_target,
        )

        llm_cfg = getattr(config, "llm", None)
        target = resolve_judge_target(router, llm_cfg)
        if target is not None:
            judge_provider, judge_model, judge_source = target
            # A local-endpoint judge (source="local") reports its base_url; the
            # api key is never surfaced.
            if judge_source == "local":
                judge_base_url = (
                    str(getattr(router, "judge_base_url", "") or "").strip() or None
                )
            # A judge resolved to a provider different from llm.provider has no
            # credential source (tier entries carry no credentials), so every
            # turn degrades to judge_unavailable even though resolve_judge_target
            # returned a non-None target. This covers the AUTO-from-tier case a
            # hand-edited cross-provider tier profile reaches (findings #2/#4),
            # which the explicit-only cross-provider config reset never sees. A
            # local-endpoint judge carries its own credentials and is exempt.
            if not judge_provider_has_credentials(
                judge_provider or "", llm_cfg, judge_source
            ):
                judge_no_credentials = True
            # A configured local endpoint (judge_base_url) is only honored with
            # an EXPLICIT judge_model; in AUTO mode (judge_model unset) control
            # falls through to the tier scan and resolves a CLOUD tier target
            # with source="auto", silently discarding the operator's local
            # endpoint. Surface it so the misconfiguration is not hidden behind a
            # working cloud judge.
            if judge_source != "local" and str(
                getattr(router, "judge_base_url", "") or ""
            ).strip():
                judge_base_url_ignored = True
        elif strategy == "llm_judge" and bool(getattr(router, "enabled", False)):
            # An llm_judge router whose judge can never resolve degrades to
            # judge_unavailable on every turn; surface it via runtimeValid=False
            # so doctor/health report a finding instead of router.ready.
            judge_resolved = False
    except Exception:  # noqa: BLE001 - doctor never fails on judge resolution.
        pass

    if not judge_resolved and runtime_valid:
        runtime_valid = False
        runtime_invalid_reason = "judge_unresolvable"
        if not error:
            error = (
                "The LLM judge could not be resolved (no judge_model and no "
                "usable tier model); the router degrades to the default tier "
                "on every turn."
            )

    if (
        judge_no_credentials
        and strategy == "llm_judge"
        and bool(getattr(router, "enabled", False))
        and runtime_valid
    ):
        # The judge resolved to a provider with no credential source; report it
        # as unhealthy so doctor/health emit a finding instead of router.ready.
        runtime_valid = False
        runtime_invalid_reason = "judge_no_credentials"
        if not error:
            error = (
                f"The LLM judge resolved to provider {judge_provider!r}, which "
                "does not match llm.provider and has no credential source; the "
                "router degrades to the default tier on every turn. Set "
                "judge_provider (and judge_model) to match llm.provider, or "
                "align the router tier profile with it."
            )

    if (
        judge_base_url_ignored
        and strategy == "llm_judge"
        and bool(getattr(router, "enabled", False))
        and runtime_valid
    ):
        # judge_base_url is set but judge_model is unset (AUTO), so the local
        # endpoint is silently ignored and the judge runs against a cloud tier
        # target. Report it as unhealthy so doctor/health emit a finding instead
        # of router.ready, giving the operator a signal their local endpoint was
        # discarded.
        runtime_valid = False
        runtime_invalid_reason = "judge_base_url_requires_model"
        if not error:
            error = (
                "The router judge_base_url (local endpoint) is set but judge_model "
                "is unset (auto), so the local endpoint is ignored and the judge "
                "resolves to a cloud tier model. Set an explicit judge_model to use "
                "the local endpoint, or clear judge_base_url to route the judge on "
                "the cloud provider."
            )

    return {
        "enabled": bool(getattr(router, "enabled", False)),
        "rolloutPhase": getattr(router, "rollout_phase", None),
        "strategy": strategy,
        "tierProfile": getattr(router, "tier_profile", None),
        "defaultTier": getattr(router, "default_tier", None),
        "runtimeValid": runtime_valid,
        "runtimeInvalidReason": runtime_invalid_reason,
        "judgeProvider": judge_provider,
        "judgeModel": judge_model,
        "judgeSource": judge_source,
        "judgeBaseUrl": judge_base_url,
        "error": error,
    }


def _memory_embedding_payload(ctx: RpcContext) -> dict[str, Any]:
    config = getattr(ctx, "config", None)
    memory_config = getattr(config, "memory", None) if config is not None else None
    if memory_config is None:
        return {
            "status": "fts_only",
            "requestedProvider": "none",
            "effectiveProvider": "none",
            "model": "fts-only",
            "retrievalMode": "fts_only",
            "reason": "memory_unavailable",
        }

    embed_cfg = getattr(memory_config, "embedding", None)
    requested = str(getattr(embed_cfg, "requested_provider", "auto") or "auto")
    retrieval_mode = str(getattr(memory_config, "retrieval_mode", "hybrid") or "hybrid")
    try:
        from agentos.memory.embedding_resolver import resolve_memory_embedding

        decision = resolve_memory_embedding(memory_config)
    except Exception as exc:  # noqa: BLE001 - doctor reports config interpretation failures.
        return {
            "status": "error",
            "requestedProvider": requested,
            "effectiveProvider": "none",
            "model": "",
            "retrievalMode": retrieval_mode,
            "error": str(exc),
        }

    effective = str(decision.effective_provider)
    return {
        "status": "fts_only" if effective == "none" else "ready",
        "requestedProvider": decision.requested_provider,
        "effectiveProvider": effective,
        "model": decision.model,
        "retrievalMode": retrieval_mode,
        "reason": decision.reason,
    }


async def _evaluate_collection(
    surface: str,
    collect: Collector,
    evaluate: Evaluator,
) -> list[HealthFinding]:
    try:
        value = collect()
        payload = await value if inspect.isawaitable(value) else value
        return evaluate(payload)
    except Exception as exc:  # noqa: BLE001 - doctor reports partial diagnostic failures.
        return [_collection_error(surface, exc)]


@_d.method("doctor.status", scope="operator.read")
async def _handle_doctor_status(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")
    params = params or {}
    agent_id = normalize_agent_id(str(params.get("agentId") or "main"))
    deep = bool(params.get("deep", True))

    findings: list[HealthFinding] = [
        HealthFinding(
            id="gateway.rpc.ready",
            severity="ok",
            surface="gateway",
            title="Gateway RPC ready",
            detail="The gateway accepted and handled doctor.status.",
            evidence={"connId": ctx.conn_id},
        )
    ]

    collectors: list[tuple[str, Collector, Evaluator]] = [
        (
            "provider",
            lambda: _handle_providers_status({"probeModels": False}, ctx),
            evaluate_provider,
        ),
        ("logs", lambda: _build_logs_status(ctx), evaluate_logs),
        (
            "memory",
            lambda: _handle_doctor_memory_status({"agentId": agent_id, "deep": deep}, ctx),
            evaluate_memory,
        ),
        ("channels", lambda: _handle_channels_status({}, ctx), evaluate_channels),
        ("sandbox", lambda: _sandbox_payload(ctx), evaluate_sandbox),
        ("router", lambda: _router_payload(ctx), evaluate_router),
        (
            "memory_embedding",
            lambda: _memory_embedding_payload(ctx),
            evaluate_memory_embedding,
        ),
        ("search", lambda: _search_payload(ctx), evaluate_search),
        (
            "image_generation",
            lambda: _image_generation_payload(ctx),
            evaluate_image_generation,
        ),
    ]

    for surface, collect, evaluate in collectors:
        findings.extend(await _evaluate_collection(surface, collect, evaluate))

    config_path = _config_path(ctx)
    findings = _with_config_recovery_steps(findings, config_path)
    report = build_report(findings)
    report["agentId"] = agent_id
    if config_path:
        report["configPath"] = config_path
    return report
