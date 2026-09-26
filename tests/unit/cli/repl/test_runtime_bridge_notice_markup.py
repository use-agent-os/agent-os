"""Regression tests: gateway runtime notices must render literally.

``_gateway_runtime_notifier``'s ``_emit`` interpolates ``notice.session_key``
(from ``--session``/a resumed session key) and ``notice.model`` (from
``--model``) straight into Rich markup with no escaping. Both are plain CLI
argument text, so a bracket in either one is parsed as a style tag instead of
being shown: a closing tag like ``[/]`` raises ``rich.errors.MarkupError``,
which crashes the chat session before the first prompt is even shown.
"""

from __future__ import annotations

import pytest
from rich.console import Console

from agentos.cli.chat.gateway_runtime import GatewayRuntimeNotice
from agentos.cli.tui.adapters.runtime_bridge import _gateway_runtime_notifier


def _emit(notice: GatewayRuntimeNotice) -> str:
    console = Console(record=True, width=200)
    notify = _gateway_runtime_notifier(console, lambda message: message)
    notify(notice)
    return console.export_text()


def test_model_notice_with_a_closing_tag_does_not_crash() -> None:
    text = _emit(GatewayRuntimeNotice(kind="model", model="gpt[/]4"))

    assert "Model: gpt[/]4" in text


def test_model_notice_without_brackets_still_renders() -> None:
    # Positive control: proves the render path is actually live.
    text = _emit(GatewayRuntimeNotice(kind="model", model="gpt-4"))

    assert "Model: gpt-4" in text


def test_created_session_notice_with_a_closing_tag_does_not_crash() -> None:
    text = _emit(
        GatewayRuntimeNotice(kind="created", session_key="agent:main:cli:test[/]x")
    )

    assert "Session: agent:main:cli:test[/]x" in text


def test_resumed_session_notice_with_a_closing_tag_does_not_crash() -> None:
    text = _emit(
        GatewayRuntimeNotice(kind="resumed", session_key="agent:main:cli:test[/]x")
    )

    assert "Resuming session: agent:main:cli:test[/]x" in text


@pytest.mark.parametrize(
    "notice",
    [
        GatewayRuntimeNotice(kind="model", model="gpt[/]4"),
        GatewayRuntimeNotice(kind="created", session_key="s[/]x"),
        GatewayRuntimeNotice(kind="resumed", session_key="s[/]x"),
    ],
)
def test_bracketed_notice_never_raises_markup_error(notice: GatewayRuntimeNotice) -> None:
    _emit(notice)  # must not raise rich.errors.MarkupError
