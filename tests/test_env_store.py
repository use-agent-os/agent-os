"""Tests for the ``~/.agentos/.env`` writer.

The central contract is round-trip: anything :func:`set_env_var` writes must
come back out of :func:`agentos.env.load_env`'s parser unchanged. Everything
else here guards a specific way a naive writer loses data — clobbering an
``export`` line, eating trailing whitespace, tightening a bind-mounted file's
permissions, or leaving a truncated file behind after a failed write.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from agentos import env_store
from agentos.env import parse_env_file
from agentos.env_policy import EnvPolicyError

WINDOWS = os.name == "nt"


@pytest.fixture
def env_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point AgentOS at an empty state dir and an empty working directory.

    ``resolve_entry`` consults ``$CWD/.env`` as well, so the working directory
    is moved too — otherwise the repository's own files would leak into the
    precedence assertions.
    """
    state = tmp_path / "state"
    state.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(state))
    monkeypatch.chdir(work)
    return state


@pytest.fixture(autouse=True)
def restore_environ() -> object:
    """Undo the live ``os.environ`` mutations the writer performs by design."""
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


def read_back(env_home: Path) -> dict[str, str]:
    return parse_env_file(env_home / ".env")


class TestRoundTrip:
    @pytest.mark.parametrize(
        "value",
        [
            "",
            "plain",
            "with space",
            "  leading and trailing  ",
            "trailing ",
            " leading",
            "a#b",
            "#leading-hash",
            "eq=sign",
            'has"quote',
            "has'quote",
            '"fully quoted"',
            "'fully quoted'",
            '"leading quote only',
            "'single leading quote",
            'quote"in_middle',
            "back\\slash",
            "C:\\new\\path",
            "tab\there",
            "unicode-á-ü-日本",
            "sk-proj-" + "x" * 64,
        ],
    )
    def test_written_value_parses_back_identical(self, env_home: Path, value: str) -> None:
        env_store.set_env_var("ROUND_TRIP", value)
        assert read_back(env_home)["ROUND_TRIP"] == value

    def test_value_that_is_itself_quoted_survives_a_rewrite_cycle(self, env_home: Path) -> None:
        # Writing '"x"' bare would let the reader strip the value's own quotes.
        env_store.set_env_var("K", '"x"')
        assert read_back(env_home)["K"] == '"x"'
        env_store.set_env_var("K", read_back(env_home)["K"])
        assert read_back(env_home)["K"] == '"x"'


class TestUpsert:
    def test_creates_the_file_when_absent(self, env_home: Path) -> None:
        assert not (env_home / ".env").exists()
        env_store.set_env_var("FIRST", "1")
        assert read_back(env_home) == {"FIRST": "1"}

    def test_updates_in_place_without_appending(self, env_home: Path) -> None:
        env_store.set_env_var("K", "old")
        env_store.set_env_var("K", "new")
        lines = (env_home / ".env").read_text(encoding="utf-8").splitlines()
        assert lines == ["K=new"]

    def test_replaces_an_export_line_rather_than_shadowing_it(self, env_home: Path) -> None:
        # A second definition would leave the old value behind after an unset.
        (env_home / ".env").write_text("export GITHUB_TOKEN=old\n", encoding="utf-8")
        env_store.set_env_var("GITHUB_TOKEN", "new")
        lines = (env_home / ".env").read_text(encoding="utf-8").splitlines()
        assert lines == ["GITHUB_TOKEN=new"]
        assert read_back(env_home)["GITHUB_TOKEN"] == "new"

    def test_collapses_pre_existing_duplicate_definitions(self, env_home: Path) -> None:
        (env_home / ".env").write_text("K=one\nOTHER=x\nK=two\n", encoding="utf-8")
        env_store.set_env_var("K", "three")
        lines = (env_home / ".env").read_text(encoding="utf-8").splitlines()
        assert lines == ["K=three", "OTHER=x"]

    def test_preserves_comments_and_unrelated_lines(self, env_home: Path) -> None:
        (env_home / ".env").write_text(
            "# managed by hand\nKEEP=yes\n\n# section\nK=old\n", encoding="utf-8"
        )
        env_store.set_env_var("K", "new")
        text = (env_home / ".env").read_text(encoding="utf-8")
        assert "# managed by hand" in text
        assert "# section" in text
        assert "KEEP=yes" in text
        assert "K=new" in text

    def test_appends_when_the_file_lacks_a_trailing_newline(self, env_home: Path) -> None:
        (env_home / ".env").write_text("FIRST=1", encoding="utf-8")
        env_store.set_env_var("SECOND", "2")
        assert read_back(env_home) == {"FIRST": "1", "SECOND": "2"}

    def test_reads_a_file_written_with_a_bom(self, env_home: Path) -> None:
        (env_home / ".env").write_text("K=old\n", encoding="utf-8-sig")
        env_store.set_env_var("K", "new")
        assert read_back(env_home) == {"K": "new"}

    def test_batch_write_is_deterministic(self, env_home: Path) -> None:
        env_store.set_env_vars({"B": "2", "A": "1", "C": "3"})
        lines = (env_home / ".env").read_text(encoding="utf-8").splitlines()
        assert lines == ["A=1", "B=2", "C=3"]


class TestPolicyIsEnforcedAtTheStore:
    def test_denylisted_name_is_refused(self, env_home: Path) -> None:
        with pytest.raises(EnvPolicyError, match="cannot be written through AgentOS"):
            env_store.set_env_var("LD_PRELOAD", "/tmp/evil.so")
        assert not (env_home / ".env").exists()

    def test_invalid_name_is_refused(self, env_home: Path) -> None:
        with pytest.raises(EnvPolicyError, match="Invalid environment variable name"):
            env_store.set_env_var("1BAD", "x")

    def test_line_break_cannot_inject_a_second_variable(self, env_home: Path) -> None:
        env_store.set_env_var("K", "safe")
        with pytest.raises(EnvPolicyError, match="line break"):
            env_store.set_env_var("K", "value\nINJECTED=pwned")
        assert read_back(env_home) == {"K": "safe"}

    def test_unset_is_gated_too(self, env_home: Path) -> None:
        with pytest.raises(EnvPolicyError):
            env_store.unset_env_var("PATH")


@pytest.mark.skipif(WINDOWS, reason="POSIX file modes are a no-op on Windows")
class TestPermissions:
    def test_new_file_is_owner_only(self, env_home: Path) -> None:
        env_store.set_env_var("K", "v")
        mode = stat.S_IMODE((env_home / ".env").stat().st_mode)
        assert mode == 0o600

    def test_existing_mode_is_preserved(self, env_home: Path) -> None:
        # Container deployments bind-mount .env at 0640 on purpose; silently
        # tightening it to 0600 breaks the container's read.
        path = env_home / ".env"
        path.write_text("K=old\n", encoding="utf-8")
        os.chmod(path, 0o640)
        env_store.set_env_var("K", "new")
        assert stat.S_IMODE(path.stat().st_mode) == 0o640


class TestAtomicity:
    def test_failed_write_leaves_the_original_intact(
        self, env_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_store.set_env_var("K", "original")

        def boom(_fd: int) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(os, "fsync", boom)
        with pytest.raises(OSError, match="disk full"):
            env_store.set_env_var("K", "replacement")

        assert read_back(env_home) == {"K": "original"}

    def test_failed_write_leaves_no_temporary_file(
        self, env_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env_store.set_env_var("K", "original")
        monkeypatch.setattr(os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("nope")))
        with pytest.raises(OSError):
            env_store.set_env_var("K", "replacement")
        leftovers = [p.name for p in env_home.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []


class TestUnset:
    def test_removes_the_line_and_the_live_value(self, env_home: Path) -> None:
        env_store.set_env_var("K", "v")
        assert os.environ["K"] == "v"
        assert env_store.unset_env_var("K") is True
        assert read_back(env_home) == {}
        assert "K" not in os.environ

    def test_removes_an_export_form_definition(self, env_home: Path) -> None:
        (env_home / ".env").write_text("export K=v\nKEEP=1\n", encoding="utf-8")
        assert env_store.unset_env_var("K") is True
        assert read_back(env_home) == {"KEEP": "1"}

    def test_absent_key_reports_false_and_leaves_the_file_alone(self, env_home: Path) -> None:
        env_store.set_env_var("KEEP", "1")
        before = (env_home / ".env").read_text(encoding="utf-8")
        assert env_store.unset_env_var("MISSING") is False
        assert (env_home / ".env").read_text(encoding="utf-8") == before


class TestLiveApplication:
    def test_set_updates_os_environ_by_default(self, env_home: Path) -> None:
        env_store.set_env_var("LIVE_KEY", "now")
        assert os.environ["LIVE_KEY"] == "now"

    def test_apply_live_false_touches_only_the_file(self, env_home: Path) -> None:
        # The migration writer uses this: it prepares a file for the next start
        # without mutating the environment of the process doing the migrating.
        env_store.set_env_var("FILE_ONLY", "v", apply_live=False)
        assert "FILE_ONLY" not in os.environ
        assert read_back(env_home)["FILE_ONLY"] == "v"


class TestResolveEntry:
    def test_reports_home_file_when_that_is_the_source(self, env_home: Path) -> None:
        env_store.set_env_var("BASE_RPC_URL", "https://example.invalid")
        entry = env_store.resolve_entry("BASE_RPC_URL")
        assert entry.is_set is True
        assert entry.source == "home_file"
        assert entry.writable is True

    def test_reports_unset_for_an_unknown_name(self, env_home: Path) -> None:
        entry = env_store.resolve_entry("NEVER_SET_ANYWHERE")
        assert entry.is_set is False
        assert entry.source == "unset"
        assert entry.masked is None

    def test_flags_a_value_shadowed_by_the_process_environment(
        self, env_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # This is the precedence trap: os.environ wins at load, so editing the
        # file changes nothing until the export goes away. The UI needs to say so.
        env_store.set_env_var("SHADOWED_URL", "from-file", apply_live=False)
        monkeypatch.setenv("SHADOWED_URL", "from-shell")
        entry = env_store.resolve_entry("SHADOWED_URL")
        assert entry.source == "process"
        assert entry.masked == "from-shell"

    def test_reports_cwd_file_when_the_working_directory_defines_it(self, env_home: Path) -> None:
        Path(".env").write_text("PROJECT_ONLY=local\n", encoding="utf-8")
        entry = env_store.resolve_entry("PROJECT_ONLY")
        assert entry.source == "cwd_file"

    def test_secret_values_are_masked_and_plain_ones_are_not(self, env_home: Path) -> None:
        env_store.set_env_var("SOME_API_KEY", "sk-proj-" + "x" * 40 + "wxyz")
        env_store.set_env_var("SOME_BASE_URL", "https://example.invalid")
        secret = env_store.resolve_entry("SOME_API_KEY")
        plain = env_store.resolve_entry("SOME_BASE_URL")
        assert secret.masked is not None
        assert "xxxx" not in secret.masked
        assert plain.masked == "https://example.invalid"

    def test_denylisted_names_report_as_not_writable(self, env_home: Path) -> None:
        assert env_store.resolve_entry("PATH").writable is False


class TestValueReaders:
    def test_process_wins_by_default(self, env_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        env_store.set_env_var("DUAL", "from-file", apply_live=False)
        monkeypatch.setenv("DUAL", "from-shell")
        assert env_store.get_env_value("DUAL") == "from-shell"

    def test_prefer_file_lets_a_rotation_win(
        self, env_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Without this, rotating a key mid-session keeps serving the stale
        # shell value and every request keeps failing with a 401.
        env_store.set_env_var("DUAL", "rotated", apply_live=False)
        monkeypatch.setenv("DUAL", "stale")
        assert env_store.get_env_value_prefer_file("DUAL") == "rotated"

    def test_prefer_file_falls_back_to_the_process(
        self, env_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ONLY_IN_SHELL", "v")
        assert env_store.get_env_value_prefer_file("ONLY_IN_SHELL") == "v"


class TestReloadEnv:
    def test_applies_additions_and_updates(self, env_home: Path) -> None:
        env_store.set_env_var("A", "1", apply_live=False)
        env_store.set_env_var("B", "2", apply_live=False)
        assert env_store.reload_env() == 2
        assert os.environ["A"] == "1"
        assert os.environ["B"] == "2"

    def test_removes_only_known_keys_that_left_the_file(
        self, env_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KNOWN_TOKEN", "stale")
        monkeypatch.setenv("UNRELATED_SHELL_VAR", "keep-me")
        env_store.set_env_var("OTHER", "1", apply_live=False)

        # Only KNOWN_TOKEN is declared as AgentOS-managed.
        env_store.reload_env(known_keys={"KNOWN_TOKEN"})

        # KNOWN_TOKEN is absent from the file and known, so it goes...
        assert "KNOWN_TOKEN" not in os.environ
        # ...but a variable AgentOS was never told to manage must survive.
        assert os.environ["UNRELATED_SHELL_VAR"] == "keep-me"

    def test_unknown_keys_are_never_stripped(
        self, env_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SOMEONE_ELSES_VAR", "v")
        env_store.reload_env()
        assert os.environ["SOMEONE_ELSES_VAR"] == "v"
