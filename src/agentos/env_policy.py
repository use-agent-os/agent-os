"""Write-side policy for environment variables.

:mod:`agentos.env` reads ``.env`` files. This module governs what may be
*written* back through an AgentOS surface — the Web UI, ``agentos env``, the
gateway RPC, or an agent tool.

Writing an environment variable is not a neutral convenience. Subprocesses
AgentOS spawns inherit most of ``os.environ`` (``tools/env_passthrough.py``
decides what is withheld), and several AgentOS behaviours — sandbox guards, the
shell allowlist, the gateway token — are themselves read from the environment.
A surface that can write any name is therefore a surface that can disable the
sandbox or execute arbitrary code on the next tool call.

The gate here is deliberately narrow and name-by-name:

* It applies on **write only**. Values an operator exported in their shell or
  hand-edited into ``~/.agentos/.env`` keep working exactly as before. The
  point is that the *writable* surface cannot escalate, not that the values
  become unusable.
* It does **not** blanket-block the ``AGENTOS_`` prefix. ``AGENTOS_LLM_API_KEY``
  is an ordinary credential that setup flows must be able to store. Only the
  specific names that steer runtime posture or state location are listed.
"""

from __future__ import annotations

import re

#: POSIX-portable environment variable name. Rejects the digit-leading and
#: dash-containing names that a shell cannot export without quoting tricks.
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Names whose *suffix* implies the value is a credential. Used to decide
# whether a value is masked by default in listings and rendered as a password
# field in the UI. Callers with real metadata (a catalog entry, a skill
# manifest) should prefer that over this heuristic.
_SECRET_SUFFIXES: tuple[str, ...] = (
    "_KEY",
    "_TOKEN",
    "_SECRET",
    "_PASSWORD",
    "_PASSWD",
    "_CREDENTIALS",
    "_JSON",
)

# Substrings that mark a credential regardless of position, for names that do
# not end in one of the suffixes above (``SECRET_ACCESS_KEY_ID``, ``PASSWORDS``).
_SECRET_SUBSTRINGS: tuple[str, ...] = ("SECRET", "PASSWORD", "TOKEN", "APIKEY")

# ── Denylist ────────────────────────────────────────────────────────────────
#
# Group 1 — dynamic loader / linker. Planting a path here means the next
# subprocess AgentOS spawns loads attacker-controlled code before main().
_LOADER_NAMES = (
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "LD_AUDIT",
    "LD_DEBUG",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
    "DYLD_FRAMEWORK_PATH",
    "DYLD_FALLBACK_LIBRARY_PATH",
    "DYLD_FALLBACK_FRAMEWORK_PATH",
)

# Group 2 — interpreter initialisation. AgentOS itself restarts through one of
# these, and skill scripts run under them.
_INTERPRETER_NAMES = (
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
    "PYTHONEXECUTABLE",
    "PYTHONNOUSERSITE",
    "NODE_OPTIONS",
    "NODE_PATH",
)

# Group 3 — what the shell resolves or invokes implicitly. ``PATH`` is too
# broad to ever allow: rewriting it redirects every binary the shell tool and
# every skill install spec resolves. ``EDITOR``/``VISUAL``/``PAGER``/``BROWSER``
# are commands other programs launch on the operator's behalf.
_SHELL_NAMES = (
    "PATH",
    "SHELL",
    "IFS",
    "ENV",
    "BASH_ENV",
    "EDITOR",
    "VISUAL",
    "PAGER",
    "BROWSER",
    "GIT_SSH_COMMAND",
    "GIT_EXEC_PATH",
    "GIT_SHELL",
)

# Group 4 — AgentOS runtime posture and state location. Each name below is
# read at a call site that decides how much the agent is allowed to do, or
# where AgentOS keeps its state:
#
#   AGENTOS_SENSITIVE_PATHS_DISABLED  sandbox/sensitive_paths.py
#   AGENTOS_SENSITIVE_PAYLOAD_DISABLED  redact.py
#   AGENTOS_REDACT_SECRETS            redact.py
#   AGENTOS_STRIP_PROVIDER_ENV        tools/env_passthrough.py
#   AGENTOS_SHELL_DENYLIST            tools/builtin/shell_policy.py
#   AGENTOS_SAFE_BIN_ALLOW/DENY/WARN  tools/builtin/shell_policy.py
#   AGENTOS_AGENT_PERMISSIONS         cli/agent_cmd.py
#   AGENTOS_TRUST_ENV                 env.py (honour ambient *_PROXY at all)
#   AGENTOS_GATEWAY_TOKEN             cli/gateway_rpc.py
#   AGENTOS_GATEWAY_CONFIG_PATH       gateway config resolution
#   AGENTOS_STATE_DIR / _ROOT / …     paths.py and friends
#
# Bind posture (host/port/listen) is CLI-only by design — ``rpc_config.py``
# already refuses to persist it — so it is listed here for the same reason.
_AGENTOS_POSTURE_NAMES = (
    "AGENTOS_SENSITIVE_PATHS_DISABLED",
    "AGENTOS_SENSITIVE_PAYLOAD_DISABLED",
    "AGENTOS_REDACT_SECRETS",
    "AGENTOS_STRIP_PROVIDER_ENV",
    "AGENTOS_SHELL_DENYLIST",
    "AGENTOS_SAFE_BIN_ALLOW",
    "AGENTOS_SAFE_BIN_DENY",
    "AGENTOS_SAFE_BIN_WARN",
    "AGENTOS_AGENT_PERMISSIONS",
    "AGENTOS_TRUST_ENV",
    "AGENTOS_GATEWAY_TOKEN",
    "AGENTOS_GATEWAY_CONFIG_PATH",
    "AGENTOS_STATE_DIR",
    "AGENTOS_LOG_DIR",
    "AGENTOS_ROOT",
    "AGENTOS_MIGRATIONS_DIR",
    "AGENTOS_MEMORY_DB",
    "AGENTOS_MEMORY_DIR",
    "AGENTOS_GATEWAY_HOST",
    "AGENTOS_GATEWAY_PORT",
    "AGENTOS_LISTEN",
    "AGENTOS_HTTP_DOWNLOAD_LIMIT",
)

#: Every name the gateway reads a control-plane credential from: the gateway
#: token in each spelling ``GatewayConfig`` accepts (``AuthConfig``'s own
#: ``AGENTOS_AUTH_`` prefix and the nested ``AGENTOS_GATEWAY_AUTH__`` one), the
#: reserved password, the webhook ingress token, the CLI's client token and the
#: desktop's operator secret. One list, so the child-environment filter
#: (``tools/env_passthrough.py``), the lifecycle-hook filter and the boot-time
#: scrub (``gateway/credential_env.py``) cannot drift apart again: the child
#: filter once listed only ``AGENTOS_GATEWAY_TOKEN`` while the gateway read its
#: token from ``AGENTOS_AUTH_TOKEN``, and every agent shell held the live token.
GATEWAY_CREDENTIAL_ENV: frozenset[str] = frozenset(
    {
        "AGENTOS_GATEWAY_TOKEN",
        "AGENTOS_AUTH_TOKEN",
        "AGENTOS_GATEWAY_AUTH__TOKEN",
        "AGENTOS_AUTH_PASSWORD",
        "AGENTOS_GATEWAY_AUTH__PASSWORD",
        "AGENTOS_GATEWAY_HOOKS__TOKEN",
        "AGENTOS_OPERATOR_SECRET",
        "AGENTOS_OPERATOR_SECRET_FILE",
    }
)

# Group 5 — egress steering. A proxy name is a posture name: whoever sets it
# chooses the machine every outbound request is handed to, credentials and all.
# ``AGENTOS_LLM_PROXY`` is read at ``gateway/llm_runtime.py``,
# ``provider/openai.py`` and ``provider/auxiliary.py`` and applied to every
# provider client, so a writable value puts an attacker in front of each
# ``Authorization: Bearer ...`` header. The standard ``*_PROXY`` names reach the
# same place through ``trust_env`` on any subprocess or library that honours
# them, and ``NO_PROXY`` belongs here too — carving a host out of the proxy is
# as much a routing decision as adding one.
#
# Case is not a boundary here. ``agentos.env`` pushes ``.env`` keys into
# ``os.environ`` verbatim, and the readers downstream lower-case whatever they
# find — ``urllib.request.getproxies_environment()``, which httpx goes through,
# treats ``Http_Proxy`` as ``http://`` just as readily as ``HTTP_PROXY``. So
# these names are matched case-insensitively (``_EGRESS_UPPER`` below) rather
# than by exact spelling; the two conventional spellings stay in
# ``WRITE_DENYLIST`` so a plain membership test still sees them.
_EGRESS_NAMES = (
    "AGENTOS_LLM_PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)

#: Egress names upper-cased, for the case-insensitive comparison. Any spelling
#: of these — ``Http_Proxy``, ``hTTp_PrOXy`` — is refused.
_EGRESS_UPPER: frozenset[str] = frozenset(name.upper() for name in _EGRESS_NAMES)

#: Names that may never be written through an AgentOS surface.
WRITE_DENYLIST: frozenset[str] = (
    frozenset(
        _LOADER_NAMES + _INTERPRETER_NAMES + _SHELL_NAMES + _AGENTOS_POSTURE_NAMES + _EGRESS_NAMES
    )
    | GATEWAY_CREDENTIAL_ENV
)

_DENY_MESSAGE = (
    "Environment variable {key!r} cannot be written through AgentOS. Names that "
    "steer subprocess execution (PATH, LD_PRELOAD, PYTHONPATH, EDITOR, ...) or "
    "AgentOS runtime posture (AGENTOS_AGENT_PERMISSIONS, AGENTOS_GATEWAY_TOKEN, "
    "...) or outbound routing (HTTP_PROXY, AGENTOS_LLM_PROXY, ...) are refused "
    "so this surface cannot escalate its own privileges. Edit "
    "~/.agentos/.env directly if you genuinely need to set it."
)


class EnvPolicyError(ValueError):
    """Raised when a name or value is not writable through an AgentOS surface."""


def assert_valid_name(key: str) -> None:
    """Raise :class:`EnvPolicyError` unless *key* is a portable env var name."""
    if not isinstance(key, str) or not ENV_NAME_RE.match(key):
        raise EnvPolicyError(
            f"Invalid environment variable name: {key!r}. Names must match "
            f"{ENV_NAME_RE.pattern} (letters, digits, underscore; no leading digit)."
        )


def _is_denied(key: str) -> bool:
    """Return whether *key* is on the denylist under either matching rule."""
    return key in WRITE_DENYLIST or key.upper() in _EGRESS_UPPER


def is_writable(key: str) -> bool:
    """Return whether *key* may be written through an AgentOS surface."""
    return bool(ENV_NAME_RE.match(key)) and not _is_denied(key)


def assert_writable(key: str) -> None:
    """Raise :class:`EnvPolicyError` unless *key* passes the name and deny gates."""
    assert_valid_name(key)
    if _is_denied(key):
        raise EnvPolicyError(_DENY_MESSAGE.format(key=key))


#: Every character the ``.env`` reader breaks a line on that the control-character
#: check below would not already refuse. ``agentos.env._parse_env_file`` splits with
#: ``str.splitlines()``, which is wider than ``\n``/``\r``: it also breaks on U+0085
#: NEXT LINE, U+2028 LINE SEPARATOR and U+2029 PARAGRAPH SEPARATOR. Those three are
#: invisible and non-C0, so they arrive unnoticed in text pasted out of a PDF, a Word
#: document or a web page. The remaining ``splitlines()`` characters — U+000B, U+000C
#: and U+001C-U+001E — are C0 and are refused one gate below, with their own message.
_LINE_BREAK_CHARS: tuple[str, ...] = ("\n", "\r", "\u0085", "\u2028", "\u2029")


def sanitize_value(key: str, value: str) -> str:
    """Return *value* validated for storage, or raise :class:`EnvPolicyError`.

    Line breaks are **rejected rather than stripped**, and a line break is
    whatever the reader splits on — see :data:`_LINE_BREAK_CHARS`, which is
    why the invisible separators count as well. A ``.env`` entry is one line,
    so a value containing one can only be stored by escaping it —
    and adding escape semantics to the reader would change how existing
    hand-written files parse (a Windows path like ``C:\\new`` would suddenly
    grow a line break). Silently truncating instead, as some writers do, turns
    a pasted multi-line credential into a mysterious 401 hours later.

    Refusing is the honest option: the caller sees the problem immediately and
    can point the variable at a file instead (``VERTEX_CREDENTIALS_PATH`` style)
    or store the value base64-encoded.
    """
    if not isinstance(value, str):
        raise EnvPolicyError(f"Value for {key!r} must be a string, got {type(value).__name__}")
    if any(char in value for char in _LINE_BREAK_CHARS):
        raise EnvPolicyError(
            f"Value for {key!r} contains a line break. A .env entry is a single "
            "line — store the value base64-encoded, or point the variable at a "
            "file path instead."
        )
    for ch in value:
        if ch == "\t":
            continue
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise EnvPolicyError(
                f"Value for {key!r} contains a control character "
                f"(U+{ord(ch):04X}) that cannot be stored in a .env file."
            )
    return value


def is_secret_name(key: str) -> bool:
    """Return whether *key* looks like it holds a credential.

    A heuristic for defaults only — masking in listings, password inputs in the
    UI. Explicit metadata (catalog entry, skill manifest ``secret:``) wins where
    it exists.
    """
    upper = key.upper()
    if upper.endswith(_SECRET_SUFFIXES):
        return True
    return any(part in upper for part in _SECRET_SUBSTRINGS)


def mask(value: str | None) -> str | None:
    """Return a display-safe rendering of *value*.

    ``None`` in, ``None`` out — an unset variable has nothing to mask. Short
    values are replaced wholesale rather than partially revealed, because
    showing four of six characters is not masking.
    """
    if value is None:
        return None
    if len(value) <= 12:
        return "•" * 8
    return f"{value[:4]}…{value[-4:]}"
