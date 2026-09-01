from __future__ import annotations

import json
import sys

from agentos.cli.output import emit_error, print_json


def test_print_json_uses_stdout(capsys):
    print_json({"text": "héllo", "value": object()})

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["text"] == "héllo"
    assert captured.err == ""


def test_print_json_handles_unicode_and_em_dash(capsys):
    print_json({"desc": "Analyze wallet — holdings 🚀"})

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["desc"] == "Analyze wallet — holdings 🚀"


def test_emit_error_json_uses_stderr(capsys):
    emit_error("bad input — test", json_output=True, code="INVALID_REQUEST", details={"field": "x"})

    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload == {
        "error": {
            "message": "bad input — test",
            "code": "INVALID_REQUEST",
            "details": {"field": "x"},
        }
    }


def test_write_encoded_json_fallback(monkeypatch, capsys):
    class DummyStream:
        def __init__(self):
            self.encoding = "ascii"
            self.written = ""

        def write(self, text: str) -> None:
            # First attempt raises UnicodeEncodeError for non-ascii
            if any(ord(c) > 127 for c in text):
                raise UnicodeEncodeError("ascii", text, 0, len(text), "ordinal not in range(128)")
            self.written += text

        def flush(self) -> None:
            pass

    dummy = DummyStream()
    monkeypatch.setattr(sys, "stdout", dummy)

    print_json({"text": "test — unicode"})
    assert "test" in dummy.written
