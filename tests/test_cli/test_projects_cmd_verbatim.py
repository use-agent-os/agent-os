"""``agentos projects delete/move`` confirm the exact ids that were acted on.

``!r`` only adds quotes — it does not escape Rich markup — so a project or
session id containing a ``[...]`` sequence was silently mangled, and one with
a ``[/]``-shaped closing tag raised ``MarkupError`` AFTER the mutation had
already been sent to the gateway. ``create``/``show``/``update`` in the same
file already escape with ``markup_escape``; these are the remaining sites.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from typer.testing import CliRunner

from agentos.cli import projects_cmd

runner = CliRunner()


class _FakeClient:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.moves: list[tuple[str, str | None]] = []

    async def delete_project(self, project_id: str) -> dict[str, Any]:
        self.deleted.append(project_id)
        return {"sessions_cleared": 3}

    async def resolve_session(self, session_id: str) -> dict[str, Any]:
        return {"session_key": session_id}

    async def move_session_to_project(self, key: str, target: str | None) -> dict[str, Any]:
        self.moves.append((key, target))
        return {"moved": True}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    fake = _FakeClient()

    def _run(action, **_kwargs):
        return asyncio.run(action(fake))

    monkeypatch.setattr(projects_cmd, "run_gateway_sync", _run)
    return fake


def test_delete_confirms_a_closing_tag_shaped_project_id(client: _FakeClient) -> None:
    result = runner.invoke(projects_cmd.app, ["delete", "evil[/]", "--yes"])

    assert result.exit_code == 0, result.output
    assert client.deleted == ["evil[/]"]
    assert "evil[/]" in result.stdout


def test_delete_confirms_a_bracketed_project_id(client: _FakeClient) -> None:
    result = runner.invoke(projects_cmd.app, ["delete", "client [redacted]", "--yes"])

    assert result.exit_code == 0, result.output
    assert "client [redacted]" in result.stdout


def test_move_confirms_a_closing_tag_shaped_target(client: _FakeClient) -> None:
    result = runner.invoke(projects_cmd.app, ["move", "some-session", "work[/]"])

    assert result.exit_code == 0, result.output
    assert client.moves == [("some-session", "work[/]")]
    assert "work[/]" in result.stdout


def test_move_confirms_a_bracketed_session_id(client: _FakeClient) -> None:
    result = runner.invoke(projects_cmd.app, ["move", "client [redacted]", "work"])

    assert result.exit_code == 0, result.output
    assert "client [redacted]" in result.stdout
