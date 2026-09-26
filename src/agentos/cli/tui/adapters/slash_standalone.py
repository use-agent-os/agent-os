"""Standalone slash-command adapter for the chat REPL backend.

This module owns TurnRunner-backed slash command dispatch. It stays independent
from prompt-toolkit and raw chat application objects: callers pass typed session
state, service handles, and optional stream callbacks.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import agentos.cli.tui.adapters.input_bridge as _input_bridge
from agentos.cli.chat.session_state import ChatSessionState
from agentos.cli.chat.turn import TurnResult
from agentos.cli.tui.adapters.commands import is_exit_command, render_help_table
from agentos.cli.tui.backend.contracts import TuiOutputHandle
from agentos.cli.tui.terminal.prompt import sync_session_chrome_from_state
from agentos.cli.ui import ACCENT, console, error_panel, markup_escape
from agentos.engine.commands import Surface
from agentos.session.compaction import (
    build_compaction_config_from_provider,
    call_compact_with_optional_config,
    resolve_compaction_provider,
)
from agentos.session.naming import normalize_session_name

STANDALONE_SLASH_HANDLER_WORDS = frozenset(
    {
        "/auto",
        "/c0",
        "/c1",
        "/c2",
        "/c3",
        "/clear",
        "/compact",
        "/cost",
        "/exit",
        "/help",
        "/image",
        "/model",
        "/new",
        "/path",
        "/quit",
        "/rename",
        "/reset",
        "/save",
        "/session",
        "/status",
        "/use",
    }
)


class StandaloneStreamResponse(Protocol):
    async def __call__(
        self,
        turn_runner: object,
        session_key: str,
        tool_context: object,
        message: str,
        *,
        model: str | None = None,
        services: object = None,
        timeout: float | None = None,
        tui_output: TuiOutputHandle | None = None,
    ) -> TurnResult: ...


class StandaloneImageCommandHandler(Protocol):
    async def __call__(
        self,
        turn_runner: object,
        session_key: str,
        tool_context: object,
        command: str,
        *,
        model: str | None = None,
        services: object = None,
        timeout: float | None = None,
        tui_output: TuiOutputHandle | None = None,
    ) -> TurnResult: ...


class StandaloneSessionReplacer(Protocol):
    def __call__(
        self,
        *,
        session_key: str,
        tool_ctx: object,
        state: ChatSessionState,
        model: str | None,
    ) -> Awaitable[None] | None: ...


class CompactWithResult(Protocol):
    def __call__(
        self,
        session_key: str,
        context_window_tokens: int,
        compaction_config: object | None,
    ) -> Awaitable[Any]: ...


class StandaloneCreateSession(Protocol):
    def __call__(
        self,
        session_key: str,
        *,
        agent_id: str = "main",
        display_name: str | None = None,
    ) -> Awaitable[Any]: ...


class StandaloneUpdateSession(Protocol):
    def __call__(self, session_key: str, **fields: Any) -> Awaitable[Any]: ...


class StandaloneReadTranscript(Protocol):
    def __call__(self, session_key: str) -> Awaitable[Any] | Any: ...


class StandaloneTruncateSession(Protocol):
    def __call__(self, session_key: str, *, max_messages: int = 0) -> Awaitable[None]: ...


class StandaloneCompactSession(Protocol):
    def __call__(
        self,
        session_key: str,
        context_window_tokens: int,
        config: object | None = None,
    ) -> Awaitable[str]: ...


@dataclass
class StandaloneSlashServices:
    create_session: StandaloneCreateSession | None = None
    update_session: StandaloneUpdateSession | None = None
    read_transcript: StandaloneReadTranscript | None = None
    truncate_session: StandaloneTruncateSession | None = None
    compact_session: StandaloneCompactSession | None = None
    compact_with_result: CompactWithResult | None = None
    config: object | None = None
    provider_selector: object | None = None


@dataclass
class StandaloneSlashContext:
    state: ChatSessionState
    session_key: str
    model: str | None
    tool_ctx: object
    slash_services: StandaloneSlashServices
    turn_runner: object
    build_tool_ctx: Callable[[str], object]
    replace_session: StandaloneSessionReplacer
    runtime_services: object = None
    timeout: float | None = None
    tui_output: TuiOutputHandle | None = None
    stream_response: StandaloneStreamResponse | None = None
    image_command_handler: StandaloneImageCommandHandler | None = None


def _slash_parts(cmd: str, name: str) -> list[str] | None:
    if cmd == name or cmd.startswith(f"{name} "):
        return cmd.split(maxsplit=1)
    return None


async def _maybe_await(value: Awaitable[Any] | Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def stream_response_turnrunner(
    turn_runner: object,
    session_key: str,
    tool_context: object,
    message: str,
    *,
    model: str | None = None,
    services: object = None,
    timeout: float | None = None,
    tui_output: TuiOutputHandle | None = None,
) -> TurnResult:
    del turn_runner, session_key, tool_context, message, model, services, timeout, tui_output
    raise RuntimeError("standalone streaming dependency was not configured")


async def handle_image_command_turnrunner(
    turn_runner: object,
    session_key: str,
    tool_context: object,
    command: str,
    *,
    model: str | None = None,
    services: object = None,
    timeout: float | None = None,
    tui_output: TuiOutputHandle | None = None,
) -> TurnResult:
    del turn_runner, session_key, tool_context, command, model, services, timeout, tui_output
    raise RuntimeError("standalone image dependency was not configured")


#: Retained as a module attribute: ``slash_bridge`` and the chat-command
#: export map address this adapter's helper by name.
_resolve_compaction_provider = resolve_compaction_provider


def _coerce_transcript_result(result: Any) -> list[Any] | None:
    if result is None:
        return []
    if isinstance(result, str | bytes) or not isinstance(result, Iterable):
        return None
    return list(result)


async def _read_standalone_transcript_handle(
    read_transcript: StandaloneReadTranscript | None,
    session_key: str,
) -> list[Any] | None:
    if read_transcript is None:
        return []
    try:
        result = read_transcript(session_key)
        if inspect.isawaitable(result):
            result = await result
    except KeyError:
        return []
    except Exception:  # noqa: BLE001
        return None
    return _coerce_transcript_result(result)


async def _read_standalone_transcript(
    session_manager: Any,
    session_key: str,
) -> list[Any] | None:
    """Read the durable transcript before a destructive standalone command."""
    if session_manager is None:
        return []
    for method_name in ("get_transcript", "read_transcript"):
        reader = getattr(session_manager, method_name, None)
        if not callable(reader):
            continue
        try:
            result = reader(session_key)
            if inspect.isawaitable(result):
                result = await result
        except KeyError:
            return []
        except Exception:  # noqa: BLE001
            return None
        return _coerce_transcript_result(result)
    return None


def _save_transcript_command(cmd: str, state: ChatSessionState) -> None:
    parts = cmd.split(maxsplit=1)
    if len(parts) > 1:
        target = Path(parts[1]).expanduser()
    else:
        suffix = state.session_key.replace(":", "-")
        target = Path(f"agentos-chat-{suffix}.md")
    target.write_text(state.transcript.to_markdown(), encoding="utf-8")
    console.print(f"[green]Saved transcript:[/green] {target}")


def _image_prompt_from_command(command: str) -> str:
    return _input_bridge.image_prompt_from_command(command)


def _path_prompt_and_attachments(command: str) -> tuple[str, list[dict[str, Any]]]:
    return _input_bridge.path_prompt_and_attachments(command)


async def _replace_with_new_session(
    context: StandaloneSlashContext,
    *,
    title: str | None = None,
) -> str:
    session_key = f"agent:main:standalone:{uuid4().hex[:8]}"
    create_session = context.slash_services.create_session
    if create_session is None:
        raise RuntimeError("standalone chat requires session manager")
    # Persist the title as the session ``display_name`` so it survives a
    # later ``/resume`` and can be surfaced in the toolbar / ``/status``.
    # ``SessionManager.create`` forwards ``**kwargs`` to the ``SessionNode``
    # constructor, which has a nullable ``display_name`` column (no
    # migration required). Fixes the pre-existing standalone bug where
    # ``/new <title>`` accepted the arg then dropped it.
    await create_session(
        session_key,
        agent_id="main",
        display_name=title,
    )
    state = ChatSessionState(
        session_key=session_key,
        model=context.model,
        display_name=title or None,
        router_hold_tier=None,
    )
    tool_ctx = context.build_tool_ctx(session_key)

    context.session_key = session_key
    context.tool_ctx = tool_ctx
    context.state = state
    await _maybe_await(
        context.replace_session(
            session_key=session_key,
            tool_ctx=tool_ctx,
            state=state,
            model=context.model,
        )
    )
    sync_session_chrome_from_state(state)
    label = f" ({markup_escape(title)})" if title else ""
    console.print(f"[green]Started new session{label}:[/green] {session_key}")
    return session_key


async def _compact_standalone_context(context: StandaloneSlashContext) -> None:
    slash_services = context.slash_services
    compact_session = slash_services.compact_session
    compact_with_result = slash_services.compact_with_result
    if compact_session is None and compact_with_result is None:
        console.print("[yellow]No session manager available.[/yellow]")
        return

    console.print(f"[{ACCENT}]compacting context...[/]")
    config = slash_services.config
    context_window = (
        getattr(config, "context_budget_tokens", 100_000) if config is not None else 100_000
    )
    compaction_config = build_compaction_config_from_provider(
        _resolve_compaction_provider(
            slash_services.provider_selector,
            context.model,
        ),
        model_override=context.model,
        compaction_config=getattr(config, "compaction", None),
    )
    try:
        if compact_with_result is not None:
            result = await compact_with_result(
                context.session_key,
                context_window,
                compaction_config,
            )
            summary = getattr(result, "summary", "") or ""
            token_stats = (
                f"{getattr(result, 'tokens_before', 0)} -> "
                f"{getattr(result, 'tokens_after', 0)} tokens, "
                f"{getattr(result, 'remaining_budget_tokens', 0)} remaining, "
                f"{getattr(result, 'summary_source', 'unknown')}"
            )
        else:
            if compact_session is None:
                console.print("[yellow]No session manager available.[/yellow]")
                return
            summary = await call_compact_with_optional_config(
                compact_session,
                context.session_key,
                context_window,
                compaction_config,
            )
            token_stats = f"summary {len(summary)} chars"
    except Exception as exc:  # noqa: BLE001 - keep chat command recoverable.
        console.print(f"[red]compact failed: {exc}[/red]")
        return

    if summary:
        console.print(f"[{ACCENT}]compacted[/] [dim]{token_stats}[/dim]")
    else:
        console.print(
            f"[{ACCENT}]compact skipped[/] "
            "[dim]already within context budget; no compact was applied[/dim]"
        )


async def _apply_router_hold_standalone(
    context: StandaloneSlashContext,
    *,
    tier: str | None,
    model: str | None = None,
) -> None:
    """Mutate the in-process Pilot Router hold store for the active session.

    ``tier=None`` means "clear" (restore automatic routing); a non-empty
    string pins the router to that tier. ``model`` pins a directly-named model
    instead — unlike the gateway path this cannot check the id against a
    catalog, because standalone mode has no model listing at all (``/models``
    says as much), so a bad id surfaces as a provider error on the next turn.
    Mirrors what the gateway's
    ``router.hold.set`` / ``router.hold.clear`` RPCs do, but touches the
    ``TurnRunner`` instance living in this process directly. The store
    and config come from ``turn_runner.router_control_hold_store`` /
    ``router_control_config`` (always present on a real TurnRunner; the
    config may be ``None`` or ``enabled=False`` when the operator hasn't
    configured a Pilot Router — we surface that as a readable message).
    """
    turn_runner = context.turn_runner
    store = getattr(turn_runner, "router_control_hold_store", None)
    cfg = getattr(turn_runner, "router_control_config", None)
    if store is None or cfg is None or not getattr(cfg, "enabled", False):
        console.print("[yellow]Pilot Router is disabled or unavailable.[/yellow]")
        return

    # Lazily import so the slash adapter never pays the router_control
    # import cost when no tier command is issued.
    from agentos.router_control import (
        HOLD_SOURCE_USER,
        RouterControlValidationError,
        resolve_router_control_model_target,
        resolve_router_control_target,
    )
    from agentos.session.keys import canonicalize_session_key

    session_key = canonicalize_session_key(context.session_key)

    if model:
        try:
            model_target = resolve_router_control_model_target(cfg, model)
        except RouterControlValidationError as exc:
            console.print(f"[yellow]{exc}[/yellow]")
            return
        store.set_hold(
            session_key,
            model_target,
            evidence=f"slash command /use {model}",
            source=HOLD_SOURCE_USER,
        )
        # The chip shows the model, not the tier hosting it — the tier is an
        # implementation detail of where the settings come from.
        context.state.router_hold_tier = model
        sync_session_chrome_from_state(context.state)
        console.print(f"[{ACCENT}]router pinned to model[/] {markup_escape(model)}")
        return

    if tier is None:
        cleared = store.clear(session_key)
        context.state.router_hold_tier = None
        sync_session_chrome_from_state(context.state)
        if cleared is not None:
            console.print(f"[{ACCENT}]router hold cleared (automatic routing)[/]")
        else:
            console.print(f"[{ACCENT}]router already on automatic routing[/]")
        return

    try:
        target = resolve_router_control_target(cfg, f"tier:{tier}")
    except RouterControlValidationError:
        console.print(
            f"[yellow]Tier '{tier}' is not configured on the Pilot Router.[/yellow]"
        )
        return
    # A slash pin is the user speaking, so it is sticky: it holds until /auto
    # clears it, unlike the self-expiring hold the model installs for itself.
    store.set_hold(
        session_key,
        target,
        evidence=f"slash command /{tier}",
        source=HOLD_SOURCE_USER,
    )
    context.state.router_hold_tier = tier
    sync_session_chrome_from_state(context.state)
    model_suffix = f" · {target.model}" if target.model else ""
    console.print(f"[{ACCENT}]router pinned to tier {tier}[/]{model_suffix}")


async def handle_standalone_slash_command(
    cmd: str,
    context: StandaloneSlashContext,
) -> bool:
    """Handle standalone-mode slash commands.

    Unknown slash commands are handled here so callers can keep the input loop
    free of command-specific fallback rendering.
    """

    state = context.state
    stream = context.stream_response or stream_response_turnrunner
    image_handler = context.image_command_handler or handle_image_command_turnrunner

    if is_exit_command(cmd, Surface.CLI_STANDALONE):
        console.print("[yellow]Goodbye.[/yellow]")
        return False

    if cmd == "/help":
        console.print(render_help_table(Surface.CLI_STANDALONE))
        return True

    if parts := _slash_parts(cmd, "/new"):
        # Standalone mode never reaches ``sessions.create``, so the title is
        # normalized here -- the same shape ``/rename`` below stores (#1618).
        title = normalize_session_name(parts[1]) if len(parts) > 1 else None
        await _replace_with_new_session(context, title=title)
        return True

    if parts := _slash_parts(cmd, "/rename"):
        name = normalize_session_name(parts[1] if len(parts) > 1 else "")
        update_session = context.slash_services.update_session
        if update_session is None:
            console.print("[yellow]/rename requires a session manager.[/yellow]")
            return True
        await _maybe_await(update_session(context.session_key, display_name=name))
        state.display_name = name
        sync_session_chrome_from_state(state)
        if name:
            console.print(f"[green]Renamed session to:[/green] {markup_escape(name)}")
        else:
            console.print("[green]Cleared the session name.[/green]")
        return True

    if cmd in {"/status", "/session"}:
        title_line = (
            f"[{ACCENT}]title[/] [dim]{markup_escape(state.display_name)}[/dim]\n"
            if state.display_name
            else ""
        )
        tier_line = (
            f"[{ACCENT}]router[/] [dim]tier:{state.router_hold_tier}[/dim]\n"
            if state.router_hold_tier
            else f"[{ACCENT}]router[/] [dim]auto[/dim]\n"
        )
        status_text = (
            f"{title_line}"
            f"[{ACCENT}]session[/] [dim]{state.session_key}[/dim]\n"
            f"[{ACCENT}]model[/] [dim]{state.model or 'default'}[/dim]\n"
            f"{tier_line}"
        )
        console.print(status_text.rstrip("\n"))
        return True

    if cmd == "/models":
        console.print("[yellow]/models requires gateway mode.[/yellow]")
        return True

    if parts := _slash_parts(cmd, "/model"):
        if len(parts) == 1:
            console.print(f"[dim]model={markup_escape(state.model or 'default')}[/dim]")
        else:
            new_model = parts[1].strip()
            context.model = new_model
            state.model = new_model
            console.print(f"[green]model:[/green] {markup_escape(new_model)}")
        return True

    if cmd == "/cost":
        console.print(state.usage.render())
        return True

    if parts := _slash_parts(cmd, "/use"):
        if len(parts) == 1:
            console.print("[yellow]Usage: /use <model-id>[/yellow]")
        else:
            await _apply_router_hold_standalone(context, tier=None, model=parts[1].strip())
        return True

    if cmd in {"/c0", "/c1", "/c2", "/c3"}:
        await _apply_router_hold_standalone(context, tier=cmd[1:])
        return True

    if cmd == "/auto":
        await _apply_router_hold_standalone(context, tier=None)
        return True

    if cmd in {"/clear", "/reset"}:
        truncate_session = context.slash_services.truncate_session
        if truncate_session is not None:
            await truncate_session(context.session_key, max_messages=0)
        state.transcript.clear()
        state.usage.reset()
        # Wipe the visible surface too: clearing only the in-memory transcript
        # leaves the reset conversation on screen, which reads as "nothing
        # happened". Done before the confirmation line so it survives.
        if context.tui_output is not None:
            await context.tui_output.clear_screen()
        console.print(f"[{ACCENT}]cleared[/] [dim]{state.session_key}[/dim]")
        return True

    if cmd == "/compact":
        await _compact_standalone_context(context)
        return True

    if _slash_parts(cmd, "/save"):
        _save_transcript_command(cmd, state)
        return True

    if parts := _slash_parts(cmd, "/image"):
        if len(parts) == 1 or not parts[1].strip():
            console.print("[red]Usage: /image <path> [prompt][/red]")
            return True
        result = await image_handler(
            context.turn_runner,
            context.session_key,
            context.tool_ctx,
            cmd,
            model=context.model,
            services=context.runtime_services,
            timeout=context.timeout,
            tui_output=context.tui_output,
        )
        state.transcript.add("user", _image_prompt_from_command(cmd))
        state.transcript.add("assistant", result.text)
        state.usage.apply(result.usage)
        return True

    if parts := _slash_parts(cmd, "/path"):
        if len(parts) == 1 or not parts[1].strip():
            console.print("[red]Usage: /path <path> [prompt][/red]")
            return True
        try:
            prompt, attachments = _path_prompt_and_attachments(cmd)
        except ValueError as exc:
            console.print(error_panel(str(exc)))
            return True
        if attachments:
            console.print(error_panel("/path must not create attachments."))
            return True
        result = await stream(
            context.turn_runner,
            context.session_key,
            context.tool_ctx,
            prompt,
            model=context.model,
            services=context.runtime_services,
            timeout=context.timeout,
            tui_output=context.tui_output,
        )
        state.transcript.add("user", prompt)
        state.transcript.add("assistant", result.text)
        state.usage.apply(result.usage)
        return True

    console.print("[red]Unknown command.[/red] [dim]Use /help.[/dim]")
    return True
