"""Tests for the bundled http-fetch skill."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = (
    ROOT / "src" / "agentos" / "skills" / "bundled" / "http-fetch" / "scripts" / "http_fetch.py"
)


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("http_fetch", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("max_bytes", "expected_len", "expected_suffix"),
    [
        (50, 50, "…"),
        (10, 10, "…"),
        (3, 3, "…"),
        (2, 2, "xx"),
        (1, 1, "x"),
        (0, 0, ""),
    ],
)
def test_truncation_never_exceeds_max_bytes(
    max_bytes: int,
    expected_len: int,
    expected_suffix: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mod = _load_module()
    payload = b"x" * 100

    with patch.object(mod, "_fetch", return_value=(200, payload, "OK")):
        code = mod.main(["--url", "https://example.com/api", "--max-bytes", str(max_bytes)])

    assert code == 0
    out = capsys.readouterr().out
    out_bytes = out.encode("utf-8")
    assert len(out_bytes) == expected_len
    assert len(out_bytes) <= max(0, max_bytes)
    if expected_suffix:
        assert out.endswith(expected_suffix)


def test_body_under_limit_is_not_truncated(
    capsys: pytest.CaptureFixture[str],
) -> None:
    mod = _load_module()
    payload = b"Hello, world!"

    with patch.object(mod, "_fetch", return_value=(200, payload, "OK")):
        code = mod.main(["--url", "https://example.com/api", "--max-bytes", "50"])

    assert code == 0
    assert capsys.readouterr().out == "Hello, world!"


def test_invalid_url_scheme_returns_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    mod = _load_module()
    code = mod.main(["--url", "ftp://example.com/file"])
    assert code == 2
    assert "invalid url" in capsys.readouterr().err


def test_unsupported_method_returns_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    mod = _load_module()
    code = mod.main(["--url", "https://example.com", "--method", "OPTIONS"])
    assert code == 2
    assert "unsupported method" in capsys.readouterr().err


def test_non_2xx_status_prints_preview_and_returns_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    mod = _load_module()
    with patch.object(mod, "_fetch", return_value=(404, b"Not found resource", "Not Found")):
        code = mod.main(["--url", "https://example.com/missing"])

    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == "Not found resource"
    assert "HTTP 404: Not Found: Not found resource" in captured.err


def test_stdin_body_is_forwarded_to_fetch(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io

    mod = _load_module()
    class FakeStdin:
        def __init__(self, data: bytes) -> None:
            self.buffer = io.BytesIO(data)

        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(sys, "stdin", FakeStdin(b'{"key": "value"}'))

    received_body: list[bytes] = []

    def mock_fetch(url: str, method: str, body: bytes, timeout: float) -> tuple[int, bytes, str]:
        received_body.append(body)
        return (200, b"created", "OK")

    with patch.object(mod, "_fetch", side_effect=mock_fetch):
        code = mod.main(["--url", "https://example.com/api", "--method", "POST"])

    assert code == 0
    assert received_body == [b'{"key": "value"}']
    assert capsys.readouterr().out == "created"

