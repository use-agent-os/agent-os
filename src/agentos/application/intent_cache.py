"""Session-scoped cache of approved action intents.

The per-approval queue treats every tool invocation as a fresh request. That
means approving ``rm /tmp/x`` does nothing for a subsequent
``os.remove("/tmp/x")`` or ``Path("/tmp/x").unlink()`` — the model can paraphrase
its way past approval prompts and the user has to press y repeatedly. This
module normalizes destructive actions to a semantic key (intent kind + target)
and remembers approvals for a short window, so paraphrased retries of the same
intent proceed without another prompt.

The key is graded by *destructiveness*, not spelling: an approval only covers a
retry that is no more destructive than what the user actually saw. Approving
``rm /tmp/logs`` does not cover ``rm -rf /tmp/logs`` — on a directory the first
is a no-op and the second wipes it recursively, and ``-rf`` never appeared on a
prompt. The reverse direction still short-circuits, so ``rm -rf X`` covers
``shutil.rmtree("X")``: same effect, different spelling.

Scope: only *delete* intents for now, since that is the bulk of user-observed
pain. Extend ``_extract_intent`` if other classes (write-outside-workspace,
network egress) need intent-level memory.
"""

from __future__ import annotations

import itertools
import os
import re
import shlex
import threading
import time
from pathlib import Path

_DEFAULT_TTL_SECONDS = 30 * 60
_ALWAYS_TTL_SECONDS = 365 * 24 * 3600  # effectively never expires within a session


def _norm_path(raw: str, *, base_dir: str | Path | None = None) -> str:
    """Best-effort absolute-path normalization.

    Leaves non-path tokens alone (so ``*`` or variable references don't get
    expanded into something wrong).
    """
    if not raw or raw.startswith(("$", "`")) or raw in {"*", "-"}:
        return raw
    try:
        path = Path(raw).expanduser()
        if base_dir is not None and not path.is_absolute():
            path = Path(base_dir).expanduser() / path
        return str(path.resolve(strict=False))
    except (OSError, ValueError):
        return raw


_DELETE = "delete"

# Escalation capabilities that a delete can carry, in canonical key order. An
# approval covers a retry only when the cached capability set is a *superset*
# of the retry's, so a plain delete never satisfies a recursive one.
_RECURSIVE = "recursive"
_PARENTS = "parents"
_FORCE = "force"
_CAPABILITY_ORDER: tuple[str, ...] = (_RECURSIVE, _PARENTS, _FORCE)

# Every capability combination, for the superset scan in ``check``/``forget``.
_CAPABILITY_SETS: tuple[frozenset[str], ...] = tuple(
    frozenset(combo)
    for size in range(len(_CAPABILITY_ORDER) + 1)
    for combo in itertools.combinations(_CAPABILITY_ORDER, size)
)


def _graded_kind(capabilities: frozenset[str], family: str = _DELETE) -> str:
    """``delete``, ``delete:recursive``, ``delete:recursive+force`` — canonical."""
    suffix = "+".join(cap for cap in _CAPABILITY_ORDER if cap in capabilities)
    return f"{family}:{suffix}" if suffix else family


# Regex-based single-capture extractors for Python-flavoured deletes, paired
# with the capabilities each call carries. Each regex uses ``finditer`` so
# ``shutil.rmtree("a"); os.remove("b")`` yields both paths.
#
# ``os.removedirs`` carries ``parents`` on top of ``recursive``: it deletes the
# leaf and then prunes empty ancestors, so it reaches *above* the path it was
# handed — something no ``rm`` spelling does. Nothing else grants ``parents``,
# so only a prior ``os.removedirs`` approval for the same target covers it.
#
# ``os.rmdir``/``Path.rmdir`` stay plain. They remove an empty directory, which
# plain ``rm`` refuses, but an empty directory holds nothing; grading it would
# buy a prompt and no protection. Same reasoning excludes ``rm -d``/``--dir``.
_PY_DELETE_PATTERNS: tuple[tuple[re.Pattern[str], frozenset[str]], ...] = (
    (
        re.compile(r"\bos\.(?:remove|unlink|rmdir)\s*\(\s*[\"']([^\"']+)[\"']"),
        frozenset(),
    ),
    (
        re.compile(r"\bos\.removedirs\s*\(\s*[\"']([^\"']+)[\"']"),
        frozenset({_RECURSIVE, _PARENTS}),
    ),
    (
        re.compile(r"\bshutil\.rmtree\s*\(\s*[\"']([^\"']+)[\"']"),
        frozenset({_RECURSIVE}),
    ),
    (
        re.compile(
            r"\b(?:pathlib\.)?Path\s*\(\s*[\"']([^\"']+)[\"']\s*\)\s*"
            r"\.(?:unlink|rmdir)\s*\("
        ),
        frozenset(),
    ),
)

# ``rm`` flags that raise destructiveness. ``-i``/``-I`` only add prompting and
# ``-v`` only adds output, so neither is graded. ``-d``/``--dir`` is deliberately
# ungraded: it removes an *empty* directory that plain ``rm`` refuses, but an
# empty directory holds nothing, so grading it would cost a prompt and protect
# nothing — the same call made for ``os.rmdir`` above.
#
# The Python spellings carry no ``force`` grade, because ``-f`` has no Python
# analogue that changes *what* gets deleted. The asymmetry that leaves is
# one-directional and deliberate: every shell -> Python paraphrase this module
# exists for still short-circuits (``rm X`` covers ``os.remove("X")``,
# ``rm -rf X`` and ``rm -r X`` both cover ``shutil.rmtree("X")``), while the
# rare reverse -- approving ``shutil.rmtree("X")`` and then running ``rm -rf X``
# -- costs one prompt.
#
# Long options are matched by prefix because ``getopt_long`` accepts any
# unambiguous abbreviation — ``rm --recu`` is a recursive delete, and grading it
# as a plain one would reopen the bypass. No other ``rm`` long option starts
# with ``r`` or ``f``, so the prefix match cannot over-match; where it is
# uncertain it errs towards the stronger grade, which costs a prompt rather
# than an unapproved delete.
_RM_SHORT_CAPABILITIES: dict[str, str] = {"r": _RECURSIVE, "R": _RECURSIVE, "f": _FORCE}
_RM_LONG_CAPABILITIES: dict[str, str] = {
    "--recursive": _RECURSIVE,
    "--force": _FORCE,
}

# ``rmdir -p a/b/c`` removes the leaf and then prunes empty ancestors, reaching
# *above* the path it was handed. That is exactly ``os.removedirs`` above, so it
# carries the same grade and the two spellings cover each other.
_RMDIR_SHORT_CAPABILITIES: dict[str, frozenset[str]] = {"p": frozenset({_RECURSIVE, _PARENTS})}
_RMDIR_LONG_CAPABILITIES: dict[str, frozenset[str]] = {
    "--parents": frozenset({_RECURSIVE, _PARENTS})
}

# cmd.exe switches. ``/s`` recurses into subdirectories and ``/f`` forces
# read-only files; ``/q`` (quiet) and ``/p`` (prompt) change only what the user
# is shown, so neither is graded — the same reasoning that leaves ``rm -i``
# ungraded above.
_CMD_SWITCH_CAPABILITIES: dict[str, str] = {"s": _RECURSIVE, "f": _FORCE}

# PowerShell parameters are matched by prefix: the parser accepts any
# unambiguous abbreviation, so ``-Recu`` is ``-Recurse``. Names are compared
# case-insensitively because PowerShell itself is.
_PWSH_PARAM_CAPABILITIES: dict[str, str] = {"-recurse": _RECURSIVE, "-force": _FORCE}
# These take the target as their *next* token rather than positionally.
_PWSH_PATH_PARAMS: tuple[str, ...] = ("-path", "-literalpath")

# A cmd.exe switch is a slash plus one letter, optionally ``:value`` (``/a:h``).
# Anything longer is a POSIX path, which keeps ``/``, ``/tmp/x`` and
# ``/etc/passwd`` as delete *targets* rather than swallowing them as flags.
_CMD_SWITCH_RE = re.compile(r"^/[A-Za-z](?::[^\s/]*)?$")

# Deletion verbs, each mapped to the flag dialects it may be speaking. A verb
# can speak more than one: ``rmdir`` takes ``-p`` on POSIX and ``/s /q`` on
# Windows, and a command the model writes may target either host. Matching is
# case-insensitive because cmd.exe and PowerShell both are.
#
# The PowerShell alias ``ri`` is deliberately absent: two letters collide with
# too many unrelated binaries to be worth the false positives.
_POSIX, _CMD, _PWSH = "posix", "cmd", "pwsh"
_DELETE_VERBS: dict[str, tuple[str, ...]] = {
    "rm": (_POSIX,),
    "unlink": (_POSIX,),
    "rmdir": (_POSIX, _CMD),
    "rd": (_CMD,),
    "del": (_CMD,),
    "erase": (_CMD,),
    "remove-item": (_PWSH,),
}

# Longest-first so ``rmdir`` is not consumed as ``rm`` followed by ``dir``.
#
# The verb must start a word that is not an attribute access and must be
# followed by whitespace, end-of-string, or an attached cmd.exe switch
# (``del/f``). That keeps the Python spellings — ``os.rmdir(...)``,
# ``Path(x).unlink()`` — with the ``_PY_DELETE_PATTERNS`` above that grade them
# properly, instead of double-matching them here as shell verbs, and stops
# ``docker run --rm image`` from reading as a delete of ``image``.
_DELETE_VERB_RE = re.compile(
    r"(?<![.\w-])("
    + "|".join(sorted((re.escape(v) for v in _DELETE_VERBS), key=len, reverse=True))
    + r")\b(?=\s|$|/[A-Za-z](?:[\s:]|$))([^;\n&|]*)",
    re.IGNORECASE,
)

# Shell command separators that terminate a single delete invocation.
_SHELL_SEPARATORS = (";", "&&", "||", "|", "&")


def _rm_invocation_capabilities(
    tokens: list[str], *, allow_parents: bool = False
) -> frozenset[str]:
    """Grade one POSIX argument list by the escalating flags it carries.

    Stops flag parsing at ``--`` so ``rm -- -rf`` treats ``-rf`` as a filename,
    the way ``rm`` itself does. ``allow_parents`` enables the ``rmdir``-only
    ``-p``/``--parents`` grade.
    """
    capabilities: set[str] = set()
    for token in tokens:
        if token == "--":
            break
        if token.startswith("--"):
            name = token.partition("=")[0]
            if len(name) <= 2:
                continue
            capabilities.update(
                cap for option, cap in _RM_LONG_CAPABILITIES.items() if option.startswith(name)
            )
            if allow_parents:
                for option, caps in _RMDIR_LONG_CAPABILITIES.items():
                    if option.startswith(name):
                        capabilities.update(caps)
        elif token.startswith("-") and len(token) > 1:
            for char in token[1:]:
                cap = _RM_SHORT_CAPABILITIES.get(char)
                if cap is not None:
                    capabilities.add(cap)
                if allow_parents:
                    capabilities.update(_RMDIR_SHORT_CAPABILITIES.get(char, ()))
    return frozenset(capabilities)


def _rm_invocation_targets(tokens: list[str]) -> list[str]:
    """Non-flag arguments of one POSIX invocation, honouring ``--``."""
    targets: list[str] = []
    end_of_flags = False
    for token in tokens:
        if not token:
            continue
        if end_of_flags:
            targets.append(token)
            continue
        if token == "--":
            end_of_flags = True
            continue
        if token.startswith("-"):
            continue
        targets.append(token)
    return targets


def _cmd_invocation(tokens: list[str]) -> tuple[frozenset[str], list[str]]:
    """Split one cmd.exe argument list into (capabilities, targets).

    Only slash-plus-one-letter tokens are switches, so ``rmdir /s /q /`` grades
    as a recursive delete of ``/`` rather than deleting three paths named
    ``/s``, ``/q`` and ``/``.

    Dash-prefixed tokens are read as PowerShell parameters, because ``del``,
    ``rd`` and ``erase`` are also PowerShell aliases for ``Remove-Item`` and
    take ``-Recurse``/``-Force`` there.
    """
    capabilities: set[str] = set()
    targets: list[str] = []
    for token in tokens:
        if not token:
            continue
        if _CMD_SWITCH_RE.match(token):
            cap = _CMD_SWITCH_CAPABILITIES.get(token[1].lower())
            if cap is not None:
                capabilities.add(cap)
            continue
        if token.startswith("-") and len(token) > 1:
            name = token.partition(":")[0].lower()
            capabilities.update(
                cap for param, cap in _PWSH_PARAM_CAPABILITIES.items() if param.startswith(name)
            )
            continue
        targets.append(token)
    return frozenset(capabilities), targets


def _select_dialect(verb: str, tokens: list[str]) -> str:
    """Pick the one dialect an invocation is actually speaking.

    Only ``rmdir`` is ambiguous — POSIX ``rmdir -p`` and cmd.exe ``rmdir /s /q``
    share the name. A switch-shaped token settles it. Applying both dialects
    instead would emit the cmd switches as extra bogus targets.
    """
    dialects = _DELETE_VERBS[verb]
    if len(dialects) == 1:
        return dialects[0]
    if _CMD in dialects and any(_CMD_SWITCH_RE.match(token) for token in tokens):
        return _CMD
    return dialects[0]


def _pwsh_invocation(tokens: list[str]) -> tuple[frozenset[str], list[str]]:
    """Split one PowerShell argument list into (capabilities, targets).

    ``-Recurse``/``-Force`` are matched as whole parameter names by prefix, not
    by scanning characters: a character scan reads the ``r`` in ``-Force`` and
    grades a forced delete as a recursive one, which would let an approval for
    ``-Force`` silently cover ``-Recurse``.
    """
    capabilities: set[str] = set()
    targets: list[str] = []
    want_path = False
    for token in tokens:
        if not token:
            continue
        if want_path and not token.startswith("-"):
            targets.append(token)
            want_path = False
            continue
        if token.startswith("-"):
            name = token.partition(":")[0].lower()
            capabilities.update(
                cap for param, cap in _PWSH_PARAM_CAPABILITIES.items() if param.startswith(name)
            )
            want_path = any(param.startswith(name) for param in _PWSH_PATH_PARAMS)
            continue
        targets.append(token)
    return frozenset(capabilities), targets


def _tokenize_tail(tail: str) -> list[list[str]]:
    """Every plausible tokenization of one invocation's argument text.

    Windows paths are also read with ``posix=False`` so ``C:\\Users\\me\\.ssh``
    survives instead of collapsing to ``C:Usersme.ssh``.
    """
    token_sets: list[list[str]] = []
    try:
        token_sets.append(shlex.split(tail))
    except ValueError:
        token_sets.append(tail.split())
    windows_path = re.search(r"(?:^|\s)\\[^\s]", tail) or re.search(r"[A-Za-z]:\\", tail)
    if "\\" in tail and (os.name == "nt" or windows_path):
        try:
            token_sets.append(shlex.split(tail, posix=False))
        except ValueError:
            token_sets.append(tail.split())
    return token_sets


def _extract_rm_targets(command: str) -> list[tuple[str, frozenset[str]]]:
    """Pull every delete target out, tagged with that invocation's flags.

    Recognises ``rm``, ``unlink``, ``rmdir``, ``rd``, ``del``, ``erase`` and
    PowerShell ``Remove-Item``, each parsed in the flag dialect(s) it speaks, so
    ``rmdir /s /q /`` and ``Remove-Item -Recurse -Force /`` reach the
    sensitive-path hard block the same way ``rm -rf /`` does.

    Uses ``finditer`` so ``rm foo; rm -rf /bar`` yields targets from both
    invocations independently, each keeping its own capability set. The verb is
    matched wherever it appears rather than only at a command boundary: this
    feeds a security hard block, and ``find . -exec rm -rf / {} +`` or
    ``xargs rm -rf /`` must still be caught. Does not try to be a full shell
    parser — falls back to whitespace split on shlex errors (unbalanced quotes).
    """
    matches = list(_DELETE_VERB_RE.finditer(command))
    if not matches:
        return []

    targets: list[tuple[str, frozenset[str]]] = []
    seen: set[tuple[str, frozenset[str]]] = set()

    for match in matches:
        verb = match.group(1).lower()
        tail = match.group(2).strip()
        if not tail:
            continue

        for tokens in _tokenize_tail(tail):
            dialect = _select_dialect(verb, tokens)
            if dialect == _POSIX:
                capabilities = _rm_invocation_capabilities(tokens, allow_parents=verb == "rmdir")
                invocation_targets = _rm_invocation_targets(tokens)
            elif dialect == _CMD:
                capabilities, invocation_targets = _cmd_invocation(tokens)
            else:
                capabilities, invocation_targets = _pwsh_invocation(tokens)
            for token in invocation_targets:
                entry = (token, capabilities)
                if entry in seen:
                    continue
                seen.add(entry)
                targets.append(entry)

    return targets


def _extract_intents(
    command: str,
    *,
    base_dir: str | Path | None = None,
) -> list[tuple[str, str]]:
    """Return every recognized destructive intent, deduped and normalized.

    ``rm /a /b /c`` -> three tuples; ``shutil.rmtree('a'); os.remove('b')`` ->
    two tuples; a plain echo returns an empty list. The kind carries the
    destructiveness grade (``delete`` vs ``delete:recursive+force``) so an
    approval for one level never silently covers a higher one.
    """
    if not command:
        return []
    graded: list[tuple[str, frozenset[str]]] = list(_extract_rm_targets(command))
    for pattern, capabilities in _PY_DELETE_PATTERNS:
        graded.extend((m.group(1), capabilities) for m in pattern.finditer(command))

    result: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw, capabilities in graded:
        intent = (_graded_kind(capabilities), _norm_path(raw, base_dir=base_dir))
        if intent in seen:
            continue
        seen.add(intent)
        result.append(intent)
    return result


def _extract_intent(command: str) -> tuple[str, str] | None:
    """First extracted intent, or None. Convenience for single-target callers."""
    intents = _extract_intents(command)
    return intents[0] if intents else None


def _split_kind(kind: str) -> tuple[str, frozenset[str]]:
    """Inverse of :func:`_graded_kind` — ``("delete", {"recursive"})``."""
    family, _, suffix = kind.partition(":")
    return family, (frozenset(suffix.split("+")) if suffix else frozenset())


def _covering_kinds(kind: str) -> tuple[str, ...]:
    """Kinds whose approval covers *kind* — itself plus every stronger grade."""
    family, required = _split_kind(kind)
    return tuple(_graded_kind(caps, family) for caps in _CAPABILITY_SETS if required <= caps)


def _sibling_kinds(kind: str) -> tuple[str, ...]:
    """Every grade in *kind*'s family, strongest and weakest alike."""
    family, _ = _split_kind(kind)
    return tuple(_graded_kind(caps, family) for caps in _CAPABILITY_SETS)


class IntentApprovalCache:
    """In-memory cache keyed by ``(kind, target)`` with scope-aware expiry.

    Two scopes exist so the approval prompt's ``once`` and ``always`` mean
    what they say:

    * ``once``  — covers only paraphrased retries within the same user turn
                  (rm → os.remove within one model response). Cleared at the
                  start of every new user message via :meth:`clear_scope`.
    * ``always`` — persists for the full session TTL; re-prompts won't appear
                  for the same intent until the process restarts.
    """

    def __init__(self, default_ttl: float = _DEFAULT_TTL_SECONDS) -> None:
        self._default_ttl = default_ttl
        # intent -> (expires_monotonic, scope)
        self._entries: dict[tuple[str, str], tuple[float, str]] = {}
        self._lock = threading.Lock()

    def record(
        self, command: str, ttl: float | None = None, *, scope: str = "once"
    ) -> list[tuple[str, str]]:
        """Mark every intent extracted from *command* as approved.

        Handles multi-target commands like ``rm a b c`` — each path becomes its
        own cache entry. Returns the list of recorded intents (empty if none
        could be extracted).
        """
        intents = _extract_intents(command)
        if not intents:
            return []
        expires = time.monotonic() + (ttl if ttl is not None else self._default_ttl)
        with self._lock:
            for intent in intents:
                self._entries[intent] = (expires, scope)
        return intents

    def record_always(self, command: str) -> list[tuple[str, str]]:
        """Remember every intent in *command* for the session lifetime."""
        return self.record(command, ttl=_ALWAYS_TTL_SECONDS, scope="always")

    def check(self, command: str) -> bool:
        """Return True only when **every** extracted intent is still approved.

        Multi-target commands must have approval for *all* targets — one
        missing path means the whole command needs fresh approval.

        An intent is satisfied by a cached approval whose capability set is a
        *superset* of its own, so ``rm -rf X`` covers ``rm X`` but never the
        other way round.
        """
        intents = _extract_intents(command)
        if not intents:
            return False
        now = time.monotonic()
        with self._lock:
            for kind, target in intents:
                if not self._satisfied_locked(kind, target, now):
                    return False
        return True

    def _satisfied_locked(self, kind: str, target: str, now: float) -> bool:
        """True when some live entry for *target* is at least as permissive."""
        satisfied = False
        for candidate in _covering_kinds(kind):
            entry = self._entries.get((candidate, target))
            if entry is None:
                continue
            expires, _scope = entry
            if expires < now:
                self._entries.pop((candidate, target), None)
                continue
            satisfied = True
        return satisfied

    def forget(self, command: str) -> None:
        """Drop approvals for every target in *command*, at every grade.

        ``/forget <path>`` builds a plain ``rm <path>``; it has to clear the
        recursive entry too or the escalated approval would outlive it.
        """
        intents = _extract_intents(command)
        if not intents:
            return
        with self._lock:
            for kind, target in intents:
                for sibling in _sibling_kinds(kind):
                    self._entries.pop((sibling, target), None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def clear_scope(self, scope: str) -> None:
        """Drop every entry whose scope matches, leaving other scopes intact."""
        with self._lock:
            self._entries = {
                intent: data for intent, data in self._entries.items() if data[1] != scope
            }


_cache: IntentApprovalCache | None = None


def get_intent_cache() -> IntentApprovalCache:
    global _cache
    if _cache is None:
        _cache = IntentApprovalCache()
    return _cache


def reset_intent_cache() -> None:
    """Test hook — drop the singleton."""
    global _cache
    _cache = None
