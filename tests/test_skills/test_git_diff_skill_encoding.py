"""The bundled ``git-diff`` script must hand back git's bytes unchanged.

SKILL.md promises "raw unified diff text on stdout", and the script is run by
the agent through the shell tool, so its stdout is always a pipe — the case
where Python falls back to the locale code page instead of a console's UTF-8
path. These drive the real script in a child process and assert on raw bytes,
because the defect lives in the stream layer and a monkeypatched stdout would
not exercise it (Issue #1834).
"""

from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "src" / "agentos" / "skills" / "bundled" / "git-diff" / "scripts" / "git_diff.py"

#: "你好世界 🎉" — CJK plus an astral emoji, so cp936 fails on the emoji and
#: cp1252 fails on every character.
NON_ASCII = "你好世界 \U0001f389"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=test",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.autocrlf=false",
            *args,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo with one commit and a staged change holding non-ASCII text."""
    _git(tmp_path, "init", "-q", ".")
    (tmp_path / "f.txt").write_bytes(b"hello\n")
    _git(tmp_path, "add", "f.txt")
    _git(tmp_path, "commit", "-qm", "base")
    (tmp_path / "f.txt").write_bytes(f"{NON_ASCII}\n".encode())
    _git(tmp_path, "add", "f.txt")
    return tmp_path


def _run(repo: Path, **env_overrides: str) -> subprocess.CompletedProcess[bytes]:
    env = {**os.environ, **env_overrides}
    env.pop("PYTHONIOENCODING", None)
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--cwd", str(repo)],
        capture_output=True,
        env=env,
    )


def _raw_git_diff(repo: Path) -> bytes:
    return subprocess.run(
        ["git", "diff", "--cached", "HEAD"],
        cwd=repo,
        capture_output=True,
        check=True,
    ).stdout


def test_non_ascii_diff_survives_a_non_utf8_stdout_encoding(repo: Path) -> None:
    # A Windows box whose active code page is cp936: the encode side used to
    # raise UnicodeEncodeError on the emoji before a byte was written.
    proc = _run(repo, PYTHONIOENCODING="cp936")

    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert NON_ASCII.encode() in proc.stdout


def test_non_ascii_diff_survives_a_c_locale(repo: Path) -> None:
    # The decode side, a separate mechanism: ``text=True`` decoded git's bytes
    # with the locale encoding, which is ASCII once C-locale coercion is off.
    proc = _run(
        repo,
        LC_ALL="C",
        LANG="C",
        PYTHONCOERCECLOCALE="0",
        PYTHONUTF8="0",
    )

    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert NON_ASCII.encode() in proc.stdout


def test_diff_is_byte_identical_to_git(repo: Path) -> None:
    proc = _run(repo)

    assert proc.returncode == 0
    assert proc.stdout == _raw_git_diff(repo)


def test_crlf_diff_keeps_its_carriage_returns(tmp_path: Path) -> None:
    # Universal newlines translated CRLF to LF, so the script rewrote the diff
    # of a CRLF file even on a UTF-8 system. Byte-exactness is the contract.
    _git(tmp_path, "init", "-q", ".")
    (tmp_path / "w.txt").write_bytes(b"a\r\nb\r\n")
    _git(tmp_path, "add", "w.txt")
    _git(tmp_path, "commit", "-qm", "base")
    (tmp_path / "w.txt").write_bytes(b"a\r\nCHANGED\r\n")
    _git(tmp_path, "add", "w.txt")

    proc = _run(tmp_path)

    assert proc.returncode == 0
    assert b"\r\n" in proc.stdout
    assert proc.stdout == _raw_git_diff(tmp_path)


def test_no_diff_marker_when_nothing_is_staged(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", ".")
    (tmp_path / "f.txt").write_bytes(b"hello\n")
    _git(tmp_path, "add", "f.txt")
    _git(tmp_path, "commit", "-qm", "base")

    proc = _run(tmp_path, PYTHONIOENCODING="cp936")

    assert proc.returncode == 0
    assert proc.stdout == b"NO_DIFF"


def test_git_failure_still_reports_on_stderr(tmp_path: Path) -> None:
    # Not a repo: the exit code stays git's own and stderr carries its output,
    # which this change deliberately leaves as it is.
    proc = _run(tmp_path, PYTHONIOENCODING="cp936")

    assert proc.returncode != 0
    assert proc.stderr


def _load_script() -> object:
    # ``git-diff`` is not a Python identifier, so the module is loaded by path.
    spec = importlib.util.spec_from_file_location("bundled_git_diff", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_emit_falls_back_when_the_stream_has_no_buffer() -> None:
    # Reviewer note on #764: the writer must stay correct when ``buffer`` is
    # absent or the stream is wrapped, rather than raising.
    module = _load_script()

    class NoBufferStream(io.StringIO):
        encoding = "cp936"

    stream = NoBufferStream()
    module._emit(f"{NON_ASCII}\n".encode(), stream)

    written = stream.getvalue()
    assert "你好世界" in written
    # The emoji cannot be represented in cp936, so it is escaped, not dropped.
    assert "?" not in written


def test_emit_writes_bytes_verbatim_through_the_buffer() -> None:
    module = _load_script()
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp936")

    payload = f"{NON_ASCII}\n".encode()
    module._emit(payload, stream)

    assert raw.getvalue() == payload
