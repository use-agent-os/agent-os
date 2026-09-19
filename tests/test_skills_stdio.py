"""``agentos.skills.stdio`` -- the shared UTF-8 stdio helpers bundled scripts use.

Issue #2804: the same ``_write_stdout`` had been copied into nine scripts by
four PRs, and 38 more scripts still had nothing. The helper now lives here,
with a second shape for scripts that print progressively, and both are pinned
against a simulated legacy code page rather than skipped off-Windows.
"""

from __future__ import annotations

import io
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from agentos.skills.stdio import SUBPROCESS_UTF8, configure_utf8_stdio, write_stdout

NON_ASCII = "日本語のテキスト cổ phiếu — 🚀"


class CodePageStream:
    """A text stream over a ``BytesIO`` that only accepts one legacy code page."""

    def __init__(self, encoding: str = "cp1252", initial: bytes = b"") -> None:
        self.sink = io.BytesIO(initial)
        self.stream = io.TextIOWrapper(self.sink, encoding=encoding, newline="")

    def bytes(self) -> bytes:
        self.stream.flush()
        return self.sink.getvalue()


@contextmanager
def stdout_as(stream: CodePageStream) -> Iterator[CodePageStream]:
    """Swap ``sys.stdout`` **inside** the test body: pytest re-activates its
    own capture after fixture setup, so a swap made in a fixture is undone
    before the test runs."""
    saved = sys.stdout
    sys.stdout = stream.stream
    try:
        yield stream
    finally:
        sys.stdout = saved


@contextmanager
def stderr_as(stream: CodePageStream) -> Iterator[CodePageStream]:
    saved = sys.stderr
    sys.stderr = stream.stream
    try:
        yield stream
    finally:
        sys.stderr = saved


# ── write_stdout ────────────────────────────────────────────────────────────


def test_write_stdout_emits_utf8_bytes_on_a_code_page_console() -> None:
    with stdout_as(CodePageStream()) as out:
        write_stdout(NON_ASCII)

        assert out.bytes().decode("utf-8") == NON_ASCII


def test_write_stdout_would_have_raised_through_the_text_layer() -> None:
    """The defect, pinned: the plain text layer cannot take these characters."""
    with stdout_as(CodePageStream()), pytest.raises(UnicodeEncodeError):
        sys.stdout.write(NON_ASCII)
        sys.stdout.flush()


@pytest.mark.parametrize("encoding", ["cp1252", "cp936", "cp932", "cp437", "ascii", "latin-1"])
def test_write_stdout_is_independent_of_the_code_page(
    monkeypatch: pytest.MonkeyPatch, encoding: str
) -> None:
    stream = CodePageStream(encoding)
    monkeypatch.setattr(sys, "stdout", stream.stream)

    write_stdout(NON_ASCII)

    assert stream.bytes() == NON_ASCII.encode("utf-8")


def test_write_stdout_falls_back_to_escaping_when_there_is_no_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A captured stdout with no binary layer still gets the text, escaped."""
    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured)

    write_stdout("x " + NON_ASCII)

    assert captured.getvalue().startswith("x ")
    assert "\\u65e5" in captured.getvalue() or "日" in captured.getvalue()


def test_write_stdout_falls_back_when_the_buffer_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    class ClosedBuffer:
        def write(self, _data: bytes) -> None:
            raise ValueError("I/O operation on closed file")

    class Stream(io.StringIO):
        buffer = ClosedBuffer()
        encoding = "ascii"

    stream = Stream()
    monkeypatch.setattr(sys, "stdout", stream)

    write_stdout("é")

    assert stream.getvalue() == "\\xe9"


def test_write_stdout_writes_exactly_the_text_and_nothing_more() -> None:
    with stdout_as(CodePageStream()) as out:
        write_stdout('{"a": 1}\n')

        assert out.bytes() == b'{"a": 1}\n'


# ── configure_utf8_stdio ────────────────────────────────────────────────────


def test_configure_makes_print_emit_utf8() -> None:
    with stdout_as(CodePageStream()) as out:
        configure_utf8_stdio()
        print(NON_ASCII)

        assert out.bytes().decode("utf-8") == NON_ASCII + "\n"


def test_configure_makes_sys_stdout_write_emit_utf8() -> None:
    with stdout_as(CodePageStream()) as out:
        configure_utf8_stdio()
        sys.stdout.write(NON_ASCII)

        assert out.bytes().decode("utf-8") == NON_ASCII


def test_configure_covers_stderr_too(monkeypatch: pytest.MonkeyPatch) -> None:
    err = CodePageStream()
    monkeypatch.setattr(sys, "stderr", err.stream)

    configure_utf8_stdio()
    print(NON_ASCII, file=sys.stderr)

    assert err.bytes().decode("utf-8") == NON_ASCII + "\n"


def test_configure_leaves_stdin_alone_unless_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    stdin = CodePageStream("cp1252", NON_ASCII.encode("utf-8"))
    monkeypatch.setattr(sys, "stdin", stdin.stream)

    configure_utf8_stdio()

    assert sys.stdin.encoding == "cp1252"


def test_configure_with_stdin_decodes_a_utf8_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    """The receiving end of ``chain_stocks.py | chain_cards.py``: the bytes
    are UTF-8 whatever the console thinks."""
    stdin = CodePageStream("cp1252", NON_ASCII.encode("utf-8"))
    monkeypatch.setattr(sys, "stdin", stdin.stream)

    configure_utf8_stdio(stdin=True)

    assert sys.stdin.read() == NON_ASCII


def test_without_configure_the_same_pipe_is_mojibake(monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect on the input side, pinned."""
    stdin = CodePageStream("cp1252", "日本".encode())
    monkeypatch.setattr(sys, "stdin", stdin.stream)

    assert sys.stdin.read() != "日本"


def test_configure_stdin_replaces_undecodable_bytes_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdin = CodePageStream("cp1252", b"ok \xff\xfe end")
    monkeypatch.setattr(sys, "stdin", stdin.stream)

    configure_utf8_stdio(stdin=True)

    assert sys.stdin.read() == "ok �� end"


def test_configure_escapes_a_lone_surrogate_instead_of_raising() -> None:
    with stdout_as(CodePageStream()) as out:
        configure_utf8_stdio()
        print("bad \ud800 char")

        assert out.bytes() == b"bad \\ud800 char\n"


def test_configure_is_a_no_op_on_a_stream_without_reconfigure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pytest's own capture, or any plain StringIO, cannot be reconfigured
    and must not make the script fail."""
    plain = io.StringIO()
    monkeypatch.setattr(sys, "stdout", plain)
    monkeypatch.setattr(sys, "stderr", io.StringIO())

    configure_utf8_stdio(stdin=True)
    print("still works", file=sys.stdout)

    assert plain.getvalue() == "still works\n"


def test_configure_survives_a_missing_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdout", None)

    configure_utf8_stdio()  # must not raise


def test_configure_is_idempotent() -> None:
    with stdout_as(CodePageStream()) as out:
        configure_utf8_stdio()
        configure_utf8_stdio()
        print(NON_ASCII)

        assert out.bytes().decode("utf-8") == NON_ASCII + "\n"


def test_configure_after_stdin_was_read_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """CPython refuses to change a stream's encoding after a read; the helper
    treats that as "leave it", not as an error."""
    stdin = CodePageStream("cp1252", b"first line\nsecond")
    monkeypatch.setattr(sys, "stdin", stdin.stream)
    sys.stdin.readline()

    configure_utf8_stdio(stdin=True)  # must not raise


# ── SUBPROCESS_UTF8 ─────────────────────────────────────────────────────────


def test_subprocess_utf8_decodes_a_utf8_child_as_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    """``text=True`` alone would decode through the locale; the shared kwargs
    name the encoding so a child printing CJK is read back intact."""
    script = f"import sys; sys.stdout.buffer.write({NON_ASCII!r}.encode('utf-8'))"

    result = subprocess.run([sys.executable, "-c", script], capture_output=True, **SUBPROCESS_UTF8)

    assert result.stdout == NON_ASCII


def test_subprocess_utf8_replaces_rather_than_raises_on_bad_bytes() -> None:
    script = "import sys; sys.stdout.buffer.write(b'ok \\xff end')"

    result = subprocess.run([sys.executable, "-c", script], capture_output=True, **SUBPROCESS_UTF8)

    assert result.stdout == "ok � end"


def test_subprocess_utf8_is_exactly_the_two_keywords() -> None:
    """The dict is spread into ``subprocess.run``; anything else in it would
    change the calls' behaviour silently."""
    assert SUBPROCESS_UTF8 == {"encoding": "utf-8", "errors": "replace"}


def _kwargs_seen(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    def fake_run(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


def test_subprocess_utf8_composes_with_text_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller that keeps ``text=True`` and adds the kwargs still gets UTF-8:
    ``encoding`` implies text mode and wins."""
    seen = _kwargs_seen(monkeypatch)

    subprocess.run(["x"], text=True, **SUBPROCESS_UTF8)

    assert seen[0]["encoding"] == "utf-8"
