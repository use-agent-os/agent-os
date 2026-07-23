"""RPC handlers for the config domain."""

from __future__ import annotations

import copy
from typing import Any, cast

from agentos.gateway.config_commit import (
    commit_config,
    config_disk_state,
    expected_revision,
    pending_restart_metadata,
    sync_provider_selector,
    update_config_in_place,
)
from agentos.gateway.rpc import RpcContext, get_dispatcher

_d = get_dispatcher()


def _update_config_in_place(old: Any, new: Any) -> None:
    """Copy all fields from new config into the existing config object in-memory."""
    update_config_in_place(old, new)


def _inherit_runtime_secrets(source: Any, target: Any) -> None:
    if hasattr(target, "inherit_runtime_secrets") and source is not None:
        target.inherit_runtime_secrets(source)


def _clear_runtime_secret_paths(config: Any, paths: set[str]) -> None:
    if not hasattr(config, "clear_runtime_secret"):
        return
    for path in paths:
        config.clear_runtime_secret(path)


def _collect_paths(payload: Any, prefix: str = "") -> set[str]:
    paths: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            current = f"{prefix}.{key}" if prefix else key
            paths.add(current)
            paths.update(_collect_paths(value, current))
    return paths


def diff_paths(old: Any, new: Any, prefix: str = "") -> set[str]:
    """Dotted paths that DIFFER between two nested dicts (added/removed/changed).

    Recurses through matching dict subtrees; a key present in exactly one side,
    or whose leaf value differs, yields its dotted path. Non-dict values (lists,
    scalars) are compared by equality — a changed list yields the list's own
    path, not per-index paths. Used by ``config.apply`` to treat a YAML-mode
    Save as an edit of only the fields the operator actually changed versus the
    baseline they were shown (``config.get``), so a field left at its runtime
    echo is restored while a deliberately edited overridden field persists.
    """
    if isinstance(old, dict) and isinstance(new, dict):
        changed: set[str] = set()
        for key in old.keys() | new.keys():
            current = f"{prefix}.{key}" if prefix else key
            if key not in old or key not in new:
                changed.add(current)
                # A whole added/removed subtree: name every leaf inside it too so
                # nested edits under a newly added section are all explicit.
                changed.update(_collect_paths(new.get(key), current))
                changed.update(_collect_paths(old.get(key), current))
            else:
                changed.update(diff_paths(old[key], new[key], current))
        return changed
    if old != new and prefix:
        return {prefix}
    return set()


def _align_auto_router_profile_for_provider_patch(
    source_config: Any,
    cfg_dict: dict[str, Any],
    explicit_paths: set[str],
) -> None:
    if "llm.provider" not in explicit_paths:
        return
    if any(
        path == "agentos_router" or path.startswith("agentos_router.")
        for path in explicit_paths
    ):
        return

    llm = cfg_dict.get("llm")
    router = cfg_dict.get("agentos_router")
    if not isinstance(llm, dict) or not isinstance(router, dict):
        return

    old_provider = str(getattr(getattr(source_config, "llm", None), "provider", "") or "")
    old_provider = old_provider.strip().lower()
    new_provider = str(llm.get("provider") or "").strip().lower()
    if not old_provider or not new_provider or old_provider == new_provider:
        return

    profile = str(router.get("tier_profile") or "").strip().lower()
    if profile != old_provider:
        return

    from agentos.gateway.config import ROUTER_TIER_PROFILE_IDS, _router_tier_profile_defaults

    try:
        old_defaults = _router_tier_profile_defaults(old_provider)
    except ValueError:
        return
    if router.get("tiers") != old_defaults:
        return

    if new_provider in ROUTER_TIER_PROFILE_IDS and new_provider != "openrouter":
        router["tier_profile"] = new_provider
        router["tiers"] = _router_tier_profile_defaults(new_provider)
        return

    router.pop("tier_profile", None)
    router.pop("tiers", None)


_REDACTED_PUBLIC_VALUE = "[redacted]"


def _is_sensitive_redacted_path(path: str) -> bool:
    if not path:
        return False
    from agentos.gateway.config import is_sensitive_config_key

    return is_sensitive_config_key(path.rsplit(".", 1)[-1])


def _has_existing_redacted_source(source: Any) -> bool:
    return source is not None and source != ""


def _restore_redacted_values(payload: Any, source: Any, prefix: str = "") -> tuple[Any, set[str]]:
    if payload == _REDACTED_PUBLIC_VALUE and _is_sensitive_redacted_path(prefix):
        if not _has_existing_redacted_source(source):
            raise ValueError(f"Cannot preserve redacted secret at {prefix}: no existing secret")
        return source, {prefix} if prefix else set()
    if isinstance(payload, dict):
        source_dict = source if isinstance(source, dict) else {}
        restored: dict[str, Any] = {}
        redacted_paths: set[str] = set()
        for key, value in payload.items():
            current = f"{prefix}.{key}" if prefix else key
            child, child_paths = _restore_redacted_values(
                value,
                source_dict.get(key),
                current,
            )
            restored[key] = child
            redacted_paths.update(child_paths)
        return restored, redacted_paths
    if isinstance(payload, list):
        source_list = source if isinstance(source, list) else []
        named_sources: dict[str, Any] = {}
        duplicate_names: set[str] = set()
        for item in source_list:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            name = item["name"]
            if name in named_sources:
                duplicate_names.add(name)
            else:
                named_sources[name] = item
        for name in duplicate_names:
            named_sources.pop(name, None)
        restored_list: list[Any] = []
        list_redacted_paths: set[str] = set()
        for index, value in enumerate(payload):
            current = f"{prefix}.{index}" if prefix else str(index)
            source_value = source_list[index] if index < len(source_list) else None
            if isinstance(value, dict) and isinstance(value.get("name"), str):
                source_value = named_sources.get(value["name"], source_value)
            child, child_paths = _restore_redacted_values(value, source_value, current)
            restored_list.append(child)
            list_redacted_paths.update(child_paths)
        return restored_list, list_redacted_paths
    return payload, set()


def _memory_restart_required_for_paths(paths: set[str]) -> bool:
    for path in paths:
        if path == "memory":
            return True
        if path == "memory.retrieval_mode":
            return True
        if path.startswith("memory.embedding"):
            return True
        if path.startswith("memory.provider"):
            return True
    return False


def _memory_restart_fingerprint(config: Any) -> dict[str, Any]:
    if config is None or not hasattr(config, "model_dump"):
        return {}
    data = config.model_dump(mode="python")
    memory = data.get("memory") if isinstance(data, dict) else None
    if not isinstance(memory, dict):
        return {}
    return {
        "source": memory.get("source"),
        "retrieval_mode": memory.get("retrieval_mode"),
        "embedding": memory.get("embedding"),
        # The external memory-provider manager is built once at boot, so
        # selecting a provider (or retuning its mem0 sub-settings) needs a
        # gateway restart to take live effect.
        "provider": memory.get("provider"),
        # Managers, repair service topology/cadence, and managed Dream cron
        # schedules are all constructed/reconciled during boot.
        "repair": {
            "enabled": memory.get("repair_enabled"),
            "interval_seconds": memory.get("repair_interval_seconds"),
            "max_items_per_tick": memory.get("repair_max_items_per_tick"),
        },
        "dream_schedule": {
            key: (memory.get("dream") or {}).get(key)
            if isinstance(memory.get("dream"), dict)
            else None
            for key in ("enabled", "auto_schedule", "interval_h", "cron")
        },
    }


def _channels_restart_fingerprint(config: Any) -> Any:
    """Fingerprint config.channels so any change forces restartRequired=True.

    ChannelManager and webhook routes are constructed once at boot, so any
    field change in config.channels — even a single token — requires a
    gateway restart to take live effect.
    """
    if config is None or not hasattr(config, "model_dump"):
        return None
    data = config.model_dump(mode="python")
    channels = data.get("channels") if isinstance(data, dict) else None
    if not isinstance(channels, dict):
        return None
    entries = channels.get("channels") or []
    if not isinstance(entries, list):
        return None
    return sorted(
        [entry for entry in entries if isinstance(entry, dict)],
        key=lambda e: (e.get("name") or "", e.get("type") or ""),
    )


def _sandbox_posture_restart_fingerprint(config: Any) -> dict[str, Any]:
    if config is None or not hasattr(config, "model_dump"):
        return {}
    data = config.model_dump(mode="python")
    if not isinstance(data, dict):
        return {}
    return {
        "permissions": data.get("permissions"),
        "sandbox": data.get("sandbox"),
    }


def _bind_restart_fingerprint(config: Any) -> dict[str, Any]:
    """Fingerprint the bind posture (host) and auth mode.

    Neither takes full effect on a hot apply: ``host`` does not rebind the
    live uvicorn socket, and while ``auth.mode`` is read live by
    AuthMiddleware, the startup guard and the captured loopback posture only
    re-evaluate on restart. Flagging these restart-required keeps the operator
    from believing a host/auth change is fully live when it is not.
    """
    if config is None or not hasattr(config, "model_dump"):
        return {}
    data = config.model_dump(mode="python")
    if not isinstance(data, dict):
        return {}
    auth = data.get("auth")
    return {
        "host": data.get("host"),
        "auth_mode": auth.get("mode") if isinstance(auth, dict) else None,
        "allow_unauthenticated_public": (
            auth.get("allow_unauthenticated_public") if isinstance(auth, dict) else None
        ),
    }


def _boot_runtime_restart_fingerprints(config: Any) -> dict[str, Any]:
    """Values captured by boot-created services without a generic hot adapter."""
    if config is None or not hasattr(config, "model_dump"):
        return {}
    data = config.model_dump(mode="python")
    if not isinstance(data, dict):
        return {}
    task_runtime = data.get("task_runtime")
    task_runtime = task_runtime if isinstance(task_runtime, dict) else {}
    subagents = data.get("subagents")
    subagents = subagents if isinstance(subagents, dict) else {}
    heartbeat = data.get("heartbeat")
    heartbeat = heartbeat if isinstance(heartbeat, dict) else {}
    tools = data.get("tools")
    tools = tools if isinstance(tools, dict) else {}
    skills = data.get("skills")
    skills = skills if isinstance(skills, dict) else {}
    return {
        # TaskRuntime constructor captures these queue/semaphore/deadline values.
        "task_runtime": {
            "max_concurrency": task_runtime.get("max_concurrency"),
            "max_pending_per_session": task_runtime.get("max_pending_per_session"),
            "channel_inflight_cap": task_runtime.get("channel_inflight_cap"),
            "turn_hard_deadline_s": task_runtime.get("turn_hard_deadline_s"),
            "pending_overflow_policy": task_runtime.get("pending_overflow_policy"),
            "subagent_reserved_slots": subagents.get("subagent_reserved_slots"),
        },
        # Uvicorn TLS and Starlette middleware/routes are assembled once.
        "tls": data.get("tls"),
        "cors": data.get("cors"),
        "control_ui": data.get("control_ui"),
        "server_debug": data.get("debug"),
        "state_dir": data.get("state_dir"),
        # RotatingFileHandler is built before services start.
        "logging": {
            "log_file_enabled": data.get("log_file_enabled"),
            "log_level": data.get("log_level"),
            "log_file_max_bytes": data.get("log_file_max_bytes"),
            "log_file_backup_count": data.get("log_file_backup_count"),
        },
        # Boot discovery owns the initially connected MCP clients/tools.
        "mcp": data.get("mcp"),
        # The SSRF resolver snapshots this trust boundary into module state.
        "tools_runtime": {
            "trusted_fake_ip_cidrs": tools.get("trusted_fake_ip_cidrs"),
        },
        # SkillLoader resolves its discovery layers once during boot. Keep
        # filter settings out: those are intentionally read live per turn.
        "skill_loader": {
            "workspace_dir": data.get("workspace_dir"),
            "allow_bundled": skills.get("allow_bundled"),
            "managed_dir": skills.get("managed_dir"),
            "extra_dirs": skills.get("extra_dirs"),
        },
        # The watcher and heartbeat ToolContext capture these paths/posture.
        "heartbeat_watcher": {
            "config_path": heartbeat.get("config_path"),
            "workspace_dir": data.get("workspace_dir"),
            "workspace_strict": data.get("workspace_strict"),
        },
        "diagnostics": data.get("diagnostics_enabled"),
    }


def _restart_required(
    *,
    old_memory_fingerprint: dict[str, Any],
    old_channels_fingerprint: Any,
    old_sandbox_posture_fingerprint: dict[str, Any],
    new_config: Any,
    old_bind_fingerprint: dict[str, Any] | None = None,
    old_boot_runtime_fingerprints: dict[str, Any] | None = None,
    changed_paths: set[str] | None = None,
) -> bool:
    return bool(
        _restart_reasons(
            old_memory_fingerprint=old_memory_fingerprint,
            old_channels_fingerprint=old_channels_fingerprint,
            old_sandbox_posture_fingerprint=old_sandbox_posture_fingerprint,
            old_bind_fingerprint=old_bind_fingerprint,
            old_boot_runtime_fingerprints=old_boot_runtime_fingerprints,
            changed_paths=changed_paths,
            new_config=new_config,
        )
    )


def _restart_reasons(
    *,
    old_memory_fingerprint: dict[str, Any],
    old_channels_fingerprint: Any,
    old_sandbox_posture_fingerprint: dict[str, Any],
    new_config: Any,
    old_bind_fingerprint: dict[str, Any] | None = None,
    old_boot_runtime_fingerprints: dict[str, Any] | None = None,
    changed_paths: set[str] | None = None,
) -> list[str]:
    reasons: list[str] = []
    if old_memory_fingerprint != _memory_restart_fingerprint(new_config):
        reasons.append("memory")
    if old_channels_fingerprint != _channels_restart_fingerprint(new_config):
        reasons.append("channels")
    if old_sandbox_posture_fingerprint != _sandbox_posture_restart_fingerprint(new_config):
        reasons.append("sandbox")
    if (
        old_bind_fingerprint is not None
        and old_bind_fingerprint != _bind_restart_fingerprint(new_config)
    ):
        reasons.append("gateway_bind")
    if old_boot_runtime_fingerprints is not None:
        new_boot = _boot_runtime_restart_fingerprints(new_config)
        for name, old_value in old_boot_runtime_fingerprints.items():
            if old_value != new_boot.get(name):
                reasons.append(name)
    if not reasons and _has_unproven_live_change(changed_paths or set()):
        reasons.append("config")
    return reasons


def _has_unproven_live_change(changed_paths: set[str]) -> bool:
    """Fail safe: unknown config changes require restart by default.

    Only paths with a concrete hot adapter or an audited live-read consumer
    are exempt. Work on leaf paths so a changed parent such as ``memory`` does
    not hide an explicitly allowlisted live child.
    """
    leaves = {
        path
        for path in changed_paths
        if not any(other.startswith(f"{path}.") for other in changed_paths if other != path)
    }
    hot_prefixes = ("llm", "image_generation", "audio")
    search_paths = {
        "search_provider",
        "search_api_key",
        "search_api_key_env",
        "search_max_results",
        "search_proxy",
        "search_use_env_proxy",
        "search_fallback_policy",
        "search_diagnostics",
    }
    live_paths = {
        *_SAFE_WRITE_PATCH_PATHS,
        "skills.filter_top_k",
        "skills.filter_strategy",
        "skills.filter_embedding_model",
        "skills.max_skills_prompt_chars",
        "skills.injection_mode",
        "task_runtime.pending_overflow_policy_per_channel",
        "task_runtime.stream_relay_coalesce_ms",
        "task_runtime.stream_relay_coalesce_chars",
        "updates.notify",
        "memory.curated_memory_char_limit",
        "memory.curated_user_char_limit",
        "memory.inject_limit",
    }
    for path in leaves:
        if path in search_paths or path in live_paths or path.startswith("rate_limit."):
            continue
        if any(path == prefix or path.startswith(f"{prefix}.") for prefix in hot_prefixes):
            continue
        if path.startswith("agentos_router.") and path != "agentos_router.require_router_runtime":
            continue
        return True
    return False


def _validate_memory_embedding_semantics(config: Any) -> None:
    memory_cfg = getattr(config, "memory", None)
    if memory_cfg is None:
        return
    from agentos.memory.embedding_resolver import resolve_memory_embedding

    resolve_memory_embedding(memory_cfg, local_available=lambda *_: False)


def _sync_provider_selector(ctx: RpcContext, config: Any) -> None:
    sync_provider_selector(ctx, config)


# Read-only paths that cannot be modified via config.set/patch/apply.
# host/port: bind posture is CLI-only (agentos gateway run --bind / --port).
# auth credentials are provisioned through the guarded CLI flow, and
# config_path is the boot-selected persistence target rather than user data.
_BIND_READONLY_PATHS = frozenset({"host", "port"})
_AUTH_CREDENTIAL_PATHS = frozenset({"auth.token", "auth.password"})
_TARGET_READONLY_PATHS = frozenset({"config_path", "version"})
_READONLY_PATHS = _BIND_READONLY_PATHS | _AUTH_CREDENTIAL_PATHS | _TARGET_READONLY_PATHS
_SAFE_WRITE_PATCH_PATHS = frozenset(
    {
        "skills.filter_enabled",
        "skills.filter_lexical_top_n",
        "skills.filter_semantic_top_n",
        "skills.filter_rrf_k",
        "prompt_cache.mode",
        "agentos_router.enabled",
        "agentos_router.rollout_phase",
        "agentos_router.strategy",
        "agentos_router.default_tier",
        "agentos_router.confidence_threshold",
    }
)


def _value_at_path(payload: dict[str, Any], path: str) -> Any:
    try:
        return _resolve_path(payload, path)
    except KeyError:
        return None


def _has_path(payload: dict[str, Any], path: str) -> bool:
    node: Any = payload
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def _assert_auth_credentials_unchanged(
    payload: dict[str, Any],
    source: dict[str, Any],
    *,
    only_present: bool = False,
) -> None:
    """Reject direct credential rotation while allowing untouched round-trips."""
    for path in sorted(_AUTH_CREDENTIAL_PATHS):
        if only_present and not _has_path(payload, path):
            continue
        if _value_at_path(payload, path) != _value_at_path(source, path):
            raise ValueError(
                f"Path is read-only: {path}; provision gateway credentials through the CLI"
            )


def _preserve_readonly_values(payload: dict, config: Any) -> dict:
    """Copy every runtime-owned/read-only value from the active config.

    Full-object clients echo these fields, so preserve instead of rejecting at
    that boundary. Direct set/dot-path credential or target edits are rejected
    separately with an explicit error.
    """
    payload = dict(payload)
    if config is not None:
        payload["host"] = config.host
        payload["port"] = config.port
        payload["config_path"] = getattr(config, "config_path", None)
        payload["version"] = getattr(config, "version", None)
        auth = dict(payload.get("auth") or {})
        auth["token"] = getattr(config.auth, "token", None)
        auth["password"] = getattr(config.auth, "password", None)
        payload["auth"] = auth
    else:
        payload.pop("config_path", None)
    return payload


def _resolve_path(obj: dict, path: str) -> Any:
    """Walk a dot-separated path into a nested dict."""
    parts = path.split(".")
    val: Any = obj
    for part in parts:
        if isinstance(val, dict):
            if part not in val:
                raise KeyError(f"Path not found: {path}")
            val = val[part]
        else:
            raise KeyError(f"Path not found: {path}")
    return val


def _set_path(obj: dict, path: str, value: Any) -> None:
    """Set a value at a dot-separated path in a nested dict."""
    parts = path.split(".")
    current = obj
    for part in parts[:-1]:
        if part not in current or not isinstance(current[part], dict):
            current[part] = {}
        current = current[part]
    current[parts[-1]] = value


def _deep_merge(base: dict, patch: dict) -> dict:
    """Deep-merge *patch* into *base*. Keys set to None delete the target key."""
    result = dict(base)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


@_d.method("config.snapshot", scope="operator.read")
async def _handle_config_snapshot(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Return one coherent, redacted setup/config view for control surfaces."""
    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")

    from agentos.gateway.rpc_onboarding import _active_config, _status_payload
    from agentos.onboarding.setup_engine import setup_catalog_payload

    config = _active_config(ctx)
    status = _status_payload(ctx, config)
    sections = dict(status.get("sections") or {})
    pending_restart, restart_reasons = pending_restart_metadata(config)
    disk_state = config_disk_state(config)
    public_config = (
        config.to_public_dict()
        if hasattr(config, "to_public_dict")
        else config.model_dump()
        if hasattr(config, "model_dump")
        else {}
    )
    return {
        "config": public_config,
        "catalog": setup_catalog_payload(),
        "status": status,
        "readiness": {
            "coreReady": sections.get("llm") == "ok",
            "runtimeReady": not bool(status.get("needsOnboarding", True)),
            "needsOnboarding": bool(status.get("needsOnboarding", True)),
            "sections": sections,
            "sectionDetails": dict(status.get("sectionDetails") or {}),
        },
        "revision": disk_state.revision,
        "configPath": status.get("configPath") or getattr(config, "config_path", None),
        "pendingRestart": pending_restart,
        "restartReasons": restart_reasons,
        "diskDiverged": disk_state.disk_diverged,
        "writeBlocked": disk_state.write_blocked,
    }


@_d.method("config.set", scope="operator.admin")
async def _handle_config_set(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict) or "path" not in params or "value" not in params:
        raise ValueError("params.path and params.value are required")

    path: str = params["path"]
    if path in _READONLY_PATHS:
        raise ValueError(f"Path is read-only: {path}")

    if ctx.config is None:
        raise ValueError("No config available")

    old_memory_fingerprint = _memory_restart_fingerprint(ctx.config)
    old_channels_fingerprint = _channels_restart_fingerprint(ctx.config)
    old_sandbox_posture_fingerprint = _sandbox_posture_restart_fingerprint(ctx.config)
    old_bind_fingerprint = _bind_restart_fingerprint(ctx.config)
    old_boot_runtime_fingerprints = _boot_runtime_restart_fingerprints(ctx.config)
    cfg_dict = ctx.config.model_dump() if hasattr(ctx.config, "model_dump") else {}
    source_cfg_dict = copy.deepcopy(cfg_dict) if isinstance(cfg_dict, dict) else {}
    # Validate path exists
    source_value = _resolve_path(cfg_dict, path)
    value = params["value"]
    if value == _REDACTED_PUBLIC_VALUE and _is_sensitive_redacted_path(path):
        raise ValueError(
            f"Cannot set redacted secret marker directly at {path}; "
            "submit the containing public config object to preserve it"
        )
    restored_value, redacted_paths = _restore_redacted_values(value, source_value, path)
    _set_path(cfg_dict, path, restored_value)
    _assert_auth_credentials_unchanged(cfg_dict, source_cfg_dict)
    cfg_dict = _preserve_readonly_values(cfg_dict, ctx.config)

    # Re-validate full config
    from agentos.gateway.config import GatewayConfig

    new_config = GatewayConfig(**cfg_dict)
    if _memory_restart_required_for_paths({path}):
        _validate_memory_embedding_semantics(new_config)
    _inherit_runtime_secrets(ctx.config, new_config)
    # host/port are never persistable via RPC (bind posture is CLI-only), so a
    # readonly path echoed inside a nested value must never enter explicit_paths
    # — otherwise it short-circuits the runtime-override restore in persist_config
    # and freezes a transient CLI bind. Subtract them unconditionally.
    explicit_paths = ({path} | _collect_paths(value, path)) - _READONLY_PATHS
    _clear_runtime_secret_paths(new_config, explicit_paths - redacted_paths)
    changed_paths = diff_paths(
        ctx.config.model_dump(mode="python"),
        new_config.model_dump(mode="python"),
    )
    result = commit_config(
        ctx,
        new_config,
        source_config=ctx.config,
        explicit_paths=explicit_paths,
        changed_paths=changed_paths,
        expected=expected_revision(params),
        restart_reasons=_restart_reasons(
            old_memory_fingerprint=old_memory_fingerprint,
            old_channels_fingerprint=old_channels_fingerprint,
            old_sandbox_posture_fingerprint=old_sandbox_posture_fingerprint,
            old_bind_fingerprint=old_bind_fingerprint,
            old_boot_runtime_fingerprints=old_boot_runtime_fingerprints,
            changed_paths=changed_paths,
            new_config=new_config,
        ),
    )
    return {"restartRequired": result.restart_required}


@_d.method("config.patch", scope="operator.admin")
async def _handle_config_patch(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("params.patch or params.patches is required")

    # Accept both "patch" (dict merge) and "patches" (dot-path key-value pairs)
    patch_data = params.get("patch") or {}
    dot_patches = params.get("patches") or {}

    if not patch_data and not dot_patches:
        raise ValueError("params.patch or params.patches is required")

    if ctx.config is None:
        raise ValueError("No config available")

    old_memory_fingerprint = _memory_restart_fingerprint(ctx.config)
    old_channels_fingerprint = _channels_restart_fingerprint(ctx.config)
    old_sandbox_posture_fingerprint = _sandbox_posture_restart_fingerprint(ctx.config)
    old_bind_fingerprint = _bind_restart_fingerprint(ctx.config)
    old_boot_runtime_fingerprints = _boot_runtime_restart_fingerprints(ctx.config)
    cfg_dict = ctx.config.model_dump() if hasattr(ctx.config, "model_dump") else {}
    source_cfg_dict = copy.deepcopy(cfg_dict) if isinstance(cfg_dict, dict) else {}
    redacted_paths: set[str] = set()

    # Apply dot-path patches (e.g. {"skills.filter_enabled": true})
    for path, value in dot_patches.items():
        if path in _BIND_READONLY_PATHS:
            continue
        if path in _TARGET_READONLY_PATHS:
            if value != _value_at_path(source_cfg_dict, path):
                raise ValueError(f"Path is read-only: {path}")
            continue
        if value == _REDACTED_PUBLIC_VALUE and _is_sensitive_redacted_path(path):
            raise ValueError(
                f"Cannot patch redacted secret marker directly at {path}; "
                "submit the containing public config object to preserve it"
            )
        try:
            source_value = _resolve_path(source_cfg_dict, path)
        except KeyError:
            source_value = None
        restored_value, restored_paths = _restore_redacted_values(value, source_value, path)
        redacted_paths.update(restored_paths)
        _set_path(cfg_dict, path, restored_value)

    # Apply dict merge patch
    if patch_data:
        patch_data, merge_restored_paths = _restore_redacted_values(patch_data, source_cfg_dict)
        redacted_paths.update(merge_restored_paths)
        cfg_dict = _deep_merge(cfg_dict, patch_data)
    _assert_auth_credentials_unchanged(cfg_dict, source_cfg_dict)
    # Merge/nested patches can carry read-only fields without naming their dot
    # paths directly. Re-assert every runtime-owned value before validation.
    cfg_dict = _preserve_readonly_values(cfg_dict, ctx.config)

    explicit_paths = set(dot_patches.keys()) | _collect_paths(patch_data)
    for path, value in dot_patches.items():
        explicit_paths.update(_collect_paths(value, path))
    # A UI form can echo the display-only host/port alongside a genuine edit;
    # they are read-only paths (skipped when applied), so they must not enter
    # explicit_paths and short-circuit the runtime-override restore. host/port
    # are never persistable via RPC regardless.
    explicit_paths -= _READONLY_PATHS
    _align_auto_router_profile_for_provider_patch(ctx.config, cfg_dict, explicit_paths)

    from agentos.gateway.config import GatewayConfig

    new_config = GatewayConfig(**cfg_dict)
    if _memory_restart_required_for_paths(explicit_paths):
        _validate_memory_embedding_semantics(new_config)
    _inherit_runtime_secrets(ctx.config, new_config)
    _clear_runtime_secret_paths(new_config, explicit_paths - redacted_paths)
    changed_paths = diff_paths(source_cfg_dict, new_config.model_dump(mode="python"))
    result = commit_config(
        ctx,
        new_config,
        source_config=ctx.config,
        explicit_paths=explicit_paths,
        changed_paths=changed_paths,
        expected=expected_revision(params),
        restart_reasons=_restart_reasons(
            old_memory_fingerprint=old_memory_fingerprint,
            old_channels_fingerprint=old_channels_fingerprint,
            old_sandbox_posture_fingerprint=old_sandbox_posture_fingerprint,
            old_bind_fingerprint=old_bind_fingerprint,
            old_boot_runtime_fingerprints=old_boot_runtime_fingerprints,
            changed_paths=changed_paths,
            new_config=new_config,
        ),
    )
    return {
        "patched": list(dot_patches.keys()) + (["(merge)"] if patch_data else []),
        "restartRequired": result.restart_required,
    }


@_d.method("config.patch.safe", scope="operator.write")
async def _handle_config_patch_safe(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("params.patches is required")

    patch_data = params.get("patch") or {}
    dot_patches = params.get("patches") or {}
    if patch_data:
        raise ValueError("params.patch is not supported for safe config patch")
    if not dot_patches:
        raise ValueError("params.patches is required")

    unsafe_paths = sorted(set(dot_patches) - _SAFE_WRITE_PATCH_PATHS)
    if unsafe_paths:
        raise ValueError(f"Path is not safe for operator.write: {unsafe_paths[0]}")

    return cast(dict[str, Any], await _handle_config_patch(params, ctx))


@_d.method("config.apply", scope="operator.admin")
async def _handle_config_apply(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("params.config is required")

    from agentos.gateway.config import GatewayConfig

    baseline_payload: Any = None
    config_payload = params.get("config")
    if config_payload is None and "config_yaml" in params:
        import yaml  # type: ignore[import-untyped]

        config_payload = yaml.safe_load(params["config_yaml"]) or {}
        if "baseline_yaml" in params:
            baseline_payload = yaml.safe_load(params["baseline_yaml"]) or {}

    if not isinstance(config_payload, dict):
        raise ValueError("params.config is required")

    config_payload = dict(config_payload)
    # YAML-mode Save seeds the payload from the RUNNING config (config.get), so
    # every CLI/break-glass override it carries (host/port/debug/auth.mode/
    # opt-in) is echoed back verbatim.
    from agentos.gateway.config_persist import get_runtime_overrides

    if isinstance(baseline_payload, dict):
        # The client sent the baseline it showed the operator (the config.get
        # snapshot). A field that DIFFERS from that baseline is a real edit and
        # must persist even while it carries a runtime override; a field left at
        # its echo is not an edit and is restored to its on-disk original by
        # persist_config. Subtract _READONLY_PATHS: host/port are never
        # persistable via RPC regardless of a diff.
        explicit_paths = diff_paths(baseline_payload, config_payload) - _READONLY_PATHS
    else:
        # Older client (or config= dict payload): no baseline to diff against.
        # Fall back to the round-2 safe behavior — subtract the full runtime-
        # override key set (mirroring rpc_onboarding._onboarding_explicit_paths)
        # so an echoed transient posture never freezes. (A form-mode config.patch
        # sends only the dirty keys, which are not in the override map.)
        explicit_paths = _collect_paths(config_payload) - set(get_runtime_overrides())
    explicit_paths -= _READONLY_PATHS

    old_memory_fingerprint = _memory_restart_fingerprint(ctx.config)
    old_channels_fingerprint = _channels_restart_fingerprint(ctx.config)
    old_sandbox_posture_fingerprint = _sandbox_posture_restart_fingerprint(ctx.config)
    old_bind_fingerprint = _bind_restart_fingerprint(ctx.config)
    old_boot_runtime_fingerprints = _boot_runtime_restart_fingerprints(ctx.config)
    old_payload = (
        ctx.config.model_dump(mode="python")
        if ctx.config is not None and hasattr(ctx.config, "model_dump")
        else {}
    )
    config_payload, redacted_paths = _restore_redacted_values(config_payload, old_payload)
    _assert_auth_credentials_unchanged(config_payload, old_payload, only_present=True)
    config_payload = _preserve_readonly_values(config_payload, ctx.config)

    # Validate and persist the full replacement config
    new_config = GatewayConfig(**config_payload)
    _validate_memory_embedding_semantics(new_config)
    _inherit_runtime_secrets(ctx.config, new_config)
    _clear_runtime_secret_paths(new_config, _collect_paths(config_payload) - redacted_paths)
    changed_paths = diff_paths(old_payload, new_config.model_dump(mode="python"))
    result = commit_config(
        ctx,
        new_config,
        source_config=ctx.config,
        explicit_paths=explicit_paths,
        changed_paths=changed_paths,
        expected=expected_revision(params),
        restart_reasons=_restart_reasons(
            old_memory_fingerprint=old_memory_fingerprint,
            old_channels_fingerprint=old_channels_fingerprint,
            old_sandbox_posture_fingerprint=old_sandbox_posture_fingerprint,
            old_bind_fingerprint=old_bind_fingerprint,
            old_boot_runtime_fingerprints=old_boot_runtime_fingerprints,
            changed_paths=changed_paths,
            new_config=new_config,
        ),
    )
    return {"restartRequired": result.restart_required}


@_d.method("config.schema", scope="operator.admin")
async def _handle_config_schema(params: dict | None, ctx: RpcContext) -> dict:
    from agentos.gateway.config import GatewayConfig

    schema = GatewayConfig.model_json_schema()

    if isinstance(params, dict) and params.get("section"):
        section = params["section"]
        # Navigate into $defs or properties
        props = schema.get("properties", {})
        if section in props:
            return {"schema": props[section]}
        defs = schema.get("$defs", {})
        if section in defs:
            return {"schema": defs[section]}
        raise KeyError(f"Schema section not found: {section}")

    return {"schema": schema}


@_d.method("config.schema.lookup", scope="operator.read")
async def _handle_config_schema_lookup(params: dict | None, ctx: RpcContext) -> dict:
    if not isinstance(params, dict) or "path" not in params:
        raise ValueError("params.path is required")

    from agentos.gateway.config import GatewayConfig

    schema = GatewayConfig.model_json_schema()
    path = params["path"]
    parts = path.split(".")

    # Walk through the schema tree resolving $ref along the way
    node: dict = schema
    for part in parts:
        props = node.get("properties", {})
        if part in props:
            node = props[part]
            # Resolve $ref if present
            ref = node.get("$ref")
            if ref and ref.startswith("#/$defs/"):
                def_name = ref.split("/")[-1]
                node = schema.get("$defs", {}).get(def_name, node)
        else:
            raise KeyError(f"Schema path not found: {path}")

    return {
        "path": path,
        "type": node.get("type", "object"),
        "description": node.get("description"),
        "default": node.get("default"),
        "enum": node.get("enum"),
    }
