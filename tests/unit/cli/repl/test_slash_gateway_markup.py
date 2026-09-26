"""Gateway slash confirmations print user-typed values verbatim.

``/new``, ``/save`` and both ``/approvals`` branches interpolated a user-typed
title/path/target into Rich markup: a ``[/]``-shaped value raised
``MarkupError`` and killed the whole chat session, and a ``[redacted]``-shaped
value silently vanished from the confirmation. ``/rename`` already escaped its
display name; these are the remaining render sites in the adapter.
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from rich.console import Console

from agentos.cli.chat.session_state import ChatSessionState
from agentos.cli.tui.adapters import slash_gateway
from agentos.cli.tui.adapters.slash_gateway import (
    GatewaySlashContext,
    _handle_approvals_command,
    _save_gateway_transcript_command,
    handle_gateway_slash_command,
)


@pytest.fixture
def screen(monkeypatch: pytest.MonkeyPatch) -> StringIO:
    """Point the adapter's console at a plain, uncoloured buffer."""
    buf = StringIO()
    capture = Console(file=buf, highlight=False, force_terminal=False, no_color=True, width=200)
    monkeypatch.setattr(slash_gateway, "console", capture)
    return buf


class _GatewayBase:
    """Stand-in for ``GatewayClient`` so the runtime isinstance guards pass."""


@pytest.fixture
def gateway_base(monkeypatch: pytest.MonkeyPatch) -> type[_GatewayBase]:
    monkeypatch.setattr("agentos.cli.gateway_client.GatewayClient", _GatewayBase)
    return _GatewayBase


class _NewSessionClient:
    """Only the surface ``/new`` touches; anything else is a test bug."""

    def __init__(self) -> None:
        self.created: list[tuple[str | None, str | None]] = []

    async def create_session(
        self, model: str | None = None, display_name: str | None = None
    ) -> str:
        self.created.append((model, display_name))
        return "agent:main:cli:abc123"

    async def resolve_session(self, key: str) -> dict[str, Any]:
        return {"model": "openai/test", "displayName": None}

    def __getattr__(self, item: str) -> Any:  # pragma: no cover - guard rail
        raise AssertionError(f"{item} is not used by these tests")


def _context(client: Any) -> GatewaySlashContext:
    state = ChatSessionState(session_key="agent:main:cli:test", model="openai/test")
    return GatewaySlashContext(state=state, client=client, elevated_state={})


@pytest.mark.asyncio
async def test_new_prints_a_closing_tag_shaped_title_verbatim(screen: StringIO) -> None:
    context = _context(_NewSessionClient())

    handled = await handle_gateway_slash_command("/new release[/]", context)

    assert handled is True
    assert "(release[/])" in screen.getvalue()


@pytest.mark.asyncio
async def test_new_prints_a_bracketed_title_verbatim(screen: StringIO) -> None:
    context = _context(_NewSessionClient())

    await handle_gateway_slash_command("/new release [redacted]", context)

    assert "(release [redacted])" in screen.getvalue()


class _SaveClient(_GatewayBase):
    async def session_history(self, key: str, limit: int = 1000) -> dict[str, Any]:
        return {"messages": [{"role": "user", "text": "hi"}]}


@pytest.mark.asyncio
async def test_save_prints_a_closing_tag_shaped_path_verbatim(
    screen: StringIO, gateway_base: type[_GatewayBase], tmp_path: Path
) -> None:
    # A path whose string form contains "[/]": directory "notes[" + file "].md".
    # (A filename cannot contain "/", so this is the writable shape of the
    # `notes[/].md` path from the issue.)
    (tmp_path / "notes[").mkdir()
    target = tmp_path / "notes[" / "].md"
    state = ChatSessionState(session_key="agent:main:cli:test")

    await _save_gateway_transcript_command(f"/save {target}", state, _SaveClient())

    assert str(target) in screen.getvalue()
    assert target.read_text(encoding="utf-8") != ""


@pytest.mark.asyncio
async def test_save_prints_a_bracketed_path_verbatim(
    screen: StringIO, gateway_base: type[_GatewayBase], tmp_path: Path
) -> None:
    target = tmp_path / "notes [draft].md"
    state = ChatSessionState(session_key="agent:main:cli:test")

    await _save_gateway_transcript_command(f"/save {target}", state, _SaveClient())

    assert "notes [draft].md" in screen.getvalue()
    assert target.read_text(encoding="utf-8") != ""


def _fake_approvals_cache(target: str) -> type:
    class _Cache:
        _entries = {("agent:main:cli:test", "rm", target): (0.0, "always")}

    return _Cache


class _Queue:
    def get_settings(self) -> Any:
        return SimpleNamespace(mode="prompt")


@pytest.mark.asyncio
async def test_approvals_lists_a_closing_tag_shaped_target_verbatim(
    screen: StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agentos.gateway.approval_queue.get_approval_queue", _Queue)
    monkeypatch.setattr(
        "agentos.sandbox.intent_cache.get_intent_cache", _fake_approvals_cache("backup[/].sql")
    )

    await _handle_approvals_command("/approvals", client=None)

    assert "backup[/].sql" in screen.getvalue()


@pytest.mark.asyncio
async def test_approvals_lists_a_bracketed_target_verbatim(
    screen: StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agentos.gateway.approval_queue.get_approval_queue", _Queue)
    monkeypatch.setattr(
        "agentos.sandbox.intent_cache.get_intent_cache",
        _fake_approvals_cache("backup [draft].sql"),
    )

    await _handle_approvals_command("/approvals", client=None)

    assert "backup [draft].sql" in screen.getvalue()


class _ApprovalsClient(_GatewayBase):
    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self._entries = entries

    async def approvals_snapshot(self) -> dict[str, Any]:
        return {"mode": "prompt", "intent_cache_entries": self._entries}


@pytest.mark.asyncio
async def test_approvals_gateway_prints_a_closing_tag_shaped_target_verbatim(
    screen: StringIO, gateway_base: type[_GatewayBase]
) -> None:
    client = _ApprovalsClient([{"scope": "session", "kind": "rm", "target": "backup[/].sql"}])

    await _handle_approvals_command("/approvals", client=client)

    assert "backup[/].sql" in screen.getvalue()


@pytest.mark.asyncio
async def test_approvals_gateway_prints_a_bracketed_target_verbatim(
    screen: StringIO, gateway_base: type[_GatewayBase]
) -> None:
    client = _ApprovalsClient([{"scope": "session", "kind": "rm", "target": "backup [draft].sql"}])

    await _handle_approvals_command("/approvals", client=client)

    assert "backup [draft].sql" in screen.getvalue()
