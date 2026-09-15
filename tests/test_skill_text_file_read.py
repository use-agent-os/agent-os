"""text-file-read skill — read.py unit tests."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "src" / "agentos" / "skills" / "bundled" / "text-file-read" / "scripts"


def _read_module():
    sys.path.insert(0, str(SCRIPTS))
    try:
        import read  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)
    return read


def test_read_utf8_file_success(tmp_path: Path, capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    read = _read_module()
    test_file = tmp_path / "sample.txt"
    payload = "Hello, AgentOS! ✨\nLine 2\n".encode()
    test_file.write_bytes(payload)

    code = read.main(["--input", str(test_file)])
    assert code == 0
    captured = capsysbinary.readouterr()
    assert captured.out == payload
    assert captured.err == b""


def test_read_file_not_found(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    read = _read_module()
    missing = tmp_path / "missing.txt"

    code = read.main(["--input", str(missing)])
    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"Error: file not found: {missing}" in captured.err


def test_read_negative_max_bytes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    read = _read_module()
    test_file = tmp_path / "sample.txt"
    test_file.write_text("Hello", encoding="utf-8")

    code = read.main(["--input", str(test_file), "--max-bytes", "-1"])
    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Error: --max-bytes must be non-negative, got -1" in captured.err


def test_read_exceeds_max_bytes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    read = _read_module()
    test_file = tmp_path / "large.txt"
    test_file.write_text("a" * 100, encoding="utf-8")

    code = read.main(["--input", str(test_file), "--max-bytes", "50"])
    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "exceeds --max-bytes 50" in captured.err


def test_read_invalid_utf8(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    read = _read_module()
    invalid_file = tmp_path / "invalid.txt"
    invalid_file.write_bytes(b"\xff\xfe\x00\x00\x80\x81")

    code = read.main(["--input", str(invalid_file)])
    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Error: not valid UTF-8:" in captured.err
    assert "Traceback" not in captured.err


def test_read_os_error_handling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    read = _read_module()
    test_file = tmp_path / "locked.txt"
    test_file.write_text("content", encoding="utf-8")

    def _mock_read_bytes(_self: Path) -> bytes:
        raise PermissionError("Permission denied: cannot access file")

    monkeypatch.setattr(Path, "read_bytes", _mock_read_bytes)

    code = read.main(["--input", str(test_file)])
    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Error: cannot read file:" in captured.err
    assert "Traceback" not in captured.err


def test_read_stdout_without_buffer_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    read = _read_module()
    test_file = tmp_path / "sample.txt"
    test_file.write_text("Plain stream without buffer", encoding="utf-8")

    stream = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stream)

    code = read.main(["--input", str(test_file)])
    assert code == 0
    assert stream.getvalue() == "Plain stream without buffer"
