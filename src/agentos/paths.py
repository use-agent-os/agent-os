"""AgentOS state-root resolution.

Single source of truth for the on-disk state root. One env var controls
the root, and every subsystem derives its sub-path from the helper here.

Precedence:
1. ``AGENTOS_STATE_DIR`` environment variable (expanded for ``~``/``$HOME``)
2. ``$HOME/.agentos``
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _home_dir() -> Path:
    home = os.environ.get("HOME", "").strip()
    if home:
        return Path(home).expanduser()
    return Path.home()


def _expand_user(path: str) -> Path:
    if path == "~":
        return _home_dir()
    if path.startswith("~/") or path.startswith("~\\"):
        return _home_dir() / path[2:]
    return Path(path).expanduser()


def default_agentos_home() -> Path:
    """Return the AgentOS state root as an absolute :class:`~pathlib.Path`.

    Honors ``AGENTOS_STATE_DIR`` (trimmed, ``~`` expanded). Falls back to
    ``$HOME/.agentos`` when unset or empty.
    """
    override = os.environ.get("AGENTOS_STATE_DIR", "").strip()
    if override:
        return _expand_user(override)
    return _home_dir() / ".agentos"


def vault_kek_dir() -> Path:
    """Where the key that seals the vault's ``unlock.key`` lives.

    Deliberately outside the state root, so one copy or recursive read of it
    does not carry both the sealed password and the key that opens it
    (``agentos.trading.unlock_key``). ``AGENTOS_VAULT_KEK_DIR`` overrides it.
    """
    override = os.environ.get("AGENTOS_VAULT_KEK_DIR", "").strip()
    if override:
        return _expand_user(override)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA", "").strip()
        root = Path(base) if base else _home_dir() / "AppData" / "Local"
        return root / "AgentOS" / "vault-keys"
    if sys.platform == "darwin":
        return _home_dir() / "Library" / "Application Support" / "AgentOS" / "vault-keys"
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    root = Path(xdg) if xdg and Path(xdg).is_absolute() else _home_dir() / ".local" / "state"
    return root / "agentos" / "vault-keys"


def state_dir(*parts: str) -> Path:
    """Return a path under AgentOS's state directory.

    ``default_agentos_home()`` is the user-visible AgentOS home. Runtime state
    lives in the ``state`` subdirectory below it, matching the gateway config
    default and keeping prompt history out of the config/env root.
    """
    return default_agentos_home() / "state" / Path(*parts)


def media_root_from_config(config: object | None = None) -> Path:
    """Return the stable attachment/artifact media root.

    Explicit ``attachments.media_root`` wins. Otherwise derive from the configured
    AgentOS home instead of process cwd so artifact links keep working when the
    gateway is launched from a long or transient source/worktree path.
    """
    attachments_cfg = getattr(config, "attachments", None)
    media_root = getattr(attachments_cfg, "media_root", None)
    if isinstance(media_root, str) and media_root.strip():
        return _expand_user(media_root.strip())

    state_root = getattr(config, "state_dir", None)
    if isinstance(state_root, str) and state_root.strip():
        state_path = _expand_user(state_root.strip())
        return state_path.parent / "media"

    config_path = getattr(config, "config_path", None)
    if isinstance(config_path, str) and config_path.strip():
        return _expand_user(config_path.strip()).parent / "media"

    return default_agentos_home() / "media"
