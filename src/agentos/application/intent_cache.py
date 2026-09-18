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

from agentos.util.bounded_registry import BoundedRegistry

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

# Deletion spellings other than ``rm``. Each is graded by its own flag grammar,
# because the same letter means different things to different commands: ``/s``
# recurses for ``rd`` but selects a *subdirectory sweep* for ``del``, and ``-p``
# prunes ancestors for ``rmdir`` while it means nothing to ``rm`` (#1015).
#
# ``unlink`` carries no grade: it unlinks exactly one name and has no recursive,
# parent-pruning or force spelling.
#
# ``rmdir``/``rd`` with no switch stay plain, the same call ``os.rmdir`` gets
# above: an empty directory holds nothing, so grading it would buy a prompt and
# no protection.
#
# ``rmdir -p`` is graded ``parents`` alone rather than ``recursive+parents`` the
# way ``os.removedirs`` is. That is the weaker of the two readings, and it is
# the safe one: a cached grade only covers a retry whose capabilities it is a
# *superset* of, so a ``parents`` approval still re-prompts for a recursive
# delete. The cost is one extra prompt when an operator paraphrases
# ``rmdir -p X`` as ``os.removedirs("X")``.
_CMD_DIR_SWITCHES: dict[str, str] = {"s": _RECURSIVE, "q": _FORCE}
_CMD_FILE_SWITCHES: dict[str, str] = {"s": _RECURSIVE, "f": _FORCE, "q": _FORCE}
_RMDIR_SHORT_CAPABILITIES: dict[str, str] = {"p": _PARENTS}
_RMDIR_LONG_CAPABILITIES: dict[str, str] = {"--parents": _PARENTS}
# PowerShell resolves ``del``, ``erase``, ``rd`` and ``rmdir`` to ``Remove-Item``,
# so every one of them accepts ``-Recurse``/``-Force`` and each is shared below.
# A parameter that matches nothing falls through to the POSIX tables, which is
# what keeps ``rmdir -p`` graded as parent-pruning rather than swallowed.
_REMOVE_ITEM_PARAMETERS: dict[str, str] = {"-recurse": _RECURSIVE, "-force": _FORCE}

#: command name (lowercased) -> (POSIX ``-x`` flags, POSIX ``--word`` flags,
#: cmd.exe ``/x`` switches, PowerShell ``-Word`` parameters).
_DELETE_SPELLINGS: dict[str, tuple[dict[str, str], dict[str, str], dict[str, str], dict[str, str]]]
_DELETE_SPELLINGS = {
    "rmdir": (
        _RMDIR_SHORT_CAPABILITIES,
        _RMDIR_LONG_CAPABILITIES,
        _CMD_DIR_SWITCHES,
        _REMOVE_ITEM_PARAMETERS,
    ),
    "rd": ({}, {}, _CMD_DIR_SWITCHES, _REMOVE_ITEM_PARAMETERS),
    "del": ({}, {}, _CMD_FILE_SWITCHES, _REMOVE_ITEM_PARAMETERS),
    "erase": ({}, {}, _CMD_FILE_SWITCHES, _REMOVE_ITEM_PARAMETERS),
    "unlink": ({}, {}, {}, {}),
    "remove-item": ({}, {}, {}, _REMOVE_ITEM_PARAMETERS),
}

# ``rm`` is matched case-sensitively, the way the POSIX command is spelled;
# cmd.exe and PowerShell resolve their own names case-insensitively, so those
# are matched that way. Longer names come first so ``rmdir`` is not consumed as
# a bare ``rm``.
#
# The name must not follow a ``.`` or a word character: ``os.rmdir("/tmp/d")``
# and ``Path("x").unlink()`` are Python spellings, already extracted (and
# graded) by ``_PY_DELETE_PATTERNS``. Matching them here as shell commands too
# regraded the paraphrase and broke the ``rm X`` -> ``os.rmdir("X")``
# short-circuit. ``/usr/bin/rmdir`` still matches — ``/`` is not a word
# character — so an absolute-path invocation is not lost.
_DELETE_COMMAND_RE = re.compile(
    r"(?<![\w.-])(?P<cmd>rmdir|unlink|rm|(?i:rmdir|unlink|remove-item|erase|del|rd))"
    r"\b(?P<tail>[^;\n&|]*)"
)

#: Commands that run whatever follows them, so a deletion verb after one is
#: still the command being executed. ``env`` also carries ``NAME=value``
#: assignments, which are accepted in the same position.
_COMMAND_WRAPPERS = frozenset(
    {"sudo", "doas", "env", "time", "nohup", "command", "xargs", "busybox"}
)

#: Shell reserved words that run the command following them. Each is only one
#: while it is itself in command position, so ``echo then del x`` stays text.
_COMMAND_KEYWORDS = frozenset({"{", "!", "do", "then", "else", "elif", "if", "while", "until"})

# Group and substitution openers: the command inside ``( … )``, ``$( … )`` and
# ``` `…` ``` runs wherever the opener sits. ``{`` opens a group too, but
# ``${`` opens a parameter expansion, so ``echo ${del}`` is not a delete.
_COMMAND_OPENER_RE = re.compile(r"[(`]|(?<!\$)\{")


def _in_command_position(command: str, span_start: int, match_start: int) -> bool:
    """Whether the verb matched at *match_start* is the command being run.

    ``rm`` does not need this: it is long enough, and specific enough, that a
    bare occurrence is almost always the command. ``del`` and ``rd`` are
    ordinary English and ordinary path segments, so without an anchor
    ``grep -rn del /etc/passwd`` and ``cat /srv/del/x /etc/passwd`` extracted
    ``/etc`` and were hard-blocked — and a hard block cannot be approved past,
    so reading a file became impossible (#1015 review).

    Command position is the start of the expression, the text after a
    separator or a group/substitution opener (``{``, ``(``, ``$(``, a
    backtick), the text after a reserved word that runs what follows it
    (``do``, ``then``, ``else`` …), or the text after a wrapper. A wrapper
    may carry its own arguments (``sudo -u root del …``), which is why anything
    after the first wrapper is accepted.
    """
    region = command[span_start:match_start]
    for separator in ("\n", ";", "&&", "||", "|", "&"):
        region = region.rpartition(separator)[2]
    # A backtick only opens a substitution when an odd number precede the verb;
    # after an even number it has closed, and ``echo `pwd` del x`` is text.
    opener_end = 0
    for opener in _COMMAND_OPENER_RE.finditer(region):
        if opener.group() != "`" or region.count("`", 0, opener.end()) % 2:
            opener_end = opener.end()
    region = region[opener_end:]

    boundary = max(region.rfind(" "), region.rfind("\t"))
    token_prefix = region[boundary + 1 :]
    # Only a directory prefix may sit in front of the verb: ``/usr/bin/rmdir``
    # is an invocation, ``/srv/del/x`` is a path that contains the word.
    if token_prefix and not token_prefix.endswith(("/", "\\")):
        return False

    seen_wrapper = False
    for token in region[: boundary + 1].split():
        if not seen_wrapper and token in _COMMAND_KEYWORDS:
            continue
        if "=" in token and not token.startswith("-"):
            continue
        if token.rpartition("/")[2].rpartition("\\")[2].lower() in _COMMAND_WRAPPERS:
            seen_wrapper = True
            continue
        if not seen_wrapper:
            return False
    return True


def _delete_invocation_capabilities(
    tokens: list[str],
    spelling: tuple[dict[str, str], dict[str, str], dict[str, str], dict[str, str]],
) -> frozenset[str]:
    """Grade one non-``rm`` deletion argument list by the flags it carries.

    ``rm`` keeps :func:`_rm_invocation_capabilities` to itself. Its grammar is
    already pinned by tests and carries the ``getopt_long`` abbreviation rule,
    and folding four more flag dialects into it would put that behaviour at
    risk for no gain — these spellings share none of its options.

    Stops flag parsing at ``--`` for the POSIX spellings, the way they do.
    PowerShell parameters are prefix-matched because PowerShell accepts any
    unambiguous abbreviation: ``Remove-Item -rec`` recurses, and grading it as
    a plain delete would reopen the bypass.
    """
    short, long, switches, parameters = spelling
    capabilities: set[str] = set()
    for token in tokens:
        if token == "--":
            break
        lowered = token.lower()
        if switches and lowered.startswith("/") and len(lowered) > 1:
            cap = switches.get(lowered[1:])
            if cap is not None:
                capabilities.add(cap)
            continue
        if parameters and lowered.startswith("-") and len(lowered) > 1:
            # PowerShell binds a parameter to its value with ``:`` as readily
            # as with a space, so the name has to come off the token before the
            # prefix match: ``-Recurse:$true`` matched no option at all and the
            # invocation came back plain, which let a plain approval cover a
            # recursive delete.
            name = _parameter_name(lowered)
            matched = {
                cap
                for option, cap in parameters.items()
                if len(name) > 1 and option.startswith(name)
            }
            if matched:
                capabilities.update(matched)
                continue
            # Not a PowerShell parameter after all: the same verb may be
            # spelled POSIX on this line (``rmdir -p``), so fall through
            # rather than swallowing the token.
        if token.startswith("--"):
            name = lowered.partition("=")[0]
            capabilities.update(
                cap for option, cap in long.items() if len(name) > 2 and option.startswith(name)
            )
        elif token.startswith("-") and len(token) > 1:
            for char in token[1:]:
                cap = short.get(char)
                if cap is not None:
                    capabilities.add(cap)
    return frozenset(capabilities)


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


def _parameter_name(token: str) -> str:
    """The parameter name of a PowerShell token, without any bound value."""
    return token.partition(":")[0].partition("=")[0]


def _delete_invocation_targets(
    tokens: list[str],
    parameters: dict[str, str],
) -> list[str]:
    """Non-flag arguments of one non-``rm`` invocation, plus bound path values.

    Under PowerShell every Windows deletion verb here — ``del``, ``erase``,
    ``rd``, ``rmdir``, ``rm`` — is an alias of ``Remove-Item``, and a parameter
    can carry its value on the same token: ``-Path:/etc/passwd``.
    :func:`_rm_invocation_targets` skips any token starting with ``-``, so the
    path went missing along with the hard block that depends on it.

    A token whose name grades the invocation (``-Recurse:$true``) is a flag,
    not a path, and is left to :func:`_delete_invocation_capabilities`.
    """
    targets = _rm_invocation_targets(tokens)
    end_of_flags = False
    for token in tokens:
        if token == "--":
            end_of_flags = True
            continue
        if end_of_flags or not token.startswith("-") or len(token) < 2:
            continue
        name, separator, value = _split_bound_parameter(token)
        if not separator or not value:
            continue
        if any(option.startswith(name.lower()) for option in parameters):
            continue
        targets.append(value)
    return targets


def _split_bound_parameter(token: str) -> tuple[str, str, str]:
    """Split ``-Name:value`` / ``-Name=value`` into name, separator and value."""
    colon = token.partition(":")
    if colon[1]:
        return colon
    return token.partition("=")


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


#: Shells that hand a ``-c`` argument straight to a command interpreter.
_SHELL_NAMES = frozenset({"sh", "bash", "zsh", "dash", "ash", "ksh"})


def _runs_quoted_argument_as_command(prefix: str) -> bool:
    """Whether *prefix* — the text right before a quote — will execute it.

    A quoted span is data *unless* something is about to run it. Skipping every
    quoted ``rm`` also skipped ``sh -c "rm -rf /etc/passwd"``, which the shell
    does execute, so that is a hard block lost rather than an approval lost.

    Only the tail of the prefix matters, which is why ``docker exec c sh -c
    "…"`` needs no rule of its own. ``ssh`` is matched anywhere in the prefix
    rather than adjacently, so ``ssh -p 22 host "…"`` is covered too — a quoted
    argument to ``ssh`` is remote command text in every spelling.

    The shell name is looked for the same way: anywhere in the prefix, scanning
    back from ``-c`` over the shell's own options. Requiring it adjacently at
    ``tokens[-2]`` made one option enough to turn the command back into data —
    ``bash -e -c "rm -rf /etc"`` and ``bash -o pipefail -c "…"`` are ordinary
    CI glue, so that is a hard block lost. The scan stops at the first token
    that is neither an option nor an option's argument, which is what keeps
    ``git commit -m -c "…"``-shaped prefixes from resolving to a shell.
    """
    tokens = prefix.replace(";", " ").replace("&", " ").replace("|", " ").split()
    if any(token.rsplit("/", 1)[-1] == "ssh" for token in tokens):
        return True
    if len(tokens) < 2:
        return False
    # ``-c`` or a combined short flag carrying it, e.g. ``bash -lc``.
    flag = tokens[-1]
    if not (flag.startswith("-") and "c" in flag):
        return False
    pending_word = False
    for token in reversed(tokens[:-1]):
        if token.rsplit("/", 1)[-1] in _SHELL_NAMES:
            return True
        if token.startswith("-"):
            pending_word = False
            continue
        # A bare word is passed over only as the argument of the option to its
        # left — the ``pipefail`` of ``bash -o pipefail -c``. Two in a row means
        # the prefix is some other command, so the scan stops.
        if pending_word:
            return False
        pending_word = True
    return False


def _command_spans(command: str) -> list[tuple[int, int]]:
    """Ranges of *command* a shell would read as command text.

    Unquoted text, plus any quoted span that a shell-invoking command is about
    to execute. A quote opened and never closed leaves the rest of the string
    quoted, which is what the shell does with it too.
    """
    spans: list[tuple[int, int]] = []
    start = 0
    quote: str | None = None
    quote_open = 0
    for index, char in enumerate(command):
        if quote is None:
            if char in "'\"":
                spans.append((start, index))
                quote = char
                quote_open = index
        elif char == quote:
            if _runs_quoted_argument_as_command(command[:quote_open]):
                spans.append((quote_open + 1, index))
            quote = None
            start = index + 1
    if quote is None:
        spans.append((start, len(command)))
    elif _runs_quoted_argument_as_command(command[:quote_open]):
        # Unbalanced quote after an introducer: the shell would still try to run
        # what follows, so it is command text rather than data.
        spans.append((quote_open + 1, len(command)))
    return spans


def _extract_shell_delete_targets(command: str) -> list[tuple[str, frozenset[str]]]:
    """Pull every deletion argument out, tagged with that invocation's flags.

    Covers ``rm`` plus the spellings an operator or a model reaches for on the
    other platforms — ``rmdir``, ``rd``, ``del``, ``erase``, ``unlink`` and
    PowerShell's ``Remove-Item``. Matching only ``rm`` meant every one of those
    yielded zero targets, so ``sensitive_target_in_command`` never saw their
    paths and the root-wipe and sensitive-path hard blocks were bypassed
    outright (#1015).

    Handles ``rm a b c``, ``rm -rf /a /b``, ``rmdir /s /q C:\\``,
    ``del /f /q ~/.ssh/id_rsa``, ``Remove-Item -Recurse -Force /``, quoted
    paths, and stops at shell separators. Uses ``finditer`` so
    ``rm foo; rmdir -p /bar`` yields targets from both invocations
    independently — and each keeps its own capability set, so the ``-rf`` on
    one does not leak onto the other. Does not try to be a full shell parser —
    falls back to whitespace split on shlex errors (unbalanced quotes).

    A cmd.exe switch is also kept as a target, because ``/s`` is a switch on
    Windows and an absolute path on POSIX and nothing in the string says which
    shell will run it. That costs a spurious plain-delete intent for ``/s`` and
    ``/q``; the alternative silently drops a real POSIX target, which is the
    bypass this function exists to close.

    A ``rm`` inside a quoted argument is text, not a command: without that
    check ``grep -rn "rm" /etc/passwd`` extracted ``/etc/passwd`` and was
    hard-blocked as a delete, and the operator could not approve past it.
    """
    # Match each deletion invocation, stopping at shell separators.
    # ``[^;\n&|]*`` captures everything from the command up to the next
    # separator or end-of-expression, so each one is tokenized independently.
    pattern = _DELETE_COMMAND_RE
    # Position, not quoting, is what tells a command from a word here. Requiring
    # ``rm`` to start the string or follow a separator would look tighter but
    # drops ``sudo rm -rf /etc``, ``env FOO=1 rm …``, ``time rm …`` and
    # ``xargs rm`` — a command prefix is ordinary, so that spelling trades a
    # false positive for a bypass.
    executable = _command_spans(command)
    matches = []
    for match in pattern.finditer(command):
        span = next(
            ((start, end) for start, end in executable if start <= match.start() < end),
            None,
        )
        if span is None:
            continue
        # ``rm`` is matched wherever it is executable; the shorter verbs are
        # ordinary words and path segments, so they must be in command
        # position and must end their own token -- ``/srv/del/x`` is a path,
        # ``/usr/bin/rmdir`` is an invocation.
        if match.group("cmd").lower() != "rm":
            if match.group("tail")[:1] in ("/", "\\", "."):
                continue
            if not _in_command_position(command, span[0], match.start()):
                continue
        matches.append(match)
    if not matches:
        return []

    targets: list[tuple[str, frozenset[str]]] = []
    seen: set[tuple[str, frozenset[str]]] = set()

    for match in matches:
        tail = match.group("tail").strip()
        if not tail:
            continue
        spelling = _DELETE_SPELLINGS.get(match.group("cmd").lower())

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
            capabilities = (
                _rm_invocation_capabilities(tokens)
                if spelling is None
                else _delete_invocation_capabilities(tokens, spelling)
            )
            invocation_targets = (
                _rm_invocation_targets(tokens)
                if spelling is None
                else _delete_invocation_targets(tokens, spelling[3])
            )
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
    graded: list[tuple[str, frozenset[str]]] = list(_extract_shell_delete_targets(command))
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
        # Keys are (kind, target), not sessions; the TTL already lives in
        # the value, so this only adds the missing size ceiling. An ``always``
        # grant carries a year-long TTL, so a long-lived gateway can push one
        # out under the LRU ceiling — that fails *closed* (the user is
        # re-prompted), which is the right direction for an approval cache.
        self._entries: BoundedRegistry[tuple[str, str], tuple[float, str]] = BoundedRegistry(
            name="IntentApprovalCache._entries",
        )
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
            self._entries.discard_where(lambda _intent, data: data[1] == scope)


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
