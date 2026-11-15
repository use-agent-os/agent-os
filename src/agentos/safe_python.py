"""Launch AgentOS's own Python processes without an agent-writable import path.

``python -m agentos.cli.main`` and ``python -c ...`` put the current working
directory at ``sys.path[0]``, ahead of site-packages. The gateway's working
directory is usually writable by the same OS user an agent's shell runs as
(``$HOME`` for the shipped service units, the launch directory for
``agentos gateway run``), so a file named like a module the gateway imports --
``eth_account.py`` on the signing path -- would run inside the process that
holds the decrypted vault keys. ``-P`` (Python 3.11+) keeps that directory off
``sys.path``; the flag is not inherited through the environment, so the
scripts and tools AgentOS later runs for the agent behave as before.

The environment is the other way in: ``agentos.env.load_env`` merges ``.env``
files into ``os.environ`` after the interpreter has started, so a
``PYTHONPATH`` that did not shape this process's ``sys.path`` would shape the
next one's. :func:`launch_env` hands a child exactly the ``PYTHON*`` variables
this process was launched with, so a restart or a spawned gateway imports
from the same places the first launch did.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping

#: Interpreter flag that keeps the script directory / cwd off ``sys.path``.
SAFE_PATH_FLAG = "-P"


def _python_vars(env: Mapping[str, str]) -> dict[str, str]:
    return {key: value for key, value in env.items() if key.upper().startswith("PYTHON")}


# Captured at import; ``agentos.cli.main`` also calls :func:`capture_launch_env`
# explicitly before its ``load_env()``, which is the moment that matters.
_LAUNCH_PYTHON_ENV: dict[str, str] = _python_vars(os.environ)
_captured = False


def capture_launch_env() -> None:
    """Record the launch-time ``PYTHON*`` variables (first call wins)."""
    global _LAUNCH_PYTHON_ENV, _captured
    if _captured:
        return
    _LAUNCH_PYTHON_ENV = _python_vars(os.environ)
    _captured = True


def python_argv(*args: str) -> list[str]:
    """``[sys.executable, "-P", *args]``: this interpreter, no cwd on ``sys.path``."""
    return [sys.executable, SAFE_PATH_FLAG, *args]


def launch_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """*env* (default ``os.environ``) with the launch-time ``PYTHON*`` variables.

    Every ``PYTHON*`` name is dropped and the ones this process started with
    are put back, so nothing a ``.env`` file or an env write added since can
    steer the child interpreter's import path or startup.
    """
    source = dict(os.environ if env is None else env)
    cleaned = {key: value for key, value in source.items() if not key.upper().startswith("PYTHON")}
    cleaned.update(_LAUNCH_PYTHON_ENV)
    return cleaned


__all__ = ["SAFE_PATH_FLAG", "capture_launch_env", "launch_env", "python_argv"]
