from __future__ import annotations

from rich.console import Console

from agentos.cli.startup_screen import (
    StartupData,
    gather_startup_data,
    render_startup_screen,
    render_wordmark,
)


def _render(width: int, **kwargs: str) -> str:
    console = Console(width=width)
    with console.capture() as captured:
        render_startup_screen(console, **kwargs)
    return captured.get()


def test_render_startup_screen_wide_contains_agentos() -> None:
    text = _render(120, session_key="agent:main:demo", model="x/y-model")
    assert "AGENTOS" not in text  # block art is not plain "AGENTOS" text
    assert "AgentOS Agent" in text
    assert "Available Tools" in text
    assert "Available Skills" in text
    assert "/help for commands" in text
    assert "Session: agent:main:demo" in text
    assert "x/y-model" in text


def test_render_startup_screen_compact_does_not_crash() -> None:
    text = _render(60, session_key="agent:main:demo")
    assert "AgentOS Agent" in text
    assert "Available Tools" not in text  # compact head omits the catalogue
    assert "/help for commands" in text


def test_wordmark_rows_are_equal_length() -> None:
    art = render_wordmark()
    lines = art.plain.split("\n")
    assert len(lines) == 6
    widths = {len(line) for line in lines}
    assert len(widths) == 1  # every art row is the same length


def test_gather_startup_data_is_defensive_and_uses_overrides() -> None:
    data = gather_startup_data(session_key="s", model="m", workdir="w")
    assert isinstance(data, StartupData)
    assert data.session_key == "s"
    assert data.model == "m"
    assert data.workdir == "w"
    # Counts are non-negative and lists are well-formed even if registries fail.
    assert data.tool_count >= 0
    assert data.skill_count >= 0
    assert all(isinstance(name, str) for name, _ in data.tool_groups)


def test_gather_startup_data_falls_back_to_placeholders() -> None:
    data = gather_startup_data()
    # Session has no discovery source, so it must be the placeholder.
    assert data.session_key == "--"
    # Model and workdir resolve from config/paths; never empty.
    assert data.model
    assert data.workdir
