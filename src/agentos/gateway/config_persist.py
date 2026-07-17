"""Shared config persistence — the single TOML write path.

Both the config RPC surface (``rpc_config``) and the CLI auth provisioning
prompt persist :class:`~agentos.gateway.config.GatewayConfig` through this
helper so the on-disk representation is produced identically everywhere.

**Runtime-only fields never persist from the in-memory config.** ``host``,
``port``, ``debug`` and ``auth.allow_unauthenticated_public`` are set for a
single run (CLI ``--bind`` / ``--port`` / ``--debug`` flags, break-glass) and
are deliberately *not* written back — the in-memory config carries a one-off
override that must not be frozen into ``config.toml`` (PR #25 review). To
change them permanently, edit ``config.toml`` directly. Every writer routes
through here, so this one rule keeps bind posture CLI-only across the CLI
prompt and all config RPCs.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from agentos.paths import default_agentos_home

# Dot-paths whose in-memory value is treated as a one-off runtime override:
# never written from the live config, always taken from the existing on-disk
# file (or dropped, so load-time defaults apply, when no file exists).
_RUNTIME_ONLY_PATHS: tuple[tuple[str, ...], ...] = (
    ("host",),
    ("port",),
    ("debug",),
    ("auth", "allow_unauthenticated_public"),
)


def _existing_value(on_disk: dict, path: tuple[str, ...]) -> tuple[bool, Any]:
    node: Any = on_disk
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return (False, None)
        node = node[key]
    return (True, node)


def _apply_runtime_only_rule(data: dict, on_disk: dict) -> None:
    """In-place: replace each runtime-only field with its on-disk value, or
    drop it entirely when the file did not have one (defaults apply on reload)."""
    for path in _RUNTIME_ONLY_PATHS:
        present, value = _existing_value(on_disk, path)
        node = data
        for key in path[:-1]:
            child = node.get(key)
            if not isinstance(child, dict):
                child = {}
                node[key] = child
            node = child
        leaf = path[-1]
        if present:
            node[leaf] = value
        else:
            node.pop(leaf, None)


def persist_config(config: Any) -> None:
    """Write config to TOML, defaulting to the user config path when unset.

    Runtime-only fields (see module docstring) are excluded — their on-disk
    value is preserved and the live override is discarded.
    """
    if not getattr(config, "config_path", None) and hasattr(config, "config_path"):
        config.config_path = str(default_agentos_home() / "config.toml")

    if not getattr(config, "config_path", None):
        return

    import tomli_w  # TOML writer (tomllib is read-only)

    path = Path(config.config_path)
    on_disk: dict = {}
    if path.exists():
        try:
            with open(path, "rb") as f:
                on_disk = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            on_disk = {}

    data = config.to_toml_dict()
    _apply_runtime_only_rule(data, on_disk)

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        tomli_w.dump(data, f)
