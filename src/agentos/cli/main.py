"""AgentOS CLI — Typer app with sub-commands."""

from __future__ import annotations

from pathlib import Path

import typer

from agentos.env import load_env, warn_if_proxy_ignored


class _CurrentStderr:
    """A file-like that resolves ``sys.stderr`` on every write.

    Handing structlog ``sys.stderr`` directly would freeze whichever object is
    installed at import time. Anything that replaces the stream afterwards —
    pytest's capture, a TUI taking over the terminal, a caller redirecting for
    one command — would then be writing past the replacement, and the logs
    would surface somewhere nobody is looking.
    """

    def write(self, message: str) -> int:
        import sys

        return sys.stderr.write(message)

    def flush(self) -> None:
        import sys

        sys.stderr.flush()


#: What the CLI shows on stderr unless ``AGENTOS_LOG_LEVEL`` says otherwise.
#: Debug events are diagnostics for the gateway log, not terminal output for
#: a command someone ran to read its result.
_CLI_DEFAULT_LOG_LEVEL = "INFO"


def _cli_log_level() -> int:
    """The structlog threshold for a CLI process, from ``AGENTOS_LOG_LEVEL``.

    Same variable the gateway and the chat REPL honour, so one setting
    controls all three; an unknown name falls back to the default rather
    than to "everything".
    """
    import logging
    import os

    name = (os.environ.get("AGENTOS_LOG_LEVEL") or _CLI_DEFAULT_LOG_LEVEL).strip().upper()
    level = logging.getLevelName(name)
    return level if isinstance(level, int) else logging.INFO


def _route_logs_to_stderr() -> None:
    """Send structlog output to stderr before anything can log, above a level.

    structlog's unconfigured default prints to stdout, and the first thing the
    CLI does is load .env files — which log. That put log lines in front of
    every ``--json`` payload the moment a user had a populated .env, so
    ``agentos <anything> --json | jq`` failed on a real install while working
    in a clean one. Logs are diagnostics; stdout belongs to the command's
    output.

    The level filter is the other half of that: with none, every
    ``log.debug`` in the code a command happens to touch reaches the terminal.
    ``agentos context`` resolves five tool profiles and got ~190
    ``tool_filtered`` lines on top of its tables (#2896). The gateway raises
    the threshold back to its own configured level when it boots.
    """
    from typing import TextIO, cast

    import structlog

    # PrintLogger only ever calls write() and flush() on this, which is the
    # whole contract _CurrentStderr implements — the annotation asks for a full
    # TextIO that structlog does not actually use.
    stream = cast("TextIO", _CurrentStderr())
    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=stream),
        wrapper_class=structlog.make_filtering_bound_logger(_cli_log_level()),
    )


_route_logs_to_stderr()

# Populate os.environ from .env files before any submodule import reads keys.
# Precedence: os.environ > $CWD/.env > $CWD/.env.test > ~/.agentos/.env.
load_env()
warn_if_proxy_ignored()

from agentos.cli.agent_cmd import run_agent_command  # noqa: E402
from agentos.cli.agents_cmd import agents_app  # noqa: E402
from agentos.cli.auth_cmd import auth_app  # noqa: E402
from agentos.cli.channels_cmd import channels_app  # noqa: E402
from agentos.cli.config_cmd import app as config_app  # noqa: E402
from agentos.cli.context_cmd import app as context_app  # noqa: E402
from agentos.cli.cost_cmd import app as cost_app  # noqa: E402
from agentos.cli.cron_cmd import cron_app  # noqa: E402
from agentos.cli.diagnostics_cmd import diagnostics_app  # noqa: E402
from agentos.cli.dist_cmd import app as dist_app  # noqa: E402
from agentos.cli.doctor_cmd import doctor_command  # noqa: E402
from agentos.cli.env_cmd import env_app  # noqa: E402
from agentos.cli.init_cmd import init_command  # noqa: E402
from agentos.cli.mcp_server_cmd import app as mcp_server_app  # noqa: E402
from agentos.cli.migrate_cmd import migrate_app  # noqa: E402
from agentos.cli.models_cmd import app as models_app  # noqa: E402
from agentos.cli.onboard_cmd import configure_command, onboard_app  # noqa: E402
from agentos.cli.projects_cmd import app as projects_app  # noqa: E402
from agentos.cli.providers_cmd import providers_app  # noqa: E402
from agentos.cli.replay import replay_app  # noqa: E402
from agentos.cli.sandbox_cmd import sandbox_app  # noqa: E402
from agentos.cli.search_cmd import search_app  # noqa: E402
from agentos.cli.sessions_cmd import app as sessions_app  # noqa: E402
from agentos.cli.skills_cmd import skills_app  # noqa: E402
from agentos.cli.upgrade_cmd import upgrade_command  # noqa: E402

app = typer.Typer(
    name="agentos",
    help="AgentOS - Python agent runtime with multi-channel support.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

# ── Sub-apps ─────────────────────────────────────────────────────────────────

app.add_typer(auth_app, name="auth")
app.add_typer(channels_app, name="channels")
app.add_typer(agents_app, name="agents")
app.add_typer(config_app, name="config")
app.add_typer(context_app, name="context")
app.add_typer(cost_app, name="cost")
app.add_typer(diagnostics_app, name="diagnostics")
app.add_typer(env_app, name="env")
app.add_typer(cron_app, name="cron")
app.add_typer(dist_app, name="dist")
app.add_typer(mcp_server_app, name="mcp-server")
app.add_typer(migrate_app, name="migrate")
app.add_typer(models_app, name="models")
app.add_typer(projects_app, name="projects")
app.add_typer(providers_app, name="providers")
app.add_typer(sandbox_app, name="sandbox")
app.add_typer(search_app, name="search")
app.add_typer(sessions_app, name="sessions")
app.add_typer(skills_app, name="skills")


def _version_callback(value: bool) -> None:
    """Print the installed version and exit, before any command runs.

    Eager so ``agentos --version`` answers without Typer demanding a
    subcommand, which is what ``no_args_is_help`` would otherwise do.
    """
    if not value:
        return
    from agentos import __version__

    typer.echo(__version__)
    raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False,
        "--version",
        help="Show the installed AgentOS version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """AgentOS - Python agent runtime with multi-channel support."""


app.command("init")(init_command)
app.command("doctor")(doctor_command)
app.command("upgrade")(upgrade_command)
app.add_typer(onboard_app, name="onboard")
app.command("configure")(configure_command)


# ── memory sub-app ────────────────────────────────────────────────────────────

memory_app = typer.Typer(help="Memory subsystem commands.")
app.add_typer(memory_app, name="memory")
raw_fallbacks_app = typer.Typer(help="Raw fallback receipt commands.")
memory_app.add_typer(raw_fallbacks_app, name="raw-fallbacks")


@memory_app.command("status")
def memory_status_cmd(
    agent_id: str = typer.Option("main", "--agent", help="Agent id (default: main)"),
    deep: bool = typer.Option(False, "--deep", help="Include detailed retrieval health"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    config_path: Path | None = typer.Option(None, "--config", help="Override config path."),
) -> None:
    """Show read-only memory backend status from the running gateway."""

    from rich.table import Table

    from agentos.cli.gateway_rpc import run_gateway_sync
    from agentos.cli.output import print_json
    from agentos.cli.ui import console

    async def _run(client):
        params: dict[str, object] = {"agentId": agent_id}
        if deep:
            params["deep"] = True
        return await client.call("doctor.memory.status", params)

    payload = run_gateway_sync(_run, json_output=json_output, config_path=config_path)
    if json_output:
        print_json(payload)
        return

    table = Table(title=f"Memory status — agent={agent_id}", show_header=True)
    table.add_column("Backend")
    table.add_column("Status")
    table.add_column("Entries", justify="right")
    table.add_column("Size bytes", justify="right")
    table.add_column("Sources")
    table.add_column("Error")
    source_counts = payload.get("sourceCounts") or {}
    source_summary = ", ".join(
        f"{source} {counts.get('files', 0)} files/{counts.get('chunks', 0)} chunks"
        for source, counts in sorted(source_counts.items())
        if isinstance(counts, dict)
    )
    table.add_row(
        str(payload.get("backend") or ""),
        str(payload.get("status") or ""),
        "" if payload.get("entryCount") is None else str(payload.get("entryCount")),
        "" if payload.get("sizeBytes") is None else str(payload.get("sizeBytes")),
        source_summary,
        str(payload.get("error") or ""),
    )
    console.print(table)

    curated = payload.get("curated") or {}
    if curated:
        curated_table = Table(title="Curated memory (MEMORY.md / USER.md)", show_header=True)
        curated_table.add_column("Store")
        curated_table.add_column("Entries", justify="right")
        curated_table.add_column("Usage")
        for store_name in ("memory", "user"):
            store_status = curated.get(store_name) or {}
            if not store_status:
                continue
            curated_table.add_row(
                "MEMORY.md" if store_name == "memory" else "USER.md",
                str(store_status.get("entries", "")),
                str(store_status.get("usage", "")),
            )
        console.print(curated_table)


@memory_app.command("index")
def memory_index_cmd(
    agent_id: str = typer.Option("main", "--agent", help="Agent id (default: main)"),
    force: bool = typer.Option(False, "--force", help="Rebuild index rows and rescan sources"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Sync or force-rebuild the memory search index through the gateway."""

    from agentos.cli.gateway_rpc import run_gateway_sync
    from agentos.cli.output import print_json
    from agentos.cli.ui import console

    async def _run(client):
        return await client.call("memory.index", {"agentId": agent_id, "force": force})

    payload = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(payload)
        return
    console.print(
        f"memory index agent={payload.get('agentId', agent_id)} force={bool(payload.get('force'))}"
    )


curated_app = typer.Typer(help="Curated memory commands (MEMORY.md / USER.md).")
memory_app.add_typer(curated_app, name="curated")


@curated_app.command("get")
def memory_curated_get_cmd(
    target: str = typer.Option("memory", "--target", "-t", help="Target: memory or user"),
    agent_id: str = typer.Option("main", "--agent", help="Agent id (default: main)"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Get curated memory entries and usage."""

    from agentos.cli.gateway_rpc import run_gateway_sync
    from agentos.cli.output import print_json
    from agentos.cli.ui import console

    async def _run(client):
        return await client.call("memory.curated.get", {"agentId": agent_id, "target": target})

    payload = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(payload)
        return

    console.print(f"[bold]{target.upper()}.md[/bold] ({payload.get('usage', '')})")
    for i, entry in enumerate(payload.get("entries", []), 1):
        console.print(f"  {i}. {entry}")


@curated_app.command("add")
def memory_curated_add_cmd(
    content: str = typer.Argument(..., help="Content to add to curated memory"),
    target: str = typer.Option("memory", "--target", "-t", help="Target: memory or user"),
    agent_id: str = typer.Option("main", "--agent", help="Agent id (default: main)"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Add an entry to curated memory."""

    from agentos.cli.gateway_rpc import run_gateway_sync
    from agentos.cli.output import print_json
    from agentos.cli.ui import console

    async def _run(client):
        return await client.call(
            "memory.curated.add", {"agentId": agent_id, "target": target, "content": content}
        )

    payload = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(payload)
        return
    console.print(f"[green]{payload.get('message', 'Entry added.')}[/green]")


@curated_app.command("remove")
def memory_curated_remove_cmd(
    old_text: str = typer.Argument(..., help="Text of the entry to remove"),
    target: str = typer.Option("memory", "--target", "-t", help="Target: memory or user"),
    agent_id: str = typer.Option("main", "--agent", help="Agent id (default: main)"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Remove an entry from curated memory."""

    from agentos.cli.gateway_rpc import run_gateway_sync
    from agentos.cli.output import print_json
    from agentos.cli.ui import console

    async def _run(client):
        return await client.call(
            "memory.curated.remove", {"agentId": agent_id, "target": target, "oldText": old_text}
        )

    payload = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(payload)
        return
    console.print(f"[green]{payload.get('message', 'Entry removed.')}[/green]")


@memory_app.command("ingest")
def memory_ingest_cmd(
    path: str = typer.Argument(
        ..., help="Path to a document file or folder to ingest into knowledge base"
    ),
    agent_id: str = typer.Option("main", "--agent", help="Agent id (default: main)"),
    recursive: bool = typer.Option(
        True, "--recursive/--no-recursive", help="Recursively ingest directories"
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Ingest a document file or folder into the agent's knowledge base."""

    from rich.table import Table

    from agentos.cli.gateway_rpc import run_gateway_sync
    from agentos.cli.output import print_json
    from agentos.cli.ui import console

    async def _run(client):
        return await client.call(
            "memory.knowledge_base.ingest",
            {"path": path, "agentId": agent_id, "recursive": recursive},
        )

    payload = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(payload)
        return

    results = payload.get("results", [])
    table = Table(title=f"Ingested documents — agent={agent_id}", show_header=True)
    table.add_column("Path")
    table.add_column("Chunks", justify="right")
    table.add_column("Size bytes", justify="right")
    table.add_column("Status")
    table.add_column("Error")
    for r in results:
        table.add_row(
            str(r.get("path") or ""),
            str(r.get("chunksIndexed") or 0),
            str(r.get("sizeBytes") or 0),
            str(r.get("status") or ""),
            str(r.get("error") or ""),
        )
    console.print(table)


@memory_app.command("list")
def memory_list_cmd(
    agent_id: str = typer.Option("main", "--agent", help="Agent id (default: main)"),
    source: str | None = typer.Option(
        None, "--source", help="Filter source: memory, knowledge_base, sessions, or all"
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """List durable memory source files from the running gateway."""

    from rich.table import Table

    from agentos.cli.gateway_rpc import run_gateway_sync
    from agentos.cli.output import print_json
    from agentos.cli.ui import console

    async def _run(client):
        params: dict[str, object] = {"agentId": agent_id}
        if source:
            params["source"] = source
        return await client.call("memory.list", params)

    payload = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(payload)
        return

    table = Table(title=f"Memory sources - agent={agent_id}", show_header=True)
    table.add_column("Path")
    table.add_column("Source")
    table.add_column("Lines", justify="right")
    table.add_column("Size bytes", justify="right")
    table.add_column("Modified")
    for row in payload.get("files", []):
        table.add_row(
            str(row.get("path") or ""),
            str(row.get("source") or "memory"),
            "" if row.get("lineCount") is None else str(row.get("lineCount")),
            "" if row.get("sizeBytes") is None else str(row.get("sizeBytes")),
            str(row.get("modifiedAt") or ""),
        )
    console.print(table)


@memory_app.command("search")
def memory_search_cmd(
    query: str = typer.Argument(..., help="Search query"),
    agent_id: str = typer.Option("main", "--agent", help="Agent id (default: main)"),
    limit: int = typer.Option(10, "--limit", "-n", help="Maximum results"),
    source: str = typer.Option(
        "memory",
        "--source",
        help="Search source: memory, knowledge_base, sessions, or all",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Search durable memory from the running gateway."""

    from rich.table import Table

    from agentos.cli.gateway_rpc import run_gateway_sync
    from agentos.cli.output import print_json
    from agentos.cli.ui import console

    async def _run(client):
        return await client.call(
            "memory.search",
            {"query": query, "agentId": agent_id, "limit": limit, "source": source},
        )

    payload = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(payload)
        return

    table = Table(title=f"Memory search - agent={agent_id}", show_header=True)
    table.add_column("Source")
    table.add_column("Path")
    table.add_column("Lines")
    table.add_column("Score", justify="right")
    table.add_column("Snippet")
    for row in payload.get("results", []):
        table.add_row(
            str(row.get("source") or "memory"),
            str(row.get("path") or ""),
            f"{row.get('startLine', '')}-{row.get('endLine', '')}",
            f"{float(row.get('score') or 0.0):.3f}",
            str(row.get("snippet") or "")[:120],
        )
    console.print(table)


@memory_app.command("show")
def memory_show_cmd(
    path: str = typer.Argument(..., help="Memory source path"),
    agent_id: str = typer.Option("main", "--agent", help="Agent id (default: main)"),
    from_line: int | None = typer.Option(None, "--from-line", help="Start line, 1-indexed"),
    lines: int | None = typer.Option(None, "--lines", help="Number of lines to return"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show one durable memory source from the running gateway."""

    from agentos.cli.gateway_rpc import run_gateway_sync
    from agentos.cli.output import print_json
    from agentos.cli.ui import console

    async def _run(client):
        params: dict[str, object] = {"path": path, "agentId": agent_id}
        if from_line is not None:
            params["fromLine"] = from_line
        if lines is not None:
            params["lines"] = lines
        return await client.call("memory.show", params)

    payload = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(payload)
        return
    console.print(str(payload.get("content") or ""))
    if payload.get("truncated"):
        console.print("[dim]... truncated[/dim]")


@memory_app.command("embedding-download")
def memory_embedding_download_cmd(
    model: str = typer.Option(
        "google/embeddinggemma-300m",
        "--model",
        help="Embedding model id to download",
    ),
) -> None:
    """Download a local embedding model's ONNX export to the models dir.

    This is a local filesystem operation (no running gateway required): it
    fetches the model files directly from Hugging Face into
    ``~/.agentos/models/embeddings/<target-dir>``.
    """

    import asyncio

    from agentos.cli.ui import console
    from agentos.memory.model_download import (
        EMBEDDING_MODEL_MANIFESTS,
        download_embedding_model,
    )

    if model not in EMBEDDING_MODEL_MANIFESTS:
        console.print(f"[red]Unknown embedding model id: {model}[/red]")
        raise typer.Exit(code=1)

    manifest = EMBEDDING_MODEL_MANIFESTS[model]
    target_dir = None

    def _progress(name: str, done: int, total: int | None) -> None:
        if total:
            pct = 100 * done // total
            console.print(f"  {name}: {done}/{total} bytes ({pct}%)")
        else:
            console.print(f"  {name}: {done} bytes")

    console.print(
        f"Downloading {model} (~{manifest.approx_total_mb} MB) to {manifest.target_dirname}/"
    )

    async def _run() -> Path:
        return await download_embedding_model(model, progress=_progress)

    try:
        target_dir = asyncio.run(_run())
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print(f"[green]Downloaded to {target_dir}[/green]")
    console.print(
        "Restart the gateway (or stop/start it) to pick up the new model; "
        "reindexing then happens automatically on the next sync."
    )


@raw_fallbacks_app.command("list")
def memory_raw_fallbacks_list_cmd(
    agent_id: str = typer.Option("main", "--agent", help="Agent id (default: main)"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """List raw fallback receipts from the running gateway."""

    from rich.table import Table

    from agentos.cli.gateway_rpc import run_gateway_sync
    from agentos.cli.output import print_json
    from agentos.cli.ui import console

    async def _run(client):
        return await client.call("memory.raw_fallbacks.list", {"agentId": agent_id})

    payload = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(payload)
        return

    table = Table(title=f"Raw memory fallbacks - agent={agent_id}", show_header=True)
    table.add_column("Path")
    table.add_column("Size bytes", justify="right")
    table.add_column("Reason")
    table.add_column("Modified")
    for row in payload.get("files", []):
        table.add_row(
            str(row.get("path") or ""),
            "" if row.get("sizeBytes") is None else str(row.get("sizeBytes")),
            str(row.get("reason") or ""),
            str(row.get("modifiedAt") or ""),
        )
    console.print(table)


@raw_fallbacks_app.command("show")
def memory_raw_fallbacks_show_cmd(
    path: str = typer.Argument(..., help="Raw fallback path"),
    agent_id: str = typer.Option("main", "--agent", help="Agent id (default: main)"),
    from_line: int | None = typer.Option(None, "--from-line", help="Start line, 1-indexed"),
    lines: int | None = typer.Option(None, "--lines", help="Number of lines to return"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show one raw fallback receipt from the running gateway."""

    from agentos.cli.gateway_rpc import run_gateway_sync
    from agentos.cli.output import print_json
    from agentos.cli.ui import console

    async def _run(client):
        params: dict[str, object] = {"path": path, "agentId": agent_id}
        if from_line is not None:
            params["fromLine"] = from_line
        if lines is not None:
            params["lines"] = lines
        return await client.call("memory.raw_fallbacks.show", params)

    payload = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(payload)
        return
    console.print(str(payload.get("content") or ""))
    if payload.get("truncated"):
        console.print("[dim]... truncated[/dim]")


# ── gateway sub-app ───────────────────────────────────────────────────────────

gateway_app = typer.Typer(help="Gateway server commands.")
app.add_typer(gateway_app, name="gateway")


@gateway_app.command("run")
def gateway_run(
    port: int | None = typer.Option(
        None,
        "--port",
        "-p",
        help="Port to bind (default: config port, usually 18791)",
    ),
    bind: str | None = typer.Option(
        None,
        "--bind",
        "-b",
        help="Host to bind (default: config host, usually 127.0.0.1)",
    ),
    listen: str = typer.Option(
        "",
        "--listen",
        help="Host to bind (alias of --bind; wins over --bind when both supplied)",
    ),
    debug: bool = typer.Option(False, "--debug", help="Enable debug mode"),
    config_path: str | None = typer.Option(None, "--config", help="Override config path."),
) -> None:
    """Start the ASGI gateway server.

    Precedence for the bind address: --listen > --bind > AGENTOS_LISTEN >
    AGENTOS_GATEWAY_HOST > default (127.0.0.1). Binding to 0.0.0.0 or :: is
    opt-in only — the gateway's default auth assumes loopback scope.
    """
    from agentos.cli.gateway_cmd import run_gateway

    run_gateway(
        port=port,
        bind=bind,
        listen=listen,
        debug=debug,
        config_path=config_path,
    )


@gateway_app.command("start")
def gateway_start(
    port: int | None = typer.Option(
        None,
        "--port",
        "-p",
        help="Port to bind (default: config port, usually 18791)",
    ),
    bind: str | None = typer.Option(
        None,
        "--bind",
        "-b",
        help="Host to bind (default: config host, usually 127.0.0.1)",
    ),
    listen: str = typer.Option("", "--listen", help="Host to bind (wins over --bind)"),
    config_path: str | None = typer.Option(None, "--config", help="Override config path."),
    health_timeout: float = typer.Option(60.0, "--timeout", help="Readiness wait timeout"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Start the gateway in the background and wait for readiness."""
    from agentos.cli.gateway_cmd import start_gateway

    start_gateway(
        port=port,
        bind=bind,
        listen=listen,
        config_path=config_path,
        health_timeout=health_timeout,
        json_output=json_output,
    )


@gateway_app.command("status")
def gateway_status(
    port: int | None = typer.Option(
        None,
        "--port",
        "-p",
        help="Port to inspect (default: config port, usually 18791)",
    ),
    bind: str | None = typer.Option(
        None,
        "--bind",
        "-b",
        help="Host to inspect (default: config host, usually 127.0.0.1)",
    ),
    listen: str = typer.Option("", "--listen", help="Host to inspect (wins over --bind)"),
    config_path: str | None = typer.Option(None, "--config", help="Override config path."),
    gateway_url: str | None = typer.Option(
        None,
        "--gateway",
        help="Remote gateway URL to inspect instead of local lifecycle state.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Inspect the managed gateway process without mutating state."""
    from agentos.cli.gateway_cmd import status_gateway

    status_gateway(
        port=port,
        bind=bind,
        listen=listen,
        config_path=config_path,
        gateway_url=gateway_url,
        json_output=json_output,
    )


@gateway_app.command("stop")
def gateway_stop(
    port: int | None = typer.Option(
        None,
        "--port",
        "-p",
        help="Port to stop (default: config port, usually 18791)",
    ),
    bind: str | None = typer.Option(
        None,
        "--bind",
        "-b",
        help="Host to stop (default: config host, usually 127.0.0.1)",
    ),
    listen: str = typer.Option("", "--listen", help="Host to stop (wins over --bind)"),
    config_path: str | None = typer.Option(None, "--config", help="Override config path."),
    shutdown_timeout: float = typer.Option(10.0, "--timeout", help="Shutdown wait timeout"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Stop the recorded gateway process."""
    from agentos.cli.gateway_cmd import stop_gateway

    stop_gateway(
        port=port,
        bind=bind,
        listen=listen,
        config_path=config_path,
        shutdown_timeout=shutdown_timeout,
        json_output=json_output,
    )


@gateway_app.command("restart")
def gateway_restart(
    port: int | None = typer.Option(
        None,
        "--port",
        "-p",
        help="Port to restart (default: config port, usually 18791)",
    ),
    bind: str | None = typer.Option(
        None,
        "--bind",
        "-b",
        help="Host to restart (default: config host, usually 127.0.0.1)",
    ),
    listen: str = typer.Option("", "--listen", help="Host to restart (wins over --bind)"),
    config_path: str | None = typer.Option(None, "--config", help="Override config path."),
    health_timeout: float = typer.Option(60.0, "--timeout", help="Readiness wait timeout"),
    shutdown_timeout: float = typer.Option(
        10.0, "--shutdown-timeout", help="Shutdown wait timeout"
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Restart the recorded gateway process."""
    from agentos.cli.gateway_cmd import restart_gateway

    restart_gateway(
        port=port,
        bind=bind,
        listen=listen,
        config_path=config_path,
        health_timeout=health_timeout,
        shutdown_timeout=shutdown_timeout,
        json_output=json_output,
    )


# ── replay sub-app ────────────────────────────────────────────────────────────

app.add_typer(replay_app, name="replay")


# ── top-level commands ────────────────────────────────────────────────────────


@app.command("agent")
def agent(
    message: str = typer.Option(..., "--message", "-m", help="Message to send"),
    agent_id: str = typer.Option("main", "--agent", help="Agent identifier"),
    session_id: str = typer.Option("", "--session-id", help="Session key/id to use"),
    model: str = typer.Option("", "--model", help="Model override"),
    workspace: str = typer.Option("", "--workspace", help="Workspace root for this run"),
    workspace_strict: bool | None = typer.Option(
        None,
        "--workspace-strict/--no-workspace-strict",
        help="Restrict read-side file tools to --workspace",
    ),
    workspace_lockdown: bool = typer.Option(
        False,
        "--workspace-lockdown",
        help=(
            "Opt in to automation write containment: writes must stay under "
            "--workspace or --scratch-dir."
        ),
    ),
    scratch_dir: str = typer.Option(
        "",
        "--scratch-dir",
        help="Directory for temporary scripts, logs, debug output, and candidate patches.",
    ),
    timeout: float | None = typer.Option(
        None, "--timeout", "-T", help="Total agent timeout in seconds (0=unlimited)"
    ),
    max_iterations: int | None = typer.Option(
        None,
        "--max-iterations",
        min=0,
        help="Maximum agent model/tool loop iterations (0=unlimited)",
    ),
    iteration_timeout_seconds: float | None = typer.Option(
        None,
        "--iteration-timeout-seconds",
        help="Per-iteration timeout in seconds (one LLM call + its tool executions)",
    ),
    tool_timeout_seconds: float | None = typer.Option(
        None,
        "--tool-timeout-seconds",
        help="Per-tool execution timeout in seconds",
    ),
    request_timeout_seconds: float | None = typer.Option(
        None,
        "--request-timeout-seconds",
        help="Single LLM HTTP/streaming request timeout in seconds",
    ),
    max_provider_retries: int | None = typer.Option(
        None,
        "--max-provider-retries",
        min=0,
        help="Maximum provider-level retries for transient errors",
    ),
    length_capped_continuations: int | None = typer.Option(
        None,
        "--length-capped-continuations",
        min=1,
        help="Maximum automatic continuations after provider output reaches its length limit",
    ),
    thinking: str = typer.Option(
        "",
        "--thinking",
        help="Thinking level override: off|minimal|low|medium|high|xhigh|adaptive",
    ),
    transcript_path: str = typer.Option(
        "", "--transcript-path", help="Write benchmark-compatible JSONL transcript"
    ),
    usage_path: str = typer.Option("", "--usage-path", help="Write usage JSON to this file"),
    session_db_path: str = typer.Option(
        ":memory:",
        "--session-db-path",
        help="Persistent session SQLite path for cross-invocation replay",
    ),
    no_memory_capture: bool = typer.Option(
        False,
        "--no-memory-capture",
        help="Do not write this invocation to durable searchable memory",
    ),
    file_paths: list[str] = typer.Option(
        [],
        "--file",
        "-f",
        help="Attach a local file; repeat for multiple files",
    ),
    unattended: bool = typer.Option(
        True,
        "--unattended/--interactive",
        help=(
            "Run without a live approval surface. Unattended is the default for "
            "single-shot automation."
        ),
    ),
    stateless: bool = typer.Option(
        False,
        "--stateless/--no-stateless",
        help="Use clean-room prompt bootstrap; does not change --unattended semantics.",
    ),
    clean_room: bool = typer.Option(
        False,
        "--clean-room",
        help="Alias for --stateless.",
    ),
    stateless_keep_project_rules: bool = typer.Option(
        False,
        "--stateless-keep-project-rules",
        help="With clean-room bootstrap, keep AGENTS.md project rules only.",
    ),
    permissions: str | None = typer.Option(
        None,
        "--permissions",
        help=(
            "Permission profile for single-shot runs: restricted, bypass, or full. "
            "Defaults to AGENTOS_AGENT_PERMISSIONS, then permissions.default_mode."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Run a single agent turn for automation."""
    run_agent_command(
        message=message,
        agent_id=agent_id,
        session_id=session_id,
        model=model,
        workspace=workspace,
        workspace_strict=workspace_strict,
        workspace_lockdown=workspace_lockdown,
        scratch_dir=scratch_dir,
        thinking=thinking,
        timeout=timeout,
        max_iterations=max_iterations,
        iteration_timeout_seconds=iteration_timeout_seconds,
        tool_timeout_seconds=tool_timeout_seconds,
        request_timeout_seconds=request_timeout_seconds,
        max_provider_retries=max_provider_retries,
        length_capped_continuations=length_capped_continuations,
        transcript_path=transcript_path,
        usage_path=usage_path,
        session_db_path=session_db_path,
        no_memory_capture=no_memory_capture,
        file_paths=file_paths,
        unattended=unattended,
        stateless=stateless,
        clean_room=clean_room,
        stateless_keep_project_rules=stateless_keep_project_rules,
        permissions=permissions,
        json_output=json_output,
    )


@app.command("chat")
def chat(
    model: str = typer.Option("", "--model", "-m", help="Model override"),
    session_id: str = typer.Option("", "--session", "-s", help="Resume session"),
    standalone: bool = typer.Option(False, "--standalone", help="Direct Agent without gateway"),
    workspace: str = typer.Option("", "--workspace", help="Workspace root for standalone tools"),
    workspace_strict: bool | None = typer.Option(
        None,
        "--workspace-strict/--no-workspace-strict",
        help="Restrict read-side file tools to --workspace in standalone mode",
    ),
    timeout: float | None = typer.Option(
        None, "--timeout", "-T", help="Total agent timeout in seconds (0=unlimited)"
    ),
) -> None:
    """Start interactive chat mode."""
    from agentos.cli.chat_cmd import run_chat

    run_chat(
        model=model,
        session_id=session_id,
        standalone=standalone,
        workspace=workspace,
        workspace_strict=workspace_strict,
        timeout=timeout,
    )


@app.command("reset")
def reset_cmd(
    key: str = typer.Option(..., "--key", help="Session key to reset."),
    gateway_url: str = typer.Option(
        "http://localhost:18791", "--gateway", envvar="AGENTOS_GATEWAY_URL"
    ),
) -> None:
    """Reset a session, rotating it to a fresh transcript.

    Exit codes: 0 on success, 1 when the gateway refuses the reset
    (session preserved).
    """
    import asyncio

    from agentos.cli.gateway_client import GatewayClient, GatewayRPCError
    from agentos.cli.url_utils import normalize_gateway_url

    async def _go():
        client = GatewayClient()
        try:
            await client.connect(normalize_gateway_url(gateway_url))
            return await client.reset_session(key)
        finally:
            await client.close()

    try:
        result = asyncio.run(_go())
    except GatewayRPCError as exc:
        data = exc.data or {}
        typer.secho(f"\u2717 Reset aborted: {exc.message}", fg=typer.colors.RED)
        typer.echo(f"  Session preserved: {data.get('session_id', '?')}")
        raise typer.Exit(1)

    payload = result
    typer.secho(
        f"\u2713 Session reset ({payload.get('previous_session_id', '?')} \u2192 "
        f"{payload.get('session_id', '?')}).",
        fg=typer.colors.GREEN,
    )
    if payload.get("reset_mode") == "archive_only":
        typer.echo(f"  Archived {payload.get('message_count', 0)} message(s) before rotating.")


if __name__ == "__main__":
    app()
