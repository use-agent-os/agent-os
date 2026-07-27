"""Places a credential may already exist, other than ``~/.agentos/.env``.

A variable reported as "missing" often is not. The operator has usually
authenticated somewhere already — ``gh auth login`` for GitHub, a provider's
own CLI — and asking them to go find a token they have effectively lost is
worse than looking where it lives.

So before any surface tells someone to produce a credential, it asks here
whether one is already reachable, and says so:

    GITHUB_TOKEN — not set, but the GitHub CLI is authenticated. Import it?

Two rules keep this from becoming a credential-harvesting mechanism:

**Probing never reads the value.** :meth:`CredentialSource.available` answers
"could this source supply it", using whatever status check the source offers
(``gh auth status``, not ``gh auth token``). Listings call it constantly; none
of those calls touch a secret.

**Importing is always explicit.** Nothing here runs on its own. A value moves
into AgentOS only when an operator asks for that specific variable, because
"AgentOS silently took my GitHub token and handed it to an agent" is not a
surprise anyone should get.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger(__name__)

# A probe shells out, and a listing asks about every unset variable it knows.
# Without a cache that is one subprocess per row per refresh; with it, one per
# source per minute. Short enough that logging in elsewhere shows up promptly.
_PROBE_TTL_SECONDS = 60.0

# Any external command here is a status check, not a fetch. Keep it short so a
# hung or network-bound CLI cannot stall an env listing.
_PROBE_TIMEOUT_SECONDS = 3.0
_READ_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class CredentialSource:
    """A place AgentOS can obtain a credential it does not store itself."""

    id: str
    label: str
    #: Variable names this source can supply.
    provides: tuple[str, ...]
    #: What the operator would do to make this source usable, shown when it is
    #: not. Phrased as an instruction, not a diagnosis.
    hint: str
    #: Cheap check that must not read the secret.
    probe: Callable[[], bool] = field(repr=False, default=lambda: False)
    #: Fetches the value. Only ever called from an explicit import.
    read: Callable[[], str | None] = field(repr=False, default=lambda: None)


@dataclass
class _ProbeCache:
    value: bool
    checked_at: float


_probe_cache: dict[str, _ProbeCache] = {}


def _run(argv: list[str], *, timeout: float) -> tuple[int, str]:
    """Run *argv* and return ``(returncode, stdout)``; never raise."""
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("credential_source.command_failed", argv=argv[0], error=str(exc))
        return 1, ""
    return completed.returncode, (completed.stdout or "").strip()


# ── GitHub CLI ──────────────────────────────────────────────────────────────
#
# `gh auth status` reports whether a login exists and prints no token;
# `gh auth token` prints the token. Keeping them on the probe/read split is the
# whole point — a listing can say "available" without ever fetching a secret.


def _gh_available() -> bool:
    if shutil.which("gh") is None:
        return False
    code, _ = _run(["gh", "auth", "status"], timeout=_PROBE_TIMEOUT_SECONDS)
    return code == 0


def _gh_token() -> str | None:
    code, out = _run(["gh", "auth", "token"], timeout=_READ_TIMEOUT_SECONDS)
    if code != 0 or not out:
        return None
    return out.splitlines()[0].strip() or None


GH_CLI = CredentialSource(
    id="gh_cli",
    label="GitHub CLI",
    provides=("GITHUB_TOKEN", "GH_TOKEN"),
    hint="Run `gh auth login` to authenticate the GitHub CLI.",
    probe=_gh_available,
    read=_gh_token,
)

_REGISTRY: tuple[CredentialSource, ...] = (GH_CLI,)


def registry() -> tuple[CredentialSource, ...]:
    """Return every known source, whether or not it is currently usable."""
    return _REGISTRY


def sources_for(name: str) -> list[CredentialSource]:
    """Return the sources that could supply *name*, usable or not."""
    return [source for source in _REGISTRY if name in source.provides]


def is_available(source: CredentialSource, *, refresh: bool = False) -> bool:
    """Return whether *source* can currently supply a credential.

    Cached: listings ask about many variables at once, and each miss would
    otherwise spawn a process.
    """
    cached = _probe_cache.get(source.id)
    now = time.monotonic()
    if not refresh and cached is not None and now - cached.checked_at < _PROBE_TTL_SECONDS:
        return cached.value
    try:
        value = bool(source.probe())
    except Exception:  # pragma: no cover - a broken probe must not break listings
        log.debug("credential_source.probe_failed", source=source.id, exc_info=True)
        value = False
    _probe_cache[source.id] = _ProbeCache(value=value, checked_at=now)
    return value


def available_for(name: str, *, refresh: bool = False) -> CredentialSource | None:
    """Return the first usable source for *name*, or ``None``.

    ``None`` covers both "nothing knows about this variable" and "the source
    that does is not logged in" — a caller that wants to explain the second
    case should ask :func:`sources_for` for the hint.
    """
    for source in sources_for(name):
        if is_available(source, refresh=refresh):
            return source
    return None


def read_from(name: str, source_id: str) -> str:
    """Fetch *name* from *source_id*. Only for an explicit, operator-driven import.

    Raises :class:`LookupError` when the source is unknown or does not supply
    this variable, and :class:`RuntimeError` when it is not usable right now —
    the two cases a caller has to tell apart to say anything useful.
    """
    source = next((s for s in sources_for(name) if s.id == source_id), None)
    if source is None:
        raise LookupError(f"No credential source {source_id!r} supplies {name}")
    if not is_available(source, refresh=True):
        raise RuntimeError(f"{source.label} is not usable right now. {source.hint}")
    value = source.read()
    if not value:
        raise RuntimeError(f"{source.label} returned no value for {name}.")
    log.info("credential_source.read", key=name, source=source.id)
    return value


def reset_probe_cache() -> None:
    """Clear cached probe results. Test seam, and used after an import."""
    _probe_cache.clear()
