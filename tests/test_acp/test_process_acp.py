"""ACP harness process layer: env scrubbing, leases, stderr ring, terminate,
the boot reaper and Codex home isolation."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from acp_runtime_helpers import FAKE_AGENT, wait_until

from agentos.acp import process as proc_mod
from agentos.acp.registry import ResolvedLaunch
from agentos.acp.types import AcpSessionInitError

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")


# -- env -----------------------------------------------------------------


def test_scrub_env_drops_agentos_and_secrets_keeps_harness_credentials() -> None:
    base = {
        "PATH": "/bin",
        "HOME": "/home/u",
        "LANG": "C.UTF-8",
        "TERM": "xterm",
        "AGENTOS_GATEWAY_TOKEN": "x",
        "AGENTOS_STATE_DIR": "/s",
        "GATEWAY_SECRET": "x",
        "SLACK_BOT_TOKEN": "x",
        "TELEGRAM_BOT_TOKEN": "x",
        "VAULT_PASSWORD": "x",
        "WEBHOOK_SECRET": "x",
        "my_token": "x",
        "ANTHROPIC_API_KEY": "keep",
        "OPENAI_API_KEY": "keep",
        "CLAUDE_CODE_OAUTH_TOKEN": "keep",
        "GITHUB_TOKEN": "keep",
        "EDITOR": "vim",
    }
    env = proc_mod.scrub_env(base)
    for gone in (
        "AGENTOS_GATEWAY_TOKEN",
        "AGENTOS_STATE_DIR",
        "GATEWAY_SECRET",
        "SLACK_BOT_TOKEN",
        "TELEGRAM_BOT_TOKEN",
        "VAULT_PASSWORD",
        "WEBHOOK_SECRET",
        "my_token",
    ):
        assert gone not in env
    for kept in (
        "PATH",
        "HOME",
        "LANG",
        "TERM",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "GITHUB_TOKEN",
        "EDITOR",
    ):
        assert env[kept] == base[kept]
    assert "OPENAI_API_KEY" not in proc_mod.scrub_env(base, drop={"OPENAI_API_KEY"})
    assert proc_mod.scrub_env(base, keep={"VAULT_PASSWORD"})["VAULT_PASSWORD"] == "x"


def test_harness_env_adds_lease_marker_and_tokio_cap() -> None:
    env = proc_mod.harness_env(
        lease_id="L1",
        base={"PATH": "/bin", "AGENTOS_X": "1"},
        extra={"FOO": "bar", "AGENTOS_ACP_LEASE": "spoofed"},
    )
    assert env["AGENTOS_ACP_LEASE"] == "L1"
    assert 1 <= int(env["TOKIO_WORKER_THREADS"]) <= 8
    assert env["FOO"] == "bar"
    assert "AGENTOS_X" not in env


# -- leases --------------------------------------------------------------


def test_lease_roundtrip(tmp_path: Path) -> None:
    lease = proc_mod.Lease(
        lease_id="abc", argv=["x", "y"], cwd="/w", session_key="k", gateway_instance_id="g", pid=1
    )
    path = proc_mod.write_lease(tmp_path, lease)
    assert path == tmp_path / "leases" / "abc.json"
    if os.name != "nt":  # Windows has no POSIX modes to check
        assert oct(path.stat().st_mode & 0o777) == oct(0o600)
    got = proc_mod.read_lease(tmp_path, "abc")
    assert got is not None and got.to_dict() == lease.to_dict()
    assert [x.lease_id for x in proc_mod.list_leases(tmp_path)] == ["abc"]
    proc_mod.remove_lease(tmp_path, "abc")
    proc_mod.remove_lease(tmp_path, "abc")  # idempotent
    assert proc_mod.list_leases(tmp_path) == []


def test_parse_etime() -> None:
    assert proc_mod.parse_etime("00:05") == 5
    assert proc_mod.parse_etime("01:02:03") == 3723
    assert proc_mod.parse_etime("2-00:00:01") == 2 * 86400 + 1
    assert proc_mod.parse_etime("") is None
    assert proc_mod.parse_etime("x:y") is None


def test_command_matches_lease() -> None:
    npx = ["npx", "-y", "@agentclientprotocol/claude-agent-acp@^0.91.0"]
    assert proc_mod.command_matches_lease(
        "npm exec @agentclientprotocol/claude-agent-acp@^0.91.0", npx
    )
    assert proc_mod.command_matches_lease("/usr/local/bin/codex-acp --flag", ["codex-acp"])
    assert proc_mod.command_matches_lease(
        f"/usr/bin/python3 {FAKE_AGENT}", ["/opt/venv/bin/python3", FAKE_AGENT]
    )
    assert not proc_mod.command_matches_lease("/usr/sbin/sshd -D", npx)
    assert not proc_mod.command_matches_lease("", npx)


# -- stderr ring -------------------------------------------------------------


def test_stderr_ring_redacts_and_stays_bounded(tmp_path: Path) -> None:
    ring = proc_mod.StderrRing(tmp_path / "logs" / "x.stderr.log", max_bytes=2048)
    ring.write_line(b"OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz0123456789\n")
    content = (tmp_path / "logs" / "x.stderr.log").read_text()
    assert "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789" not in content
    for i in range(500):
        ring.write_line(f"line {i:04d} ".encode() + b"x" * 40)
    blob = (tmp_path / "logs" / "x.stderr.log").read_bytes()
    assert len(blob) <= 2048
    assert b"line 0499" in blob
    assert b"line 0000" not in blob
    assert "line 0499" in ring.tail(1)


# -- launch / terminate --------------------------------------------------------


def _resolved(argv: list[str]) -> ResolvedLaunch:
    return ResolvedLaunch(agent_id="fake", argv=argv, source="override")


@posix_only
async def test_launch_writes_lease_and_terminate_releases_it(tmp_path: Path) -> None:
    env = proc_mod.harness_env(lease_id="L1", base={"PATH": os.environ["PATH"]}, extra={})
    env["FAKE_ACP_STDERR_SECRET"] = "1"
    hp = await proc_mod.launch_harness(
        _resolved([sys.executable, FAKE_AGENT]),
        cwd=str(tmp_path),
        env=env,
        state_dir=tmp_path,
        lease_id="L1",
        session_key="agent:main:acp:1",
        gateway_instance_id="gw",
        record_id="rec1",
    )
    lease = proc_mod.read_lease(tmp_path, "L1")
    assert lease is not None and lease.pid == hp.pid and lease.pgid == hp.pid
    assert lease.session_key == "agent:main:acp:1" and lease.gateway_instance_id == "gw"
    log = tmp_path / "logs" / "rec1.stderr.log"
    assert await wait_until(lambda: log.exists() and "fake-acp starting" in log.read_text())
    assert "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789" not in log.read_text()
    assert os.getpgid(hp.pid) == hp.pid  # own process group
    await proc_mod.terminate(hp, grace_s=1.0)
    assert not hp.alive
    assert proc_mod.read_lease(tmp_path, "L1") is None


async def test_launch_failure_raises_and_leaves_no_lease(tmp_path: Path) -> None:
    with pytest.raises(AcpSessionInitError) as ei:
        await proc_mod.launch_harness(
            _resolved([str(tmp_path / "does-not-exist")]),
            cwd=str(tmp_path),
            env={"PATH": os.environ["PATH"]},
            state_dir=tmp_path,
            lease_id="L2",
        )
    assert ei.value.code == "ACP_SPAWN_FAILED"
    assert proc_mod.list_leases(tmp_path) == []


_STUBBORN = (
    "import signal, subprocess, sys, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
    "print(child.pid, flush=True)\n"
    "time.sleep(30)\n"
)


@posix_only
async def test_terminate_escalates_to_sigkill_and_takes_the_group(tmp_path: Path) -> None:
    hp = await proc_mod.launch_harness(
        _resolved([sys.executable, "-c", _STUBBORN]),
        cwd=str(tmp_path),
        env={"PATH": os.environ["PATH"]},
        state_dir=tmp_path,
        lease_id="L3",
    )
    grandchild = int((await asyncio.wait_for(hp.stdout.readline(), 10)).decode().strip())
    assert proc_mod.pid_alive(grandchild)
    rc = await proc_mod.terminate(hp, grace_s=0.1)
    assert rc == -signal.SIGKILL
    assert await wait_until(lambda: not _running(grandchild), timeout=3)
    assert proc_mod.read_lease(tmp_path, "L3") is None


@posix_only
async def test_close_gracefully_exits_clean_on_stdin_eof(tmp_path: Path) -> None:
    hp = await proc_mod.launch_harness(
        _resolved([sys.executable, FAKE_AGENT]),
        cwd=str(tmp_path),
        env={"PATH": os.environ["PATH"]},
        state_dir=tmp_path,
        lease_id="L4",
    )
    rc = await proc_mod.close_gracefully(hp, wait_s=5.0, grace_s=0.2)
    assert rc == 0
    assert proc_mod.read_lease(tmp_path, "L4") is None


def _running(pid: int) -> bool:
    """Alive and not a zombie (a reaped-later child still answers kill 0)."""
    if not proc_mod.pid_alive(pid):
        return False
    out = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
    ).stdout.strip()
    return bool(out) and not out.startswith("Z")


# -- reaper -----------------------------------------------------------------------


@posix_only
async def test_reaper_kills_stale_tree_and_removes_lease(tmp_path: Path) -> None:
    hp = await proc_mod.launch_harness(
        _resolved([sys.executable, "-c", _STUBBORN]),
        cwd=str(tmp_path),
        env={"PATH": os.environ["PATH"], proc_mod.LEASE_ENV: "ignored"},
        state_dir=tmp_path,
        lease_id="stale1",
        gateway_instance_id="old-gateway",
    )
    grandchild = int((await asyncio.wait_for(hp.stdout.readline(), 10)).decode().strip())
    result = await proc_mod.reap_stale_leases(
        tmp_path, current_instance_id="new-gateway", grace_s=0.1
    )
    assert "stale1" in result["inspected"]
    assert hp.pid in result["terminated"] and grandchild in result["terminated"]
    # leaves first: the grandchild is signalled before its parent
    assert result["terminated"].index(grandchild) < result["terminated"].index(hp.pid)
    assert await hp.wait(5) is not None
    assert await wait_until(lambda: not _running(grandchild), timeout=3)
    assert proc_mod.list_leases(tmp_path) == []


@posix_only
def test_list_processes_reads_full_command_despite_columns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A narrow COLUMNS used to make ps cut the command column, so a harness
    # with a long argv[0] path lost its distinctive basename and the reaper
    # mistook it for a reused PID.
    monkeypatch.setenv("COLUMNS", "80")
    nested = tmp_path.joinpath(*(f"deeply-nested-directory-level-{i:02d}" for i in range(8)))
    nested.mkdir(parents=True)
    link = nested / "acp-harness-long-path-python"
    link.symlink_to(os.path.realpath(sys.executable))
    marker_arg = str(nested / "distinctive-harness-argument")
    argv = [str(link), "-c", "import time; time.sleep(30)", marker_arg]
    child = subprocess.Popen(argv)
    try:
        rows = proc_mod.list_processes()
        assert rows is not None
        command = next(r.command for r in rows if r.pid == child.pid)
        assert len(command) > 80
        assert command.startswith(str(link))
        assert command.endswith(marker_arg)
        assert proc_mod.command_matches_lease(command, [str(link), marker_arg])
    finally:
        child.kill()
        child.wait()


@posix_only
async def test_reaper_never_kills_a_pid_running_something_else(tmp_path: Path) -> None:
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        proc_mod.write_lease(
            tmp_path,
            proc_mod.Lease(
                lease_id="reused",
                argv=["claude-agent-acp-not-this"],
                cwd="/",
                session_key="k",
                gateway_instance_id="old",
                pid=other.pid,
            ),
        )
        result = await proc_mod.reap_stale_leases(tmp_path, current_instance_id="new", grace_s=0.1)
        assert result["terminated"] == []
        assert "reused" in result["removed"]
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()


@posix_only
async def test_reaper_drops_dead_and_skips_own_leases(tmp_path: Path) -> None:
    proc_mod.write_lease(
        tmp_path,
        proc_mod.Lease(
            lease_id="dead",
            argv=["x"],
            cwd="/",
            session_key="",
            gateway_instance_id="old",
            pid=2**22 + 12345,
        ),
    )
    proc_mod.write_lease(
        tmp_path,
        proc_mod.Lease(
            lease_id="nopid", argv=["x"], cwd="/", session_key="", gateway_instance_id="old"
        ),
    )
    proc_mod.write_lease(
        tmp_path,
        proc_mod.Lease(
            lease_id="mine",
            argv=["x"],
            cwd="/",
            session_key="",
            gateway_instance_id="me",
            pid=os.getpid(),
        ),
    )
    result = await proc_mod.reap_stale_leases(tmp_path, current_instance_id="me")
    assert sorted(result["removed"]) == ["dead", "nopid"]
    assert result["terminated"] == []
    assert [x.lease_id for x in proc_mod.list_leases(tmp_path)] == ["mine"]


@posix_only
async def test_reaper_start_time_mismatch_is_not_killed(tmp_path: Path) -> None:
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        proc_mod.write_lease(
            tmp_path,
            proc_mod.Lease(
                lease_id="old-start",
                argv=[sys.executable, "-c"],
                cwd="/",
                session_key="",
                gateway_instance_id="old",
                pid=other.pid,
                started_at=1_000_000.0,  # 1970: cannot be this process
            ),
        )
        result = await proc_mod.reap_stale_leases(tmp_path, current_instance_id="new", grace_s=0.1)
        assert result["terminated"] == []
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()


# -- codex home ------------------------------------------------------------------


def test_prepare_codex_home_inherits_only_safe_keys_and_trusts_cwd(tmp_path: Path) -> None:
    user = tmp_path / "user-codex"
    user.mkdir()
    (user / "auth.json").write_text('{"token": "t"}')
    (user / "config.toml").write_text(
        'model = "gpt-5-codex"\n'
        'model_provider = "corp"\n'
        'approval_policy = "never"\n'
        'sandbox_mode = "workspace-write"\n'
        "[model_providers.corp]\n"
        'name = "Corp"\n'
        'base_url = "https://llm.corp/v1"\n'
        'http_headers = { Authorization = "Bearer nope" }\n'
        "[mcp_servers.fs]\n"
        'command = "fs-mcp"\n'
        '[projects."/already/trusted"]\n'
        'trust_level = "trusted"\n'
        '[projects."/untrusted"]\n'
        'trust_level = "untrusted"\n'
    )
    state = tmp_path / "state"
    home = proc_mod.prepare_codex_home(state, str(tmp_path / "repo"), user_codex_home=user)
    assert home == state / "codex-home"
    auth = home / "auth.json"
    assert auth.read_text() == '{"token": "t"}'
    if os.name != "nt":  # Windows has no POSIX modes to check
        assert oct(auth.stat().st_mode & 0o777) == oct(0o600)
    cfg = tomllib.loads((home / "config.toml").read_text())
    assert cfg["model"] == "gpt-5-codex"
    assert cfg["model_provider"] == "corp"
    assert cfg["sandbox_mode"] == "workspace-write"
    assert "approval_policy" not in cfg
    assert "mcp_servers" not in cfg
    assert cfg["model_providers"] == {"corp": {"name": "Corp", "base_url": "https://llm.corp/v1"}}
    projects = cfg["projects"]
    assert projects[str(tmp_path / "repo")] == {"trust_level": "trusted"}
    # Trusted paths are normalized with abspath, which adds a drive on Windows.
    assert projects[os.path.abspath("/already/trusted")] == {"trust_level": "trusted"}
    assert os.path.abspath("/untrusted") not in projects

    # a second session in another cwd keeps the first one trusted
    proc_mod.prepare_codex_home(state, str(tmp_path / "repo2"), user_codex_home=user)
    projects = tomllib.loads((home / "config.toml").read_text())["projects"]
    assert str(tmp_path / "repo") in projects and str(tmp_path / "repo2") in projects


def test_prepare_codex_home_without_user_config(tmp_path: Path) -> None:
    home = proc_mod.prepare_codex_home(tmp_path / "s", "/w", user_codex_home=tmp_path / "missing")
    assert not (home / "auth.json").exists()
    cfg = tomllib.loads((home / "config.toml").read_text())
    assert cfg == {"projects": {os.path.abspath("/w"): {"trust_level": "trusted"}}}


def test_prepare_codex_home_rejects_invalid_toml(tmp_path: Path) -> None:
    user = tmp_path / "u"
    user.mkdir()
    (user / "config.toml").write_text("model = [unterminated\n")
    with pytest.raises(AcpSessionInitError) as ei:
        proc_mod.prepare_codex_home(tmp_path / "s", "/w", user_codex_home=user)
    assert ei.value.code == "ACP_CODEX_CONFIG_INVALID"
