"""``agentos sessions list`` filters must see past the display limit.

``sessions.list`` narrows only by project, so ``--agent``, ``--status``,
``--channel``, ``--since`` and ``--search`` all run client-side. A filter
applied over the default fetch keeps whichever of those rows happen to match
and says nothing about the rest — an empty table for sessions that exist.
``--search`` already widened the fetch; its four siblings did not.

The gateway round-trip is stubbed at ``run_gateway_sync``, like
``test_sessions_rename_cmd``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from typer.testing import CliRunner

from agentos.cli import sessions_cmd

runner = CliRunner()

# 100 rows. The matching ones sit at the far end, outside any narrow fetch.
_ROWS: list[dict[str, Any]] = [
    {
        "key": f"agent:main:cli:{index:03d}",
        "display_name": f"session-{index}",
        "agent_id": "ops" if index >= 95 else "main",
        "status": "failed" if index >= 95 else "done",
        "channel": "slack" if index >= 95 else "cli",
        "updated_at": 1_700_000_000 + index,
        "model": "gpt-x",
        "message_count": 1,
    }
    for index in range(100)
]


class _FakeClient:
    def __init__(self) -> None:
        self.list_limits: list[int] = []

    async def list_sessions(self, limit: int = 50) -> dict[str, Any]:
        self.list_limits.append(limit)
        # The gateway returns the newest `limit` rows; anything past the
        # window is simply not sent.
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
    payload = json.loads(result.stdout)
    return list(payload["sessions"])


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--agent", "ops"),
        ("--status", "failed"),
        ("--channel", "slack"),
    ],
)
def test_a_filter_widens_the_fetch_past_the_display_limit(
    client: _FakeClient, flag: str, value: str
) -> None:
    """The five matching rows sit outside a 20-row fetch, but they exist."""
    rows = _listed(flag, value, "-n", "20")

    assert len(rows) == 5
    assert client.list_limits[-1] >= len(_ROWS)


def test_since_widens_the_fetch_past_the_display_limit(client: _FakeClient) -> None:
    rows = _listed("--since", "1700000095", "-n", "20")

    assert len(rows) == 5
    assert client.list_limits[-1] >= len(_ROWS)


def test_an_unfiltered_list_still_honours_the_limit_verbatim(client: _FakeClient) -> None:
    """No filter, no widening: the caller's --limit is the fetch."""
    rows = _listed("-n", "7")

    assert client.list_limits == [7]
    assert len(rows) == 7


def test_the_display_limit_still_bounds_a_widened_fetch(client: _FakeClient) -> None:
    """Widening changes what the filter sees, not how much is shown."""
    rows = _listed("--status", "done", "-n", "3")

    assert len(rows) == 3
    assert client.list_limits[-1] > 3
