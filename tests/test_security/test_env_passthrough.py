"""What a child process inherits, and who may widen it."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from agentos.tools import env_passthrough
from agentos.tools.types import ToolContext, current_tool_context


@pytest.fixture(autouse=True)
def _isolate_registry() -> None:
    env_passthrough.clear_env_passthrough(all_sessions=True)
    env_passthrough.reset_managed_credentials_cache()
    yield
    env_passthrough.clear_env_passthrough(all_sessions=True)
    env_passthrough.reset_managed_credentials_cache()


def _without_custody(env: dict[str, str]) -> dict[str, str]:
    """Drop what every child gets for the skill-signing-key custody (WAL-10)."""
    held = {"GMGN_PRIVATE_KEY", env_passthrough.SIGNING_KEYS_HELD_ENV}
    return {key: value for key, value in env.items() if key not in held}


class TestSubprocessEnv:
    """What is withheld. PATH is rewritten too, but that is TestOwnCliFirst's
    subject; neutralising it here keeps these assertions about stripping."""

    @pytest.fixture(autouse=True)
    def _no_cli_beside_us(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(env_passthrough, "own_cli_dir", lambda: None)

    def test_the_gateway_token_never_reaches_a_child(self) -> None:
        """It authenticates to the control plane; nothing a command runs needs it."""
        base = {"PATH": "/usr/bin", "AGENTOS_GATEWAY_TOKEN": "tok"}
        assert _without_custody(env_passthrough.build_subprocess_env(base)) == {"PATH": "/usr/bin"}

    @pytest.mark.parametrize(
        "name",
        [
            "AGENTOS_SENSITIVE_PATHS_DISABLED",
            "AGENTOS_SENSITIVE_PAYLOAD_DISABLED",
            "AGENTOS_REDACT_SECRETS",
            "AGENTOS_OPERATOR_SECRET",
            "AGENTOS_OPERATOR_SECRET_FILE",
        ],
    )
    def test_guard_switches_never_reach_a_child(self, name: str) -> None:
        """A child that reads them learns the posture; one that writes them changes it."""
        assert name not in env_passthrough.build_subprocess_env({name: "1", "PATH": "/usr/bin"})

    def test_the_extra_argument_cannot_reintroduce_a_stripped_name(self) -> None:
        """``env=`` on a tool call is model input like any other."""
        result = env_passthrough.build_subprocess_env(
            {"PATH": "/usr/bin"}, {"AGENTOS_GATEWAY_TOKEN": "tok", "MY_VAR": "v"}
        )
        assert _without_custody(result) == {"PATH": "/usr/bin", "MY_VAR": "v"}

    def test_the_users_own_credentials_still_cross(self) -> None:
        """The local shell is the operator's; breaking gh and aws protects nothing."""
        base = {"GITHUB_TOKEN": "gh", "AWS_ACCESS_KEY_ID": "ak", "DOCKER_HOST": "unix://x"}
        assert _without_custody(env_passthrough.build_subprocess_env(base)) == base

    def test_provider_keys_cross_by_default(self) -> None:
        """Bundled skill scripts read them straight out of os.environ."""
        base = {"OPENROUTER_API_KEY": "sk-or-v1-x", "AGENTOS_LLM_API_KEY": "k"}
        assert _without_custody(env_passthrough.build_subprocess_env(base)) == base

    def test_provider_keys_are_stripped_when_the_operator_opts_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENTOS_STRIP_PROVIDER_ENV", "1")
        base = {"OPENROUTER_API_KEY": "sk-or-v1-x", "PATH": "/usr/bin", "GITHUB_TOKEN": "gh"}
        result = env_passthrough.build_subprocess_env(base)
        assert "OPENROUTER_API_KEY" not in result
        assert result["PATH"] == "/usr/bin"
        assert result["GITHUB_TOKEN"] == "gh"


class TestRegistration:
    def test_a_skill_declaration_reaches_the_allowlist_sandbox(self) -> None:
        assert env_passthrough.register_env_passthrough(["CAP_API_KEY"]) == []
        assert env_passthrough.is_env_passthrough("CAP_API_KEY")

    def test_an_untrusted_skill_cannot_ask_for_a_runtime_credential(self) -> None:
        """A skill must not be able to tunnel the runtime's own key into a sandbox."""
        refused = env_passthrough.register_env_passthrough(
            ["OPENROUTER_API_KEY", "AGENTOS_LLM_API_KEY", "CAP_API_KEY"]
        )
        assert set(refused) == {"OPENROUTER_API_KEY", "AGENTOS_LLM_API_KEY"}
        assert not env_passthrough.is_env_passthrough("OPENROUTER_API_KEY")
        assert env_passthrough.is_env_passthrough("CAP_API_KEY")

    def test_a_bundled_skill_may_ask_for_one(self) -> None:
        """Its frontmatter ships in the wheel and was reviewed as ours."""
        assert env_passthrough.register_env_passthrough(["OPENROUTER_API_KEY"], trusted=True) == []
        assert env_passthrough.is_env_passthrough("OPENROUTER_API_KEY")

    def test_not_even_a_bundled_skill_may_ask_for_a_guard_switch(self) -> None:
        refused = env_passthrough.register_env_passthrough(
            ["AGENTOS_SENSITIVE_PATHS_DISABLED"], trusted=True
        )
        assert refused == ["AGENTOS_SENSITIVE_PATHS_DISABLED"]
        assert not env_passthrough.is_env_passthrough("AGENTOS_SENSITIVE_PATHS_DISABLED")

    def test_malformed_names_are_dropped_rather_than_registered(self) -> None:
        assert env_passthrough.register_env_passthrough(["", "  ", "not-a-name", "9LEADING"]) == []
        assert not env_passthrough.is_env_passthrough("not-a-name")

    def test_the_catalog_covers_every_provider_family(self) -> None:
        managed = env_passthrough.agentos_managed_credentials()
        assert "OPENROUTER_API_KEY" in managed  # LLM
        assert "TAVILY_API_KEY" in managed  # search
        assert "ELEVENLABS_API_KEY" in managed  # audio
        assert "GITHUB_TOKEN" not in managed  # the user's own


class TestRegistrationSurvivesTheToolBoundary:
    """A registration is only useful if the *next* tool call can see it.

    Every tool call runs in its own asyncio task, and a task gets a copy of the
    context — so anything stored per-context in the ``skill_view`` call is
    invisible to the ``execute_code`` call that follows. Registering in one
    task and reading in the same task, as a naive test does, hides that
    completely.
    """

    @staticmethod
    def _in_session(session_key: str, fn):
        async def run():
            current_tool_context.set(ToolContext(session_key=session_key))
            return fn()

        return run

    def test_a_later_tool_call_sees_it(self) -> None:
        async def scenario() -> tuple[bool, bool]:
            registered = await asyncio.create_task(
                self._in_session(
                    "s1", lambda: env_passthrough.register_env_passthrough(["CAP_API_KEY"]) == []
                )()
            )
            seen = await asyncio.create_task(
                self._in_session("s1", lambda: env_passthrough.is_env_passthrough("CAP_API_KEY"))()
            )
            return registered, seen

        registered, seen = asyncio.run(scenario())
        assert registered
        assert seen, "a skill's declaration must survive to the tool it exists to serve"

    def test_another_session_does_not_see_it(self) -> None:
        async def scenario() -> bool:
            await asyncio.create_task(
                self._in_session(
                    "s1", lambda: env_passthrough.register_env_passthrough(["CAP_API_KEY"])
                )()
            )
            return await asyncio.create_task(
                self._in_session("s2", lambda: env_passthrough.is_env_passthrough("CAP_API_KEY"))()
            )

        assert asyncio.run(scenario()) is False

    def test_old_sessions_are_evicted_rather_than_accumulating(self) -> None:
        async def scenario() -> tuple[bool, bool]:
            for index in range(env_passthrough._MAX_SESSIONS + 5):
                await asyncio.create_task(
                    self._in_session(
                        f"s{index}",
                        lambda: env_passthrough.register_env_passthrough(["CAP_API_KEY"]),
                    )()
                )
            oldest = await asyncio.create_task(
                self._in_session("s0", lambda: env_passthrough.is_env_passthrough("CAP_API_KEY"))()
            )
            newest = await asyncio.create_task(
                self._in_session(
                    f"s{env_passthrough._MAX_SESSIONS + 4}",
                    lambda: env_passthrough.is_env_passthrough("CAP_API_KEY"),
                )()
            )
            return oldest, newest

        oldest, newest = asyncio.run(scenario())
        assert newest
        assert not oldest
        assert len(env_passthrough._registry) <= env_passthrough._MAX_SESSIONS


def test_sandboxed_code_sees_registered_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """execute_code forwards almost nothing; a declaration is how a skill reaches its key."""
    from agentos.tools.builtin import code_exec

    monkeypatch.setenv("CAP_API_KEY", "cap_live_value")
    monkeypatch.setenv("UNRELATED_SECRET", "nope")

    assert "CAP_API_KEY" not in code_exec._build_safe_env()

    env_passthrough.register_env_passthrough(["CAP_API_KEY"])
    safe_env = code_exec._build_safe_env()
    assert safe_env["CAP_API_KEY"] == "cap_live_value"
    assert "UNRELATED_SECRET" not in safe_env


def _path(*parts: str) -> str:
    """A PATH in the platform's own separator (``:`` on POSIX, ``;`` on Windows)."""
    return os.pathsep.join(parts)


class TestOwnCliFirst:
    """Which ``agentos`` an agent reaches when it runs one.

    An agent trades by running ``agentos trade …`` in a shell, so the name
    resolves against the user's PATH rather than against the build that holds
    the vault. A second, older install earlier in PATH — ``uv tool install``
    into ``~/.local/bin`` is the usual one — answers instead, and says only
    ``No such command 'trade'``.
    """

    @pytest.fixture(autouse=True)
    def _pretend_we_live_here(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(env_passthrough, "own_cli_dir", lambda: "/opt/agentos/bin")

    def test_the_running_build_wins_over_an_older_one_in_path(self) -> None:
        result = env_passthrough.build_subprocess_env(
            {"PATH": _path("/home/u/.local/bin", "/usr/bin")}
        )
        assert result["PATH"] == _path("/opt/agentos/bin", "/home/u/.local/bin", "/usr/bin")

    def test_a_later_copy_of_our_own_bin_is_not_left_behind(self) -> None:
        """Otherwise PATH grows a duplicate on every nested tool call."""
        result = env_passthrough.build_subprocess_env(
            {"PATH": _path("/usr/bin", "/opt/agentos/bin")}
        )
        assert result["PATH"] == _path("/opt/agentos/bin", "/usr/bin")

    def test_an_already_correct_path_is_untouched(self) -> None:
        result = env_passthrough.build_subprocess_env(
            {"PATH": _path("/opt/agentos/bin", "/usr/bin")}
        )
        assert result["PATH"] == _path("/opt/agentos/bin", "/usr/bin")

    def test_an_absent_path_is_not_invented(self) -> None:
        """One directory is not a PATH: it would take ``sh`` off the child's."""
        assert "PATH" not in env_passthrough.build_subprocess_env({"HOME": "/home/u"})

    def test_nothing_changes_when_no_agentos_sits_beside_the_interpreter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A system Python keeps its scripts elsewhere; guessing would reorder
        which ``git`` and ``python3`` every shell call resolves to."""
        monkeypatch.setattr(env_passthrough, "own_cli_dir", lambda: None)
        assert env_passthrough.build_subprocess_env({"PATH": "/usr/bin"})["PATH"] == "/usr/bin"


_CONSOLE_SCRIPT = "agentos.exe" if os.name == "nt" else "agentos"


class TestOwnCliDir:
    def test_it_finds_the_console_script_next_to_a_venv_interpreter(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        bin_dir = tmp_path / "venv" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / _CONSOLE_SCRIPT).write_text("#!/bin/sh\n")
        monkeypatch.setattr(sys, "executable", str(bin_dir / "python"))
        assert env_passthrough.own_cli_dir() == str(bin_dir)

    def test_it_declines_when_there_is_no_console_script_there(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(sys, "executable", str(tmp_path / "python"))
        assert env_passthrough.own_cli_dir() is None

    @pytest.mark.skipif(os.name == "nt", reason="venv symlinks are a POSIX layout")
    def test_a_venv_symlink_is_not_followed_out_of_its_own_bin(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``venv/bin/python`` points at the base interpreter, whose directory
        holds no console scripts — resolving it loses the venv entirely."""
        base_bin = tmp_path / "pythons" / "bin"
        base_bin.mkdir(parents=True)
        (base_bin / "python3").write_text("#!/bin/sh\n")
        venv_bin = tmp_path / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "agentos").write_text("#!/bin/sh\n")
        (venv_bin / "python").symlink_to(base_bin / "python3")
        monkeypatch.setattr(sys, "executable", str(venv_bin / "python"))
        assert env_passthrough.own_cli_dir() == str(venv_bin)
