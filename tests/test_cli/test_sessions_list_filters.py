"""Issue #1913: every ``sessions list`` filter runs client-side, so the fetch
has to be wide enough for the filter to have something to match against.

``--search`` already widened; ``--agent``, ``--status``, ``--channel`` and
``--since`` did not, so they only ever filtered the first ``--limit`` rows and
reported an empty table for sessions that exist. The gateway round-trip is
stubbed at ``run_gateway_sync``, the way the sibling sessions tests do it, so
the CLI surface is exercised as-is.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from typer.testing import CliRunner

from agentos.cli import sessions_cmd

runner = CliRunner()

#: 100 sessions. The five worth finding are the *oldest*, so they fall outside
#: any window narrower than the whole list -- the shape of every real case in
#: the issue: the failed run from yesterday, one channel, an occasional agent.
_ROWS: list[dict[str, Any]] = [
    {
        "key": f"agent:main:cli:{i:03d}",
        "display_name": f"session-{i:03d}",
        "agent_id": "ops" if i >= 95 else "main",
        "status": "error" if i >= 95 else "done",
        "channel": "slack" if i >= 95 else "cli",
        "model": "ops-model" if i >= 95 else "m",
        "message_count": 1,
        "updated_at": "2026-09-13T00:00:00Z" if i >= 95 else "2020-01-01T00:00:00Z",
    }
    for i in range(100)
]


class _FakeClient:
    """Serves the newest ``limit`` rows, as a paging gateway does."""

    def __init__(self) -> None:
        self.list_limits: list[int] = []

    async def list_sessions(self, limit: int = 50) -> dict[str, Any]:
        self.list_limits.append(limit)
        return {"sessions": _ROWS[:limit], "count": min(limit, len(_ROWS))}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    fake = _FakeClient()
    monkeypatch.setattr(
        sessions_cmd, "run_gateway_sync", lambda action, **_kw: asyncio.run(action(fake))
    )
    return fake


def _listed(*args: str) -> list[dict[str, Any]]:
    result = runner.invoke(sessions_cmd.app, ["list", *args, "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)["sessions"]


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--agent", "ops"),
        ("--status", "error"),
        ("--channel", "slack"),
        ("--since", "2026-09-01"),
        ("--search", "ops-model"),
    ],
)
def test_every_filter_looks_past_the_display_limit(
    client: _FakeClient, flag: str, value: str
) -> None:
    """Each of these reported 0 of 5 before the fix -- except --search, which
    is the control: it already widened, and that contrast is what made the
    others a bug rather than a design limit."""
    rows = _listed(flag, value, "--limit", "20")

    assert client.list_limits[-1] == sessions_cmd._FILTER_FETCH_LIMIT
    assert len(rows) == 5
    assert {row["key"] for row in rows} == {f"agent:main:cli:{i:03d}" for i in range(95, 100)}


def test_an_unfiltered_list_fetches_exactly_the_limit(client: _FakeClient) -> None:
    """Without a filter the page *is* the answer, so nothing is widened."""
    rows = _listed("--limit", "20")

    assert client.list_limits[-1] == 20
    assert len(rows) == 20


def test_limit_still_bounds_what_is_printed(client: _FakeClient) -> None:
    """Widening changes what the filter sees, never how much is shown."""
    rows = _listed("--status", "done", "--limit", "3")

    assert client.list_limits[-1] == sessions_cmd._FILTER_FETCH_LIMIT
    assert len(rows) == 3


def test_a_limit_larger_than_the_widened_fetch_is_honoured(client: _FakeClient) -> None:
    """The widening is a floor, not a ceiling -- a caller who pinned a bigger
    --limit keeps it."""
    _listed("--agent", "main", "--limit", str(sessions_cmd._FILTER_FETCH_LIMIT + 250))

    assert client.list_limits[-1] == sessions_cmd._FILTER_FETCH_LIMIT + 250


def test_combined_filters_still_widen(client: _FakeClient) -> None:
    rows = _listed("--agent", "ops", "--channel", "slack", "--limit", "20")

    assert client.list_limits[-1] == sessions_cmd._FILTER_FETCH_LIMIT
    assert len(rows) == 5


def test_filters_that_match_nothing_report_an_honest_empty(client: _FakeClient) -> None:
    """The empty table now means "no such sessions", which is the whole point:
    it used to be indistinguishable from "not in the rows I fetched"."""
    assert _listed("--agent", "nobody", "--limit", "20") == []
    assert client.list_limits[-1] == sessions_cmd._FILTER_FETCH_LIMIT


@pytest.mark.parametrize("args", [("--search", "   "), ("--agent", ""), ("--since", "")])
def test_an_empty_filter_value_is_not_a_filter(client: _FakeClient, args: tuple[str, str]) -> None:
    """An empty value filters nothing downstream, so it must not widen either."""
    _listed(*args, "--limit", "20")

    assert client.list_limits[-1] == 20
