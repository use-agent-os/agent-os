"""agent-browser adapter: arg building, env scrub, session registry, lifecycle.

Real tests: they spawn a fake ``agent-browser`` script (a real subprocess on
disk speaking the JSON contract), so the adapter's spawn/parse path is exercised
end to end without Chromium.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_tools.browser_fake_engine import posix_only, write_fake_engine

from agentos.tools import agent_browser

# A fake agent-browser: records each invocation (argv + a slice of its env) to
# $FAKE_AB_LOG, then emits the real JSON shape for the requested command.
_FAKE_ENGINE = """#!/usr/bin/env python3
import json, os, sys

argv = sys.argv[1:]
# Split flags from the command. Value-flags consume the next token.
value_flags = {"--session", "--cdp", "--allowed-domains", "--session-name", "--max-output"}
i = 0
session = ""
cmd = None
rest = []
while i < len(argv):
    tok = argv[i]
    if tok in value_flags:
        val = argv[i + 1] if i + 1 < len(argv) else ""
        if tok == "--session":
            session = val
        i += 2
        continue
    if tok.startswith("--"):
        i += 1
        continue
    cmd = tok
    rest = argv[i + 1:]
    break

# Write next to ourselves — the scrubbed subprocess env can't carry a log-path
# variable, so derive it from the script location instead.
log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "invocations.log")
sentinels = ["AGENTOS_GATEWAY_TOKEN", "OPENROUTER_API_KEY", "XAI_API_KEY", "PATH", "HOME"]
with open(log_path, "a") as fh:
    fh.write(json.dumps({
        "argv": argv,
        "command": cmd,
        "rest": rest,
        "session": session,
        "env_present": {k: (k in os.environ) for k in sentinels},
    }) + "\\n")


def out(data):
    print(json.dumps({"success": True, "data": data, "error": None}))


if cmd == "open":
    out({"title": "Fake Title", "url": rest[0] if rest else ""})
elif cmd == "snapshot":
    out({"origin": "http://example.com", "refs": {"e1": {"name": "Go", "role": "button"}},
         "snapshot": "- button \\"Go\\" [ref=e1]"})
elif cmd == "eval":
    out({"result": "Fake Title"})
elif cmd == "get":
    out({"cdpUrl": "ws://127.0.0.1:53870/devtools/browser/abc"})
elif cmd == "close":
    out({"closed": True})
elif cmd in ("click", "type", "fill", "select", "press", "scroll", "wait", "back",
             "screenshot", "tab"):
    out({"ok": True, "command": cmd, "args": rest})
else:
    print(json.dumps({"success": False, "data": None, "error": "unknown command: %s" % cmd}))
    sys.exit(1)
"""


@pytest.fixture
def fake_engine(tmp_path: Path) -> tuple[str, Path]:
    """Write the fake engine to disk and return (binary_path, log_path)."""
    binary = write_fake_engine(tmp_path, _FAKE_ENGINE)
    log = tmp_path / "invocations.log"
    return binary, log


@pytest.fixture(autouse=True)
def _reset() -> Any:
    agent_browser.reset_browser_runtime()
    agent_browser.close_all_sessions()
    yield
    agent_browser.close_all_sessions()
    agent_browser.reset_browser_runtime()


def _config(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "enabled": True,
        "headless": True,
        "binary_path": "",
        "cdp_port": 0,
        "persist_profile": False,
        "session_ttl_minutes": 15,
        "max_sessions": 3,
        "allowed_domains": [],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _read_log(log: Path) -> list[dict[str, Any]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


class TestAvailability:
    def test_hidden_when_disabled(self, fake_engine: tuple[str, Path]) -> None:
        binary, _ = fake_engine
        agent_browser.configure_browser(_config(enabled=False, binary_path=binary))
        assert agent_browser.browser_available() is False

    def test_visible_with_binary(self, fake_engine: tuple[str, Path]) -> None:
        binary, _ = fake_engine
        agent_browser.configure_browser(_config(binary_path=binary))
        assert agent_browser.browser_available() is True

    def test_hidden_without_binary(self, tmp_path: Path) -> None:
        agent_browser.configure_browser(_config(binary_path=str(tmp_path / "nope")))
        # Falls back to PATH lookup; on a machine without agent-browser this is
        # False. If the real binary is installed, availability is True — so only
        # assert the explicit-missing-path case does not resolve to that path.
        assert agent_browser.resolve_binary() != str(tmp_path / "nope")


def _argv_for(command: str = "snapshot", args: list[str] | None = None) -> list[str]:
    """Build the argv the adapter would spawn, without spawning it.

    Asserting on the built command instead of on a log the fake engine writes
    keeps these checks honest on every platform: Windows cannot execute a
    shebang script, so the spawn-based version of these tests failed there while
    passing on POSIX.
    """
    session = agent_browser.get_or_create_session("sess")
    return agent_browser._command_line("agent-browser", session, command, args or [])


class TestArgBuilding:
    def test_managed_mode_flags(self) -> None:
        agent_browser.configure_browser(_config(binary_path=""))
        argv = _argv_for()
        assert "--session" in argv
        assert "--json" in argv
        assert "--no-auto-dialog" in argv
        assert "--cdp" not in argv
        assert argv[argv.index("--session") + 1].startswith("agentos-")

    def test_attach_mode_passes_int_port(self) -> None:
        agent_browser.configure_browser(_config(binary_path="", cdp_port=9222))
        assert agent_browser.is_attach_mode() is True
        argv = _argv_for()
        assert "--cdp" in argv
        assert argv[argv.index("--cdp") + 1] == "9222"

    def test_headed_flag_when_not_headless(self) -> None:
        agent_browser.configure_browser(_config(binary_path="", headless=False))
        assert "--headed" in _argv_for()

    def test_allowed_domains_passed_to_engine(self) -> None:
        agent_browser.configure_browser(
            _config(binary_path="", allowed_domains=["example.com", "test.org"])
        )
        argv = _argv_for()
        assert "--allowed-domains" in argv
        assert argv[argv.index("--allowed-domains") + 1] == "example.com,test.org"

    def test_persist_profile_uses_session_name(self) -> None:
        agent_browser.configure_browser(_config(binary_path="", persist_profile=True))
        assert "--session-name" in _argv_for()

    def test_no_session_name_by_default(self) -> None:
        agent_browser.configure_browser(_config(binary_path=""))
        assert "--session-name" not in _argv_for()


class TestEnvScrub:
    def test_scrubbed_env_excludes_agentos_and_provider_secrets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Asserted on the built environment, so it holds on every platform —
        the spawn-based check below can only run where the fake engine is
        executable."""
        monkeypatch.setenv("AGENTOS_GATEWAY_TOKEN", "secret-token")
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-provider")
        monkeypatch.setenv("XAI_API_KEY", "xai-key")
        monkeypatch.setenv("AGENTOS_STRIP_PROVIDER_ENV", "1")
        from agentos.tools.env_passthrough import reset_managed_credentials_cache

        reset_managed_credentials_cache()

        env = agent_browser._scrubbed_env()

        assert "AGENTOS_GATEWAY_TOKEN" not in env
        assert "OPENROUTER_API_KEY" not in env
        assert "XAI_API_KEY" not in env
        assert "PATH" in env
        # Nothing outside the allowlist leaks in either.
        assert set(env) <= set(agent_browser._ENV_PASSTHROUGH_NAMES)

    def test_windows_essentials_are_on_the_allowlist(self) -> None:
        """A Windows child without SystemRoot/COMSPEC often cannot start."""
        names = {n.upper() for n in agent_browser._ENV_PASSTHROUGH_NAMES}
        assert {"SYSTEMROOT", "COMSPEC", "PATHEXT"} <= names

    @posix_only
    @pytest.mark.asyncio
    async def test_gateway_token_and_provider_keys_stripped(
        self, fake_engine: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        binary, log = fake_engine
        monkeypatch.setenv("AGENTOS_GATEWAY_TOKEN", "secret-token")
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-provider")
        monkeypatch.setenv("XAI_API_KEY", "xai-key")
        monkeypatch.setenv("AGENTOS_STRIP_PROVIDER_ENV", "1")
        # Reset the managed-credentials cache so the new env is seen.
        from agentos.tools.env_passthrough import reset_managed_credentials_cache

        reset_managed_credentials_cache()
        agent_browser.configure_browser(_config(binary_path=binary))
        await agent_browser.run_command("sess", "snapshot")
        env_present = _read_log(log)[0]["env_present"]
        assert env_present["AGENTOS_GATEWAY_TOKEN"] is False
        assert env_present["OPENROUTER_API_KEY"] is False
        assert env_present["XAI_API_KEY"] is False
        # PATH/HOME are legitimately passed through.
        assert env_present["PATH"] is True


class TestSessionRegistry:
    @pytest.mark.asyncio
    async def test_session_reused_across_commands(self, fake_engine: tuple[str, Path]) -> None:
        binary, _ = fake_engine
        agent_browser.configure_browser(_config(binary_path=binary))
        s1 = agent_browser.get_or_create_session("sess")
        s2 = agent_browser.get_or_create_session("sess")
        assert s1 is s2
        assert agent_browser.active_session_count() == 1

    @pytest.mark.asyncio
    async def test_max_sessions_evicts_oldest(self, fake_engine: tuple[str, Path]) -> None:
        binary, _ = fake_engine
        agent_browser.configure_browser(_config(binary_path=binary, max_sessions=2))
        agent_browser.get_or_create_session("a")
        agent_browser.get_or_create_session("b")
        agent_browser.get_or_create_session("c")
        assert agent_browser.active_session_count() <= 2
        # The oldest ('a') should have been evicted.
        assert agent_browser.get_session("a") is None

    def test_reap_idle_sessions(self, fake_engine: tuple[str, Path]) -> None:
        binary, _ = fake_engine
        agent_browser.configure_browser(_config(binary_path=binary, session_ttl_minutes=15))
        session = agent_browser.get_or_create_session("old")
        session.last_used_at = 0.0  # ancient
        reaped = agent_browser.reap_idle_sessions(now=10_000_000)
        assert "old" in reaped
        assert agent_browser.get_session("old") is None

    def test_close_session(self, fake_engine: tuple[str, Path]) -> None:
        binary, _ = fake_engine
        agent_browser.configure_browser(_config(binary_path=binary))
        agent_browser.get_or_create_session("x")
        assert agent_browser.close_session("x") is True
        assert agent_browser.close_session("x") is False

    def test_engine_session_name_is_stable_across_processes(
        self, fake_engine: tuple[str, Path]
    ) -> None:
        """The name must not come from ``hash()``, which is salted per process:
        a gateway restart would mint a new name and orphan the old browser."""
        binary, _ = fake_engine
        agent_browser.configure_browser(_config(binary_path=binary))
        name = agent_browser.get_or_create_session("agent:main:webchat:abc").engine_session_name

        script = (
            "from agentos.tools import agent_browser as ab;"
            "ab.configure_browser(None);"
            "print(ab.get_or_create_session('agent:main:webchat:abc').engine_session_name)"
        )
        out = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": "12345"},
        )
        # structlog also writes to stdout; take the bare name line.
        printed = [
            line.strip()
            for line in out.stdout.splitlines()
            if line.strip().startswith("agentos-") and " " not in line.strip()
        ]
        assert printed, f"child printed no session name: {out.stdout!r}"
        assert printed[-1] == name

    def test_dropping_a_session_stops_its_supervisor(self, fake_engine: tuple[str, Path]) -> None:
        """A dropped session must not leave the CDP supervisor's thread and
        WebSocket running."""
        from agentos.tools.browser_supervisor import SUPERVISOR_REGISTRY

        binary, _ = fake_engine
        agent_browser.configure_browser(_config(binary_path=binary))
        agent_browser.get_or_create_session("leaky")

        stopped: list[str] = []

        class _FakeTransport:
            def start(self, on_event: Any) -> None:
                return None

            def call(
                self,
                method: str,
                params: dict[str, Any],
                timeout: float,
                session_id: str | None = None,
            ) -> dict[str, Any]:
                return {}

            def stop(self) -> None:
                stopped.append("yes")

        SUPERVISOR_REGISTRY.get_or_start("leaky", "ws://127.0.0.1:1/x", transport=_FakeTransport())
        assert SUPERVISOR_REGISTRY.get("leaky") is not None

        agent_browser.close_session("leaky")
        assert SUPERVISOR_REGISTRY.get("leaky") is None
        assert stopped == ["yes"]

    def test_missing_binary_is_not_cached_forever(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The doctor's own fix step is `npm install -g agent-browser`; caching
        the miss would leave the tool hidden until a restart."""
        import shutil as shutil_mod

        monkeypatch.setattr(agent_browser, "_fallback_bin_dirs", lambda: [])
        monkeypatch.setattr(shutil_mod, "which", lambda _name, path=None: None)
        agent_browser.configure_browser(_config(binary_path=""))
        assert agent_browser.resolve_binary() is None
        # …operator installs it…
        installed = tmp_path / "agent-browser"
        installed.write_text("#!/bin/sh\nexit 0\n")
        installed.chmod(installed.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setattr(shutil_mod, "which", lambda _name, path=None: str(installed))
        assert agent_browser.resolve_binary() == str(installed)

    def test_changing_cdp_port_drops_live_sessions(self, fake_engine: tuple[str, Path]) -> None:
        """A session captured its mode at creation; flipping the port would
        otherwise leave it driving the managed browser under attach policy."""
        binary, _ = fake_engine
        agent_browser.configure_browser(_config(binary_path=binary, cdp_port=0))
        agent_browser.get_or_create_session("s")
        assert agent_browser.get_session("s") is not None

        agent_browser.configure_browser(_config(binary_path=binary, cdp_port=9222))
        assert agent_browser.get_session("s") is None

    def test_disabling_the_tool_drops_live_sessions(self, fake_engine: tuple[str, Path]) -> None:
        """Nothing calls in again once the tool is hidden, so the TTL sweep would
        never run and the browser would survive until shutdown."""
        binary, _ = fake_engine
        agent_browser.configure_browser(_config(binary_path=binary))
        agent_browser.get_or_create_session("s")
        agent_browser.configure_browser(_config(binary_path=binary, enabled=False))
        assert agent_browser.get_session("s") is None

    def test_idle_sessions_are_reaped_on_next_use(self, fake_engine: tuple[str, Path]) -> None:
        """Nothing runs a background reaper, so the TTL has to be enforced when
        a session is next requested — otherwise idle browsers pile up."""
        binary, _ = fake_engine
        agent_browser.configure_browser(_config(binary_path=binary, session_ttl_minutes=1))
        stale = agent_browser.get_or_create_session("stale")
        stale.last_used_at = 0.0
        agent_browser.get_or_create_session("fresh")
        assert agent_browser.get_session("stale") is None


@posix_only
class TestCommandExecution:
    @pytest.mark.asyncio
    async def test_open_returns_parsed_json(self, fake_engine: tuple[str, Path]) -> None:
        binary, _ = fake_engine
        agent_browser.configure_browser(_config(binary_path=binary))
        result = await agent_browser.run_command("sess", "open", ["http://example.com"])
        assert result["success"] is True
        assert result["data"]["title"] == "Fake Title"

    @pytest.mark.asyncio
    async def test_missing_binary_returns_install_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force resolution to the missing path by clearing PATH and fallback lookup.
        import shutil

        monkeypatch.setattr(agent_browser, "_fallback_bin_dirs", lambda: [])
        monkeypatch.setattr(shutil, "which", lambda _name, path=None: None)
        agent_browser.configure_browser(_config(binary_path=str(tmp_path / "missing")))
        result = await agent_browser.run_command("sess", "snapshot")
        assert result["success"] is False
        assert "agent-browser is not installed" in result["error"]

    @pytest.mark.asyncio
    async def test_managed_cdp_url_resolved_and_loopback_checked(
        self, fake_engine: tuple[str, Path]
    ) -> None:
        binary, _ = fake_engine
        agent_browser.configure_browser(_config(binary_path=binary))
        agent_browser.get_or_create_session("sess")
        endpoint = await agent_browser.resolve_cdp_endpoint("sess")
        assert endpoint == "ws://127.0.0.1:53870/devtools/browser/abc"

    def test_non_loopback_cdp_url_refused(self) -> None:
        assert agent_browser.is_loopback_cdp_url("ws://127.0.0.1:9/x") is True
        assert agent_browser.is_loopback_cdp_url("ws://10.0.0.5:9/x") is False
        assert agent_browser.is_loopback_cdp_url("ws://0.0.0.0:9/x") is False


class TestMaxSessionsOnReconfigure:
    """Lowering the cap has to act on sessions that are already running.

    Nothing else will: `_evict_if_over_cap` is only reached when a new session
    is created, and reusing an existing one refreshes `last_used_at` so the
    idle reaper never takes it. Each managed session is a Chromium process, and
    lowering this is how an operator relieves memory pressure.
    """

    def test_lowering_max_sessions_trims_the_live_set(self) -> None:
        agent_browser.configure_browser(_config(max_sessions=3))
        for key in ("s1", "s2", "s3"):
            agent_browser.get_or_create_session(key)
        assert agent_browser.active_session_count() == 3

        agent_browser.configure_browser(_config(max_sessions=1))

        assert agent_browser.active_session_count() == 1

    def test_the_most_recently_used_session_is_the_one_kept(self) -> None:
        """The docs say "oldest-idle evicted"; the survivor follows that order.

        The timestamps are assigned rather than produced by touching the
        sessions. `time.time()` has ~15.6ms resolution on Windows, so several
        rapid calls land inside one tick, every session ends up sharing a
        timestamp, and `min()` then breaks the tie on insertion order instead
        of recency -- which is a real property of the eviction on that
        platform, but not what this test is for.
        """
        agent_browser.configure_browser(_config(max_sessions=3))
        for key in ("s1", "s2", "s3"):
            agent_browser.get_or_create_session(key)
        agent_browser._sessions["s1"].last_used_at = 300.0  # newest
        agent_browser._sessions["s2"].last_used_at = 100.0  # oldest
        agent_browser._sessions["s3"].last_used_at = 200.0

        agent_browser.configure_browser(_config(max_sessions=1))

        assert list(agent_browser._sessions) == ["s1"]

    def test_raising_max_sessions_closes_nothing(self) -> None:
        agent_browser.configure_browser(_config(max_sessions=1))
        agent_browser.get_or_create_session("s1")

        agent_browser.configure_browser(_config(max_sessions=5))

        assert agent_browser.active_session_count() == 1

    def test_an_unchanged_cap_closes_nothing(self) -> None:
        agent_browser.configure_browser(_config(max_sessions=2))
        agent_browser.get_or_create_session("s1")
        agent_browser.get_or_create_session("s2")

        agent_browser.configure_browser(_config(max_sessions=2))

        assert agent_browser.active_session_count() == 2

    def test_creating_a_session_at_the_cap_still_makes_room_for_one(self) -> None:
        """`_evict_if_over_cap` compares with `>=` on purpose — keep it that way.

        Trimming after a config change needs `>` to land exactly on the cap;
        reusing that comparison here would evict one session too many.
        """
        agent_browser.configure_browser(_config(max_sessions=3))
        for key in ("s1", "s2", "s3"):
            agent_browser.get_or_create_session(key)

        agent_browser.get_or_create_session("s4")

        assert agent_browser.active_session_count() == 3
        assert "s4" in agent_browser._sessions


@posix_only
class TestObserveWithoutStarting:
    """``create=False`` / ``touch=False``: the operator's view of a session's browser."""

    @pytest.mark.asyncio
    async def test_no_session_spawns_nothing(self, fake_engine: tuple[str, Path]) -> None:
        binary, log = fake_engine
        agent_browser.configure_browser(_config(binary_path=binary))
        result = await agent_browser.run_command("s-view", "tab", ["list"], create=False)
        assert result == {
            "success": False,
            "code": "not_running",
            "error": "No browser is running for this session.",
        }
        assert agent_browser.get_session("s-view") is None
        assert _read_log(log) == []

    @pytest.mark.asyncio
    async def test_an_untouched_read_leaves_the_idle_clock(
        self, fake_engine: tuple[str, Path]
    ) -> None:
        binary, log = fake_engine
        agent_browser.configure_browser(_config(binary_path=binary))
        await agent_browser.run_command("s-view", "open", ["https://example.com"])
        session = agent_browser.get_session("s-view")
        assert session is not None
        session.last_used_at = 5.0
        result = await agent_browser.run_command(
            "s-view", "tab", ["list"], create=False, touch=False
        )
        assert result["success"] is True
        assert session.last_used_at == 5.0
        # A touched read is use, as any tool call is.
        await agent_browser.run_command("s-view", "reload", [], create=False)
        assert session.last_used_at > 5.0
        assert [entry["command"] for entry in _read_log(log)] == ["open", "tab", "reload"]
