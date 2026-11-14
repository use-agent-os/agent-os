"""Permission bridge: harness ``session/request_permission`` -> AgentOS policy.

The runtime calls :meth:`ApprovalQueuePermissionBridge.decide` for every
permission prompt a harness raises and answers it with the returned
:data:`~agentos.acp.types.PermissionDecision`. Static modes answer without a
human; ``ask`` puts the prompt on the operator approval queue (namespace
``"acp"``) and waits for the Web UI / desktop / RPC to resolve it.

Semantics follow OpenClaw's ``extensions/acpx/src/harness-attempt.ts``
(MIT, see ``THIRD_PARTY_NOTICES.md``): never widen the operator's grant --
``allow_always`` is only ever returned under ``approve-all`` and only when the
harness offers no ``allow_once`` -- and fail closed on timeout or error.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog

from agentos.acp.policy import AcpConfig, is_unattended_ctx
from agentos.acp.registry import get_harness, normalize_agent_id
from agentos.acp.types import (
    PermissionDecision,
    PermissionOptionInfo,
    PermissionPromptUnavailableError,
    PermissionRequest,
)
from agentos.util.bounded_registry import BoundedRegistry

if TYPE_CHECKING:
    from agentos.application.approval_queue import ApprovalQueue

log = structlog.get_logger(__name__)

ACP_APPROVAL_NAMESPACE = "acp"

#: Tool kinds ``approve-reads`` answers without asking (ACP ``ToolKind``).
READ_TOOL_KINDS = frozenset({"read", "search", "fetch", "think"})

#: Cap on the serialized ``raw_input`` shown to the operator.
RAW_INPUT_MAX_BYTES = 4096

_VERBS: dict[str, str] = {
    "read": "read",
    "edit": "edit",
    "delete": "delete",
    "move": "move",
    "search": "search",
    "execute": "run",
    "think": "think",
    "fetch": "fetch",
    "switch_mode": "switch mode",
}

_SECRET_KEY = re.compile(
    r"(pass(word|wd)?|secret|token|api[_-]?key|authorization|auth|cookie|credential|private[_-]?key)",
    re.IGNORECASE,
)

#: ``interaction_mode`` resolver: given a session key, return
#: ``"interactive"``, ``"unattended"`` or ``None`` (unknown; fall through to
#: the next source).
SurfaceResolver = Callable[[str], str | None]


def agent_display_name(agent_id: str) -> str:
    harness = get_harness(agent_id)
    return harness.name if harness is not None else (agent_id or "Harness")


def acp_approval_title(agent_id: str, tool_kind: str | None, title: str) -> str:
    """``"<agent> wants to <kind>: <title>"`` -- the line the operator sees."""
    verb = _VERBS.get(str(tool_kind or "").lower(), "use a tool")
    detail = " ".join(str(title or "").split())
    if len(detail) > 200:
        detail = detail[:199] + "…"
    head = f"{agent_display_name(agent_id)} wants to {verb}"
    return f"{head}: {detail}" if detail else head


def _redact_value(value: Any, key: str | None = None) -> Any:
    from agentos.redact import redact_sensitive_text

    if key is not None and _SECRET_KEY.search(key) and isinstance(value, (str, int, float)):
        return "«redacted»"
    if isinstance(value, str):
        return redact_sensitive_text(value, force=True)
    if isinstance(value, dict):
        return {str(k): _redact_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(v) for v in value]
    return value


def redact_raw_input(raw_input: Any, *, max_bytes: int = RAW_INPUT_MAX_BYTES) -> Any:
    """Mask credentials in a harness tool input and cap its serialized size.

    Over-limit input becomes ``{"_truncated": True, "preview": <first bytes>}``
    so the queue row (and the Web UI) never carries an unbounded blob.
    """
    if raw_input is None:
        return None
    redacted = _redact_value(raw_input)
    try:
        text = json.dumps(redacted, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = str(redacted)
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return redacted
    preview = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return {"_truncated": True, "bytes": len(encoded), "preview": preview}


def command_of(raw_input: Any) -> str:
    """The shell command in a harness ``execute`` input, as one string."""
    if not isinstance(raw_input, dict):
        return ""
    for key in ("command", "cmd", "argv", "commandLine"):
        value = raw_input.get(key)
        if isinstance(value, str) and value.strip():
            extra = raw_input.get("args")
            if isinstance(extra, list) and extra:
                return " ".join([value.strip(), *(str(a) for a in extra)])
            return value.strip()
        if isinstance(value, list) and value:
            return " ".join(str(part) for part in value)
    return ""


_COMMAND_KEYS = ("command", "cmd", "argv", "commandLine")

#: Characters that let a shell chain, pipe, redirect, background, substitute,
#: expand or continue onto another line. A command containing any of them --
#: quoted or not -- is never remembered and never matches a remembered grant.
_UNSAFE_COMMAND_CHARS = frozenset(";&|<>$`(){}\\\n\r") | (
    frozenset("%^!") if os.name == "nt" else frozenset()
)

#: Programs whose job is to run another command (shells, privilege and exec
#: wrappers, state-changing shell builtins). "Always" on one of them would
#: cover any command at all, so they are never remembered.
_COMMAND_RUNNERS = frozenset(
    {
        # shells
        "sh", "bash", "zsh", "dash", "ksh", "mksh", "ash", "fish", "csh", "tcsh",
        "busybox", "cmd", "powershell", "pwsh",
        # builtins that run, source or re-route commands
        ".", "source", "eval", "exec", "command", "builtin", "time", "coproc",
        "alias", "hash", "enable", "trap", "export", "declare", "typeset",
        "readonly", "local", "set", "unset", "shopt", "cd", "pushd", "popd",
        "umask", "ulimit",
        # exec / privilege wrappers
        "env", "sudo", "doas", "su", "runuser", "pkexec", "xargs", "nohup",
        "nice", "ionice", "timeout", "stdbuf", "setsid", "chroot", "unshare",
        "nsenter", "flock", "script", "watch", "strace", "ltrace", "taskset",
        "chrt", "caffeinate", "parallel",
    }
)  # fmt: skip

#: ``<shell> -c '<script>'`` argv that is unwrapped to its one inner command
#: (Codex sends ``["bash", "-lc", "git status"]``).
_WRAPPING_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_SHELL_SCRIPT_FLAGS = frozenset({"-c", "-lc", "-cl"})

_ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_SUBCOMMAND = re.compile(r"[A-Za-z][\w.:-]*")


def _is_safe_command_text(text: str) -> bool:
    return not any(
        ch in _UNSAFE_COMMAND_CHARS or (ch != "\t" and (ord(ch) < 0x20 or ord(ch) == 0x7F))
        for ch in text
    )


def _split_simple_command(text: str) -> list[str] | None:
    """argv of one simple shell command, or ``None`` (fail closed).

    ``None`` for anything a shell could turn into more than one program run:
    control operators, pipes, redirections, substitutions, expansions, line
    continuations, unbalanced quotes.
    """
    text = text.strip()
    if not text or not _is_safe_command_text(text):
        return None
    try:
        tokens = shlex.split(text, posix=True)
    except ValueError:
        return None
    return tokens or None


def _program_name(path: str) -> str:
    name = os.path.basename(path).lower()
    if os.name == "nt":
        stem, ext = os.path.splitext(name)
        if ext in {e.lower() for e in os.environ.get("PATHEXT", ".EXE").split(os.pathsep) if e}:
            return stem
    return name


def _is_executable_file(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _resolve_program(token: str) -> str | None:
    """Absolute path of the program a shell would run for ``token``.

    Absolute paths are taken as given; bare names are looked up on the
    absolute ``PATH`` entries only (an empty or ``.`` entry would search the
    current directory). Relative paths and unresolvable names give ``None``.
    """
    if not token or any(ch.isspace() or ch in "*?[]~" for ch in token):
        return None
    has_sep = "/" in token or os.sep in token or bool(os.altsep and os.altsep in token)
    if has_sep:
        if not os.path.isabs(token):
            return None
        candidate = os.path.normpath(token)
        return candidate if _is_executable_file(candidate) else None
    names = [token]
    if os.name == "nt":
        names += [token + ext for ext in os.environ.get("PATHEXT", ".EXE").split(os.pathsep) if ext]
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry or not os.path.isabs(entry):
            continue
        for name in names:
            candidate = os.path.normpath(os.path.join(entry, name))
            if _is_executable_file(candidate):
                return candidate
    return None


def _argv_prefix(argv: list[str], *, unwrap_shell: bool = True) -> str | None:
    """``"<resolved program> [<subcommand>]"`` for one argv, or ``None``."""
    if not argv or not all(isinstance(a, str) and _is_safe_command_text(a) for a in argv):
        return None
    if _ENV_ASSIGNMENT.match(argv[0]):
        return None
    program = _resolve_program(argv[0])
    if program is None:
        return None
    name = _program_name(program)
    if (
        unwrap_shell
        and name in _WRAPPING_SHELLS
        and len(argv) == 3
        and argv[1] in _SHELL_SCRIPT_FLAGS
    ):
        inner = _split_simple_command(argv[2])
        inner_prefix = _argv_prefix(inner, unwrap_shell=False) if inner else None
        return f"{program} {argv[1]} {inner_prefix}" if inner_prefix else None
    if name in _COMMAND_RUNNERS:
        return None
    if len(argv) > 1 and _SUBCOMMAND.fullmatch(argv[1]):
        return f"{program} {argv[1]}"
    return program


def command_prefix(command: str) -> str | None:
    """Resolved program plus subcommand, the unit an "always" covers.

    ``git push --force`` remembers ``/usr/bin/git push``; ``rm -rf /``
    remembers ``/usr/bin/rm``. The second token is kept only when it looks
    like a subcommand (no flag, no path).

    ``None`` -- never remember, never match -- unless ``command`` is one
    simple command whose program resolves to an absolute path: no control
    operator, pipe, redirection, substitution, newline or ``VAR=`` prefix, no
    relative program path, no shell or exec wrapper, and it parses.
    """
    argv = _split_simple_command(command)
    return _argv_prefix(argv) if argv else None


def _execute_argv(raw_input: Any, title: str) -> list[str] | None:
    """argv of the command a harness ``execute`` prompt will run, or ``None``.

    Read from ``raw_input`` (what the harness executes); the display title is
    used only when the harness sent no input at all. Ambiguous input (two
    command keys, non-string parts) gives ``None``.
    """
    if raw_input is None or raw_input == {}:
        return _split_simple_command(title or "")
    if not isinstance(raw_input, dict):
        return None
    present = [raw_input[k] for k in _COMMAND_KEYS if k in raw_input]
    if len(present) != 1:
        return None
    value = present[0]
    if isinstance(value, str):
        argv = _split_simple_command(value)
    elif isinstance(value, list) and value and all(isinstance(p, str) for p in value):
        argv = list(value)
    else:
        return None
    if argv is None:
        return None
    extra = raw_input.get("args")
    if extra is not None and extra != []:
        if not isinstance(extra, list) or not all(isinstance(a, str) for a in extra):
            return None
        argv += extra
    return argv


def _has_command(raw_input: Any) -> bool:
    return isinstance(raw_input, dict) and any(k in raw_input for k in _COMMAND_KEYS)


def remember_key(request: PermissionRequest) -> tuple[str, str] | None:
    """``(tool_kind, prefix)`` for the per-session "always" memory.

    ``execute`` prompts (and ``other`` prompts that carry a shell command) are
    remembered per :func:`command_prefix`; any other kind is remembered for
    the whole kind (the harness's own "allow edits this session"
    granularity). ``None`` means the prompt can be neither remembered nor
    answered from memory.
    """
    kind = str(request.tool_kind or "other").lower()
    if kind == "execute" or (kind == "other" and _has_command(request.raw_input)):
        argv = _execute_argv(request.raw_input, request.title)
        prefix = _argv_prefix(argv) if argv else None
        return (kind, prefix) if prefix else None
    return kind, ""


def _pick(options: list[PermissionOptionInfo], *kinds: str) -> PermissionDecision | None:
    offered = {o.kind for o in options}
    if not options:
        # The harness listed no options; let the runtime map the bare kind.
        return kinds[0]  # type: ignore[return-value]
    for kind in kinds:
        if kind in offered:
            return kind  # type: ignore[return-value]
    return None


def allow_decision(
    options: list[PermissionOptionInfo], *, allow_always_fallback: bool = False
) -> PermissionDecision:
    """``allow_once``; ``allow_always`` only as an explicit fallback; else reject.

    OpenClaw rule: never widen a once-grant into an always-grant. Without a
    usable allow option the prompt is rejected rather than over-granted.
    """
    kinds = ("allow_once", "allow_always") if allow_always_fallback else ("allow_once",)
    picked = _pick(options, *kinds)
    return picked if picked is not None else reject_decision(options)


def reject_decision(options: list[PermissionOptionInfo]) -> PermissionDecision:
    picked = _pick(options, "reject_once", "reject_always")
    return picked if picked is not None else "cancel"


class ApprovalQueuePermissionBridge:
    """:class:`~agentos.acp.types.PermissionBridge` over ``ApprovalQueue``.

    ``permission_mode`` (default: ``config.permission_mode``) is the mode for
    every session this bridge serves; :meth:`bind_session` overrides it per
    session (the spawn's effective mode) and records whether an operator can
    answer prompts for that session. The runtime holds one bridge and calls
    :meth:`decide` from its ACP client callback.
    """

    def __init__(
        self,
        config: AcpConfig,
        *,
        queue: ApprovalQueue | Callable[[], ApprovalQueue] | None = None,
        permission_mode: str | None = None,
        surface: SurfaceResolver | None = None,
    ) -> None:
        self._config = config
        self._queue_source = queue
        self._mode = permission_mode
        self._surface = surface
        self._session_modes: BoundedRegistry[str, str] = BoundedRegistry(
            name="ApprovalQueuePermissionBridge._session_modes",
            session_of=lambda key, _value: key,
        )
        self._session_surfaces: BoundedRegistry[str, str] = BoundedRegistry(
            name="ApprovalQueuePermissionBridge._session_surfaces",
            session_of=lambda key, _value: key,
        )
        self._remembered: BoundedRegistry[str, set[tuple[str, str]]] = BoundedRegistry(
            name="ApprovalQueuePermissionBridge._remembered",
            session_of=lambda key, _value: key,
        )

    # ------------------------------------------------------------------
    # session bookkeeping
    # ------------------------------------------------------------------

    def bind_session(
        self,
        session_key: str,
        *,
        permission_mode: str | None = None,
        interactive: bool | None = None,
        requester_ctx: Any = None,
    ) -> None:
        """Record a session's effective mode and whether a human can answer.

        Pass the spawning turn's ``ToolContext`` as ``requester_ctx``: an
        ``UNATTENDED`` interaction mode (cron, channel automation) marks the
        session as having no operator, exactly as ``tools/builtin/shell.py``
        decides it for exec approvals. ``interactive`` overrides that.
        """
        if permission_mode:
            self._session_modes[session_key] = permission_mode
        if interactive is None and requester_ctx is not None:
            interactive = not is_unattended_ctx(requester_ctx)
        if interactive is not None:
            self._session_surfaces[session_key] = "interactive" if interactive else "unattended"

    def forget_session(self, session_key: str) -> None:
        """Drop everything held for a closed session (mode, surface, grants)."""
        self._session_modes.pop(session_key, None)
        self._session_surfaces.pop(session_key, None)
        self._remembered.pop(session_key, None)

    def remembered(self, session_key: str) -> set[tuple[str, str]]:
        return set(self._remembered.get(session_key) or ())

    def remember(self, request: PermissionRequest) -> None:
        key = remember_key(request)
        if key is None:
            log.info(
                "acp.permission.not_remembered",
                session_key=request.session_key,
                agent_id=request.agent_id,
                tool_kind=request.tool_kind,
                tool_call_id=request.tool_call_id,
            )
            return
        grants = set(self._remembered.get(request.session_key) or ())
        grants.add(key)
        self._remembered[request.session_key] = grants

    def mode_for(self, session_key: str) -> str:
        return self._session_modes.get(session_key) or self._mode or self._config.permission_mode

    def _queue(self) -> ApprovalQueue:
        source = self._queue_source
        if source is None:
            from agentos.application.approval_queue import get_approval_queue

            return get_approval_queue()
        if callable(source) and not hasattr(source, "request"):
            return source()
        return source  # type: ignore[return-value]

    def is_unattended(self, session_key: str) -> bool:
        """Whether no operator can answer prompts for ``session_key``.

        Order: the surface recorded by :meth:`bind_session`, then the injected
        resolver. Unknown means interactive: the gateway always has the Web UI.
        """
        bound = self._session_surfaces.get(session_key)
        if bound is not None:
            return bound == "unattended"
        if self._surface is not None:
            value = self._surface(session_key)
            if value:
                return str(value).lower() == "unattended"
        # The gateway always has the Web UI / desktop approval surface.
        return False

    # ------------------------------------------------------------------
    # PermissionBridge
    # ------------------------------------------------------------------

    async def decide(self, request: PermissionRequest) -> PermissionDecision:
        mode = self.mode_for(request.session_key)
        kind = str(request.tool_kind or "other").lower()
        options = list(request.options or [])
        if mode == "approve-all":
            return self._log(request, mode, allow_decision(options, allow_always_fallback=True))
        if mode == "deny-all":
            return self._log(request, mode, reject_decision(options))
        if mode == "approve-reads" and kind in READ_TOOL_KINDS:
            return self._log(request, mode, allow_decision(options))
        # ask (and approve-reads for anything that is not a read)
        key = remember_key(request)
        if key is not None and key in self.remembered(request.session_key):
            return self._log(request, mode, allow_decision(options), source="remembered")
        if self.is_unattended(request.session_key):
            return self._no_surface(request, mode, options, reason="unattended")
        return await self._ask_operator(request, mode, options)

    def _no_surface(
        self,
        request: PermissionRequest,
        mode: str,
        options: list[PermissionOptionInfo],
        *,
        reason: str,
    ) -> PermissionDecision:
        if self._config.non_interactive_permissions == "deny":
            return self._log(request, mode, reject_decision(options), source=f"no_surface:{reason}")
        log.warning(
            "acp.permission.unavailable",
            session_key=request.session_key,
            agent_id=request.agent_id,
            tool_kind=request.tool_kind,
            reason=reason,
        )
        raise PermissionPromptUnavailableError(
            f"{agent_display_name(request.agent_id)} asked for permission "
            f"({request.tool_kind or 'tool'}: {request.title}) but no operator can answer "
            f"({reason}). Set [acp].permission_mode or non_interactive_permissions = 'deny' "
            "for unattended runs.",
            session_key=request.session_key,
            agent_id=request.agent_id,
            tool_call_id=request.tool_call_id,
            reason=reason,
        )

    def build_params(self, request: PermissionRequest) -> dict[str, Any]:
        """Queue-row params: the brief's contract keys plus the display keys
        the existing Web UI renders (``toolName``, ``command``, ``warning``)."""
        raw_input = redact_raw_input(request.raw_input)
        command = command_of(raw_input) if str(request.tool_kind) == "execute" else ""
        title = acp_approval_title(request.agent_id, request.tool_kind, request.title)
        return {
            "session_key": request.session_key,
            "agent_id": normalize_agent_id(request.agent_id),
            "acp_session_id": request.acp_session_id,
            "tool_call_id": request.tool_call_id,
            "title": request.title,
            "tool_kind": request.tool_kind,
            "raw_input": raw_input,
            "cwd": request.cwd,
            "options": [
                {"option_id": o.option_id, "name": o.name, "kind": o.kind} for o in request.options
            ],
            # display aliases (Web UI / desktop approval modal)
            "displayTitle": title,
            "toolName": title,
            "sessionKey": request.session_key,
            "agent": agent_display_name(request.agent_id),
            "command": command,
            "warning": f"{title}\ncwd: {request.cwd}" if request.cwd else title,
        }

    async def _ask_operator(
        self,
        request: PermissionRequest,
        mode: str,
        options: list[PermissionOptionInfo],
    ) -> PermissionDecision:
        from agentos.application.approval_queue import ApprovalQueueOwnedByGatewayError

        queue = self._queue()
        timeout = float(self._config.approval_timeout_s)
        try:
            approval_id = queue.request(
                namespace=ACP_APPROVAL_NAMESPACE,
                params=self.build_params(request),
                ttl=timeout,
            )
        except ApprovalQueueOwnedByGatewayError:
            return self._no_surface(request, mode, options, reason="queue_owned_by_gateway")
        log.info(
            "acp.permission.requested",
            approval_id=approval_id,
            session_key=request.session_key,
            agent_id=request.agent_id,
            tool_kind=request.tool_kind,
        )
        try:
            approved = await queue.wait(approval_id, timeout=timeout)
        except asyncio.CancelledError:
            _deny_quietly(queue, approval_id)
            raise
        except Exception as exc:  # fail closed
            log.warning("acp.permission.wait_failed", approval_id=approval_id, error=str(exc))
            _deny_quietly(queue, approval_id)
            return self._log(request, mode, reject_decision(options), source="error")
        try:
            entry = queue.get(approval_id)
        except KeyError:
            entry = None
        if entry is not None and not entry.resolved:
            _deny_quietly(queue, approval_id)
            approved = False
        if not approved:
            source = "timeout" if entry is not None and not entry.resolved else "operator"
            return self._log(request, mode, reject_decision(options), source=source)
        if entry is not None and entry.params.get("rememberDecision"):
            self.remember(request)
        return self._log(request, mode, allow_decision(options), source="operator")

    @staticmethod
    def _log(
        request: PermissionRequest,
        mode: str,
        decision: PermissionDecision,
        *,
        source: str = "policy",
    ) -> PermissionDecision:
        log.info(
            "acp.permission.decided",
            session_key=request.session_key,
            agent_id=request.agent_id,
            tool_kind=request.tool_kind,
            tool_call_id=request.tool_call_id,
            mode=mode,
            decision=decision,
            source=source,
        )
        return decision


def _deny_quietly(queue: ApprovalQueue, approval_id: str) -> None:
    """Resolve a still-pending row as denied so no stale prompt lingers."""
    try:
        if not queue.get(approval_id).resolved:
            queue.resolve(approval_id, False, resolved_by="system")
    except (KeyError, ValueError):
        pass
    except Exception as exc:  # pragma: no cover - best effort
        log.debug("acp.permission.deny_failed", approval_id=approval_id, error=str(exc))


__all__ = [
    "ACP_APPROVAL_NAMESPACE",
    "READ_TOOL_KINDS",
    "ApprovalQueuePermissionBridge",
    "acp_approval_title",
    "agent_display_name",
    "allow_decision",
    "command_of",
    "command_prefix",
    "redact_raw_input",
    "reject_decision",
    "remember_key",
]
