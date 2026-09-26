"""Regression tests: background task-group status text must render literally.

``render_gateway_task_group_status`` / ``renderer_status`` fall back to
``console.print(f"[{style}]{message}[/]")`` whenever the active renderer has
no ``status``/``astatus`` method. ``message`` can embed an arbitrary
``error_message`` from a failed background subagent (a real Python exception
string, e.g. ``TypeError: unhashable type: 'list[str]'``), which Rich parses
as markup: a bracketed substring like ``[str]`` is consumed as a style tag
and silently dropped from the rendered text instead of being shown to the
user debugging the failure.
"""

from __future__ import annotations

from rich.console import Console

from agentos.cli.chat.turn_stream import (
    default_turn_stream_dependencies,
    render_gateway_task_group_status,
    renderer_status,
)


class _RendererWithoutStatus:
    """No ``status``/``astatus`` attribute, so the console.print fallback runs."""


def _rendered_text(event: dict[str, object]) -> str:
    console = Console(record=True, width=200)
    deps = default_turn_stream_dependencies(output_console=console)
    render_gateway_task_group_status(
        "background.task_group.failed", event, _RendererWithoutStatus(), deps=deps
    )
    return console.export_text()


def test_task_group_failed_message_with_bracketed_type_name_is_not_mangled() -> None:
    text = _rendered_text({"error_message": "unhashable type: 'list[str]'"})

    assert "unhashable type: 'list[str]'" in text


def test_task_group_failed_message_without_brackets_still_renders() -> None:
    # Positive control: proves the render path itself is live, not just that
    # escaping happens to be a no-op.
    text = _rendered_text({"error_message": "boom"})

    assert "background synthesis failed: boom" in text


async def test_renderer_status_fallback_does_not_mangle_bracketed_message() -> None:
    console = Console(record=True, width=200)
    deps = default_turn_stream_dependencies(output_console=console)

    await renderer_status(
        _RendererWithoutStatus(),
        "waiting on tool_result[list[int]]",
        style="dim",
        deps=deps,
    )

    assert "waiting on tool_result[list[int]]" in console.export_text()
