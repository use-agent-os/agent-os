"""WebSocket client for connecting to AgentOS gateway daemon."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

from agentos.gateway_client import (
    RECONNECT_INITIAL_DELAY_S,
    RECONNECT_MAX_DELAY_S,
    WS_CLOSE_SEQ_GAP,
    EventSeqTracker,
    GatewayHandshakeError,
    SessionSubscriptions,
    build_connect_params,
    close_websocket,
    open_websocket,
    reconnect_delays,
)
from agentos.session.keys import canonicalize_session_key
from agentos.session.terminal_reply import build_terminal_reply, sanitize_agent_error


class GatewayRPCError(Exception):
    """Operator-facing RPC failure raised by GatewayClient."""

    def __init__(
        self,
        method: str,
        *,
        code: str | None = None,
        message: str = "RPC failed",
        data: dict | None = None,
    ) -> None:
        self.method = method
        self.code = code
        self.message = message
        self.data = data
        super().__init__(self.__str__())

    def __str__(self) -> str:
        code = f"{self.code}: " if self.code else ""
        return f"{self.method} failed: {code}{self.message}"


def gateway_base_is_local(base_url: str | None) -> bool:
    """Return True for loopback/same-machine gateway origins.

    Unknown, unparsable, or non-loopback hosts fail closed so CLI `/path`
    cannot confuse a remote gateway with files on the operator's machine.
    """

    if not base_url:
        return False
    parsed = urlparse(base_url)
    host = parsed.hostname
    if not host:
        return False
    normalized = host.strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


AGENT_TOKEN_ENV = "AGENTOS_AGENT_TOKEN"
OPERATOR_SECRET_FILE_ENV = "AGENTOS_OPERATOR_SECRET_FILE"

#: Variables the gateway sets in the environment of a process an agent runs:
#: the minted token and the session markers (``tools/builtin/shell.py``,
#: ``scheduler/scripts.py``) and the ACP harness lease (``acp/process.py``).
#: *Present* is enough, even empty: ``AGENTOS_AGENT_TOKEN= agentos …`` is
#: the same as unsetting it.
AGENT_SESSION_ENV: tuple[str, ...] = (
    AGENT_TOKEN_ENV,
    "AGENTOS_SESSION_KEY",
    "AGENTOS_AGENT",
    "AGENTOS_ACP_LEASE",
)


def _operator_secret_path() -> Path:
    override = os.environ.get(OPERATOR_SECRET_FILE_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    from agentos.gateway.agent_surface import default_operator_secret_path

    return default_operator_secret_path()


def read_operator_secret() -> str | None:
    """The operator secret the local gateway wrote at boot, if readable.

    ``AGENTOS_OPERATOR_SECRET_FILE`` overrides the path (tests). Unreadable,
    missing or empty means ``None``: the connection is then an agent's, which
    fails safe.
    """
    try:
        text = _operator_secret_path().read_text(encoding="utf-8")
    except OSError:
        return None
    secret = text.strip()
    return secret or None


def _gateway_pid_candidates() -> set[int]:
    """Pids of the gateways that own the default state directory, as recorded."""
    from agentos.application.gateway_pidlock import PID_FILENAME, read_pid_from_path
    from agentos.paths import default_agentos_home

    pid = read_pid_from_path(default_agentos_home() / "state" / PID_FILENAME)
    return {pid} if pid else set()


def _looks_like_gateway(argv: list[str]) -> bool:
    """``agentos gateway run`` in any of its spellings (console script, ``-m``, desktop)."""
    names = [Path(arg).name for arg in argv]
    for index, name in enumerate(names[:-1]):
        if name == "gateway" and names[index + 1] == "run":
            return any("agentos" in arg for arg in argv) or names[0] == "gateway"
    return False


def _ancestors() -> list[tuple[int, list[str]]]:
    """This process's ancestors as ``(pid, argv)``, nearest first. Best effort."""
    chain: list[tuple[int, list[str]]] = []
    if os.name != "posix":
        return chain
    proc = Path("/proc")
    if (proc / "self" / "stat").exists():
        pid = os.getppid()
        for _ in range(64):
            if pid <= 1:
                break
            try:
                stat = (proc / str(pid) / "stat").read_text()
                raw = (proc / str(pid) / "cmdline").read_bytes()
            except OSError:
                break
            chain.append(
                (pid, [part.decode(errors="replace") for part in raw.split(b"\0") if part])
            )
            fields = stat.rsplit(")", 1)[-1].split()
            pid = int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else 0
        return chain
    # macOS and other POSIX systems without /proc: one ``ps`` for the table.
    import subprocess

    try:
        listing = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return chain
    table: dict[int, tuple[int, list[str]]] = {}
    for line in listing.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            command = parts[2].split() if len(parts) > 2 else []
            table[int(parts[0])] = (int(parts[1]), command)
    pid = os.getppid()
    for _ in range(64):
        if pid <= 1 or pid not in table:
            break
        parent, command = table[pid]
        chain.append((pid, command))
        pid = parent
    return chain


def agent_session_marker() -> str | None:
    """Why this process belongs to an agent's session, or ``None``.

    Two independent signals, so unsetting one variable is not enough:

    * any of :data:`AGENT_SESSION_ENV` is present in the environment;
    * a running gateway is among this process's ancestors. The gateway spawns
      every agent shell, cron script, ``execute_code`` run and ACP harness;
      the operator's terminal and the desktop app are never its children.
      ``env -u``/``env -i`` cannot change who the parent is.

    The gateway checks the same thing from its side (``gateway.peer_process``,
    ``gateway.agent_surface``), so this is a second wall, not the only one: a
    client an agent wrote itself never runs this code.
    """
    for name in AGENT_SESSION_ENV:
        if name in os.environ:
            return name
    try:
        gateway_pids = _gateway_pid_candidates()
        for pid, argv in _ancestors():
            if pid in gateway_pids or _looks_like_gateway(argv):
                return "gateway-ancestor"
    except Exception:  # noqa: BLE001 - detection must never break the CLI
        return None
    return None


def agent_surface_auth(url: str | None) -> dict[str, str]:
    """What this process presents so the gateway can tell agent from operator.

    Exactly one of these, never both (``gateway.agent_surface``):

    * ``agentToken`` when ``AGENTOS_AGENT_TOKEN`` is set. The gateway planted
      it in an agent's shell; presenting it binds the connection to that chat.
      It wins even if the operator file happens to be readable, so an agent
      that can somehow see the file still cannot use it from here.
    * ``operatorSecret`` when the gateway is local, the file it wrote at boot
      is readable, and this process is not an agent's
      (:func:`agent_session_marker`). That is what makes the operator's own
      terminal the operator.
    * nothing otherwise — a remote gateway, no file, or an agent's process
      that dropped its token. The gateway then binds the connection as
      ``unbound`` (or the agent's, if it can tell) and applies the agent's
      rules, so neither ``env -u AGENTOS_AGENT_TOKEN`` nor a detached process
      an agent left behind gains anything by connecting.
    """
    agent_token = os.environ.get(AGENT_TOKEN_ENV, "").strip()
    if agent_token:
        return {"agentToken": agent_token}
    if not gateway_base_is_local(url):
        return {}
    if agent_session_marker() is not None:
        return {}
    secret = read_operator_secret()
    if secret:
        return {"operatorSecret": secret}
    return {}


def _frame_belongs_to_turn(
    payload: dict[str, Any],
    accepted_keys: set[str],
    stream_watermark: int | None,
) -> bool:
    """Return whether a session event frame may belong to the turn in flight.

    The gateway stamps every ``session.event.*`` payload with ``session_key``
    and a per-session monotonic ``stream_seq``. A frame tagged with another
    session, or positioned at/below the stream watermark observed when this
    turn subscribed, predates the turn. Frames without ``stream_seq``
    (``task.*`` events) or without either field (older servers) are only
    checked as far as their fields allow.
    """
    frame_key = payload.get("session_key")
    if isinstance(frame_key, str) and frame_key and frame_key not in accepted_keys:
        return False
    frame_seq = payload.get("stream_seq")
    if (
        stream_watermark is not None
        and isinstance(frame_seq, int)
        and not isinstance(frame_seq, bool)
        and frame_seq <= stream_watermark
    ):
        return False
    return True


_CONNECTION_LOST = (
    "Gateway connection lost; restart chat or reconnect before sending another command."
)
#: How long the CLI client keeps reconnecting after a drop before giving up.
CLI_RECONNECT_BUDGET_S = 60.0
#: How long ``GatewayClient.connect`` keeps retrying a gateway that is booting.
STARTUP_WAIT_S = 30.0


class GatewayClient:
    """WebSocket client for connecting to AgentOS gateway daemon.

    After :meth:`connect` succeeds the client reconnects by itself when the
    socket drops or an event-sequence gap shows frames were lost: requests in
    flight fail, later calls wait for the new connection, and each session
    subscription is renewed from its last ``stream_seq`` so a turn being
    streamed resumes. A wrong token or a protocol mismatch ends it for good.
    """

    def __init__(self, *, auto_reconnect: bool = True) -> None:
        self._ws: Any = None
        self._recv_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._pending: dict[str, asyncio.Future[dict]] = {}
        self._listener_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._heartbeat_interval: float = 48.0
        self._connection_error: ConnectionError | None = None
        self._closing = False
        self._http_base: str | None = None
        self._auth_token: str | None = None
        self._server_version: str | None = None
        self._url: str | None = None
        self._seq = EventSeqTracker()
        self._subscriptions = SessionSubscriptions()
        self._ready: asyncio.Event | None = None
        self.auto_reconnect = auto_reconnect
        self.reconnect_initial_delay_s = RECONNECT_INITIAL_DELAY_S
        self.reconnect_max_delay_s = RECONNECT_MAX_DELAY_S
        #: How long to keep reconnecting before failing for good, so a caller
        #: waiting on the gateway (``agentos chat``) is never stuck for ever.
        self.reconnect_budget_s: float | None = CLI_RECONNECT_BUDGET_S
        #: Told about outages as they happen ("reconnecting", "reconnected",
        #: "lost"), e.g. to print a line in ``agentos chat``.
        self.on_connection_status: Callable[[str, str], None] | None = None
        #: Successful reconnects since :meth:`connect`.
        self.reconnects = 0

    async def connect(
        self,
        url: str = "ws://localhost:18791/ws",
        *,
        token: str | None = None,
        startup_wait_s: float = STARTUP_WAIT_S,
    ) -> None:
        """Connect to gateway. Raises SystemExit with friendly message on failure.

        A gateway that is still booting refuses the handshake with a retryable
        ``UNAVAILABLE`` / ``STARTUP_SIDECARS``; the first connect retries it for
        up to ``startup_wait_s`` (``0`` fails at once, which status probes want).
        """
        has_existing_connection = (
            self._ws is not None
            or self._listener_task is not None
            or self._heartbeat_task is not None
            or self._reconnect_task is not None
        )
        if has_existing_connection:
            await self.close()
        self._closing = False
        self._connection_error = None
        try:
            import websockets  # noqa: F401
        except ImportError:
            raise SystemExit("websockets package is required: uv pip install websockets")

        # Cache an HTTP base derived from the WS URL for the bridge upload
        # endpoint. ws://host:port/ws -> http://host:port; same scheme swap
        # for wss:// -> https://.
        if url.startswith("ws://"):
            base = "http://" + url[len("ws://") :]
        elif url.startswith("wss://"):
            base = "https://" + url[len("wss://") :]
        else:
            base = url
        if base.endswith("/ws"):
            base = base[: -len("/ws")]
        self._http_base = base.rstrip("/")
        self._auth_token = token
        self._url = url
        self._subscriptions = SessionSubscriptions()
        self.reconnects = 0

        try:
            await self._open_waiting_for_startup(url, startup_wait_s)
        except _SocketOpenError as exc:
            raise SystemExit(
                f"Cannot connect to AgentOS gateway at {url}\n"
                f"Is the gateway running? Start it with: agentos gateway run\n"
                f"Error: {exc.__cause__}"
            ) from None
        except GatewayHandshakeError as exc:
            raise SystemExit(f"Handshake failed: {exc.frame}") from None
        except _UnexpectedFrameError as exc:
            raise SystemExit(f"Unexpected handshake frame: {exc.frame}") from None

    async def _open_waiting_for_startup(self, url: str, startup_wait_s: float) -> None:
        """:meth:`_open`, retried while the gateway answers ``STARTUP_SIDECARS``."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, startup_wait_s)
        while True:
            try:
                await self._open()
                return
            except GatewayHandshakeError as exc:
                if exc.detail_code != "STARTUP_SIDECARS":
                    raise
                raw = exc.details.get("retry_after_ms") or 1000
                try:
                    delay = min(max(int(raw), 100), 2000) / 1000
                except (TypeError, ValueError):
                    delay = 1.0
                if loop.time() + delay > deadline:
                    raise SystemExit(
                        f"The AgentOS gateway at {url} is still starting; retry in a moment."
                    ) from None
                await asyncio.sleep(delay)

    async def _open(self) -> None:
        """One connection attempt: socket, handshake, listener, heartbeat."""
        assert self._url is not None
        try:
            ws = await open_websocket(self._url)
        except Exception as exc:
            raise _SocketOpenError() from exc

        try:
            # Wait for connect.challenge
            raw = await ws.recv()
            challenge = json.loads(raw)
            if challenge.get("type") != "event" or challenge.get("event") != "connect.challenge":
                raise _UnexpectedFrameError(challenge)

            # Send connect request
            req_id = str(uuid.uuid4())
            auth: dict[str, str] = {}
            if self._auth_token:
                auth["token"] = self._auth_token
            auth.update(agent_surface_auth(self._url))
            params = build_connect_params(client_id="agentos-cli", mode="cli", auth=auth or None)
            await ws.send(
                json.dumps(
                    {
                        "type": "req",
                        "id": req_id,
                        "method": "connect",
                        "params": params,
                    }
                )
            )

            # Wait for hello-ok
            raw = await ws.recv()
            hello = json.loads(raw)
            if hello.get("type") != "hello-ok":
                raise GatewayHandshakeError.from_response(hello)
        except BaseException:
            await close_websocket(ws)
            raise

        self._ws = ws
        self._seq.reset()
        # Capture the gateway's reported version from the handshake so callers
        # can detect version skew without a second RPC round-trip.
        server_info = hello.get("server") if isinstance(hello.get("server"), dict) else {}
        version = server_info.get("version")
        self._server_version = version if isinstance(version, str) and version else None
        policy = hello.get("policy") if isinstance(hello.get("policy"), dict) else {}
        self._heartbeat_interval = _heartbeat_interval_from_policy(policy)

        # Start background listener and application-level keepalive.
        self._listener_task = asyncio.create_task(self._listen(ws))
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))
        self._ready_event().set()

    def _ready_event(self) -> asyncio.Event:
        if self._ready is None:
            self._ready = asyncio.Event()
        return self._ready

    def set_auth_token(self, token: str | None) -> None:
        """Cache a bearer token used for HTTP-side requests (e.g. uploads)."""

        self._auth_token = token

    @property
    def is_local_gateway(self) -> bool:
        """True when the connected gateway URL is loopback/same-machine."""

        return gateway_base_is_local(self._http_base)

    @property
    def server_version(self) -> str | None:
        """Gateway version reported in the connect handshake, if any."""

        return self._server_version

    async def upload_file(
        self,
        path: Any,
        mime: str,
        name: str,
    ) -> str:
        """POST a file to /api/v1/files/upload and return the file_uuid.

        The CLI keeps its WebSocket connection for RPC; the bridge upload
        is a sibling HTTP request to the same gateway origin (the
        ``/api/v1/files/upload`` endpoint). Multipart only — query-token
        auth is rejected by the
        endpoint, so we always send the Authorization header when a token
        is configured. When the upload fails (network, 4xx, 5xx) the
        error is raised so the caller can surface a clear message.
        """

        if self._http_base is None:
            raise ConnectionError("GatewayClient has no HTTP base URL — call connect() first")
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise SystemExit("httpx package is required: uv pip install httpx") from exc

        from pathlib import Path as _Path

        local = _Path(path)
        url = f"{self._http_base}/api/v1/files/upload"
        headers: dict[str, str] = {}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"

        with local.open("rb") as fh:
            files = {"file": (name, fh, mime)}
            data = {"mime": mime}
            async with httpx.AsyncClient(timeout=60.0) as http:
                response = await http.post(url, headers=headers, files=files, data=data)

        if response.status_code != 200:
            raise ConnectionError(
                f"upload {url} failed: HTTP {response.status_code} {response.text[:200]}"
            )
        body = response.json()
        if not isinstance(body, dict) or "file_uuid" not in body:
            raise ConnectionError(f"upload returned malformed body: {body!r}")
        return str(body["file_uuid"])

    async def _listen(self, ws: Any | None = None) -> None:
        """Read frames and route to pending futures or the event queue."""
        sock = self._ws if ws is None else ws
        lost: BaseException = ConnectionError("WebSocket connection closed")
        try:
            async for raw in sock:
                frame = json.loads(raw)
                frame_type = frame.get("type")
                if frame_type == "res":
                    fut = self._pending.pop(frame["id"], None)
                    if fut and not fut.done():
                        fut.set_result(frame)
                elif frame_type == "event":
                    gap = self._seq.observe(frame)
                    if gap is not None:
                        # Frames were lost: drop this socket and resubscribe
                        # from the last stream position on a fresh one.
                        lost = ConnectionError(
                            f"event sequence gap: expected {gap[0]}, got {gap[1]}"
                        )
                        await close_websocket(sock, WS_CLOSE_SEQ_GAP, "seq gap")
                        break
                    self._subscriptions.note_event(frame)
                    await self._recv_queue.put(frame)
                elif frame_type == "pong":
                    continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            lost = exc
        if not self._closing:
            # Fail all pending requests so callers don't hang forever
            self._connection_lost(lost, ws=sock)

    async def _heartbeat_loop(self, ws: Any | None = None) -> None:
        """Send application-level text pings so server receive_text() stays active."""
        heartbeat_ws = self._ws if ws is None else ws
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval)
                if self._closing or heartbeat_ws is not self._ws:
                    return
                await self._send_ping(heartbeat_ws)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._connection_lost(exc, ws=heartbeat_ws)

    async def _send_ping(self, ws: Any | None = None) -> None:
        target = self._ws if ws is None else ws
        if target is None:
            raise ConnectionError("WebSocket is not connected")
        await target.send('{"type":"ping"}')

    async def _call(self, method: str, params: dict | None = None) -> Any:
        """Send a JSON-RPC request and await its response."""
        if self._connection_error is not None:
            raise self._connection_error
        if self._ws is None:
            await self._await_reconnect()
        req_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict] = loop.create_future()
        self._pending[req_id] = fut
        try:
            await self._ws.send(
                json.dumps({"type": "req", "id": req_id, "method": method, "params": params})
            )
        except asyncio.CancelledError:
            self._pending.pop(req_id, None)
            raise
        except Exception as exc:
            self._pending.pop(req_id, None)
            err = self._connection_lost(exc)
            raise err from exc
        res = await fut
        if not res.get("ok"):
            err = res.get("error", {})
            # The gateway's ErrorShape names the payload ``details``; ``data``
            # is the older spelling some callers and doubles still send.
            data = err.get("details")
            if not isinstance(data, dict):
                data = err.get("data")
            raise GatewayRPCError(
                method,
                code=err.get("code"),
                message=err.get("message") or "RPC failed",
                data=data if isinstance(data, dict) else None,
            )
        payload = res.get("payload")
        self._subscriptions.note_call(method, params, payload)
        return {} if payload is None else payload

    async def _await_reconnect(self) -> None:
        task = self._reconnect_task
        if task is None or task.done():
            if self._connection_error is not None:
                raise self._connection_error
            raise ConnectionError(_CONNECTION_LOST)
        await self._ready_event().wait()
        if self._connection_error is not None:
            raise self._connection_error
        if self._ws is None:
            raise ConnectionError(_CONNECTION_LOST)

    async def _call_idempotent(self, method: str, params: dict[str, Any]) -> Any:
        """:meth:`_call` for a request carrying an ``idempotency_key``.

        If the socket drops before the answer arrives, the same request is
        sent once more after the reconnect: a gateway that already accepted
        it answers with the turn it started instead of starting a second one.
        """
        try:
            return await self._call(method, params)
        except GatewayRPCError:
            raise
        except ConnectionError:
            if not self.auto_reconnect or self._connection_error is not None:
                raise
            # _call() waits for the reconnect before sending.
            return await self._call(method, params)

    async def call(self, method: str, params: dict | None = None) -> Any:
        """Public thin wrapper for CLI commands that intentionally use RPC names."""

        return await self._call(method, params)

    async def create_session(
        self,
        agent_id: str = "main",
        model: str | None = None,
        display_name: str | None = None,
        project_id: str | None = None,
    ) -> str:
        """Create a new session, return session key."""
        params: dict[str, Any] = {"agentId": agent_id, "kind": "cli"}
        if model is not None:
            params["model"] = model
        if display_name:
            params["displayName"] = display_name
        if project_id:
            params["projectId"] = project_id
        result = await self._call("sessions.create", params)
        return cast(str, result["key"])

    async def list_sessions(self, limit: int = 50) -> dict[str, Any]:
        return cast(dict[str, Any], await self._call("sessions.list", {"limit": limit}))

    async def preview_sessions(
        self,
        keys: list[str] | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if keys is not None:
            params["keys"] = keys
        return cast(dict[str, Any], await self._call("sessions.preview", params))

    async def resolve_session(self, key: str) -> dict[str, Any]:
        return cast(dict[str, Any], await self._call("sessions.resolve", {"key": key}))

    async def reset_session(self, key: str) -> dict[str, Any]:
        return cast(dict[str, Any], await self._call("sessions.reset", {"key": key}))

    async def compact_session(self, key: str) -> dict[str, Any]:
        return cast(dict[str, Any], await self._call("sessions.contextCompact", {"key": key}))

    async def delete_sessions(self, keys: list[str]) -> dict[str, Any]:
        return cast(dict[str, Any], await self._call("sessions.delete", {"keys": keys}))

    async def session_history(self, session_key: str, limit: int = 1000) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self._call("chat.history", {"sessionKey": session_key, "limit": limit}),
        )

    async def abort_session(self, key: str) -> dict[str, Any]:
        """Stop the session's run (and its child runs); waits for the end.

        A cascade that did not finish within the gateway's bound is reported,
        not raised: the session's own run is stopped either way, and the
        caller (Ctrl-C, ``/abort``) has nothing better to do than say so.
        """
        try:
            return cast(dict[str, Any], await self._call("sessions.abort", {"key": key}))
        except GatewayRPCError as exc:
            details = exc.data or {}
            # ErrorDetailCode.ABORT_INCOMPLETE; the gateway package is too
            # heavy to import into the CLI client for one string.
            if details.get("code") != "ABORT_INCOMPLETE":
                raise
            return {
                "key": key,
                "aborted": details.get("aborted") or [],
                "children_aborted": details.get("children_aborted") or [],
                "still_running": details.get("still_running") or [],
                "incomplete": True,
            }

    async def rename_session(self, key: str, name: str | None) -> dict[str, Any]:
        """Set a session's display name; an empty/``None`` name clears it."""
        return cast(
            dict[str, Any],
            await self._call("sessions.rename", {"key": key, "name": name}),
        )

    async def patch_session(self, key: str, **fields: Any) -> dict[str, Any]:
        params: dict[str, Any] = {"key": key, **fields}
        return cast(dict[str, Any], await self._call("sessions.patch", params))

    async def create_project(
        self,
        name: str,
        knowledge: str = "",
        agent_id: str = "main",
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self._call(
                "projects.create",
                {"agentId": agent_id, "name": name, "knowledge": knowledge},
            ),
        )

    async def list_projects(self, agent_id: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if agent_id:
            params["agentId"] = agent_id
        return cast(dict[str, Any], await self._call("projects.list", params))

    async def get_project(self, project_id: str) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self._call("projects.get", {"projectId": project_id}),
        )

    async def update_project(
        self,
        project_id: str,
        name: str | None = None,
        knowledge: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"projectId": project_id}
        if name is not None:
            params["name"] = name
        if knowledge is not None:
            params["knowledge"] = knowledge
        return cast(dict[str, Any], await self._call("projects.update", params))

    async def delete_project(self, project_id: str) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self._call("projects.delete", {"projectId": project_id}),
        )

    async def move_session_to_project(self, key: str, project_id: str | None) -> dict[str, Any]:
        """Move a session into a project; ``None`` detaches it."""
        return await self.patch_session(key, projectId=project_id)

    async def list_models(
        self, provider: str | None = None, capabilities: list[str] | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if provider:
            params["provider"] = provider
        if capabilities:
            params["capabilities"] = capabilities
        result = await self._call("models.list", params)
        if isinstance(result, list):
            return cast(list[dict[str, Any]], result)
        return cast(list[dict[str, Any]], list(result.get("models", [])))

    async def usage_status(self) -> dict[str, Any]:
        return cast(dict[str, Any], await self._call("usage.status", {}))

    async def usage_cost(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return cast(dict[str, Any], await self._call("usage.cost", params or {}))

    async def diagnostics_status(self) -> dict[str, Any]:
        return cast(dict[str, Any], await self._call("diagnostics.status", {}))

    async def diagnostics_set(self, *, enabled: bool, raw: bool = False) -> dict[str, Any]:
        params: dict[str, Any] = {"enabled": enabled}
        if enabled:
            params["raw"] = raw
        return cast(dict[str, Any], await self._call("diagnostics.set", params))

    async def get_config(self, path: str | None = None) -> Any:
        params = {"path": path} if path else None
        return await self._call("config.get", params)

    async def patch_config_safe(self, patches: dict[str, Any]) -> dict[str, Any]:
        result = await self._call("config.patch.safe", {"patches": patches})
        return result if isinstance(result, dict) else {}

    async def forget_approvals(self, target: str | None = None) -> dict[str, Any]:
        """Wipe cached intent approvals on the server.

        ``target`` selects a specific path/command; omit to clear all.
        Returns the scope reported by the server.
        """
        params: dict[str, Any] = {}
        if target:
            params["target"] = target
        return cast(dict[str, Any], await self._call("exec.approval.forget", params))

    async def approvals_snapshot(self) -> dict[str, Any]:
        """Return current approval mode + cache contents (diagnostic)."""
        return cast(dict[str, Any], await self._call("exec.approval.snapshot", {}))

    async def set_approval_mode(self, mode: str) -> dict[str, Any]:
        """Set the global approval queue mode (prompt / auto-approve / auto-deny)."""
        return cast(dict[str, Any], await self._call("exec.approvals.set", {"mode": mode}))

    async def resolve_approval(
        self,
        approval_id: str,
        approved: bool,
        *,
        allow_always: bool = False,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Approve or deny a pending approval by id.

        ``allow_always`` populates the session-lifetime intent cache so the
        same destructive intent doesn't prompt again. ``payload`` carries
        answers for an ACP elicitation form (``{"content": {...}}``).
        """
        params: dict[str, Any] = {
            "id": approval_id,
            "approved": approved,
            "allowAlways": allow_always,
        }
        if payload is not None:
            params["payload"] = payload
        return cast(dict[str, Any], await self._call("exec.approval.resolve", params))

    async def send_message(
        self,
        session_key: str,
        message: str,
        attachments: list[dict] | None = None,
        elevated: str | None = None,
    ) -> AsyncIterator[dict]:
        """Send message and yield session events until done.

        ``elevated`` — None (use configured default), "off" (sandboxed),
        "on" (host exec with approval), "bypass" (host exec, auto-approve,
        sensitive paths still blocked), or "full" (host exec, auto-approve,
        sensitive paths bypassed).
        """
        # Anything already queued belongs to an earlier turn -- typically the
        # ``done(reason="aborted")`` and trailing deltas of a Ctrl-C'd turn
        # whose generator was abandoned. Nothing for *this* turn can exist
        # before it is sent, so drop it all rather than let a stale terminal
        # end the new turn on its first frame.
        self._drain_recv_queue()

        # Subscribe to message events for this session. The response carries
        # the canonical session key and the stream position at this moment;
        # frames tagged with another key, or at/below that position, are not
        # ours (late leftovers of the previous turn, or another session this
        # connection is still subscribed to after ``/new`` / ``/resume``).
        subscribed = await self._call("sessions.messages.subscribe", {"key": session_key})
        accepted_keys = {session_key, canonicalize_session_key(session_key)}
        canonical_key = subscribed.get("key") if isinstance(subscribed, dict) else None
        if isinstance(canonical_key, str) and canonical_key:
            accepted_keys.add(canonical_key)
        stream_watermark = (
            subscribed.get("current_stream_seq") if isinstance(subscribed, dict) else None
        )
        if not isinstance(stream_watermark, int) or isinstance(stream_watermark, bool):
            stream_watermark = None

        params: dict[str, Any] = {
            "key": session_key,
            "message": message,
            "attachments": attachments or [],
            # One per message: a gateway that already accepted it answers a
            # resend with the same turn instead of starting another.
            "idempotency_key": uuid.uuid4().hex,
            "_source": {
                "caller_kind": "cli",
                "channel_kind": "cli",
                "channel_id": "cli:chat",
                "source_kind": "cli",
                "source_name": "chat",
            },
        }
        if elevated in ("on", "bypass", "full"):
            params["_source"]["elevated"] = elevated

        # Send the message (accepted immediately; agent runs async). The
        # response names the task that will run this turn, so a task-runtime
        # terminal for any other task on this session is not ours.
        accepted = await self._call_idempotent("sessions.send", params)
        accepted_task_id = accepted.get("task_id") if isinstance(accepted, dict) else None
        if not isinstance(accepted_task_id, str) or not accepted_task_id:
            accepted_task_id = None

        active_task_groups: set[str] = set()

        # Yield events until session completion, extending the stream while a
        # background subagent group is still waiting for parent synthesis.
        while True:
            frame = await self._recv_queue.get()
            event_name: str = frame.get("event", "")
            payload: dict = frame.get("payload") or {}
            if not _frame_belongs_to_turn(payload, accepted_keys, stream_watermark):
                continue
            if (
                accepted_task_id is not None
                and not active_task_groups
                and (
                    event_name.startswith("task.")
                    or event_name in ("session.event.done", "session.event.error")
                )
                and isinstance(payload.get("task_id"), str)
                and payload["task_id"] != accepted_task_id
            ):
                # ``task.cancelled`` left over from the aborted previous turn,
                # or a cron run's ``task.failed`` / terminal frame on this
                # session: with no task group in flight, only our own task may
                # end the turn.
                continue
            if event_name == "session.event.error":
                payload = _normalize_session_error_payload(payload)
            if task_terminal := _task_terminal_as_session_event(event_name, payload):
                yield task_terminal
                if active_task_groups:
                    continue
                break
            group_id = payload.get("group_id")
            if (
                event_name
                in (
                    "session.event.task_group.waiting",
                    "session.event.task_group.synthesizing",
                )
                and isinstance(group_id, str)
                and group_id
            ):
                active_task_groups.add(group_id)
            elif (
                event_name
                in (
                    "session.event.task_group.done",
                    "session.event.task_group.failed",
                )
                and isinstance(group_id, str)
                and group_id
            ):
                was_active_group = group_id in active_task_groups
                active_task_groups.discard(group_id)
            else:
                was_active_group = False
            yield {"event": event_name, **payload}
            if (
                event_name
                in (
                    "session.event.task_group.done",
                    "session.event.task_group.failed",
                )
                and was_active_group
                and not active_task_groups
            ):
                break
            if event_name in ("session.event.done", "session.event.error"):
                if active_task_groups:
                    continue
                break

    def _drain_recv_queue(self) -> None:
        """Discard every frame currently queued (see ``send_message``)."""
        while True:
            try:
                self._recv_queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def close(self) -> None:
        """Close the WebSocket connection."""
        self._closing = True
        for task in (self._reconnect_task, self._heartbeat_task, self._listener_task):
            if task is None or task is asyncio.current_task():
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()
        self._ws = None
        self._heartbeat_task = None
        self._listener_task = None
        self._reconnect_task = None

    def _connection_lost(self, exc: BaseException, *, ws: Any = None) -> ConnectionError:
        """The socket is gone: reconnect when possible, else fail for good."""
        if ws is not None and ws is not self._ws:
            return ConnectionError(f"{_CONNECTION_LOST} Original error: {exc}")
        can_reconnect = (
            self.auto_reconnect
            and self._url is not None
            and not self._closing
            and self._connection_error is None
        )
        if not can_reconnect:
            return self._mark_connection_failed(exc)
        err = ConnectionError(f"Gateway connection lost (reconnecting). Original error: {exc}")
        self._fail_pending(err)
        self._stop_connection_tasks()
        self._ws = None
        self._ready_event().clear()
        if self._reconnect_task is None or self._reconnect_task.done():
            self._report_status("reconnecting", "Gateway connection lost; reconnecting…")
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())
        return err

    def _report_status(self, state: str, text: str) -> None:
        callback = self.on_connection_status
        if callback is None:
            return
        try:
            callback(state, text)
        except Exception:  # noqa: BLE001 - a status line must never break the client
            pass

    def _give_up(self, exc: BaseException) -> None:
        self._mark_connection_failed(exc)
        self._ready_event().set()
        self._report_status("lost", _CONNECTION_LOST)

    async def _reconnect_loop(self) -> None:
        loop = asyncio.get_running_loop()
        budget = self.reconnect_budget_s
        deadline = None if budget is None else loop.time() + budget
        last_error: BaseException = ConnectionError("gateway unreachable")
        delays = reconnect_delays(
            initial=self.reconnect_initial_delay_s, maximum=self.reconnect_max_delay_s
        )
        for delay in delays:
            if deadline is not None:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    self._give_up(
                        ConnectionError(
                            f"{_CONNECTION_LOST} Original error: no gateway for "
                            f"{budget:g}s ({last_error})"
                        )
                    )
                    return
                delay = min(delay, remaining)
            await asyncio.sleep(delay)
            if self._closing:
                return
            try:
                await self._open()
            except asyncio.CancelledError:
                raise
            except GatewayHandshakeError as exc:
                last_error = exc
                if exc.recoverable:
                    continue
                self._give_up(exc)
                return
            except Exception as exc:  # noqa: BLE001 - network errors: try again later
                last_error = exc.__cause__ or exc
                continue
            self.reconnects += 1
            self._report_status("reconnected", "Reconnected to the gateway.")
            for method, params in self._subscriptions.resubscribe_calls():
                try:
                    await self._call(method, params)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - the next drop or gap retries it
                    pass
            return

    def _fail_pending(self, err: BaseException) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(err)
        self._pending.clear()

    def _stop_connection_tasks(self) -> None:
        current_task = asyncio.current_task()
        for task in (self._heartbeat_task, self._listener_task):
            if task is not None and task is not current_task and not task.done():
                task.cancel()

    def _mark_connection_failed(self, exc: BaseException) -> ConnectionError:
        if isinstance(exc, ConnectionError) and str(exc).startswith("Gateway connection lost"):
            err = exc
        else:
            err = ConnectionError(f"{_CONNECTION_LOST} Original error: {exc}")
        if self._connection_error is None:
            self._connection_error = err
        else:
            err = self._connection_error
        self._fail_pending(err)
        self._stop_connection_tasks()
        return err


class _SocketOpenError(ConnectionError):
    """The TCP/WebSocket connection itself could not be opened."""


class _UnexpectedFrameError(RuntimeError):
    def __init__(self, frame: Any) -> None:
        super().__init__(f"Unexpected handshake frame: {frame}")
        self.frame = frame


def _task_terminal_as_session_event(event_name: str, payload: dict) -> dict[str, Any] | None:
    """Map task-runtime terminal events to chat stream terminal events.

    Gateway chat normally terminates with ``session.event.done`` or
    ``session.event.error``. If the task runtime fails before the agent stream
    starts, older servers may only emit a task terminal event; without this
    fallback the CLI waits forever.
    """
    if event_name == "task.cancelled":
        return {"event": "session.event.done", "reason": "aborted"}

    if event_name not in {"task.failed", "task.timeout", "task.abandoned"}:
        return None

    reason = payload.get("terminal_reason")
    status = event_name.removeprefix("task.")
    message = build_terminal_reply(
        {
            "status": status,
            "terminal_reason": reason,
            **payload,
        }
    )
    return {
        "event": "session.event.error",
        "message": message,
        "code": status,
        **payload,
    }


def _normalize_session_error_payload(payload: dict) -> dict[str, Any]:
    message = payload.get("message")
    error_message = payload.get("error_message")
    raw_message = error_message if isinstance(error_message, str) and error_message else message
    code = payload.get("code")
    code_text = str(code or "").lower()
    raw_text = raw_message if isinstance(raw_message, str) and raw_message else "Agent error"
    is_timeout = "timeout" in code_text or "stream idle" in raw_text.lower()
    terminal_payload = {
        "status": "timeout" if is_timeout else "failed",
        "terminal_reason": payload.get("terminal_reason") or ("timeout" if is_timeout else "error"),
        "error_class": code,
        "error_message": raw_text,
        **payload,
    }
    _, safe_error_message = sanitize_agent_error(
        terminal_payload,
        fallback_error_class=str(code) if code else None,
        fallback_error_message=raw_text,
    )
    terminal_message = build_terminal_reply(terminal_payload)
    return {
        **payload,
        "message": terminal_message,
        "terminal_message": terminal_message,
        "terminal_reason": terminal_payload["terminal_reason"],
        "error_message": safe_error_message,
    }


def _heartbeat_interval_from_policy(policy: dict[str, Any]) -> float:
    raw = policy.get("client_ws_keepalive_timeout_ms", 120_000)
    try:
        keepalive_ms = int(raw)
    except (TypeError, ValueError):
        keepalive_ms = 120_000
    keepalive_s = max(0, keepalive_ms) / 1000.0
    if keepalive_s <= 0.0:
        keepalive_s = 120.0
    minimum = 15.0 if keepalive_s > 15.0 else 0.05
    return max(minimum, keepalive_s * 0.4)
