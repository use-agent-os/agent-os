"""Issue #248: ``agentos sessions rename`` plus name-aware ``sessions list``.

The gateway round-trip is stubbed at ``run_gateway_sync`` — these cover the
CLI surface (argument shapes, rendered output, filtering), not the RPC, which
``tests/test_gateway/test_rpc_sessions_rename.py`` owns.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from typer.testing import CliRunner

from agentos.cli import sessions_cmd

runner = CliRunner()


class _FakeClient:
    def __init__(self) -> None:
        self.rename_calls: list[tuple[str, str | None]] = []
        self.list_limits: list[int] = []

    async def rename_session(self, key: str, name: str | None) -> dict[str, Any]:
        self.rename_calls.append((key, name))
        return {"key": key, "name": name, "displayName": name, "previousName": None}

    async def list_sessions(self, limit: int = 50) -> dict[str, Any]:
        self.list_limits.append(limit)
        return {"sessions": _ROWS, "count": len(_ROWS)}


_ROWS: list[dict[str, Any]] = [
    {
        "key": "agent:main:cli:aaa",
        "display_name": "api-refactor",
        "status": "running",
        "model": "gpt-x",
        "message_count": 3,
    },
    {
        "key": "agent:main:cli:bbb",
        "derived_title": "bbb12345",
        "status": "done",
        "model": "gpt-y",
        "message_count": 1,
    },
]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    fake = _FakeClient()

    def _run(action, **kwargs):
        return asyncio.run(action(fake))

    monkeypatch.setattr(sessions_cmd, "run_gateway_sync", _run)
    return fake


def test_rename_sends_the_name_and_confirms(client: _FakeClient) -> None:
    result = runner.invoke(sessions_cmd.app, ["rename", "agent:main:cli:aaa", "api-refactor"])

    assert result.exit_code == 0
    assert client.rename_calls == [("agent:main:cli:aaa", "api-refactor")]
    assert "api-refactor" in result.stdout


def test_rename_with_no_name_clears_it(client: _FakeClient) -> None:
    result = runner.invoke(sessions_cmd.app, ["rename", "agent:main:cli:aaa"])

    assert result.exit_code == 0
    assert client.rename_calls == [("agent:main:cli:aaa", "")]
    assert "Cleared" in result.stdout


def test_rename_clear_flag_sends_none(client: _FakeClient) -> None:
    result = runner.invoke(sessions_cmd.app, ["rename", "agent:main:cli:aaa", "x", "--clear"])

    assert result.exit_code == 0
    assert client.rename_calls == [("agent:main:cli:aaa", None)]


def test_list_renders_the_name_column(client: _FakeClient) -> None:
    result = runner.invoke(sessions_cmd.app, ["list"])

    assert result.exit_code == 0
    assert "api-refactor" in result.stdout


def test_list_search_matches_the_custom_name(client: _FakeClient) -> None:
    result = runner.invoke(sessions_cmd.app, ["list", "--search", "REFACTOR", "--json"])

    assert result.exit_code == 0
    assert "agent:main:cli:aaa" in result.stdout
    assert "agent:main:cli:bbb" not in result.stdout


def test_a_name_containing_rich_markup_does_not_break_rendering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Names are user-typed and Rich parses markup in everything it prints.

    An unbalanced "[/]" used to raise MarkupError, so one poisoned session
    took down the whole listing — including the rename command needed to undo
    it.
    """
    poisoned = [{"key": "agent:main:cli:aaa", "display_name": "oops [/] [red]name"}]

    class _Poisoned(_FakeClient):
        async def list_sessions(self, limit: int = 50) -> dict[str, Any]:
            return {"sessions": poisoned, "count": 1}

    fake = _Poisoned()
    monkeypatch.setattr(
        sessions_cmd, "run_gateway_sync", lambda action, **_kw: asyncio.run(action(fake))
    )

    listed = runner.invoke(sessions_cmd.app, ["list"])
    assert listed.exit_code == 0, listed.output

    renamed = runner.invoke(sessions_cmd.app, ["rename", "agent:main:cli:aaa", "oops [/] name"])
    assert renamed.exit_code == 0, renamed.output


def test_search_widens_the_fetch_beyond_the_display_limit(client: _FakeClient) -> None:
    """Filtering is client-side, so a narrow fetch would hide older matches."""
    result = runner.invoke(sessions_cmd.app, ["list", "-n", "5", "--search", "refactor", "--json"])

    assert result.exit_code == 0
    assert client.list_limits == [sessions_cmd._FILTER_FETCH_LIMIT]

    # Without --search the caller's limit is honoured verbatim.
    runner.invoke(sessions_cmd.app, ["list", "-n", "5"])
    assert client.list_limits[-1] == 5


def test_row_name_prefers_display_name_then_derived_title() -> None:
    assert sessions_cmd._row_name(_ROWS[0]) == "api-refactor"
    assert sessions_cmd._row_name(_ROWS[1]) == "bbb12345"
    assert sessions_cmd._row_name({"key": "k"}) == ""


def test_filter_sessions_search_is_case_insensitive_and_spans_key() -> None:
    assert sessions_cmd._filter_sessions(
        _ROWS, agent=None, status=None, channel=None, since=None, search="Api-Ref"
    ) == [_ROWS[0]]
    assert sessions_cmd._filter_sessions(
        _ROWS, agent=None, status=None, channel=None, since=None, search="cli:bbb"
    ) == [_ROWS[1]]
    # An empty search is a no-op rather than a match-nothing filter.
    assert (
        sessions_cmd._filter_sessions(
            _ROWS, agent=None, status=None, channel=None, since=None, search="  "
        )
        == _ROWS
    )
