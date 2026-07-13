from __future__ import annotations

import json

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
