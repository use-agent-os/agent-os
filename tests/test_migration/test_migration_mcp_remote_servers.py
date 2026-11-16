"""A remote MCP server keeps its transport and headers through migration."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

from agentos.migration._mcp import headers, remote_transport
from agentos.migration.hermes import HermesMigrationOptions, HermesMigrator
from agentos.migration.openclaw import MigrationOptions, OpenClawMigrator

_AUTH = {"Authorization": "Bearer tok"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"transport": "streamable-http"}, "streamable_http"),
        ({"transport": "streamable_http"}, "streamable_http"),
        ({"transport": " Streamable-HTTP "}, "streamable_http"),
        ({"transport": "http"}, "streamable_http"),
        ({"transport": "sse"}, "sse"),
        # No transport named, or one we do not know: the previous default.
        ({}, "sse"),
        ({"transport": None}, "sse"),
        ({"transport": "carrier-pigeon"}, "sse"),
    ],
)
def test_remote_transport(raw: dict[str, Any], expected: str) -> None:
    assert remote_transport(raw) == expected


def test_headers_keeps_string_pairs_only() -> None:
    assert headers({"headers": {"A": "1", "B": 2, "C": None}}) == {"A": "1", "B": "2"}
    assert headers({"headers": ["Authorization: x"]}) == {}
    assert headers({}) == {}


def _openclaw_servers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, servers: dict) -> list:
    source = tmp_path / ".openclaw"
    source.mkdir()
    (source / "openclaw.json").write_text(json.dumps({"mcp": {"servers": servers}}))
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path / "home"))
    config_path = tmp_path / "agentos.toml"
    OpenClawMigrator(MigrationOptions(source=source, config_path=config_path, apply=True)).migrate()
    return tomllib.loads(config_path.read_text(encoding="utf-8"))["mcp"]["servers"]


def _hermes_servers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, yaml_text: str) -> list:
    source = tmp_path / ".hermes"
    source.mkdir()
    (source / "config.yaml").write_text(yaml_text, encoding="utf-8")
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path / "home"))
    config_path = tmp_path / "agentos.toml"
    HermesMigrator(
        HermesMigrationOptions(source=source, config_path=config_path, apply=True)
    ).migrate()
    return tomllib.loads(config_path.read_text(encoding="utf-8"))["mcp"]["servers"]


def test_openclaw_streamable_http_server_keeps_transport_and_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (server,) = _openclaw_servers(
        tmp_path,
        monkeypatch,
        {
            "remote": {
                "url": "https://mcp.example.com/mcp",
                "transport": "streamable-http",
                "headers": _AUTH,
            }
        },
    )
    assert server["transport"] == "streamable_http"
    assert server["url"] == "https://mcp.example.com/mcp"
    assert server["headers"] == _AUTH


def test_openclaw_url_server_without_transport_stays_sse_and_keeps_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (server,) = _openclaw_servers(
        tmp_path,
        monkeypatch,
        {"legacy": {"url": "https://mcp.example.com/sse", "headers": _AUTH}},
    )
    assert server["transport"] == "sse"
    assert server["headers"] == _AUTH


def test_openclaw_stdio_server_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (server,) = _openclaw_servers(
        tmp_path,
        monkeypatch,
        {"local": {"command": "npx", "args": ["-y", "srv"], "env": {"K": "v"}}},
    )
    assert server["transport"] == "stdio"
    assert server["command"] == "npx"
    assert server["env"] == {"K": "v"}
    assert server["headers"] == {}


def test_openclaw_headers_and_transport_are_not_reported_as_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / ".openclaw"
    source.mkdir()
    (source / "openclaw.json").write_text(
        json.dumps(
            {
                "mcp": {
                    "servers": {
                        "remote": {
                            "url": "https://mcp.example.com/mcp",
                            "transport": "streamable-http",
                            "headers": _AUTH,
                            "cwd": "/srv",
                        }
                    }
                }
            }
        )
    )
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path / "home"))
    report = OpenClawMigrator(
        MigrationOptions(source=source, config_path=tmp_path / "agentos.toml", apply=True)
    ).migrate()
    (item,) = [i for i in report["items"] if i["kind"] == "mcp-servers"]
    # Only the field AgentOS really has no home for is named.
    assert item["details"]["unsupported_fields"] == {"remote": ["cwd"]}


def test_hermes_streamable_http_server_keeps_transport_and_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (server,) = _hermes_servers(
        tmp_path,
        monkeypatch,
        "mcp:\n  servers:\n    remote:\n"
        "      url: https://mcp.example.com/mcp\n"
        "      transport: streamable-http\n"
        "      headers:\n        Authorization: Bearer tok\n",
    )
    assert server["transport"] == "streamable_http"
    assert server["headers"] == _AUTH


def test_hermes_url_server_without_transport_stays_sse_and_keeps_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (server,) = _hermes_servers(
        tmp_path,
        monkeypatch,
        "mcp:\n  servers:\n    legacy:\n"
        "      url: https://mcp.example.com/sse\n"
        "      headers:\n        Authorization: Bearer tok\n",
    )
    assert server["transport"] == "sse"
    assert server["headers"] == _AUTH


def test_hermes_command_server_ignores_a_stray_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (server,) = _hermes_servers(
        tmp_path,
        monkeypatch,
        "mcp:\n  servers:\n    local:\n      command: /usr/bin/x\n      transport: sse\n",
    )
    assert server["transport"] == "stdio"
    assert server["headers"] == {}


def test_openclaw_enabled_cwd_timeout_and_tool_filter_are_migrated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (server,) = _openclaw_servers(
        tmp_path,
        monkeypatch,
        {
            "local": {
                "command": "npx",
                "cwd": "/srv/tools",
                "enabled": False,
                "connectionTimeoutMs": 2500,
                "toolFilter": {"include": ["search", "read_*"], "exclude": ["read_secret"]},
            }
        },
    )
    assert server["enabled"] is False
    assert server["cwd"] == "/srv/tools"
    assert server["connect_timeout_seconds"] == 2.5
    assert server["tool_filter"] == {"include": ["search", "read_*"], "exclude": ["read_secret"]}
