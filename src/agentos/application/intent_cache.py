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

# Shell command separators that terminate a single ``rm`` invocation.
_SHELL_SEPARATORS = (";", "&&", "||", "|", "&")


def _rm_invocation_capabilities(tokens: list[str]) -> frozenset[str]:
    """Grade one ``rm`` argument list by the escalating flags it carries.

    Stops flag parsing at ``--`` so ``rm -- -rf`` treats ``-rf`` as a filename,
    the way ``rm`` itself does.
    """
    capabilities: set[str] = set()
    for token in tokens:
        if token == "--":
            break
        if token.startswith("--"):
            name = token.partition("=")[0]
            capabilities.update(
                cap
                for option, cap in _RM_LONG_CAPABILITIES.items()
                if len(name) > 2 and option.startswith(name)
            )
        elif token.startswith("-") and len(token) > 1:
            for char in token[1:]:
                cap = _RM_SHORT_CAPABILITIES.get(char)
                if cap is not None:
                    capabilities.add(cap)
    return frozenset(capabilities)


def _rm_invocation_targets(tokens: list[str]) -> list[str]:
    """Non-flag arguments of one ``rm`` invocation, honouring ``--``."""
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


def _extract_rm_targets(command: str) -> list[tuple[str, frozenset[str]]]:
    """Pull every ``rm`` argument out, tagged with that invocation's flags.

    Handles ``rm a b c``, ``rm -rf /a /b``, quoted paths, and stops at shell
    separators. Uses ``finditer`` so ``rm foo; rm -rf /bar`` yields targets
    from both invocations independently — and each keeps its own capability
    set, so the ``-rf`` on the second does not leak onto the first. Does not
    try to be a full shell parser — falls back to whitespace split on shlex
    errors (unbalanced quotes).
    """
    # Match each ``rm`` invocation, stopping at shell separators.
    # ``[^;\n&|]*`` captures everything from ``rm`` up to the next separator
    # or end-of-expression, so each ``rm`` is tokenized independently.
    pattern = re.compile(r"\brm\b([^;\n&|]*)")
    matches = list(pattern.finditer(command))
    if not matches:
        return []

    targets: list[tuple[str, frozenset[str]]] = []
    seen: set[tuple[str, frozenset[str]]] = set()

    for match in matches:
        tail = match.group(1).strip()
        if not tail:
            continue

        token_sets: list[list[str]] = []
        try:
            token_sets.append(shlex.split(tail))
        except ValueError:
            token_sets.append(tail.split())
        if "\\" in tail and (os.name == "nt" or re.search(r"(?:^|\s)\\[^\s]", tail)):
            try:
                token_sets.append(shlex.split(tail, posix=False))
            except ValueError:
                token_sets.append(tail.split())

        for tokens in token_sets:
            capabilities = _rm_invocation_capabilities(tokens)
            for token in _rm_invocation_targets(tokens):
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
