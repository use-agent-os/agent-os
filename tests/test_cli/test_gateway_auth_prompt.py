"""Tests for the interactive public-bind auth provisioning prompt (Part A)."""

from __future__ import annotations

import tomllib

import pytest

from agentos.cli.gateway_auth_prompt import (
    AuthProvisionOutcome,
    provision_public_bind_auth,
)
from agentos.gateway.config import AuthConfig, GatewayConfig


@pytest.fixture(autouse=True)
def _clean_auth_env(monkeypatch, tmp_path):
    """Keep ambient auth env vars from leaking into constructed configs."""
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path / "home"))
    for var in (
        "AGENTOS_AUTH_MODE",
        "AGENTOS_AUTH_TOKEN",
        "AGENTOS_AUTH_ALLOW_UNAUTHENTICATED_PUBLIC",
    ):
        monkeypatch.delenv(var, raising=False)


def _config(tmp_path, *, host: str = "0.0.0.0", auth: AuthConfig | None = None) -> GatewayConfig:
    return GatewayConfig(
        host=host,
        auth=auth if auth is not None else AuthConfig(),
        config_path=str(tmp_path / "agentos.toml"),
    )


def _no_prompt(_msg: str) -> str:
    raise AssertionError("prompt must not be called")


# --- UNCHANGED paths -------------------------------------------------------


def test_loopback_bind_is_unchanged_and_silent(tmp_path) -> None:
    config = _config(tmp_path, host="127.0.0.1")
    emitted: list[str] = []

    outcome, result = provision_public_bind_auth(
        config, interactive=True, prompt=_no_prompt, emit=emitted.append
    )

    assert outcome is AuthProvisionOutcome.UNCHANGED
    assert result is config
    assert emitted == []


def test_public_bind_with_token_mode_is_unchanged(tmp_path) -> None:
    config = _config(tmp_path, auth=AuthConfig(mode="token", token="secret"))

    outcome, result = provision_public_bind_auth(
        config, interactive=True, prompt=_no_prompt, emit=lambda _msg: None
    )

    assert outcome is AuthProvisionOutcome.UNCHANGED
    assert result is config


def test_public_bind_with_opt_in_is_unchanged_and_warns_lan_open(tmp_path) -> None:
    config = _config(tmp_path, auth=AuthConfig(mode="none", allow_unauthenticated_public=True))
    emitted: list[str] = []

    outcome, result = provision_public_bind_auth(
        config, interactive=True, prompt=_no_prompt, emit=emitted.append
    )

    assert outcome is AuthProvisionOutcome.UNCHANGED
    assert result is config
    output = "\n".join(emitted)
    assert "wildcard" in output
    assert "LAN-open" in output


def test_public_bind_non_interactive_is_unchanged_without_prompt(tmp_path) -> None:
    """Non-TTY never prompts — the startup guard raises downstream as today."""
    config = _config(tmp_path)
    emitted: list[str] = []

    outcome, result = provision_public_bind_auth(
        config, interactive=False, prompt=_no_prompt, emit=emitted.append
    )

    assert outcome is AuthProvisionOutcome.UNCHANGED
    assert result is config
    output = "\n".join(emitted)
    # The wildcard warning still prints, but not the contradictory LAN-open
    # notice (the guard is about to refuse this combination).
    assert "wildcard" in output
    assert "LAN-open" not in output


# --- interactive choices ---------------------------------------------------


def test_choice_1_generates_token_and_persists(tmp_path) -> None:
    config = _config(tmp_path)
    emitted: list[str] = []

    outcome, result = provision_public_bind_auth(
        config, interactive=True, prompt=lambda _msg: "1", emit=emitted.append
    )

    assert outcome is AuthProvisionOutcome.PROCEED
    assert result.auth.mode == "token"
    assert result.auth.token
    assert len(result.auth.token) >= 32
    # The operator must be shown the token or they can never authenticate.
    assert any(result.auth.token in line for line in emitted)

    with open(tmp_path / "agentos.toml", "rb") as f:
        persisted = tomllib.load(f)
    assert persisted["auth"]["mode"] == "token"
    assert persisted["auth"]["token"] == result.auth.token


def test_choice_1_persists_only_auth_not_one_off_cli_flags(tmp_path) -> None:
    """Provisioning a token must NOT freeze one-off CLI flags (host, port,
    debug that run_gateway injected via model_copy) into the config file.
    Only the auth change is persisted; the on-disk bind/debug stay as they
    were so a plain `agentos gateway run` next time is unaffected."""
    import tomli_w

    cfg_path = tmp_path / "agentos.toml"
    # Seed the on-disk config as a plain loopback, non-debug deployment.
    with open(cfg_path, "wb") as f:
        tomli_w.dump({"host": "127.0.0.1", "debug": False}, f)

    # The in-memory config carries one-off CLI overrides (public bind + debug).
    config = GatewayConfig(
        host="0.0.0.0",
        debug=True,
        auth=AuthConfig(),
        config_path=str(cfg_path),
    )

    outcome, result = provision_public_bind_auth(
        config, interactive=True, prompt=lambda _msg: "1", emit=lambda _m: None
    )

    assert outcome is AuthProvisionOutcome.PROCEED
    assert result.auth.mode == "token"

    with open(cfg_path, "rb") as f:
        persisted = tomllib.load(f)
    # Auth WAS persisted...
    assert persisted["auth"]["mode"] == "token"
    assert persisted["auth"]["token"] == result.auth.token
    # ...but the one-off CLI overrides were NOT frozen into the file.
    assert persisted.get("host") == "127.0.0.1"
    assert persisted.get("debug") is False


def test_empty_input_defaults_to_token_choice(tmp_path) -> None:
    config = _config(tmp_path)

    outcome, result = provision_public_bind_auth(
        config, interactive=True, prompt=lambda _msg: "", emit=lambda _msg: None
    )

    assert outcome is AuthProvisionOutcome.PROCEED
    assert result.auth.mode == "token"
    assert result.auth.token


def test_invalid_input_defaults_to_token_choice(tmp_path) -> None:
    config = _config(tmp_path)

    outcome, result = provision_public_bind_auth(
        config, interactive=True, prompt=lambda _msg: "banana", emit=lambda _msg: None
    )

    assert outcome is AuthProvisionOutcome.PROCEED
    assert result.auth.mode == "token"


def test_choice_2_break_glass_is_session_only(tmp_path) -> None:
    config = _config(tmp_path)
    emitted: list[str] = []

    outcome, result = provision_public_bind_auth(
        config, interactive=True, prompt=lambda _msg: "2", emit=emitted.append
    )

    assert outcome is AuthProvisionOutcome.PROCEED
    assert result.auth.allow_unauthenticated_public is True
    assert result.auth.mode == "none"
    assert "LAN-open" in "\n".join(emitted)
    # Break-glass must NOT be written to disk — the stored config stays safe.
    assert not (tmp_path / "agentos.toml").exists()


def test_choice_3_cancels(tmp_path) -> None:
    config = _config(tmp_path)

    outcome, result = provision_public_bind_auth(
        config, interactive=True, prompt=lambda _msg: "3", emit=lambda _msg: None
    )

    assert outcome is AuthProvisionOutcome.CANCEL
    assert result is config
    assert not (tmp_path / "agentos.toml").exists()


@pytest.mark.parametrize("exc", [EOFError, KeyboardInterrupt])
def test_prompt_interrupt_is_cancel(tmp_path, exc) -> None:
    config = _config(tmp_path)

    def raising_prompt(_msg: str) -> str:
        raise exc

    outcome, result = provision_public_bind_auth(
        config, interactive=True, prompt=raising_prompt, emit=lambda _msg: None
    )

    assert outcome is AuthProvisionOutcome.CANCEL
    assert result is config


def test_persist_failure_warns_but_still_proceeds(tmp_path, monkeypatch) -> None:
    from agentos.cli import gateway_auth_prompt

    def failing_persist(_config: object) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(gateway_auth_prompt, "persist_config", failing_persist)
    config = _config(tmp_path)
    emitted: list[str] = []

    outcome, result = provision_public_bind_auth(
        config, interactive=True, prompt=lambda _msg: "1", emit=emitted.append
    )

    assert outcome is AuthProvisionOutcome.PROCEED
    assert result.auth.mode == "token"
    assert result.auth.token
    output = "\n".join(emitted)
    assert "could not persist" in output.lower() or "persist" in output.lower()


# --- shared persistence path ----------------------------------------------


def test_rpc_persist_path_is_the_shared_helper() -> None:
    """The RPC config write path and the CLI prompt must share one writer."""
    from agentos.gateway import config_persist, rpc_config

    assert rpc_config._persist_config is config_persist.persist_config


def test_persist_config_writes_toml_round_trip(tmp_path) -> None:
    from agentos.gateway.config_persist import persist_config

    config = _config(tmp_path, auth=AuthConfig(mode="token", token="abc123"))
    persist_config(config)

    with open(tmp_path / "agentos.toml", "rb") as f:
        persisted = tomllib.load(f)
    assert persisted["auth"]["mode"] == "token"
    assert persisted["auth"]["token"] == "abc123"


# --- CLI integration (gateway run call site) -------------------------------


def _invoke_gateway_run(tmp_path, monkeypatch, *, stdin: str, captured: dict):
    from typer.testing import CliRunner

    from agentos.cli import gateway_cmd
    from agentos.cli.main import app

    target = tmp_path / "agentos.toml"
    if not target.exists():
        target.write_text("", encoding="utf-8")
    monkeypatch.setattr(gateway_cmd, "_stdin_isatty", lambda: True)

    async def fake_start_gateway_server(**kwargs):
        captured["config"] = kwargs.get("config")
        raise KeyboardInterrupt

    monkeypatch.setattr(gateway_cmd, "start_gateway_server", fake_start_gateway_server)

    runner = CliRunner()
    return runner.invoke(
        app,
        ["gateway", "run", "--config", str(target), "--listen", "0.0.0.0"],
        input=stdin,
    )


def test_gateway_run_cancel_exits_zero_without_starting(tmp_path, monkeypatch) -> None:
    captured: dict = {}

    result = _invoke_gateway_run(tmp_path, monkeypatch, stdin="3\n", captured=captured)

    assert result.exit_code == 0
    assert "config" not in captured  # server never started


def test_gateway_run_token_choice_starts_with_token_auth(tmp_path, monkeypatch) -> None:
    captured: dict = {}

    result = _invoke_gateway_run(tmp_path, monkeypatch, stdin="1\n", captured=captured)

    assert result.exit_code == 0
    assert captured["config"].auth.mode == "token"
    assert captured["config"].auth.token
    # Token was persisted, so the next run will not prompt again.
    with open(tmp_path / "agentos.toml", "rb") as f:
        persisted = tomllib.load(f)
    assert persisted["auth"]["token"] == captured["config"].auth.token


def test_gateway_run_break_glass_starts_without_persisting(tmp_path, monkeypatch) -> None:
    captured: dict = {}

    result = _invoke_gateway_run(tmp_path, monkeypatch, stdin="2\n", captured=captured)

    assert result.exit_code == 0
    assert captured["config"].auth.allow_unauthenticated_public is True
    assert (tmp_path / "agentos.toml").read_text(encoding="utf-8") == ""
    assert "LAN-open" in result.stdout
