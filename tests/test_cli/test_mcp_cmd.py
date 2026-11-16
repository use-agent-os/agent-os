"""``agentos mcp`` — config-only management plus a live probe against a FastMCP server."""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentos.cli.main import app

runner = CliRunner()

DEMO_SERVER = Path(__file__).resolve().parents[1] / "fixtures" / "mcp" / "fastmcp_demo_server.py"


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "agentos.toml"
    path.write_text('[llm]\nmodel = "x/y"\n\n[skills]\nfilter_top_k = 7\n', encoding="utf-8")
    return path


def _run(*args: str):
    return runner.invoke(app, ["mcp", *args])


def _servers(path: Path) -> list[dict]:
    return tomllib.loads(path.read_text(encoding="utf-8")).get("mcp", {}).get("servers", [])


def _add_demo(config_file: Path, *extra: str):
    return _run(
        "add",
        "demo",
        "--command",
        sys.executable,
        "--arg",
        str(DEMO_SERVER),
        "--config",
        str(config_file),
        *extra,
    )


def test_add_stdio_server_writes_every_option(config_file: Path, tmp_path: Path) -> None:
    result = _run(
        "add",
        "fs",
        "--command",
        "npx",
        "--arg",
        "-y",
        "--arg",
        "@modelcontextprotocol/server-filesystem",
        "--cwd",
        str(tmp_path),
        "--env",
        "TOKEN=abc",
        "--env",
        "MODE=a=b",
        "--include",
        "search,read_*",
        "--exclude",
        "write_*",
        "--connect-timeout",
        "9",
        "--config",
        str(config_file),
        "--json",
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["action"] == "added"
    assert payload["mcpEnabled"] is True
    data = tomllib.loads(config_file.read_text(encoding="utf-8"))
    # Unrelated settings survive the rewrite.
    assert data["llm"]["model"] == "x/y"
    assert data["skills"]["filter_top_k"] == 7
    # The first server switches the MCP client on.
    assert data["mcp"]["enabled"] is True
    (server,) = data["mcp"]["servers"]
    assert server["name"] == "fs"
    assert server["transport"] == "stdio"
    assert server["enabled"] is True
    assert server["args"] == ["-y", "@modelcontextprotocol/server-filesystem"]
    assert server["cwd"] == str(tmp_path)
    assert server["env"] == {"TOKEN": "abc", "MODE": "a=b"}
    assert server["tool_filter"] == {"include": ["search", "read_*"], "exclude": ["write_*"]}
    assert server["connect_timeout_seconds"] == 9.0


def test_add_url_server_defaults_to_streamable_http(config_file: Path) -> None:
    result = _run(
        "add",
        "remote",
        "--url",
        "https://mcp.example.com/mcp",
        "--header",
        "X-Team=a",
        "--disabled",
        "--config",
        str(config_file),
    )
    assert result.exit_code == 0, result.output
    (server,) = _servers(config_file)
    assert server["transport"] == "streamable_http"
    assert server["headers"] == {"X-Team": "a"}
    assert server["enabled"] is False


@pytest.mark.parametrize(
    "args",
    [
        ["--command", "x", "--url", "https://a"],
        [],
        ["--url", "https://a", "--cwd", "/tmp"],
        ["--command", "x", "--header", "A=b"],
        ["--command", "x", "--env", "NOEQUALS"],
        ["--command", "x", "--transport", "carrier"],
        ["--url", "https://a", "--transport", "sse", "--oauth"],
        ["--command", "x", "--connect-timeout", "0"],
    ],
)
def test_add_rejects_bad_input(config_file: Path, args: list[str]) -> None:
    before = config_file.read_text(encoding="utf-8")
    result = _run("add", "bad", *args, "--config", str(config_file), "--json")
    assert result.exit_code == 2, result.output
    assert json.loads(result.stderr)["error"]["code"] == "INVALID_ARGUMENT"
    assert config_file.read_text(encoding="utf-8") == before


def test_add_refuses_duplicate_unless_replace(config_file: Path) -> None:
    assert _run("add", "a", "--command", "x", "--config", str(config_file)).exit_code == 0
    assert _run("add", "b", "--command", "y", "--config", str(config_file)).exit_code == 0

    dup = _run("add", "a", "--command", "z", "--config", str(config_file), "--json")
    assert dup.exit_code == 1
    assert json.loads(dup.stderr)["error"]["code"] == "ALREADY_EXISTS"

    replaced = _run("add", "a", "--command", "z", "--replace", "--config", str(config_file))
    assert replaced.exit_code == 0, replaced.output
    # Replacing keeps the server's place: config order decides tool-name precedence.
    assert [(s["name"], s["command"]) for s in _servers(config_file)] == [("a", "z"), ("b", "y")]


def test_add_keeps_an_explicit_runtime_off_switch(config_file: Path) -> None:
    config_file.write_text(
        '[mcp]\nenabled = false\n\n[[mcp.servers]]\nname = "old"\ncommand = "x"\n',
        encoding="utf-8",
    )
    result = _run("add", "new", "--command", "y", "--config", str(config_file))
    assert result.exit_code == 0, result.output
    assert "mcp.enabled is false" in result.output
    data = tomllib.loads(config_file.read_text(encoding="utf-8"))
    assert data["mcp"]["enabled"] is False


def test_enable_disable_remove_round_trip(config_file: Path) -> None:
    assert _run("add", "a", "--command", "x", "--config", str(config_file)).exit_code == 0

    off = _run("disable", "a", "--config", str(config_file), "--json")
    assert off.exit_code == 0, off.output
    assert json.loads(off.stdout)["action"] == "disabled"
    assert _servers(config_file)[0]["enabled"] is False

    assert _run("enable", "a", "--config", str(config_file)).exit_code == 0
    assert _servers(config_file)[0]["enabled"] is True

    gone = _run("remove", "a", "--config", str(config_file), "--json")
    assert gone.exit_code == 0, gone.output
    assert _servers(config_file) == []


@pytest.mark.parametrize("command", ["remove", "enable", "disable", "probe", "doctor"])
def test_unknown_server_is_not_found(config_file: Path, command: str) -> None:
    result = _run(command, "ghost", "--config", str(config_file), "--json")
    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["code"] == "NOT_FOUND"


def test_list_and_status_json(config_file: Path) -> None:
    _run("add", "a", "--command", "x", "--env", "SECRET=v", "--config", str(config_file))
    _run("add", "b", "--url", "https://h/mcp", "--disabled", "--config", str(config_file))

    listed = json.loads(_run("list", "--config", str(config_file), "--json").stdout)
    assert [row["name"] for row in listed["servers"]] == ["a", "b"]
    # Env values are never printed, only their names.
    assert listed["servers"][0]["envKeys"] == ["SECRET"]
    assert "v" not in json.dumps(listed["servers"][0]["envKeys"])
    assert "env" not in listed["servers"][0]

    status = json.loads(_run("status", "--config", str(config_file), "--json").stdout)
    assert status["counts"] == {"total": 2, "enabled": 1, "disabled": 1}
    assert status["enabled"] is True


def test_list_with_no_servers(config_file: Path) -> None:
    result = _run("list", "--config", str(config_file))
    assert result.exit_code == 0
    assert "No MCP servers configured" in result.output


# ── live probe against the FastMCP fixture ───────────────────────────


def test_probe_lists_tools_and_applies_the_filter(config_file: Path) -> None:
    assert (
        _add_demo(config_file, "--include", "read_*,search", "--exclude", "read_dir").exit_code == 0
    )

    result = _run("probe", "demo", "--config", str(config_file), "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    tools = {tool["name"]: tool["registered"] for tool in payload["tools"]}
    assert tools == {
        "search": True,
        "read_file": True,
        "read_dir": False,
        "delete_all": False,
        "where": False,
    }


def test_probe_reports_an_unreachable_server(config_file: Path, tmp_path: Path) -> None:
    missing = tmp_path / "no-such-binary"
    assert (
        _run("add", "dead", "--command", str(missing), "--config", str(config_file)).exit_code == 0
    )

    result = _run("probe", "dead", "--config", str(config_file), "--json")

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]


def test_doctor_flags_a_missing_cwd_and_skips_the_probe(config_file: Path, tmp_path: Path) -> None:
    assert _add_demo(config_file, "--cwd", str(tmp_path / "gone")).exit_code == 0

    result = _run("doctor", "demo", "--probe", "--config", str(config_file), "--json")

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert {"level": "error", "check": "cwd"}.items() <= next(
        c for c in payload["checks"] if c["check"] == "cwd"
    ).items()
    assert payload["probe"] is None


def test_doctor_probe_warns_about_a_filter_pattern_that_matches_nothing(config_file: Path) -> None:
    assert _add_demo(config_file, "--include", "search,nothing_*").exit_code == 0

    result = _run("doctor", "demo", "--probe", "--config", str(config_file), "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["probe"]["ok"] is True
    warnings = [c for c in payload["checks"] if c["level"] == "warn"]
    assert any("nothing_*" in c["message"] for c in warnings)


def test_doctor_without_probe_validates_only(config_file: Path) -> None:
    assert _run("add", "r", "--url", "https://h/mcp", "--config", str(config_file)).exit_code == 0
    result = _run("doctor", "r", "--config", str(config_file))
    assert result.exit_code == 0, result.output
    assert "url" in result.output
