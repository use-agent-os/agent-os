"""CLI tests for `agentos search`."""

from __future__ import annotations

from typer.testing import CliRunner

from agentos.cli.main import app

runner = CliRunner()


def test_search_list_shows_runtime_providers():
    result = runner.invoke(app, ["search", "list"])
    assert result.exit_code == 0, result.stdout
    assert "brave" in result.stdout
    assert "duckduckgo" in result.stdout


def test_search_configure_writes_config(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(target))
    result = runner.invoke(
        app,
        [
            "search",
            "configure",
            "brave",
            "--api-key",
            "brave-secret",
            "--max-results",
            "8",
        ],
    )
    assert result.exit_code == 0, result.stdout
    text = target.read_text()
    assert 'search_provider = "brave"' in text
    assert 'search_api_key = "brave-secret"' in text
    assert "brave-secret" not in result.stdout


def test_search_configure_warns_when_env_key_is_not_set(tmp_path, monkeypatch):
    target = tmp_path / "c.toml"
    monkeypatch.setenv("AGENTOS_GATEWAY_CONFIG_PATH", str(target))
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)

    result = runner.invoke(
        app,
        [
            "search",
            "configure",
            "brave",
            "--api-key-env",
            "BRAVE_SEARCH_API_KEY",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert "Warning" in result.stdout
    assert "BRAVE_SEARCH_API_KEY" in result.stdout
