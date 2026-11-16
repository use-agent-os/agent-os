"""``agentos mcp`` — manage the MCP servers AgentOS connects to as a client.

Every command except ``probe``/``doctor --probe`` reads or writes the config
file only; no gateway is needed. Writes go through the same
``load_config``/``persist_config`` path as ``agentos config set --config``: the
file is validated, backed up, and rewritten. ``mcp`` is an operator-only root,
so with a gateway running a change is held until ``agentos config reload``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
from rich.markup import escape
from rich.table import Table

from agentos.cli.output import emit_error, print_json
from agentos.cli.ui import ACCENT_HEADER, ACCENT_MARKUP, console

if TYPE_CHECKING:
    from agentos.mcp.probe import MCPProbeResult
    from agentos.mcp.types import MCPServerConfig

app = typer.Typer(help="Manage the MCP servers AgentOS connects to (MCP client).")

_TRANSPORTS = ("stdio", "sse", "streamable_http")

_CONFIG_OPTION = typer.Option(None, "--config", help="Config file (default: active config).")
_JSON_OPTION = typer.Option(False, "--json", help="Emit machine-readable JSON.")


def _fail(message: str, *, json_output: bool, code: str, exit_code: int) -> typer.Exit:
    emit_error(message, json_output=json_output, code=code)
    return typer.Exit(exit_code)


def _load(config_path: Path | None) -> tuple[Any, Path]:
    from agentos.onboarding.config_store import load_config, resolve_config_path

    target, _source = resolve_config_path(config_path)
    return load_config(target), target


def _find(cfg: Any, name: str) -> Any | None:
    return next((entry for entry in cfg.mcp.servers if entry.name == name), None)


def _require(cfg: Any, name: str, *, json_output: bool) -> Any:
    entry = _find(cfg, name)
    if entry is None:
        raise _fail(
            f"MCP server not found: {name}",
            json_output=json_output,
            code="NOT_FOUND",
            exit_code=1,
        )
    return entry


def _runtime(cfg: Any, entry: Any) -> MCPServerConfig:
    from agentos.mcp.types import MCPServerConfig

    return MCPServerConfig.from_entry(entry, state_dir=cfg.state_dir, defaults=cfg.mcp)


def _server_row(entry: Any) -> dict[str, Any]:
    return {
        "name": entry.name,
        "enabled": entry.enabled,
        "transport": entry.transport,
        "command": entry.command,
        "args": list(entry.args),
        "cwd": entry.cwd,
        "url": entry.url,
        "oauth": entry.oauth,
        "envKeys": sorted(entry.env),
        "headerKeys": sorted(entry.headers),
        "toolFilter": {
            "include": list(entry.tool_filter.include),
            "exclude": list(entry.tool_filter.exclude),
        },
        "toolTimeoutSeconds": entry.tool_timeout_seconds,
        "connectTimeoutSeconds": entry.connect_timeout_seconds,
    }


def _target_text(row: dict[str, Any]) -> str:
    if row["transport"] == "stdio":
        return " ".join([row["command"] or "", *row["args"]]).strip()
    return row["url"] or ""


def _filter_text(row: dict[str, Any]) -> str:
    parts = []
    if row["toolFilter"]["include"]:
        parts.append("+" + ",".join(row["toolFilter"]["include"]))
    if row["toolFilter"]["exclude"]:
        parts.append("-" + ",".join(row["toolFilter"]["exclude"]))
    return " ".join(parts) or "all"


def _print_servers(cfg: Any, target: Path, *, json_output: bool, summary: bool) -> None:
    rows = [_server_row(entry) for entry in cfg.mcp.servers]
    enabled_count = sum(1 for row in rows if row["enabled"])
    if json_output:
        payload: dict[str, Any] = {
            "configPath": str(target),
            "enabled": cfg.mcp.enabled,
            "connectTimeoutSeconds": cfg.mcp.connect_timeout_seconds,
            "servers": rows,
        }
        if summary:
            payload["counts"] = {
                "total": len(rows),
                "enabled": enabled_count,
                "disabled": len(rows) - enabled_count,
            }
        print_json(payload)
        return
    console.print(f"[{ACCENT_MARKUP}]Config:[/] {escape(str(target))}")
    state = "on" if cfg.mcp.enabled else "[yellow]off[/yellow] (mcp.enabled = false)"
    console.print(f"[{ACCENT_MARKUP}]MCP client:[/] {state}")
    if summary:
        console.print(
            f"[{ACCENT_MARKUP}]Servers:[/] {len(rows)} configured, {enabled_count} enabled, "
            f"{len(rows) - enabled_count} disabled"
        )
    if not rows:
        console.print("No MCP servers configured. Add one with `agentos mcp add <name>`.")
        return
    table = Table(show_header=True, header_style=ACCENT_HEADER)
    table.add_column("Name")
    table.add_column("Enabled")
    table.add_column("Transport")
    table.add_column("Command / URL")
    table.add_column("Tools")
    for row in rows:
        table.add_row(
            escape(row["name"]),
            "yes" if row["enabled"] else "no",
            row["transport"],
            escape(_target_text(row)),
            escape(_filter_text(row)),
        )
    console.print(table)


@app.command("list")
def mcp_list(
    config_path: Path | None = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """List configured MCP servers (from the config file; no gateway needed)."""
    cfg, target = _load(config_path)
    _print_servers(cfg, target, json_output=json_output, summary=False)


@app.command("status")
def mcp_status(
    config_path: Path | None = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Summarize the MCP client config: runtime switch, counts, and each server."""
    cfg, target = _load(config_path)
    _print_servers(cfg, target, json_output=json_output, summary=True)


def _split_patterns(values: list[str]) -> list[str]:
    """``--include 'search,read_*' --include x`` -> ``['search', 'read_*', 'x']``."""
    return [part.strip() for value in values for part in value.split(",") if part.strip()]


def _parse_pairs(values: list[str], flag: str, *, json_output: bool) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for value in values:
        key, sep, val = value.partition("=")
        if not sep or not key.strip():
            raise _fail(
                f"{flag} expects KEY=VALUE, got {value!r}",
                json_output=json_output,
                code="INVALID_ARGUMENT",
                exit_code=2,
            )
        pairs[key.strip()] = val
    return pairs


def _persist(cfg: Any, target: Path, *, json_output: bool, name: str, action: str) -> None:
    from agentos.onboarding.config_store import persist_config

    try:
        persist = persist_config(cfg, path=target, restart_required=True)
    except ValueError as exc:
        raise _fail(
            f"Invalid MCP server definition: {exc}",
            json_output=json_output,
            code="INVALID_ARGUMENT",
            exit_code=2,
        ) from exc
    if json_output:
        print_json(
            {
                "name": name,
                "action": action,
                "configPath": str(persist.path),
                "backupPath": str(persist.backup_path) if persist.backup_path else None,
                "held": list(persist.held),
                "mcpEnabled": cfg.mcp.enabled,
                "restartRequired": True,
            }
        )
        return
    console.print(f"MCP server {action}: [bold]{escape(name)}[/bold]")
    console.print(f"[{ACCENT_MARKUP}]Config:[/] {escape(str(persist.path))}")
    if persist.backup_path:
        console.print(f"[dim]Backup:[/dim] {escape(str(persist.backup_path))}")
    for warning in persist.warnings:
        console.print(f"[yellow]{escape(warning)}[/yellow]", soft_wrap=True)
    if not cfg.mcp.enabled:
        console.print(
            "[yellow]mcp.enabled is false: no server connects until you run "
            "`agentos config set mcp.enabled true --config <path>`.[/yellow]",
            soft_wrap=True,
        )
    console.print("[yellow]Restart the gateway to apply this change.[/yellow]")


@app.command("add")
def mcp_add(
    name: str = typer.Argument(..., help="Server name (unique)."),
    command: str | None = typer.Option(None, "--command", help="Executable for a stdio server."),
    args: list[str] = typer.Option([], "--arg", help="Argument for --command (repeatable)."),
    cwd: str | None = typer.Option(None, "--cwd", help="Working directory for a stdio server."),
    env: list[str] = typer.Option([], "--env", help="KEY=VALUE for a stdio server (repeatable)."),
    url: str | None = typer.Option(None, "--url", help="URL of an SSE / Streamable HTTP server."),
    transport: str | None = typer.Option(
        None,
        "--transport",
        help="stdio | sse | streamable_http (default: stdio with --command, "
        "streamable_http with --url).",
    ),
    headers: list[str] = typer.Option([], "--header", help="KEY=VALUE HTTP header (repeatable)."),
    oauth: bool = typer.Option(False, "--oauth", help="Streamable HTTP server uses OAuth."),
    include: list[str] = typer.Option(
        [], "--include", help="Tool glob to register (repeatable or comma-separated)."
    ),
    exclude: list[str] = typer.Option(
        [], "--exclude", help="Tool glob to skip (repeatable or comma-separated)."
    ),
    tool_timeout: float | None = typer.Option(
        None, "--tool-timeout", help="Seconds per tool call (default 30)."
    ),
    connect_timeout: float | None = typer.Option(
        None, "--connect-timeout", help="Seconds to connect (default: mcp.connect_timeout_seconds)."
    ),
    disabled: bool = typer.Option(False, "--disabled", help="Save the server disabled."),
    replace: bool = typer.Option(False, "--replace", help="Overwrite a server with this name."),
    config_path: Path | None = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Add (or with --replace, overwrite) an MCP server definition."""
    from agentos.gateway.config import MCPServerEntry

    def bad(message: str) -> typer.Exit:
        return _fail(message, json_output=json_output, code="INVALID_ARGUMENT", exit_code=2)

    name = name.strip()
    if not name:
        raise bad("Server name must not be empty.")
    if bool(command) == bool(url):
        raise bad("Give exactly one of --command (stdio) or --url (sse / streamable_http).")
    chosen = transport or ("stdio" if command else "streamable_http")
    if chosen not in _TRANSPORTS:
        raise bad(f"--transport must be one of {', '.join(_TRANSPORTS)}.")
    if chosen == "stdio":
        if not command:
            raise bad("A stdio server needs --command.")
        if headers or oauth:
            raise bad("--header and --oauth apply to HTTP servers, not stdio.")
    else:
        if not url:
            raise bad(f"A {chosen} server needs --url.")
        if args or cwd or env:
            raise bad("--arg, --cwd and --env apply to stdio servers only.")
        if oauth and chosen != "streamable_http":
            raise bad("--oauth needs --transport streamable_http.")

    payload: dict[str, Any] = {
        "name": name,
        "enabled": not disabled,
        "transport": chosen,
        "command": command,
        "args": list(args),
        "cwd": cwd,
        "url": url,
        "env": _parse_pairs(env, "--env", json_output=json_output),
        "headers": _parse_pairs(headers, "--header", json_output=json_output),
        "oauth": oauth,
        "tool_filter": {"include": _split_patterns(include), "exclude": _split_patterns(exclude)},
    }
    if tool_timeout is not None:
        payload["tool_timeout_seconds"] = tool_timeout
    if connect_timeout is not None:
        payload["connect_timeout_seconds"] = connect_timeout
    try:
        entry = MCPServerEntry.model_validate(payload)
    except ValueError as exc:
        raise bad(f"Invalid MCP server definition: {exc}") from exc

    cfg, target = _load(config_path)
    existing = _find(cfg, name)
    if existing is not None and not replace:
        raise _fail(
            f"MCP server already exists: {name} (use --replace to overwrite)",
            json_output=json_output,
            code="ALREADY_EXISTS",
            exit_code=1,
        )
    servers = [server for server in cfg.mcp.servers if server.name != name]
    # Same rule as `agentos migrate`: the first server switches the MCP client
    # on, but an explicit `mcp.enabled = false` next to existing servers stays.
    if not cfg.mcp.enabled and not servers:
        cfg.mcp.enabled = True
    if existing is not None:
        index = cfg.mcp.servers.index(existing)
        servers.insert(index, entry)
    else:
        servers.append(entry)
    cfg.mcp.servers = servers
    _persist(
        cfg,
        target,
        json_output=json_output,
        name=name,
        action="replaced" if existing is not None else "added",
    )


@app.command("remove")
def mcp_remove(
    name: str = typer.Argument(..., help="Server name."),
    config_path: Path | None = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Remove an MCP server definition."""
    cfg, target = _load(config_path)
    _require(cfg, name, json_output=json_output)
    cfg.mcp.servers = [server for server in cfg.mcp.servers if server.name != name]
    _persist(cfg, target, json_output=json_output, name=name, action="removed")


def _set_enabled(name: str, enabled: bool, config_path: Path | None, json_output: bool) -> None:
    cfg, target = _load(config_path)
    entry = _require(cfg, name, json_output=json_output)
    entry.enabled = enabled
    _persist(
        cfg,
        target,
        json_output=json_output,
        name=name,
        action="enabled" if enabled else "disabled",
    )


@app.command("enable")
def mcp_enable(
    name: str = typer.Argument(..., help="Server name."),
    config_path: Path | None = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Enable an MCP server so the gateway connects it."""
    _set_enabled(name, True, config_path, json_output)


@app.command("disable")
def mcp_disable(
    name: str = typer.Argument(..., help="Server name."),
    config_path: Path | None = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Disable an MCP server: keep the definition, never connect it."""
    _set_enabled(name, False, config_path, json_output)


def _probe_timeout(cfg: Any, runtime: MCPServerConfig, override: float | None) -> float:
    if override is not None:
        return override
    connect = runtime.effective_connect_timeout(float(cfg.mcp.connect_timeout_seconds))
    # Listing tools is part of the probe; give it at least the tool budget.
    return max(connect, runtime.tool_timeout_seconds)


def _print_probe(result: MCPProbeResult) -> None:
    if not result.ok:
        console.print(
            f"[red]✗[/red] {escape(result.name)}: {escape(result.error or 'failed')}",
            soft_wrap=True,
        )
        return
    console.print(
        f"[green]✓[/green] {escape(result.name)} ({result.transport}) answered in "
        f"{result.elapsed_ms} ms with {len(result.tools)} tool(s), "
        f"{len(result.registered)} registered after the filter."
    )
    if not result.tools:
        return
    registered = set(result.registered)
    table = Table(show_header=True, header_style=ACCENT_HEADER)
    table.add_column("Tool")
    table.add_column("Registered")
    table.add_column("Description")
    for tool in result.tools:
        table.add_row(
            escape(tool.name),
            "yes" if tool.name in registered else "[dim]filtered[/dim]",
            escape((tool.description or "").splitlines()[0] if tool.description else ""),
        )
    console.print(table)


@app.command("probe")
def mcp_probe(
    name: str = typer.Argument(..., help="Server name."),
    timeout: float | None = typer.Option(
        None, "--timeout", help="Seconds to connect and list tools (default: the server's)."
    ),
    config_path: Path | None = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Connect to a server, list its tools, and disconnect. Works without a gateway.

    A stdio server is spawned by this process, as a child of your shell.
    Exits 1 when the server cannot be reached.
    """
    from agentos.mcp.probe import probe_server

    cfg, _target = _load(config_path)
    runtime = _runtime(cfg, _require(cfg, name, json_output=json_output))
    result = asyncio.run(probe_server(runtime, timeout=_probe_timeout(cfg, runtime, timeout)))
    if json_output:
        print_json(result.to_dict())
    else:
        _print_probe(result)
    if not result.ok:
        raise typer.Exit(1)


@app.command("doctor")
def mcp_doctor(
    name: str = typer.Argument(..., help="Server name."),
    probe: bool = typer.Option(False, "--probe", help="Also connect and list tools."),
    timeout: float | None = typer.Option(None, "--timeout", help="Probe timeout in seconds."),
    config_path: Path | None = _CONFIG_OPTION,
    json_output: bool = _JSON_OPTION,
) -> None:
    """Validate a server definition; with --probe, then connect and check the tool filter.

    Exits 1 when any check is an error or the probe fails.
    """
    from agentos.mcp.probe import filter_checks, probe_server, validate_server

    cfg, _target = _load(config_path)
    runtime = _runtime(cfg, _require(cfg, name, json_output=json_output))
    checks = validate_server(runtime, runtime_enabled=cfg.mcp.enabled)
    has_error = any(check.level == "error" for check in checks)
    probe_result = None
    if probe and not has_error:
        probe_result = asyncio.run(
            probe_server(runtime, timeout=_probe_timeout(cfg, runtime, timeout))
        )
        if probe_result.ok:
            checks.extend(filter_checks(runtime, probe_result.tools))
        else:
            has_error = True
    ok = not has_error
    if json_output:
        print_json(
            {
                "name": name,
                "ok": ok,
                "checks": [check.to_dict() for check in checks],
                "probe": probe_result.to_dict() if probe_result is not None else None,
            }
        )
    else:
        marks = {"ok": "[green]✓[/green]", "warn": "[yellow]![/yellow]", "error": "[red]✗[/red]"}
        for check in checks:
            console.print(
                f"{marks[check.level]} {escape(check.check)}: {escape(check.message)}",
                soft_wrap=True,
            )
        if probe and has_error and probe_result is None:
            console.print("[dim]Probe skipped: fix the errors above first.[/dim]")
        if probe_result is not None:
            _print_probe(probe_result)
    if not ok:
        raise typer.Exit(1)
