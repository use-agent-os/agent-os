"""CLI tests for ``agentos config set``."""

from __future__ import annotations

from pathlib import Path

import pytest
import tomli_w
from typer.testing import CliRunner

from agentos.cli.config_cmd import _set_key
from agentos.cli.main import app
from agentos.gateway.config import GatewayConfig
from agentos.onboarding.config_store import load_config
from agentos.skills.config_vars import _value_at

runner = CliRunner()


def test_set_key_creates_nested_skills_config() -> None:
    data = GatewayConfig().to_toml_dict()
    assert "config" not in data.get("skills", {})
    declared = GatewayConfig().model_dump()
    assert _set_key(data, "skills.config.wiki.path", "/srv/wiki", declared=declared) is True
    assert data["skills"]["config"]["wiki"]["path"] == "/srv/wiki"
    cfg = GatewayConfig.model_validate(data)
    assert _value_at(cfg, "wiki.path") == "/srv/wiki"


def test_set_key_rejects_unknown_keys_outside_skills_config() -> None:
    data = GatewayConfig().to_toml_dict()
    declared = GatewayConfig().model_dump()
    assert _set_key(data, "skills.no_such_key", "x", declared=declared) is False
    assert _set_key(data, "skills.max_skills_prompt_chars", 32000, declared=declared) is True


def test_config_set_persists_documented_wiki_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path / "state"))
    cfg_path = tmp_path / "config.toml"
    result = runner.invoke(
        app,
        [
            "config",
            "set",
            "skills.config.wiki.path",
            "/srv/wiki",
            "--config",
            str(cfg_path),
        ],
    )
    assert result.exit_code == 0, result.output
    loaded = load_config(cfg_path)
    assert _value_at(loaded, "wiki.path") == "/srv/wiki"


def test_config_set_existing_model_field_still_works(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path / "state"))
    cfg_path = tmp_path / "config.toml"
    result = runner.invoke(
        app,
        [
            "config",
            "set",
            "skills.max_skills_prompt_chars",
            "32000",
            "--config",
            str(cfg_path),
        ],
    )
    assert result.exit_code == 0, result.output
    loaded = load_config(cfg_path)
    assert loaded.skills.max_skills_prompt_chars == 32000


def test_config_set_unknown_key_still_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path / "state"))
    cfg_path = tmp_path / "config.toml"
    result = runner.invoke(
        app,
        ["config", "set", "skills.no_such_key", "x", "--config", str(cfg_path)],
    )
    assert result.exit_code == 1
    assert "Key not found" in result.output
    assert not cfg_path.exists()


def test_config_set_env_hint_rejects_unknown_key() -> None:
    result = runner.invoke(app, ["config", "set", "gateway.port", "18791"])
    assert result.exit_code == 1
    assert "Key not found" in result.output
    assert "export " not in result.output.lower()


def test_config_set_env_hint_rejects_skills_config_map() -> None:
    result = runner.invoke(app, ["config", "set", "skills.config.wiki.path", "/srv/wiki"])
    assert result.exit_code == 1
    assert "Key not found" in result.output
    assert "export " not in result.output.lower()


def test_config_set_env_hint_still_prints_for_existing_field() -> None:
    result = runner.invoke(app, ["config", "set", "skills.max_skills_prompt_chars", "32000"])
    assert result.exit_code == 0, result.output
    assert "export AGENTOS_GATEWAY_SKILLS__MAX_SKILLS_PROMPT_CHARS=32000" in result.output


# Issue #2031: ``to_toml_dict()`` is ``model_dump(exclude_none=True)``, so a
# declared key whose value is null was absent from the view both branches of
# ``config set`` checked against and read as a typo -- while ``config get``
# printed it happily. Once refused it stayed null, so it could never be set.


def test_set_key_accepts_a_declared_key_whose_value_is_null() -> None:
    data = GatewayConfig().to_toml_dict()
    assert "token" not in data.get("auth", {})
    assert _set_key(data, "auth.token", "s3cr3t", declared=GatewayConfig().model_dump()) is True
    assert data["auth"]["token"] == "s3cr3t"
    assert GatewayConfig.model_validate(data).auth.token == "s3cr3t"


def test_set_key_creates_a_table_the_toml_view_omitted() -> None:
    data = GatewayConfig().to_toml_dict()
    data.pop("auth", None)
    assert _set_key(data, "auth.token", "s3cr3t", declared=GatewayConfig().model_dump()) is True
    assert data["auth"]["token"] == "s3cr3t"


def test_set_key_still_rejects_an_undeclared_key() -> None:
    data = GatewayConfig().to_toml_dict()
    declared = GatewayConfig().model_dump()
    assert _set_key(data, "auth.no_such_key", "x", declared=declared) is False
    assert _set_key(data, "no_such_section.token", "x", declared=declared) is False
    assert _set_key(data, "auth.token.nested", "x", declared=declared) is False


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("auth.token", "s3cr3t", "s3cr3t"),
        ("auth.password", "hunter2hunter2", "hunter2hunter2"),
        ("workspace_strict", "true", True),
        ("task_runtime.turn_hard_deadline_s", "120", 120),
    ],
)
def test_config_set_persists_a_null_defaulted_key(
    tmp_path: Path, monkeypatch, key: str, value: str, expected: object
) -> None:
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path / "state"))
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(tomli_w.dumps(GatewayConfig().to_toml_dict()), encoding="utf-8")

    result = runner.invoke(app, ["config", "set", key, value, "--config", str(cfg_path)])

    assert result.exit_code == 0, result.output
    loaded = load_config(cfg_path)
    cursor: object = loaded
    for part in key.split("."):
        cursor = getattr(cursor, part)
    assert cursor == expected


def test_config_set_can_be_read_back_by_config_get(tmp_path: Path, monkeypatch) -> None:
    """A key ``config get`` can print is a key ``config set`` can write."""
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path / "state"))
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(tomli_w.dumps(GatewayConfig().to_toml_dict()), encoding="utf-8")

    before = runner.invoke(app, ["config", "get", "auth.token", "--config", str(cfg_path)])
    assert before.exit_code == 0, before.output
    assert "auth.token = None" in before.output.replace("'", "")

    result = runner.invoke(
        app, ["config", "set", "auth.token", "s3cr3t", "--config", str(cfg_path)]
    )
    assert result.exit_code == 0, result.output
    assert load_config(cfg_path).auth.token == "s3cr3t"


def test_config_set_env_hint_prints_for_a_null_defaulted_key() -> None:
    result = runner.invoke(app, ["config", "set", "auth.token", "s3cr3t"])
    assert result.exit_code == 0, result.output
    assert "export AGENTOS_GATEWAY_AUTH__TOKEN=s3cr3t" in result.output
