"""uvicorn WebSocket protocol with a per-connection inbound message limit.

uvicorn applies one ``ws_max_size`` to every connection for its whole life,
and the websockets reader reads a whole frame into memory before the
gateway's handshake can look at it. With the post-handshake ceiling (25 MiB)
that let an unauthenticated peer park 25 MiB per socket in the gateway's
memory before being refused.

:class:`GatewayWebSocketProtocol` keeps uvicorn's ``ws_max_size`` as the
outer ceiling and adds a per-connection limit that starts at
``MAX_PREAUTH_PAYLOAD_BYTES``. It is checked when a frame's payload is about
to be read (after its header, before its bytes) and against the running size
of a fragmented or compressed message, so an oversized pre-auth message is
refused with close 1009 without being buffered. The gateway raises the limit
to ``hello-ok.policy.max_payload`` once the handshake succeeds, through the
callable it publishes in the ASGI scope under :data:`FRAME_LIMIT_EXTENSION`.

It also records the socket's own peer under :data:`TRANSPORT_PEER_EXTENSION`
before the app runs. The gateway turns uvicorn's proxy-header middleware off
(``boot.UVICORN_PEER_KWARGS``), which would otherwise rewrite
``scope["client"]`` from ``X-Forwarded-For`` for loopback peers; the agent
surface still reads the address the socket actually saw, so it stays correct
for any host that embeds the app with that middleware on.

The limit is read when the payload arrives, not when the read starts: the
reader is already waiting for the first post-handshake message while the
handshake is being answered.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from uvicorn.protocols.websockets.websockets_impl import WebSocketProtocol
from websockets.exceptions import PayloadTooBig
from websockets.frames import OP_BINARY, OP_CONT, OP_TEXT
from websockets.legacy.framing import Frame

from agentos.gateway.protocol import MAX_PREAUTH_PAYLOAD_BYTES

#: ASGI scope extension holding ``set_limit(bytes | None)`` for a connection.
FRAME_LIMIT_EXTENSION = "agentos.ws.message_limit"
#: ASGI scope extension holding the socket's ``(host, port)`` peer, unrewritten.
TRANSPORT_PEER_EXTENSION = "agentos.ws.transport_peer"

_DATA_OPCODES = frozenset({OP_TEXT, OP_BINARY, OP_CONT})
# Frame header reads are at most 8 bytes; anything larger is a payload read.
_HEADER_READ_MAX = 8


class _LimitedExtension:
    """Wraps a negotiated extension so decoding obeys the current limit."""

    def __init__(self, extension: Any, protocol: GatewayWebSocketProtocol) -> None:
        self._extension = extension
        self._protocol = protocol

    def decode(self, frame: Any, *, max_size: int | None = None) -> Any:
        remaining = self._protocol.remaining_message_bytes()
        if remaining is not None:
            max_size = remaining if max_size is None else min(max_size, remaining)
        return self._extension.decode(frame, max_size=max_size)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._extension, name)


class GatewayWebSocketProtocol(WebSocketProtocol):
    """uvicorn's websockets protocol plus a per-connection message limit."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.message_limit: int | None = MAX_PREAUTH_PAYLOAD_BYTES
        self._message_bytes = 0

    def set_message_limit(self, limit: int | None) -> None:
        self.message_limit = limit

    def remaining_message_bytes(self) -> int | None:
        if self.message_limit is None:
            return None
        return max(0, self.message_limit - self._message_bytes)

    async def run_asgi(self) -> None:
        # The scope exists by now (process_request built it) and the app has
        # not started yet: publish the limit setter before it does.
        scope: dict[str, Any] = self.scope  # type: ignore[assignment]
        extensions = scope.get("extensions")
        if not isinstance(extensions, dict):
            extensions = {}
            scope["extensions"] = extensions
        extensions[FRAME_LIMIT_EXTENSION] = self.set_message_limit
        # Before the app (and uvicorn's proxy-header middleware) sees the scope.
        extensions[TRANSPORT_PEER_EXTENSION] = scope.get("client")
        await super().run_asgi()

    async def read_frame(self, max_size: int | None) -> Frame:
        readexactly: Callable[[int], Awaitable[bytes]] = self.reader.readexactly

        async def limited_read(n: int) -> bytes:
            if n > _HEADER_READ_MAX:
                remaining = self.remaining_message_bytes()
                if remaining is not None and n > remaining:
                    raise PayloadTooBig(self._message_bytes + n, self.message_limit or 0)
            return await readexactly(n)

        extensions = [_LimitedExtension(ext, self) for ext in self.extensions]
        frame = await Frame.read(
            limited_read,
            mask=not self.is_client,
            max_size=max_size,
            extensions=extensions,  # type: ignore[arg-type]
        )
        if frame.opcode in _DATA_OPCODES:
            if frame.fin:
                self._message_bytes = 0
            else:
                self._message_bytes += len(frame.data)
        if self.debug:
            self.logger.debug("< %s", frame)
        return frame


def raise_message_limit(scope: Any, limit: int | None) -> bool:
    """Apply ``limit`` to the connection behind ``scope``; False if unsupported."""
    extensions = scope.get("extensions") if isinstance(scope, dict) else None
    setter = extensions.get(FRAME_LIMIT_EXTENSION) if isinstance(extensions, dict) else None
    if not callable(setter):
        return False
    setter(limit)
    return True


__all__ = [
    "FRAME_LIMIT_EXTENSION",
    "GatewayWebSocketProtocol",
    "raise_message_limit",
]
