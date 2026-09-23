"""Projects command — group chat sessions and share project knowledge."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from rich.table import Table

from agentos.cli.gateway_rpc import run_gateway_sync
from agentos.cli.output import print_json
from agentos.cli.ui import ACCENT, ACCENT_HEADER, console, markup_escape

app = typer.Typer(help="Manage session projects (grouping + shared knowledge).")


def _project_payload(result: dict[str, Any]) -> dict[str, Any]:
    project = result.get("project") if isinstance(result, dict) else None
    return project if isinstance(project, dict) else {}


def _read_knowledge(knowledge: str | None, knowledge_file: Path | None) -> str | None:
    if knowledge is not None and knowledge_file is not None:
        raise typer.BadParameter("Use either --knowledge or --knowledge-file, not both")
    if knowledge_file is not None:
        try:
            return knowledge_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise typer.BadParameter(f"Cannot read {knowledge_file}: {exc}") from exc
    return knowledge


def _excerpt(text: str, max_chars: int = 60) -> str:
    flattened = " ".join(text.split())
    if len(flattened) <= max_chars:
        return flattened
    return flattened[: max_chars - 1] + "…"


@app.command("list")
def projects_list(
    agent: str | None = typer.Option(
        None,
        "--agent",
        help="Filter by the project's default agent (not a membership filter)",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """List projects with session counts."""

    async def _run(client):
        return await client.list_projects(agent_id=agent)

    result = run_gateway_sync(_run, json_output=json_output)
    rows = result.get("projects", []) if isinstance(result, dict) else []
    if json_output:
        print_json(result if isinstance(result, dict) else {"projects": rows})
        return

    table = Table(title="Projects", show_header=True, header_style=ACCENT_HEADER)
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Agent")
    table.add_column("Sessions", justify="right")
    table.add_column("Knowledge")
    for row in rows:
        if not isinstance(row, dict):
            continue
        table.add_row(
            str(row.get("project_id") or ""),
            markup_escape(str(row.get("name") or "")),
            str(row.get("agent_id") or ""),
            str(row.get("session_count") or 0),
            markup_escape(_excerpt(str(row.get("knowledge") or ""))),
        )
    console.print(table)


@app.command("create")
def projects_create(
    name: str = typer.Argument(..., help="Project name (unique across projects)"),
    agent: str = typer.Option(
        "main", "--agent", help="Default agent for new chats in the project"
    ),
    knowledge: str | None = typer.Option(
        None, "--knowledge", help="Shared knowledge text injected into member sessions"
    ),
    knowledge_file: Path | None = typer.Option(
        None, "--knowledge-file", help="Read the knowledge text from a file"
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Create a project; its knowledge is injected into every member session."""
    knowledge_text = _read_knowledge(knowledge, knowledge_file) or ""

    async def _run(client):
        return await client.create_project(name, knowledge=knowledge_text, agent_id=agent)

    result = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(result)
        return
    project = _project_payload(result)
    console.print(
        f"Created project [{ACCENT}]{markup_escape(str(project.get('name') or name))}[/] "
        f"({project.get('project_id')})"
    )


@app.command("show")
def projects_show(
    project_id: str = typer.Argument(..., help="Project ID to inspect"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show project details, member sessions, and knowledge."""

    async def _run(client):
        project = await client.get_project(project_id)
        sessions = await client.call("sessions.list", {"projectId": project_id, "limit": 200})
        return {"project": project.get("project"), "sessions": sessions.get("sessions", [])}

    result = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(result)
        return

    project = result.get("project") if isinstance(result, dict) else None
    project = project if isinstance(project, dict) else {}
    sessions = result.get("sessions", []) if isinstance(result, dict) else []
    table = Table(
        title=f"Project {project.get('project_id') or project_id}",
        show_header=True,
        header_style=ACCENT_HEADER,
    )
    table.add_column("Field", style=ACCENT)
    table.add_column("Value")
    for field, value in (
        ("name", project.get("name")),
        ("agent_id", project.get("agent_id")),
        ("sessions", project.get("session_count")),
        ("updated_at", project.get("updated_at")),
    ):
        if value not in (None, ""):
            table.add_row(field, markup_escape(str(value)))
    console.print(table)
    knowledge = str(project.get("knowledge") or "")
    if knowledge:
        console.print(f"[{ACCENT}]Knowledge:[/]")
        console.print(markup_escape(knowledge))
    if sessions:
        session_table = Table(title="Sessions", show_header=True, header_style=ACCENT_HEADER)
        session_table.add_column("Key")
        session_table.add_column("Name")
        session_table.add_column("Status")
        for row in sessions:
            if not isinstance(row, dict):
                continue
            session_table.add_row(
                str(row.get("key") or ""),
                markup_escape(
                    str(row.get("display_name") or row.get("derived_title") or "")
                ),
                str(row.get("status") or ""),
            )
        console.print(session_table)


@app.command("update")
def projects_update(
    project_id: str = typer.Argument(..., help="Project ID to update"),
    name: str | None = typer.Option(None, "--name", help="New project name"),
    knowledge: str | None = typer.Option(
        None, "--knowledge", help="Replacement knowledge text (full replace)"
    ),
    knowledge_file: Path | None = typer.Option(
        None, "--knowledge-file", help="Read the replacement knowledge text from a file"
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Rename a project and/or replace its knowledge text."""
    knowledge_text = _read_knowledge(knowledge, knowledge_file)
    if name is None and knowledge_text is None:
        raise typer.BadParameter("Provide --name, --knowledge, or --knowledge-file")

    async def _run(client):
        return await client.update_project(project_id, name=name, knowledge=knowledge_text)

    result = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(result)
        return
    project = _project_payload(result)
    console.print(
        f"Updated project [{ACCENT}]{markup_escape(str(project.get('name') or project_id))}[/]"
    )


@app.command("delete")
def projects_delete(
    project_id: str = typer.Argument(..., help="Project ID to delete"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Delete a project. Member sessions survive and become project-less."""
    if not yes:
        confirmed = typer.confirm(
            f"Delete project {project_id!r}? Sessions are kept and detached."
        )
        if not confirmed:
            raise typer.Abort()

    async def _run(client):
        return await client.delete_project(project_id)

    result = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(result)
        return
    cleared = result.get("sessions_cleared", 0) if isinstance(result, dict) else 0
    console.print(
        f"Deleted project {markup_escape(repr(project_id))} ({cleared} session(s) detached)"
    )


@app.command("move")
def projects_move(
    session_id: str = typer.Argument(..., help="Session key to move"),
    project_id: str = typer.Argument(
        ..., help="Target project ID, or 'none' to detach the session"
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Move a session into a project (or out of one with 'none')."""
    target = None if project_id.strip().lower() == "none" else project_id.strip()

    async def _run(client):
        resolved = await client.resolve_session(session_id)
        key = str(resolved.get("session_key") or resolved.get("key") or session_id)
        result = await client.move_session_to_project(key, target)
        return {"key": key, **(result if isinstance(result, dict) else {})}

    result = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(result)
        return
    key = result.get("key") or session_id
    if target is None:
        console.print(f"Detached session {markup_escape(repr(key))} from its project")
    else:
        console.print(
            f"Moved session {markup_escape(repr(key))} into project {markup_escape(repr(target))}"
        )
