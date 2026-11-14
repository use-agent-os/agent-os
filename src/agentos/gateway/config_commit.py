"""Transactional config commits shared by gateway RPC writers.

The gateway exposes both guided onboarding mutations and a raw config editor.
Both surfaces must follow the same ordering contract: validate a cloned config,
persist it atomically, and only then mutate the running config and its hot
runtime adapters.  This module owns that final commit boundary.
"""

from __future__ import annotations

import copy
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
        not isinstance(llm_payload, dict) or "api_key" not in llm_payload
    ) and disk_config.llm.api_key:
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
        revision=None if diverged else f"sha256:{hashlib.sha256(disk_bytes).hexdigest()}",
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
            f"config revision mismatch: expected {expected}, current {current}; refresh and retry"
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
    from agentos.provider.auxiliary import configure_auxiliary
    from agentos.tools.builtin.media import configure_audio, configure_image_generation

    configure_image_generation(
        getattr(config, "image_generation", None),
        llm_config=getattr(config, "llm", None),
        agentos_router_config=getattr(config, "agentos_router", None),
    )
    configure_audio(getattr(config, "audio", None))
    configure_auxiliary(
        getattr(config, "auxiliary", None),
        llm_config=getattr(config, "llm", None),
    )


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


def sync_x_search(config: Any) -> None:
    from agentos.tools.builtin.x_search import configure_x_search

    configure_x_search(getattr(config, "x_search", None))


def sync_browser(config: Any) -> None:
    from agentos.tools.builtin.browser import configure_browser

    configure_browser(getattr(config, "browser", None))


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
    if _touches(changed_paths, {"x_search"}):
        steps.append(("x_search", sync_x_search))
    if _touches(changed_paths, {"browser"}):
        steps.append(("browser", sync_browser))
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


def _refuse_agent_config_change(
    ctx: Any, source: Any, new_config: Any, changed_paths: Iterable[str] | None
) -> None:
    """The one gate every config writer passes: no agent sets the operator's settings.

    ``config.set`` / ``config.patch`` refused the trading limits by the path
    the caller *named*, and ``config.apply`` -- the full-replace sibling --
    did not refuse them at all. The check here runs on what actually changes
    (the caller's list plus a diff of the two configs), for every RPC writer:
    set, patch, apply, onboarding, heartbeat and whatever comes next.
    ``trading`` is refused for any binding, unbound included; the other
    operator-only roots for a connection the gateway recognizes as an agent's.
    """
    from agentos.gateway.agent_surface import agent_binding

    if agent_binding(ctx) is None:
        return
    paths = {str(path) for path in (changed_paths or ())}
    dump_old = getattr(source, "model_dump", None)
    dump_new = getattr(new_config, "model_dump", None)
    if callable(dump_old) and callable(dump_new):
        from agentos.gateway.rpc_config import diff_paths

        paths |= diff_paths(dump_old(mode="python"), dump_new(mode="python"))
    trading = sorted(p for p in paths if p == "trading" or p.startswith("trading."))
    if trading:
        raise ValueError(
            "trading settings are the user's to change; an agent cannot set " + ", ".join(trading)
        )
    refuse_agent_operator_only_change(ctx, paths)


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
    _refuse_agent_config_change(ctx, source, new_config, changed_paths)
    has_existing_runtime = running_config is not None or source_config is not None
    if new_config.auth.mode == "token" and not new_config.auth.token:
        source_had_missing_token_mode = (
            has_existing_runtime and source.auth.mode == "token" and not source.auth.token
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
        source.auth.trusted_proxy,
    )
    candidate_posture = (
        new_config.host,
        new_config.auth.mode,
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


class ConfigReloadError(ValueError):
    """The file on disk cannot become the running config (it stays as it was)."""


#: Settings that are the operator's alone: the trading guardrails, commands
#: the gateway itself runs (``hooks``), the tool policy (allow/deny, the HTTP
#: ``tools.invoke`` surface, SSRF exceptions), the default elevation, the
#: sandbox, authentication, the prompt-injection guards and the workspace
#: confinement. An agent-bound connection may not write them through any
#: config RPC (:func:`refuse_agent_operator_only_change`), and an edit of the
#: config file -- which an agent shell can reach on a host without the OS
#: sandbox -- does not go live by itself (:func:`reload_config_from_disk`).
OPERATOR_ONLY_CONFIG_ROOTS: tuple[str, ...] = (
    "trading",
    "hooks",
    "tools",
    "permissions",
    "sandbox",
    "auth",
    "safety",
    "workspace_dir",
    "workspace_strict",
)


def _root_of(path: str) -> str | None:
    for root in OPERATOR_ONLY_CONFIG_ROOTS:
        if path == root or path.startswith(f"{root}."):
            return root
    return None


def operator_only_paths(paths: Iterable[str]) -> list[str]:
    """The paths in *paths* that touch an operator-only root (or contain one)."""
    touched: set[str] = set()
    for path in paths:
        text = str(path)
        if _root_of(text) is not None:
            touched.add(text)
        elif not text or any(root.startswith(f"{text}.") for root in OPERATOR_ONLY_CONFIG_ROOTS):
            touched.add(text)
    return sorted(touched)


def refuse_agent_operator_only_change(ctx: Any, changed_paths: Iterable[str]) -> None:
    """Refuse a config write from a connection the gateway recognizes as an agent's.

    Applied to the *computed* diff of every config writer (``commit_config``),
    so a full-replace (``config.apply``), a merge patch or an onboarding
    mutation cannot carry what a ``config.set`` of the same path would be
    refused. ``trading`` is refused for an unbound connection too
    (``rpc_config._refuse_agent_trading_paths``).
    """
    from agentos.gateway.agent_surface import recognized_agent

    if recognized_agent(ctx) is None:
        return
    touched = operator_only_paths(changed_paths)
    if touched:
        raise ValueError(
            "these settings are the user's to change; an agent cannot set " + ", ".join(touched)
        )


@dataclass(frozen=True)
class ConfigReloadResult:
    """Outcome of applying an external edit of the config file."""

    path: Path
    revision: str
    changed_paths: tuple[str, ...]
    hot_paths: tuple[str, ...]
    restart_paths: tuple[str, ...]
    restart_reasons: tuple[str, ...]
    applied_now: bool
    #: Operator-only paths the file changed that were *not* applied: they wait
    #: for ``config.reload`` from the operator, or a restart.
    held_paths: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.changed_paths)

    @property
    def held(self) -> bool:
        return bool(self.held_paths)


def _set_dotted_attr(target: Any, path: str, value: Any) -> None:
    parts = path.split(".")
    node = target
    for part in parts[:-1]:
        node = getattr(node, part)
    setattr(node, parts[-1], value)


def _get_dotted_attr(target: Any, path: str) -> Any:
    node = target
    for part in path.split("."):
        node = getattr(node, part)
    return node


def _leaf_paths(paths: set[str]) -> set[str]:
    return {
        path
        for path in paths
        if not any(other.startswith(f"{path}.") for other in paths if other != path)
    }


def reload_config_from_disk(
    ctx: RpcContext, *, include_operator_only: bool = False
) -> ConfigReloadResult:
    """Make an edit of the active config file the running config.

    The file was written by someone else -- an editor, a deploy script -- so
    unlike :func:`commit_config` nothing is persisted: rewriting it would
    discard the operator's comments and layout. Everything after the write is
    the same: the same validation, the same public-bind safety guard, the same
    in-place update and hot adapters, and the same restart bookkeeping. Values
    the process holds that the file does not -- ``--listen``/``--port``
    overrides, an auto-generated token, env-sourced secrets -- carry over.

    "Someone else" may also be an agent's shell: on a host without the OS
    sandbox the file is a same-user write away, and the config RPC refuses
    an agent the same settings. So unless *include_operator_only* (the
    operator's ``config.reload``), changes under
    :data:`OPERATOR_ONLY_CONFIG_ROOTS` are not applied, hot or otherwise: the
    running values stay and the paths come back as ``held_paths``. A restart
    reads the file as the operator's, as it always has.

    Raises :class:`ConfigReloadError` when the file cannot be read, parsed or
    validated; the running config is untouched then.
    """
    from pydantic import ValidationError

    from agentos.gateway.config import GatewayConfig, enforce_public_bind_auth_guard
    from agentos.gateway.config_migration import migrate_config_payload
    from agentos.gateway.config_persist import get_runtime_overrides
    from agentos.gateway.rpc_config import (
        _bind_restart_fingerprint,
        _boot_runtime_restart_fingerprints,
        _channels_restart_fingerprint,
        _has_unproven_live_change,
        _memory_restart_fingerprint,
        _restart_reasons,
        _sandbox_posture_restart_fingerprint,
        diff_paths,
    )

    running = getattr(ctx, "config", None)
    path = _resolved_config_path(running)
    if running is None or path is None:
        raise ConfigReloadError("no active config file")
    try:
        text = path.read_bytes().decode("utf-8")
        raw = tomllib.loads(text)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigReloadError(f"cannot read {path.name}: {exc}") from exc
    try:
        migrated = migrate_config_payload(raw).payload
        new_config = GatewayConfig(**migrated)
        new_config.mark_env_sourced_auth_secrets(migrated)
        _assert_payload_is_modeled(migrated, new_config)
    except (ValidationError, ValueError, TypeError) as exc:
        raise ConfigReloadError(_short_error(exc)) from exc
    new_config.config_path = str(path)
    # What the CLI set for this process (``--listen``, ``--port``, ``--debug``)
    # keeps winning over the file, exactly as it did at boot.
    for override_path in get_runtime_overrides():
        try:
            _set_dotted_attr(new_config, override_path, _get_dotted_attr(running, override_path))
        except AttributeError:
            continue
    # Runtime-only secrets the file does not carry (an auto-generated token,
    # an env-sourced key) stay; the file can still set its own value.
    new_config.inherit_runtime_secrets(running)
    for secret_path in sorted(getattr(running, "_runtime_secret_paths", ())):
        if _dotted_path_present(migrated, secret_path):
            new_config.clear_runtime_secret(secret_path)
            continue
        try:
            _set_dotted_attr(new_config, secret_path, _get_dotted_attr(running, secret_path))
        except (AttributeError, ValidationError):
            continue

    old_payload = running.model_dump(mode="python")
    changed = diff_paths(old_payload, new_config.model_dump(mode="python"))
    held: list[str] = []
    if not include_operator_only:
        held = sorted(p for p in _leaf_paths(changed) if _root_of(p) is not None)
        # Keep the running value of every operator-only root the file touched;
        # the rest of the edit goes ahead as before.
        for root in sorted({cast(str, _root_of(p)) for p in held}):
            _set_dotted_attr(new_config, root, copy.deepcopy(_get_dotted_attr(running, root)))
        if held:
            log.warning(
                "config.reload_held_operator_only paths=%s; apply with `agentos config reload` "
                "as the operator, or restart the gateway",
                ",".join(held),
            )
            changed = diff_paths(old_payload, new_config.model_dump(mode="python"))
    leaves = _leaf_paths(changed)
    if not leaves:
        return ConfigReloadResult(
            path=path,
            revision=config_revision(running),
            changed_paths=(),
            hot_paths=(),
            restart_paths=(),
            restart_reasons=(),
            applied_now=True,
            held_paths=tuple(held),
        )

    if new_config.auth.mode == "token" and not new_config.auth.token:
        raise ConfigReloadError("auth.mode='token' requires a token")
    posture = (running.host, running.auth.mode, running.auth.trusted_proxy)
    if posture != (new_config.host, new_config.auth.mode, new_config.auth.trusted_proxy):
        try:
            enforce_public_bind_auth_guard(new_config)
        except ValueError as exc:
            raise ConfigReloadError(str(exc)) from exc

    reasons = set(
        _restart_reasons(
            old_memory_fingerprint=_memory_restart_fingerprint(running),
            old_channels_fingerprint=_channels_restart_fingerprint(running),
            old_sandbox_posture_fingerprint=_sandbox_posture_restart_fingerprint(running),
            old_bind_fingerprint=_bind_restart_fingerprint(running),
            old_boot_runtime_fingerprints=_boot_runtime_restart_fingerprints(running),
            changed_paths=changed,
            new_config=new_config,
        )
    )
    # Per path: hot only when a live consumer or hot adapter is audited for
    # it (the same table config RPC writes use); anything else, including an
    # unknown key, waits for a restart.
    restart_paths = sorted(p for p in leaves if _has_unproven_live_change({p}))
    hot_paths = sorted(leaves - set(restart_paths))
    if restart_paths and not reasons:
        reasons.add("config")

    update_config_in_place(running, new_config)
    applied_now = True
    for name, apply_step in _runtime_steps(ctx, changed):
        try:
            apply_step(running)
        except Exception:
            log.exception("config hot apply failed adapter=%s", name)
            applied_now = False
            reasons.add(f"hot_apply:{name}")
    pending = _record_pending_restart(running, reasons)
    return ConfigReloadResult(
        path=path,
        revision=config_revision(running),
        changed_paths=tuple(sorted(leaves)),
        hot_paths=tuple(hot_paths),
        restart_paths=tuple(restart_paths),
        restart_reasons=pending,
        applied_now=applied_now,
        held_paths=tuple(held),
    )


def _short_error(exc: BaseException) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            first = errors()[0]
            loc = ".".join(str(part) for part in first.get("loc", ()))
            return f"{loc}: {first.get('msg', 'invalid value')}" if loc else str(first.get("msg"))
        except (IndexError, TypeError):
            pass
    return str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__


__all__ = [
    "OPERATOR_ONLY_CONFIG_ROOTS",
    "ConfigCommitResult",
    "ConfigReloadError",
    "ConfigReloadResult",
    "reload_config_from_disk",
    "ConfigDiskState",
    "commit_config",
    "config_disk_state",
    "config_revision",
    "expected_revision",
    "operator_only_paths",
    "pending_restart_metadata",
    "refuse_agent_operator_only_change",
    "sync_media",
    "sync_provider_selector",
    "sync_search",
    "update_config_in_place",
]
