"""CLI subcommand: ``agentos replay --session <id> --turn <n>``.

Replays a recorded turn from the decision log. This is read-only — no tools
are re-executed.
"""

from __future__ import annotations

import typer

from agentos.cli.ui import console, markup_escape
from agentos.observability.replay import format_transcript, load_turn

replay_app = typer.Typer(
    name="replay",
    help="Replay a recorded turn from the decision log.",
    no_args_is_help=True,
)


@replay_app.callback(invoke_without_command=True)
def replay(
    session: str = typer.Option(..., "--session", "-s", help="Session key"),
    turn: str = typer.Option(..., "--turn", "-t", help="Turn ID"),
) -> None:
    """Print a human-readable transcript for ``(session, turn)``."""

    entry = load_turn(session, turn)
    if entry is None:
        console.print(
            f"[red]No entry found for session={markup_escape(session)} "
            f"turn={markup_escape(turn)}[/red]"
        )
        raise typer.Exit(code=1)
    # The transcript embeds recorded free text (session key, tool_choice,
    # fallback_reason, ...) that must render as literal characters, not be
    # parsed as Rich markup.
    console.print(format_transcript(entry), markup=False, emoji=False, soft_wrap=True)
