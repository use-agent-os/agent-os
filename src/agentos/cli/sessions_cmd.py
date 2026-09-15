"""Sessions command — list/show/rename/resume/delete/export sessions."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from rich.table import Table

from agentos.cli.chat.session_state import messages_to_markdown
from agentos.cli.gateway_rpc import default_gateway_token, default_gateway_url, run_gateway_sync
from agentos.cli.output import print_json
from agentos.cli.ui import ACCENT, ACCENT_HEADER, console, error_panel, markup_escape
from agentos.session.manager import _safe_archive_part

app = typer.Typer(help="Manage chat sessions.")

_CLIENT_UNAVAILABLE = object()
_ACTION_FAILED = object()

# Rows pulled before a client-side --search filter runs.
_SEARCH_FETCH_LIMIT = 500


def _resolved_key(payload: dict[str, Any], fallback: str) -> str:
    value = payload.get("session_key") or payload.get("key") or fallback
    return str(value)


#: A compact calendar date, ``20260101``. Read as epoch seconds it would be
#: 1970-08-23, which is not a date any session carries, so the digit count is
#: the whole disambiguation.
_COMPACT_DATE_LEN = 8

#: Below this an all-digit value is not a plausible timestamp: 1e8 seconds is
#: 1973. Anything shorter is a date or a year the user typed without
#: separators, and reading it as an epoch silently lands the filter in 1970 --
#: where it matches everything (Issue #2132).
_MIN_EPOCH_SECONDS = 100_000_000

#: Above this, seconds would be past the year 2286, so the value is milliseconds.
_MILLIS_THRESHOLD_SECONDS = 10_000_000_000

#: The upper end of what counts as a session timestamp, 2200-01-01. Bounded
#: explicitly so the answer does not depend on the platform:
#: ``datetime.fromtimestamp`` accepts up to year 9999 on Linux, while the
#: Windows CRT rejects anything past roughly year 3000, so an unbounded value
#: parsed cleanly on one and raised on the other.
_MAX_EPOCH_SECONDS = 7_258_118_400

_SINCE_HELP = (
    "--since must be an ISO date/datetime (2026-09-14, 2026-09-14T08:00:00Z), "
    "a compact date (20260914), or an epoch timestamp in seconds or milliseconds"
)


def _epoch_to_datetime(number: float) -> datetime:
    """Seconds or milliseconds since the epoch, as an aware datetime.

    ``fromtimestamp`` raises ``OSError``/``OverflowError`` -- not ``ValueError``
    -- for an out-of-range value, so both are normalised to ``ValueError`` for
    every caller to handle in one place.

    The range is also checked here rather than left to the platform. What
    ``fromtimestamp`` accepts differs by OS -- Linux goes to year 9999, the
    Windows CRT stops around year 3000 -- so the same ``--since`` would be
    rejected on one machine and silently accepted as a year-5138 filter on
    another.
    """
    if number > _MILLIS_THRESHOLD_SECONDS:
        number = number / 1000
    if number > _MAX_EPOCH_SECONDS:
        raise ValueError(f"{number} is beyond any plausible session timestamp")
    try:
        return datetime.fromtimestamp(number, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError(f"{number} is out of range for a timestamp") from exc


def _datetime_from_text(raw: str) -> datetime:
    """Parse one user- or gateway-supplied instant, or raise ``ValueError``.

    Kept free of ``typer`` so ``_row_datetime`` can call it while filtering rows
    and skip a bad value, rather than aborting the whole listing.
    """
    if raw.isdigit():
        if len(raw) == _COMPACT_DATE_LEN:
            return datetime.strptime(raw, "%Y%m%d").replace(tzinfo=UTC)
        number = float(int(raw))
        if number < _MIN_EPOCH_SECONDS:
            raise ValueError(f"{raw!r} is neither a date nor a plausible epoch timestamp")
        return _epoch_to_datetime(number)
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        return _datetime_from_text(raw)
    except ValueError as exc:
        raise typer.BadParameter(f"{_SINCE_HELP} (got {value!r})") from exc


def _row_datetime(row: dict[str, Any]) -> datetime | None:
    value = row.get("updated_at", row.get("updatedAt"))
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return _epoch_to_datetime(float(value))
        except ValueError:
            # A row with an unusable timestamp is skipped, never fatal: one bad
            # row must not take down the whole listing.
            return None
    if isinstance(value, str):
        try:
            return _datetime_from_text(value)
        except ValueError:
            return None
    return None


def _row_name(row: dict[str, Any]) -> str:
    """Best display name for a session row, falling back to the derived title."""
    for field in ("display_name", "displayName", "derived_title", "derivedTitle", "subject"):
        value = row.get(field)
        if value:
            return str(value)
    return ""


def _matches_search(row: dict[str, Any], needle: str) -> bool:
    """Case-insensitive substring match over a row's name and identity fields."""
    haystack = [
        _row_name(row),
        str(row.get("key") or ""),
        str(row.get("subject") or ""),
        str(row.get("model") or ""),
    ]
    return any(needle in value.lower() for value in haystack if value)


def _filter_sessions(
    rows: list[dict[str, Any]],
    *,
    agent: str | None,
    status: str | None,
    channel: str | None,
    since: datetime | None,
    search: str | None = None,
) -> list[dict[str, Any]]:
    needle = (search or "").strip().lower()
    filtered: list[dict[str, Any]] = []
    for row in rows:
        if needle and not _matches_search(row, needle):
            continue
        if agent and str(row.get("agent_id") or row.get("agentId") or "") != agent:
            continue
        if status and str(row.get("status") or "").lower() != status.lower():
            continue
        if channel:
            channel_values = {
                str(row.get("channel") or ""),
                str(row.get("last_channel") or ""),
                str(row.get("lastChannel") or ""),
                str(row.get("source_channel") or ""),
                str(row.get("sourceChannel") or ""),
            }
            if channel not in channel_values:
                continue
        if since:
            updated = _row_datetime(row)
            if updated is None or updated < since:
                continue
        filtered.append(row)
    return filtered


async def _with_client(action):
    from agentos.cli.gateway_client import GatewayClient, GatewayRPCError

    client = GatewayClient()
    try:
        await client.connect(default_gateway_url(), token=default_gateway_token())
        return await action(client)
    except SystemExit as exc:
        console.print(f"[dim]{exc}[/dim]")
        return _CLIENT_UNAVAILABLE
    except GatewayRPCError as exc:
        console.print(error_panel(str(exc)))
        return _ACTION_FAILED
    finally:
        await client.close()


@app.command("list")
def sessions_list(
    limit: int = typer.Option(50, "--limit", "-n", help="Maximum rows"),
    agent: str | None = typer.Option(None, "--agent", help="Filter by agent id"),
    status: str | None = typer.Option(None, "--status", help="Filter by session status"),
    channel: str | None = typer.Option(None, "--channel", help="Filter by channel/source"),
    since: str | None = typer.Option(None, "--since", help="ISO date/datetime or epoch timestamp"),
    search: str | None = typer.Option(
        None, "--search", "-q", help="Filter by session name, key, subject or model"
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """List recent sessions."""
    since_dt = _parse_since(since)
    # Filtering happens client-side, so a search over the default 50 most
    # recent rows would miss the older session the user named months ago —
    # the exact case renaming exists for. Widen the fetch when searching
    # unless the caller pinned a larger --limit themselves.
    fetch_limit = max(limit, _SEARCH_FETCH_LIMIT) if (search or "").strip() else limit

    async def _run(client):
        return await client.list_sessions(limit=fetch_limit)

    result = run_gateway_sync(_run, json_output=json_output)
    raw_rows = result.get("sessions", []) if isinstance(result, dict) else []
    rows = _filter_sessions(
        [row for row in raw_rows if isinstance(row, dict)],
        agent=agent,
        status=status,
        channel=channel,
        since=since_dt,
        search=search,
    )
    # --limit still bounds what the user sees; the widened fetch above only
    # widens what search looks at.
    if fetch_limit != limit:
        rows = rows[:limit]
    if json_output:
        payload = dict(result) if isinstance(result, dict) else {}
        payload["sessions"] = rows
        payload["count"] = len(rows)
        print_json(payload)
        return

    table = Table(title="Sessions", show_header=True, header_style=ACCENT_HEADER)
    table.add_column("Key")
    table.add_column("Name")
    table.add_column("Agent")
    table.add_column("Status")
    table.add_column("Model")
    table.add_column("Messages", justify="right")
    for row in rows:
        table.add_row(
            str(row.get("key") or ""),
            # Table cells are markup-parsed too: one session named "[/]" would
            # otherwise take down the whole listing.
            markup_escape(_row_name(row)),
            str(row.get("agent_id") or row.get("agentId") or ""),
            str(row.get("status") or ""),
            str(row.get("model") or ""),
            str(row.get("message_count") or row.get("entry_count") or 0),
        )
    console.print(table)


@app.command("show")
def sessions_show(
    session_id: str = typer.Argument(..., help="Session ID to inspect"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show details of a specific session."""

    async def _run(client):
        resolved = await client.resolve_session(session_id)
        preview = await client.preview_sessions(keys=[_resolved_key(resolved, session_id)])
        return {"resolved": resolved, "preview": preview}

    result = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(result)
        return

    resolved = result.get("resolved", {}) if isinstance(result, dict) else {}
    previews = result.get("preview", {}).get("previews", []) if isinstance(result, dict) else []
    preview = previews[0] if previews else {}
    key = _resolved_key(resolved, session_id)
    table = Table(title=f"Session {key}", show_header=True, header_style=ACCENT_HEADER)
    table.add_column("Field", style=ACCENT)
    table.add_column("Value")
    for field, value in (
        ("session_key", key),
        ("session_id", resolved.get("session_id")),
        ("agent_id", resolved.get("agent_id")),
        ("status", resolved.get("status")),
        ("model", resolved.get("model")),
        ("updated_at", resolved.get("updated_at") or preview.get("updatedAt")),
        ("title", preview.get("title")),
    ):
        if value not in (None, ""):
            table.add_row(field, str(value))
    console.print(table)
    last_message = str(preview.get("lastMessage") or "")
    if last_message:
        console.print(last_message)


@app.command("resume")
def sessions_resume(session_id: str = typer.Argument(..., help="Session ID to resume")) -> None:
    """Resume a session in interactive chat."""
    from agentos.cli.chat_cmd import run_chat

    async def _run(client):
        return await client.resolve_session(session_id)

    result = asyncio.run(_with_client(_run))
    if result is _CLIENT_UNAVAILABLE:
        console.print(f"[dim]Session {session_id!r} requires a running gateway.[/dim]")
        return
    if result is _ACTION_FAILED:
        return
    run_chat(session_id=_resolved_key(result, session_id))


@app.command("abort")
def sessions_abort(
    session_id: str = typer.Argument(..., help="Session ID to abort"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Abort a running session turn."""

    async def _run(client):
        resolved = await client.resolve_session(session_id)
        key = _resolved_key(resolved, session_id)
        result = await client.abort_session(key)
        if isinstance(result, dict):
            return {"resolved": resolved, **result}
        return {"resolved": resolved, "result": result}

    payload = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(payload)
        return
    key = payload.get("key") or session_id
    aborted = bool(payload.get("aborted", False))
    console.print(f"{'Aborted' if aborted else 'No running task for'} session {key!r}")


@app.command("rename")
def sessions_rename(
    session_id: str = typer.Argument(..., help="Session ID (or current name) to rename"),
    name: str = typer.Argument(
        "", help="New session name; pass an empty string or --clear to remove it"
    ),
    clear: bool = typer.Option(False, "--clear", help="Clear the custom name"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Give a session a human-readable name so it is easy to find later."""

    async def _run(client):
        return await client.rename_session(session_id, None if clear else name)

    payload = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(payload)
        return
    key = payload.get("key") or session_id
    new_name = payload.get("name") or payload.get("displayName")
    if new_name:
        # Names are user-typed and Rich parses markup in everything it prints,
        # so escape before interpolating — an unbalanced "[/]" in a name would
        # otherwise raise MarkupError instead of printing.
        console.print(f"Renamed session {key!r} to [{ACCENT}]{markup_escape(new_name)}[/]")
    else:
        console.print(f"Cleared the custom name for session {key!r}")


@app.command("delete")
def sessions_delete(
    session_id: str = typer.Argument(..., help="Session ID to delete"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Delete a session."""
    if not yes:
        confirmed = typer.confirm(f"Delete session {session_id!r}?")
        if not confirmed:
            raise typer.Abort()

    async def _run(client):
        resolved = await client.resolve_session(session_id)
        key = _resolved_key(resolved, session_id)
        return await client.delete_sessions([key])

    result = asyncio.run(_with_client(_run))
    if result is _CLIENT_UNAVAILABLE:
        console.print("[dim]Session deletion requires a running gateway.[/dim]")
        return
    if result is _ACTION_FAILED:
        return
    console.print_json(data=result)


@app.command("export")
def sessions_export(
    session_id: str = typer.Argument(..., help="Session ID to export"),
    output: Path | None = typer.Option(None, "--output", "-o", help="Output file"),
    format: str = typer.Option("md", "--format", help="Export format: md|json"),
) -> None:
    """Export session transcript and metadata.

    Uses the existing chat.history RPC for persisted transcript messages and
    falls back to session preview when no messages are available.
    """
    if format not in {"md", "json"}:
        console.print("[red]--format must be md or json[/red]")
        raise typer.Exit(2)

    async def _run(client):
        resolved = await client.resolve_session(session_id)
        key = _resolved_key(resolved, session_id)
        preview = await client.preview_sessions(keys=[key])
        history = await client.session_history(key, limit=1000)
        return {"resolved": resolved, "preview": preview, "history": history}

    result: dict[str, Any] | None = asyncio.run(_with_client(_run))
    if result is _CLIENT_UNAVAILABLE:
        console.print("[dim]Session export requires a running gateway.[/dim]")
        return
    if result is _ACTION_FAILED:
        return
    if result is None:
        console.print("[red]Session export returned no data.[/red]")
        return
    target = output or Path(f"{_safe_archive_part(session_id)}.{format}")
    if format == "json":
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        resolved = result.get("resolved", {})
        key = _resolved_key(resolved, session_id)
        previews = result.get("preview", {}).get("previews", [])
        preview = previews[0] if previews else {}
        messages = result.get("history", {}).get("messages", [])
        transcript = messages_to_markdown(messages) if isinstance(messages, list) else ""
        if not transcript.strip():
            transcript = f"## Preview\n\n{preview.get('lastMessage', '')}\n"
        body = (
            f"# Session {key}\n\n"
            f"- Status: {resolved.get('status', '')}\n"
            f"- Model: {resolved.get('model') or ''}\n"
            f"- Updated: {resolved.get('updated_at', '')}\n\n"
            f"{transcript}"
        )
        target.write_text(body, encoding="utf-8")
    console.print(f"[green]Exported:[/green] {target}")
