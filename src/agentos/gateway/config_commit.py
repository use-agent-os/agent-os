"""Transactional config commits shared by gateway RPC writers.

The gateway exposes both guided onboarding mutations and a raw config editor.
Both surfaces must follow the same ordering contract: validate a cloned config,
persist it atomically, and only then mutate the running config and its hot
runtime adapters.  This module owns that final commit boundary.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from agentos.gateway.config_persist import persist_config, prepare_persist_payload

if TYPE_CHECKING:
    from agentos.gateway.rpc import RpcContext

HotApply = Callable[[Any], None]
_PENDING_RESTART_REASONS_ATTR = "_gateway_pending_restart_reasons"
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConfigCommitResult:
    """Outcome of one persisted config transaction."""

    path: Path
    revision: str
    restart_required: bool
    restart_reasons: tuple[str, ...]
    applied_now: bool


@dataclass(frozen=True)
class ConfigDiskState:
    """Coherence between the active runtime config and its persistence target."""

    revision: str | None
    disk_diverged: bool
    write_blocked: bool


def _resolved_config_path(config: Any) -> Path | None:
    value = getattr(config, "config_path", None)
    if value:
        return Path(str(value)).expanduser()
    return None


def _canonical_payload_bytes(config: Any) -> bytes:
    if hasattr(config, "to_toml_dict"):
        payload = prepare_persist_payload(config)
    elif hasattr(config, "model_dump"):
        payload = config.model_dump(mode="python")
    else:
        payload = {}
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def config_revision(config: Any) -> str:
    """Return a deterministic revision for the persisted config snapshot.

    Existing files are hashed byte-for-byte so edits made by another process
    participate in optimistic concurrency.  Before the first write, the
    canonical TOML payload is hashed instead.
    """

    path = _resolved_config_path(config)
    if path is not None:
        try:
            payload = path.read_bytes()
        except OSError:
            payload = _canonical_payload_bytes(config)
    else:
        payload = _canonical_payload_bytes(config)
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _normalized_disk_payload(path: Path, raw: dict[str, Any]) -> dict[str, Any]:
    from agentos.gateway.config import GatewayConfig
    from agentos.gateway.config_migration import migrate_config_payload
    from agentos.gateway.config_persist import get_runtime_overrides

    migrated = migrate_config_payload(raw).payload
    # Match GatewayConfig.load exactly: BaseSettings construction applies
    # AGENTOS_GATEWAY_* values for keys absent from TOML.
    disk_config = GatewayConfig(**migrated)
    disk_config.mark_env_sourced_auth_secrets(migrated)
    _assert_payload_is_modeled(migrated, disk_config)
    llm_payload = raw.get("llm") if isinstance(raw, dict) else None
    if (
        (not isinstance(llm_payload, dict) or "api_key" not in llm_payload)
        and disk_config.llm.api_key
    ):
        disk_config.mark_runtime_secret("llm.api_key")
    normalized = _normalized_config_payload(
        disk_config,
        path,
        restore_runtime_overrides=False,
    )
    # Environment settings participate in GatewayConfig.load semantics, but a
    # boot-overridden field absent from TOML is intentionally absent from the
    # persistence snapshot. Remove only those absent paths. Present values are
    # left exactly as the newly read disk model produced them, so a post-boot
    # external edit cannot be masked by the stale boot override map.
    for override_path, original in get_runtime_overrides().items():
        if original is None and not _dotted_path_present(migrated, override_path):
            _drop_dotted_path(normalized, override_path)
    return normalized


def _dotted_path_present(payload: dict[str, Any], path: str) -> bool:
    node: Any = payload
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def _drop_dotted_path(payload: dict[str, Any], path: str) -> None:
    parts = path.split(".")
    node: Any = payload
    for part in parts[:-1]:
        if not isinstance(node, dict):
            return
        node = node.get(part)
    if isinstance(node, dict):
        node.pop(parts[-1], None)


def _assert_payload_is_modeled(payload: Any, model_value: Any, prefix: str = "") -> None:
    """Fail closed when Pydantic would silently discard an external TOML key."""
    from pydantic import BaseModel

    if isinstance(payload, dict) and isinstance(model_value, BaseModel):
        fields = type(model_value).model_fields
        for raw_key, raw_value in payload.items():
            field_name: str | None = None
            for name, field in fields.items():
                aliases = {name}
                if isinstance(field.alias, str):
                    aliases.add(field.alias)
                validation_alias = field.validation_alias
                if isinstance(validation_alias, str):
                    aliases.add(validation_alias)
                else:
                    for choice in getattr(validation_alias, "choices", ()):
                        if isinstance(choice, str):
                            aliases.add(choice)
                if raw_key in aliases:
                    field_name = name
                    break
            current = f"{prefix}.{raw_key}" if prefix else str(raw_key)
            if field_name is None:
                raise ValueError(f"unmodeled config key: {current}")
            _assert_payload_is_modeled(raw_value, getattr(model_value, field_name), current)
        return
    if isinstance(payload, dict) and isinstance(model_value, dict):
        for key, raw_value in payload.items():
            current = f"{prefix}.{key}" if prefix else str(key)
            if key not in model_value:
                raise ValueError(f"unmodeled config key: {current}")
            _assert_payload_is_modeled(raw_value, model_value[key], current)
        return
    if isinstance(payload, list) and isinstance(model_value, list):
        for index, raw_value in enumerate(payload):
            if index < len(model_value):
                _assert_payload_is_modeled(
                    raw_value,
                    model_value[index],
                    f"{prefix}.{index}",
                )


def _normalized_config_payload(
    config: Any,
    path: Path,
    *,
    restore_runtime_overrides: bool = True,
) -> dict[str, Any]:
    candidate = config.model_copy(deep=True) if hasattr(config, "model_copy") else config
    if candidate is not config and hasattr(candidate, "inherit_runtime_secrets"):
        candidate.inherit_runtime_secrets(config)
    candidate.config_path = str(path)
    from agentos.gateway.llm_runtime import resolve_llm_runtime_config

    resolve_llm_runtime_config(candidate)
    if restore_runtime_overrides:
        return prepare_persist_payload(candidate)
    return cast(dict[str, Any], candidate.to_toml_dict())


def config_disk_state(config: Any) -> ConfigDiskState:
    """Return a fail-closed runtime/disk coherence snapshot.

    A semantic external edit must never be paired with the stale runtime
    payload and a fresh disk revision. When the two differ, callers can still
    render the active config but every write is blocked until reload/restart.
    """

    path = _resolved_config_path(config)
    if path is None or not path.exists():
        return ConfigDiskState(
            revision=config_revision(config),
            disk_diverged=False,
            write_blocked=False,
        )
    try:
        disk_bytes = path.read_bytes()
        raw = tomllib.loads(disk_bytes.decode("utf-8"))
        disk_payload = _normalized_disk_payload(path, raw)
        live_payload = _normalized_config_payload(config, path)
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        return ConfigDiskState(revision=None, disk_diverged=True, write_blocked=True)

    diverged = disk_payload != live_payload
    return ConfigDiskState(
        revision=None
        if diverged
        else f"sha256:{hashlib.sha256(disk_bytes).hexdigest()}",
        disk_diverged=diverged,
        write_blocked=diverged,
    )


def expected_revision(params: Any) -> str | None:
    """Read and validate an optional ``expectedRevision`` RPC parameter."""

    if not isinstance(params, dict) or "expectedRevision" not in params:
        return None
    value = params["expectedRevision"]
    if not isinstance(value, str) or not value.strip():
        raise ValueError("params.expectedRevision must be a non-empty string")
    return value.strip()


def _assert_expected_revision(config: Any, expected: str | None) -> None:
    state = config_disk_state(config)
    if state.write_blocked:
        raise ValueError(
            "config file diverged from the running gateway; reload or restart before writing"
        )
    if expected is None:
        return
    current = state.revision
    if expected != current:
        raise ValueError(
            "config revision mismatch: "
            f"expected {expected}, current {current}; refresh and retry"
        )


def update_config_in_place(target: Any, source: Any) -> None:
    """Copy all public model fields while preserving runtime-secret metadata."""

    for field_name in type(source).model_fields:
        setattr(target, field_name, getattr(source, field_name))
    if hasattr(target, "inherit_runtime_secrets"):
        target.inherit_runtime_secrets(source)


def pending_restart_metadata(config: Any) -> tuple[bool, list[str]]:
    reasons = sorted(
        {
            str(reason)
            for reason in getattr(config, _PENDING_RESTART_REASONS_ATTR, ())
            if str(reason)
        }
    )
    return bool(reasons), reasons


def _record_pending_restart(config: Any, reasons: Iterable[str]) -> tuple[str, ...]:
    existing = set(getattr(config, _PENDING_RESTART_REASONS_ATTR, ()))
    existing.update(str(reason) for reason in reasons if str(reason))
    ordered = tuple(sorted(existing))
    setattr(config, _PENDING_RESTART_REASONS_ATTR, ordered)
    return ordered


def _touches(paths: set[str], roots: set[str]) -> bool:
    return any(
        path == root or path.startswith(f"{root}.") or root.startswith(f"{path}.")
        for path in paths
        for root in roots
    )


def sync_provider_selector(ctx: RpcContext, config: Any) -> None:
    """Hot-apply the active provider, or signal that boot must construct it."""

    from agentos.gateway.llm_runtime import resolve_llm_runtime_config
    from agentos.provider.selector import ProviderConfig

    runtime = resolve_llm_runtime_config(config)
    selector = getattr(ctx, "provider_selector", None)
    if selector is None or not hasattr(selector, "sync_primary"):
        raise RuntimeError("provider selector is not initialized")
    selector.sync_primary(
        ProviderConfig(
            provider=runtime.provider,
            model=runtime.model,
            api_key=runtime.api_key,
            base_url=runtime.base_url,
            proxy=runtime.proxy,
            provider_routing=runtime.provider_routing,
        )
    )


def sync_media(config: Any) -> None:
    from agentos.tools.builtin.media import configure_audio, configure_image_generation

    configure_image_generation(
        getattr(config, "image_generation", None),
        llm_config=getattr(config, "llm", None),
        agentos_router_config=getattr(config, "agentos_router", None),
    )
    configure_audio(getattr(config, "audio", None))


def sync_search(config: Any) -> None:
    from agentos.search.registry import get_provider_spec
    from agentos.tools.builtin.web import configure_search

    provider = str(getattr(config, "search_provider", "") or "")
    api_key = str(getattr(config, "search_api_key", "") or "")
    if not api_key and provider:
        spec = get_provider_spec(provider)
        env_key = str(getattr(config, "search_api_key_env", "") or spec.env_key)
        api_key = os.environ.get(env_key, "") if env_key else ""
    configure_search(
        provider_name=provider,
        max_results=config.search_max_results,
        api_key=api_key,
        proxy=config.search_proxy,
        use_env_proxy=config.search_use_env_proxy,
        fallback_policy=config.search_fallback_policy,
        diagnostics=config.search_diagnostics,
    )


def _runtime_steps(ctx: RpcContext, changed_paths: set[str]) -> list[tuple[str, HotApply]]:
    if getattr(ctx, "config", None) is None:
        return []

    steps: list[tuple[str, HotApply]] = []
    provider_roots = {"llm", "agentos_router"}
    media_roots = {"llm", "agentos_router", "image_generation", "audio"}
    search_roots = {
        "search_provider",
        "search_api_key",
        "search_api_key_env",
        "search_max_results",
        "search_proxy",
        "search_use_env_proxy",
        "search_fallback_policy",
        "search_diagnostics",
    }
    if _touches(changed_paths, provider_roots):
        steps.append(("provider", lambda config: sync_provider_selector(ctx, config)))
    if _touches(changed_paths, media_roots):
        steps.append(("media", sync_media))
    if _touches(changed_paths, search_roots):
        steps.append(("search", sync_search))
    return steps


def _prepare_for_persistence(config: Any, changed_paths: set[str]) -> None:
    """Resolve clone-local runtime values needed for a correct serialized form."""
    if not _touches(changed_paths, {"llm", "agentos_router"}):
        return
    from agentos.gateway.llm_runtime import resolve_llm_runtime_config

    # Resolution marks env-sourced credentials as runtime-only. Doing this on
    # the validated clone keeps those values out of TOML without touching the
    # running config or any runtime singleton before the atomic write.
    resolve_llm_runtime_config(config)


def commit_config(
    ctx: RpcContext,
    new_config: Any,
    *,
    source_config: Any | None = None,
    explicit_paths: set[str] | None = None,
    changed_paths: set[str] | None = None,
    expected: str | None = None,
    restart_reasons: Iterable[str] = (),
) -> ConfigCommitResult:
    """Persist ``new_config`` before exposing it to the running gateway."""

    from agentos.gateway.config import enforce_public_bind_auth_guard

    running_config = getattr(ctx, "config", None)
    model_copy = getattr(new_config, "model_copy", None)
    if running_config is new_config and callable(model_copy):
        candidate = model_copy(deep=True)
        if hasattr(candidate, "inherit_runtime_secrets"):
            candidate.inherit_runtime_secrets(new_config)
        new_config = candidate
    source = (
        source_config
        if source_config is not None
        else running_config
        if running_config is not None
        else new_config
    )
    has_existing_runtime = running_config is not None or source_config is not None
    if new_config.auth.mode == "token" and not new_config.auth.token:
        source_had_missing_token_mode = (
            has_existing_runtime
            and source.auth.mode == "token"
            and not source.auth.token
        )
        if not source_had_missing_token_mode:
            raise ValueError(
                "auth.mode='token' requires a provisioned token; provision gateway "
                "credentials through the CLI before enabling token auth"
            )

    # AuthMiddleware reads the shared config object live. Never let an RPC
    # commit weaken a public listener before the boot-only safety guard gets a
    # chance to run again. A legacy already-running unsafe posture may still
    # save unrelated fields, but any safety-posture transition is revalidated.
    source_posture = (
        source.host,
        source.auth.mode,
        source.auth.allow_unauthenticated_public,
        source.auth.trusted_proxy,
    )
    candidate_posture = (
        new_config.host,
        new_config.auth.mode,
        new_config.auth.allow_unauthenticated_public,
        new_config.auth.trusted_proxy,
    )
    try:
        enforce_public_bind_auth_guard(source)
    except ValueError:
        if source_posture != candidate_posture:
            enforce_public_bind_auth_guard(new_config)
    else:
        enforce_public_bind_auth_guard(new_config)

    _assert_expected_revision(source, expected)

    source_path = getattr(source, "config_path", None)
    if source_config is not None or running_config is not None:
        new_config.config_path = str(source_path) if source_path else None

    changed = set(changed_paths or ())
    _prepare_for_persistence(new_config, changed)

    # The atomic writer is deliberately first. No running object or singleton
    # adapter has been touched if this raises.
    path = persist_config(new_config, explicit_paths=explicit_paths)
    if not getattr(new_config, "config_path", None):
        new_config.config_path = str(path)

    live_config = new_config
    if running_config is not None and running_config is not new_config:
        update_config_in_place(running_config, new_config)
        live_config = running_config

    reasons = {str(reason) for reason in restart_reasons if str(reason)}
    applied_now = True
    for name, apply_step in _runtime_steps(ctx, changed):
        try:
            apply_step(live_config)
        except Exception:
            log.exception("config hot apply failed adapter=%s", name)
            applied_now = False
            reasons.add(f"hot_apply:{name}")

    pending = _record_pending_restart(live_config, reasons)
    return ConfigCommitResult(
        path=path,
        revision=config_revision(live_config),
        restart_required=bool(reasons),
        restart_reasons=pending,
        applied_now=applied_now,
    )


__all__ = [
    "ConfigCommitResult",
    "ConfigDiskState",
    "commit_config",
    "config_disk_state",
    "config_revision",
    "expected_revision",
    "pending_restart_metadata",
    "sync_media",
    "sync_provider_selector",
    "sync_search",
    "update_config_in_place",
]
