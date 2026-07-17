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


def persist_config(
    config: Any,
    *,
    explicit_paths: set[str] | None = None,
) -> None:
    """Write config to TOML atomically (0600), restoring any runtime overrides.

    The process-global runtime-override map (see :func:`set_runtime_overrides`)
    maps each CLI/break-glass-overridden dotted path to the value that was on
    disk before the override. Each such field is restored to its original value
    (or dropped when the original was ``None``) so a transient
    ``--listen``/``--debug``/break-glass posture never persists — UNLESS the
    path is in ``explicit_paths``, meaning the user explicitly changed it this
    write and the new value must persist verbatim.
    """
    from agentos.onboarding.config_store import persist_config as _store_persist

    explicit = explicit_paths or set()
    data = config.to_toml_dict()
    for path, original in _RUNTIME_OVERRIDES.items():
        if path in explicit:
            continue
        if original is None:
            _drop_dotted(data, path)
        else:
            _set_dotted(data, path, original)

    _store_persist(
        cast("GatewayConfig", _TomlDictConfig(data)),
        path=getattr(config, "config_path", None),
        backup=True,
    )


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
