"""Policy for the browser ``eval`` action — JS evaluation in a page context.

Ported from NousResearch/hermes-agent ``tools/browser_tool`` (MIT, Copyright
(c) 2025 Nous Research) — see ``THIRD_PARTY_NOTICES.md``. The functions here are
the same two-tier eval guard Hermes ships:

* an **opt-in** denylist (``browser.restrict_evaluate``, default off) that refuses
  expressions touching sensitive browser primitives — matched both as bare
  identifiers and as string-literal property names so ``document["coo"+"kie"]``
  cannot slip past a check on ``document.cookie``;
* an SSRF pre-scan of ``http(s)://`` literals in the expression, so a
  ``fetch('http://169.254.169.254/…')`` that never updates ``location.href`` is
  refused before it runs.

The denylist is *off by default* on purpose: gating on primitive *names*
cripples legitimate DOM extraction (Hermes reached the same conclusion). It is a
belt for operators who want it, not the load-bearing control — output redaction
(:func:`redact_browser_output`) and the network SSRF guards are.

Everything here is a pure function over the expression string plus process-wide
SSRF configuration; no browser is required to exercise it.
"""

from __future__ import annotations

import re
from typing import Any

from agentos.redact import redact_sensitive_text
from agentos.tools.ssrf import assert_not_metadata_endpoint, validate_http_url_for_fetch

# ---------------------------------------------------------------------------
# Denylist (opt-in): risky primitives, as regexes and as bare token names.
# ---------------------------------------------------------------------------

#: Direct-spelling patterns. Each carries a human-readable reason so a refusal
#: can name *what* it blocked without echoing the expression back.
_RISKY_EVAL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bdocument\s*\.\s*cookie\b", re.I), "document.cookie"),
    (re.compile(r"\b(?:localStorage|sessionStorage)\b", re.I), "web storage"),
    (re.compile(r"\bindexedDB\b", re.I), "IndexedDB"),
    (re.compile(r"\bcaches\s*\.\s*(?:open|match|keys)\b", re.I), "Cache Storage"),
    (
        re.compile(r"\bnavigator\s*\.\s*(?:clipboard|credentials|serviceWorker)\b", re.I),
        "navigator sensitive API",
    ),
    (
        re.compile(r"\b(?:fetch|XMLHttpRequest|WebSocket|EventSource)\s*\(", re.I),
        "network request",
    ),
    (re.compile(r"\bnavigator\s*\.\s*sendBeacon\s*\(", re.I), "network beacon"),
    (re.compile(r"\bdocument\s*\.\s*forms\b.*\bvalue\b", re.I | re.S), "form value extraction"),
    (
        re.compile(
            r"\bquerySelector(?:All)?\s*\([^)]*(?:input|textarea|password)[^)]*\).*\bvalue\b",
            re.I | re.S,
        ),
        "form value extraction",
    ),
)

#: Token names re-checked against decoded string literals, to catch bracket /
#: concatenation obfuscation (``document["coo" + "kie"]``).
_SENSITIVE_EVAL_TOKENS: tuple[tuple[str, str], ...] = (
    ("cookie", "document.cookie"),
    ("localStorage", "web storage"),
    ("sessionStorage", "web storage"),
    ("indexedDB", "IndexedDB"),
    ("caches", "Cache Storage"),
    ("clipboard", "navigator sensitive API"),
    ("credentials", "navigator sensitive API"),
    ("serviceWorker", "navigator sensitive API"),
    ("fetch", "network request"),
    ("XMLHttpRequest", "network request"),
    ("WebSocket", "network request"),
    ("EventSource", "network request"),
    ("sendBeacon", "network beacon"),
)

#: JS string literals — single, double, or backtick quoted, with escapes.
_JS_STRING_LITERAL_RE = re.compile(
    r"""'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`""",
    re.S,
)

#: ``http(s)://…`` literals embedded in the expression (fetch/XHR/navigation
#: targets the model may have written). The post-eval page-URL recheck can't see
#: a direct fetch that never touches ``location.href``, so pre-screen here.
_JS_URL_LITERAL_RE = re.compile(r"""https?://[^\s'"`)\]<>]+""", re.IGNORECASE)

#: Protocol-relative targets — ``//host/path`` inherits the page's scheme and
#: reaches the network exactly like an ``http(s)://`` spelling. The host must
#: look like a hostname or IP (dotted, or ``localhost``) so a JavaScript line
#: comment such as ``//todo`` is not mistaken for a URL.
_JS_PROTOCOL_RELATIVE_RE = re.compile(
    r"""//(?:localhost|[0-9A-Za-z_-]+(?:\.[0-9A-Za-z_-]+)+)(?::\d+)?(?:/[^\s'"`)\]<>]*)?""",
    re.IGNORECASE,
)


def _decode_js_string_literal(literal: str) -> str:
    """Best-effort decode of a single quoted JS string literal to its value."""
    if len(literal) < 2:
        return literal
    body = literal[1:-1]
    # Only the escapes that matter for hiding a token name; anything exotic is
    # left as-is because the concatenation pass below still catches it.
    return (
        body.replace("\\\\", "\\")
        .replace("\\'", "'")
        .replace('\\"', '"')
        .replace("\\`", "`")
        .replace("\\/", "/")
    )


def _decoded_js_string_literals(expression: str) -> list[str]:
    """Return the decoded values of every string literal in *expression*."""
    return [_decode_js_string_literal(match) for match in _JS_STRING_LITERAL_RE.findall(expression)]


def _sensitive_eval_token_reason(expression: str) -> str | None:
    """Reason if a sensitive primitive appears as an identifier or literal name.

    A denylist that only searches direct spellings like ``document.cookie``
    misses ``document["cookie"]`` and ``document["coo" + "kie"]``. Treat token
    names as risky whether they appear as identifiers or as decoded
    string-literal property names, and also scan the concatenation of every
    literal to catch simple split obfuscation.
    """
    string_literals = _decoded_js_string_literals(expression)
    concatenated = "".join(string_literals).lower()
    for token, reason in _SENSITIVE_EVAL_TOKENS:
        if re.search(rf"\b{re.escape(token)}\b", expression, re.I):
            return reason
        token_lower = token.lower()
        if any(token_lower in literal.lower() for literal in string_literals):
            return reason
        if token_lower in concatenated:
            return reason
    return None


def risky_eval_reason(expression: str) -> str | None:
    """Return a human-readable reason if *expression* uses risky primitives."""
    if not expression:
        return None
    for pattern, reason in _RISKY_EVAL_PATTERNS:
        if pattern.search(expression):
            return reason
    return _sensitive_eval_token_reason(expression)


def enforce_eval_policy(
    expression: str,
    *,
    restrict_evaluate: bool,
    allow_unsafe_evaluate: bool,
) -> str | None:
    """Return a refusal message when the opt-in denylist blocks *expression*.

    ``restrict_evaluate`` off (the default) → never blocks here. ``allow_unsafe``
    is the escape hatch when the denylist is on but a page is trusted. Network
    egress to private addresses is enforced separately by
    :func:`expression_targets_private_url` and does not depend on this policy.
    """
    if not restrict_evaluate or allow_unsafe_evaluate:
        return None
    reason = risky_eval_reason(expression)
    if not reason:
        return None
    return (
        f"Blocked: browser eval tried to use a sensitive JavaScript primitive "
        f"({reason}) while browser.restrict_evaluate is enabled. Use snapshot / "
        f"screenshot for normal inspection, or set browser.restrict_evaluate = "
        f"false (or browser.allow_unsafe_evaluate = true) to permit programmatic "
        f"evaluation."
    )


def _url_is_blocked(url: str) -> bool:
    """True when *url* targets a private/internal or cloud-metadata address."""
    try:
        assert_not_metadata_endpoint(url)
    except Exception:  # noqa: BLE001 - any raise means blocked
        return True
    try:
        validate_http_url_for_fetch(url)
    except Exception:  # noqa: BLE001 - private/internal/unsupported → blocked
        return True
    return False


def expression_targets_private_url(expression: str) -> str | None:
    """Return the first private/internal URL literal in *expression*, if any.

    Best-effort scan for ``http(s)://…`` literals; returns the first that targets
    a private/internal address or the always-blocked cloud-metadata floor, else
    ``None``.

    Obfuscated spellings are covered too:

    * protocol-relative — ``fetch('//169.254.169.254/…')`` is normalized to an
      ``http://`` candidate before the same private/internal checks run;
    * split-string — ``fetch('htt' + 'p://169.254.169.254/…')`` hides the
      scheme across two literals, so the concatenation of every decoded string
      literal is scanned the same way
      ``_sensitive_eval_token_reason`` reconstructs ``document["coo" + "kie"]``.
    """
    if not isinstance(expression, str):
        return None
    # Scan the raw expression and, separately, the concatenation of every
    # decoded string literal. The latter reconstructs a scheme split across
    # literals (`'htt' + 'p://host'`), the same technique
    # `_sensitive_eval_token_reason` uses for `document["coo" + "kie"]`.
    haystacks = (expression, "".join(_decoded_js_string_literals(expression)))
    candidates: list[str] = []
    for haystack in haystacks:
        for match in _JS_URL_LITERAL_RE.findall(haystack):
            candidates.append(str(match).rstrip(".,;"))
        # Normalize protocol-relative targets to a scheme-bearing URL so the
        # shared private/internal checks (which require http/https) can judge
        # them instead of rejecting them on scheme alone.
        for match in _JS_PROTOCOL_RELATIVE_RE.findall(haystack):
            candidates.append("http:" + str(match).rstrip(".,;"))
    for candidate in candidates:
        if _url_is_blocked(candidate):
            return candidate
    return None


def redact_browser_output(value: Any) -> Any:
    """Recursively mask credentials in browser-originated data.

    Snapshots, console messages, JS errors, and eval results can carry
    page-rendered API keys, cookies, or bearer tokens. Tool output is a model
    boundary, so redaction is forced here even if global log redaction is off.
    """
    if isinstance(value, str):
        return redact_sensitive_text(value, force=True)
    if isinstance(value, list):
        return [redact_browser_output(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_browser_output(item) for item in value)
    if isinstance(value, dict):
        return {key: redact_browser_output(item) for key, item in value.items()}
    return value
