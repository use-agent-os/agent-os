"""Denylist of host paths that should never be touched without explicit operator trust.

Certain host paths are classed as sensitive (SSH keys, cloud credentials,
system configuration) and must not fall under the ordinary "requires
approval" flow. Users clicking *approve* under pressure have been a reliable
source of incidents, so these paths are hard-blocked at the tool boundary and
only the explicit ``/elevated full`` operator mode can override them.

The list is a best-effort floor — add more entries as production surface
grows. It is not a substitute for OS-level permissions.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path, PurePosixPath

# Operator escape hatch — set AGENTOS_SENSITIVE_PATHS_DISABLED=1 to no-op
# the entire sensitive-path block layer. ONLY for trusted single-operator
# environments / E2E testing where sandbox=false + sensitive_path checks
# block valid agent commands like ``ls /etc/...``. Default off.
_DISABLED = os.environ.get(
    "AGENTOS_SENSITIVE_PATHS_DISABLED", ""
).lower() in ("1", "true", "yes", "on")


# Directory prefixes whose contents must not be read/written/deleted by the agent
# in default mode. Strings starting with ``~`` expand to the current user's
# home at check time.
_SENSITIVE_PREFIXES: tuple[str, ...] = (
    "~/.ssh",
    "~/.aws",
    "~/.azure",
    "~/.config/gcloud",
    "~/.docker/config",
    "~/.kube",
    "~/.npmrc",
    "~/.pypirc",
    "~/.netrc",
    "~/.gnupg",
    "~/.password-store",
    "/etc",
    "/boot",
    "/sys",
    "/proc",
    "/dev",
    "/root",
    "/var/log",
    "/lib/systemd",
    "/usr/lib/systemd",
)

# Exact filename tails we never want mutated, regardless of parent directory.
# Covers cases like moving an id_rsa out of ~/.ssh into /tmp.
_SENSITIVE_SUFFIXES: tuple[str, ...] = (
    "/id_rsa",
    "/id_ed25519",
    "/id_ecdsa",
    "/id_dsa",
    "/known_hosts",
    "/authorized_keys",
    "/.env",
    "/.env.local",
    "/.env.development",
    "/.env.production",
    "/.env.test",
    "/.bash_history",
    "/.zsh_history",
    "/.mysql_history",
    "/.psql_history",
)

_WORKSPACE_PARENT_EXCEPTION_MARKERS: tuple[str, ...] = ("/root",)

# Marker reported when a destructive intent targets the filesystem root. Root
# is not in ``_SENSITIVE_PREFIXES`` on purpose: reading or listing ``/`` is
# harmless, so only the delete-intent scan treats it as sensitive.
_ROOT_TARGET_MARKER = "/"

# Path segments that keep a target at the filesystem root: ``/``, ``/.``,
# ``/..`` and ``//`` all resolve to root.
_ROOT_TARGET_SEGMENTS = frozenset({"", ".", "..", "*"})

# A segment made only of glob metacharacters and dots expands across every
# entry of its parent, so at depth 1 it is a root wipe by another spelling:
# ``/*``, ``/**``, ``/?*``, ``/.*`` and ``/[a-z]*`` all sweep the top level.
# Bracket expressions are dropped first so ``/[a-z]*`` counts while a segment
# carrying literal text (``/tmp*``) does not.
_BRACKET_EXPRESSION_RE = re.compile(r"\[[^]]*\]")
_GLOB_ONLY_CHARS = frozenset("*?.")

# Windows runners resolve ``/`` to a drive root (``C:\``), so the drive letter
# is stripped before the segment check.
_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")

_TOKEN_EDGE_CHARS = " \t\r\n'\"`$(){}[]<>;,|&"
_ABSOLUTE_OR_TILDE_PATH_RE = re.compile(r"(?:~)?/(?:[^\s'\"`$(){}\[\]<>;,|&]+)")
_DOTENV_LITERAL_RE = re.compile(
    r"(?i)(?:^|[\s'\"`$(){}\[\]<>;,|&])"
    r"(?P<path>(?:[^\s'\"`$(){}\[\]<>;,|&]*/)?\.env(?:\.[A-Za-z0-9_.-]+)?)"
    r"(?=$|[\s'\"`$(){}\[\]<>;,|&])"
)


def _expand(path: str) -> str:
    """Expand ``~`` and resolve to absolute without requiring existence."""
    try:
        return str(Path(path).expanduser().resolve(strict=False))
    except (OSError, RuntimeError):
        return path


def _comparison_path(path: str) -> str:
    normalized = _expand(path).replace("\\", "/")
    return normalized.casefold() if os.name == "nt" else normalized


def _comparison_path_candidates(path: str) -> list[str]:
    candidates = [_comparison_path(path)]
    raw = str(path).strip().replace("\\", "/")
    if raw:
        candidates.append(raw.casefold() if os.name == "nt" else raw)
    if raw.startswith("~/"):
        expanded_home = str(Path.home()).replace("\\", "/") + raw[1:]
        candidates.append(expanded_home.casefold() if os.name == "nt" else expanded_home)
    return list(dict.fromkeys(candidates))


_ENV_TOKEN_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _shell_style_expand(text: str) -> str:
    """Expand ``$VAR``/``${VAR}`` tokens against ``os.environ``.

    ``os.path.expandvars`` only understands ``%VAR%`` on Windows, which makes
    denylist matching miss ``$HOME/...`` literals written for POSIX shells.
    Expanding manually keeps the scanner platform-independent.
    """

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        return os.environ.get(name, match.group(0))

    return _ENV_TOKEN_RE.sub(_replace, text)


def _looks_like_envvar_reference(text: str) -> bool:
    return "$" in text or "`" in text


def _looks_like_rooted_path_text(path: str) -> bool:
    normalized = str(path).strip().replace("\\", "/")
    return normalized.startswith(("/", "~/")) and not normalized.startswith("//")


def _path_name(path: str) -> str:
    normalized = str(path).strip().replace("\\", "/").rstrip("/")
    return PurePosixPath(normalized).name.lower()


def _path_contains(path: str, root: str) -> bool:
    if not path or not root:
        return False
    normalized_path = path.rstrip("/")
    normalized_root = root.rstrip("/")
    return normalized_path == normalized_root or normalized_path.startswith(
        normalized_root + "/"
    )


def _segment_sweeps_its_parent(segment: str) -> bool:
    """Return whether *segment* is a glob matching every entry beside it.

    ``*``, ``**``, ``?*``, ``.*`` and ``[a-z]*`` all do; ``tmp*`` does not,
    because the literal text narrows it to a named subset.
    """
    if "*" not in segment and "?" not in segment:
        return False
    literal = _BRACKET_EXPRESSION_RE.sub("", segment)
    return all(char in _GLOB_ONLY_CHARS for char in literal)


def _is_root_target(path: str) -> bool:
    """Return whether *path* names — or sweeps — the filesystem root.

    ``rm -rf /`` and its spellings carry no sensitive prefix, so the prefix
    scan never matched them. A target is root when every segment either
    resolves in place (empty, ``.``, ``..``) or globs across the whole level,
    covering ``/``, ``//``, ``/.``, ``/..``, ``/*``, ``/**``, ``/.*`` and
    ``/*/*``.
    """
    normalized = str(path).strip().replace("\\", "/")
    normalized = _DRIVE_PREFIX_RE.sub("", normalized, count=1)
    if not normalized.startswith("/"):
        return False
    return all(
        segment in _ROOT_TARGET_SEGMENTS or _segment_sweeps_its_parent(segment)
        for segment in normalized.split("/")
    )


def is_sensitive_path(path: str) -> str | None:
    """Return the matched sensitive marker, or None.

    Accepts any absolute or tilde-prefixed path. Relative paths are returned
    as-is without match — callers should resolve beforehand if needed.

    Honors :data:`_DISABLED` (env var ``AGENTOS_SENSITIVE_PATHS_DISABLED``).
    """
    if _DISABLED:
        return None
    if not path:
        return None
    candidates = _comparison_path_candidates(path)
    for expanded in candidates:
        if (
            expanded == "/root/.ssh"
            or expanded.startswith("/root/.ssh/")
            or expanded.endswith("/root/.ssh")
            or "/root/.ssh/" in expanded
        ):
            return "~/.ssh"
    for prefix in _SENSITIVE_PREFIXES:
        for expanded in candidates:
            for normalized in _comparison_path_candidates(prefix):
                if expanded == normalized or expanded.startswith(normalized + "/"):
                    return prefix
    for suffix in _SENSITIVE_SUFFIXES:
        normalized_suffix = suffix.replace("\\", "/")
        if os.name == "nt":
            normalized_suffix = normalized_suffix.casefold()
        if any(expanded.endswith(normalized_suffix) for expanded in candidates):
            return suffix
    name = _path_name(path)
    if name == ".env" or name.startswith(".env."):
        return "/.env*"
    return None


def _workspace_contains(path: str, workspace: str | Path | None) -> bool:
    if workspace is None:
        return False
    try:
        candidate = Path(path).expanduser().resolve(strict=False)
        root = Path(workspace).expanduser().resolve(strict=False)
        candidate.relative_to(root)
        return True
    except (OSError, RuntimeError, ValueError):
        pass
    candidate_paths = _comparison_path_candidates(str(path))
    workspace_paths = _comparison_path_candidates(str(workspace))
    return any(
        _path_contains(candidate, root)
        for candidate in candidate_paths
        for root in workspace_paths
    )


def _workspace_nested_under_marker(workspace: str | Path | None, marker: str) -> bool:
    if workspace is None or marker not in _WORKSPACE_PARENT_EXCEPTION_MARKERS:
        return False
    try:
        root = Path(workspace).expanduser().resolve(strict=False)
        marker_root = Path(marker).expanduser().resolve(strict=False)
        if root == marker_root:
            return False
        root.relative_to(marker_root)
        return True
    except (OSError, RuntimeError, ValueError):
        pass
    for workspace_text in _comparison_path_candidates(str(workspace)):
        for marker_text in _comparison_path_candidates(marker):
            if workspace_text != marker_text and _path_contains(
                workspace_text, marker_text
            ):
                return True
    return False


def _sensitive_leaf_marker(path: str) -> str | None:
    candidates = _comparison_path_candidates(path)
    for suffix in _SENSITIVE_SUFFIXES:
        normalized_suffix = suffix.replace("\\", "/")
        if os.name == "nt":
            normalized_suffix = normalized_suffix.casefold()
        if any(expanded.endswith(normalized_suffix) for expanded in candidates):
            return suffix
    name = _path_name(path)
    if name == ".env" or name.startswith(".env."):
        return "/.env*"
    return None


def sensitive_path_marker(
    path: str,
    *,
    workspace: str | Path | None = None,
) -> str | None:
    """Return a sensitive marker, honoring the active workspace boundary.

    Container deployments commonly place AgentOS's default workspace under
    ``/root/.agentos/workspace``. The broad ``/root`` deny prefix should not
    make that configured workspace unusable, but credential-like leaf files
    such as ``.env`` and private-key names remain blocked.
    """

    text = str(path).strip()
    # Environment-variable references ($HOME/..., ${HOME}/...) defeat the
    # tilde check below because pathlib does not expand them. Redirect any
    # token containing a `$` (or backtick command substitution) to the full
    # prefix matcher after expansion so the denylist still applies.
    if "$" in text or "`" in text:
        expanded = _shell_style_expand(text)
        if expanded != text:
            marker = is_sensitive_path(expanded)
            if marker is not None:
                return marker
    raw = Path(text).expanduser()
    if (
        text
        and not text.startswith("~")
        and not raw.is_absolute()
        and not _looks_like_rooted_path_text(text)
    ):
        return _sensitive_leaf_marker(text)

    marker = is_sensitive_path(path)
    if marker is None:
        return None
    if _workspace_contains(path, workspace) and _workspace_nested_under_marker(
        workspace, marker
    ):
        leaf_marker = _sensitive_leaf_marker(path)
        return leaf_marker
    return marker


def sensitive_path_in_text(
    text: str,
    *,
    workspace: str | Path | None = None,
) -> str | None:
    """Return the first sensitive path marker appearing in free-form text.

    This is intentionally conservative glue for shell/Python-code scanners.
    Structured callers should still resolve concrete paths and call
    :func:`is_sensitive_path` directly.

    Honors :data:`_DISABLED` (env var ``AGENTOS_SENSITIVE_PATHS_DISABLED``).
    """
    if _DISABLED:
        return None
    if not text:
        return None

    # Environment-variable references ($HOME/..., ${HOME}/...) reach this
    # scanner as literal text while the shell would expand them at execution
    # time. Scan the env-expanded variant too so the denylist sees the same
    # paths the child process will actually touch.
    expanded_text = os.path.expandvars(text)
    scan_texts = [text] if expanded_text == text else [text, expanded_text]

    candidates: list[str] = []
    for scan in scan_texts:
        try:
            candidates.extend(shlex.split(scan))
        except ValueError:
            candidates.extend(scan.split())
        candidates.extend(scan.split())
    with_context: list[tuple[str, int]] = []
    for scan in scan_texts:
        with_context.extend(
            (match.group(0), match.start()) for match in _ABSOLUTE_OR_TILDE_PATH_RE.finditer(scan)
        )
        with_context.extend(
            (match.group("path"), match.start("path"))
            for match in _DOTENV_LITERAL_RE.finditer(scan)
        )

    for raw in candidates:
        if "://" in raw:
            continue
        candidate = raw.strip(_TOKEN_EDGE_CHARS)
        if not candidate:
            continue
        marker = sensitive_path_marker(candidate, workspace=workspace)
        if marker is not None:
            return marker

    for raw, start in with_context:
        candidate = raw.strip(_TOKEN_EDGE_CHARS)
        if not candidate or candidate.startswith("//") or "://" in candidate:
            continue
        if start >= 2 and text[max(0, start - 3) : start] == "://":
            continue
        marker = sensitive_path_marker(candidate, workspace=workspace)
        if marker is not None:
            return marker

    return None


def sensitive_target_in_command(
    command: str,
    *,
    workspace: str | Path | None = None,
    cwd: str | Path | None = None,
) -> str | None:
    """Return the first sensitive marker for any destructive target, or None.

    Multi-target commands (``rm /tmp/ok /etc/bad``) are each checked — the
    presence of a single sensitive path is enough to block the whole command.
    The filesystem root is sensitive here and only here: deleting ``/`` wipes
    the host, while reading or listing it is ordinary work.

    Honors :data:`_DISABLED` (env var ``AGENTOS_SENSITIVE_PATHS_DISABLED``).
    """
    if _DISABLED:
        return None
    from agentos.sandbox.intent_cache import _extract_intents

    effective_workspace = workspace
    if effective_workspace is None:
        effective_workspace = cwd if cwd is not None else Path.cwd()

    for _kind, target in _extract_intents(command, base_dir=effective_workspace):
        if _is_root_target(target):
            return _ROOT_TARGET_MARKER
        marker = sensitive_path_marker(target, workspace=effective_workspace)
        if marker is not None:
            return marker
    return None


def build_block_envelope(
    command: str,
    sensitive_marker: str,
    *,
    tool_name: str = "",
) -> dict[str, object]:
    """Shape of a hard-block result returned to the caller / model.

    The model-facing ``message`` is intentionally terse and tells the agent
    not to retry — ``retryable=False`` should be enough for a well-behaved
    model to stop paraphrasing the same dangerous intent.
    """
    return {
        "status": "blocked",
        "reason": "sensitive_path",
        "tool": tool_name or None,
        "command": command,
        "sensitive_path": sensitive_marker,
        "message": (
            f"Refusing to operate on sensitive host path: {sensitive_marker}. "
            "This is a hard-block regardless of user approval. If this is "
            "truly intended, the operator must set /elevated full and retry."
        ),
        "retryable": False,
    }
