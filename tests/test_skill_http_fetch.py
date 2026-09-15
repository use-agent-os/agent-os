"""http-fetch — the ``--max-bytes`` response cap.

SKILL.md documents the cap twice, as "response body cap" and as "truncated to
``max_bytes`` if larger", so the property under test is simply that the bytes
reaching stdout never outnumber the cap -- for any body, at any cap, including
the degenerate ones a caller can type.

Offline: no server and no socket. The cap is a pure function, and the
end-to-end cases replace ``_fetch`` with a canned response.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

BUNDLED = Path(__file__).resolve().parents[1] / "src" / "agentos" / "skills" / "bundled"
SCRIPT = BUNDLED / "http-fetch" / "scripts" / "http_fetch.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("http_fetch", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


http_fetch = _load()

ASCII_BODY = b"A" * 5000
CJK_BODY = "漢".encode() * 2000
MIXED_BODY = ("ab" + "漢" * 100 + "cd").encode()


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("ascii", ASCII_BODY),
        ("three_byte_characters", CJK_BODY),
        ("mixed_widths", MIXED_BODY),
        ("four_byte_characters", "🎬".encode() * 500),
    ],
)
@pytest.mark.parametrize("cap", [4000, 1002, 1001, 1000, 37, 4, 3, 2, 1, 0])
def test_the_cap_is_never_exceeded(label: str, body: bytes, cap: int) -> None:
    """The one promise: what is written out fits the budget that was asked for."""
    out = http_fetch._truncate(body, cap)

    assert len(out) <= cap


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("three_byte_characters", CJK_BODY),
        ("four_byte_characters", "🎬".encode() * 500),
        ("mixed_widths", MIXED_BODY),
    ],
)
@pytest.mark.parametrize("cap", [1000, 1001, 1002, 37, 10])
def test_a_cut_never_lands_inside_a_character(label: str, body: bytes, cap: int) -> None:
    """A split character decodes to U+FFFD, which is three bytes again on the
    way back out -- so the cut has to respect character boundaries or the cap
    cannot hold."""
    out = http_fetch._truncate(body, cap)

    out.decode("utf-8")  # strict: raises if a partial sequence survived


def test_a_body_within_the_cap_is_untouched() -> None:
    """Guard: truncation only ever applies to a body that is actually too big."""
    assert http_fetch._truncate(ASCII_BODY, len(ASCII_BODY)) == ASCII_BODY
    assert http_fetch._truncate(ASCII_BODY, len(ASCII_BODY) + 1) == ASCII_BODY
    assert http_fetch._truncate(b"", 0) == b""


def test_a_truncated_body_says_so() -> None:
    """The marker is the only signal the caller gets that bytes were dropped."""
    out = http_fetch._truncate(ASCII_BODY, 100)

    assert out.endswith("…".encode())
    assert len(out) == 100


@pytest.mark.parametrize("cap", [2, 1, 0, -1, -5000])
def test_a_cap_too_small_for_the_marker_drops_the_marker_not_the_cap(cap: int) -> None:
    """``--max-bytes 0`` returned the whole body: ``raw[:-1]`` is everything
    but the last byte, not nothing."""
    out = http_fetch._truncate(ASCII_BODY, cap)

    assert len(out) <= max(cap, 0)
    assert "…".encode() not in out


def test_an_invalid_byte_inside_the_kept_region_is_left_to_the_lossy_decode() -> None:
    """Scope: the trim is for the cut, not for bytes that were always broken."""
    body = b"ab\xffcd" + b"Z" * 100

    out = http_fetch._truncate(body, 20)

    assert out.startswith(b"ab\xffcd")
    assert out.decode("utf-8", errors="replace").startswith("ab�cd")


def _canned_response(monkeypatch: pytest.MonkeyPatch, body: bytes) -> None:
    monkeypatch.setattr(http_fetch, "_fetch", lambda url, method, data, timeout: (200, body, "OK"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)


@pytest.mark.parametrize("cap", [1000, 100, 3, 1, 0])
def test_stdout_honours_the_cap_end_to_end(
    cap: int, monkeypatch: pytest.MonkeyPatch, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    """Measured on stdout, where a caller measures it, after the lossy decode."""
    _canned_response(monkeypatch, CJK_BODY)

    assert http_fetch.main(["--url", "http://example.invalid/", "--max-bytes", str(cap)]) == 0

    assert len(capsysbinary.readouterr().out) <= cap


def test_a_small_body_still_reaches_stdout_whole(
    monkeypatch: pytest.MonkeyPatch, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    """Guard: passes either way, and pins that the cap changed nothing else."""
    _canned_response(monkeypatch, b"hello world")

    assert http_fetch.main(["--url", "http://example.invalid/"]) == 0

    assert capsysbinary.readouterr().out == b"hello world"


def test_a_non_2xx_body_is_capped_too(
    monkeypatch: pytest.MonkeyPatch, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    """SKILL.md: stdout still carries the body on a non-2xx, so it is capped too."""
    monkeypatch.setattr(
        http_fetch, "_fetch", lambda url, method, data, timeout: (500, ASCII_BODY, "Boom")
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)

    assert http_fetch.main(["--url", "http://example.invalid/", "--max-bytes", "50"]) == 1

    captured = capsysbinary.readouterr()
    assert len(captured.out) <= 50
    assert b"HTTP 500" in captured.err
