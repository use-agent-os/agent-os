"""``agentos sessions pin`` / ``unpin``.

The gateway round-trip is stubbed at ``run_gateway_sync`` — these cover the
CLI surface; ``tests/test_gateway/test_rpc_sessions_pin.py`` owns the RPC.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from typer.testing import CliRunner

from agentos.cli import sessions_cmd

runner = CliRunner()


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []
        self.changed = True

    async def pin_session(self, key: str, pinned: bool = True) -> dict[str, Any]:
        self.calls.append((key, pinned))
        return {"key": "agent:main:cli:aaa", "pinned": pinned, "changed": self.changed}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    fake = _FakeClient()

    def _run(action, **kwargs):
        return asyncio.run(action(fake))

    monkeypatch.setattr(sessions_cmd, "run_gateway_sync", _run)
    return fake


def test_pin_sends_pinned_true_and_names_the_resolved_key(client: _FakeClient) -> None:
    result = runner.invoke(sessions_cmd.app, ["pin", "api-refactor"])

    assert result.exit_code == 0
    assert client.calls == [("api-refactor", True)]
    assert "Pinned session 'agent:main:cli:aaa'" in result.stdout


def test_unpin_sends_pinned_false(client: _FakeClient) -> None:
    result = runner.invoke(sessions_cmd.app, ["unpin", "aaa"])

    assert result.exit_code == 0
    assert client.calls == [("aaa", False)]
    assert "Unpinned session" in result.stdout


def test_a_repeat_says_so_instead_of_claiming_a_change(client: _FakeClient) -> None:
    client.changed = False

    pinned = runner.invoke(sessions_cmd.app, ["pin", "aaa"])
    unpinned = runner.invoke(sessions_cmd.app, ["unpin", "aaa"])

    assert "already pinned" in pinned.stdout
    assert "not pinned" in unpinned.stdout


def test_json_output_is_the_payload(client: _FakeClient) -> None:
    result = runner.invoke(sessions_cmd.app, ["pin", "aaa", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "key": "agent:main:cli:aaa",
        "pinned": True,
        "changed": True,
    }
