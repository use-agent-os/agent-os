"""Tests for the env.* RPC surface.

The load-bearing assertion in this file is that a real value never appears in
a list/set/unset response. Everything else — the policy gate, the rate limit,
the audit line — exists to keep that true under pressure.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentos import credential_sources, env_catalog, env_store
from agentos.gateway import rpc_env
from agentos.gateway.access import CONTROL_ONLY, ConnectionSurface
from agentos.gateway.auth import AccessContext
from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.gateway.rpc_env import _reset_reveal_budget

SECRET = "sk-live-" + "z" * 40 + "tail"


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
    _reset_reveal_budget()
    yield state
    os.environ.clear()
    os.environ.update(snapshot)
    _reset_reveal_budget()


def _ctx(surface: ConnectionSurface = ConnectionSurface.CONTROL) -> RpcContext:
    return RpcContext(
        conn_id="conn-1",
        access=AccessContext(surface=surface, admitted=True, credential_verified=True),
    )


async def call(method: str, params: dict | None = None):
    return await get_dispatcher().dispatch("r1", method, params, _ctx())


class TestAudience:
    @pytest.mark.parametrize("method", ["env.list", "env.set", "env.unset", "env.reveal"])
    def test_every_method_is_control_only(self, method: str) -> None:
        # A channel connection is a chat surface; it has no business reading or
        # writing the credentials the gateway runs on.
        assert get_dispatcher().get_entry(method).audiences == CONTROL_ONLY


class TestList:
    @pytest.mark.asyncio
    async def test_reports_state_without_values(self) -> None:
        env_store.set_env_var("OPENAI_API_KEY", SECRET)
        res = await call("env.list")

        assert res.error is None, res.error
        # The whole payload, not just the field we remembered to check.
        assert SECRET not in json.dumps(res.payload)

        row = next(v for v in res.payload["vars"] if v["name"] == "OPENAI_API_KEY")
        assert row["isSet"] is True
        assert row["masked"] and SECRET not in row["masked"]

    @pytest.mark.asyncio
    async def test_describes_what_a_variable_is_for(self) -> None:
        res = await call("env.list")
        row = next(v for v in res.payload["vars"] if v["name"] == "OPENAI_API_KEY")
        assert row["description"]
        assert row["category"] == "provider"
        assert row["restartRequired"] is True

    @pytest.mark.asyncio
    async def test_surfaces_undeclared_keys_from_the_users_file(self) -> None:
        env_store.set_env_var("MY_OWN_VARIABLE", "value")
        res = await call("env.list")
        row = next(v for v in res.payload["vars"] if v["name"] == "MY_OWN_VARIABLE")
        assert row["category"] == "custom"

    @pytest.mark.asyncio
    async def test_counts_variables_shadowed_by_the_process(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # "I saved it and nothing happened" is what this count exists to explain.
        env_store.set_env_var("OPENAI_API_KEY", "from-file", apply_live=False)
        monkeypatch.setenv("OPENAI_API_KEY", "from-shell")
        res = await call("env.list")
        row = next(v for v in res.payload["vars"] if v["name"] == "OPENAI_API_KEY")
        assert row["source"] == "process"
        assert res.payload["shadowedCount"] >= 1

    @pytest.mark.asyncio
    async def test_marks_denylisted_names_unwritable_when_present(self) -> None:
        env_store.write_env_file_values(
            env_store.env_file_path(), {"PATH": "/tmp/x"}, enforce_denylist=False
        )
        res = await call("env.list")
        row = next(v for v in res.payload["vars"] if v["name"] == "PATH")
        assert row["writable"] is False


class TestSet:
    @pytest.mark.asyncio
    async def test_writes_the_value_and_does_not_echo_it(self) -> None:
        res = await call("env.set", {"name": "OPENAI_API_KEY", "value": SECRET})

        assert res.error is None, res.error
        assert SECRET not in json.dumps(res.payload)
        assert res.payload["isSet"] is True
        assert env_store.read_env_file()["OPENAI_API_KEY"] == SECRET

    @pytest.mark.asyncio
    async def test_applies_live_so_new_tool_runs_see_it(self) -> None:
        await call("env.set", {"name": "BASE_RPC_URL", "value": "https://rpc.example.invalid"})
        assert os.environ["BASE_RPC_URL"] == "https://rpc.example.invalid"

    @pytest.mark.asyncio
    async def test_refuses_a_denylisted_name_with_a_usable_message(self) -> None:
        res = await call("env.set", {"name": "LD_PRELOAD", "value": "/tmp/evil.so"})
        assert res.error is not None
        assert "cannot be written through AgentOS" in res.error.message
        assert not env_store.env_file_path().exists()

    @pytest.mark.asyncio
    async def test_refuses_an_invalid_name(self) -> None:
        res = await call("env.set", {"name": "1BAD", "value": "x"})
        assert res.error is not None

    @pytest.mark.asyncio
    async def test_refuses_a_value_containing_a_line_break(self) -> None:
        res = await call("env.set", {"name": "GOOD_TOKEN", "value": "a\nINJECTED=1"})
        assert res.error is not None
        assert "INJECTED" not in json.dumps(env_store.read_env_file())

    @pytest.mark.asyncio
    async def test_requires_name_and_value(self) -> None:
        assert (await call("env.set", {"value": "x"})).error is not None
        assert (await call("env.set", {"name": "K"})).error is not None
        assert (await call("env.set", {"name": "K", "value": 5})).error is not None

    @pytest.mark.asyncio
    async def test_reports_whether_a_restart_is_needed(self) -> None:
        provider = await call("env.set", {"name": "OPENAI_API_KEY", "value": SECRET})
        other = await call("env.set", {"name": "MY_OWN_VARIABLE", "value": "v"})
        # A provider client was built at boot with the old key.
        assert provider.payload["restartRequired"] is True
        # Nothing holds this one; the next spawned process reads it fresh.
        assert other.payload["restartRequired"] is False


class TestUnset:
    @pytest.mark.asyncio
    async def test_removes_the_variable(self) -> None:
        env_store.set_env_var("MY_OWN_VARIABLE", "v")
        res = await call("env.unset", {"name": "MY_OWN_VARIABLE"})
        assert res.payload["removed"] is True
        assert res.payload["isSet"] is False
        assert "MY_OWN_VARIABLE" not in env_store.read_env_file()

    @pytest.mark.asyncio
    async def test_absent_variable_reports_not_removed(self) -> None:
        res = await call("env.unset", {"name": "NEVER_SET"})
        assert res.payload["removed"] is False

    @pytest.mark.asyncio
    async def test_denylisted_name_is_refused(self) -> None:
        res = await call("env.unset", {"name": "PATH"})
        assert res.error is not None


class TestReveal:
    @pytest.mark.asyncio
    async def test_returns_the_real_value(self) -> None:
        env_store.set_env_var("OPENAI_API_KEY", SECRET)
        res = await call("env.reveal", {"name": "OPENAI_API_KEY"})
        assert res.payload["value"] == SECRET

    @pytest.mark.asyncio
    async def test_unset_variable_is_an_error_not_an_empty_string(self) -> None:
        res = await call("env.reveal", {"name": "NEVER_SET"})
        assert res.error is not None

    @pytest.mark.asyncio
    async def test_rate_limit_stops_a_bulk_export(self) -> None:
        env_store.set_env_var("OPENAI_API_KEY", SECRET)
        for _ in range(5):
            assert (await call("env.reveal", {"name": "OPENAI_API_KEY"})).error is None
        blocked = await call("env.reveal", {"name": "OPENAI_API_KEY"})
        assert blocked.error is not None
        assert "Too many reveal requests" in blocked.error.message

    @pytest.mark.asyncio
    async def test_audit_line_records_the_name_and_never_the_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Recording against the module's logger rather than structlog's global
        # capture: whether capture_logs() sees anything depends on how
        # structlog happens to be configured by whatever ran earlier in the
        # session, and this assertion is too important to be flaky.
        emitted: list[tuple[str, dict]] = []

        class Recorder:
            def info(self, event: str, **kwargs: object) -> None:
                emitted.append((event, dict(kwargs)))

            def warning(self, event: str, **kwargs: object) -> None:
                emitted.append((event, dict(kwargs)))

        monkeypatch.setattr(rpc_env, "log", Recorder())
        env_store.set_env_var("OPENAI_API_KEY", SECRET)
        await call("env.reveal", {"name": "OPENAI_API_KEY"})

        event, fields = next(e for e in emitted if e[0] == "env.revealed")
        assert fields["key"] == "OPENAI_API_KEY"
        # The log exists so someone can tell which secrets were read — not to
        # make a second copy of them.
        assert SECRET not in json.dumps(emitted)


class TestCredentialAvailability:
    """A variable is often not missing — it is authenticated somewhere else."""

    @pytest.fixture(autouse=True)
    def _clear_probe_cache(self):
        credential_sources.reset_probe_cache()
        yield
        credential_sources.reset_probe_cache()

    @pytest.fixture
    def gh_authenticated(self, monkeypatch: pytest.MonkeyPatch):
        """Pretend `gh auth status` succeeds, without shelling out."""
        monkeypatch.setattr(credential_sources.shutil, "which", lambda _: "/usr/bin/gh")
        monkeypatch.setattr(
            credential_sources, "_run", lambda argv, timeout: (0, "Logged in to github.com")
        )

    @pytest.mark.asyncio
    async def test_an_unset_variable_reports_where_it_can_come_from(
        self, gh_authenticated, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A skill declaring the variable is what puts it in the catalog while
        # it is unset — which is exactly the case worth reporting on.
        monkeypatch.setattr(
            env_catalog,
            "build_catalog",
            lambda *a, **k: {
                "GITHUB_TOKEN": env_catalog.EnvVarSpec(
                    name="GITHUB_TOKEN",
                    description="Required by the repo-triage skill.",
                    category=env_catalog.CATEGORY_SKILL,
                    owner="repo-triage",
                    required=True,
                )
            },
        )
        res = await call("env.list")
        row = next(v for v in res.payload["vars"] if v["name"] == "GITHUB_TOKEN")
        assert row["isSet"] is False
        assert row["availableFrom"] == {"id": "gh_cli", "label": "GitHub CLI"}

    @pytest.mark.asyncio
    async def test_a_variable_already_set_is_not_offered_an_import(self, gh_authenticated) -> None:
        env_store.set_env_var("GITHUB_TOKEN", "already-here")
        res = await call("env.list")
        row = next(v for v in res.payload["vars"] if v["name"] == "GITHUB_TOKEN")
        # Nothing to import: it is already configured.
        assert row["availableFrom"] is None

    @pytest.mark.asyncio
    async def test_listing_never_reads_a_credential(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Probing answers "could this supply it", never "what is it". The
        # commands a listing is allowed to run are asserted here by name.
        ran: list[list[str]] = []

        def record(argv, timeout):
            ran.append(argv)
            return 0, "Logged in"

        monkeypatch.setattr(credential_sources.shutil, "which", lambda _: "/usr/bin/gh")
        monkeypatch.setattr(credential_sources, "_run", record)
        # Put the unset variable in the catalog so the probe actually fires.
        monkeypatch.setattr(
            env_catalog,
            "build_catalog",
            lambda *a, **k: {"GITHUB_TOKEN": env_catalog.EnvVarSpec(name="GITHUB_TOKEN")},
        )
        await call("env.list")

        assert ran, "the probe should have run at least once"
        assert ran == [["gh", "auth", "status"]]


class TestImport:
    @pytest.fixture(autouse=True)
    def _clear_probe_cache(self):
        credential_sources.reset_probe_cache()
        yield
        credential_sources.reset_probe_cache()

    @pytest.mark.asyncio
    async def test_copies_the_value_in_without_returning_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(credential_sources, "read_from", lambda n, s: "gho_imported")
        res = await call("env.import", {"name": "GITHUB_TOKEN", "sourceId": "gh_cli"})

        assert res.error is None, res.error
        assert "gho_imported" not in json.dumps(res.payload)
        assert res.payload["isSet"] is True
        assert env_store.read_env_file()["GITHUB_TOKEN"] == "gho_imported"

    @pytest.mark.asyncio
    async def test_says_the_copy_will_not_follow_rotation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A copied token goes stale when the source rotates; better said once
        # here than discovered as a mystery 401 later.
        monkeypatch.setattr(credential_sources, "read_from", lambda n, s: "gho_imported")
        res = await call("env.import", {"name": "GITHUB_TOKEN", "sourceId": "gh_cli"})
        assert "rotates" in res.payload["note"]

    @pytest.mark.asyncio
    async def test_requires_a_source(self) -> None:
        res = await call("env.import", {"name": "GITHUB_TOKEN"})
        assert res.error is not None

    @pytest.mark.asyncio
    async def test_an_unusable_source_explains_how_to_fix_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(credential_sources.shutil, "which", lambda _: None)
        res = await call("env.import", {"name": "GITHUB_TOKEN", "sourceId": "gh_cli"})
        assert res.error is not None
        assert "gh auth login" in res.error.message

    @pytest.mark.asyncio
    async def test_the_policy_gate_still_applies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An import is still a write; a denylisted name cannot sneak in by it.
        monkeypatch.setattr(credential_sources, "read_from", lambda n, s: "/tmp/evil.so")
        res = await call("env.import", {"name": "LD_PRELOAD", "sourceId": "gh_cli"})
        assert res.error is not None
