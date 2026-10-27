"""Unified readiness doctor RPC."""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any, cast

from agentos.gateway.config import GatewayConfig
from agentos.gateway.exposure import gateway_security_payload
from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.gateway.rpc_channels import _handle_channels_status
from agentos.gateway.rpc_logs import _build_logs_status
from agentos.gateway.rpc_system import _handle_doctor_memory_status
from agentos.gateway.rpc_tools import _handle_providers_status, _handle_search_status
from agentos.health.evaluator import (
    evaluate_browser,
    evaluate_channels,
    evaluate_control_ui,
    evaluate_image_generation,
    evaluate_logs,
    evaluate_memory,
    evaluate_memory_embedding,
    evaluate_provider,
    evaluate_router,
    evaluate_sandbox,
    evaluate_search,
)
from agentos.health.gateway_security import evaluate_gateway_security
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
        fix_steps.append(FixStep(label="Inspect diagnostics", command="agentos diagnostics status"))
    fix_steps.append(FixStep(label="Restart gateway", command="agentos gateway restart"))
    severity: HealthSeverity = "error" if surface in _READINESS_CRITICAL_COLLECTIONS else "warn"
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

    # Routing is single-provider: tiers only select the model, every request
    # goes through llm.provider. Surface each tier's declared provider so
    # evaluate_router can flag tiers pointing at a provider the runtime never
    # builds a client for. Tiers without a non-empty provider are omitted.
    llm_provider = str(getattr(getattr(config, "llm", None), "provider", "") or "")
    raw_tiers = getattr(router, "tiers", None)
    tier_providers: dict[str, str] = {}
    if isinstance(raw_tiers, dict):
        for tier_name, tier in raw_tiers.items():
            if not isinstance(tier, dict):
                continue
            tier_provider = str(tier.get("provider") or "").strip()
            if tier_provider:
                tier_providers[str(tier_name)] = tier_provider

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

    # Local ML routers (strategy="pilot-v1", the default) need no
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
    # driven, so it covers any local-asset strategy. A raise already
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

    credential_env: str | None = None
    if (
        _info is not None
        and _info.requires_remote_credentials
        and _info.credential_probe is not None
    ):
        # A remote-credential strategy (Jev): surface the env var name the key
        # is read from (never the key) and report a missing key as an invalid
        # runtime so doctor/health emit a finding instead of router.ready.
        sub_cfg = getattr(router, strategy, None) if isinstance(strategy, str) else None
        credential_env = str(getattr(sub_cfg, "api_key_env", "") or "").strip() or None
        if runtime_valid:
            problem = _info.credential_probe(router)
            if problem:
                runtime_valid = False
                runtime_invalid_reason = "credentials_missing"
                if not error:
                    error = (
                        f"The {strategy} router is selected but has no API key "
                        f"({problem}), so every request routes to the "
                        f"{getattr(router, 'default_tier', None) or 'default'} tier."
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
            "credentialEnv": credential_env,
            "llmProvider": llm_provider,
            "tierProviders": tier_providers,
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
                judge_base_url = str(getattr(router, "judge_base_url", "") or "").strip() or None
            # A judge resolved to a provider different from llm.provider has no
            # credential source (tier entries carry no credentials), so every
            # turn degrades to judge_unavailable even though resolve_judge_target
            # returned a non-None target. This covers the AUTO-from-tier case a
            # hand-edited cross-provider tier profile reaches (findings #2/#4),
            # which the explicit-only cross-provider config reset never sees. A
            # local-endpoint judge carries its own credentials and is exempt.
            if not judge_provider_has_credentials(judge_provider or "", llm_cfg, judge_source):
                judge_no_credentials = True
            # A configured local endpoint (judge_base_url) is only honored with
            # an EXPLICIT judge_model; in AUTO mode (judge_model unset) control
            # falls through to the tier scan and resolves a CLOUD tier target
            # with source="auto", silently discarding the operator's local
            # endpoint. Surface it so the misconfiguration is not hidden behind a
            # working cloud judge.
            if judge_source != "local" and str(getattr(router, "judge_base_url", "") or "").strip():
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
        "llmProvider": llm_provider,
        "tierProviders": tier_providers,
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


def _browser_payload(ctx: RpcContext) -> dict[str, Any]:
    # Reads the adapter's live state (already configured at boot from the same
    # config); side-effect-free so the doctor probe never mutates runtime state.
    del ctx
    try:
        from agentos.tools.agent_browser import browser_doctor_status

        return browser_doctor_status()
    except Exception:  # noqa: BLE001 - a doctor probe must never raise
        return {"enabled": False, "binaryPresent": False, "binaryPath": "", "attachMode": False}


def _control_ui_payload(ctx: RpcContext) -> dict[str, Any]:
    from agentos.gateway.control_ui import _DIST_DIR
    from agentos.health.control_ui import inspect_control_ui_bundle

    config = getattr(ctx, "config", None)
    if config is not None and not config.control_ui.enabled:
        # Boot skips the check entirely when the Control UI is disabled; there
        # is no point telling an operator a bundle nobody serves is out of date.
        return {"stale": None, "sourceMtime": None, "bundleMtime": None, "wheelInstall": False}
    report = inspect_control_ui_bundle(_DIST_DIR / "index.html")
    return {
        "stale": report.stale,
        "sourceMtime": report.source_mtime,
        "bundleMtime": report.bundle_mtime,
        "wheelInstall": report.wheel_install,
    }


def _gateway_security_payload(ctx: RpcContext) -> dict[str, Any]:
    config = getattr(ctx, "config", None)
    if config is None:
        raise RuntimeError("gateway config is not available")
    return gateway_security_payload(config)


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


def _gateway_log_hint() -> str:
    from agentos.paths import default_agentos_home

    return str(default_agentos_home() / "logs" / "debug.log")


def _safe_mode_findings() -> list[HealthFinding]:
    """The crash-loop breaker, when it holds: what is paused and the way out."""
    from agentos.gateway.boot_lifecycle import get_boot_tracker

    tracker = get_boot_tracker()
    if tracker is None or not tracker.safe_mode_active:
        return []
    snapshot = tracker.snapshot()
    clears_at = snapshot.get("clears_at")
    return [
        HealthFinding(
            id="gateway.safe_mode",
            severity="warn",
            readiness_impact="degrades",
            surface="gateway",
            title="Gateway is in safe mode",
            detail=(
                f"{snapshot.get('reason') or 'Repeated unclean boots'}. Channels, scheduled "
                "jobs and heartbeats are held back so a crash at startup cannot loop; the "
                "control plane works as usual. Safe mode clears by itself"
                + (f" at {clears_at}" if clears_at else "")
                + " if no further unclean boot happens."
            ),
            evidence=snapshot,
            fix_steps=[
                FixStep(
                    label="Find the crash in the gateway log",
                    detail=(
                        f"The last lines of the crashed runs are in {_gateway_log_hint()}"
                        " (the desktop app and the installed service also write to"
                        " ~/Library/Logs/AgentOS/ on macOS)."
                    ),
                ),
                FixStep(
                    label="Start one channel to test it",
                    command="agentos channels start NAME --yes",
                    detail="Starts just that channel; safe mode stays on for the rest.",
                ),
                FixStep(
                    label="Leave safe mode now",
                    command="agentos channels start --yes",
                ),
            ],
        )
    ]


_NOT_RESUMED_CAUSES = {
    "restart_requested": "the turn asked for the restart itself",
    "attempts_exhausted": "resume attempts used up",
    "max_age": "older than 24 hours",
    "route_unknown": "the chat to answer is not recorded",
}


def _not_resumed_causes(recoveries: list[dict[str, Any]], snapshot: dict[str, Any]) -> str:
    """Why the not-resumed turns were not resumed, from their recorded reasons."""
    causes: list[str] = []
    for item in recoveries:
        state, reason = item.get("state"), str(item.get("reason") or "")
        if state not in ("tombstoned", "reported", "expired"):
            continue
        if reason in _NOT_RESUMED_CAUSES:
            cause = _NOT_RESUMED_CAUSES[reason]
        elif state == "reported":
            cause = (
                "automatic resume is off"
                if not snapshot.get("auto_resume")
                else "the gateway was in safe mode"
            )
        else:
            cause = reason.replace("_", " ") or "unknown"
        if cause not in causes:
            causes.append(cause)
    return "; ".join(causes) or "unknown"


def _turn_recovery_findings() -> list[HealthFinding]:
    """Turns a restart or crash interrupted: being resumed, or left for the user."""
    from agentos.gateway.turn_recovery import get_turn_recovery

    recovery = get_turn_recovery()
    if recovery is None:
        return []
    snapshot = recovery.snapshot()
    if not snapshot.get("found"):
        return []
    sessions = [item for item in recovery.recoveries() if item.get("state") != "closed"][:20]
    evidence = {**snapshot, "sessions": sessions}
    not_resumed = sum(int(snapshot.get(key) or 0) for key in ("tombstoned", "reported", "expired"))
    in_progress = int(snapshot.get("pending") or 0) + int(snapshot.get("running") or 0)
    findings: list[HealthFinding] = []
    if not_resumed:
        findings.append(
            HealthFinding(
                id="gateway.turn_recovery.not_resumed",
                severity="warn",
                readiness_impact="none",
                surface="gateway",
                title=f"{not_resumed} interrupted turn(s) were not resumed",
                detail=(
                    "A gateway restart or crash stopped these turns and they were not "
                    "continued automatically ("
                    + _not_resumed_causes(recovery.recoveries(), snapshot)
                    + "). Each session has a note; a new message there continues it."
                ),
                evidence=evidence,
                fix_steps=[
                    FixStep(
                        label="Continue the session",
                        detail="Open the session and send a message. Check any tool "
                        "result marked unknown before repeating that action.",
                    ),
                    *(
                        []
                        if snapshot.get("auto_resume")
                        else [
                            FixStep(
                                label="Resume interrupted turns automatically",
                                command=(
                                    "agentos config set task_runtime.resume_interrupted_turns true"
                                ),
                            )
                        ]
                    ),
                ],
            )
        )
    if in_progress:
        findings.append(
            HealthFinding(
                id="gateway.turn_recovery.in_progress",
                severity="info",
                readiness_impact="none",
                surface="gateway",
                title=f"Resuming {in_progress} turn(s) interrupted by a restart",
                detail="The gateway is continuing turns it was running when it stopped.",
                evidence=evidence,
            )
        )
    if not findings:
        findings.append(
            HealthFinding(
                id="gateway.turn_recovery.ok",
                severity="ok",
                surface="gateway",
                title="Interrupted turns handled",
                detail=(
                    f"{snapshot.get('resumed', 0)} resumed, {snapshot.get('closed', 0)} "
                    f"needed nothing, {snapshot.get('cancelled', 0)} superseded by a new "
                    "message."
                ),
                evidence=evidence,
            )
        )
    return findings


def _config_reload_findings(ctx: RpcContext) -> list[HealthFinding]:
    """Restart-only config changes still pending, and a config file edit that was refused."""
    from agentos.gateway.config_commit import pending_restart_metadata
    from agentos.gateway.config_watcher import last_reload_status

    findings: list[HealthFinding] = []
    config = getattr(ctx, "config", None)
    pending, reasons = pending_restart_metadata(config) if config is not None else (False, [])
    status = last_reload_status()
    if pending:
        findings.append(
            HealthFinding(
                id="gateway.config.restart_pending",
                severity="warn",
                surface="gateway",
                title="Config changes wait for a gateway restart",
                detail=(
                    "Changes to "
                    + ", ".join(reasons)
                    + " are saved, but parts of the gateway read them only at startup; "
                    "they take full effect after a restart."
                ),
                evidence={"reasons": reasons, "paths": list(status.restart_paths)},
                fix_steps=[FixStep(label="Restart the gateway", command="agentos gateway restart")],
                restart_required=True,
            )
        )
    if status.status == "invalid":
        findings.append(
            HealthFinding(
                id="gateway.config.reload_skipped",
                severity="warn",
                surface="gateway",
                title="The config file has an error; the gateway kept its last good config",
                detail=status.error or "The edited config file did not validate.",
                evidence={"at": status.at},
                fix_steps=[
                    FixStep(
                        label="Fix the file",
                        detail="Correct the setting named above and save; the gateway "
                        "applies the file again on the next save.",
                    )
                ],
            )
        )
    return findings


@_d.method("doctor.status")
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
        ),
        *_safe_mode_findings(),
        *_turn_recovery_findings(),
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
        ("browser", lambda: _browser_payload(ctx), evaluate_browser),
        ("control_ui", lambda: _control_ui_payload(ctx), evaluate_control_ui),
        ("gateway", lambda: _gateway_security_payload(ctx), evaluate_gateway_security),
    ]

    for surface, collect, evaluate in collectors:
        findings.extend(await _evaluate_collection(surface, collect, evaluate))
    findings.extend(_config_reload_findings(ctx))

    config_path = _config_path(ctx)
    findings = _with_config_recovery_steps(findings, config_path)
    report = build_report(findings)
    report["agentId"] = agent_id
    if config_path:
        report["configPath"] = config_path
    return report
