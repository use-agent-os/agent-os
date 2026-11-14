"""Gateway credentials that arrive through the environment, and where they go.

The shipped ``compose.yaml`` (and the desktop, for a token-mode gateway) hand
the gateway its token as ``AGENTOS_AUTH_TOKEN``. ``GatewayConfig`` reads it
once at load; after that the variable has no reader in this process, but it
was still sitting in ``os.environ`` -- inherited by every child that copied
the environment (a skill dependency install, an ACP harness, anything a
library spawns) unless that child's spawner remembered to filter it.

:func:`scrub_credential_env` runs at boot, after the config is loaded, and
removes every name in :data:`agentos.env_policy.GATEWAY_CREDENTIAL_ENV` from
the process environment. The values are kept in memory for exactly one
consumer: an in-place restart re-executes this process with a copy of its
environment (``gateway/restart.py``), and the new image needs the token it
was started with. The desktop's operator secret is not kept here; it travels
to a re-exec through its own 0600 file (``agent_surface``).

``/proc/<pid>/environ`` still shows the environment the process was started
with; that is why a token held by an agent is not, on its own, a way to the
operator binding (``agent_surface`` refuses rule 1b from this host's own
addresses).
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping

import structlog

from agentos.env_policy import GATEWAY_CREDENTIAL_ENV

log = structlog.get_logger(__name__)

#: The desktop's secret is scrubbed (and re-materialized for a re-exec) by
#: ``AgentSurface.load_operator_secret_from_env``; it is never kept here.
_HANDLED_ELSEWHERE = frozenset({"AGENTOS_OPERATOR_SECRET", "AGENTOS_OPERATOR_SECRET_FILE"})

_scrubbed: dict[str, str] = {}


def scrub_credential_env(environ: MutableMapping[str, str] | None = None) -> tuple[str, ...]:
    """Remove the gateway's credentials from *environ* (default: ``os.environ``).

    Returns the names removed. Only the names are logged, never a value.
    Calling it again (a test, a second boot in one process) adds to what is
    kept for a re-exec rather than forgetting it.
    """
    env = os.environ if environ is None else environ
    removed: list[str] = []
    for name in sorted(GATEWAY_CREDENTIAL_ENV - _HANDLED_ELSEWHERE):
        value = env.pop(name, None)
        if value is None:
            continue
        _scrubbed[name] = value
        removed.append(name)
    if removed:
        log.info("gateway.credential_env_scrubbed", names=removed)
    return tuple(removed)


def credential_env_for_reexec() -> dict[str, str]:
    """The scrubbed credentials, for the environment of an in-place re-exec."""
    return dict(_scrubbed)


def reset_scrubbed_credential_env() -> None:
    """Forget what was scrubbed. For tests."""
    _scrubbed.clear()


__all__ = [
    "credential_env_for_reexec",
    "reset_scrubbed_credential_env",
    "scrub_credential_env",
]
