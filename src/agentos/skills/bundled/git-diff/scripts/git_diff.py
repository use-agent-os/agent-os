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


def _run_git(args: list[str], cwd: Path) -> tuple[int, bytes, bytes]:
    proc = subprocess.run(  # noqa: S603 — argv is constructed from a static allowlist
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _has_head(cwd: Path) -> bool:
    """Whether the repository has a commit to diff against.

    ``git diff HEAD`` is the spelling that reports staged and unstaged work in
    one pass, but before the first commit lands ``HEAD`` names nothing and git
    exits 128 with ``ambiguous argument 'HEAD'``. There the index *is* the
    entire change set, so the revision is dropped and ``--cached`` carries it.

    Asked as its own question, with the same probe
    ``tools/builtin/git.py::_diff_revision`` already uses, rather than inferred
    from a failed diff: a diff can fail for reasons that have nothing to do
    with ``HEAD`` -- a damaged object store is the easy one -- and retrying
    those without the revision answers a *different* question and calls it
    success.
    """
    rc, _out, _err = _run_git(["rev-parse", "--verify", "--quiet", "HEAD"], cwd)
    return rc == 0


def _diff_argv(*, cached: bool, head: bool) -> list[str]:
    argv = ["diff"]
    if cached:
        argv.append("--cached")
    if head:
        argv.append("HEAD")
    return argv


def _diff_for_mode(mode: str, cwd: Path) -> tuple[int, bytes, bytes]:
    if mode == "staged_files":
        # Never spelled HEAD, so it worked on an unborn branch already.
        return _run_git(["diff", "--cached", "--name-only"], cwd)

    if mode not in ("cached_fallback_worktree", "cached", "worktree"):
        raise ValueError(f"unsupported mode {mode!r}")

    head = _has_head(cwd)
    if mode == "cached":
        return _run_git(_diff_argv(cached=True, head=head), cwd)
    if mode == "worktree":
        return _run_git(_diff_argv(cached=False, head=head), cwd)

    rc, out, err = _run_git(_diff_argv(cached=True, head=head), cwd)
    if rc != 0:
        return rc, out, err
    if out.strip():
        return 0, out, err
    return _run_git(_diff_argv(cached=False, head=head), cwd)


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
