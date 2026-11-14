"""Git built-in tools: git_status, git_diff, git_commit, git_log."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from agentos.redact import redact_sensitive_text
from agentos.sandbox.integration import (
    SandboxWorkdirError,
    confine_sandbox_cwd,
    get_runtime,
    run_under_backend,
    sandbox_workspace_root,
    sandboxed,
)
from agentos.sandbox.policy import build_policy, select_level
from agentos.tools.path_policy import reject_foreign_host_path
from agentos.tools.registry import tool
from agentos.tools.types import current_tool_context


def _effective_workdir(workdir: str | None) -> str | None:
    ctx = current_tool_context.get()
    if workdir:
        workspace = (
            Path(ctx.workspace_dir).expanduser().resolve(strict=False)
            if ctx is not None and ctx.workspace_dir
            else None
        )
        reject_foreign_host_path(workdir, platform=os.name, workspace=workspace)
        # A relative workdir must be joined onto the workspace, mirroring
        # ``shell._effective_workdir``. Returning it raw made ``_run_git``
        # resolve it against the process CWD instead — inspecting a different
        # repository than the caller asked for when the gateway runs anywhere
        # but the workspace (#1566).
        raw = Path(workdir).expanduser()
        if not raw.is_absolute() and ctx and ctx.workspace_dir:
            return str((Path(ctx.workspace_dir).expanduser().resolve() / raw).resolve())
        return str(raw.resolve())
    if ctx and ctx.workspace_dir:
        return str(Path(ctx.workspace_dir).expanduser().resolve())
    return None


def _reject_foreign_git_path(path: str) -> None:
    ctx = current_tool_context.get()
    workspace = (
        Path(ctx.workspace_dir).expanduser().resolve(strict=False)
        if ctx is not None and ctx.workspace_dir
        else None
    )
    reject_foreign_host_path(path, platform=os.name, workspace=workspace)


def _redact_git_output(output: str) -> str:
    """Mask credentials in git output before it reaches the model.

    ``git_diff`` hands back arbitrary repository content — a committed ``.env``
    is common enough not to be treated as the exotic case — so the assignment
    pass runs unconditionally (``code_file=False``) rather than being gated on
    the command shape the way :func:`redact_terminal_output` gates it: the git
    argv says nothing about what kind of content is coming back.

    ``file_read=True`` for the same reason the file surfaces use it: a diff is
    file content, and an agent that pipes one back through ``git apply`` must
    not write a head/tail mask that reads as a real-but-truncated credential.
    The sentinel is syntactically invalid, so the round trip fails loudly.
    """
    redacted = redact_sensitive_text(output, force=True, code_file=False, file_read=True)
    return redacted if redacted is not None else output


async def _run_git(*args: str, cwd: str | None = None) -> str:
    runtime = get_runtime()
    if runtime is not None and runtime.effective.sandbox_enabled:
        # The configured workspace is the mount; the repository directory is
        # only where git starts inside it. Mounting ``cwd`` itself let a
        # model-chosen workdir ('/', $HOME) become the writable /workspace.
        workspace = sandbox_workspace_root(runtime)
        action_kind = (
            "git.write" if any(arg in {"add", "commit"} for arg in args[:2]) else "git.read"
        )
        level = (
            select_level(action_kind)
            if runtime.effective.grading_enabled
            else runtime.effective.default_level
        )
        policy = build_policy(
            level,
            action_kind,
            workspace,
            runtime.settings,
            trusted=True,
        )
        try:
            run_cwd = confine_sandbox_cwd(Path(cwd) if cwd else workspace, policy)
        except SandboxWorkdirError as exc:
            raise RuntimeError(str(exc)) from exc
        result = await run_under_backend(
            build_request_for_git(args, run_cwd, action_kind, policy),
            runtime=runtime,
        )
        output = _redact_git_output(result.stdout + result.stderr)
        if result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed (exit {result.returncode}):\n{output}")
        return output
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
    )
    stdout, _ = await proc.communicate()
    output = _redact_git_output(stdout.decode("utf-8", errors="replace"))
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed (exit {proc.returncode}):\n{output}")
    return output


def build_request_for_git(args: tuple[str, ...], cwd: Path, action_kind: str, policy):
    from agentos.sandbox.integration import build_request

    return build_request(
        action_kind=action_kind,
        argv=("git", *args),
        cwd=cwd,
        policy=policy,
        env={},
    )


@tool(
    name="git_status",
    concurrency="parallel",
    read_only=True,
    description="Show the working tree status.",
    params={
        "workdir": {"type": "string", "description": "Git repository directory (default: cwd)."},
    },
    required=[],
)
@sandboxed(
    kind="git.read",
    argv_factory=lambda a: ("git", "status", "--short", "--branch"),
    record_payload=False,
)
async def git_status(workdir: str | None = None) -> str:
    return await _run_git("status", "--short", "--branch", cwd=_effective_workdir(workdir))


async def _diff_revision(cwd: str | None) -> str | None:
    """``"HEAD"`` when the repository has a commit to diff against, else ``None``.

    ``git diff HEAD`` is the spelling that reports staged and unstaged work in
    one pass, but it exits 128 with ``ambiguous argument 'HEAD'`` before the
    first commit lands. There the caller diffs against the empty tree instead
    (see ``_empty_tree``); ``git_log`` relies on the ``None`` to report "no
    commits yet" (#2737).
    """
    try:
        await _run_git("rev-parse", "--verify", "--quiet", "HEAD", cwd=cwd)
    except RuntimeError:
        return None
    return "HEAD"


# The empty tree's id depends on the repository's object format, so a SHA-1 id
# hard-coded into a SHA-256 repository is an ``ambiguous argument`` (#3072).
_EMPTY_TREES = {
    "sha1": "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
    "sha256": "6ef19b41225c5369f1c104d45d8d85efa9b057b53b14b4b9b939dd74decc5321",
}


async def _empty_tree(cwd: str | None) -> str | None:
    """The empty tree's id for this repository's object format, else ``None``.

    Diffing against it before the first commit reports staged and unstaged work
    together, as ``git diff HEAD`` does once a commit exists. ``_run_git`` has
    no stdin, so the id is picked by ``--show-object-format`` rather than asked
    of ``git hash-object --stdin`` the way the bundled ``git-diff`` skill does.
    ``None`` (an old git, an unknown format) keeps the ``--cached`` fallback.
    """
    try:
        object_format = await _run_git("rev-parse", "--show-object-format", cwd=cwd)
    except RuntimeError:
        return None
    return _EMPTY_TREES.get(object_format.strip())


def _git_diff_argv(a: dict[str, Any]) -> tuple[str, ...]:
    # Mirrors the ``git_diff`` body (#614). ``HEAD`` is spelled unconditionally
    # here because the fingerprint is derived before the repository is
    # inspected; the body swaps it out in a repository without a first commit,
    # which is the one case this argv describes more precisely than it runs.
    argv = ["git", "diff"]
    if a.get("staged"):
        argv.append("--cached")
    argv.append("HEAD")
    path = a.get("path")
    if path:
        argv += ["--", str(path)]
    return tuple(argv)


@tool(
    name="git_diff",
    concurrency="parallel",
    read_only=True,
    description="Show git diff against HEAD (staged + unstaged changes).",
    params={
        "path": {"type": "string", "description": "Limit diff to this path."},
        "staged": {
            "type": "boolean",
            "description": "Show only staged changes (omit for staged + unstaged).",
        },
        "workdir": {"type": "string", "description": "Git repository directory (default: cwd)."},
    },
    required=[],
)
@sandboxed(
    kind="git.read",
    argv_factory=_git_diff_argv,
    record_payload=False,
)
async def git_diff(
    path: str | None = None,
    staged: bool = False,
    workdir: str | None = None,
) -> str:
    cwd = _effective_workdir(workdir)
    # Bare ``git diff`` compares the working tree against the *index*, so every
    # staged hunk — and every staged new file — is invisible in the output the
    # description promises. On a tree the caller has just ``git add -A``-ed
    # that is the whole change set, returned as an empty string with no error,
    # which reads as "nothing to review" rather than as a wrong question (#1963).
    # ``HEAD`` is the revision that reports both halves, and is the spelling the
    # bundled ``git-diff`` skill already uses.
    revision = await _diff_revision(cwd)
    if revision is None and not staged:
        # Bare ``--cached`` would drop every edit made after staging (#3072);
        # ``staged=True`` keeps it, since it already compares the index
        # against the empty tree before the first commit.
        revision = await _empty_tree(cwd)
    args = ["diff"]
    if staged or revision is None:
        args.append("--cached")
    if revision is not None:
        args.append(revision)
    if path:
        _reject_foreign_git_path(path)
        args += ["--", path]
    return await _run_git(*args, cwd=cwd)


@tool(
    name="git_commit",
    description="Stage specified files (or all changes) and create a commit.",
    params={
        "message": {"type": "string", "description": "Commit message."},
        "files": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Files to stage. If omitted, stages all changes (git add -A). "
                "Pass an empty array to stage nothing and commit only what is "
                "already staged."
            ),
        },
        "workdir": {"type": "string", "description": "Git repository directory (default: cwd)."},
    },
    required=["message"],
    exposed_by_default=False,
)
@sandboxed(
    kind="git.write",
    argv_factory=lambda a: (
        "git",
        "commit",
        str(a.get("message", "")),
        str(len(a.get("files") or [])),
    ),
    record_payload=False,
)
async def git_commit(
    message: str,
    files: list[str] | None = None,
    workdir: str | None = None,
) -> str:
    cwd = _effective_workdir(workdir)
    # ``files`` omitted (None) means "stage everything"; an explicitly empty
    # list means "stage nothing" and commit what the caller already staged.
    # Collapsing the two with ``if files:`` swept untracked files the caller
    # never named into the commit.
    if files is None:
        await _run_git("add", "-A", cwd=cwd)
    elif files:
        for file_path in files:
            _reject_foreign_git_path(file_path)
        await _run_git("add", "--", *files, cwd=cwd)
    return await _run_git("commit", "-m", message, cwd=cwd)


@tool(
    name="git_log",
    concurrency="parallel",
    read_only=True,
    description="Show recent git commit log.",
    params={
        "count": {"type": "integer", "description": "Number of commits to show (default 10)."},
        "workdir": {"type": "string", "description": "Git repository directory (default: cwd)."},
    },
    required=[],
)
@sandboxed(
    kind="git.read",
    argv_factory=lambda a: ("git", "log", str(a.get("count", 10))),
    record_payload=False,
)
async def git_log(count: int = 10, workdir: str | None = None) -> str:
    cwd = _effective_workdir(workdir)
    if await _diff_revision(cwd) is None:
        await _run_git("rev-parse", "--git-dir", cwd=cwd)
        return "No commits yet in repository."
    return await _run_git(
        "log",
        f"--max-count={count}",
        "--oneline",
        "--decorate",
        cwd=cwd,
    )
