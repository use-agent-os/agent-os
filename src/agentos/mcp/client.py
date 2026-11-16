"""MCPClient abstract base class and the shared SDK-session base."""

from __future__ import annotations

import asyncio
import contextlib
import json
from abc import ABC, abstractmethod
from contextlib import AsyncExitStack
from typing import Any

import anyio
import structlog

from agentos.mcp.types import MCPServerConfig, MCPToolDef, MCPToolResult

log = structlog.get_logger(__name__)


class MCPClient(ABC):
    """Abstract base class for MCP transport clients."""

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to the MCP server."""

    @abstractmethod
    async def close(self) -> None:
        """Close the connection."""

    @abstractmethod
    async def list_tools(self) -> list[MCPToolDef]:
        """List available tools from the MCP server."""

    @abstractmethod
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPToolResult:
        """Call a tool on the MCP server."""


class MCPSessionClient(MCPClient):
    """Shared plumbing for the two SDK-session-backed HTTP transports.

    Streamable HTTP and legacy SSE differ only in the transport they enter:
    both hold it in an ``AsyncExitStack``, talk to the server through an
    ``mcp.ClientSession``, and unwrap the same result shapes. Only
    :meth:`_open_session` is transport-specific.

    The transport is entered and exited by a task this class owns, not by the
    caller of ``connect()``. Both the SDK transports and ``ClientSession`` are
    built on anyio task groups, and an anyio cancel scope may only be exited by
    the task that entered it — but nothing in AgentOS closes an MCP client from
    the task that opened it. ``discover_and_register`` runs during boot or in an
    RPC handler while ``close_active_clients`` runs from gateway shutdown or a
    later ``mcp.disconnect`` call, so entering the stack inline would make every
    real close raise ``RuntimeError: Attempted to exit cancel scope in a
    different task``, and ``close_active_clients`` swallows that and leaks the
    connection. Here ``connect()`` waits for a runner task to report the session,
    and ``close()`` asks that same task to unwind, from wherever it is called.
    """

    #: Used in the "not connected" error so a caller can tell the two apart.
    transport_label = "MCP"

    def __init__(self, config: MCPServerConfig) -> None:
        super().__init__(config)
        self._session: Any = None
        self._runner: asyncio.Task[None] | None = None
        self._closing: asyncio.Event | None = None
        self._reconnect_lock = asyncio.Lock()
        # Set when a call finds the transport's streams closed under it.
        self._transport_lost = False

    @abstractmethod
    async def _open_session(self, stack: AsyncExitStack) -> Any:
        """Enter this transport's contexts on *stack* and return a live session.

        Cleanup is not this method's job: whatever was entered before a failure
        is unwound by the runner task, in the task that entered it.
        """

    async def connect(self) -> None:
        """Open the transport in a task this client owns, then hand back the session."""
        if self._runner is not None:
            raise RuntimeError(f"{self.transport_label} client is already connected")
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[Any] = loop.create_future()
        closing = asyncio.Event()
        handed_off = asyncio.Event()
        self._closing = closing
        self._runner = loop.create_task(
            self._run(ready, closing, handed_off), name=f"mcp-transport-{self.config.name}"
        )
        try:
            self._session = await ready
        except BaseException:
            # A handshake that failed on its own is already unwinding its stack
            # in the runner; cancelling it there would cut that unwind short at
            # the first ``__aexit__`` that suspends. Only work still inside
            # ``_open_session`` — which nothing will finish once we give up —
            # has to be interrupted.
            await self._teardown(cancel=not handed_off.is_set())
            raise

    async def _run(
        self,
        ready: asyncio.Future[Any],
        closing: asyncio.Event,
        handed_off: asyncio.Event,
    ) -> None:
        """Own the transport stack for the life of the connection.

        ``handed_off`` is set the moment the handshake is over, however it
        ended: past that point the unwind belongs to this task and ``connect()``
        must not cancel it.
        """
        stack = AsyncExitStack()
        try:
            try:
                session = await self._open_session(stack)
            except asyncio.CancelledError:
                handed_off.set()
                # A request that fails inside the SDK transport fails its anyio
                # task group, which cancels the handshake still awaiting a reply.
                # That cancel is not ours: only unwinding the stack, where the
                # group exits, turns it back into the error that caused it.
                try:
                    await stack.aclose()
                except asyncio.CancelledError:
                    pass
                except BaseException as exc:
                    if not ready.done():
                        ready.set_exception(_root_cause(exc))
                    return
                if not ready.done():
                    ready.cancel()
                raise
            except BaseException as exc:
                handed_off.set()
                if not ready.done():
                    ready.set_exception(exc)
                return
            handed_off.set()
            if ready.done():
                # ``connect()`` gave up while the handshake was in flight.
                return
            if closing.is_set():
                # ``close()`` disowned this client mid-handshake. Publishing the
                # session now would hand ``connect()`` a live-looking session
                # that the ``finally`` below is about to tear down.
                ready.set_exception(
                    RuntimeError(f"{self.transport_label} client was closed while connecting")
                )
                return
            ready.set_result(session)
            await closing.wait()
        finally:
            await stack.aclose()

    async def _teardown(self, *, cancel: bool) -> None:
        """Forget the connection and let the runner unwind, swallowing its failure."""
        runner, self._runner = self._runner, None
        self._closing = None
        self._session = None
        if runner is None:
            return
        if cancel:
            runner.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await runner

    async def close(self) -> None:
        """Ask the runner to unwind the transport, which cancels the SDK reader task."""
        runner, self._runner = self._runner, None
        closing, self._closing = self._closing, None
        self._session = None
        if runner is None:
            return
        if closing is not None:
            closing.set()
        try:
            await runner
        except asyncio.CancelledError:
            # The runner ending cancelled is an unwind we asked for; this task
            # being cancelled while waiting on it is the caller's business —
            # absorbing that would let a shutdown deadline pass unnoticed.
            if not runner.cancelled():
                raise

    def _require_session(self) -> Any:
        if self._session is None:
            raise RuntimeError(f"{self.transport_label} client is not connected")
        return self._session

    @property
    def is_alive(self) -> bool:
        """Whether the transport is still up, not just whether ``connect()`` once succeeded.

        One failed HTTP request (an auth error, a 5xx, a dropped socket) fails
        the SDK transport's task group, which closes its streams for good. The
        runner task then ends while ``_session`` still points at the dead
        session, and every later call fails with ``ClosedResourceError``.
        """
        if self._session is None or self._runner is None or self._runner.done():
            return False
        if self._transport_lost:
            return False
        # SDK versions that log a failed request instead of failing the task
        # group close the session's send stream and leave the runner up.
        send_stream = getattr(self._session, "_write_stream", None)
        return not getattr(send_stream, "_closed", False)

    async def _live_session(self) -> Any:
        if self._session is not None and not self.is_alive:
            return await self._reconnect()
        return self._require_session()

    async def _reconnect(self) -> Any:
        """Replace a dead transport with a fresh one, once, however many calls noticed."""
        async with self._reconnect_lock:
            if self.is_alive:
                return self._session
            if self._session is None and self._runner is None:
                # ``close()`` ran while this call waited; it was not a crash.
                raise RuntimeError(f"{self.transport_label} client is not connected")
            log.warning("mcp.transport_reconnecting", server=self.config.name)
            # ``close()`` rather than ``_teardown``: a runner whose streams the
            # SDK closed is still parked on ``closing`` and only that wakes it.
            with contextlib.suppress(Exception):
                await self.close()
            self._transport_lost = False
            await self.connect()
            return self._require_session()

    async def list_tools(self) -> list[MCPToolDef]:
        """List tools from the MCP server."""
        result = await (await self._live_session()).list_tools()
        return [
            MCPToolDef(
                name=tool.name,
                description=tool.description or "",
                input_schema=tool.inputSchema,
            )
            for tool in result.tools
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPToolResult:
        """Call a tool on the MCP server, reconnecting first if the transport died."""
        session = await self._live_session()
        try:
            result = await session.call_tool(name, arguments)
        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
            # Raised while handing the request to a transport that is already
            # gone, so the server never saw it and sending it again is safe.
            self._transport_lost = True
            session = await self._reconnect()
            result = await session.call_tool(name, arguments)
        except Exception as exc:
            # The transport died with this request in flight. The server may
            # have run it, so it is not resent; the next call reconnects.
            if _is_connection_closed(exc):
                self._transport_lost = True
            raise
        chunks: list[str] = []
        for block in result.content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                chunks.append(text)
                continue
            if hasattr(block, "model_dump_json"):
                chunks.append(block.model_dump_json())
        structured = getattr(result, "structuredContent", None)
        if not chunks and structured is not None:
            chunks.append(json.dumps(structured, ensure_ascii=False))
        return MCPToolResult(
            content="\n".join(chunks),
            is_error=bool(getattr(result, "isError", False)),
        )


def _root_cause(exc: BaseException) -> BaseException:
    """Unwrap single-member exception groups that anyio task groups add."""
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    return exc


def _is_connection_closed(exc: BaseException) -> bool:
    error = getattr(exc, "error", None)
    try:
        from mcp.types import CONNECTION_CLOSED
    except ImportError:  # pragma: no cover - the SDK is a hard dependency
        return False
    return getattr(error, "code", None) == CONNECTION_CLOSED
