from __future__ import annotations

import io
import json
import sys

from agentos.cli.output import emit_error, print_json


def test_print_json_uses_stdout(capsys):
    print_json({"text": "héllo", "value": object()})

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["text"] == "héllo"
    assert captured.err == ""


def test_emit_error_json_uses_stderr(capsys):
    emit_error("bad input", json_output=True, code="INVALID_REQUEST", details={"field": "x"})

    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload == {
        "error": {
            "message": "bad input",
            "code": "INVALID_REQUEST",
            "details": {"field": "x"},
        }
    }


def test_print_json_survives_latin1_buffer_stream(monkeypatch):
    """Simulate Windows cp1252/latin-1 terminal with a raw underlying buffer.

    Ensures UTF-8 bytes containing non-ASCII characters (em-dash, emoji, non-Latin)
    are written directly to the buffer rather than raising UnicodeEncodeError.
    """
    raw_buffer = io.BytesIO()
    # A TextIOWrapper with latin-1 encoding will fail if text is written through it,
    # but print_json should write raw UTF-8 bytes to raw_buffer.
    text_stream = io.TextIOWrapper(raw_buffer, encoding="latin-1")
    monkeypatch.setattr(sys, "stdout", text_stream)

    test_payload = {
        "title": "Fix — em-dash & 日本語 & 🚀",
        "description": "Unicode test string",
    }
    print_json(test_payload)

    raw_buffer.seek(0)
    data = raw_buffer.read().decode("utf-8")
    assert json.loads(data) == test_payload


def test_emit_error_json_survives_latin1_buffer_stream(monkeypatch):
    """Simulate Windows cp1252/latin-1 stderr with a raw underlying buffer."""
    raw_buffer = io.BytesIO()
    text_stream = io.TextIOWrapper(raw_buffer, encoding="latin-1")
    monkeypatch.setattr(sys, "stderr", text_stream)

    emit_error("Crash — 💥 non-ASCII error", json_output=True, code="TEST_CODE")

    raw_buffer.seek(0)
    data = raw_buffer.read().decode("utf-8")
    payload = json.loads(data)
    assert payload["error"]["message"] == "Crash — 💥 non-ASCII error"
    assert payload["error"]["code"] == "TEST_CODE"


def test_print_json_fallback_without_buffer_replaces_unencodable(monkeypatch):
    """Simulate a buffer-less stream with restricted encoding (e.g. ascii StringIO).

    When .buffer is absent and encoding cannot represent characters,
    errors='replace' ensures a valid line is still produced without raising.
    """

    class RestrictedStringIO(io.StringIO):
        @property
        def encoding(self) -> str:
            return "ascii"

    restricted_stream = RestrictedStringIO()
    monkeypatch.setattr(sys, "stdout", restricted_stream)

    print_json({"title": "Fix — em-dash"})
    output = restricted_stream.getvalue()
    # Line ends with newline and is valid JSON despite character replacement
    assert output.endswith("\n")
    payload = json.loads(output)
    assert "Fix" in payload["title"]
