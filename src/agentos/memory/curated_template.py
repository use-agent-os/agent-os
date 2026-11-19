"""The instructional text older workspaces carry in MEMORY.md and USER.md.

Until 2026.11 a new workspace was seeded with a ``MEMORY.md`` and a
``USER.md`` that explained what each file is for ("Use this file for curated
durable non-profile facts...", ``- Name:``, "Do not store secrets..."). Both
files are curated stores, so that text was injected into every prompt as if
the agent remembered it and counted against the char budget, and the
free-form migration split the ``MEMORY.md`` one into eight entries. When a
store filled up, real facts were removed to make room while these stayed.

New workspaces start with both files empty; what belongs in each is in the
system prompt. For existing workspaces :func:`without_bootstrap_template`
recognises the old text so ``CuratedMemoryStore`` can remove it once. An
entry, as the store holds it (line endings as ``\\n``, trimmed), is template
text in exactly two cases:

1. It equals a whole shipped template (a ``MEMORY.md`` the migration never
   touched, or a ``USER.md``, which it never touches), or one of the entries
   the free-form migration made from the ``MEMORY.md`` template. It is
   removed.
2. It is a shipped template the user only added to: every non-blank line of
   the template is still there, in order and unchanged (trailing whitespace
   aside), except that a field line such as ``- Name:`` may have a value after
   the colon. The unchanged template lines are removed; the filled-in field
   lines and every added line are kept, verbatim and in order, as one entry.
   Nothing left means the entry is removed.

Anything else is left alone: an entry that quotes, rewraps or rewords
template text, or a template with any line edited or deleted, is the user's.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from functools import cache
from typing import Final

from agentos.memory.curated import CuratedMemoryStore

# The templates exactly as shipped. Every release through 2026.11.18.post2
# shipped this same text; the wording never changed. If another wording turns
# up, add it to the tuple: matching is exact, so a wording missing here is left
# alone, never half-removed.
_MEMORY_MD_TEMPLATE_V1: Final[str] = """\
# MEMORY.md

Use this file for curated durable non-profile facts, preferences, decisions, and
constraints that are safe to include in private agent context.

Examples:

- durable user preferences that are not identity/profile fields
- project decisions or recurring constraints
- facts the user explicitly asks the agent to remember
- stable context that should survive across sessions

User identity and profile fields belong in `USER.md`.
Agent name, tone, and persona belong in IDENTITY.md or SOUL.md.
Daily/session notes belong in `memory/**/*.md`.

Do not store secrets, credentials, private keys, or ordinary one-off deliverables
here.
"""

_USER_MD_TEMPLATE_V1: Final[str] = """\
# USER.md

Use this file for stable user profile notes that are safe to include in private
agent context.

Put facts about the user here, not facts about the assistant or the current
task.

- Name:
- What to call them:
- Pronouns:
- Timezone:
- Notes:

## Context

Do not put secrets, credentials, or one-off task notes here.
"""

SHIPPED_TEMPLATES: Final[Mapping[str, tuple[str, ...]]] = {
    "memory": (_MEMORY_MD_TEMPLATE_V1,),
    "user": (_USER_MD_TEMPLATE_V1,),
}

#: Curated file name -> store target, for callers that start from a path.
TEMPLATE_TARGETS: Final[Mapping[str, str]] = {"MEMORY.md": "memory", "USER.md": "user"}

# ``- Name:`` and friends: a bullet whose whole text is a label and a colon.
_FIELD_LINE = re.compile(r"- [^:]+:")


@cache
def _exact_entries(target: str) -> frozenset[str]:
    """Entries that are template text in full (rule 1)."""
    # Imported here: the migration imports this module.
    from agentos.memory.curated_migration import _split_freeform

    exact: set[str] = set()
    for template in SHIPPED_TEMPLATES.get(target, ()):
        exact.add(template.strip())
        if target == "memory":
            # Only MEMORY.md was ever run through the free-form migration.
            exact.update(_split_freeform(template))
    return frozenset(exact)


@cache
def _template_lines(template: str) -> tuple[str, ...]:
    return tuple(line for line in template.splitlines() if line.strip())


def _strip_template_lines(entry: str, template: str) -> str | None:
    """*entry* without *template*'s unchanged lines (rule 2).

    Returns ``None`` when *entry* is not *template* plus additions, i.e. a
    template line is missing, out of order or changed.
    """
    lines = _template_lines(template)
    kept: list[str] = []
    gap = False
    i = 0
    for line in entry.splitlines():
        text = line.rstrip()
        if not text:
            gap = bool(kept)
            continue
        if i < len(lines):
            expected = lines[i]
            if text == expected:
                i += 1
                continue
            if (
                _FIELD_LINE.fullmatch(expected)
                and text.startswith(expected)
                and text[len(expected) :].strip()
            ):
                i += 1
        if gap:
            kept.append("")
            gap = False
        kept.append(line)
    if i < len(lines):
        return None
    return "\n".join(kept).strip()


def without_bootstrap_template(target: str, entries: Sequence[str]) -> list[str]:
    """Return *entries* with the shipped template text removed (see module doc).

    Entries are expected as the store holds them. Order is kept, and an entry
    that only loses template lines stays where it was.
    """
    exact = _exact_entries(target)
    templates = SHIPPED_TEMPLATES.get(target, ())
    result: list[str] = []
    for entry in entries:
        if entry in exact:
            continue
        kept = entry
        for template in templates:
            stripped = _strip_template_lines(entry, template)
            if stripped is None:
                continue
            if stripped and CuratedMemoryStore._would_not_round_trip(stripped):
                break  # cannot be stored as one entry: leave it as it was
            kept = stripped
            break
        if kept:
            result.append(kept)
    return list(dict.fromkeys(result))


def holds_only_bootstrap_template(target: str, text: str) -> bool:
    """True when *text* (a whole curated file) holds nothing but template text.

    An empty file counts: that is what a new workspace is seeded with.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return not without_bootstrap_template(target, CuratedMemoryStore._parse_entries(normalized))
