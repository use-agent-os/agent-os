from __future__ import annotations

from typing import Any, cast

import pytest
from rich.console import Console
from rich.panel import Panel

from agentos.cli.repl import gateway_runtime, standalone_runtime
from agentos.cli.repl.session_state import ChatSessionState
from agentos.cli.repl.stream import TurnResult
from agentos.engine.commands import Surface


async def _fake_gateway_stream(*args: Any, **kwargs: Any) -> TurnResult:
    return TurnResult(text="gateway")


async def _fake_gateway_slash(*args: Any, **kwargs: Any) -> bool:
    return True


async def _fake_standalone_stream(*args: Any, **kwargs: Any) -> TurnResult:
    return TurnResult(text="standalone")


def _fake_error_panel(message: str, *, title: str = "Error") -> Panel:
    return Panel(message, title=title)


class _RecordingConsole:
    def __init__(self) -> None:
        self.printed: list[object] = []

    def print(self, value: object) -> None:
        self.printed.append(value)


def _render_to_text(renderable: object) -> str:
    capture = Console(width=120)
    with capture.capture() as captured:
        capture.print(renderable)
    return captured.get()


def test_gateway_runtime_notifier_maps_all_notice_kinds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentos.cli.repl import runtime_bridge

    # Pin the native-scrollback path so the notice mapping is asserted
    # directly (full-screen buffers startup notices — covered separately).
    monkeypatch.setenv("AGENTOS_CHAT_FULLSCREEN", "0")

    output_console = _RecordingConsole()
    notify = runtime_bridge._gateway_runtime_notifier(
        output_console,
        _fake_error_panel,
    )

    for notice in (
        gateway_runtime.GatewayRuntimeNotice(
            kind="created",
            session_key="agent:main:new",
        ),
        gateway_runtime.GatewayRuntimeNotice(
            kind="resumed",
            session_key="agent:main:old",
        ),
        gateway_runtime.GatewayRuntimeNotice(kind="resume_model_ignored"),
        gateway_runtime.GatewayRuntimeNotice(kind="model", model="openai/test"),
        gateway_runtime.GatewayRuntimeNotice(kind="welcome"),
        gateway_runtime.GatewayRuntimeNotice(kind="goodbye"),
        gateway_runtime.GatewayRuntimeNotice(kind="unknown_command"),
        gateway_runtime.GatewayRuntimeNotice(kind="error", message="boom"),
    ):
        notify(notice)

    assert output_console.printed[0] == (
        "[dim]Connected to gateway. Session: agent:main:new[/dim]"
    )
    assert output_console.printed[1] == (
        "[dim]Connected to gateway. Resuming session: agent:main:old[/dim]"
    )
    assert output_console.printed[2] == (
        "[yellow]Note: --model is honored only at session creation; ignored "
        "when resuming a session.[/yellow]"
    )
    assert output_console.printed[3] == "[dim]Model: openai/test[/dim]"
    # The welcome notice now renders the branded startup screen (a Rich Group).
    welcome = output_console.printed[4]
    assert not isinstance(welcome, str)
    welcome_text = _render_to_text(welcome)
    assert "AgentOS" in welcome_text
    assert output_console.printed[5] == "[yellow]Goodbye.[/yellow]"
    assert output_console.printed[6] == "[red]Unknown command.[/red] [dim]Use /help.[/dim]"
    assert isinstance(output_console.printed[7], Panel)


def test_gateway_notifier_buffers_startup_notices_in_fullscreen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In full-screen, startup notices are captured into the pane queue (so the
    app's alternate screen buffer does not wipe them) rather than printed."""
    from agentos.cli.repl import runtime_bridge
    from agentos.cli.tui.terminal import prompt as prompt_module

    monkeypatch.setenv("AGENTOS_CHAT_FULLSCREEN", "1")
    prompt_module._pending_pane_output.clear()

    # A real Rich Console so the notifier can capture() startup output.
    output_console = Console(width=120, force_terminal=True)
    notify = runtime_bridge._gateway_runtime_notifier(output_console, _fake_error_panel)

    notify(
        gateway_runtime.GatewayRuntimeNotice(kind="created", session_key="agent:main:x")
    )
    notify(gateway_runtime.GatewayRuntimeNotice(kind="welcome", session_key="agent:main:x"))

    queued = "".join(prompt_module._pending_pane_output)
    assert "Connected to gateway. Session: agent:main:x" in queued
    assert "AgentOS" in queued  # branded startup screen captured into the queue

    # During-session notices still print directly (the pane is live by then).
    with output_console.capture() as captured:
        notify(gateway_runtime.GatewayRuntimeNotice(kind="goodbye"))
    assert "Goodbye" in captured.get()

    prompt_module._pending_pane_output.clear()


@pytest.mark.asyncio
async def test_gateway_runtime_bridge_assembles_gateway_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentos.cli.repl import runtime_bridge

    captured: dict[str, Any] = {}
    output_console = _RecordingConsole()

    async def fake_run_gateway_chat(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(gateway_runtime, "run_gateway_chat", fake_run_gateway_chat)

    await runtime_bridge.run_gateway_chat(
        model="openai/test",
        session_id="agent:main:test",
        stream_response=_fake_gateway_stream,
        handle_slash_command=_fake_gateway_slash,
        output_console=output_console,
        error_panel_factory=_fake_error_panel,
    )

    deps = cast(gateway_runtime.GatewayRuntimeDependencies, captured["deps"])
    assert captured["model"] == "openai/test"
    assert captured["session_id"] == "agent:main:test"
    assert deps.stream_response is _fake_gateway_stream
    assert deps.handle_slash_command is _fake_gateway_slash
    assert callable(deps.run_input_loop)
    assert deps.get_tui_output is runtime_bridge.get_tui_output
    deps.notify(gateway_runtime.GatewayRuntimeNotice(kind="error", message="boom"))
    assert isinstance(output_console.printed[-1], Panel)


@pytest.mark.asyncio
async def test_gateway_runtime_bridge_resolves_default_repl_runner_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentos.cli.repl import runtime_bridge

    captured: dict[str, Any] = {}

    async def fake_run_gateway_chat(**kwargs: Any) -> None:
        captured.update(kwargs)

    runner_kwargs: dict[str, Any] = {}

    async def replacement_run_concurrent_repl(**kwargs: Any) -> None:
        runner_kwargs.update(kwargs)
        return None

    async def fake_dispatch(_value: str) -> bool:
        return True

    async def fake_abort() -> None:
        return None

    monkeypatch.setattr(gateway_runtime, "run_gateway_chat", fake_run_gateway_chat)
    monkeypatch.setattr(runtime_bridge, "run_concurrent_repl", replacement_run_concurrent_repl)

    await runtime_bridge.run_gateway_chat(
        model=None,
        session_id=None,
        stream_response=_fake_gateway_stream,
        handle_slash_command=_fake_gateway_slash,
    )

    deps = cast(gateway_runtime.GatewayRuntimeDependencies, captured["deps"])
    scope = {
        "session_key": "agent:main:test",
        "state": ChatSessionState(session_key="agent:main:test"),
        "model": None,
    }
    await deps.run_input_loop(
        scope=scope,
        dispatch=fake_dispatch,
        abort_active_turn=fake_abort,
    )
    assert runner_kwargs["surface"] is Surface.CLI_GATEWAY
    assert runner_kwargs["scope"] is scope
    assert runner_kwargs["dispatch"] is fake_dispatch
    assert runner_kwargs["abort_active_turn"] is fake_abort


@pytest.mark.asyncio
async def test_gateway_runtime_bridge_owns_default_turn_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentos.cli.repl import runtime_bridge

    captured: dict[str, Any] = {}

    async def fake_run_gateway_chat(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(gateway_runtime, "run_gateway_chat", fake_run_gateway_chat)

    await runtime_bridge.run_gateway_chat(model=None, session_id=None)

    deps = cast(gateway_runtime.GatewayRuntimeDependencies, captured["deps"])
    assert deps.stream_response is runtime_bridge.stream_response_gateway
    assert deps.handle_slash_command is runtime_bridge.handle_gateway_slash_command


@pytest.mark.asyncio
async def test_gateway_runtime_bridge_threads_stream_override_to_default_slash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentos.cli.repl import runtime_bridge

    captured: dict[str, Any] = {}
    slash_captured: dict[str, Any] = {}
    output_console = Console(file=None, force_terminal=False)

    async def fake_run_gateway_chat(**kwargs: Any) -> None:
        captured.update(kwargs)

    async def fake_handle_gateway_slash_command(*args: Any, **kwargs: Any) -> bool:
        slash_captured.update({"args": args, **kwargs})
        return True

    monkeypatch.setattr(gateway_runtime, "run_gateway_chat", fake_run_gateway_chat)
    monkeypatch.setattr(
        runtime_bridge._slash_bridge,
        "handle_gateway_slash_command",
        fake_handle_gateway_slash_command,
    )

    await runtime_bridge.run_gateway_chat(
        model=None,
        session_id=None,
        stream_response=_fake_gateway_stream,
        output_console=output_console,
        error_panel_factory=_fake_error_panel,
    )

    deps = cast(gateway_runtime.GatewayRuntimeDependencies, captured["deps"])
    handled = await deps.handle_slash_command(
        "/path report.md summarize",
        ChatSessionState(session_key="agent:main:test"),
        cast(gateway_runtime.GatewayClientLike, object()),
        {"mode": None},
        tui_output=None,
    )

    assert handled is True
    assert slash_captured["stream_response"] is _fake_gateway_stream
    assert slash_captured["output_console"] is output_console
    assert slash_captured["error_panel_factory"] is _fake_error_panel


@pytest.mark.asyncio
async def test_standalone_runtime_bridge_assembles_standalone_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentos.cli.repl import runtime_bridge

    captured: dict[str, Any] = {}
    output_console = Console(file=None, force_terminal=False)

    async def fake_run_standalone_chat(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(standalone_runtime, "run_standalone_chat", fake_run_standalone_chat)

    await runtime_bridge.run_standalone_chat(
        model="openai/test",
        session_id="agent:main:test",
        workspace="repo",
        workspace_strict=True,
        timeout=7.25,
        stream_response=_fake_standalone_stream,
        image_command_handler=_fake_standalone_stream,
        output_console=output_console,
        error_panel_factory=_fake_error_panel,
    )

    deps = cast(standalone_runtime.StandaloneRuntimeDependencies, captured["deps"])
    assert captured["model"] == "openai/test"
    assert captured["session_id"] == "agent:main:test"
    assert captured["workspace"] == "repo"
    assert captured["workspace_strict"] is True
    assert captured["timeout"] == 7.25
    assert deps.stream_response is _fake_standalone_stream
    assert deps.image_command_handler is _fake_standalone_stream
    assert deps.run_concurrent_repl is runtime_bridge.run_concurrent_repl
    assert deps.slash_services_factory is runtime_bridge.standalone_slash_services_from_runtime
    assert deps.get_tui_output is runtime_bridge.get_tui_output
    assert deps.output_console is output_console


@pytest.mark.asyncio
async def test_standalone_runtime_bridge_resolves_default_repl_runner_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentos.cli.repl import runtime_bridge

    captured: dict[str, Any] = {}

    async def fake_run_standalone_chat(**kwargs: Any) -> None:
        captured.update(kwargs)

    async def replacement_run_concurrent_repl(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(standalone_runtime, "run_standalone_chat", fake_run_standalone_chat)
    monkeypatch.setattr(runtime_bridge, "run_concurrent_repl", replacement_run_concurrent_repl)

    await runtime_bridge.run_standalone_chat(
        model=None,
        session_id=None,
        stream_response=_fake_standalone_stream,
        image_command_handler=_fake_standalone_stream,
    )

    deps = cast(standalone_runtime.StandaloneRuntimeDependencies, captured["deps"])
    assert deps.run_concurrent_repl is replacement_run_concurrent_repl


@pytest.mark.asyncio
async def test_standalone_runtime_bridge_owns_default_turn_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentos.cli.repl import runtime_bridge

    captured: dict[str, Any] = {}

    async def fake_run_standalone_chat(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(standalone_runtime, "run_standalone_chat", fake_run_standalone_chat)

    await runtime_bridge.run_standalone_chat(model=None, session_id=None)

    deps = cast(standalone_runtime.StandaloneRuntimeDependencies, captured["deps"])
    assert deps.stream_response is runtime_bridge.stream_response_turnrunner
    assert deps.image_command_handler is runtime_bridge.handle_image_command_turnrunner
