"""Read/write access to ``~/.agentos/.env`` — the one place AgentOS writes env.

:mod:`agentos.env` loads ``.env`` files into ``os.environ`` at process start.
This module is its write-side counterpart and the **only** supported way to
change that file from inside AgentOS. Everything user-facing — the ``/env``
screen, ``agentos env``, the gateway RPC, the ``env_set`` tool, and the
OpenClaw migration — funnels through :func:`set_env_var` so one policy gate
(:mod:`agentos.env_policy`) and one file format apply everywhere.

Two properties the writer guarantees:

**Round-trip.** Whatever :func:`set_env_var` writes, ``agentos.env`` parses
back byte-identical. The quoting rules below are chosen to satisfy the reader
*as it already behaves* rather than requiring new escape semantics, so
hand-written ``.env`` files keep parsing exactly as they did.

**Never half-written.** The file is replaced atomically. A crash mid-write
leaves the previous file intact, never a truncated one — losing an API key to
a power cut is not an acceptable failure mode for a credential store.

Precedence is *not* changed here. ``os.environ`` still wins over the file (see
:func:`agentos.env.load_env`), which means a variable exported in the operator's
shell shadows what this module writes. Callers that need the file to win —
rotating a credential mid-session — should use :func:`get_env_value_prefer_file`,
and user-facing surfaces should show :attr:`EnvEntry.source` so the operator can
see when a value they just set is being shadowed.
"""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import structlog

from agentos import env_policy
from agentos.env import parse_env_file
from agentos.paths import default_agentos_home

log = structlog.get_logger(__name__)

#: Where a variable's effective value comes from, most to least specific.
Source = Literal["process", "cwd_file", "home_file", "unset"]

# Characters that force quoting. A value made only of "safe" characters is
# written bare; anything else is wrapped so the reader hands back the exact
# string. See _quote_value for why the wrapping quote is chosen dynamically.
_QUOTE_CHARS = ("'", '"')


@dataclass(frozen=True)
class EnvEntry:
    """The state of one environment variable, safe to send to a UI.

    Never carries the raw value — :attr:`masked` is what listings show, and
    reading the real thing is a separate, audited operation.
    """

    name: str
    is_set: bool
    source: Source
    masked: str | None
    writable: bool


def env_file_path() -> Path:
    """Return the path of the ``.env`` file AgentOS writes to."""
    return default_agentos_home() / ".env"


def read_env_file(path: Path | None = None) -> dict[str, str]:
    """Return the parsed contents of *path* (default: the AgentOS ``.env``).

    Uses the same parser as :func:`agentos.env.load_env`, so what this returns
    is exactly what a restart would inject.
    """
    return parse_env_file(path if path is not None else env_file_path())


def _cwd_env_files() -> list[Path]:
    """Return the working-directory ``.env`` candidates, in load precedence."""
    work_dir = Path.cwd()
    return [work_dir / ".env", work_dir / ".env.test"]


def resolve_entry(name: str, *, secret: bool | None = None) -> EnvEntry:
    """Return the current state of *name* without exposing its value.

    ``source`` answers the question a listing actually needs: *if I edit the
    file, will it take effect?* A value reported as ``process`` is shadowed by
    the environment the gateway was started with, and editing the file changes
    nothing until that export is removed and the gateway restarts.
    """
    is_secret = env_policy.is_secret_name(name) if secret is None else secret
    writable = env_policy.is_writable(name)

    process_value = os.environ.get(name)
    home_value = read_env_file().get(name)
    cwd_value: str | None = None
    for candidate in _cwd_env_files():
        parsed = parse_env_file(candidate)
        if name in parsed:
            cwd_value = parsed[name]
            break

    if process_value is not None:
        # os.environ wins at load time, but attribute the value to the file it
        # came from when they agree — "process" is reserved for the shadowing
        # case the operator needs to know about.
        if home_value is not None and home_value == process_value:
            source: Source = "home_file"
        elif cwd_value is not None and cwd_value == process_value:
            source = "cwd_file"
        else:
            source = "process"
        effective: str | None = process_value
    elif cwd_value is not None:
        source, effective = "cwd_file", cwd_value
    elif home_value is not None:
        source, effective = "home_file", home_value
    else:
        source, effective = "unset", None

    masked = None
    if effective is not None:
        masked = env_policy.mask(effective) if is_secret else effective

    return EnvEntry(
        name=name,
        is_set=effective is not None,
        source=source,
        masked=masked,
        writable=writable,
    )


def _quote_value(value: str) -> str:
    """Return *value* serialized so the reader hands back the same string.

    ``agentos.env._parse_env_file`` strips surrounding whitespace and then one
    matching pair of surrounding quotes, without interpreting escapes. Three
    cases therefore need wrapping, and everything else is written bare:

    * empty — a bare ``KEY=`` is indistinguishable from ``KEY=""`` for some
      readers, so be explicit
    * leading or trailing whitespace — otherwise the reader's ``strip()`` eats it
    * already begins and ends with the same quote character — otherwise the
      reader strips the value's *own* quotes

    In the last case the wrapper is the *other* quote character, which is what
    lets the pair round-trip without any escape syntax.
    """
    if value == "":
        return '""'
    needs_quote = value != value.strip()
    starts_with_quote = len(value) > 0 and value[0] in _QUOTE_CHARS
    ends_with_quote = len(value) >= 2 and value[0] == value[-1] and value[0] in _QUOTE_CHARS
    if not needs_quote and not starts_with_quote:
        return value
    wrapper = "'" if (starts_with_quote and value[0] == '"') else '"'
    return f"{wrapper}{value}{wrapper}"


def _line_defines_key(line: str, key: str) -> bool:
    """Return whether *line* assigns *key*, in either ``KEY=`` or ``export KEY=`` form.

    Matching the ``export`` form matters: miss it and a save appends a *second*
    definition, then a later unset removes only one of them and silently
    resurrects the old value.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return False
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].lstrip()
    name, _, _ = stripped.partition("=")
    return name.strip() == key


def _read_lines(path: Path) -> list[str]:
    """Return the file's lines, tolerating BOMs and mixed encodings.

    ``utf-8-sig`` with ``errors="replace"`` because a ``.env`` written by a
    Windows editor may carry a BOM or stray cp1252 bytes, and refusing to read
    it would mean refusing to update a key the operator can plainly see.
    """
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    return text.splitlines()


def _write_lines(path: Path, lines: list[str]) -> None:
    """Replace *path* with *lines* atomically, preserving its permissions.

    A pre-existing file keeps its mode — deployments that bind-mount ``.env``
    into a container often use ``0640`` deliberately, and tightening it to
    ``0600`` behind their back breaks the container's read. A file created here
    gets ``0600``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode: int | None = None
    if path.exists():
        try:
            existing_mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:  # pragma: no cover - stat failure is platform noise
            existing_mode = None

    fd, tmp_name = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines))
            if lines:
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:  # pragma: no cover - already gone
            pass
        raise

    try:
        os.chmod(path, existing_mode if existing_mode is not None else 0o600)
    except OSError:  # pragma: no cover - no-op on Windows
        pass


def _apply_to_file(
    path: Path,
    updates: Mapping[str, str],
    removals: Iterable[str] = (),
    *,
    enforce_denylist: bool = True,
) -> set[str]:
    """Apply *updates* and *removals* to the ``.env`` at *path* in one rewrite.

    Returns the set of keys that already had a definition in the file, which is
    what callers need to distinguish "created" from "updated"/"removed".

    Existing definitions are replaced where they stand so surrounding comments
    and ordering survive; duplicates of the same key collapse to one line; new
    keys are appended in sorted order for a deterministic result.
    """
    for key in list(updates) + list(removals):
        if enforce_denylist:
            env_policy.assert_writable(key)
        else:
            env_policy.assert_valid_name(key)
    serialized = {
        key: f"{key}={_quote_value(env_policy.sanitize_value(key, value))}"
        for key, value in updates.items()
    }
    drop = set(removals)

    lines = _read_lines(path)
    seen: set[str] = set()
    kept: list[str] = []
    for line in lines:
        matched = next(
            (key for key in (*serialized, *drop) if _line_defines_key(line, key)),
            None,
        )
        if matched is None:
            kept.append(line)
            continue
        already_seen = matched in seen
        seen.add(matched)
        if matched in drop or already_seen:
            continue
        kept.append(serialized[matched])

    for key in sorted(set(serialized) - seen):
        kept.append(serialized[key])

    if lines != kept:
        _write_lines(path, kept)
    return seen


def write_env_file_values(
    path: Path,
    values: Mapping[str, str],
    *,
    enforce_denylist: bool = True,
) -> None:
    """Write *values* into the ``.env`` at an arbitrary *path*.

    For callers that target a file other than the running AgentOS home — the
    OpenClaw and Hermes migrations, which write into the home they are building.
    Ordinary surfaces want :func:`set_env_var` instead.

    ``enforce_denylist=False`` exists for those migrations alone. Importing an
    operator's own prior configuration is equivalent to them editing the file by
    hand, and refusing e.g. their command allowlist would silently drop settings
    they already had. Name and value validation still applies — that guards file
    integrity, not privilege.
    """
    _apply_to_file(path, values, enforce_denylist=enforce_denylist)


def set_env_var(key: str, value: str, *, apply_live: bool = True) -> EnvEntry:
    """Write ``key=value`` to the AgentOS ``.env`` and return the resulting state.

    Raises :class:`agentos.env_policy.EnvPolicyError` when the name is not
    writable or the value cannot be stored on one line.

    With ``apply_live`` the value also lands in ``os.environ``, so tools spawned
    afterwards see it without a restart — every subprocess helper copies
    ``os.environ`` at call time, and skill eligibility rebuilds its cache per
    check. Components that read a credential once at boot (provider clients)
    still need a restart; callers decide how to surface that.
    """
    existed = _apply_to_file(env_file_path(), {key: value})
    if apply_live:
        os.environ[key] = env_policy.sanitize_value(key, value)
    log.info("env.set", key=key, applied_live=apply_live, created=key not in existed)
    return resolve_entry(key)


def set_env_vars(mapping: Mapping[str, str], *, apply_live: bool = True) -> list[EnvEntry]:
    """Write several variables in one rewrite, returning one entry per key."""
    _apply_to_file(env_file_path(), mapping)
    if apply_live:
        for key, value in mapping.items():
            os.environ[key] = env_policy.sanitize_value(key, value)
    log.info("env.set_many", count=len(mapping), applied_live=apply_live)
    return [resolve_entry(key) for key in sorted(mapping)]


def unset_env_var(key: str, *, apply_live: bool = True) -> bool:
    """Remove *key* from the AgentOS ``.env``. Returns whether it was present.

    ``os.environ`` is cleared too when ``apply_live`` is set, so the removal
    takes effect for subsequently spawned tools rather than only after a
    restart. A value inherited from the parent shell is gone from this process
    but returns on the next start — :attr:`EnvEntry.source` is what tells the
    operator that is going to happen.
    """
    removed = key in _apply_to_file(env_file_path(), {}, [key])
    if apply_live:
        os.environ.pop(key, None)
    log.info("env.unset", key=key, removed=removed, applied_live=apply_live)
    return removed


def get_env_value(key: str) -> str | None:
    """Return the effective value of *key* — process environment first."""
    if key in os.environ:
        return os.environ[key]
    return read_env_file().get(key)


def get_env_value_prefer_file(key: str) -> str | None:
    """Return the value of *key*, letting the ``.env`` file win over the process.

    Use this for credentials AgentOS itself manages. When an operator rotates a
    key through the UI or CLI, a stale copy inherited from the launching shell
    would otherwise keep being served and every request would keep failing with
    a 401 that no amount of re-saving fixes.
    """
    from_file = read_env_file().get(key)
    if from_file is not None:
        return from_file
    return os.environ.get(key)


def reload_env(known_keys: set[str] | None = None) -> int:
    """Re-read the ``.env`` file into ``os.environ``. Returns keys changed.

    Additions and updates always apply. Deletions apply only to *known_keys* —
    names AgentOS is responsible for. Without that bound, reloading after a key
    was deleted from the file would also strip unrelated variables the operator
    exported in their shell.
    """
    entries = read_env_file()
    changed = 0
    for key, value in entries.items():
        if os.environ.get(key) != value:
            os.environ[key] = value
            changed += 1
    for key in known_keys or set():
        if key not in entries and key in os.environ:
            del os.environ[key]
            changed += 1
    if changed:
        log.info("env.reloaded", count=changed)
    return changed
