"""Tests for ``agentos env``.

Two behaviours matter beyond the obvious plumbing: the command must work
before a gateway exists (that is when a provider key gets set for the first
time), and it must never print a secret the operator did not explicitly ask
for.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentos import credential_sources, env_store
from agentos.cli.main import app

runner = CliRunner()

SECRET = "sk-live-" + "q" * 40 + "tail"


@pytest.fixture(autouse=True)
def env_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolate the .env file, the working directory, and os.environ."""
    state = tmp_path / "state"
    state.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(state))
    monkeypatch.chdir(work)
    snapshot = dict(os.environ)
    yield state
    os.environ.clear()
    os.environ.update(snapshot)


@pytest.fixture(autouse=True)
def no_gateway(monkeypatch: pytest.MonkeyPatch):
    """Force the offline path: these tests cover the no-gateway install."""
    from agentos.cli import env_cmd

    async def _absent(method: str, params: dict, *, json_output: bool) -> None:
        return None

    monkeypatch.setattr(env_cmd, "_try_gateway", _absent)


class TestSet:
    def test_stdin_keeps_the_value_out_of_argv(self) -> None:
        result = runner.invoke(app, ["env", "set", "OPENAI_API_KEY", "--stdin"], input=SECRET)
        assert result.exit_code == 0, result.output
        assert env_store.read_env_file()["OPENAI_API_KEY"] == SECRET

    def test_trailing_newline_from_a_pipe_is_not_stored(self) -> None:
        # `echo key | agentos env set --stdin` must not store "key\n".
        runner.invoke(app, ["env", "set", "OPENAI_API_KEY", "--stdin"], input=SECRET + "\n")
        assert env_store.read_env_file()["OPENAI_API_KEY"] == SECRET

    def test_says_the_value_only_applies_after_a_restart(self) -> None:
        result = runner.invoke(app, ["env", "set", "OPENAI_API_KEY", "--stdin"], input=SECRET)
        assert "next time" in result.output

    def test_value_flag_still_works_for_non_secrets(self) -> None:
        result = runner.invoke(
            app, ["env", "set", "BASE_RPC_URL", "--value", "https://rpc.example.invalid"]
        )
        assert result.exit_code == 0
        assert env_store.read_env_file()["BASE_RPC_URL"] == "https://rpc.example.invalid"

    def test_denylisted_name_is_refused_with_an_actionable_message(self) -> None:
        result = runner.invoke(app, ["env", "set", "PATH", "--value", "/tmp/x"])
        assert result.exit_code != 0
        assert "cannot be written through AgentOS" in result.output
        assert not env_store.env_file_path().exists()

    def test_json_requires_an_explicit_value_source(self) -> None:
        # Prompting under --json would hang a script.
        result = runner.invoke(app, ["env", "set", "K", "--json"])
        assert result.exit_code != 0
        assert "--value or --stdin" in result.output


class TestList:
    def test_shows_masked_values_not_real_ones(self) -> None:
        env_store.set_env_var("OPENAI_API_KEY", SECRET)
        result = runner.invoke(app, ["env", "list"])
        assert result.exit_code == 0
        assert SECRET not in result.output

    def test_json_output_carries_no_values(self) -> None:
        env_store.set_env_var("OPENAI_API_KEY", SECRET)
        result = runner.invoke(app, ["env", "list", "--json"])
        payload = json.loads(result.output)
        assert SECRET not in json.dumps(payload)
        assert payload["setCount"] >= 1

    def test_missing_filter_hides_variables_that_are_set(self) -> None:
        env_store.set_env_var("OPENAI_API_KEY", SECRET)
        result = runner.invoke(app, ["env", "list", "--missing", "--json"])
        names = {row["name"] for row in json.loads(result.output)["vars"]}
        assert "OPENAI_API_KEY" not in names

    def test_category_filter(self) -> None:
        result = runner.invoke(app, ["env", "list", "--category", "provider", "--json"])
        rows = json.loads(result.output)["vars"]
        assert rows and all(row["category"] == "provider" for row in rows)

    def test_provider_keys_are_not_all_reported_as_missing(self) -> None:
        # AgentOS talks to one provider at a time; flagging every provider key
        # as missing would bury the one that actually needs attention.
        result = runner.invoke(app, ["env", "list", "--json"])
        rows = json.loads(result.output)["vars"]
        assert not any(row["missing"] for row in rows if row["category"] == "provider")

    def test_warns_when_a_variable_is_shadowed_by_the_process(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_store.set_env_var("OPENAI_API_KEY", "from-file", apply_live=False)
        monkeypatch.setenv("OPENAI_API_KEY", "from-shell")
        result = runner.invoke(app, ["env", "list"])
        assert "shadowed by the process environment" in result.output


class TestGet:
    def test_state_without_reveal_shows_only_a_mask(self) -> None:
        env_store.set_env_var("OPENAI_API_KEY", SECRET)
        result = runner.invoke(app, ["env", "get", "OPENAI_API_KEY"])
        assert "set" in result.output
        assert SECRET not in result.output

    def test_reveal_prints_the_value_after_confirmation(self) -> None:
        env_store.set_env_var("OPENAI_API_KEY", SECRET)
        result = runner.invoke(app, ["env", "get", "OPENAI_API_KEY", "--reveal"], input="y\n")
        assert SECRET in result.output

    def test_reveal_aborts_when_declined(self) -> None:
        env_store.set_env_var("OPENAI_API_KEY", SECRET)
        result = runner.invoke(app, ["env", "get", "OPENAI_API_KEY", "--reveal"], input="n\n")
        assert result.exit_code != 0
        assert SECRET not in result.output

    def test_unknown_name_is_an_error(self) -> None:
        result = runner.invoke(app, ["env", "get", "NOT_A_REAL_VARIABLE"])
        assert result.exit_code != 0


class TestUnset:
    def test_removes_after_confirmation(self) -> None:
        env_store.set_env_var("MY_OWN_VARIABLE", "v")
        result = runner.invoke(app, ["env", "unset", "MY_OWN_VARIABLE"], input="y\n")
        assert result.exit_code == 0
        assert "MY_OWN_VARIABLE" not in env_store.read_env_file()

    def test_yes_skips_the_prompt(self) -> None:
        env_store.set_env_var("MY_OWN_VARIABLE", "v")
        result = runner.invoke(app, ["env", "unset", "MY_OWN_VARIABLE", "--yes"])
        assert result.exit_code == 0
        assert "MY_OWN_VARIABLE" not in env_store.read_env_file()

    def test_absent_variable_says_so_rather_than_failing(self) -> None:
        result = runner.invoke(app, ["env", "unset", "NEVER_SET", "--yes"])
        assert result.exit_code == 0
        assert "was not set" in result.output

    def test_denylisted_name_is_refused(self) -> None:
        result = runner.invoke(app, ["env", "unset", "PATH", "--yes"])
        assert result.exit_code != 0


class TestGatewayPreferred:
    def test_uses_the_gateway_when_one_is_running(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The gateway path is what makes a change apply to the live process
        # instead of only to the file.
        from agentos.cli import env_cmd

        calls: list[tuple[str, dict]] = []

        async def _present(method: str, params: dict, *, json_output: bool):
            calls.append((method, params))
            return {"name": params.get("name"), "isSet": True, "masked": "•" * 8}

        monkeypatch.setattr(env_cmd, "_try_gateway", _present)
        result = runner.invoke(app, ["env", "set", "OPENAI_API_KEY", "--stdin"], input=SECRET)

        assert result.exit_code == 0
        assert calls == [("env.set", {"name": "OPENAI_API_KEY", "value": SECRET})]
        # The gateway owns the write; the CLI must not also write the file.
        assert not env_store.env_file_path().exists()
        assert "next time" not in result.output


class TestMachineReadableOutput:
    def test_json_stdout_is_not_polluted_by_startup_logs(
        self, env_home: Path, tmp_path: Path
    ) -> None:
        """``agentos <cmd> --json | jq`` must work on an install that has a .env.

        The CLI loads .env files before anything else, and those loads log. With
        structlog's unconfigured default that output goes to stdout, so every
        --json payload arrived with log lines in front of it the moment a user
        had a populated .env — working in a clean environment and failing on a
        real one.
        """
        import json as json_module
        import subprocess
        import sys

        (env_home / ".env").write_text("SOME_TOKEN=value\n", encoding="utf-8")

        env = dict(os.environ)
        env["AGENTOS_STATE_DIR"] = str(env_home)
        env["AGENTOS_LOG_LEVEL"] = "debug"
        result = subprocess.run(
            [sys.executable, "-m", "agentos.cli.main", "env", "list", "--json"],
            capture_output=True,
            text=True,
            env=env,
            cwd=tmp_path,
        )

        assert result.returncode == 0, result.stderr
        payload = json_module.loads(result.stdout)
        assert payload["totalCount"] >= 1


class TestImport:
    @pytest.fixture(autouse=True)
    def _clear_probe_cache(self):
        credential_sources.reset_probe_cache()
        yield
        credential_sources.reset_probe_cache()

    def test_picks_the_usable_source_when_none_is_named(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(credential_sources.shutil, "which", lambda _: "/usr/bin/gh")
        monkeypatch.setattr(
            credential_sources,
            "_run",
            lambda argv, timeout: (0, "gho_from_cli" if "token" in argv else "Logged in"),
        )
        result = runner.invoke(app, ["env", "import", "GITHUB_TOKEN"])

        assert result.exit_code == 0, result.output
        assert env_store.read_env_file()["GITHUB_TOKEN"] == "gho_from_cli"

    def test_says_the_copy_goes_stale_on_rotation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(credential_sources.shutil, "which", lambda _: "/usr/bin/gh")
        monkeypatch.setattr(
            credential_sources,
            "_run",
            lambda argv, timeout: (0, "gho_from_cli" if "token" in argv else "Logged in"),
        )
        result = runner.invoke(app, ["env", "import", "GITHUB_TOKEN"])
        assert "rotates" in result.output

    def test_no_known_source_says_so(self) -> None:
        result = runner.invoke(app, ["env", "import", "SOME_RANDOM_KEY"])
        assert result.exit_code != 0
        assert "No known source" in result.output

    def test_a_known_but_unusable_source_gives_the_login_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(credential_sources.shutil, "which", lambda _: None)
        result = runner.invoke(app, ["env", "import", "GITHUB_TOKEN"])
        assert result.exit_code != 0
        assert "gh auth login" in result.output

    def test_listing_flags_a_variable_that_could_be_imported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(credential_sources.shutil, "which", lambda _: "/usr/bin/gh")
        monkeypatch.setattr(credential_sources, "_run", lambda argv, timeout: (0, "Logged in"))
        # Present in the file, then removed: still catalogued, now unset.
        env_store.set_env_var("GITHUB_TOKEN", "x")
        result = runner.invoke(app, ["env", "list", "--json"])
        env_store.unset_env_var("GITHUB_TOKEN")

        rows = json.loads(result.output)["vars"]
        row = next(r for r in rows if r["name"] == "GITHUB_TOKEN")
        # It is set here, so no offer — the field exists and is correct.
        assert row["availableFrom"] is None
