"""An ACP "Always allow" never covers a program that deletes data or runs code.

Interpreters, shells, package runners, build tools, container and cluster
clients, downloaders, ``rm``/``dd``/``mkfs``-style programs, recursive
``chmod``/``chown``/``chgrp`` and the ``git`` subcommands that publish or
discard work always go back to the operator: an Always on one is never
remembered, and a grant remembered for one (before this rule existed, or
under another spelling) never answers a later prompt.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

import pytest
from acp_perm_helpers import make_queue, next_pending, perm_request

from agentos.acp.permissions import (
    ACP_APPROVAL_NAMESPACE,
    ApprovalQueuePermissionBridge,
    command_prefix,
    remember_key,
)
from agentos.acp.types import PermissionRequest
from agentos.application.approval_queue import ApprovalQueue
from agentos.gateway.config import AcpConfig

SESSION = "agent:main:acp:0123abcd"

PROGRAMS = (
    "rm", "rmdir", "dd", "mkfs", "mkfs.ext4", "shred", "truncate", "chmod", "chown", "chgrp",
    "python", "python3", "python3.12", "py", "node", "deno", "bun", "ruby", "perl", "php",
    "sh", "bash", "zsh", "fish", "dash", "pwsh", "powershell", "cmd",
    "uv", "uvx", "pipx", "npx", "npm", "pnpm", "yarn", "make", "cargo", "go",
    "docker", "podman", "kubectl", "curl", "wget", "git", "ls", "find",
)  # fmt: skip

#: Each family the rule names, then the spellings that used to slip past a
#: program-plus-first-token key (a flag in front of the subcommand).
ALWAYS_ASK = {
    "destructive": [
        "rm -rf build",
        "rm notes.txt",
        "rmdir build",
        "dd if=a.img of=b.img",
        "mkfs -t ext4 disk.img",
        "mkfs.ext4 disk.img",
        "shred secrets.txt",
        "truncate -s 0 app.log",
    ],
    "recursive-ownership": [
        "chmod -R 777 build",
        "chmod -fR 777 build",
        "chmod --recursive 777 build",
        "chown -R nobody build",
        "chgrp -R staff build",
    ],
    "interpreters": [
        "python x.py",
        "python3 -c pass",
        "python3.12 -m pip --version",
        "py -3 x.py",
        "node x.js",
        "deno run x.ts",
        "bun x.ts",
        "ruby x.rb",
        "perl x.pl",
        "php x.php",
    ],
    "shells": [
        "sh x.sh",
        "bash x.sh",
        "zsh x.sh",
        "fish x.fish",
        "dash x.sh",
        "pwsh x.ps1",
        "powershell x.ps1",
        "cmd /c build",
    ],
    "runners": [
        "uv run pytest",
        "uv --quiet run pytest",
        "uv tool run ruff",
        "uvx ruff",
        "pipx run cowsay",
        "npx cowsay",
        "npm exec cowsay",
        "npm run build",
        "npm test",
        "pnpm dlx cowsay",
        "pnpm exec cowsay",
        "yarn dlx cowsay",
        "make",
        "make all",
        "cargo run",
        "cargo +nightly run",
        "go run .",
        "docker run alpine",
        "docker ps",
        "podman run alpine",
        "kubectl get pods",
        "curl https://example.invalid/x",
        "wget https://example.invalid/x",
    ],
    "git": [
        "git push",
        "git push --force origin main",
        "git reset --hard",
        "git clean -fdx",
        "git checkout -- notes.txt",
        "git checkout .",
        "git restore notes.txt",
        "git -C . push",
        "git --no-pager push",
        "git -c core.pager=less log",
        "git switch -f main",
        "git stash drop",
    ],
    "find": ["find . -delete", "find . -name x -execdir true"],
}

CASES = [
    pytest.param(cmd, id=f"{family}:{cmd}") for family, cmds in ALWAYS_ASK.items() for cmd in cmds
]

#: Still rememberable: the rule is about programs, not every command.
STILL_REMEMBERED = [
    "git status",
    "git log --oneline",
    "git switch main",
    "ls -la",
    "chmod +x build.sh",
    "npm install",
    "uv sync",
    "cargo build",
    "go build ./...",
    "find . -name x",
]


def _make_exe(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (f"{name}.exe" if os.name == "nt" else name)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def bin_dir(tmp_path, monkeypatch) -> Path:
    trusted = tmp_path / "bin"
    for name in PROGRAMS:
        _make_exe(trusted, name)
    monkeypatch.setenv("PATH", str(trusted))
    if os.name == "nt":
        monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    monkeypatch.chdir(tmp_path)
    return trusted


def _exec(command: str) -> PermissionRequest:
    return perm_request(title=command, raw_input={"command": command})


async def _prompts(bridge: ApprovalQueuePermissionBridge, queue: ApprovalQueue, req: Any) -> bool:
    """Whether ``req`` reached the operator (the row is then denied)."""
    seen = {row["id"] for row in queue.list_pending(ACP_APPROVAL_NAMESPACE)}
    task = asyncio.create_task(bridge.decide(req))
    for _ in range(400):
        if task.done():
            assert await task == "allow_once"
            return False
        fresh = [r for r in queue.list_pending(ACP_APPROVAL_NAMESPACE) if r["id"] not in seen]
        if fresh:
            queue.resolve(fresh[0]["id"], False)
            assert await task == "reject_once"
            return True
        await asyncio.sleep(0.005)
    raise AssertionError("decide() neither prompted nor answered")


async def _always_allow(
    bridge: ApprovalQueuePermissionBridge, queue: ApprovalQueue, req: PermissionRequest
) -> None:
    seen = {row["id"] for row in queue.list_pending(ACP_APPROVAL_NAMESPACE)}
    task = asyncio.create_task(bridge.decide(req))
    row = await next_pending(queue, ACP_APPROVAL_NAMESPACE, seen=seen)
    queue.resolve(row["id"], True, allow_always=True, remember_intent=True)
    assert await task == "allow_once"


def _legacy_key(bin_dir: Path, command: str) -> str:
    """The key the previous rule remembered: resolved program + first token."""
    argv = command.split()
    name = f"{argv[0]}.exe" if os.name == "nt" else argv[0]
    path = os.path.normcase(os.path.normpath(str(bin_dir / name)))
    if len(argv) > 1 and re.fullmatch(r"[A-Za-z][\w.:-]*", argv[1]):
        return f"{path} {argv[1]}"
    return path


@pytest.mark.parametrize("command", CASES)
async def test_always_allow_is_not_remembered(tmp_path, bin_dir, command) -> None:
    assert command_prefix(command) is None
    queue = make_queue(tmp_path)
    bridge = ApprovalQueuePermissionBridge(AcpConfig(), queue=queue)
    bridge.bind_session(SESSION, interactive=True)
    await _always_allow(bridge, queue, _exec(command))
    assert bridge.remembered(SESSION) == set()
    assert await _prompts(bridge, queue, _exec(command))
    queue.close()


@pytest.mark.parametrize("command", CASES)
async def test_a_grant_remembered_before_the_rule_stops_matching(
    tmp_path, bin_dir, command
) -> None:
    queue = make_queue(tmp_path)
    bridge = ApprovalQueuePermissionBridge(AcpConfig(), queue=queue)
    bridge.bind_session(SESSION, interactive=True)
    # What an Always on this command (or on ``git --version`` / ``uv
    # --version`` / ``cargo build`` ...) stored under the previous rule.
    bridge._remembered[SESSION] = {
        ("execute", _legacy_key(bin_dir, command)),
        ("execute", _legacy_key(bin_dir, command.split()[0])),
    }
    assert await _prompts(bridge, queue, _exec(command))
    queue.close()


async def test_a_shell_wrapped_command_is_judged_by_the_command_inside(tmp_path, bin_dir) -> None:
    queue = make_queue(tmp_path)
    bridge = ApprovalQueuePermissionBridge(AcpConfig(), queue=queue)
    for inner in ("python x.py", "rm -rf build", "git push", "npx cowsay"):
        req = perm_request(raw_input={"command": ["bash", "-lc", inner]})
        assert remember_key(req) is None, inner
        await _always_allow(bridge, queue, req)
        assert bridge.remembered(SESSION) == set()
    queue.close()


@pytest.mark.parametrize("command", STILL_REMEMBERED)
async def test_other_programs_are_still_remembered(tmp_path, bin_dir, command) -> None:
    queue = make_queue(tmp_path)
    bridge = ApprovalQueuePermissionBridge(AcpConfig(), queue=queue)
    await _always_allow(bridge, queue, _exec(command))
    assert bridge.remembered(SESSION) != set()
    assert not await _prompts(bridge, queue, _exec(command))
    queue.close()


async def test_an_older_flag_only_grant_does_not_cover_a_subcommand_after_a_flag(
    tmp_path, bin_dir
) -> None:
    queue = make_queue(tmp_path)
    bridge = ApprovalQueuePermissionBridge(AcpConfig(), queue=queue)
    # ``git --version`` remembers the bare program, which also keys
    # ``git -C . push``; the push must still ask.
    await _always_allow(bridge, queue, _exec("git --version"))
    assert not await _prompts(bridge, queue, _exec("git --version"))
    assert await _prompts(bridge, queue, _exec("git -C . push --force"))
    queue.close()


def test_other_kind_titled_with_an_always_ask_program_is_not_kind_wide(bin_dir) -> None:
    no_input = perm_request(tool_kind="other", title="rm -rf build && true", raw_input={})
    assert remember_key(no_input) is None
    plain = perm_request(tool_kind="other", title="Refresh the index", raw_input={})
    assert remember_key(plain) == ("other", "")


@pytest.mark.parametrize("suffix", [".exe", ".cmd", ".bat", ".ps1", ".EXE", ".Cmd"])
def test_windows_launcher_suffixes_do_not_hide_the_program(bin_dir, suffix) -> None:
    for name in ("npx", "python", "rm"):
        launcher = bin_dir / f"{name}{suffix}"
        launcher.write_text("@echo off\n")
        launcher.chmod(0o755)
        assert command_prefix(f"{launcher.as_posix()} x") is None, launcher.name


def test_program_case_does_not_hide_the_program(bin_dir) -> None:
    # A new file on a case-sensitive filesystem, python3 itself elsewhere.
    exe = _make_exe(bin_dir, "PYTHON3")
    assert command_prefix(f"{exe.as_posix()} x.py") is None


def test_a_link_to_an_interpreter_is_the_interpreter(tmp_path, bin_dir) -> None:
    target = bin_dir / ("python3.exe" if os.name == "nt" else "python3")
    link = tmp_path / "links" / ("ls.exe" if os.name == "nt" else "ls")
    link.parent.mkdir()
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert command_prefix(f"{link.as_posix()} -c pass") is None
