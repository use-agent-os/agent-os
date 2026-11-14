"""What of AgentOS's own environment a child process is allowed to see.

Every subprocess AgentOS spawns used to inherit ``os.environ`` verbatim — the
concern :mod:`agentos.env_policy` names in its opening paragraph, from the
other side. That means the provider key AgentOS authenticates with was sitting
in the environment of every ``exec_command`` the model ran, reachable by any
command that prints the environment and by every dependency those commands
load.

Three rules govern what crosses:

* **AgentOS's control surface never crosses.** The gateway token authenticates
  to the control plane, and the guard switches tell a child how the sandbox is
  configured — and let it reconfigure them for the next call. No child has a
  legitimate use for either, so they are stripped unconditionally.
* **Everything else crosses by default.** The user's own ``GITHUB_TOKEN``, AWS
  chain, or ``DOCKER_HOST`` are what makes the shell tool useful, and the local
  shell is the operator's own. Stripping by "does the name look secret" would
  break ``gh``, ``aws`` and ``terraform`` to protect nothing AgentOS owns.
* **Provider credentials cross unless the operator says otherwise.**
  ``AGENTOS_STRIP_PROVIDER_ENV=1`` removes the LLM, search, image, audio and
  embedding keys too. It is opt-in rather than the default because bundled
  skills read those names straight out of ``os.environ`` — see
  ``skills/bundled/seedance-2-prompt/scripts/generate_video.py`` — and their
  config fallback reads the same environment, so a user whose key lives in
  ``~/.agentos/.env`` would find those skills broken with no way to tell why.
  Closing that gap needs ``requires.envAny`` to be parsed into the skill model
  (today it is inert metadata) so the declarations below can cover it; until
  then the default stays where it does not break working installs. The leak
  that mattered most — an ``env`` dump landing in the transcript — is closed
  regardless, by the redaction in :func:`agentos.redact.redact_terminal_output`.

:func:`register_env_passthrough` opens a door in the other direction, for the
*allowlist* sandbox (``execute_code``) where the default is to forward almost
nothing: a skill that declares ``metadata.requires.env`` gets exactly those
names. A skill AgentOS did not ship cannot use it to reach AgentOS's own
credentials — asking for ``AGENTOS_LLM_API_KEY`` is asking to tunnel the
runtime's key into the sandbox that exists to contain untrusted code, and the
request is refused and logged. Bundled skills ship inside the wheel and their
frontmatter was reviewed as ours (the same trust split
:mod:`agentos.skills.publishers` already draws), so they register as trusted.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Mapping

import structlog

from agentos import env_policy
from agentos.tools.types import current_tool_context

log = structlog.get_logger(__name__)

__all__ = [
    "agentos_managed_credentials",
    "build_subprocess_env",
    "clear_env_passthrough",
    "is_env_passthrough",
    "own_cli_dir",
    "register_env_passthrough",
]

#: Keyed by session so a skill loaded in one gateway session cannot widen the
#: environment of another.
#:
#: Deliberately *not* a ContextVar. Every tool call runs in its own asyncio
#: task (``engine/agent.py`` creates one per call), and a task gets a **copy**
#: of the context — so a ``ContextVar.set()`` inside the ``skill_view`` call is
#: invisible to the ``execute_code`` call that follows it. A registration that
#: never reaches the tool it exists to serve is worse than none: the skill
#: looks configured and fails at run time with a missing variable.
_registry: OrderedDict[str, set[str]] = OrderedDict()

#: Bound on remembered sessions. A long-lived gateway would otherwise grow one
#: entry per session forever; the oldest is dropped, which at worst costs a
#: re-view of the skill in a session nobody has touched in a long time.
_MAX_SESSIONS = 256

#: Bucket for entry points that carry no session key — a one-shot CLI run,
#: where there is only one session in the process anyway.
_DEFAULT_SESSION = "\x00default"


def _session_key() -> str:
    ctx = current_tool_context.get()
    key = getattr(ctx, "session_key", None) if ctx is not None else None
    return key or _DEFAULT_SESSION


def _allowed(*, create: bool = True) -> set[str]:
    key = _session_key()
    existing = _registry.get(key)
    if existing is not None:
        _registry.move_to_end(key)
        return existing
    if not create:
        return set()
    value: set[str] = set()
    _registry[key] = value
    while len(_registry) > _MAX_SESSIONS:
        _registry.popitem(last=False)
    return value


#: Names AgentOS reads to decide how much the agent may do, or where its state
#: lives. A child that can see them can be made to change them for the next
#: invocation, and the gateway credentials authenticate to the control plane.
#: The credentials come from :data:`agentos.env_policy.GATEWAY_CREDENTIAL_ENV`
#: (every spelling the gateway reads its token from, the hooks token, the
#: desktop's operator secret); the gateway also drops them from its own
#: environment at boot, and this keeps them out even if a caller planted one in
#: ``extra``. ``AGENTOS_AGENT_TOKEN`` is withheld too: the shell mints a fresh
#: one per call, so one inherited from whoever started the gateway, or passed
#: by the model, must never reach the child.
_RUNTIME_POSTURE_NAMES: frozenset[str] = env_policy.GATEWAY_CREDENTIAL_ENV | frozenset(
    {
        "AGENTOS_AGENT_TOKEN",
        "AGENTOS_SENSITIVE_PATHS_DISABLED",
        "AGENTOS_SENSITIVE_PAYLOAD_DISABLED",
        "AGENTOS_REDACT_SECRETS",
    }
)

#: The generic key an OpenRouter install stores when no provider-specific name
#: applies (``cli/init_cmd.py``). Not in the onboarding catalog, because it is
#: a fallback name rather than a provider's own.
_GENERIC_PROVIDER_NAMES: frozenset[str] = frozenset({"AGENTOS_LLM_API_KEY"})

_managed_cache: frozenset[str] | None = None


def strip_provider_env() -> bool:
    """Return whether provider credentials are stripped from child processes."""
    import os

    return os.environ.get("AGENTOS_STRIP_PROVIDER_ENV", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def agentos_managed_credentials() -> frozenset[str]:
    """Return the names AgentOS authenticates with.

    Derived from the onboarding provider families so a newly supported provider
    is covered the day it is added, rather than the day someone remembers to
    update a second list. These are always refused to an untrusted skill's
    passthrough request; whether they also leave the child environment is the
    operator's call (:func:`strip_provider_env`).

    Fails closed. If the catalog cannot be built the fallback is the
    name-shaped heuristic over the current environment, which covers more than
    necessary — the wrong direction to guess in is the one that leaks.
    """
    global _managed_cache
    if _managed_cache is not None:
        return _managed_cache

    names: set[str] = set(_RUNTIME_POSTURE_NAMES) | set(_GENERIC_PROVIDER_NAMES)
    try:
        from agentos.env_catalog import (
            CATEGORY_AUDIO,
            CATEGORY_IMAGE,
            CATEGORY_MEMORY,
            CATEGORY_PROVIDER,
            CATEGORY_SEARCH,
            _provider_specs,
        )

        managed_categories = {
            CATEGORY_PROVIDER,
            CATEGORY_SEARCH,
            CATEGORY_IMAGE,
            CATEGORY_AUDIO,
            CATEGORY_MEMORY,
        }
        names.update(spec.name for spec in _provider_specs() if spec.category in managed_categories)
    except Exception:
        import os

        log.warning("env_passthrough.catalog_unavailable_failing_closed", exc_info=True)
        names.update(key for key in os.environ if env_policy.is_secret_name(key))
        # Not cached. This branch is a degraded guess taken from whatever the
        # environment happened to hold at one moment; caching it would freeze
        # a transient import failure into the answer for the rest of the
        # process, and miss every variable set afterwards.
        return frozenset(names)

    _managed_cache = frozenset(names)
    return _managed_cache


def reset_managed_credentials_cache() -> None:
    """Drop the cached credential list. For tests and provider reconfiguration."""
    global _managed_cache
    _managed_cache = None


def register_env_passthrough(names: Iterable[str], *, trusted: bool = False) -> list[str]:
    """Allow *names* through to allowlist sandboxes for this session.

    *trusted* is for declarations AgentOS ships itself — bundled skills, whose
    frontmatter went through the same review as the code. Only those may name
    an AgentOS-managed credential; a skill installed from a hub may not,
    whatever it declares.

    Returns the names that were refused, so a caller can tell the user which of
    a skill's declarations did not take effect instead of leaving them to
    discover it as a missing variable at run time.
    """
    refused: list[str] = []
    managed = agentos_managed_credentials()
    allowed = _allowed()
    for raw in names:
        name = str(raw or "").strip()
        if not name or not env_policy.ENV_NAME_RE.match(name):
            continue
        if name in _RUNTIME_POSTURE_NAMES:
            # Not even a bundled skill: these are how the sandbox is steered.
            refused.append(name)
            log.warning("env_passthrough.refused_runtime_posture", name=name)
            continue
        if name in managed and not trusted:
            refused.append(name)
            log.warning("env_passthrough.refused_managed_credential", name=name)
            continue
        allowed.add(name)
    return refused


def is_env_passthrough(name: str) -> bool:
    """Return whether *name* was allowed through for this session."""
    return name in _allowed(create=False)


def clear_env_passthrough(*, all_sessions: bool = False) -> None:
    """Forget registrations for this session, or for every session."""
    if all_sessions:
        _registry.clear()
        return
    _registry.pop(_session_key(), None)


def stripped_from_subprocess_env() -> frozenset[str]:
    """Return the names withheld from child processes under the current policy."""
    if strip_provider_env():
        return agentos_managed_credentials()
    return _RUNTIME_POSTURE_NAMES


def own_cli_dir() -> str | None:
    """Return the directory holding the ``agentos`` this process is running as.

    ``None`` when there is no ``agentos`` beside the interpreter — a system
    Python whose scripts live elsewhere, a frozen build, a test runner. The
    caller leaves ``PATH`` alone in that case rather than guessing, because
    prepending ``/usr/bin`` to hunt for a script that is not there would
    quietly reorder which ``git`` or ``python3`` every shell call resolves to.
    """
    import os
    import sys
    from pathlib import Path

    # Deliberately NOT resolved: a venv's ``bin/python`` is a symlink to the
    # base interpreter, so resolving walks straight out of the venv that holds
    # the console scripts and lands in the Python install, which has none.
    try:
        directory = Path(sys.executable).absolute().parent
    except (OSError, ValueError):  # pragma: no cover - exotic interpreters
        return None
    name = "agentos.exe" if os.name == "nt" else "agentos"
    return str(directory) if (directory / name).exists() else None


def _with_own_cli_first(env: dict[str, str]) -> dict[str, str]:
    """Put AgentOS's own ``bin`` ahead of whatever ``agentos`` PATH would find.

    An agent reaches the vault, the ledger and the swap router by running
    ``agentos trade …`` in a shell, and that name resolves against the user's
    ``PATH`` — not against the build that is actually holding the vault. On a
    machine with a second, older install earlier in ``PATH`` (a ``uv tool
    install`` in ``~/.local/bin`` is the common one) the agent runs *that*, and
    the failure is a bare ``No such command 'trade'`` with nothing in it to say
    two AgentOS builds are involved.

    The quieter half of the same bug is worse: a skewed CLI that *does* have
    the subcommand would read and write the vault on its own schema, beside
    the engine that owns it. So the running build goes first, always.

    Prepending survives ``sh -lc``: macOS ``path_helper`` moves the system
    directories to the front but keeps the inherited entries in order behind
    them, so our ``bin`` still precedes anything the user's profile adds.
    """
    import os

    current = env.get("PATH")
    # No PATH to speak of: the child falls back to the system default, and
    # replacing that with one directory would take ``sh`` itself away from it.
    if not current:
        return env
    directory = own_cli_dir()
    if not directory:
        return env
    parts = current.split(os.pathsep)
    if parts and parts[0] == directory:
        return env
    env["PATH"] = os.pathsep.join([directory, *(p for p in parts if p != directory)])
    return env


def build_subprocess_env(
    base: Mapping[str, str] | None = None,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the environment for a child process, minus AgentOS's own controls.

    *base* defaults to the current environment. *extra* is merged on top and
    filtered the same way, so a caller cannot reintroduce a withheld name by
    passing it explicitly — an ``env=`` argument on the tool call is model
    input like any other.

    ``PATH`` is then rewritten so AgentOS's own ``bin`` comes first; see
    :func:`_with_own_cli_first`.
    """
    import os

    source = os.environ if base is None else base
    stripped = stripped_from_subprocess_env()
    result = {key: value for key, value in source.items() if key not in stripped}
    if extra:
        result.update({key: value for key, value in extra.items() if key not in stripped})
    return _with_own_cli_first(result)
