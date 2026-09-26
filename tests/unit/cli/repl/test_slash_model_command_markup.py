"""Regression tests: /model and /use render their argument literally.

Both ``handle_gateway_slash_command`` (gateway mode) and
``handle_standalone_slash_command`` (standalone mode) take the raw text a user
typed after ``/model`` or ``/use`` and print it straight back through Rich
markup, with no escaping. Typing a bracket — ``/model gpt[/]4`` — crashes the
whole chat session with ``rich.errors.MarkupError`` before the reply is even
shown, since these commands always reach the print (no provider validation
gates it).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agentos.cli.chat.session_state import ChatSessionState
from agentos.cli.tui.adapters.slash_gateway import (
    GatewaySlashContext,
    handle_gateway_slash_command,
)
from agentos.cli.tui.adapters.slash_standalone import (
    StandaloneSlashContext,
    handle_standalone_slash_command,
)


class _GatewayClient:
    def __init__(self) -> None:
        self.patched: list[tuple[str, str | None]] = []

    async def patch_session(self, key: str, *, model: str | None = None) -> dict[str, Any]:
        self.patched.append((key, model))
        return {}

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    def __getattr__(self, item: str) -> Any:  # pragma: no cover - guard rail
        raise AssertionError(f"{item} is not used by these tests")


def _gateway_context() -> GatewaySlashContext:
    state = ChatSessionState(session_key="agent:main:cli:test", model="openai/test")
    return GatewaySlashContext(state=state, client=_GatewayClient(), elevated_state={})


@pytest.mark.asyncio
async def test_gateway_model_command_with_a_closing_tag_does_not_crash(capsys) -> None:
    context = _gateway_context()

    handled = await handle_gateway_slash_command("/model gpt[/]4", context)

    assert handled is True
    assert context.state.model == "gpt[/]4"
    out = capsys.readouterr().out
    assert "gpt[/]4" in out


@pytest.mark.asyncio
async def test_gateway_model_command_without_brackets_still_renders(capsys) -> None:
    context = _gateway_context()

    handled = await handle_gateway_slash_command("/model gpt-4", context)

    assert handled is True
    out = capsys.readouterr().out
    assert "gpt-4" in out


@pytest.mark.asyncio
async def test_gateway_bare_model_query_with_a_closing_tag_does_not_crash(capsys) -> None:
    context = _gateway_context()
    context.state.model = "gpt[/]4"

    handled = await handle_gateway_slash_command("/model", context)

    assert handled is True
    out = capsys.readouterr().out
    assert "gpt[/]4" in out


@pytest.mark.asyncio
async def test_gateway_use_command_with_a_closing_tag_does_not_crash(capsys) -> None:
    context = _gateway_context()

    handled = await handle_gateway_slash_command("/use gpt[/]4", context)

    assert handled is True
    out = capsys.readouterr().out
    assert "gpt[/]4" in out


class _FakeHoldStore:
    def __init__(self) -> None:
        self.holds: list[tuple[str, object]] = []

    def set_hold(self, session_key: str, target: object, *, evidence: str, source: str) -> None:
        self.holds.append((session_key, target))

    def clear(self, session_key: str) -> object | None:
        return None


def _standalone_context() -> StandaloneSlashContext:
    state = ChatSessionState(session_key="agent:main:cli:test", model="openai/test")
    turn_runner = SimpleNamespace(
        router_control_hold_store=_FakeHoldStore(),
        router_control_config=SimpleNamespace(enabled=True),
    )
    return StandaloneSlashContext(
        state=state,
        session_key="agent:main:cli:test",
        model="openai/test",
        tool_ctx=object(),
        slash_services=SimpleNamespace(),
        turn_runner=turn_runner,
        build_tool_ctx=lambda _s: object(),
        replace_session=lambda **_kwargs: None,
    )


@pytest.mark.asyncio
async def test_standalone_model_command_with_a_closing_tag_does_not_crash(capsys) -> None:
    context = _standalone_context()

    handled = await handle_standalone_slash_command("/model gpt[/]4", context)

    assert handled is True
    assert context.state.model == "gpt[/]4"
    out = capsys.readouterr().out
    assert "gpt[/]4" in out


@pytest.mark.asyncio
async def test_standalone_use_command_with_a_closing_tag_does_not_crash(
    capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agentos.router_control as router_control

    monkeypatch.setattr(
        router_control,
        "resolve_router_control_model_target",
        lambda _cfg, model: SimpleNamespace(model=model),
    )
    context = _standalone_context()

    handled = await handle_standalone_slash_command("/use gpt[/]4", context)

    assert handled is True
    out = capsys.readouterr().out
    assert "gpt[/]4" in out
