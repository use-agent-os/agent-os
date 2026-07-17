"""Shared config persistence — the single TOML write path.

Both the config RPC surface (``rpc_config``) and the CLI auth provisioning
prompt persist :class:`~agentos.gateway.config.GatewayConfig` through this
helper so the on-disk representation is produced identically everywhere.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agentos.paths import default_agentos_home


def persist_config(config: Any) -> None:
    """Write config to TOML, defaulting to the user config path when unset."""
    if not getattr(config, "config_path", None) and hasattr(config, "config_path"):
        config.config_path = str(default_agentos_home() / "config.toml")

    if not getattr(config, "config_path", None):
        return

    import tomli_w  # TOML writer (tomllib is read-only)

    path = Path(config.config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        tomli_w.dump(config.to_toml_dict(), f)
