"""Multi-strategy text matching for tool-driven file edits.

``edit_file`` receives an ``old_text`` the model reproduced from memory or from
an earlier ``read_file``. Exact string equality fails on differences that carry
no meaning for the edit: a block the model re-indented, a tab that became
spaces, a literal ``\\n`` it never unescaped, a smart quote carried in from
prose. Every one of those costs a turn.

This module tries a fixed chain of strategies, ordered from strict to
permissive, and stops at the first one that finds the text. It reports which
strategy matched, because a hit from ``context_similarity`` deserves more
scrutiny than one from ``exact``.

Three invariants make the permissive strategies safe to run by default:

* **Spans are always original-coordinate.** A strategy may normalize text to
  *find* a match, but it maps the result back to offsets in the untouched
  ``content``, so characters outside the replaced region are never rewritten.
* **Replacements are re-indented to the region they land in.** When a strategy
  ignored indentation to match, the model's ``new_text`` almost never carries
  the file's actual indentation; pasting it verbatim produces broken code.
* **Ambiguity is an error, never a guess.** A strategy that finds more than one
  region raises instead of silently taking the first.

Everything here is pure — strings in, strings out, no filesystem access.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

# Strategy order. Strict first: a permissive strategy must never win a match a
# stricter one could have found, or the reported strategy understates the risk.
STRATEGIES: tuple[str, ...] = (
    "exact",
    "escape_normalized",
    "unicode_normalized",
    "trimmed_boundary",
    "indent_agnostic",
    "line_trimmed",
    "whitespace_collapsed",
    "block_anchor",
    "context_similarity",
)

# Strategies that matched without honouring the file's indentation. A hit from
# any of these means new_text has to be re-indented to the matched region.
_INDENT_BLIND = frozenset(
    {
        "trimmed_boundary",
        "indent_agnostic",
        "line_trimmed",
        "whitespace_collapsed",
        "block_anchor",
        "context_similarity",
    }
)

# Similarity floors for the two strategies that match on resemblance rather
# than on a normalization. Deliberately high: these rewrite source code, and a
# near-miss that replaces the wrong block is worse than reporting no match.
_BLOCK_ANCHOR_MIN_SIMILARITY = 0.70
_CONTEXT_MIN_SIMILARITY = 0.90
# A context_similarity winner must beat the runner-up by this margin, otherwise
# the file has two comparable candidates and picking either one is a guess.
_CONTEXT_MIN_MARGIN = 0.05
# block_anchor needs a head and a tail to anchor on, plus something between.
_BLOCK_ANCHOR_MIN_LINES = 3
# Below this, a "closest region" is noise — every line in the file scores a few
# percent against any pattern, and offering those as a hint misleads the model.
_HINT_MIN_SIMILARITY = 0.40

# ``block_anchor``, ``context_similarity`` and the failed-match hint all slide
# the pattern's window down the whole file and score each position with
# ``SequenceMatcher``, so their cost scales with lines × pattern lines. Past
# this many cells the sweep is measured in tens of seconds — a 12,500-line file
# against a 200-line pattern takes over a minute — and a model that gets a
# prompt "not found" is better served than one that waits for a guess. The
# bound still leaves every edit shape we actually see intact: a 2,000-line file
# with a 50-line pattern, a 5,000-line file with a 20-line pattern, a
# 10,000-line file with a 10-line pattern.
_MAX_SWEEP_CELLS = 100_000

_ESCAPE_SEQUENCES = (
    ("\\r\\n", "\n"),
    ("\\n", "\n"),
    ("\\r", "\r"),
    ("\\t", "\t"),
    ('\\"', '"'),
    ("\\'", "'"),
)

# Characters prose editors and chat clients substitute silently.
_UNICODE_LOOKALIKES = {
    "“": '"',
    "”": '"',
    "‘": "'",
    "’": "'",
    "—": "--",
    "–": "-",
    "…": "...",
    " ": " ",
}

_INDENT_RE = re.compile(r"^[ \t]*")


@dataclass(frozen=True)
class FuzzyMatchResult:
    """Outcome of a successful match-and-replace."""

    updated: str
    match_count: int
    strategy: str
    spans: tuple[tuple[int, int], ...]


class FuzzyMatchError(ValueError):
    """No strategy located *old_text*.

    ``hint`` holds the closest regions found, with line numbers, so the caller
    can hand the model something actionable instead of "not found".
    """

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


class AmbiguousMatchError(FuzzyMatchError):
    """A strategy matched several regions and the caller wanted exactly one."""

    def __init__(self, message: str, *, strategy: str, match_count: int, lines: Sequence[int]):
        super().__init__(message)
        self.strategy = strategy
        self.match_count = match_count
        self.lines = tuple(lines)


# ---------------------------------------------------------------------------
# line/offset bookkeeping
# ---------------------------------------------------------------------------


def _content_lines(content: str) -> tuple[list[str], list[int], list[int]]:
    """Split *content* into lines plus their start and end offsets.

    The end offset excludes the newline, so a caller replacing lines i..j can
    decide for itself whether to consume the trailing newline.
    """

    lines: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    start = 0
    for index, char in enumerate(content):
        if char == "\n":
            lines.append(content[start:index])
            starts.append(start)
            ends.append(index)
            start = index + 1
    lines.append(content[start:])
    starts.append(start)
    ends.append(len(content))
    return lines, starts, ends


def _split_pattern_lines(pattern: str) -> tuple[list[str], bool]:
    """Return the pattern's lines and whether it ended on a newline."""

    trailing = pattern.endswith("\n")
    body = pattern[:-1] if trailing else pattern
    return body.split("\n"), trailing


def _line_number(content: str, offset: int) -> int:
    return content.count("\n", 0, offset) + 1


def _leading_indent(line: str) -> str:
    match = _INDENT_RE.match(line)
    return match.group(0) if match else ""


def _first_meaningful_line(lines: Sequence[str]) -> str | None:
    for line in lines:
        if line.strip():
            return line
    return None


# ---------------------------------------------------------------------------
# normalization with original-coordinate maps
# ---------------------------------------------------------------------------


def _map_unicode(text: str) -> tuple[str, list[int], list[int]]:
    """Fold lookalike characters, tracking where each output char came from."""

    out: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    for index, char in enumerate(text):
        replacement = _UNICODE_LOOKALIKES.get(char, char)
        for piece in replacement:
            out.append(piece)
            starts.append(index)
            ends.append(index + 1)
    return "".join(out), starts, ends


def _map_collapsed_whitespace(text: str) -> tuple[str, list[int], list[int]]:
    """Collapse runs of spaces/tabs to one space, tracking source offsets.

    Newlines survive untouched so line structure still means something.
    """

    out: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char in " \t":
            run_start = index
            while index < length and text[index] in " \t":
                index += 1
            out.append(" ")
            starts.append(run_start)
            ends.append(index)
            continue
        out.append(char)
        starts.append(index)
        ends.append(index + 1)
        index += 1
    return "".join(out), starts, ends


def _find_all(haystack: str, needle: str) -> list[tuple[int, int]]:
    """Every non-overlapping occurrence of *needle*, as (start, end) spans."""

    if not needle:
        return []
    spans: list[tuple[int, int]] = []
    cursor = 0
    while True:
        found = haystack.find(needle, cursor)
        if found < 0:
            return spans
        spans.append((found, found + len(needle)))
        cursor = found + len(needle)


def _project_spans(
    spans: Sequence[tuple[int, int]],
    starts: Sequence[int],
    ends: Sequence[int],
) -> list[tuple[int, int]]:
    """Map spans in normalized coordinates back to the original string."""

    projected: list[tuple[int, int]] = []
    for span_start, span_end in spans:
        if span_end <= span_start or span_end > len(starts):
            continue
        projected.append((starts[span_start], ends[span_end - 1]))
    return projected


# ---------------------------------------------------------------------------
# line-block matching
# ---------------------------------------------------------------------------


def _block_span(
    starts: Sequence[int],
    ends: Sequence[int],
    first: int,
    last: int,
    *,
    content: str,
    include_newline: bool,
) -> tuple[int, int]:
    end = ends[last]
    if include_newline and end < len(content) and content[end] == "\n":
        end += 1
    return starts[first], end


def _keyed_block_spans(
    content: str,
    pattern: str,
    key: Callable[[str], str],
) -> list[tuple[int, int]]:
    """Match pattern lines against content lines under a per-line normalizer."""

    pattern_lines, trailing_newline = _split_pattern_lines(pattern)
    pattern_keys = [key(line) for line in pattern_lines]
    if not any(pattern_keys):
        # Every line normalizes to nothing — this would match whitespace
        # anywhere in the file. Refuse rather than pick a region at random.
        return []

    lines, starts, ends = _content_lines(content)
    window = len(pattern_keys)
    if window > len(lines):
        return []

    line_keys = [key(line) for line in lines]
    spans: list[tuple[int, int]] = []
    index = 0
    while index <= len(lines) - window:
        if line_keys[index : index + window] == pattern_keys:
            spans.append(
                _block_span(
                    starts,
                    ends,
                    index,
                    index + window - 1,
                    content=content,
                    include_newline=trailing_newline,
                )
            )
            index += window
            continue
        index += 1
    return spans


def _similarity(left: str, right: str) -> float:
    if not left and not right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


def _sweep_too_large(line_count: int, window: int) -> bool:
    """True when a full similarity sweep over this input is not worth its cost."""

    return line_count * window > _MAX_SWEEP_CELLS


# ---------------------------------------------------------------------------
# strategies
# ---------------------------------------------------------------------------


def _strategy_exact(content: str, pattern: str) -> list[tuple[int, int]]:
    return _find_all(content, pattern)


def _strategy_escape_normalized(content: str, pattern: str) -> list[tuple[int, int]]:
    unescaped = pattern
    for literal, actual in _ESCAPE_SEQUENCES:
        unescaped = unescaped.replace(literal, actual)
    if unescaped == pattern:
        return []
    return _find_all(content, unescaped)


def _strategy_unicode_normalized(content: str, pattern: str) -> list[tuple[int, int]]:
    normalized_content, starts, ends = _map_unicode(content)
    normalized_pattern, _, _ = _map_unicode(pattern)
    if normalized_content == content and normalized_pattern == pattern:
        return []
    return _project_spans(_find_all(normalized_content, normalized_pattern), starts, ends)


def _align_trimmed_span(
    content: str,
    span: tuple[int, int],
    *,
    restore_newline: bool,
) -> tuple[int, int]:
    """Grow a trimmed match back over the whitespace ``strip()`` removed.

    ``trimmed_boundary`` searches for ``old_text.strip()``, so its match begins
    after the indentation the file already carries and ends before the newline
    the pattern ended on. That span is not line-aligned, but the strategy is
    ``_INDENT_BLIND``: ``_resolve_replacement`` reads the landing indent off the
    span's own first line, finds none, and dedents every line of ``new_text``.
    That is right for the first line, which really does start mid-line, and
    wrong for every line after it -- those start at a line boundary and have to
    keep the file's indentation.

    Absorbing the leading whitespace and the trailing newline back into the span
    makes it line-aligned, so the shared re-indentation works as documented.
    Only characters ``strip()`` removed are absorbed, and only where the file
    actually holds them, so no other text joins the replaced region.

    Two matches can never be merged into one by this: a span only grows left
    over whitespace, which cannot contain an earlier match, and only grows right
    over a newline, which no later match can start before.
    """

    start, end = span
    line_start = content.rfind("\n", 0, start) + 1
    if start > line_start and not content[line_start:start].strip():
        start = line_start
    if restore_newline and end < len(content) and content[end] == "\n":
        end += 1
    return start, end


def _strategy_trimmed_boundary(content: str, pattern: str) -> list[tuple[int, int]]:
    trimmed = pattern.strip()
    if not trimmed or trimmed == pattern:
        return []
    # A newline anywhere in the trailing scrap means the caller's region ran to
    # the end of its line; the span has to run there too or the replacement
    # inserts a newline the caller never asked for.
    restore_newline = "\n" in pattern[len(pattern.rstrip()) :]
    return [
        _align_trimmed_span(content, span, restore_newline=restore_newline)
        for span in _find_all(content, trimmed)
    ]


def _strategy_indent_agnostic(content: str, pattern: str) -> list[tuple[int, int]]:
    return _keyed_block_spans(content, pattern, lambda line: line.lstrip(" \t").rstrip("\r"))


def _strategy_line_trimmed(content: str, pattern: str) -> list[tuple[int, int]]:
    return _keyed_block_spans(content, pattern, lambda line: line.strip())


def _strategy_whitespace_collapsed(content: str, pattern: str) -> list[tuple[int, int]]:
    normalized_content, starts, ends = _map_collapsed_whitespace(content)
    normalized_pattern, _, _ = _map_collapsed_whitespace(pattern)
    if normalized_content == content and normalized_pattern == pattern:
        return []
    return _project_spans(_find_all(normalized_content, normalized_pattern), starts, ends)


def _strategy_block_anchor(content: str, pattern: str) -> list[tuple[int, int]]:
    """Anchor on the first and last line, judge the middle on resemblance.

    This is how a model's paraphrased middle still lands on the right block:
    the boundaries are exact, only the interior drifted.
    """

    pattern_lines, trailing_newline = _split_pattern_lines(pattern)
    if len(pattern_lines) < _BLOCK_ANCHOR_MIN_LINES:
        return []
    head = pattern_lines[0].strip()
    tail = pattern_lines[-1].strip()
    if not head or not tail:
        return []

    lines, starts, ends = _content_lines(content)
    window = len(pattern_lines)
    if window > len(lines) or _sweep_too_large(len(lines), window):
        return []

    middle = "\n".join(line.strip() for line in pattern_lines[1:-1])
    spans: list[tuple[int, int]] = []
    for index in range(len(lines) - window + 1):
        last = index + window - 1
        if lines[index].strip() != head or lines[last].strip() != tail:
            continue
        candidate = "\n".join(line.strip() for line in lines[index + 1 : last])
        if _similarity(candidate, middle) < _BLOCK_ANCHOR_MIN_SIMILARITY:
            continue
        spans.append(
            _block_span(
                starts,
                ends,
                index,
                last,
                content=content,
                include_newline=trailing_newline,
            )
        )
    return spans


def _strategy_context_similarity(content: str, pattern: str) -> list[tuple[int, int]]:
    """Last resort: the single most similar block, if it is clearly the one.

    Requires both a high absolute score and a clear gap to the runner-up. Two
    comparable candidates mean the file genuinely has two similar blocks, and
    choosing between them is the model's job, not ours.
    """

    pattern_lines, trailing_newline = _split_pattern_lines(pattern)
    normalized_pattern = "\n".join(line.strip() for line in pattern_lines)
    if not normalized_pattern.strip():
        return []

    lines, starts, ends = _content_lines(content)
    window = len(pattern_lines)
    if window > len(lines) or _sweep_too_large(len(lines), window):
        return []

    best_score = 0.0
    best_index = -1
    runner_up = 0.0
    for index in range(len(lines) - window + 1):
        candidate = "\n".join(line.strip() for line in lines[index : index + window])
        score = _similarity(candidate, normalized_pattern)
        if score > best_score:
            runner_up = best_score
            best_score = score
            best_index = index
        elif score > runner_up:
            runner_up = score

    if best_index < 0 or best_score < _CONTEXT_MIN_SIMILARITY:
        return []
    if best_score - runner_up < _CONTEXT_MIN_MARGIN:
        return []
    return [
        _block_span(
            starts,
            ends,
            best_index,
            best_index + window - 1,
            content=content,
            include_newline=trailing_newline,
        )
    ]


_STRATEGY_FUNCTIONS: dict[str, Callable[[str, str], list[tuple[int, int]]]] = {
    "exact": _strategy_exact,
    "escape_normalized": _strategy_escape_normalized,
    "unicode_normalized": _strategy_unicode_normalized,
    "trimmed_boundary": _strategy_trimmed_boundary,
    "indent_agnostic": _strategy_indent_agnostic,
    "line_trimmed": _strategy_line_trimmed,
    "whitespace_collapsed": _strategy_whitespace_collapsed,
    "block_anchor": _strategy_block_anchor,
    "context_similarity": _strategy_context_similarity,
}


# ---------------------------------------------------------------------------
# replacement
# ---------------------------------------------------------------------------


def _reindent(new_text: str, *, pattern_indent: str, target_indent: str) -> str:
    """Re-hang *new_text* under the indentation of the region it replaces.

    The model wrote ``new_text`` against the indentation it believed the file
    had (``pattern_indent``). Where the file actually sits at
    ``target_indent``, every line shifts by the difference — relative structure
    inside the block is preserved, blank lines stay blank.
    """

    if pattern_indent == target_indent:
        return new_text

    out: list[str] = []
    for line in new_text.split("\n"):
        if not line.strip():
            out.append("")
            continue
        if pattern_indent and line.startswith(pattern_indent):
            out.append(target_indent + line[len(pattern_indent) :])
        elif not pattern_indent:
            out.append(target_indent + line)
        else:
            # The line is shallower than the pattern's own indent; keep whatever
            # relative depth it has rather than inventing one.
            out.append(target_indent + line.lstrip(" \t"))
    return "\n".join(out)


def _resolve_replacement(
    content: str,
    span: tuple[int, int],
    old_text: str,
    new_text: str,
    strategy: str,
) -> str:
    if strategy not in _INDENT_BLIND:
        return new_text

    pattern_first = _first_meaningful_line(_split_pattern_lines(old_text)[0])
    matched_region = content[span[0] : span[1]]
    matched_first = _first_meaningful_line(matched_region.split("\n"))
    if pattern_first is None or matched_first is None:
        return new_text
    return _reindent(
        new_text,
        pattern_indent=_leading_indent(pattern_first),
        target_indent=_leading_indent(matched_first),
    )


def _drop_overlaps(spans: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    ordered = sorted(spans)
    kept: list[tuple[int, int]] = []
    for span in ordered:
        if kept and span[0] < kept[-1][1]:
            continue
        kept.append(span)
    return kept


def find_closest_lines(content: str, old_text: str, *, max_results: int = 3) -> str:
    """Describe the regions that came closest, for a failed-match hint."""

    pattern_lines, _ = _split_pattern_lines(old_text)
    normalized_pattern = "\n".join(line.strip() for line in pattern_lines)
    lines, _, _ = _content_lines(content)
    window = min(len(pattern_lines), len(lines))
    if window == 0 or not normalized_pattern.strip():
        return ""
    if _sweep_too_large(len(lines), window):
        # No hint beats making a failed edit wait a minute for one.
        return ""

    scored: list[tuple[float, int]] = []
    for index in range(len(lines) - window + 1):
        candidate = "\n".join(line.strip() for line in lines[index : index + window])
        scored.append((_similarity(candidate, normalized_pattern), index))
    scored.sort(key=lambda item: (-item[0], item[1]))

    parts: list[str] = []
    for score, index in scored[:max_results]:
        if score < _HINT_MIN_SIMILARITY:
            continue
        preview = lines[index].strip()[:80]
        parts.append(f"line {index + 1} ({score:.0%} similar): {preview}")
    return "; ".join(parts)


def fuzzy_find_and_replace(
    content: str,
    old_text: str,
    new_text: str,
    *,
    replace_all: bool = False,
    strategies: Sequence[str] | None = None,
) -> FuzzyMatchResult:
    """Locate *old_text* in *content* and replace it with *new_text*.

    Tries each strategy in order and uses the first that finds anything.

    Raises:
        ValueError: *old_text* is empty.
        AmbiguousMatchError: the winning strategy found several regions and
            ``replace_all`` is False.
        FuzzyMatchError: no strategy found the text.
    """

    if not old_text:
        raise ValueError("old_text must not be empty")

    chain = tuple(strategies) if strategies is not None else STRATEGIES
    for strategy in chain:
        matcher = _STRATEGY_FUNCTIONS.get(strategy)
        if matcher is None:
            continue
        spans = _drop_overlaps(matcher(content, old_text))
        if not spans:
            continue

        if len(spans) > 1 and not replace_all:
            raise AmbiguousMatchError(
                f"old_text matches {len(spans)} locations (strategy: {strategy});"
                " be more specific",
                strategy=strategy,
                match_count=len(spans),
                lines=[_line_number(content, start) for start, _ in spans],
            )

        # Right to left, so each replacement leaves earlier offsets valid.
        updated = content
        for span in reversed(spans):
            replacement = _resolve_replacement(content, span, old_text, new_text, strategy)
            updated = updated[: span[0]] + replacement + updated[span[1] :]

        return FuzzyMatchResult(
            updated=updated,
            match_count=len(spans),
            strategy=strategy,
            spans=tuple(spans),
        )

    raise FuzzyMatchError(
        "old_text not found",
        hint=find_closest_lines(content, old_text),
    )
