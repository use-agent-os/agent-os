"""Unified .env file loader — single source of truth for API keys.

Precedence (highest to lowest):
1. os.environ (already set by shell / CI)
2. .env in current working directory
3. ~/.agentos/.env (global user config)

Existing environment variables are NEVER overridden.

The working directory's ``.env`` cannot set a name ``agentos.env_policy``
protects (loader and interpreter variables, AgentOS posture and state
location, the operator-only config roots) or one that steers the runtime
(``PYTHON*``, the signer's crypto backends, TLS trust):
that directory is whatever the gateway was started from, often writable by an
agent's shell, and the values would land in the process that holds the vault
keys. ``~/.agentos/.env`` -- the operator's own file, on the sensitive-path
floor -- is read as before.
"""

from __future__ import annotations

import os
from pathlib import Path

import structlog

from agentos.paths import default_agentos_home

log = structlog.get_logger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}
_PROXY_ENV_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")


def trust_env() -> bool:
    """Return True when agentos's httpx clients should honor env proxy/TLS vars.

    Gated by ``AGENTOS_TRUST_ENV``. Off by default — agentos defaults to
    deterministic, env-isolated networking so a stray HTTP_PROXY in a parent
    shell cannot silently reroute agent traffic. Set ``AGENTOS_TRUST_ENV=1``
    (e.g. in ~/.agentos/.env) to opt in; required on WSL2 / corporate networks
    where the only route to external APIs is a shell-exported proxy.
    """
    return os.environ.get("AGENTOS_TRUST_ENV", "").strip().lower() in _TRUTHY


def warn_if_proxy_ignored() -> None:
    """Log a one-time hint if env has HTTP(S)_PROXY but trust_env is off."""
    if trust_env():
        return
    present = [v for v in _PROXY_ENV_VARS if os.environ.get(v)]
    if present:
        log.warning(
            "env.proxy_ignored",
            vars=present,
            hint="Set AGENTOS_TRUST_ENV=1 to let agentos honor env proxy settings.",
        )


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a .env file into a dict. Skips comments and blank lines.

    Both ``KEY=value`` and the bash-compatible ``export KEY=value`` are
    recognized. Supporting ``export`` is not cosmetic: an operator who wrote
    ``export GITHUB_TOKEN=...`` by hand must see that variable as *set*
    everywhere AgentOS reports on it, and the writer in
    :mod:`agentos.env_store` must be able to find and replace that same line
    rather than appending a second, competing definition.

    Values are read literally — one pair of surrounding quotes is removed and
    nothing is unescaped. :func:`agentos.env_store.set_env_var` quotes what it
    writes to match, so a written value parses back unchanged; hand-written
    files keep whatever meaning they already had.
    """
    if not path.is_file():
        return {}
    entries: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip surrounding quotes
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            entries[key] = value
    return entries


def parse_env_file(path: Path) -> dict[str, str]:
    """Public wrapper over :func:`_parse_env_file` for out-of-module readers.

    Anything that needs to know what a ``.env`` file *would* inject on the next
    start should go through here, so the reader stays a single implementation.
    """
    return _parse_env_file(path)


def _same_file(left: Path, right: Path) -> bool:
    try:
        return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))
    except OSError:
        return False


def load_env(cwd: str | Path | None = None) -> int:
    """Load .env files into os.environ with precedence rules.

    Returns the number of new variables injected.
    """
    from agentos.env_policy import is_protected_name, is_runtime_steering_name

    candidates: list[tuple[Path, bool]] = []

    # 1. cwd/.env (or cwd/.env.test as alias for dev)
    work_dir = Path(cwd) if cwd else Path.cwd()
    for name in (".env", ".env.test"):
        candidates.append((work_dir / name, True))

    # 2. ~/.agentos/.env (global)
    home_env = default_agentos_home() / ".env"
    candidates.append((home_env, False))

    # Merge: first file wins per key, but os.environ always wins
    merged: dict[str, str] = {}
    for path, project_local in candidates:
        if project_local and _same_file(path, home_env):
            project_local = False
        for key, value in _parse_env_file(path).items():
            if project_local and (is_protected_name(key) or is_runtime_steering_name(key)):
                log.warning(
                    "env.project_env_name_ignored",
                    key=key,
                    source=str(path),
                    hint="set it in ~/.agentos/.env or export it before starting AgentOS",
                )
                continue
            if key not in merged:
                merged[key] = value
                log.debug("env.loaded", key=key, source=str(path))

    # Inject into os.environ — never override existing
    injected = 0
    for key, value in merged.items():
        if key not in os.environ:
            os.environ[key] = value
            injected += 1

    if injected:
        log.info("env.injected", count=injected)

    return injected
