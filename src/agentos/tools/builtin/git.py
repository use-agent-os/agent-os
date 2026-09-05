"""Git built-in tools: git_status, git_diff, git_commit, git_log."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from agentos.redact import redact_sensitive_text
from agentos.sandbox.integration import get_runtime, run_under_backend, sandboxed
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
        return workdir
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
        ctx = current_tool_context.get()
        if cwd:
            workspace = Path(cwd).expanduser().resolve()
        elif ctx and ctx.workspace_dir:
            workspace = Path(ctx.workspace_dir).expanduser().resolve()
        else:
            workspace = runtime.workspace.expanduser().resolve()
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
        result = await run_under_backend(
            build_request_for_git(args, workspace, action_kind, policy),
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


def _git_diff_argv(a: dict[str, Any]) -> tuple[str, ...]:
    argv = ["git", "diff"]
    if a.get("staged"):
        argv.append("--cached")
    path = a.get("path")
    if path:
        argv += ["--", str(path)]
    return tuple(argv)


@tool(
    name="git_diff",
    description="Show git diff (staged + unstaged changes).",
    params={
        "path": {"type": "string", "description": "Limit diff to this path."},
        "staged": {"type": "boolean", "description": "Show only staged changes."},
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
    args = ["diff"]
    if staged:
        args.append("--cached")
    if path:
        _reject_foreign_git_path(path)
        args += ["--", path]
    return await _run_git(*args, cwd=_effective_workdir(workdir))


@tool(
    name="git_commit",
    description="Stage specified files (or all changes) and create a commit.",
    params={
        "message": {"type": "string", "description": "Commit message."},
        "files": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Files to stage. If omitted, stages all changes (git add -A).",
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
    if files is not None:
        if files:
            for file_path in files:
                _reject_foreign_git_path(file_path)
            await _run_git("add", "--", *files, cwd=cwd)
    else:
        await _run_git("add", "-A", cwd=cwd)
    return await _run_git("commit", "-m", message, cwd=cwd)


@tool(
    name="git_log",
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
    return await _run_git(
        "log",
        f"--max-count={count}",
        "--oneline",
        "--decorate",
        cwd=_effective_workdir(workdir),
    )
