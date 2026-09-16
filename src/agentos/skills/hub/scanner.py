"""Security scanner for SKILL.md files before installation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

# Patterns that indicate prompt injection attempts
_PROMPT_INJECTION = [
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.I),
    re.compile(r"override\s+(all\s+)?instructions", re.I),
    re.compile(r"you\s+are\s+now\s+(a\s+)?new\s+ai", re.I),
    re.compile(r"disregard\s+(all\s+)?(prior|previous)", re.I),
    re.compile(r"forget\s+(all\s+)?rules", re.I),
    re.compile(r"system\s*:\s*you\s+are", re.I),
]

# Patterns that indicate shell injection
_SHELL_INJECTION = [
    re.compile(r"\$\("),  # $(command)
    re.compile(r"`[^`]*\$\([^)]+\)[^`]*`"),  # backtick with subshell: `$(cmd)`
]

# Patterns that indicate data exfiltration
_EXFILTRATION = [
    re.compile(r"\b(curl|wget|nc|ncat)\s+['\"]?https?://(?!localhost|127\.0\.0\.1)", re.I),
    re.compile(r"\bfetch\s*\(\s*['\"]https?://(?!localhost|127\.0\.0\.1)", re.I),
]

# Hidden unicode patterns
_HIDDEN_UNICODE = [
    re.compile(r"[\u200b-\u200f\u2028-\u202f\u2060-\u206f\ufeff]"),  # zero-width chars
    re.compile(r"[\u202a-\u202e]"),  # directional overrides
]


@dataclass
class ScanFinding:
    """A single security finding."""

    category: str  # "prompt_injection" | "shell_injection" | "exfiltration" | "hidden_unicode"
    severity: str  # "warning" | "dangerous"
    line: int
    text: str
    pattern: str


@dataclass
class ScanResult:
    """Result of scanning a skill."""

    verdict: str = "safe"  # "safe" | "warning" | "dangerous"
    findings: list[ScanFinding] = field(default_factory=list)
    strategy: str = "skill-md-v1"


# A blockquote marker: up to three spaces, ">", and one optional space.
_QUOTE_MARKER_RE = re.compile(r"^ {0,3}>[ ]?")

# A bullet or ordered list marker, and the whitespace that sets its content
# indent. Used only to find how far a list item's content is indented, so that
# "four spaces in" is measured from there rather than from column zero.
_LIST_MARKER_RE = re.compile(r"^ *(?:[-*+]|\d{1,9}[.)])(?:[ \t]+|$)")


def _unquote(line: str) -> tuple[int, str]:
    """Split a line into its blockquote depth and the content inside."""

    depth = 0
    rest = line
    while True:
        match = _QUOTE_MARKER_RE.match(rest)
        if match is None:
            return depth, rest
        depth += 1
        rest = rest[match.end() :]


def _leading_width(line: str) -> int:
    """Indent width of a line, with tabs advancing to the next 4-column stop."""

    width = 0
    for char in line:
        if char == " ":
            width += 1
        elif char == "\t":
            width += 4 - (width % 4)
        else:
            break
    return width


def _fence_open(body: str, base_indent: int) -> tuple[str, int] | None:
    """Return the marker character and run length if ``body`` opens a fence."""

    if _leading_width(body) > base_indent + 3:
        return None
    marker = body.lstrip(" \t")
    if not marker or marker[0] not in "`~":
        return None
    char = marker[0]
    run = len(marker) - len(marker.lstrip(char))
    if run < 3:
        return None
    # A backtick fence's info string may not itself contain a backtick,
    # otherwise ``a `b` c`` would read as an opening fence.
    if char == "`" and "`" in marker[run:]:
        return None
    return char, run


def _fence_closes(body: str, char: str, size: int, base_indent: int) -> bool:
    """Whether ``body`` is a closing fence for an open ``char``/``size`` fence."""

    if _leading_width(body) > base_indent + 3:
        return False
    marker = body.lstrip(" \t")
    if not marker or marker[0] != char:
        return False
    run = len(marker) - len(marker.lstrip(char))
    # A closing fence must use the same character, be at least as long as the
    # opener, and carry no info string.
    return run >= size and not marker[run:].strip()


def _strip_code_blocks(text: str) -> str:
    """Replace code blocks with blank lines to preserve line numbering.

    This exists so that example commands in documentation are not scored the
    same as a live exfiltration attempt. It previously used a single
    unanchored regex that recognised only exactly-three-backtick fences;
    every other block form CommonMark defines was left as prose, and a
    ``dangerous`` verdict hard-blocks a hub install.

    Both fence markers are now scanned line by line with one set of rules, so
    the marker character and its run length are known: a fence closes only on
    the same character at no less than the opening length, which is what lets a
    ````` ```` ````` fence contain a literal ``` ``` ``` (the usual way to
    document fence syntax) and what stops a ``~~~`` block from being closed by
    a stray backtick run. An unclosed fence runs to the end of the document,
    as CommonMark specifies.

    Indented blocks are measured from the enclosing list item's content indent
    rather than from column zero. That distinction matters in both directions:
    ``- step`` followed by a six-space line is code, while the four-space
    continuation paragraph of a ``1.`` item is prose and stays scanned.

    Where the spec is ambiguous this under-recognises rather than over-
    recognises. Missing a real code block costs a false positive the author can
    pass ``force=True`` around; exempting real prose costs a missed detection,
    which is the direction that actually matters here.
    """

    lines = text.split("\n")
    out = list(lines)
    total = len(lines)
    quote_depth = 0
    list_indent = 0
    after_blank = True
    index = 0

    while index < total:
        depth, body = _unquote(lines[index])
        if not body.strip():
            after_blank = True
            index += 1
            continue

        # Leaving or entering a blockquote starts a fresh list context.
        if depth != quote_depth:
            quote_depth = depth
            list_indent = 0
        width = _leading_width(body)
        if width < list_indent:
            list_indent = 0

        fence = _fence_open(body, list_indent)
        if fence is not None:
            char, size = fence
            out[index] = ""
            cursor = index + 1
            while cursor < total:
                inner_depth, inner_body = _unquote(lines[cursor])
                if inner_depth == depth and _fence_closes(inner_body, char, size, list_indent):
                    break
                out[cursor] = ""
                cursor += 1
            if cursor < total:
                out[cursor] = ""
            index = cursor + 1
            after_blank = False
            continue

        if after_blank and width >= list_indent + 4:
            cursor = index
            while cursor < total:
                inner_depth, inner_body = _unquote(lines[cursor])
                if not inner_body.strip():
                    cursor += 1
                    continue
                if inner_depth != depth or _leading_width(inner_body) < list_indent + 4:
                    break
                cursor += 1
            # Trailing blank lines belong to whatever follows, not to the block.
            end = cursor
            while end > index and not _unquote(lines[end - 1])[1].strip():
                end -= 1
            for blanked in range(index, end):
                out[blanked] = ""
            index = end
            after_blank = False
            continue

        marker = _LIST_MARKER_RE.match(body)
        if marker is not None:
            list_indent = marker.end()
        after_blank = False
        index += 1

    return "\n".join(out)


def scan_skill(skill_md_content: str) -> ScanResult:
    """Scan a SKILL.md file for security concerns.

    Returns a ScanResult with verdict and findings.
    Code blocks are excluded from shell/exfiltration checks
    (shell commands inside code examples are expected).
    """
    findings: list[ScanFinding] = []
    lines = skill_md_content.split("\n")
    stripped = _strip_code_blocks(skill_md_content)
    stripped_lines = stripped.split("\n")

    # Check prompt injection (full text — these are dangerous anywhere)
    for i, line in enumerate(lines, 1):
        for pat in _PROMPT_INJECTION:
            if pat.search(line):
                findings.append(
                    ScanFinding(
                        category="prompt_injection",
                        severity="dangerous",
                        line=i,
                        text=line.strip()[:100],
                        pattern=pat.pattern,
                    )
                )

    # Check shell injection (outside code blocks only)
    for i, line in enumerate(stripped_lines, 1):
        for pat in _SHELL_INJECTION:
            if pat.search(line):
                findings.append(
                    ScanFinding(
                        category="shell_injection",
                        severity="warning",
                        line=i,
                        text=line.strip()[:100],
                        pattern=pat.pattern,
                    )
                )

    # Check exfiltration (outside code blocks only)
    for i, line in enumerate(stripped_lines, 1):
        for pat in _EXFILTRATION:
            if pat.search(line):
                findings.append(
                    ScanFinding(
                        category="exfiltration",
                        severity="dangerous",
                        line=i,
                        text=line.strip()[:100],
                        pattern=pat.pattern,
                    )
                )

    # Check hidden unicode (full text)
    for i, line in enumerate(lines, 1):
        for pat in _HIDDEN_UNICODE:
            if pat.search(line):
                findings.append(
                    ScanFinding(
                        category="hidden_unicode",
                        severity="dangerous",
                        line=i,
                        text=repr(line.strip()[:80]),
                        pattern=pat.pattern,
                    )
                )

    # Determine verdict
    if any(f.severity == "dangerous" for f in findings):
        verdict = "dangerous"
    elif findings:
        verdict = "warning"
    else:
        verdict = "safe"

    return ScanResult(verdict=verdict, findings=findings, strategy="skill-md-v1")


def scan_skill_bundle(files: Mapping[str, str | bytes]) -> ScanResult:
    """Scan an install bundle, including text sidecars and binary inventory.

    ``scan_skill`` remains the SKILL.md scanner. This bundle-level wrapper keeps
    the installer verdict honest when a package contains additional files.
    Binary files are not inspected, so they become warning findings instead of
    allowing the bundle to be reported as fully safe.
    """
    findings: list[ScanFinding] = []
    for rel_path, content in sorted(files.items()):
        if isinstance(content, bytes):
            findings.append(
                ScanFinding(
                    category="unscanned_binary",
                    severity="warning",
                    line=0,
                    text=rel_path[:100],
                    pattern="binary file not scanned",
                )
            )
            continue

        result = scan_skill(content)
        for finding in result.findings:
            findings.append(
                ScanFinding(
                    category=finding.category,
                    severity=finding.severity,
                    line=finding.line,
                    text=f"{rel_path}: {finding.text}"[:100],
                    pattern=finding.pattern,
                )
            )

    if any(f.severity == "dangerous" for f in findings):
        verdict = "dangerous"
    elif findings:
        verdict = "warning"
    else:
        verdict = "safe"
    return ScanResult(verdict=verdict, findings=findings, strategy="bundle-v1")
