"""Which connections belong to an agent.

Trading guardrails (the per-order approval threshold, the daily cap, the
human-only approve/reject/export) are only worth anything if the *gateway*
decides who is asking. Until now the client said ``initiator: manual`` or
``agent`` and was believed; an ``agentos trade swap`` run from an agent's
shell with the session variables unset became a human order.

This module computes an :class:`AgentBinding` for every admitted connection.
The rule that orders everything below: **a sign that an agent is asking
outranks any proof an agent could have read.** The signals, strongest first:

1. **An agent token.** When an agent turn spawns a shell (or the scheduler
   runs a cron script), a token is minted here and passed to the child as
   ``AGENTOS_AGENT_TOKEN``. The CLI presents it back; the connection is bound
   to that session and agent. A connection that presents *any* agent token
   -- a live one, or one this gateway does not know (minted before a
   restart, or made up) -- is an agent's, whatever else it presents: an
   operator secret next to it is ignored.
2. **The peer process.** On Linux the gateway looks up which process holds
   the client end of a local connection (``gateway.peer_process``). One the
   gateway spawned, directly or not -- an agent's shell, a background
   process, ``execute_code``, an ACP harness, an MCP server -- is an agent's
   (``via="process"``), whatever it presents.
3. **The desktop's operator secret.** The desktop app spawns the gateway and
   hands it a random secret through a 0600 file the gateway deletes after
   reading; it is never on disk afterwards, never in the environment, and
   no agent process can read it. A connection presenting it is the
   operator's, even while an agent shell runs, so the app keeps working.
4. **The gateway token, from another machine** (rule "1b"). Neither secret
   can reach a gateway on another host (a container behind a reverse
   proxy): the desktop app in External mode, the web console and a remote
   CLI only hold the gateway's bearer token. A Control WebSocket that proved
   that token from another machine is the operator's. The peer is the
   socket's own address, never an ``X-Forwarded-For`` claim
   (``ws_transport``), and it must not be one of this host's own addresses:
   an agent shell on the gateway host that read the token (from the
   environment of a compose deployment, or the config file) and dialled the
   host's LAN or container address is still on this host. HTTP RPC does not
   apply this rule: it cannot see past uvicorn's proxy-header rewrite.
5. **An exec window.** While any agent shell is running (an ``exec_command``,
   a background process it left, a cron script, ``execute_code``), a new
   connection that proves nothing stronger is the agent's -- *including one
   that presents the operator file below*: that file is on this host's disk,
   so a shell that got past the sensitive-path floor could have read it. A
   human who opens the CLI in that window gets the agent's rules, which
   fail safe: their order waits for approval in the app instead of
   executing, and an approval they try to give is refused with a reason.
6. **The operator file.** The gateway writes its own secret at every boot to
   ``<agentos home>/wallets/operator.secret`` (0600, rotated each boot); the
   CLI on the same machine reads it back, and only over the WebSocket: an
   HTTP request presenting it is not the operator (nothing first-party
   sends it there, and an in-process HTTP tool could). The wallets
   directory, wherever ``AGENTOS_STATE_DIR`` puts it, is on the
   sensitive-path floor for agent tools, and the CLI never presents the
   file from an agent's session (``cli.gateway_client``).
7. **Nothing.** A connection with no secret, no token and no open window is
   *unbound* (``via="unbound"``) and gets the agent's rules too. Being
   anonymous is never a way to become the operator: a process an agent
   detached (``setsid``, ``nohup``, a double fork) that connects after the
   window closed is still treated as the agent's.

Nothing here is a full sandbox: a same-user process that escaped every rule
above (a detached process re-parented away from the gateway, on a host
without the OS sandbox) can still read the operator file once no agent shell
is running. It is the difference between a guardrail the prompt can talk its
way around and one it cannot.
"""

from __future__ import annotations

import contextlib
import hmac
import ipaddress
import os
import secrets
import tempfile
import threading
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from agentos.gateway.access import ConnectionSurface, is_own_address
from agentos.paths import default_agentos_home

if TYPE_CHECKING:
    from agentos.gateway.auth import AccessContext

log = structlog.get_logger(__name__)

AGENT_TOKEN_ENV = "AGENTOS_AGENT_TOKEN"
OPERATOR_SECRET_ENV = "AGENTOS_OPERATOR_SECRET"
OPERATOR_SECRET_FILE_ENV = "AGENTOS_OPERATOR_SECRET_FILE"
#: Where the gateway writes its own operator secret, relative to the AgentOS
#: home. Lives under ``wallets`` because that directory -- resolved from the
#: real state root, wherever ``AGENTOS_STATE_DIR`` puts it -- is on the
#: sensitive-path floor for agent tools (sandbox.sensitive_paths).
OPERATOR_SECRET_FILENAME = "wallets/operator.secret"

_MAX_TOKENS = 4096


def default_operator_secret_path(state_dir: Path | None = None) -> Path:
    """The operator secret file for *state_dir* (default: the AgentOS home)."""
    root = Path(state_dir) if state_dir is not None else default_agentos_home()
    return root / OPERATOR_SECRET_FILENAME


@dataclass(frozen=True)
class AgentBinding:
    """Why a connection counts as an agent's, and for which chat."""

    via: str  # "token" | "process" | "window" | "unbound"
    session_key: str | None = None
    agent_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"via": self.via, "sessionKey": self.session_key, "agentId": self.agent_id}

    @property
    def recognized(self) -> bool:
        """The gateway *knows* this is an agent (token, process or window).

        ``unbound`` is weaker: agent rules for trading, but the user's own
        web console on loopback is unbound too, so the human-decision surfaces
        (approvals, elevation, ``tools.invoke``) only refuse a recognized one.
        """
        return self.via != "unbound"


class AgentSurface:
    """Process-wide registry of agent tokens, exec windows and operator secrets."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: OrderedDict[str, AgentBinding] = OrderedDict()
        self._windows: dict[int, str | None] = {}
        self._next_window = 1
        self._operator_secrets: tuple[str, ...] = ()
        # The two named slots: what the desktop handed us at spawn, and what
        # we wrote to disk for the local CLI. Each is replaced, not stacked.
        self._env_secret: str | None = None
        self._file_secret: str | None = None
        self._operator_file: Path | None = None

    # ── operator ───────────────────────────────────────────────────────

    def add_operator_secret(self, secret: str | None) -> None:
        """Accept *secret* as proof of the operator, alongside any others."""
        if not secret:
            return
        with self._lock:
            if secret not in self._operator_secrets:
                self._operator_secrets = (*self._operator_secrets, secret)

    def _replace_operator_secret(self, old: str | None, new: str | None) -> None:
        with self._lock:
            kept = tuple(s for s in self._operator_secrets if s != old)
            if new and new not in kept:
                kept = (*kept, new)
            self._operator_secrets = kept

    def set_operator_secret(self, secret: str | None) -> None:
        """Set the desktop-handed secret (replacing the previous one, if any).

        Other secrets — the file the gateway wrote for the local CLI — are
        untouched; use :meth:`add_operator_secret` to accept one more.
        """
        secret = secret or None
        previous = self._env_secret
        self._env_secret = secret
        self._replace_operator_secret(previous, secret)

    @property
    def has_operator_secret(self) -> bool:
        return bool(self._operator_secrets)

    def is_operator(self, presented: Any) -> bool:
        if not isinstance(presented, str) or not presented:
            return False
        candidate = presented.encode()
        matched = False
        # Every secret is compared: constant time over the (tiny) set.
        for secret in self._operator_secrets:
            if hmac.compare_digest(candidate, secret.encode()):
                matched = True
        return matched

    def _is_spawn_secret(self, presented: Any) -> bool:
        """The desktop's secret: handed over in memory, never on disk afterwards."""
        secret = self._env_secret
        if not secret or not isinstance(presented, str) or not presented:
            return False
        return hmac.compare_digest(presented.encode(), secret.encode())

    @property
    def operator_file(self) -> Path | None:
        """Where :meth:`ensure_operator_file` last wrote, if it did."""
        return self._operator_file

    def ensure_operator_file(self, state_dir: Path | None = None) -> Path:
        """Write a fresh operator secret for the local CLI and accept it.

        The file is ``0600`` under a ``0700`` directory and is rotated on
        every call (every boot): a copy an agent somehow took earlier is
        worthless afterwards. Only the path is logged, never the secret.
        Raises :class:`OSError` when the file cannot be written; the caller
        decides whether that is fatal (at boot it is not: the desktop secret
        and the agent token still work, the local CLI is merely an agent).
        """
        path = default_operator_secret_path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(path.parent, 0o700)
        secret = secrets.token_urlsafe(32)
        fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        try:
            os.write(fd, (secret + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        # ``O_CREAT``'s mode only applies to a new file; a leftover one keeps
        # whatever it had, so tighten it explicitly.
        os.chmod(path, 0o600)
        previous = self._file_secret
        self._file_secret = secret
        self._operator_file = path
        self._replace_operator_secret(previous, secret)
        log.info("agent_surface.operator_file_written", path=str(path))
        return path

    def remove_operator_file(self) -> None:
        """Best-effort cleanup of the file written by :meth:`ensure_operator_file`."""
        path = self._operator_file
        previous = self._file_secret
        self._operator_file = None
        self._file_secret = None
        self._replace_operator_secret(previous, None)
        if path is not None:
            with contextlib.suppress(OSError):
                path.unlink()

    def export_operator_secret_file(self, state_dir: Path | None = None) -> Path | None:
        """Write the desktop-handed secret to a fresh 0600 file for a re-exec.

        :meth:`load_operator_secret_from_env` deletes the file the desktop
        handed us, so a gateway that re-executes itself in place has to hand
        the secret on the same way or the desktop renderer reconnects as an
        agent. The new image reads and deletes this file at boot exactly as
        it does the desktop's. Returns ``None`` when no desktop secret is
        held. Only the path is logged, never the secret.
        """
        secret = self._env_secret
        if not secret:
            return None
        directory = default_operator_secret_path(state_dir).parent
        directory.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(directory, 0o700)
        fd, raw_path = tempfile.mkstemp(prefix="operator-secret.reexec-", dir=str(directory))
        try:
            os.write(fd, (secret + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        path = Path(raw_path)
        os.chmod(path, 0o600)
        log.info("agent_surface.operator_secret_exported", path=str(path))
        return path

    def load_operator_secret_from_env(self, environ: dict[str, str] | None = None) -> bool:
        """Read the desktop's secret at boot and scrub every trace of it.

        The file form is preferred: it is deleted right after reading, so a
        child process cannot find it later. The bare variable form is removed
        from this process's environment for the same reason.
        """
        env = os.environ if environ is None else environ
        secret: str | None = None
        path = env.pop(OPERATOR_SECRET_FILE_ENV, None)
        if path:
            try:
                secret = Path(path).read_text(encoding="utf-8").strip() or None
            except OSError as exc:
                log.warning("agent_surface.operator_secret_unreadable", error=str(exc))
            with contextlib.suppress(OSError):
                Path(path).unlink()
        bare = env.pop(OPERATOR_SECRET_ENV, None)
        if secret is None and bare:
            secret = bare.strip() or None
        self.set_operator_secret(secret)
        if secret:
            log.info("agent_surface.operator_secret_loaded", source="file" if path else "env")
        return secret is not None

    # ── agent tokens ───────────────────────────────────────────────────

    def mint_token(self, session_key: str | None, agent_id: str | None) -> str:
        token = secrets.token_urlsafe(32)
        binding = AgentBinding(via="token", session_key=session_key or None, agent_id=agent_id)
        with self._lock:
            self._tokens[token] = binding
            while len(self._tokens) > _MAX_TOKENS:
                self._tokens.popitem(last=False)
        return token

    def resolve_token(self, presented: Any) -> AgentBinding | None:
        if not isinstance(presented, str) or not presented:
            return None
        with self._lock:
            # Constant-time over the candidate set: the token space is tiny
            # and a timing walk over a 43-char random string is not a real
            # risk, but there is no cost to doing it right.
            for token, binding in self._tokens.items():
                if hmac.compare_digest(token.encode(), presented.encode()):
                    return binding
        return None

    # ── exec windows ───────────────────────────────────────────────────

    def begin_window(self, session_key: str | None) -> int:
        with self._lock:
            handle = self._next_window
            self._next_window += 1
            self._windows[handle] = session_key or None
            return handle

    def end_window(self, handle: int) -> None:
        with self._lock:
            self._windows.pop(handle, None)

    @contextlib.contextmanager
    def exec_window(self, session_key: str | None) -> Iterator[None]:
        handle = self.begin_window(session_key)
        try:
            yield
        finally:
            self.end_window(handle)

    def active_windows(self) -> int:
        with self._lock:
            return len(self._windows)

    def _window_binding(self) -> AgentBinding | None:
        with self._lock:
            if not self._windows:
                return None
            keys = {k for k in self._windows.values() if k}
            session = next(iter(keys)) if len(keys) == 1 else None
            return AgentBinding(via="window", session_key=session)

    # ── admission ──────────────────────────────────────────────────────

    def bind(
        self,
        auth_params: dict[str, Any] | None,
        *,
        remote_operator: bool = False,
        agent_process: bool = False,
        file_secret: bool = True,
    ) -> AgentBinding | None:
        """The binding for a connection presenting ``auth_params``.

        ``None`` means the operator. The order is the module docstring's: an
        agent token (known or not) and an agent *process* come first and
        cannot be outvoted by any secret; the desktop's in-memory secret and
        ``remote_operator`` (the gateway token proved from another machine,
        see :meth:`attach`) are the operator even while a shell runs; an open
        exec window outranks the on-disk operator file; the file is honoured
        only when *file_secret* (the WebSocket handshake, never HTTP). A
        connection that proves nothing is ``via="unbound"``: agent rules.
        """
        params = auth_params if isinstance(auth_params, dict) else {}
        presented_token = params.get("agentToken")
        if presented_token:
            token = self.resolve_token(presented_token)
            if token is not None:
                return token
            # A token this gateway never minted (or minted before a restart):
            # the client says it is an agent, and adding a secret does not
            # change that.
            return self._window_binding() or AgentBinding(via="token")
        if agent_process:
            window = self._window_binding()
            return AgentBinding(via="process", session_key=window.session_key if window else None)
        secret = params.get("operatorSecret")
        if self._is_spawn_secret(secret):
            return None
        if remote_operator:
            return None
        window = self._window_binding()
        if window is not None:
            return window
        if file_secret and self.is_operator(secret):
            return None
        return AgentBinding(via="unbound")

    def attach(
        self,
        access: AccessContext,
        auth_params: dict[str, Any] | None,
        *,
        peer_ip: str | None = None,
        agent_process: bool = False,
        file_secret: bool = True,
    ) -> AccessContext:
        """Mark *access* with its binding.

        *peer_ip* is the transport peer (for rule 1b, WebSocket only);
        *agent_process* is :func:`gateway.peer_process.peer_is_descendant` for
        the connection; *file_secret* is false for HTTP requests.
        """
        binding = self.bind(
            auth_params,
            remote_operator=_token_proved_remotely(access, peer_ip),
            agent_process=agent_process,
            file_secret=file_secret,
        )
        if binding is None:
            return access
        log.debug(
            "agent_surface.bound",
            via=binding.via,
            session_key=binding.session_key,
            agent_id=binding.agent_id,
        )
        return replace(access, agent=binding)

    def reset(self) -> None:
        with self._lock:
            self._tokens.clear()
            self._windows.clear()
            self._operator_secrets = ()
            self._env_secret = None
            self._file_secret = None
            self._operator_file = None


def _token_proved_remotely(access: AccessContext, peer_ip: str | None) -> bool:
    """A Control connection that proved the gateway token from another machine.

    ``credential_verified`` is only set by token auth. The peer must be a
    literal IP that is neither loopback nor one of this host's own addresses:
    an agent shell on the gateway host that dials the host's LAN or container
    address (``172.18.0.2`` in the compose network) is still on this host,
    and the token it presents may well be one it read from the gateway's
    environment or config file. Anything unparsable (a test client, a socket
    path) fails closed, and so does an IPv6 link-local peer, whose zone the
    address check below cannot see.
    """
    if not access.credential_verified or access.surface != ConnectionSurface.CONTROL:
        return False
    host = (peer_ip or "").strip().strip("[]").split("%", 1)[0]
    try:
        addr: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if addr.is_loopback or addr.is_unspecified or addr.is_multicast or addr.is_link_local:
        return False
    return not is_own_address(addr)


_surface = AgentSurface()


def get_agent_surface() -> AgentSurface:
    return _surface


def ensure_operator_file(state_dir: Path | None = None) -> Path:
    """Module-level shorthand for the process-wide surface (see the method)."""
    return _surface.ensure_operator_file(state_dir)


def remove_operator_file() -> None:
    _surface.remove_operator_file()


def agent_binding(ctx: Any) -> AgentBinding | None:
    """The binding on an RPC context, tolerant of test doubles."""
    access = getattr(ctx, "access", None)
    binding = getattr(access, "agent", None)
    return binding if isinstance(binding, AgentBinding) else None


def recognized_agent(ctx: Any) -> AgentBinding | None:
    """The binding on *ctx* when the gateway recognizes the caller as an agent.

    Token, process and window bindings; not ``unbound`` (see
    :attr:`AgentBinding.recognized`).
    """
    binding = agent_binding(ctx)
    return binding if binding is not None and binding.recognized else None


__all__ = [
    "AGENT_TOKEN_ENV",
    "OPERATOR_SECRET_ENV",
    "OPERATOR_SECRET_FILE_ENV",
    "OPERATOR_SECRET_FILENAME",
    "AgentBinding",
    "AgentSurface",
    "agent_binding",
    "default_operator_secret_path",
    "ensure_operator_file",
    "get_agent_surface",
    "is_own_address",
    "recognized_agent",
    "remove_operator_file",
]
