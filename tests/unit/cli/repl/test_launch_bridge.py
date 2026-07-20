from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import typer


class FakeConsole:
    def __init__(self, *, is_terminal: bool = True) -> None:
        self.is_terminal = is_terminal
        self.clears = 0
        self.prints: list[Any] = []

    def clear(self) -> None:
        self.clears += 1

    def print(self, payload: Any) -> None:
        self.prints.append(payload)


def test_launch_bridge_prepares_terminal_and_quiets_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentos.cli.repl import launch_bridge

    calls: list[str] = []

    class FakeStdin:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(
        launch_bridge,
        "quiet_logs_for_interactive_chat",
        lambda: calls.append("quiet"),
    )

    console = FakeConsole(is_terminal=True)

    launch_bridge.prepare_interactive_chat(
        input_stream=FakeStdin(),
        output_console=console,
    )

    assert calls == ["quiet"]
    assert console.clears == 1


def test_launch_bridge_rejects_non_interactive_input() -> None:
    from agentos.cli.repl import launch_bridge

    class FakeStdin:
        def isatty(self) -> bool:
            return False

    with pytest.raises(typer.Exit) as exc_info:
        launch_bridge.prepare_interactive_chat(
            input_stream=FakeStdin(),
            output_console=FakeConsole(is_terminal=True),
        )

    assert exc_info.value.exit_code == 2


def test_launch_bridge_prints_standalone_banner_and_runs_standalone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentos.cli.repl import launch_bridge

    # Pin native scrollback: full-screen instead buffers the banner into the
    # transcript pane (covered separately) rather than printing it here.
    monkeypatch.setenv("AGENTOS_CHAT_FULLSCREEN", "0")

    calls: list[dict[str, Any]] = []
    console = FakeConsole(is_terminal=True)

    async def fake_standalone(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(launch_bridge, "prepare_interactive_chat", lambda **_kwargs: None)

    launch_bridge.launch_chat(
        model="openai/test",
        session_id="agent:main:test",
        standalone=True,
        workspace="repo",
        workspace_strict=True,
        timeout=7.25,
        standalone_runner=fake_standalone,
        gateway_runner=None,
        output_console=console,
    )

    from rich.console import Console

    # The standalone path now renders the branded startup screen via a single
    # console.print of a Rich renderable group.
    assert len(console.prints) == 1
    capture = Console(width=120)
    with capture.capture() as captured:
        capture.print(console.prints[0])
    text = captured.get()
    assert "AgentOS" in text
    assert "openai/test" in text  # the supplied model is shown
    assert "Session: agent:main:test" in text
    assert calls == [
        {
            "model": "openai/test",
            "session_id": "agent:main:test",
            "workspace": "repo",
            "workspace_strict": True,
            "timeout": 7.25,
        }
    ]


def test_launch_bridge_buffers_standalone_banner_into_pane_in_fullscreen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In full-screen the standalone banner is captured into the pane queue
    (so the alternate screen buffer does not wipe it) rather than printed."""
    from rich.console import Console

    from agentos.cli.repl import launch_bridge
    from agentos.cli.tui.terminal import prompt as prompt_module

    monkeypatch.setenv("AGENTOS_CHAT_FULLSCREEN", "1")
    prompt_module._pending_pane_output.clear()

    async def fake_standalone(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(launch_bridge, "prepare_interactive_chat", lambda **_kwargs: None)

    # A real Rich Console so the launch path can capture() the banner.
    console = Console(width=120, force_terminal=True)

    launch_bridge.launch_chat(
        model="openai/test",
        session_id="agent:main:test",
        standalone=True,
        workspace="",
        workspace_strict=None,
        timeout=None,
        standalone_runner=fake_standalone,
        gateway_runner=None,
        output_console=console,
    )

    queued = "".join(prompt_module._pending_pane_output)
    assert "AgentOS" in queued
    assert "Session: agent:main:test" in queued
    prompt_module._pending_pane_output.clear()


def test_launch_bridge_warns_gateway_workspace_options_without_forwarding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentos.cli.repl import launch_bridge

    calls: list[dict[str, Any]] = []
    console = FakeConsole(is_terminal=True)

    async def fake_gateway(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(launch_bridge, "prepare_interactive_chat", lambda **_kwargs: None)

    launch_bridge.launch_chat(
        model="",
        session_id="",
        standalone=False,
        workspace="repo",
        workspace_strict=True,
        timeout=None,
        standalone_runner=None,
        gateway_runner=fake_gateway,
        output_console=console,
    )

    assert calls == [{"model": None, "session_id": None}]
    assert len(console.prints) == 1
    assert "--workspace only affects --standalone chat" in str(console.prints[0])


def test_launch_chat_command_uses_typed_overrides() -> None:
    from agentos.cli.chat.launch import (
        ChatCommandLaunchOverrides,
        ChatCommandRequest,
    )
    from agentos.cli.repl import launch_bridge

    calls: list[dict[str, Any]] = []

    async def fake_standalone(**kwargs: Any) -> None:
        return None

    async def fake_gateway(**kwargs: Any) -> None:
        return None

    def fake_launch_chat(**kwargs: Any) -> None:
        calls.append(kwargs)

    launch_bridge.launch_chat_command(
        ChatCommandRequest(
            model="openai/test",
            session_id="agent:main:test",
            standalone=True,
            workspace="repo",
            workspace_strict=True,
            timeout=7.25,
        ),
        overrides=ChatCommandLaunchOverrides(
            launch_chat=fake_launch_chat,
            standalone_runner=fake_standalone,
            gateway_runner=fake_gateway,
        ),
    )

    assert calls == [
        {
            "model": "openai/test",
            "session_id": "agent:main:test",
            "standalone": True,
            "workspace": "repo",
            "workspace_strict": True,
            "timeout": 7.25,
            "standalone_runner": fake_standalone,
            "gateway_runner": fake_gateway,
        }
    ]


def test_launch_chat_command_keeps_legacy_override_mapping() -> None:
    from agentos.cli.chat.launch import ChatCommandRequest
    from agentos.cli.repl import launch_bridge

    calls: list[dict[str, Any]] = []

    async def fake_standalone(**kwargs: Any) -> None:
        return None

    async def fake_gateway(**kwargs: Any) -> None:
        return None

    def fake_launch_chat(**kwargs: Any) -> None:
        calls.append(kwargs)

    launch_bridge.launch_chat_command(
        ChatCommandRequest(
            model="openai/test",
            session_id="agent:main:test",
            standalone=False,
            workspace="",
            workspace_strict=None,
            timeout=None,
        ),
        legacy_overrides={
            "_launch_bridge": SimpleNamespace(launch_chat=fake_launch_chat),
            "_standalone_repl": fake_standalone,
            "_gateway_chat": fake_gateway,
        },
    )

    assert calls == [
        {
            "model": "openai/test",
            "session_id": "agent:main:test",
            "standalone": False,
            "workspace": "",
            "workspace_strict": None,
            "timeout": None,
            "standalone_runner": fake_standalone,
            "gateway_runner": fake_gateway,
        }
    ]
