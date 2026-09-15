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


_BACKTICK_FENCE_RE = re.compile(r"```[\s\S]*?```")
_TILDE_FENCE_RE = re.compile(r"~~~[\s\S]*?~~~")


def _replace_with_blanks(m: re.Match[str]) -> str:
    return "\n" * m.group(0).count("\n")


def _strip_fenced_code_blocks(text: str) -> str:
    """Replace fenced code blocks with blank lines to preserve line numbering.

    CommonMark fences may open with backticks *or* tildes; only backticks
    were recognised before this, so a perfectly ordinary ``~~~bash`` example
    -- CommonMark-valid, and common in some doc generators -- was left as
    plain text and scored the same as a live exfiltration attempt.

    The backtick pattern is untouched from before this fix, on purpose: it
    is a separate, independent pattern from the new tilde one rather than
    one generalised regex covering both markers. A single pattern needs a
    backreference to stop a ``` block from being closed by a stray ~~~ (or
    the reverse), which in turn requires capturing the *whole* opening
    marker run -- and that changes existing backtick behavior in one edge
    case: a fence of four-or-more backticks whose body itself contains a
    literal run of exactly three (documenting fence syntax itself, for
    instance) used to have its body only partially recognised, which
    happened to leave any content after that literal run still scanned as
    prose. Generalizing to `` `{3,}` `` most likely closes that as a real
    gap too, but that is a different, broader change than this issue's
    reported one, made without the same case-by-case verification the rest
    of this fix got -- so it is left alone here, and backticks and tildes
    are matched independently instead.
    """
    text = _BACKTICK_FENCE_RE.sub(_replace_with_blanks, text)
    return _TILDE_FENCE_RE.sub(_replace_with_blanks, text)


def _strip_indented_code_blocks(text: str) -> str:
    """Replace CommonMark indented code blocks (4+ spaces, or a tab) with
    blank lines, the same way fenced ones are stripped.

    Deliberately conservative in both directions a security check can fail
    (see 6.6): only a run of indented lines bounded by a blank line (or
    start/end of text) on *both* sides is treated as code. CommonMark itself
    doesn't require a trailing blank line to end the block -- a change in
    indentation is enough -- so this recognises strictly fewer blocks than
    the spec, and says nothing about list-item or blockquote continuation
    text, which a fuller block parser would need to place correctly. That
    means some real indented code stays scanned as prose; the alternative
    (an indentation heuristic that's too eager) risks exempting real prose
    from the checks below, which is the direction that actually matters for
    a security scanner -- under-recognizing costs a false positive an
    author can route around with force=True, over-recognizing costs a
    missed detection.
    """
    lines = text.split("\n")
    out = list(lines)
    total = len(lines)
    i = 0
    while i < total:
        indented = lines[i].startswith("    ") or lines[i].startswith("\t")
        if indented and (i == 0 or lines[i - 1].strip() == ""):
            j = i
            while j < total and (
                lines[j].startswith("    ") or lines[j].startswith("\t") or lines[j].strip() == ""
            ):
                j += 1
            end = j
            while end > i and lines[end - 1].strip() == "":
                end -= 1
            if end > i and (end == total or lines[end].strip() == ""):
                for k in range(i, end):
                    out[k] = ""
                i = end
                continue
        i += 1
    return "\n".join(out)


def _strip_code_blocks(text: str) -> str:
    """Replace every recognised code block (fenced or indented) with blank
    lines, to preserve line numbering for the checks that run on the rest.
    """
    return _strip_indented_code_blocks(_strip_fenced_code_blocks(text))


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
