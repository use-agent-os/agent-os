"""``AcpRuntime`` implementation over the ``acp`` SDK.

One harness process per ACP session. :class:`AcpRuntimeImpl` resolves the
launch argv (``[acp.agents.<id>]`` override, then the built-in registry),
spawns the harness in its own process group with a lease, runs
``initialize`` + ``session/new`` (or ``session/load`` when resuming), and
drives turns: ``start_turn`` returns an :class:`AcpTurn` whose ``events``
iterator yields normalized :class:`AcpEvent` objects and ends with a
synthesized ``done`` (or ``error``) event; ``result`` resolves to an
:class:`AcpTurnResult`.

Failure contract (what callers can rely on):

- ``ensure_session`` raises :class:`AcpSessionInitError` (``code`` tells
  why: ``ACP_HARNESS_MISSING``, ``ACP_UNKNOWN_AGENT``, ``ACP_SPAWN_FAILED``,
  ``ACP_ADAPTER_MISSING``, ``ACP_CAPACITY``, ``ACP_CWD_MISSING``,
  ``ACP_SESSION_INIT_FAILED``) or :class:`AcpAgentNotAllowedError`.
- A turn that ends with any ACP stop reason (``end_turn``, ``max_tokens``,
  ``max_turn_requests``, ``refusal``, ``cancelled``) resolves ``result``
  normally; the last event is ``done``.
- A turn that fails resolves ``result`` by *raising* an :class:`AcpError`
  subclass (``AcpTurnTimeoutError``, ``PermissionPromptUnavailableError``,
  or ``AcpTurnError`` with ``code="ACP_PROCESS_EXITED"`` /
  ``"ACP_TURN_FAILED"``); the last event is ``error`` whose ``error``
  field is ``"<code>: <message>"``.

Lifecycle and cancel ladder follow OpenClaw's acpx runtime (MIT, see
``THIRD_PARTY_NOTICES.md``): ``session/cancel``, wait up to 2 s for the
prompt to return, then SIGTERM the process group and SIGKILL 750 ms later.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

import structlog

from agentos.acp import process as acp_process
from agentos.acp.client import AgentOSAcpClient, _dump
from agentos.acp.registry import ResolvedLaunch, normalize_agent_id, resolve_launch
from agentos.acp.store import AcpSessionStore, argv_hash
from agentos.acp.types import (
    AcpAgentNotAllowedError,
    AcpError,
    AcpEvent,
    AcpSessionHandle,
    AcpSessionInitError,
    AcpSessionSpec,
    AcpTurn,
    AcpTurnError,
    AcpTurnResult,
    AcpTurnTimeoutError,
    AcpUsage,
    ElicitationBridge,
    PermissionBridge,
)

if TYPE_CHECKING:
    from acp.client.connection import ClientSideConnection

_log = structlog.get_logger("agentos.acp.runtime")

_LAUNCHERS = frozenset({"npx", "uvx", "bunx", "pnpx", "pnpm", "yarn"})
_STOP_REASONS = frozenset({"end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"})


class AcpAgentOverrideLike(Protocol):
    @property
    def command(self) -> str | None: ...
    @property
    def args(self) -> list[str]: ...
    @property
    def env(self) -> dict[str, str]: ...


class AcpConfigLike(Protocol):
    """The slice of ``agentos.gateway.config.AcpConfig`` the runtime reads.

    Typed structurally so ``agentos.acp`` does not import ``agentos.gateway``
    (architecture import contract); pass ``config.acp``.
    """

    @property
    def allowed_agents(self) -> list[str]: ...
    @property
    def turn_timeout_s(self) -> int: ...
    @property
    def max_concurrent(self) -> int: ...
    @property
    def state_dir(self) -> str | None: ...
    @property
    def isolate_codex_home(self) -> bool: ...
    @property
    def install_missing_adapters(self) -> bool: ...
    @property
    def agents(self) -> Mapping[str, AcpAgentOverrideLike]: ...


# --------------------------------------------------------------------------
# Internal state
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Turn:
    queue: asyncio.Queue[AcpEvent | None]
    result: asyncio.Future[AcpTurnResult]
    started: float = field(default_factory=time.monotonic)
    prompt_task: asyncio.Task[Any] | None = None
    runner: asyncio.Task[None] | None = None
    cancel_task: asyncio.Task[None] | None = None
    cancel_requested: bool = False
    cancel_reason: str = ""
    stream_closed: bool = False
    finished: bool = False
    text_parts: list[str] = field(default_factory=list)
    tool_ids: set[str] = field(default_factory=set)
    cost_seen: bool = False
    cost_before: float | None = None
    failure: BaseException | None = None


@dataclass(slots=True)
class _Live:
    handle: AcpSessionHandle
    session_key: str
    spec: AcpSessionSpec
    resolved: ResolvedLaunch
    proc: acp_process.HarnessProcess
    conn: ClientSideConnection
    client: AgentOSAcpClient
    turn_timeout_s: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    turn: _Turn | None = None
    closing: bool = False


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


_USAGE_KEYS: dict[str, tuple[str, ...]] = {
    "input_tokens": ("inputTokens", "input_tokens", "prompt_tokens"),
    "output_tokens": ("outputTokens", "output_tokens", "completion_tokens"),
    "cached_read_tokens": (
        "cachedReadTokens",
        "cached_read_tokens",
        "cache_read_input_tokens",
        "cached_input_tokens",
    ),
    "cached_write_tokens": (
        "cachedWriteTokens",
        "cached_write_tokens",
        "cache_creation_input_tokens",
    ),
    "thought_tokens": (
        "thoughtTokens",
        "thought_tokens",
        "reasoning_output_tokens",
        "reasoning_tokens",
    ),
    "total_tokens": ("totalTokens", "total_tokens"),
}


def usage_from_mapping(data: Any) -> AcpUsage | None:
    """Lenient token-usage parse (camelCase ACP ``Usage`` or snake_case
    ``_meta.quota`` / ``_meta.usage`` blobs, one nesting level allowed)."""
    if not isinstance(data, dict):
        return None
    for nested in ("token_count", "tokenUsage", "token_usage", "usage", "last"):
        inner = data.get(nested)
        if isinstance(inner, dict) and not any(
            k in data for keys in _USAGE_KEYS.values() for k in keys
        ):
            return usage_from_mapping(inner)
    found = False
    values: dict[str, int] = {}
    for name, keys in _USAGE_KEYS.items():
        for k in keys:
            if k in data and data[k] is not None:
                values[name] = _int(data[k])
                found = True
                break
    if not found:
        return None
    usage = AcpUsage(
        input_tokens=values.get("input_tokens", 0),
        output_tokens=values.get("output_tokens", 0),
        cached_read_tokens=values.get("cached_read_tokens", 0),
        cached_write_tokens=values.get("cached_write_tokens", 0),
        thought_tokens=values.get("thought_tokens", 0),
        total_tokens=values.get("total_tokens", 0),
    )
    if not usage.total_tokens:
        usage.total_tokens = usage.input_tokens + usage.output_tokens
    return usage


def _attachment_blocks(attachments: list[dict[str, Any]] | None) -> list[Any]:
    import acp

    blocks: list[Any] = []
    for att in attachments or []:
        if not isinstance(att, dict):
            continue
        kind = str(att.get("type") or "").lower()
        mime = att.get("mime_type") or att.get("mimeType")
        if kind == "image" and att.get("data") and mime:
            blocks.append(acp.image_block(str(att["data"]), str(mime), uri=att.get("uri")))
        elif kind == "text" and att.get("text"):
            blocks.append(acp.text_block(str(att["text"])))
        elif kind in ("resource_link", "file", "link") and (att.get("uri") or att.get("path")):
            uri = str(att.get("uri") or att.get("path"))
            if "://" not in uri:
                uri = Path(uri).expanduser().resolve().as_uri()
            name = str(att.get("name") or Path(uri).name or uri)
            blocks.append(acp.resource_link_block(name, uri, mime_type=mime))
        else:
            _log.info("acp.runtime.attachment_skipped", kind=kind)
    return blocks


def _mcp_servers(servers: list[dict[str, Any]] | None) -> list[Any]:
    """Validate MCP server dicts into the SDK's union models."""
    if not servers:
        return []
    from acp.schema import AcpMcpServer, HttpMcpServer, McpServerStdio, SseMcpServer
    from pydantic import TypeAdapter

    adapter: TypeAdapter[list[HttpMcpServer | SseMcpServer | AcpMcpServer | McpServerStdio]]
    adapter = TypeAdapter(list[HttpMcpServer | SseMcpServer | AcpMcpServer | McpServerStdio])
    try:
        return list(adapter.validate_python(servers))
    except Exception as exc:
        raise AcpSessionInitError(
            f"invalid mcp_servers for ACP session: {exc}", code="ACP_SESSION_INIT_FAILED"
        ) from exc


def _modes_from(resp: Any) -> tuple[str | None, list[dict[str, Any]]]:
    modes = getattr(resp, "modes", None)
    if modes is None:
        return None, []
    return (
        str(modes.current_mode_id),
        [_dump(m) for m in (modes.available_modes or [])],
    )


def _config_from(resp: Any) -> list[dict[str, Any]]:
    opts = getattr(resp, "config_options", None)
    return [_dump(o) for o in (opts or [])]


def _find_option(options: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    for opt in options:
        if opt.get("id") == key:
            return opt
    return None


def _option_values(opt: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for item in opt.get("options") or []:
        if isinstance(item, dict):
            if "value" in item:
                out.add(str(item["value"]))
            for sub in item.get("options") or []:
                if isinstance(sub, dict) and "value" in sub:
                    out.add(str(sub["value"]))
    return out


# --------------------------------------------------------------------------
# Runtime
# --------------------------------------------------------------------------


class AcpRuntimeImpl:
    """Drives every ACP harness; the registry (or config) supplies argv.

    ``permission_bridge`` / ``elicitation_bridge`` come from the permissions
    stream; without them permission prompts are rejected (``reject_once``)
    and elicitations declined, never approved.
    """

    def __init__(
        self,
        config: AcpConfigLike,
        *,
        permission_bridge: PermissionBridge | None = None,
        elicitation_bridge: ElicitationBridge | None = None,
        state_dir: Path | str | None = None,
        gateway_instance_id: str | None = None,
        store: AcpSessionStore | None = None,
        init_timeout_s: float = 120.0,
        cancel_wait_s: float = 2.0,
        kill_grace_s: float = 0.75,
        close_wait_s: float = 1.0,
        which: Callable[[str], str | None] = shutil.which,
        base_env: dict[str, str] | None = None,
        user_codex_home: Path | None = None,
    ) -> None:
        self.config = config
        self.permission_bridge = permission_bridge
        self.elicitation_bridge = elicitation_bridge
        if state_dir is None:
            if config.state_dir:
                state_dir = Path(config.state_dir).expanduser()
            else:
                from agentos.paths import state_dir as _state_dir

                state_dir = _state_dir("acp")
        self.state_dir = Path(state_dir)
        self.gateway_instance_id = gateway_instance_id or uuid.uuid4().hex
        self.store = store or AcpSessionStore(self.state_dir)
        self.init_timeout_s = init_timeout_s
        self.cancel_wait_s = cancel_wait_s
        self.kill_grace_s = kill_grace_s
        self.close_wait_s = close_wait_s
        self._which = which
        self._base_env = base_env
        self._user_codex_home = user_codex_home
        self._live: dict[str, _Live] = {}
        self._by_key: dict[str, str] = {}
        self._spawn_lock = asyncio.Lock()
        self._bg: set[asyncio.Task[Any]] = set()

    # -- launch resolution --------------------------------------------

    def _overrides(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for raw_id, ov in (self.config.agents or {}).items():
            if ov.command:
                out[normalize_agent_id(raw_id)] = [ov.command, *ov.args]
        return out

    def _override_env(self, agent_id: str) -> dict[str, str]:
        for raw_id, ov in (self.config.agents or {}).items():
            if normalize_agent_id(raw_id) == agent_id:
                return dict(ov.env or {})
        return {}

    def resolve(self, agent_id: str) -> ResolvedLaunch:
        aid = normalize_agent_id(agent_id)
        try:
            return resolve_launch(aid, overrides=self._overrides(), which=self._which)
        except KeyError as exc:
            raise AcpSessionInitError(
                f"unknown ACP agent {agent_id!r}", code="ACP_UNKNOWN_AGENT", agent_id=aid
            ) from exc

    def _check_allowed(self, aid: str) -> None:
        allowed = [normalize_agent_id(a) for a in (self.config.allowed_agents or [])]
        if "*" in allowed or aid in allowed:
            return
        raise AcpAgentNotAllowedError(
            f"ACP agent {aid!r} is not in acp.allowed_agents", agent_id=aid
        )

    def _check_launchable(self, resolved: ResolvedLaunch) -> None:
        if resolved.missing_commands:
            raise AcpSessionInitError(
                f"ACP agent {resolved.agent_id!r} needs {', '.join(resolved.missing_commands)} "
                "on PATH",
                code="ACP_HARNESS_MISSING",
                missing=list(resolved.missing_commands),
                agent_id=resolved.agent_id,
            )
        if (
            resolved.source == "package-exec"
            and not self.config.install_missing_adapters
            and os.path.basename(resolved.argv[0]) in _LAUNCHERS
        ):
            raise AcpSessionInitError(
                f"ACP adapter for {resolved.agent_id!r} is not installed and "
                "acp.install_missing_adapters is off",
                code="ACP_ADAPTER_MISSING",
                argv=list(resolved.argv),
                agent_id=resolved.agent_id,
            )

    # -- ensure_session -----------------------------------------------

    async def ensure_session(self, spec: AcpSessionSpec, *, session_key: str) -> AcpSessionHandle:
        aid = normalize_agent_id(spec.agent_id)
        self._check_allowed(aid)
        resolved = self.resolve(aid)
        self._check_launchable(resolved)
        cwd = os.path.abspath(os.path.expanduser(spec.cwd or os.getcwd()))
        if not os.path.isdir(cwd):
            raise AcpSessionInitError(
                f"ACP session cwd does not exist: {cwd}", code="ACP_CWD_MISSING", cwd=cwd
            )
        ahash = argv_hash(resolved.argv)

        async with self._spawn_lock:
            live = self._live_for_key(session_key)
            if live is not None:
                h = live.handle
                if live.proc.alive and (h.agent_id, h.cwd, h.argv_hash) == (aid, cwd, ahash):
                    return h
                await self._dispose(live, discard=False)

            if len(self._live) >= self.config.max_concurrent:
                raise AcpSessionInitError(
                    f"too many live ACP sessions (acp.max_concurrent={self.config.max_concurrent})",
                    code="ACP_CAPACITY",
                )

            prior = None
            if spec.resume_session_id:
                prior = self.store.find_reusable(
                    aid, cwd, ahash, acp_session_id=spec.resume_session_id
                )
                if prior is None:
                    _log.info("acp.runtime.resume_mismatch", agent_id=aid)
            else:
                prior = self._record_for_key(session_key, aid, cwd, ahash)
            record_id = prior.handle.record_id if prior else uuid.uuid4().hex
            resume_id = prior.handle.acp_session_id if prior else None

            try:
                live = await self._start(
                    spec, session_key, resolved, cwd, ahash, record_id, resume_id
                )
            except AcpError as exc:
                if not resume_id:
                    raise
                _log.warning("acp.runtime.resume_failed", agent_id=aid, error=str(exc))
                live = await self._start(spec, session_key, resolved, cwd, ahash, record_id, None)
            if prior is not None:
                live.handle.created_at = prior.handle.created_at
            self._live[record_id] = live
            self._by_key[session_key] = record_id
            self.store.save(live.handle, session_key=session_key)
            _log.info(
                "acp.runtime.session_ready",
                agent_id=aid,
                record_id=record_id,
                pid=live.proc.pid,
                resumed=bool(resume_id and live.handle.acp_session_id == resume_id),
            )
            return live.handle

    def _live_for_key(self, session_key: str) -> _Live | None:
        rid = self._by_key.get(session_key)
        return self._live.get(rid) if rid else None

    def _record_for_key(self, session_key: str, aid: str, cwd: str, ahash: str) -> Any:
        if not session_key:
            return None
        return self.store.find_reusable(aid, cwd, ahash, session_key=session_key)

    def _env_for(self, aid: str, spec: AcpSessionSpec, lease_id: str, cwd: str) -> dict[str, str]:
        extra: dict[str, str] = {}
        if aid == "codex" and self.config.isolate_codex_home:
            home = acp_process.prepare_codex_home(
                self.state_dir, cwd, user_codex_home=self._user_codex_home
            )
            extra["CODEX_HOME"] = str(home)
        extra.update(self._override_env(aid))
        extra.update({str(k): str(v) for k, v in (spec.env or {}).items()})
        return acp_process.harness_env(lease_id=lease_id, base=self._base_env, extra=extra)

    def _client_capabilities(self) -> Any:
        from acp.schema import (
            ClientCapabilities,
            ElicitationCapabilities,
            ElicitationFormCapabilities,
            ElicitationUrlCapabilities,
            FileSystemCapabilities,
        )

        return ClientCapabilities(
            fs=FileSystemCapabilities(read_text_file=False, write_text_file=False),
            terminal=False,
            elicitation=(
                ElicitationCapabilities(
                    form=ElicitationFormCapabilities(), url=ElicitationUrlCapabilities()
                )
                if self.elicitation_bridge is not None
                else None
            ),
        )

    async def _start(
        self,
        spec: AcpSessionSpec,
        session_key: str,
        resolved: ResolvedLaunch,
        cwd: str,
        ahash: str,
        record_id: str,
        resume_id: str | None,
    ) -> _Live:
        import acp
        from acp.schema import Implementation

        from agentos import __version__

        aid = resolved.agent_id
        lease_id = uuid.uuid4().hex[:16]
        env = self._env_for(aid, spec, lease_id, cwd)
        proc = await acp_process.launch_harness(
            resolved,
            cwd=cwd,
            env=env,
            state_dir=self.state_dir,
            lease_id=lease_id,
            session_key=session_key,
            gateway_instance_id=self.gateway_instance_id,
            record_id=record_id,
        )
        client = AgentOSAcpClient(
            agent_id=aid,
            cwd=cwd,
            session_key=session_key,
            permission_bridge=self.permission_bridge,
            elicitation_bridge=self.elicitation_bridge,
        )
        conn = acp.connect_to_agent(
            cast("acp.Client", client), proc.stdin, proc.stdout, use_unstable_protocol=True
        )
        try:
            init = await asyncio.wait_for(
                conn.initialize(
                    protocol_version=acp.PROTOCOL_VERSION,
                    client_capabilities=self._client_capabilities(),
                    client_info=Implementation(
                        name="agentos", title="AgentOS", version=__version__
                    ),
                ),
                self.init_timeout_s,
            )
            caps = init.agent_capabilities
            caps_dump = _dump(caps) if caps is not None else {}
            if init.agent_info is not None:
                caps_dump["_agent_info"] = _dump(init.agent_info)
            mcp = _mcp_servers(spec.mcp_servers)
            session_id: str | None = None
            resp: Any = None
            if resume_id and caps is not None and caps.load_session:
                try:
                    resp = await asyncio.wait_for(
                        conn.load_session(cwd=cwd, session_id=resume_id, mcp_servers=mcp),
                        self.init_timeout_s,
                    )
                    session_id = resume_id
                except Exception as exc:
                    _log.warning("acp.runtime.load_failed", agent_id=aid, error=str(exc))
                    if not proc.alive:
                        raise
            if session_id is None:
                new = await asyncio.wait_for(
                    conn.new_session(cwd=cwd, mcp_servers=mcp), self.init_timeout_s
                )
                session_id = new.session_id
                resp = new
        except BaseException as exc:
            tail = proc.stderr_tail(10)
            with contextlib.suppress(Exception):
                await conn.close()
            await acp_process.terminate(proc, grace_s=self.kill_grace_s)
            if isinstance(exc, asyncio.CancelledError):
                raise
            if isinstance(exc, AcpError):
                raise
            msg = str(exc) or type(exc).__name__
            if isinstance(exc, TimeoutError):
                msg = f"timed out after {self.init_timeout_s:g}s"
            raise AcpSessionInitError(
                f"ACP agent {aid!r} failed to start a session: {msg}"
                + (f"\nstderr:\n{tail}" if tail else ""),
                agent_id=aid,
                stderr_tail=tail,
            ) from exc

        client.acp_session_id = session_id
        current_mode, modes = _modes_from(resp)
        handle = AcpSessionHandle(
            record_id=record_id,
            agent_id=aid,
            cwd=cwd,
            argv=list(resolved.argv),
            argv_hash=ahash,
            acp_session_id=session_id,
            pid=proc.pid,
            lease_id=lease_id,
            mode=spec.mode,
            current_mode_id=client.current_mode_id or current_mode,
            available_modes=modes,
            config_options=_config_from(resp),
            agent_capabilities=caps_dump,
            status="idle",
        )
        timeout = float(spec.turn_timeout_s or self.config.turn_timeout_s or 0.0)
        live = _Live(
            handle=handle,
            session_key=session_key,
            spec=spec,
            resolved=resolved,
            proc=proc,
            conn=conn,
            client=client,
            turn_timeout_s=timeout,
        )
        await self._apply_initial_settings(live)
        return live

    async def _apply_initial_settings(self, live: _Live) -> None:
        spec, h = live.spec, live.handle
        if spec.harness_mode and spec.harness_mode != h.current_mode_id:
            mode_ids = {str(m.get("id")) for m in h.available_modes}
            mode_opt = next(
                (
                    o
                    for o in h.config_options
                    if o.get("category") == "mode" or o.get("id") == "mode"
                ),
                None,
            )
            try:
                if spec.harness_mode in mode_ids:
                    await self._set_mode(live, spec.harness_mode, persist=False)
                elif mode_opt is not None and spec.harness_mode in _option_values(mode_opt):
                    await self._set_config_option(
                        live, str(mode_opt["id"]), spec.harness_mode, persist=False
                    )
                else:
                    _log.warning(
                        "acp.runtime.mode_unavailable",
                        agent_id=h.agent_id,
                        mode=spec.harness_mode,
                        available=sorted(mode_ids),
                    )
            except AcpError as exc:
                _log.warning("acp.runtime.mode_failed", agent_id=h.agent_id, error=str(exc))
        if spec.model:
            model_opt = next(
                (
                    o
                    for o in h.config_options
                    if o.get("category") == "model" or o.get("id") == "model"
                ),
                None,
            )
            if model_opt is not None and spec.model != model_opt.get("currentValue"):
                try:
                    await self._set_config_option(
                        live, str(model_opt["id"]), spec.model, persist=False
                    )
                except AcpError as exc:
                    _log.warning("acp.runtime.model_failed", agent_id=h.agent_id, error=str(exc))

    # -- turns ----------------------------------------------------------

    def _require_live(self, handle: AcpSessionHandle) -> _Live:
        live = self._live.get(handle.record_id)
        if live is None or live.closing:
            raise AcpTurnError(
                f"ACP session {handle.record_id} is not running", code="ACP_SESSION_NOT_RUNNING"
            )
        return live

    async def start_turn(
        self,
        handle: AcpSessionHandle,
        *,
        text: str,
        attachments: list[dict[str, Any]] | None = None,
        session_key: str,
    ) -> AcpTurn:
        import acp

        live = self._require_live(handle)
        await live.lock.acquire()
        try:
            if not live.proc.alive or live.closing:
                live.handle.status = "unhealthy"
                raise AcpTurnError(
                    f"ACP agent {live.handle.agent_id!r} exited (code {live.proc.returncode})",
                    code="ACP_PROCESS_EXITED",
                )
            loop = asyncio.get_running_loop()
            turn = _Turn(queue=asyncio.Queue(), result=loop.create_future())
            # Consume an unawaited failure so asyncio does not log it.
            turn.result.add_done_callback(lambda f: None if f.cancelled() else f.exception())
            turn.cost_before = live.client.last_cost_usd
            blocks = [acp.text_block(text), *_attachment_blocks(attachments)]
            live.turn = turn
            live.handle.status = "busy"
            live.handle.last_used_at = time.time()
            live.client.attach(lambda ev: self._on_event(turn, ev))
            live.client.on_turn_failure = lambda exc: self._on_turn_failure(live, turn, exc)
            turn.runner = asyncio.create_task(
                self._run_turn(live, turn, blocks), name=f"acp-turn-{handle.record_id}"
            )
        except BaseException:
            live.lock.release()
            raise
        _log.info(
            "acp.turn.started",
            agent_id=live.handle.agent_id,
            record_id=handle.record_id,
            chars=len(text),
            attachments=len(attachments or []),
        )

        async def _cancel() -> None:
            await self._cancel_turn(live, turn, "cancelled by caller")

        async def _close_stream() -> None:
            turn.stream_closed = True
            while not turn.queue.empty():
                turn.queue.get_nowait()
            turn.queue.put_nowait(None)

        return AcpTurn(
            events=self._iter_events(turn),
            result=turn.result,
            cancel=_cancel,
            close_stream=_close_stream,
        )

    def _on_event(self, turn: _Turn, event: AcpEvent) -> None:
        if event.kind == "text_delta" and event.text:
            turn.text_parts.append(event.text)
        elif event.kind == "tool_call" and event.tool_call_id:
            turn.tool_ids.add(event.tool_call_id)
        elif event.kind == "usage" and event.cost_usd is not None:
            turn.cost_seen = True
        if not turn.stream_closed:
            turn.queue.put_nowait(event)

    def _on_turn_failure(self, live: _Live, turn: _Turn, exc: BaseException) -> None:
        if turn.failure is None:
            turn.failure = exc
        self._spawn_bg(self._cancel_turn(live, turn, f"turn failed: {exc}"))

    def _spawn_bg(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _iter_events(self, turn: _Turn) -> AsyncIterator[AcpEvent]:
        while True:
            ev = await turn.queue.get()
            if ev is None:
                return
            yield ev
            if ev.kind in ("done", "error"):
                return

    async def _run_turn(self, live: _Live, turn: _Turn, blocks: list[Any]) -> None:
        h = live.handle
        session_id = h.acp_session_id or ""
        timed_out = False
        outcome: AcpTurnResult | None = None
        failure: AcpError | None = None
        try:
            turn.prompt_task = asyncio.create_task(
                live.conn.prompt(session_id=session_id, prompt=blocks)
            )
            timeout = live.turn_timeout_s if live.turn_timeout_s > 0 else None
            done, _ = await asyncio.wait({turn.prompt_task}, timeout=timeout)
            if not done:
                timed_out = True
                _log.warning("acp.turn.timeout", agent_id=h.agent_id, timeout_s=timeout)
                await self._cancel_turn(live, turn, "turn timeout")
                with contextlib.suppress(BaseException):
                    await asyncio.wait({turn.prompt_task}, timeout=self.kill_grace_s + 2.5)
            # Everything the harness sent before its response is in the queue
            # (the SDK waits for in-flight session/update handlers).
            pt = turn.prompt_task
            resp: Any = None
            err: BaseException | None = None
            if pt.done() and not pt.cancelled():
                err = pt.exception()
                if err is None:
                    resp = pt.result()
            elif not pt.done():
                pt.cancel()
                err = AcpTurnError("prompt did not return", code="ACP_TURN_FAILED")
            else:
                err = AcpTurnError("prompt was cancelled", code="ACP_TURN_FAILED")

            if turn.failure is not None:
                failure = (
                    turn.failure
                    if isinstance(turn.failure, AcpError)
                    else AcpTurnError(str(turn.failure), code="ACP_TURN_FAILED")
                )
            elif timed_out:
                failure = AcpTurnTimeoutError(
                    f"ACP turn exceeded {live.turn_timeout_s:g}s", timeout_s=live.turn_timeout_s
                )
            elif resp is not None:
                outcome = self._build_result(live, turn, resp)
            elif turn.cancel_requested:
                outcome = self._build_result(live, turn, None, stop_reason="cancelled")
            else:
                exited = not live.proc.alive
                if exited or isinstance(err, ConnectionError):
                    # A closed pipe usually means the agent died, but asyncio
                    # may not have reaped it yet; until it has, the process
                    # still reads as alive and the session would be left
                    # "idle" on a dead agent instead of being closed.
                    await live.proc.wait(0.5)
                    exited = not live.proc.alive
                tail = live.proc.stderr_tail(10)
                if exited or isinstance(err, ConnectionError):
                    failure = AcpTurnError(
                        f"ACP agent {h.agent_id!r} exited mid-turn "
                        f"(code {live.proc.returncode})" + (f"\nstderr:\n{tail}" if tail else ""),
                        code="ACP_PROCESS_EXITED",
                        returncode=live.proc.returncode,
                    )
                else:
                    failure = AcpTurnError(f"ACP prompt failed: {err}", code="ACP_TURN_FAILED")
        except Exception as exc:  # defensive: never leave the turn hanging
            failure = AcpTurnError(f"ACP turn crashed: {exc}", code="ACP_TURN_FAILED")
        finally:
            live.client.detach()
            live.client.on_turn_failure = None

        if failure is not None:
            ev = AcpEvent(kind="error", error=f"{failure.code}: {failure}", stop_reason="error")
        else:
            assert outcome is not None
            ev = AcpEvent(kind="done", stop_reason=outcome.stop_reason, text=outcome.text)
            ev.usage = {
                "input_tokens": outcome.usage.input_tokens,
                "output_tokens": outcome.usage.output_tokens,
                "total_tokens": outcome.usage.total_tokens,
            }
            ev.cost_usd = outcome.usage.cost_usd
        if not turn.stream_closed:
            turn.queue.put_nowait(ev)
            turn.queue.put_nowait(None)
        turn.finished = True
        live.turn = None

        proc_ok = live.proc.alive
        if not proc_ok:
            h.status = "unhealthy"
            h.pid = None
        else:
            h.status = "idle"
        h.last_used_at = time.time()
        if h.current_mode_id != live.client.current_mode_id and live.client.current_mode_id:
            h.current_mode_id = live.client.current_mode_id
        if live.client.config_options is not None:
            h.config_options = live.client.config_options
        with contextlib.suppress(Exception):
            if not live.closing:
                self.store.save(h)
        live.lock.release()

        close_after = h.mode == "oneshot" or not proc_ok
        if close_after and not live.closing:
            with contextlib.suppress(Exception):
                await self.close(h, discard=False)

        _log.info(
            "acp.turn.finished",
            agent_id=h.agent_id,
            record_id=h.record_id,
            stop_reason=(outcome.stop_reason if outcome else "error"),
            code=(failure.code if failure else None),
            duration_ms=int((time.monotonic() - turn.started) * 1000),
        )
        if failure is not None:
            turn.result.set_exception(failure)
        else:
            assert outcome is not None
            turn.result.set_result(outcome)

    def _build_result(
        self, live: _Live, turn: _Turn, resp: Any, *, stop_reason: str | None = None
    ) -> AcpTurnResult:
        reason = stop_reason or str(getattr(resp, "stop_reason", "") or "end_turn")
        if reason not in _STOP_REASONS:
            reason = str(reason)
        usage: AcpUsage | None = None
        if resp is not None and getattr(resp, "usage", None) is not None:
            usage = usage_from_mapping(_dump(resp.usage))
        if usage is None and resp is not None:
            meta = getattr(resp, "field_meta", None) or {}
            for key in ("quota", "usage"):
                usage = usage_from_mapping(meta.get(key))
                if usage is not None:
                    break
        usage = usage or AcpUsage()
        client = live.client
        if client.last_usage:
            usage.context_used = client.last_usage.get("used")
            usage.context_size = client.last_usage.get("size")
        if turn.cost_seen and client.last_cost_usd is not None:
            before = turn.cost_before
            cost = client.last_cost_usd
            # usage_update.cost is cumulative per session; report this turn's share.
            usage.cost_usd = (
                round(cost - before, 10) if before is not None and cost >= before else cost
            )
        model_opt = next(
            (
                o
                for o in (client.config_options or live.handle.config_options)
                if o.get("category") == "model" or o.get("id") == "model"
            ),
            None,
        )
        if model_opt and model_opt.get("currentValue"):
            usage.model = str(model_opt["currentValue"])
        return AcpTurnResult(
            stop_reason=reason,
            text="".join(turn.text_parts),
            usage=usage,
            duration_ms=int((time.monotonic() - turn.started) * 1000),
            tool_calls=len(turn.tool_ids),
        )

    async def _cancel_turn(self, live: _Live, turn: _Turn, reason: str) -> None:
        if turn.finished:
            return
        if turn.cancel_task is None:
            turn.cancel_requested = True
            turn.cancel_reason = reason
            turn.cancel_task = asyncio.ensure_future(self._cancel_ladder(live, turn))
        await asyncio.shield(turn.cancel_task)

    async def _cancel_ladder(self, live: _Live, turn: _Turn) -> None:
        h = live.handle
        _log.info("acp.turn.cancel", agent_id=h.agent_id, record_id=h.record_id)
        pt = turn.prompt_task
        if live.proc.alive and h.acp_session_id:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(live.conn.cancel(session_id=h.acp_session_id), 1.0)
        if pt is not None and not pt.done():
            await asyncio.wait({pt}, timeout=self.cancel_wait_s)
        if pt is not None and not pt.done():
            _log.warning("acp.turn.cancel_escalate", agent_id=h.agent_id, pid=live.proc.pid)
            await acp_process.terminate(live.proc, grace_s=self.kill_grace_s)

    async def cancel(self, handle: AcpSessionHandle, *, reason: str = "") -> None:
        live = self._live.get(handle.record_id)
        if live is None or live.turn is None:
            return
        await self._cancel_turn(live, live.turn, reason or "cancelled")

    # -- close / status --------------------------------------------------

    async def close(self, handle: AcpSessionHandle, *, discard: bool = False) -> None:
        live = self._live.get(handle.record_id)
        if live is not None:
            await self._dispose(live, discard=discard)
        else:
            if discard:
                self.store.delete(handle.record_id)
            else:
                self.store.mark_closed(handle.record_id)
        handle.status = "closed"
        handle.pid = None

    async def _dispose(self, live: _Live, *, discard: bool) -> None:
        if live.closing:
            return
        live.closing = True
        h = live.handle
        turn = live.turn
        if turn is not None and not turn.finished:
            await self._cancel_turn(live, turn, "session closed")
            if turn.runner is not None:
                with contextlib.suppress(BaseException):
                    await asyncio.wait({turn.runner}, timeout=self.kill_grace_s + 3.0)
        if live.proc.alive and h.acp_session_id:
            sess_caps = (h.agent_capabilities or {}).get("sessionCapabilities") or {}
            if isinstance(sess_caps, dict) and sess_caps.get("close") is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        live.conn.close_session(session_id=h.acp_session_id), 1.0
                    )
        rc = await acp_process.close_gracefully(
            live.proc, wait_s=self.close_wait_s, grace_s=self.kill_grace_s
        )
        with contextlib.suppress(Exception):
            await asyncio.wait_for(live.conn.close(), 1.0)
        self._live.pop(h.record_id, None)
        if self._by_key.get(live.session_key) == h.record_id:
            self._by_key.pop(live.session_key, None)
        h.status = "closed"
        h.pid = None
        h.lease_id = None
        if discard:
            self.store.delete(h.record_id)
        else:
            self.store.save(h)
        _log.info(
            "acp.runtime.session_closed",
            agent_id=h.agent_id,
            record_id=h.record_id,
            returncode=rc,
            discard=discard,
        )

    async def status(self, handle: AcpSessionHandle) -> AcpSessionHandle:
        live = self._live.get(handle.record_id)
        if live is not None:
            h = live.handle
            if live.proc.alive:
                h.pid = live.proc.pid
                h.status = "busy" if live.turn is not None else "idle"
            else:
                h.pid = None
                h.status = "unhealthy"
            return h
        rec = self.store.load(handle.record_id)
        h = rec.handle if rec else handle
        if h.status != "closed":
            # Not running in this runtime: resumable via ensure_session, not live.
            h.status = "unhealthy"
            h.pid = None
        return h

    # -- modes / config options -----------------------------------------

    def _live_or_raise(self, handle: AcpSessionHandle) -> _Live:
        live = self._live.get(handle.record_id)
        if live is None:
            raise AcpError(
                f"ACP session {handle.record_id} is not running", code="ACP_SESSION_NOT_RUNNING"
            )
        return live

    async def set_mode(self, handle: AcpSessionHandle, mode_id: str) -> None:
        await self._set_mode(self._live_or_raise(handle), mode_id, persist=True)

    async def set_config_option(self, handle: AcpSessionHandle, key: str, value: Any) -> None:
        await self._set_config_option(self._live_or_raise(handle), key, value, persist=True)

    async def _set_mode(self, live: _Live, mode_id: str, *, persist: bool) -> None:
        h = live.handle
        ids = {str(m.get("id")) for m in h.available_modes}
        if mode_id not in ids:
            raise AcpError(
                f"ACP agent {h.agent_id!r} does not offer mode {mode_id!r}",
                code="ACP_MODE_UNAVAILABLE",
                available=sorted(ids),
            )
        try:
            await live.conn.set_session_mode(session_id=h.acp_session_id or "", mode_id=mode_id)
        except Exception as exc:
            raise AcpError(f"session/set_mode failed: {exc}", code="ACP_MODE_FAILED") from exc
        h.current_mode_id = mode_id
        live.client.current_mode_id = mode_id
        if persist:
            self.store.save(h)

    async def _set_config_option(self, live: _Live, key: str, value: Any, *, persist: bool) -> None:
        h = live.handle
        opt = _find_option(h.config_options, key)
        if opt is None:
            raise AcpError(
                f"ACP agent {h.agent_id!r} does not offer config option {key!r}",
                code="ACP_CONFIG_OPTION_UNAVAILABLE",
                available=[o.get("id") for o in h.config_options],
            )
        coerced: str | bool = value if isinstance(value, bool) else str(value)
        try:
            resp = await live.conn.set_config_option(
                config_id=key, session_id=h.acp_session_id or "", value=coerced
            )
        except Exception as exc:
            raise AcpError(
                f"session/set_config_option failed: {exc}", code="ACP_CONFIG_OPTION_FAILED"
            ) from exc
        new_opts = _config_from(resp) if resp is not None else []
        if new_opts:
            h.config_options = new_opts
        else:
            opt["currentValue"] = coerced
        if opt.get("category") == "mode" and isinstance(coerced, str):
            h.current_mode_id = coerced
        if persist:
            self.store.save(h)

    # -- doctor / housekeeping ------------------------------------------

    async def doctor(self, agent_id: str, *, probe: bool = False) -> dict[str, Any]:
        aid = normalize_agent_id(agent_id)
        out: dict[str, Any] = {"agent_id": aid}
        try:
            resolved = self.resolve(aid)
        except AcpSessionInitError as exc:
            out.update(
                argv=[],
                source="unknown",
                missing_commands=[],
                adapter_installed=False,
                error=str(exc),
            )
            return out
        argv0 = resolved.argv[0] if resolved.argv else ""
        launcher = os.path.basename(argv0) in _LAUNCHERS
        out.update(
            argv=list(resolved.argv),
            source=resolved.source,
            missing_commands=list(resolved.missing_commands),
            adapter_installed=(
                resolved.source in ("installed", "override")
                or (not launcher and self._which(argv0) is not None)
            ),
        )
        if not probe:
            return out
        try:
            info = await asyncio.wait_for(self._probe(resolved), 30.0)
            out["probe"] = "ok"
            out.update(info)
        except Exception as exc:
            out["probe"] = "error"
            out["error"] = str(exc) or type(exc).__name__
        return out

    async def _probe(self, resolved: ResolvedLaunch) -> dict[str, Any]:
        import acp
        from acp.schema import Implementation

        from agentos import __version__

        self._check_launchable(resolved)
        lease_id = uuid.uuid4().hex[:16]
        cwd = os.getcwd()
        env = acp_process.harness_env(
            lease_id=lease_id, base=self._base_env, extra=self._override_env(resolved.agent_id)
        )
        proc = await acp_process.launch_harness(
            resolved,
            cwd=cwd,
            env=env,
            state_dir=self.state_dir,
            lease_id=lease_id,
            session_key="acp:probe",
            gateway_instance_id=self.gateway_instance_id,
        )
        client = AgentOSAcpClient(agent_id=resolved.agent_id, cwd=cwd, session_key="acp:probe")
        conn = acp.connect_to_agent(
            cast("acp.Client", client), proc.stdin, proc.stdout, use_unstable_protocol=True
        )
        try:
            init = await conn.initialize(
                protocol_version=acp.PROTOCOL_VERSION,
                client_capabilities=self._client_capabilities(),
                client_info=Implementation(name="agentos", title="AgentOS", version=__version__),
            )
            info: dict[str, Any] = {"protocol_version": init.protocol_version}
            if init.agent_info is not None:
                info["version"] = init.agent_info.version
                info["agent_name"] = init.agent_info.name
            caps = init.agent_capabilities
            if caps is not None:
                info["load_session"] = bool(caps.load_session)
            return info
        finally:
            await acp_process.close_gracefully(proc, wait_s=0.5, grace_s=self.kill_grace_s)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(conn.close(), 1.0)

    async def reap_stale(self) -> dict[str, Any]:
        """Boot-time cleanup of harness processes left by an earlier gateway."""
        return await acp_process.reap_stale_leases(
            self.state_dir,
            current_instance_id=self.gateway_instance_id,
            grace_s=self.kill_grace_s,
        )

    def live_handles(self) -> list[AcpSessionHandle]:
        return [live.handle for live in self._live.values()]

    async def aclose(self) -> None:
        """Gateway shutdown: close every live session (records stay resumable)."""
        for live in list(self._live.values()):
            with contextlib.suppress(Exception):
                await self._dispose(live, discard=False)
        for task in list(self._bg):
            task.cancel()
