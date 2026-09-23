"""``console.print`` parses ``[...]`` as Rich markup; a bracketed session
title, save path, or cached-approval target used to be interpolated raw.

``/rename`` already escapes (see ``test_slash_rename.py``); this pins the
three sibling sites that did not: ``/new <title>``, ``/save <path>``, and
``/approvals`` (both the no-gateway local intent cache and the RPC
``approvals_snapshot`` branch).
"""

from __future__ import annotations

from typing import Any

import pytest

from agentos.cli.chat.session_state import ChatSessionState
from agentos.cli.gateway_client import GatewayClient
from agentos.cli.tui.adapters.slash_gateway import (
    GatewaySlashContext,
    handle_gateway_slash_command,
)


class _Client(GatewayClient):
    """Only the surfaces these tests touch; anything else is a test bug.

    Subclasses the real ``GatewayClient`` — several handlers in
    ``slash_gateway.py`` assert ``isinstance(client, GatewayClient)`` before
    dispatching, and ``GatewayClient.__init__`` only sets local attributes
    (no network I/O).
    """

    def __init__(self, *, approvals_snapshot: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.created: list[tuple[str | None, str | None]] = []
        self._snapshot = approvals_snapshot

    async def create_session(self, *, model: str | None, display_name: str | None) -> str:
        self.created.append((model, display_name))
        return "agent:main:new-session"

    async def resolve_session(self, key: str) -> dict[str, Any]:
        raise RuntimeError("no gateway in this test")

    async def approvals_snapshot(self) -> dict[str, Any]:
        assert self._snapshot is not None
        return self._snapshot

    def __getattr__(self, item: str) -> Any:  # pragma: no cover - guard rail
        raise AssertionError(f"{item} is not used by these tests")


def _context(client: _Client) -> GatewaySlashContext:
    state = ChatSessionState(session_key="agent:main:cli:test", model="openai/test")
    return GatewaySlashContext(state=state, client=client, elevated_state={})


@pytest.mark.asyncio
async def test_new_session_title_with_a_closing_tag_does_not_crash(capsys) -> None:
    client = _Client()
    context = _context(client)

    handled = await handle_gateway_slash_command("/new release[/]", context)

    assert handled is True
    assert context.state.display_name == "release[/]"
    out = capsys.readouterr().out
    assert "release[/]" in out


@pytest.mark.asyncio
async def test_new_session_title_keeps_bracketed_text_literal(capsys) -> None:
    client = _Client()
    context = _context(client)

    handled = await handle_gateway_slash_command("/new [bold]evil[/bold] title", context)

    assert handled is True
    out = capsys.readouterr().out
    assert "[bold]evil[/bold] title" in out


@pytest.mark.asyncio
async def test_save_transcript_path_with_a_closing_tag_does_not_crash(tmp_path, capsys) -> None:
    client = _Client()
    context = _context(client)

    async def _history(_key: str, limit: int = 1000) -> dict[str, Any]:
        return {"messages": []}

    client.session_history = _history  # type: ignore[method-assign]
    # A literal "/" can't sit inside one filename component, but the path
    # separator between "notes[" and "draft].md" reproduces the same
    # "[/draft]"-shaped closing tag once the two segments are joined.
    save_dir = tmp_path / "notes["
    save_dir.mkdir()
    target = save_dir / "draft].md"

    handled = await handle_gateway_slash_command(f"/save {target}", context)

    assert handled is True
    assert target.exists()
    # A long path can soft-wrap across lines in a narrow test terminal; that
    # is unrelated to this bug, so compare with wrapping collapsed out.
    out = capsys.readouterr().out.replace("\n", "")
    assert str(target) in out


@pytest.mark.asyncio
async def test_approvals_local_cache_target_with_a_closing_tag_does_not_crash(capsys) -> None:
    from agentos.application.intent_cache import get_intent_cache, reset_intent_cache

    reset_intent_cache()
    cache = get_intent_cache()
    try:
        # Mirrors what /approvals reads directly (`cache._entries`), same as
        # the handler itself does — the bug is in the render, not the
        # shell-command extraction that normally populates this dict.
        cache._entries[("session-1", "delete", "backup[/].sql")] = (0.0, "always")

        state = ChatSessionState(session_key="agent:main:cli:test", model="openai/test")
        context = GatewaySlashContext(state=state, client=None, elevated_state={})
        handled = await handle_gateway_slash_command("/approvals", context)

        assert handled is True
        out = capsys.readouterr().out
        assert "backup[/].sql" in out
    finally:
        reset_intent_cache()


@pytest.mark.asyncio
async def test_approvals_rpc_snapshot_target_with_a_closing_tag_does_not_crash(capsys) -> None:
    client = _Client(
        approvals_snapshot={
            "mode": "prompt",
            "intent_cache_entries": [
                {"scope": "session", "kind": "rm", "target": "backup[/].sql"},
            ],
        }
    )
    context = _context(client)

    handled = await handle_gateway_slash_command("/approvals", context)

    assert handled is True
    out = capsys.readouterr().out
    assert "backup[/].sql" in out


@pytest.mark.asyncio
async def test_new_session_without_a_title_is_unaffected(capsys) -> None:
    """Positive control: the plain path still prints the session key."""
    client = _Client()
    context = _context(client)

    handled = await handle_gateway_slash_command("/new", context)

    assert handled is True
    assert context.state.display_name is None
    out = capsys.readouterr().out
    assert "agent:main:new-session" in out
