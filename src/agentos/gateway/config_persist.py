"""Shared config persistence — the single TOML write path for live writers.

Every live writer (the config RPC surface ``rpc_config``, the onboarding RPC
surface ``rpc_onboarding``, and the CLI auth-provisioning prompt) persists
:class:`~agentos.gateway.config.GatewayConfig` through :func:`persist_config`.
It delegates the actual write to
:func:`agentos.onboarding.config_store.persist_config`, so every live writer
shares one atomic + ``0600`` contract (temp file + ``os.replace`` + ``chmod``);
a freshly generated bearer token is never world-readable.

**Provenance, not field names.** ``run_gateway`` and break-glass inject one-off
overrides (``--bind`` / ``--port`` / ``--debug``; break-glass ``auth.mode`` /
``allow_unauthenticated_public``) into the in-memory config, which then flows
into ``ctx.config`` for every RPC. Writing those wholesale would freeze a
one-off ``--listen 0.0.0.0 --debug`` into ``config.toml``. So at boot the CLI
records the ON-DISK original values of exactly those fields in a process-global
override map via :func:`set_runtime_overrides`, and every ``persist_config``
call restores each recorded field to its original on-disk value (or drops the
field when the original was ``None`` — it was absent, so the load-time default
applies) — UNLESS that exact dotted path is named in ``explicit_paths``,
meaning the user explicitly changed it this write and the new value must
persist. A genuine config edit (UI "Save" via ``config.patch``/``apply``, or an
onboarding mutation) passes ``explicit_paths`` for what it actually changed, so
it is never silently discarded.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path

    from agentos.gateway.config import GatewayConfig

# Process-global map recorded at boot by ``run_gateway`` (and augmented by the
# break-glass prompt). Keys are dotted paths (``host``, ``port``, ``debug``,
# ``auth.mode``, ``auth.allow_unauthenticated_public``); values are the original
# on-disk value, or ``None`` meaning "was absent -> drop so the default applies".
_RUNTIME_OVERRIDES: dict[str, Any] = {}


def set_runtime_overrides(mapping: dict[str, Any] | None) -> None:
    """Record (or clear) the boot-time runtime override map.

    Called once by ``run_gateway`` before it overrides ``host``/``port``/
    ``debug`` in memory, and augmented by the break-glass prompt when it forces
    ``auth.mode=none``. ``None`` clears the map (used by tests and by a plain
    loopback run that overrode nothing).
    """
    global _RUNTIME_OVERRIDES
    _RUNTIME_OVERRIDES = dict(mapping) if mapping else {}


def get_runtime_overrides() -> dict[str, Any]:
    """Return a copy of the current runtime override map."""
    return dict(_RUNTIME_OVERRIDES)


def _clear_committed_runtime_overrides(explicit_paths: set[str] | None) -> None:
    """Forget boot overrides superseded by an explicit persisted edit."""
    if not explicit_paths:
        return
    for override_path in tuple(_RUNTIME_OVERRIDES):
        if any(
            explicit == override_path
            or explicit.startswith(f"{override_path}.")
            or override_path.startswith(f"{explicit}.")
            for explicit in explicit_paths
        ):
            _RUNTIME_OVERRIDES.pop(override_path, None)


def read_raw_bind_overrides(config_path: str | None) -> dict[str, Any]:
    """Return the RAW on-disk ``host``/``port``/``debug`` for the override map.

    ``run_gateway`` must record the values that are literally in ``config.toml``,
    NOT the env-merged effective values that ``GatewayConfig.load`` produces
    (``env_prefix="AGENTOS_GATEWAY_"`` merges ``AGENTOS_GATEWAY_HOST`` etc.).
    An env-supplied bind posture is itself transient — env vars are per-invocation
    — so recording the env value as the "on-disk original" would let a later
    ``config.patch`` freeze it into the file. A key absent from the TOML maps to
    ``None`` so ``persist_config`` drops it and the load-time default/env applies
    at the next boot. A missing/unreadable file yields all-``None`` (nothing to
    restore beyond the defaults).
    """
    keys = ("host", "port", "debug")
    result: dict[str, Any] = {key: None for key in keys}
    if not config_path:
        return result
    try:
        import tomllib

        with open(config_path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):
        # ValueError covers tomllib.TOMLDecodeError (a subclass); a malformed or
        # unreadable file leaves every original None so only defaults apply.
        return result
    if not isinstance(data, dict):
        return result
    for key in keys:
        if key in data:
            result[key] = data[key]
    return result


def _set_dotted(data: dict, path: str, value: Any) -> None:
    keys = path.split(".")
    node = data
    for key in keys[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[keys[-1]] = value


def _drop_dotted(data: dict, path: str) -> None:
    keys = path.split(".")
    node: Any = data
    for key in keys[:-1]:
        node = node.get(key)
        if not isinstance(node, dict):
            return
    node.pop(keys[-1], None)


def prepare_persist_payload(
    config: Any,
    *,
    explicit_paths: set[str] | None = None,
) -> dict[str, Any]:
    """Return the exact semantic payload a live writer would persist."""
    explicit = explicit_paths or set()
    data = cast(dict[str, Any], config.to_toml_dict())
    for path, original in _RUNTIME_OVERRIDES.items():
        if path in explicit:
            continue
        if original is None:
            _drop_dotted(data, path)
        else:
            _set_dotted(data, path, original)
    return data


def persist_config(
    config: Any,
    *,
    explicit_paths: set[str] | None = None,
) -> Path:
    """Write config to TOML atomically (0600), restoring any runtime overrides.

    The process-global runtime-override map (see :func:`set_runtime_overrides`)
    maps each CLI/break-glass-overridden dotted path to the value that was on
    disk before the override. Each such field is restored to its original value
    (or dropped when the original was ``None``) so a transient
    ``--listen``/``--debug``/break-glass posture never persists — UNLESS the
    path is in ``explicit_paths``, meaning the user explicitly changed it this
    write and the new value must persist verbatim.

    Returns the resolved file path the write landed on. On a true first run the
    caller's ``config.config_path`` is ``None`` and the onboarding writer
    resolves the home path internally; that resolved path is returned AND
    back-filled onto ``config.config_path`` so callers can report where the
    token was saved instead of "None".
    """
    from agentos.onboarding.config_store import persist_config as _store_persist

    data = prepare_persist_payload(config, explicit_paths=explicit_paths)

    result = _store_persist(
        cast("GatewayConfig", _TomlDictConfig(data)),
        path=getattr(config, "config_path", None),
        backup=True,
    )
    # Once an explicitly edited boot-override field is durable, its old
    # provenance is stale. Clearing it only after the atomic writer succeeds
    # prevents a later unrelated save from restoring the pre-edit value.
    _clear_committed_runtime_overrides(explicit_paths)
    # Back-fill the resolved path so a first-run caller (config_path was None)
    # can name the real file, and subsequent writes target the same one.
    if not getattr(config, "config_path", None):
        try:
            config.config_path = str(result.path)
        except (AttributeError, TypeError):
            pass
    return result.path


class _TomlDictConfig:
    """Adapter so a plain (override-corrected) TOML dict can be persisted
    through the onboarding writer, which re-serializes and re-validates via
    ``GatewayConfig``; we hand it the already-corrected dict via
    ``to_toml_dict`` and let it validate + write.
    """

    __slots__ = ("_data",)

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def to_toml_dict(self) -> dict[str, Any]:
        return self._data
