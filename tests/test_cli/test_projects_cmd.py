"""``agentos projects`` CLI surface tests.

The gateway round-trip is stubbed at ``run_gateway_sync`` — these cover the
CLI surface (argument shapes, rendered output), not the RPC, which
``tests/test_gateway/test_rpc_projects.py`` owns.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from typer.testing import CliRunner

from agentos.cli import projects_cmd

runner = CliRunner()

_PROJECT = {
    "project_id": "proj-1",
    "projectId": "proj-1",
    "agent_id": "main",
    "name": "Research",
    "knowledge": "Shared facts.",
    "session_count": 2,
    "updated_at": 1000,
}


class _FakeClient:
    def __init__(self) -> None:
        self.create_calls: list[tuple[str, str, str]] = []
        self.update_calls: list[tuple[str, str | None, str | None]] = []
        self.delete_calls: list[str] = []
        self.move_calls: list[tuple[str, str | None]] = []

    async def list_projects(self, agent_id: str | None = None) -> dict[str, Any]:
        return {"projects": [_PROJECT], "count": 1}

    async def create_project(
        self, name: str, knowledge: str = "", agent_id: str = "main"
    ) -> dict[str, Any]:
        self.create_calls.append((name, knowledge, agent_id))
        return {"project": {**_PROJECT, "name": name, "knowledge": knowledge}}

    async def get_project(self, project_id: str) -> dict[str, Any]:
        return {"project": _PROJECT}

    async def update_project(
        self,
        project_id: str,
        name: str | None = None,
        knowledge: str | None = None,
    ) -> dict[str, Any]:
        self.update_calls.append((project_id, name, knowledge))
        return {"project": {**_PROJECT, "name": name or _PROJECT["name"]}}

    async def delete_project(self, project_id: str) -> dict[str, Any]:
        self.delete_calls.append(project_id)
        return {"deleted": True, "sessions_cleared": 2, "sessionsCleared": 2}

    async def resolve_session(self, key: str) -> dict[str, Any]:
        return {"session_key": key}

    async def move_session_to_project(
        self, key: str, project_id: str | None
    ) -> dict[str, Any]:
        self.move_calls.append((key, project_id))
        return {"key": key, "updated": ["projectId"]}

    async def call(self, method: str, params: dict | None = None) -> dict[str, Any]:
        return {"sessions": [{"key": "agent:main:webchat:aaa", "status": "running"}]}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    fake = _FakeClient()

    def _run(action, **kwargs):
        return asyncio.run(action(fake))

    monkeypatch.setattr(projects_cmd, "run_gateway_sync", _run)
    return fake


def test_list_renders_projects_table(client: _FakeClient) -> None:
    result = runner.invoke(projects_cmd.app, ["list"])
    assert result.exit_code == 0
    assert "Research" in result.output
    assert "proj-1" in result.output


def test_create_passes_name_knowledge_agent(client: _FakeClient) -> None:
    result = runner.invoke(
        projects_cmd.app,
        ["create", "Research", "--agent", "ops", "--knowledge", "Facts here"],
    )
    assert result.exit_code == 0
    assert client.create_calls == [("Research", "Facts here", "ops")]


def test_create_reads_knowledge_file(client: _FakeClient, tmp_path) -> None:
    knowledge_file = tmp_path / "knowledge.md"
    knowledge_file.write_text("From file", encoding="utf-8")
    result = runner.invoke(
        projects_cmd.app, ["create", "Research", "--knowledge-file", str(knowledge_file)]
    )
    assert result.exit_code == 0
    assert client.create_calls == [("Research", "From file", "main")]


def test_create_rejects_both_knowledge_sources(client: _FakeClient, tmp_path) -> None:
    knowledge_file = tmp_path / "knowledge.md"
    knowledge_file.write_text("x", encoding="utf-8")
    result = runner.invoke(
        projects_cmd.app,
        ["create", "Research", "--knowledge", "a", "--knowledge-file", str(knowledge_file)],
    )
    assert result.exit_code != 0


def test_show_renders_project_and_sessions(client: _FakeClient) -> None:
    result = runner.invoke(projects_cmd.app, ["show", "proj-1"])
    assert result.exit_code == 0
    assert "Research" in result.output
    assert "Shared facts." in result.output
    assert "agent:main:webchat:aaa" in result.output


def test_update_requires_a_field(client: _FakeClient) -> None:
    result = runner.invoke(projects_cmd.app, ["update", "proj-1"])
    assert result.exit_code != 0


def test_update_sends_knowledge(client: _FakeClient) -> None:
    result = runner.invoke(projects_cmd.app, ["update", "proj-1", "--knowledge", "v2"])
    assert result.exit_code == 0
    assert client.update_calls == [("proj-1", None, "v2")]


def test_delete_confirms_and_reports_detached(client: _FakeClient) -> None:
    result = runner.invoke(projects_cmd.app, ["delete", "proj-1", "--yes"])
    assert result.exit_code == 0
    assert client.delete_calls == ["proj-1"]
    assert "2 session(s) detached" in result.output


def test_move_and_detach(client: _FakeClient) -> None:
    result = runner.invoke(projects_cmd.app, ["move", "agent:main:webchat:aaa", "proj-1"])
    assert result.exit_code == 0
    result = runner.invoke(projects_cmd.app, ["move", "agent:main:webchat:aaa", "none"])
    assert result.exit_code == 0
    assert client.move_calls == [
        ("agent:main:webchat:aaa", "proj-1"),
        ("agent:main:webchat:aaa", None),
    ]


# ── Rich markup: `!r` quotes a value but does not escape Rich markup, so a ──
# ── bracketed id used to crash the confirmation line printed AFTER the ──────
# ── mutation had already gone through. ───────────────────────────────────────


def test_delete_survives_a_closing_tag_in_the_project_id(client: _FakeClient) -> None:
    result = runner.invoke(projects_cmd.app, ["delete", "evil[/]", "--yes"])

    assert result.exit_code == 0, result.output
    assert client.delete_calls == ["evil[/]"]
    assert "evil[/]" in result.output


def test_move_survives_a_closing_tag_in_the_target_project_id(client: _FakeClient) -> None:
    result = runner.invoke(projects_cmd.app, ["move", "agent:main:webchat:aaa", "work[/]"])

    assert result.exit_code == 0, result.output
    assert client.move_calls == [("agent:main:webchat:aaa", "work[/]")]
    assert "work[/]" in result.output


def test_detach_survives_a_closing_tag_in_the_session_key(client: _FakeClient) -> None:
    result = runner.invoke(projects_cmd.app, ["move", "session[/]", "none"])

    assert result.exit_code == 0, result.output
    assert client.move_calls == [("session[/]", None)]
    assert "session[/]" in result.output
