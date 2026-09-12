"""TerminalChannel: interactive stdin/stdout channel adapter."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field

import structlog

from agentos.channels.types import IncomingMessage, OutgoingMessage

log = structlog.get_logger(__name__)


@dataclass
class TerminalChannel:
    """Channel adapter for interactive terminal (stdin/stdout)."""

    channel_id: str = "terminal"
    sender_id: str = "user"
    _reader: asyncio.StreamReader | None = field(default=None, init=False, repr=False)
    _reader_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    async def _get_reader(self) -> asyncio.StreamReader:
        async with self._reader_lock:
            if self._reader is None:
                loop = asyncio.get_running_loop()
                reader = asyncio.StreamReader()
                protocol = asyncio.StreamReaderProtocol(reader)
                await loop.connect_read_pipe(lambda: protocol, sys.stdin)
                self._reader = reader
            return self._reader

    async def _readline(self) -> str:
        """One line of stdin, decoded and stripped of its trailing newline.

        ``loop.connect_read_pipe`` registers ``sys.stdin`` with the event
        loop's I/O multiplexer. On Windows, the default ``ProactorEventLoop``
        does that via IOCP, which requires an overlapped-capable handle -- an
        interactive console handle is not one, so the registration raises
        ``OSError: [WinError 6] The handle is invalid`` and permanently
        breaks the reader. ``send``/``edit`` in this same class already avoid
        touching the loop's I/O machinery for stdio by running the blocking
        call in a thread; reading stdin the same way sidesteps IOCP
        registration entirely.

        Reads via ``sys.stdin.buffer`` (bytes), not the text-mode
        ``sys.stdin``, so this decodes with the same ``errors="replace"``
        policy as the POSIX path below -- the text-mode object would use
        Python's default (strict) handler and raise on malformed input
        instead of substituting, which the POSIX path never does.
        """
        if sys.platform == "win32":
            loop = asyncio.get_running_loop()
            line_bytes: bytes = await loop.run_in_executor(None, sys.stdin.buffer.readline)
            return line_bytes.decode(errors="replace").rstrip("\n")
        reader = await self._get_reader()
        line_bytes = await reader.readline()
        return line_bytes.decode(errors="replace").rstrip("\n")

    async def receive(self) -> IncomingMessage:
        """Read one line from stdin and return as IncomingMessage."""
        content = await self._readline()
        log.debug("terminal.receive", content=content[:80])
        return IncomingMessage(
            sender_id=self.sender_id,
            channel_id=self.channel_id,
            content=content,
        )

    async def send(self, message: OutgoingMessage) -> None:
        """Write message content to stdout."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._write_stdout, message.content)
        log.debug("terminal.send", content=message.content[:80])

    async def edit(self, message_id: str, content: str) -> None:
        """Edit is not supported on terminal; re-print with prefix."""
        prefix = f"[edit:{message_id}] "
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._write_stdout, prefix + content)
        log.debug("terminal.edit", message_id=message_id)

    async def delete(self, message_id: str) -> None:
        """Delete is not supported on terminal; print a notice."""
        notice = f"[deleted:{message_id}]\n"
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._write_stdout, notice)
        log.debug("terminal.delete", message_id=message_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _write_stdout(text: str) -> None:
        if not text.endswith("\n"):
            text += "\n"
        sys.stdout.write(text)
        sys.stdout.flush()
