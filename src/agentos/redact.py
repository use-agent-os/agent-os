"""Secret redaction and credential-literal detection.

Two jobs, one vocabulary of patterns:

* :func:`redact_sensitive_text` masks credentials in text that is about to
  reach a log file, a transcript, or the model's context. This is the primary
  defence — a secret the model never sees is a secret it cannot leak.
* :func:`secret_literal_marker` answers the narrower question "does this text
  contain a credential *value*?" for the payload guards on ``http_request``
  and ``exec_command``.

The distinction matters, and getting it wrong is what made the previous guard
unusable. Matching on **names** ("this header is called ``x-api-key``, refuse
to send it") blocks the ordinary case — every authenticated API call — while
missing the real one, because a pasted key does not announce itself in a
variable name. So the rules here are:

1. **Names are matched on segment boundaries, never as substrings.** A key is
   split on ``-``/``_``/``.``/camelCase humps and only qualified pairs
   (``api``+``key``, ``access``+``token``) or unambiguous single words
   (``secret``, ``password``) count. ``sellToken``, ``tokenAddress`` and
   ``token_count`` are ordinary field names, not credentials — in a web3
   payload "token" is an asset.
2. **A name alone is never enough.** The value must also look like a literal
   secret. ``CAP_API_KEY=$(jq -r .CAP_API_KEY creds.json)`` names a credential
   but contains none; the value is a reference the shell resolves later.
3. **Values are matched on shape.** Vendor prefixes (``sk-``, ``ghp_``,
   ``AKIA``…), PEM blocks, JWTs and DSN passwords are recognisable on sight
   and carry the match on their own, whatever they are assigned to.

Redaction is on by default and its state is read **once at import**. An agent
that runs ``export AGENTOS_REDACT_SECRETS=0`` mid-session must not be able to
unmask the rest of that session; operators who genuinely need raw values set
the variable before AgentOS starts. The name is also on
:data:`agentos.env_policy.WRITE_DENYLIST`, so no AgentOS surface can persist
it on the agent's behalf.
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Callable, Iterable, Mapping

__all__ = [
    "is_env_dump_command",
    "mask_secret",
    "reads_credential_file",
    "redact_file_output",
    "redact_sensitive_text",
    "redact_terminal_output",
    "secret_literal_marker",
]


def _flag(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


#: Snapshot at import — see the module docstring for why this is not re-read.
_REDACT_ENABLED = _flag("AGENTOS_REDACT_SECRETS", default=True)

#: Operator escape hatch for the payload guards, mirroring
#: ``AGENTOS_SENSITIVE_PATHS_DISABLED``. Snapshotted for the same reason.
_PAYLOAD_GUARD_DISABLED = _flag("AGENTOS_SENSITIVE_PAYLOAD_DISABLED", default=False)


# ── Value shapes ────────────────────────────────────────────────────────────
#
# A credential recognisable from its own text. These carry a match without any
# help from a surrounding name, which is what makes them worth listing: they
# are the only evidence that survives ``curl -H "x-api-key: sk-live-..."``,
# where the name says nothing the guard can use.
#
# Deliberately not exhaustive. Every entry here is a vendor that stamps a
# fixed prefix on its keys; vendors that issue opaque random strings cannot be
# recognised this way and are covered by the env scrub instead.
_PREFIX_PATTERNS: tuple[str, ...] = (
    r"sk-ant-[A-Za-z0-9_-]{16,}",  # Anthropic
    r"sk-or-v1-[A-Za-z0-9]{16,}",  # OpenRouter
    r"sk-proj-[A-Za-z0-9_-]{16,}",  # OpenAI project key
    r"sk-[A-Za-z0-9]{20,}",  # OpenAI and lookalikes
    r"sk_live_[A-Za-z0-9]{16,}",  # Stripe live
    r"sk_test_[A-Za-z0-9]{16,}",  # Stripe test
    r"rk_live_[A-Za-z0-9]{16,}",  # Stripe restricted
    r"ghp_[A-Za-z0-9]{16,}",  # GitHub PAT (classic)
    r"gho_[A-Za-z0-9]{16,}",  # GitHub OAuth
    r"ghu_[A-Za-z0-9]{16,}",  # GitHub user-to-server
    r"ghs_[A-Za-z0-9]{16,}",  # GitHub server-to-server
    r"ghr_[A-Za-z0-9]{16,}",  # GitHub refresh
    r"github_pat_[A-Za-z0-9_]{16,}",  # GitHub PAT (fine-grained)
    r"gsk_[A-Za-z0-9]{16,}",  # Groq
    r"xai-[A-Za-z0-9]{20,}",  # xAI
    r"AIza[A-Za-z0-9_-]{30,}",  # Google API key
    r"AKIA[A-Z0-9]{16}",  # AWS access key id
    r"ASIA[A-Z0-9]{16}",  # AWS temporary credentials
    r"ABIA[A-Z0-9]{16}",  # AWS Backint Agent key id
    r"ACCA[A-Z0-9]{16}",  # AWS Backup key id
    r"xox[baprs]-[A-Za-z0-9-]{16,}",  # Slack
    r"xapp-\d+-[A-Za-z0-9-]{16,}",  # Slack app-level
    r"hf_[A-Za-z0-9]{16,}",  # Hugging Face
    r"npm_[A-Za-z0-9]{16,}",  # npm
    r"pypi-[A-Za-z0-9_-]{16,}",  # PyPI
    r"tvly-[A-Za-z0-9]{16,}",  # Tavily
    r"fc-[A-Za-z0-9]{16,}",  # Firecrawl
    r"r8_[A-Za-z0-9]{16,}",  # Replicate
    r"ntn_[A-Za-z0-9]{16,}",  # Notion
    r"SG\.[A-Za-z0-9_-]{16,}",  # SendGrid
)
#: Anchored on the left so ``AKIA…`` inside a base64 blob is not mistaken for
#: a key — masking it corrupts the blob on a read-then-write round trip.
_PREFIX_RE = re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(_PREFIX_PATTERNS) + ")")

#: ``<scheme>://[user]:token@host`` — userinfo is a credential whatever the
#: scheme in front of it is. Redaction-only: the payload guard keeps its
#: narrower connection-string vocabulary (``_DB_CONNSTR_RE``).
#:
#: Matched structurally rather than against a scheme list, because a list only
#: ever covers the schemes someone thought of: ``ws``/``wss`` (a gateway URL
#: with basic auth), ``ftp``, ``sftp``, ``ssh``, ``smtp``, ``ldap`` and any
#: database scheme outside the five in ``_DB_CONNSTR_RE`` were all handed to
#: the model verbatim (#3432).
#:
#: The username part is ``*`` rather than ``+`` so an empty one still matches:
#: ``redis://:password@host`` is the canonical Redis spelling and carries no
#: username at all.
_URL_USERINFO_RE = re.compile(
    r"([a-z][a-z0-9+.-]*://[^:@\s/]*:)([^@\s/]+)(@)",
    re.IGNORECASE,
)

_PEM_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE)
_PEM_PRIVATE_KEY_BLOCK_RE = re.compile(
    r"-----BEGIN[A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z0-9 ]*PRIVATE KEY-----",
    re.IGNORECASE,
)
#: ``root:x:0:0:`` — a line lifted out of ``/etc/passwd`` or ``/etc/shadow``.
_PASSWD_ENTRY_RE = re.compile(r"(?m)^(?:\d+\t)?[a-z_][a-z0-9_-]*:x?:\d+:\d+:")
#: JWTs always start with the base64 of ``{``.
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{16,}(?:\.[A-Za-z0-9_=-]{8,}){1,2}")
#: ``postgres://user:PASSWORD@host`` and friends. Whitespace is excluded from
#: both halves so a match can never span a line break.
_DB_CONNSTR_RE = re.compile(
    r"((?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^:\s/]+:)([^@\s]+)(@)",
    re.IGNORECASE,
)

# ── Names ───────────────────────────────────────────────────────────────────
#
# Single segments that mean "credential" on their own. ``token`` and ``key``
# are pointedly absent: a token is an asset in web3 payloads and a key is a
# map entry everywhere else. They only count in the qualified pairs below.
_STRONG_NAME_SEGMENTS: frozenset[str] = frozenset(
    {
        "secret",
        "password",
        "passwd",
        # A passphrase is a password that happens to be a sentence, and it is
        # the one credential no shape rule can ever catch: `_is_secret_literal_value`
        # requires a single opaque run with no whitespace, so
        # ``correct horse battery staple`` is invisible to the value pass. The
        # name is the only thing standing between it and the model. The word
        # sits alongside ``password`` and ``passwd`` rather than being treated
        # more conservatively than they are — ``password_required`` and
        # ``passwd_hint`` are already recognized today.
        "passphrase",
        "apikey",
        "credential",
        "credentials",
        "authorization",
    }
)

#: Adjacent segment pairs that together name a credential. ``sellToken`` splits
#: to ``sell`` + ``token`` and matches nothing; ``x-cap-api-key`` splits to
#: ``x`` + ``cap`` + ``api`` + ``key`` and matches on ``api`` + ``key``.
_QUALIFIED_NAME_PAIRS: frozenset[tuple[str, str]] = frozenset(
    {
        ("api", "key"),
        ("api", "token"),
        ("api", "secret"),
        ("access", "key"),
        ("access", "token"),
        ("refresh", "token"),
        ("id", "token"),
        ("auth", "token"),
        ("auth", "key"),
        ("bearer", "token"),
        # ``token`` is qualified by what issues it. ``bot`` was missing, so
        # ``SLACK_BOT_TOKEN`` / ``DISCORD_BOT_TOKEN`` / ``TELEGRAM_BOT_TOKEN``
        # were not credential names at all — and for Discord and Telegram the
        # value pass does not cover them either, so a correctly-shaped bot
        # token reached the model verbatim.
        ("bot", "token"),
        ("client", "secret"),
        ("private", "key"),
        ("secret", "key"),
        ("session", "token"),
        ("service", "key"),
        # Qualifiers that can only mean key material. ``key`` on its own stays
        # out (``sort_key``, ``partition_key`` are field names), but nothing
        # signs a token, encrypts a column or authenticates a storage account
        # with a value the model is meant to read.
        ("signing", "key"),
        ("encryption", "key"),
        ("account", "key"),
    }
)

_NAME_SPLIT_RE = re.compile(
    # Separators, then the two camel-case boundaries. The second one is what
    # makes an all-caps acronym split from a Capitalised word after it:
    # `APISecret` has no lower-to-upper transition, so without it the whole
    # name stays one segment and matches nothing, while `apiSecret`,
    # `ApiSecret`, `API_SECRET` and `api-secret` all match. Same credential,
    # four spellings, one of them leaked.
    r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])"
)


def _name_segments(name: str) -> list[str]:
    """Split an identifier into lower-cased word segments.

    Handles the casings a credential name arrives in: ``CAP_API_KEY``,
    ``x-cap-api-key``, ``capApiKey`` and ``APISecret`` all reduce to the same
    segments.
    """
    return [segment.lower() for segment in _NAME_SPLIT_RE.split(name) if segment]


def _is_credential_name(name: str) -> bool:
    """Return whether *name* names a credential on a segment boundary."""
    segments = _name_segments(name)
    if any(segment in _STRONG_NAME_SEGMENTS for segment in segments):
        return True
    return any(pair in _QUALIFIED_NAME_PAIRS for pair in zip(segments, segments[1:], strict=False))


# ── Values that are references, not secrets ─────────────────────────────────
#
# ``CAP_API_KEY=$CAP_API_KEY``, ``"apiKey": os.getenv("X")`` and
# ``--data @body.json`` all name where a value lives instead of carrying it.
# Treating them as leaks is what pushed agents into writing the real key to a
# file and running that instead — strictly worse than the thing being blocked.
_REFERENCE_VALUE_RE = re.compile(
    r"""^(?:
        \$\{?[A-Za-z_][A-Za-z0-9_]*\}?      # $VAR / ${VAR}
      | \$\(.*                              # $(command substitution)
      | `.*                                 # `command substitution`
      | @[^\s]+                             # @file (curl --data @body.json)
      | os\.(?:getenv|environ)\b.*          # Python
      | process\.env\b.*                    # Node
      | System\.getenv\b.*                  # Java
      | ENV\[.*                             # Ruby
      | <[^>]*>                             # <YOUR_KEY_HERE> placeholders
      | (?:x{3,}|\*{3,}|\.{3,}|•{3,})       # already-masked values
      | (?:your|my|the)[-_ ].*              # your-api-key-here
      | (?:changeme|placeholder|example|redacted|dummy|test|fake|none|null|true|false)
    )$""",
    re.IGNORECASE | re.VERBOSE,
)

#: A filesystem path or URL assigned to a credential-named key says *where* the
#: credential lives, which is the same kind of pointer as ``$VAR``.
#: ``GOOGLE_APPLICATION_CREDENTIALS=/etc/secrets/creds.json`` and
#: ``api_key_file=./certs/server.pem`` are configuration, not leaks.
_LOCATION_VALUE_RE = re.compile(
    r"""^(?:
        [~.]{0,2}/          # /abs, ./rel, ../rel, ~/home
      | [A-Za-z]:[\\/]      # C:\ or C:/
      | \\\\                # \\unc\share
      | [a-z][a-z0-9+.\-]*://
    )""",
    re.IGNORECASE | re.VERBOSE,
)

#: Below this length a value carries too little entropy to be worth blocking,
#: and short placeholders ("KEY", "abc") are common in documentation.
_MIN_SECRET_VALUE_LEN = 12


def _is_reference_value(value: str) -> bool:
    """Return whether *value* points at a secret rather than being one."""
    stripped = value.strip().strip("\"'")
    if not stripped:
        return True
    if _REFERENCE_VALUE_RE.match(stripped):
        return True
    # A path or URL is a location, unless it carries userinfo — in
    # ``https://user:token@host`` the credential is right there in the value.
    return bool(_LOCATION_VALUE_RE.match(stripped)) and "@" not in stripped


def _is_secret_literal_value(value: str) -> bool:
    """Return whether *value* is plausibly a credential written out in full."""
    stripped = value.strip().strip("\"'")
    if _is_reference_value(stripped):
        return False
    # An earlier pass already masked this one. Re-masking would collapse the
    # head/tail the first pass deliberately left behind for debuggability.
    if _MASK in stripped or "«redacted" in stripped:
        return False
    if _PREFIX_RE.search(stripped) or _JWT_RE.search(stripped):
        return True
    if len(stripped) < _MIN_SECRET_VALUE_LEN:
        return False
    # A credential is one opaque run of characters. Anything with whitespace or
    # shell/URL structure in it is a sentence, a command, or a path.
    return not re.search(r"[\s;|&<>()]", stripped)


# ── Assignment forms ────────────────────────────────────────────────────────
#
# ``NAME=value`` and ``"name": "value"``. The name half is captured loosely and
# then judged by :func:`_is_credential_name` so the segment rules live in one
# place instead of being re-encoded in every regex.
_ASSIGNMENT_RE = re.compile(
    r"""(?ix)
    (?:^|[\s"'{,(;])                    # start of a token
    (?:\d+\t)?                          # grep -n line prefix
    (?:[+\-]{1,2})?                     # diff marker: unified +/-, combined ++/--
    ([A-Za-z][A-Za-z0-9_.\-]{0,64})     # name
    ['\"]?                              # closing quote of a JSON/YAML key
    \s*[:=]\s*
    (?:
        "([^"\n]{1,4096})"
      | '([^'\n]{1,4096})'
      | ([^\s,;)}\]"']{1,4096})
    )
    """
)

#: A header value ends at whitespace, at a quote, or at the punctuation that
#: closes the structure carrying it. Running past ``}``/``)``/``]``/``;`` is
#: what turned ``{"xi-api-key": api_key}`` into unbalanced source code.
_HEADER_VALUE = r"[^\s\"',;)}\]`]+"
#: Names are matched on a segment boundary, never as a substring, for the same
#: reason as :func:`_is_credential_name`: ``requiresApiKey`` is a field name.
_NAME_START = r"(?<![A-Za-z0-9_])"
_AUTH_HEADER_RE = re.compile(
    rf"({_NAME_START}(?:Proxy-)?Authorization['\"]?\s*:\s*['\"]?)"
    rf"([A-Za-z][\w.+-]*\s+)?({_HEADER_VALUE})",
    re.IGNORECASE,
)
_SECRET_HEADER_NAMES = (
    r"(?:x-api-key|x-goog-api-key|api-key|apikey|x-api-token|x-auth-token|x-access-token)"
)
_SECRET_HEADER_RE = re.compile(
    rf"({_NAME_START}{_SECRET_HEADER_NAMES}['\"]?\s*:\s*['\"]?)({_HEADER_VALUE})",
    re.IGNORECASE,
)


def secret_literal_marker(text: str | None) -> str | None:
    """Return a marker when *text* carries credential material, else ``None``.

    This is the payload guard's whole vocabulary, and it is deliberately much
    narrower than what :func:`redact_sensitive_text` masks. Redaction is cheap
    to be wrong about — a masked non-secret costs a little readability. A block
    is expensive to be wrong about: it stops the task, and the model routes
    around it. So only material with **no legitimate outbound use at all**
    blocks:

    * a PEM private key,
    * an ``/etc/passwd``-shaped account line,
    * a vendor-prefixed provider key (``sk-ant-``, ``ghp_``, ``AKIA``…),
    * a connection string with an inline password.

    Everything else — including an opaque API key in an ``x-api-key`` header,
    which is simply how authenticated APIs work — is left to the caller. An
    opaque key sent to the service that issued it is the normal case and cannot
    be told apart from exfiltration by looking at the bytes; that distinction
    is enforced by keeping the credential out of the model's context in the
    first place (see :mod:`agentos.tools.env_passthrough`), not here.

    Even the four blocking cases have a working alternative: reference the
    value (``$OPENAI_API_KEY``) instead of pasting it, and the child process
    resolves it without the literal ever entering the transcript.
    """
    if not text or _PAYLOAD_GUARD_DISABLED:
        return None
    if _PEM_PRIVATE_KEY_RE.search(text):
        return "private_key"
    if _PASSWD_ENTRY_RE.search(text):
        return "passwd_entry"
    if _has_known_prefix(text) and _PREFIX_RE.search(text):
        return "credential_literal"
    if "://" in text and _DB_CONNSTR_RE.search(text):
        return "connection_string"
    return None


def credential_text_marker(text: str | None) -> str | None:
    """Return a marker for text about to be handed to an unrelated third party.

    Broader than :func:`secret_literal_marker`, and deliberately so. Sending an
    opaque key to the service that issued it is the normal case; sending it to
    a search engine, a scraping backend, or any party that has no business with
    it is not, and no legitimate query looks like ``API_KEY=<value>``. At that
    boundary a credential-shaped assignment is evidence enough, because the
    receiving side is wrong regardless of what the value turns out to be.
    """
    if not text or _PAYLOAD_GUARD_DISABLED:
        return None
    marker = secret_literal_marker(text)
    if marker is not None:
        return marker
    for match in _ASSIGNMENT_RE.finditer(text):
        if not _is_credential_name(match.group(1)):
            continue
        value = match.group(2) or match.group(3) or match.group(4) or ""
        if _is_secret_literal_value(value):
            return "secret_assignment"
    return None


def _iter_header_values(headers: object) -> list[str]:
    """Return header values from whatever shape the caller passed.

    A mapping is the documented form, but a list of ``(name, value)`` pairs is
    equally valid to the HTTP client underneath. Assuming the mapping raised
    ``AttributeError`` on the pair form — a guard that crashes instead of
    inspecting is a guard that stopped reading its input.
    """
    if isinstance(headers, Mapping):
        return [value for value in headers.values() if isinstance(value, str)]
    if isinstance(headers, str | bytes):
        return []
    if isinstance(headers, Iterable):
        values: list[str] = []
        for item in headers:
            if isinstance(item, str | bytes):
                continue
            if isinstance(item, Iterable):
                parts = list(item)
                if len(parts) == 2 and isinstance(parts[1], str):
                    values.append(parts[1])
        return values
    return []


def secret_header_marker(headers: object) -> str | None:
    """Return a marker when a header **value** carries credential material.

    Header *names* are not evidence. ``Authorization`` and ``x-api-key`` are how
    authenticated APIs work; refusing them refuses the tool its purpose. Only
    the value is inspected, under the same narrow rules as
    :func:`secret_literal_marker`.
    """
    if not headers:
        return None
    for value in _iter_header_values(headers):
        marker = secret_literal_marker(value)
        if marker is not None:
            return marker
    return None


# ── Masking ─────────────────────────────────────────────────────────────────

_MASK = "***"
#: Below this length, showing any of the value is showing too much of it.
_MASK_REVEAL_FLOOR = 18


def mask_secret(value: str, *, head: int = 6, tail: int = 4) -> str:
    """Mask *value* for display, keeping a short head and tail when it is long."""
    if not value:
        return value
    if len(value) < _MASK_REVEAL_FLOOR:
        return _MASK
    return f"{value[:head]}{_MASK}{value[-tail:]}"


def _mask_token(token: str) -> str:
    return mask_secret(token)


def _mask_nonreusable(token: str) -> str:
    """Mask *token* as something that cannot be mistaken for a usable key.

    A head/tail mask reads as a real-but-truncated credential. An agent that
    reads one out of a config file and writes it back replaces the working
    value with a dead 13-character string, and the failure surfaces much later
    as an unexplained 401. The sentinel is syntactically invalid as a token, so
    a round trip through the model corrupts nothing silently.
    """
    prefix = token[:4] if len(token) > 8 else ""
    return f"«redacted:{prefix}…»" if prefix else "«redacted»"


def redact_sensitive_text(
    text: str | None,
    *,
    force: bool = False,
    code_file: bool = False,
    file_read: bool = False,
) -> str | None:
    """Mask credentials in *text*.

    Safe on any string; text with nothing to mask comes back unchanged.

    ``force=True`` redacts regardless of the global preference, for boundaries
    that must never emit a raw credential. ``code_file=True`` skips the
    assignment passes for text known to be source code, where ``MAX_TOKENS=4096``
    and ``"apiKey": "test"`` fixtures are not leaks. ``file_read=True`` is for
    file content handed back to the agent: secrets are still masked, but with
    the non-reusable sentinel so the agent cannot write a corrupted value back.
    The two are orthogonal — file content is often source code, and
    :func:`redact_file_output` decides which of the two a given path is.
    """
    if text is None:
        return None
    if not isinstance(text, str):
        text = str(text)
    if not text or not (force or _REDACT_ENABLED):
        return text

    mask = _mask_nonreusable if file_read else _mask_token
    text = _redact_value_shapes(text, mask=mask)
    return _redact_named_credentials(text, mask=mask, assignments=not code_file)


def _redact_value_shapes(text: str, *, mask: Callable[[str], str], line_safe: bool = False) -> str:
    """Mask credentials recognisable from their own text alone.

    PEM blocks, vendor-prefixed keys and JWTs carry the match without any help
    from a surrounding name, which makes this pass safe to run on anything —
    source code included. ``line_safe`` keeps a collapsed PEM block from
    swallowing the line numbers around it in a ``read_file`` window.
    """
    if "-----BEGIN" in text:
        text = _redact_pem_blocks(text, line_safe=line_safe)
    if _has_known_prefix(text):
        text = _PREFIX_RE.sub(lambda m: mask(m.group(0)), text)
    if "eyJ" in text:
        text = _JWT_RE.sub(lambda m: mask(m.group(0)), text)
    return text


def _redact_named_credentials(
    text: str,
    *,
    mask: Callable[[str], str],
    assignments: bool,
    dsn_mask: str = _MASK,
) -> str:
    """Mask values that are credentials because of the *name* next to them.

    Unlike :func:`_redact_value_shapes` this pass reads structure, not shape,
    so it cannot tell ``apiKey: NotRequired[str]`` from ``apiKey: <secret>``.
    Callers holding source code keep it off.
    """
    if "://" in text:
        text = _URL_USERINFO_RE.sub(
            lambda m: (
                f"{m.group(1)}{dsn_mask}{m.group(3)}"
                if not _is_reference_value(m.group(2))
                else m.group(0)
            ),
            text,
        )
        text = _DB_CONNSTR_RE.sub(
            lambda m: (
                f"{m.group(1)}{dsn_mask}{m.group(3)}"
                if not _is_reference_value(m.group(2))
                else m.group(0)
            ),
            text,
        )
    if ":" in text:
        text = _AUTH_HEADER_RE.sub(
            lambda m: (
                f"{m.group(1)}{m.group(2) or ''}{mask(m.group(3))}"
                if _is_maskable_header_value(m.group(3))
                else m.group(0)
            ),
            text,
        )
        text = _SECRET_HEADER_RE.sub(
            lambda m: (
                f"{m.group(1)}{mask(m.group(2))}"
                if _is_maskable_header_value(m.group(2))
                else m.group(0)
            ),
            text,
        )
    if assignments and ("=" in text or ":" in text):
        text = _redact_assignments(text, mask=mask)
    return text


#: A bare integer is a count or an id, never a credential. ``"authorization":
#: 20104`` is a token-vocabulary entry in a tokenizer, not a header.
_NUMERIC_VALUE_RE = re.compile(r"^[-+]?\d+(?:\.\d+)?$")


def _is_maskable_header_value(value: str) -> bool:
    """Return whether a header value is worth masking.

    Deliberately *not* :func:`_is_secret_literal_value`: a short opaque session
    token is still a credential, and applying that predicate's length floor here
    would un-mask values this has always masked. This only drops the two shapes
    that provably carry nothing — a number, and a placeholder like
    ``Bearer <token>`` in documentation.
    """
    stripped = value.strip().strip("\"'")
    if not stripped or _NUMERIC_VALUE_RE.match(stripped):
        return False
    return not _is_reference_value(stripped)


#: ``12\tMIIEow…`` — the line-number prefix ``read_file`` puts on every line.
_LINE_NUMBER_PREFIX_RE = re.compile(r"^(\d+\t)?")


#: A PEM body is base64, optionally preceded by ``Proc-Type:``-style headers.
#: Anything else between the markers means they are two string literals in a
#: source file, not one key — masking that span destroys the code between them.
_PEM_BODY_LINE_RE = re.compile(r"^(?:\d+\t)?(?:[A-Za-z0-9+/=.…]*|[A-Za-z][A-Za-z-]*: .*)$")


def _is_pem_key_block(block: str) -> bool:
    """Return whether a BEGIN/END span is one key rather than two literals.

    A ``_SK_START = b"…OPENSSH PRIVATE KEY…"`` marker constant on one line and
    ``_SK_END = …`` on the next is a match with no body: real code that masking
    would eat. A key on a single line is the JSON-escaped form
    (``"private_key": "…BEGIN…\\nMIIE…"``) and does carry a secret.
    """
    lines = block.split("\n")
    if len(lines) == 1:
        return True
    body = [line.strip("\r") for line in lines[1:-1]]
    if not any(body):
        return False
    return all(_PEM_BODY_LINE_RE.match(line) for line in body)


def _redact_pem_blocks(text: str, *, line_safe: bool) -> str:
    """Mask PEM private-key blocks, optionally one line at a time.

    Collapsing the block to a single token is right for a log line and wrong
    for a numbered file window: the reader sees line 12 followed by line 41 and
    computes the next ``offset=`` from a file that looks shorter than it is.
    """
    if not line_safe:
        return _PEM_PRIVATE_KEY_BLOCK_RE.sub(
            lambda m: "«redacted:private-key»" if _is_pem_key_block(m.group(0)) else m.group(0),
            text,
        )

    def _mask_block(match: re.Match[str]) -> str:
        if not _is_pem_key_block(match.group(0)):
            return match.group(0)
        masked = []
        for line in match.group(0).split("\n"):
            prefix = _LINE_NUMBER_PREFIX_RE.match(line)
            masked.append(f"{prefix.group(1) or '' if prefix else ''}«redacted:private-key»")
        return "\n".join(masked)

    return _PEM_PRIVATE_KEY_BLOCK_RE.sub(_mask_block, text)


def _redact_assignments(text: str, *, mask: Callable[[str], str] = _mask_token) -> str:
    """Mask the value half of ``NAME=secret`` / ``"name": "secret"`` pairs."""

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        value = match.group(2) or match.group(3) or match.group(4) or ""
        if not _is_credential_name(name) or not _is_secret_literal_value(value):
            return match.group(0)
        return match.group(0).replace(value, mask(value))

    return _ASSIGNMENT_RE.sub(_replace, text)


def _literal_prefix(pattern: str) -> str:
    """Return the leading literal characters of a regex pattern."""
    literal: list[str] = []
    for char in pattern:
        if char in "\\[](){}.*+?|^$":
            break
        literal.append(char)
    return "".join(literal)


#: Cheap substring gate for :data:`_PREFIX_RE`. Derived from the patterns so a
#: newly added prefix cannot silently fall outside the gate.
_PREFIX_GATES: tuple[str, ...] = tuple(
    sorted({prefix for prefix in (_literal_prefix(p) for p in _PREFIX_PATTERNS) if prefix})
)


def _has_known_prefix(text: str) -> bool:
    return any(gate in text for gate in _PREFIX_GATES)


# ── Terminal output ─────────────────────────────────────────────────────────

#: Commands that print the environment whatever their arguments: ``printenv``
#: with names prints those names' values, which is still environment output.
_ALWAYS_ENV_DUMP_COMMANDS = frozenset({"printenv"})

#: Shell builtins that print the environment only when given nothing to do.
#: ``set -e`` sets an option, ``export X=y`` assigns, ``declare -A m``
#: declares -- none of them print anything, and treating them as dumps runs
#: the assignment pass over whatever the *next* command prints, which for a
#: ``set -euo pipefail`` script is the whole script's output.
_BARE_ENV_DUMP_BUILTINS = frozenset({"set", "export", "declare", "typeset"})

#: Commands that run their operand, so ``sudo printenv`` is ``printenv``. Each
#: maps to the options of its own that take a separate argument, so that
#: ``sudo -u root printenv`` skips ``root`` and finds the command -- the
#: option lists differ per wrapper (``sudo -n`` takes nothing, ``nice -n``
#: takes a number), so they cannot be one shared set.
_COMMAND_WRAPPERS: Mapping[str, frozenset[str]] = {
    "sudo": frozenset({"-u", "-g", "-p", "-C", "-D", "-h", "-r", "-t", "-T", "-U"}),
    "doas": frozenset({"-u", "-C"}),
    "command": frozenset(),
    "exec": frozenset({"-a"}),
    "nohup": frozenset(),
    "nice": frozenset({"-n"}),
    "time": frozenset({"-f", "-o"}),
    "busybox": frozenset(),
}

#: ``env`` options that take a separate argument. ``-S``/``--split-string``
#: is deliberately absent: its argument *is* a command, handled below.
_ENV_OPTIONS_WITH_ARGUMENT = frozenset({"-u", "--unset", "-C", "--chdir"})

#: The separators that end one command and start the next. A newline is one of
#: them — in POSIX shell it does the same job as ``;`` — and ``exec_command``
#: hands the whole string to ``create_subprocess_shell``, so a two-line script
#: is an ordinary thing for an agent to run. Leaving ``\n`` out classified
#: ``cd /srv\nprintenv`` as not-a-dump while the identical ``cd /srv &&
#: printenv`` was masked.
_SEGMENT_SEPARATOR_RE = re.compile(r"[|;&\n\r]+")

#: ``(printenv)`` and ``(cd /srv; printenv)`` keep the grouping character glued
#: to the command by the time ``shlex`` is done with them.
_SHELL_GROUPING_CHARS = "(){}"

#: A redirection is not an operand: ``env 2>&1`` still prints the environment
#: and ``set >vars.txt`` is still a bare ``set``. Matches the operator at the
#: front of a token (``2>&1``, ``2>/dev/null``, ``>out``, ``&>log``, ``<in``).
_REDIRECTION_RE = re.compile(r"^(\d+|&)?(>>?|<<?<?)")

#: The operator alone (``2>``, ``>``, ``>>``, ``<``): the target is the token
#: after it, and neither is an operand.
_BARE_REDIRECTION_RE = re.compile(r"^(\d+|&)?(>>?|<<?<?)$")


def _unwrap_command(tokens: list[str]) -> list[str]:
    """Peel ``sudo -E``, ``command``, ``exec`` ... off the front of *tokens*.

    Returns the tokens of the command the wrapper actually runs, or an empty
    list when the wrapper had nothing to run.
    """
    while tokens:
        options_with_argument = _COMMAND_WRAPPERS.get(os.path.basename(tokens[0]).lower())
        if options_with_argument is None:
            return tokens
        rest = tokens[1:]
        while rest and rest[0].startswith("-"):
            option = rest.pop(0)
            if option == "--":
                break
            if option in options_with_argument and rest:
                rest.pop(0)
        tokens = rest
    return tokens


def _without_redirections(tokens: list[str]) -> list[str]:
    """Drop redirections and their targets, which are never operands."""
    kept: list[str] = []
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if _BARE_REDIRECTION_RE.match(token):
            skip_next = True
            continue
        if _REDIRECTION_RE.match(token):
            continue
        kept.append(token)
    return kept


def _env_operand(arguments: list[str]) -> list[str] | None:
    """The command ``env`` runs with *arguments*, as its own token list.

    Empty when nothing remains after ``env``'s options and ``NAME=value``
    assignments, which is when ``env`` prints the environment. ``None`` for
    ``-S``, whose argument carries a command this parser does not split.
    """
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            return arguments[index + 1 :]
        if argument.startswith("-"):
            if argument in ("-S", "--split-string") or argument.startswith("--split-string="):
                return None
            if argument in _ENV_OPTIONS_WITH_ARGUMENT:
                index += 1
            index += 1
            continue
        if "=" in argument:
            index += 1
            continue
        return arguments[index:]
    return []


def _segment_dumps_environment(tokens: list[str]) -> bool:
    """Whether one pipeline or sequence segment prints the environment."""
    tokens = _unwrap_command(tokens)
    if not tokens:
        return False
    name = os.path.basename(tokens[0]).lower()
    arguments = tokens[1:]
    if name in _ALWAYS_ENV_DUMP_COMMANDS:
        return True
    if name == "env":
        # ``env`` with nothing to run prints; with something to run it is
        # whatever that is, so ``env -i printenv`` and ``env sudo printenv``
        # are judged as the command they run.
        operand = _env_operand(arguments)
        if operand is None:
            return False
        return not operand or _segment_dumps_environment(operand)
    if name == "set":
        # ``set -o`` lists options and ``set -- a b`` sets the positionals;
        # only a bare ``set`` prints variables.
        return not arguments
    if name in _BARE_ENV_DUMP_BUILTINS:
        # ``export`` / ``declare -p`` with no names print everything; a name
        # or an assignment means the builtin is being used to *set* one.
        # ``declare -p NAME`` prints that variable's value and stays a dump.
        if all(argument.startswith("-") for argument in arguments):
            return True
        return name in ("declare", "typeset") and any(
            argument.startswith("-") and not argument.startswith("--") and "p" in argument[1:]
            for argument in arguments
        )
    return False


def is_env_dump_command(command: str | None) -> bool:
    """Return whether *command* prints the environment to stdout.

    Looks at every pipeline or sequence segment, with shell grouping characters
    stripped off. The command is found under any wrapper that runs it
    (``sudo printenv``) and by its basename (``/usr/bin/env``), and its
    arguments decide what it does: ``env python3 build.py`` runs Python and
    ``set -e`` sets an option, so neither is a dump.

    Conservative: anything it cannot parse is reported as not-a-dump, and the
    caller falls back to the pass that has fewer false positives.
    """
    if not command or not isinstance(command, str):
        return False
    for segment in _SEGMENT_SEPARATOR_RE.split(command):
        segment = segment.strip()
        if not segment:
            continue
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = segment.split()
        tokens = [
            stripped
            for stripped in (token.strip(_SHELL_GROUPING_CHARS) for token in tokens)
            if stripped
        ]
        if _segment_dumps_environment(_without_redirections(tokens)):
            return True
    return False


def reads_credential_file(command: str | None) -> bool:
    """Return whether *command* names a credential file as an operand.

    ``cat ~/.aws/credentials`` is the same disclosure as ``read_file`` on it,
    and blocking only the tool just moves the agent to the shell. Any operand
    that is not source code by :func:`_is_source_code_path` and is either a
    known credential file or lives in a credential directory counts.
    """
    if not command or not isinstance(command, str):
        return False
    for token in _command_operands(command):
        if token.startswith("-"):
            continue
        # ``basename`` on a POSIX host does not split on ``\``; normalise so a
        # Windows-native operand is judged by its real name.
        name = os.path.basename(token.replace("\\", "/"))
        if _is_credential_file_name(name) or _in_credential_dir(token):
            return True
    return False


def _command_operands(command: str) -> list[str]:
    """Split *command* into tokens, keeping Windows-native paths intact.

    ``shlex`` in POSIX mode reads ``\\`` as an escape, so an unquoted
    ``C:\\Users\\u\\.aws\\credentials`` came out as ``C:Usersu.awscredentials``
    and matched nothing. On a Windows host that spelling is the natural one
    for ``type`` or ``Get-Content``, so a command carrying a backslash is
    also split with escapes off, and both readings are checked.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if "\\" in command:
        try:
            literal = shlex.split(command, posix=False)
        except ValueError:
            literal = command.split()
        tokens.extend(token.strip("'\"") for token in literal)
    return tokens


#: Credential files whose secret is positional or keyword-introduced rather
#: than ``name=value``. The assignment pass cannot see a secret it cannot
#: name, so these get a rule of their own, keyed by basename. Both are in
#: :data:`_CREDENTIAL_FILE_NAMES` -- the gate fired for them all along; what
#: ran behind it could not read the file.
_CREDENTIAL_FILE_FORMATS: Mapping[str, str] = {
    ".pgpass": "pgpass",
    ".netrc": "netrc",
    "_netrc": "netrc",
}

#: ``.netrc`` introduces each value with a keyword: ``machine H login U
#: password P``. ``account`` is a second password in the same format, and a
#: value may be quoted (curl reads quotes; ftp does not).
#: ``\s+`` rather than blanks only: ``.netrc`` tokens are whitespace-separated,
#: so the value may sit on the line after its keyword (``password\n  secret``).
_NETRC_SECRET_RE = re.compile(r'(?i)\b(password|passwd|account)(\s+)("(?:[^"\\]|\\.)*"|\S+)')


def _mask_whole(_token: str) -> str:
    """Mask a password entirely.

    The head/tail reveal in :func:`mask_secret` exists so an agent can tell
    *which* vendor key it is looking at. A positional password has no such
    prefix, so showing six characters of it shows six characters of a password.
    """
    return _MASK


def _mask_whole_nonreusable(_token: str) -> str:
    return _mask_nonreusable("")


def _credential_file_format(path: str | os.PathLike[str] | None) -> str | None:
    if path is None:
        return None
    # Normalise separators first: a Windows path handed to a POSIX host has
    # no ``/`` for ``basename`` to split on, as ``_is_source_code_path`` knows.
    name = os.path.basename(os.fspath(path).replace("\\", "/")).lower()
    return _CREDENTIAL_FILE_FORMATS.get(name)


def _credential_file_formats_in(command: str) -> set[str]:
    """The formats of every credential file *command* names as an operand."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    formats: set[str] = set()
    for token in tokens:
        if token.startswith("-"):
            continue
        file_format = _credential_file_format(token)
        if file_format is not None:
            formats.add(file_format)
    return formats


def _pgpass_password_start(line: str) -> int | None:
    """Offset of the fifth ``:``-field, honouring ``\\:`` and ``\\\\`` escapes.

    ``.pgpass`` is ``hostname:port:database:username:password``; the password
    is everything after the fourth unescaped colon, escapes included.
    """
    colons = 0
    index = 0
    while index < len(line):
        char = line[index]
        if char == "\\":
            index += 2
            continue
        if char == ":":
            colons += 1
            if colons == 4:
                return index + 1
        index += 1
    return None


def _redact_pgpass(text: str, *, mask: Callable[[str], str]) -> str:
    masked: list[str] = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body) :]
        start = None if body.lstrip().startswith("#") else _pgpass_password_start(body)
        if start is None or start >= len(body):
            masked.append(line)
            continue
        masked.append(body[:start] + mask(body[start:]) + ending)
    return "".join(masked)


def _redact_netrc(text: str, *, mask: Callable[[str], str]) -> str:
    return _NETRC_SECRET_RE.sub(
        lambda match: match.group(1) + match.group(2) + mask(match.group(3)), text
    )


def _redact_credential_file_format(
    text: str, file_format: str, *, mask: Callable[[str], str]
) -> str:
    if file_format == "pgpass":
        return _redact_pgpass(text, mask=mask)
    if file_format == "netrc":
        return _redact_netrc(text, mask=mask)
    return text


def redact_terminal_output(output: str, command: str | None = None, *, force: bool = False) -> str:
    """Mask credentials in command output before it reaches the model.

    One policy for every terminal surface — foreground ``exec_command`` and
    background process polling alike — so the two cannot drift. ``env`` and
    friends get the assignment pass, because that is exactly the shape their
    output has, and so does a command that reads a credential file, whose
    output is that file. Everything else skips it, because ordinary output is
    source code and config dumps where the assignment pass is mostly false
    positives.

    A credential file whose secret is positional (``.pgpass``) or
    keyword-introduced (``.netrc``) gets its own format rule first: the
    assignment pass cannot name a value that has no name.
    """
    if not output:
        return output
    if command and (force or _REDACT_ENABLED):
        for file_format in sorted(_credential_file_formats_in(command)):
            output = _redact_credential_file_format(output, file_format, mask=_mask_whole)
    assignments = is_env_dump_command(command or "") or reads_credential_file(command)
    redacted = redact_sensitive_text(output, force=force, code_file=not assignments)
    return redacted if redacted is not None else output


#: Suffixes whose content is source code. The name-driven pass is kept off
#: these: it cannot tell ``apiKey: NotRequired[str]`` or
#: ``api_key=self._api_key`` from a real secret, and a masked identifier is
#: code the agent can no longer match with ``edit_file`` — or worse, writes
#: back with the sentinel in it. Shape-matched credentials (``sk-…``, JWTs, PEM
#: blocks) are still masked here; those carry their own evidence.
_SOURCE_CODE_SUFFIXES: frozenset[str] = frozenset(
    {
        ".bash",
        ".c",
        ".cc",
        ".cjs",
        ".cpp",
        ".cs",
        ".css",
        ".dart",
        ".erl",
        ".ex",
        ".exs",
        ".fish",
        ".go",
        ".h",
        ".hpp",
        ".hs",
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".kts",
        ".lua",
        ".m",
        ".mjs",
        ".mm",
        ".php",
        ".pl",
        ".proto",
        ".ps1",
        ".py",
        ".pyi",
        ".r",
        ".rb",
        ".rs",
        ".scala",
        ".sh",
        ".sql",
        ".svelte",
        ".swift",
        ".ts",
        ".tsx",
        ".vue",
        ".zsh",
    }
)

#: Files that are nothing but credentials, matched whole because they carry no
#: suffix. ``~/.aws/credentials`` is the case #355 was filed about.
_CREDENTIAL_FILE_NAMES: frozenset[str] = frozenset(
    {
        ".dockercfg",
        ".git-credentials",
        ".htpasswd",
        ".netrc",
        ".npmrc",
        ".pgpass",
        ".pypirc",
        "_netrc",
        "credentials",
        # The trading vault's auto-unlock password file.
        "unlock.key",
    }
)

#: Public name for the set above. The sandbox's sensitive-path denylist derives
#: its credential-file entries from this so the two layers cannot drift; the
#: private name stays as-is for this module's own callers.
CREDENTIAL_FILE_NAMES: frozenset[str] = _CREDENTIAL_FILE_NAMES

#: Home-relative directories whose every file is credential material. This is
#: the one list for both layers: the sandbox denylist blocks ``read_file``
#: under ``~/<entry>``, and :func:`reads_credential_file` gates ``cat`` of the
#: same path. It used to be two lists, and they drifted -- #1138 added
#: ``~/.azure`` to the sandbox and nothing to redaction, so ``read_file`` on
#: ``~/.azure/service_principal_entries.json`` was blocked while ``cat`` of
#: it handed the model ``client_secret`` intact (#2621). An entry may be more
#: than one segment (``.config/gh``), and matches as a contiguous run of
#: segments anywhere in a path, so a single-segment entry like ``.aws``
#: matches under any parent.
#:
#: ``.docker`` is the directory, not ``.docker/config`` as the sandbox once
#: had it: Docker writes ``config.json``, and a prefix match anchored at a
#: segment boundary never matched the file that exists (#2623).
#: ``AppData/Roaming/gcloud`` is where the Cloud SDK keeps its credentials
#: on Windows, so the same entry covers both platforms.
CREDENTIAL_HOME_DIRS: tuple[str, ...] = (
    ".ssh",
    ".aws",
    ".azure",
    ".config/gcloud",
    "AppData/Roaming/gcloud",
    ".config/gh",
    ".anthropic",
    ".openai",
    ".docker",
    ".kube",
    ".gnupg",
    ".password-store",
)

#: Files masked when read but **not** blocked by the sandbox. Each holds a
#: credential often enough to deserve the assignment pass, and build or tool
#: configuration often enough that hard-blocking ``read_file`` on it would
#: break ordinary work -- ``gradle.properties`` sits in every Gradle project,
#: usually with nothing secret in it, and sometimes with ``signing.password``.
#: Files, not directories: gating all of ``~/.cargo`` or ``~/.gradle`` would
#: strip the source-code exemption from every crate and JVM source under
#: their caches, and the one file that would have gained from it,
#: ``~/.m2/settings.xml``, holds its password in XML the pass cannot read.
_REDACT_ONLY_CREDENTIAL_FILE_NAMES: frozenset[str] = frozenset(
    {
        ".boto",
        ".my.cnf",
        ".s3cfg",
        ".yarnrc.yml",
        "credentials.tfrc.json",
        "credentials.toml",
        "gradle.properties",
    }
)

#: ``service-account.json``, ``my-project-service_account-key.json`` -- a GCP
#: service-account key by any of its usual names.
_SERVICE_ACCOUNT_FILE_RE = re.compile(r"service[-_]?account.*\.json$", re.IGNORECASE)


def _is_credential_file_name(name: str) -> bool:
    lowered = name.lower()
    return (
        lowered in _CREDENTIAL_FILE_NAMES
        or lowered in _REDACT_ONLY_CREDENTIAL_FILE_NAMES
        or lowered.startswith(".env")
        or _SERVICE_ACCOUNT_FILE_RE.search(lowered) is not None
    )


def _in_credential_dir(path: str) -> bool:
    """Whether *path* sits under one of the credential directories.

    An entry's segments must appear as a contiguous run in the path's
    directory segments, so ``.config/gh`` matches ``~/.config/gh/hosts.yml``
    and not ``~/.config/other/gh/x``.
    """
    segments = [part.lower() for part in path.replace("\\", "/").split("/")[:-1]]
    for entry in CREDENTIAL_HOME_DIRS:
        wanted = entry.lower().split("/")
        width = len(wanted)
        if any(segments[i : i + width] == wanted for i in range(len(segments) - width + 1)):
            return True
    return False


def _is_source_code_path(path: str | os.PathLike[str] | None) -> bool:
    """Return whether *path* is source code rather than configuration data."""
    if path is None:
        return False
    text = os.fspath(path)
    name = os.path.basename(text)
    if _is_credential_file_name(name) or _in_credential_dir(text):
        return False
    return os.path.splitext(name.lower())[1] in _SOURCE_CODE_SUFFIXES


def redact_file_output(text: str, *, path: str | os.PathLike[str] | None = None) -> str:
    """Mask credentials in file content before it reaches the model.

    One policy for every file-read surface — ``read_file``, ``read_spreadsheet``,
    ``grep_search`` and ``edit_file``'s closest-match hint alike — so they
    cannot drift. This sits *behind* the sensitive-path denylist rather than
    replacing it: the denylist is switched off entirely under elevated-full
    mode, so it cannot be the only thing standing between ``~/.aws/credentials``
    and the persisted transcript.

    Masking uses the non-reusable sentinel (see :func:`_mask_nonreusable`): an
    agent that reads a config file and writes it back must not silently replace
    a working key with a truncated one that fails a request hours later.

    *path* decides how much of the pass runs. Shape-matched credentials — PEM
    blocks, vendor-prefixed keys, JWTs — are masked in every file. The
    name-driven pass, which is the only one that catches a shapeless secret like
    ``aws_secret_access_key``, runs everywhere **except** source code, where it
    would mask identifiers and hand back code that no longer matches the file.
    Without a path, the conservative source-code reading applies.
    """
    if not text or not _REDACT_ENABLED:
        return text
    file_format = _credential_file_format(path)
    if file_format is not None:
        text = _redact_credential_file_format(text, file_format, mask=_mask_whole_nonreusable)
    masked = _redact_value_shapes(text, mask=_mask_nonreusable, line_safe=True)
    if _is_source_code_path(path) or path is None:
        return masked
    return _redact_named_credentials(
        masked, mask=_mask_nonreusable, assignments=True, dsn_mask="«redacted»"
    )


#: CDP endpoint URLs (``ws(s)://…/devtools/browser/<token>``) carry a
#: browser-session token in the path. Mask it in any log line so an attach /
#: cloud debug endpoint never lands in the transcript verbatim.
_CDP_WS_URL_RE = re.compile(
    r"(wss?://[^\s'\"]*?/devtools/(?:browser|page)/)[A-Za-z0-9._-]+",
    re.IGNORECASE,
)


def redact_cdp_url(value: str | None) -> str:
    """Mask the session token in a CDP WebSocket URL, leaving the shape legible.

    ``ws://127.0.0.1:9222/devtools/browser/abc123`` →
    ``ws://127.0.0.1:9222/devtools/browser/«redacted»``. Safe on any string;
    text without a CDP URL comes back unchanged.
    """
    if not value:
        return value or ""
    text = value if isinstance(value, str) else str(value)
    return _CDP_WS_URL_RE.sub(lambda m: f"{m.group(1)}«redacted»", text)
