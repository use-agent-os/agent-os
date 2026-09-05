"""Dependency-free version comparison for the upgrade / skew machinery.

AgentOS ships CalVer / PEP 440 style versions (``2026.7.18``,
``2026.7.18.post1``, ``0.0.0+unknown``). The upgrade command, the passive
update notice, and the version-skew policy all need to answer one question:
"is version A older / newer / equal to version B?" — without pulling in a new
runtime dependency (``packaging``) purely for that.

The parser here handles the release segment plus the common pre-release
(``rcN`` / ``aN`` / ``bN``), ``.postN``, and ``.devN`` tails, and ignores the
local version label (``+unknown``) for ordering. It is intentionally small: it
is NOT a full PEP 440 implementation, but it is exact for the versions AgentOS
actually ships, and it degrades to a stable total ordering for anything it does
not fully understand rather than raising.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from functools import total_ordering
from typing import Any

# release [ {a|b|rc}N ] [ .postN ] [ .devN ] [ +local ]
_VERSION_RE = re.compile(
    r"^\s*v?"
    r"(?P<release>\d+(?:\.\d+)*)"
    r"(?:[._-]?(?P<pre_l>a|b|c|rc|alpha|beta|pre|preview)[._-]?(?P<pre_n>\d+)?)?"
    r"(?:[._-]?(?P<post_l>post)[._-]?(?P<post_n>\d+)?)?"
    r"(?:[._-]?(?P<dev_l>dev)[._-]?(?P<dev_n>\d+)?)?"
    r"(?:\+(?P<local>[a-zA-Z0-9.]+))?"
    r"\s*$",
    re.IGNORECASE,
)

# Ordering weight for the pre-release phase (lower = earlier).
_PRE_ORDER = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "c": 2, "rc": 2, "pre": 2, "preview": 2}


@total_ordering
@dataclass(frozen=True)
class Version:
    """A parsed, comparable version.

    Phase ordering within one release number is
    ``dev < pre < final < post``. Unparsable input sorts below every real
    release so a stale ``0.0.0+unknown`` never spuriously looks *newer* than a
    running gateway.
    """

    raw: str
    release: tuple[int, ...]
    pre: tuple[int, int] | None
    post: int | None
    dev: int | None
    parsed: bool

    def sort_key(self, width: int) -> tuple[Any, ...]:
        release = self.release + (0,) * (width - len(self.release))
        if not self.parsed:
            # All unparsable versions sort together, below any real release,
            # but a given raw string stays equal to itself.
            return ((-1,) * width, (-1,), self.raw)
        if self.dev is not None and self.pre is None and self.post is None:
            phase: tuple[int, ...] = (0, self.dev)
        elif self.pre is not None:
            dev_tie = sys.maxsize if self.dev is None else self.dev
            phase = (1, self.pre[0], self.pre[1], dev_tie)
        elif self.post is not None:
            dev_tie = sys.maxsize if self.dev is None else self.dev
            phase = (3, self.post, dev_tie)
        else:
            phase = (2,)
        return (release, phase, "")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        width = max(len(self.release), len(other.release))
        return self.sort_key(width) == other.sort_key(width)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        width = max(len(self.release), len(other.release))
        return self.sort_key(width) < other.sort_key(width)


def parse_version(value: str | None) -> Version:
    """Parse ``value`` into a :class:`Version` (never raises)."""

    raw = (value or "").strip()
    match = _VERSION_RE.match(raw)
    if not match:
        return Version(raw=raw, release=(-1,), pre=None, post=None, dev=None, parsed=False)

    release = tuple(int(part) for part in match.group("release").split("."))
    pre: tuple[int, int] | None = None
    if match.group("pre_l"):
        pre_tag = match.group("pre_l").lower()
        pre = (_PRE_ORDER.get(pre_tag, 2), int(match.group("pre_n") or 0))
    post = int(match.group("post_n") or 0) if match.group("post_l") else None
    dev = int(match.group("dev_n") or 0) if match.group("dev_l") else None
    return Version(raw=raw, release=release, pre=pre, post=post, dev=dev, parsed=True)


def compare_versions(a: str | None, b: str | None) -> int:
    """Return -1 if ``a < b``, 0 if equal, 1 if ``a > b``."""

    va = parse_version(a)
    vb = parse_version(b)
    if va < vb:
        return -1
    if va > vb:
        return 1
    return 0


def is_newer(candidate: str | None, current: str | None) -> bool:
    """True when ``candidate`` is strictly newer than ``current``."""

    return compare_versions(candidate, current) > 0
