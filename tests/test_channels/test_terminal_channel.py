"""TerminalChannel.receive() on Windows vs POSIX (#1575).

``_get_reader`` calls ``loop.connect_read_pipe`` on ``sys.stdin``, which the
default Windows ``ProactorEventLoop`` tries to register with IOCP. An
interactive console handle is not overlapped-capable, so that registration
raises ``OSError: [WinError 6] The handle is invalid`` and permanently
breaks the reader. Windows now reads stdin via
``loop.run_in_executor(None, sys.stdin.buffer.readline)`` instead -- the
same pattern ``send``/``edit`` in this class already use for stdout, reading
through the *buffer* (bytes) rather than the text-mode ``sys.stdin`` so
decoding goes through the same ``errors="replace"`` policy as the POSIX
path below it, instead of Python's default strict handler.
"""

from __future__ import annotations

import sys

from agentos.channels.terminal import TerminalChannel


class _FakeStreamReader:
    """Stand-in for the asyncio.StreamReader the POSIX path would build."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)


class _FakeStdinBuffer:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)


class _FakeStdin:
    def __init__(self, lines: list[bytes]) -> None:
        self.buffer = _FakeStdinBuffer(lines)


async def test_receive_uses_executor_readline_on_windows(monkeypatch) -> None:
    """The Windows path must never touch connect_read_pipe / _get_reader --
    that is exactly the call that raises WinError 6.
    """
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdin", _FakeStdin([b"hello world\n", b"second line\n"]))
    channel = TerminalChannel()

    async def _get_reader_should_not_be_called() -> _FakeStreamReader:
        raise AssertionError("_get_reader (connect_read_pipe) must not run on Windows")

    monkeypatch.setattr(channel, "_get_reader", _get_reader_should_not_be_called)

    first = await channel.receive()
    second = await channel.receive()

    assert first.content == "hello world"
    assert second.content == "second line"
    assert first.sender_id == "user"
    assert first.channel_id == "terminal"


async def test_receive_reports_eof_as_empty_content_on_windows(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdin", _FakeStdin([]))
    channel = TerminalChannel()

    message = await channel.receive()

    assert message.content == ""


async def test_receive_replaces_malformed_bytes_on_windows_like_posix_does(monkeypatch) -> None:
    """The precision gap this fix closes versus a plain ``sys.stdin.readline()``
    approach: malformed input must be replaced, not raise, on *both*
    platforms -- reading through ``sys.stdin.buffer`` (bytes) and decoding
    with ``errors="replace"`` is what makes that true on Windows too.
    """
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdin", _FakeStdin([b"bad \xff\xfe byte\n"]))
    channel = TerminalChannel()

    message = await channel.receive()

    assert message.content == "bad \ufffd\ufffd byte"


async def test_receive_still_uses_the_stream_reader_off_windows(monkeypatch) -> None:
    """Non-Windows platforms keep the original connect_read_pipe-backed
    reader -- this fix is additive, not a platform-wide behavior change.
    """
    monkeypatch.setattr(sys, "platform", "linux")
    channel = TerminalChannel()
    fake_reader = _FakeStreamReader([b"posix line\n"])

    async def _fake_get_reader() -> _FakeStreamReader:
        return fake_reader

    monkeypatch.setattr(channel, "_get_reader", _fake_get_reader)

    message = await channel.receive()

    assert message.content == "posix line"


async def test_receive_replaces_malformed_bytes_off_windows_too(monkeypatch) -> None:
    """Same malformed-input guarantee on the untouched POSIX path, to pin
    down that both branches genuinely share one decoding policy.
    """
    monkeypatch.setattr(sys, "platform", "linux")
    channel = TerminalChannel()
    fake_reader = _FakeStreamReader([b"bad \xff\xfe byte\n"])

    async def _fake_get_reader() -> _FakeStreamReader:
        return fake_reader

    monkeypatch.setattr(channel, "_get_reader", _fake_get_reader)

    message = await channel.receive()

    assert message.content == "bad \ufffd\ufffd byte"
