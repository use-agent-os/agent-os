#!/usr/bin/env python3
"""Direct shell wrapper around ``git diff`` — meta-skill entrypoint.

Returns the diff text on stdout, the literal ``NO_DIFF`` when the
diff is empty, and exits non-zero with the git error on stderr when
git itself fails (not a repo, missing binary, etc.).

git's output is carried as bytes from capture to stdout: SKILL.md
promises the diff back raw, and the text layer cannot keep that
promise — it decodes with the locale encoding (ASCII under a C
locale), re-encodes with the console code page (cp936/cp1252 on
Windows, where a piped stdout does not get the UTF-8 path), and
translates CRLF to LF, rewriting the diff of a CRLF file.

Used by workflows that need repository diffs while skipping a full
sub-Agent loop just to call ``git diff``.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import TextIO

_VALID_MODES = {
    "cached_fallback_worktree",
    "cached",
    "worktree",
    "staged_files",
}


def _emit(data: bytes, stream: TextIO) -> None:
    """Write bytes verbatim, surviving a non-UTF-8 or wrapped stream.

    The binary buffer is the primary path: it keeps UTF-8 content and CRLF
    intact even when the stream's own encoding cannot represent them. A stream
    without a usable ``buffer`` — a wrapper, or a captured stdout — still gets
    the payload, escaped rather than lost or raised over.
    """
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        try:
            buffer.write(data)
            buffer.flush()
            return
        except (AttributeError, OSError, ValueError):
            # Buffer closed or not writable — fall through to the text layer.
            pass

    encoding = getattr(stream, "encoding", None) or "utf-8"
    text = data.decode("utf-8", errors="backslashreplace")
    # Lossless: unencodable chars become \\uXXXX escapes, not "?".
    stream.write(text.encode(encoding, errors="backslashreplace").decode(encoding))
    stream.flush()


def _run_git(
    args: list[str], cwd: Path, input_bytes: bytes | None = None
) -> tuple[int, bytes, bytes]:
    proc = subprocess.run(  # noqa: S603 — argv is constructed from a static allowlist
        ["git", *args],
        cwd=str(cwd),
        input=input_bytes,
        capture_output=True,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _base_revision(cwd: Path) -> str:
    """What to diff against: ``HEAD``, or the empty tree before the first commit.

    Before the first commit HEAD is an unborn branch, and ``git diff HEAD`` /
    ``git diff --cached HEAD`` exit 128 with ``ambiguous argument 'HEAD'``
    even when files are staged (#2882). Dropping the revision is not a
    substitute: bare ``git diff`` compares the worktree to the index, so it
    loses everything already staged, and bare ``git diff --cached`` loses any
    edit made after staging. With no commit yet, "changes since HEAD" means
    "changes since nothing" -- a diff against the empty tree, which reports
    the staged content and later edits together, as ``git diff HEAD`` would.

    The empty tree's id is asked of git rather than hard-coded, so a SHA-256
    repository gets its own. If neither lookup works (not a repository at
    all), ``HEAD`` is kept so git's own error is what the caller sees.
    """
    rc, _out, _err = _run_git(["rev-parse", "--verify", "--quiet", "HEAD"], cwd)
    if rc == 0:
        return "HEAD"
    rc, out, _err = _run_git(["hash-object", "-t", "tree", "--stdin"], cwd, input_bytes=b"")
    empty_tree = out.decode("ascii", "replace").strip()
    return empty_tree if rc == 0 and empty_tree else "HEAD"


def _diff_for_mode(mode: str, cwd: Path) -> tuple[int, bytes, bytes]:
    if mode == "staged_files":
        return _run_git(["diff", "--cached", "--name-only"], cwd)
    base = _base_revision(cwd)
    if mode == "cached_fallback_worktree":
        rc, out, err = _run_git(["diff", "--cached", base], cwd)
        if rc != 0:
            return rc, out, err
        if out.strip():
            return 0, out, err
        return _run_git(["diff", base], cwd)
    if mode == "cached":
        return _run_git(["diff", "--cached", base], cwd)
    if mode == "worktree":
        return _run_git(["diff", base], cwd)
    raise ValueError(f"unsupported mode {mode!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", default="cached_fallback_worktree")
    parser.add_argument("--cwd", default=".")
    args = parser.parse_args(argv)

    if args.mode not in _VALID_MODES:
        print(
            f"unsupported mode {args.mode!r}; valid: {sorted(_VALID_MODES)!r}",
            file=sys.stderr,
        )
        return 2

    cwd = Path(args.cwd).expanduser().resolve()
    if not cwd.is_dir():
        print(f"cwd does not exist: {cwd}", file=sys.stderr)
        return 2

    try:
        rc, out, err = _diff_for_mode(args.mode, cwd)
    except FileNotFoundError as exc:
        print(f"git binary not found: {exc}", file=sys.stderr)
        return 1

    if rc != 0:
        _emit(err, sys.stderr)
        return rc

    _emit(out if out.strip() else b"NO_DIFF", sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
