"""CLI commands for environment variables.

Prefers the running gateway so a change applies to the live process instead of
only to the file. Falls back to writing the file directly when no gateway is
up, because the first thing a new install needs is a way to set a provider key
*before* anything can start.

The fallback is not a silent equivalence: it says the value will apply at next
start, so nobody is left wondering why the running agent still fails.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import typer

from agentos.cli.gateway_rpc import default_gateway_url, rpc_error_exit_code
from agentos.cli.output import emit_error, print_json
from agentos.cli.ui import console

env_app = typer.Typer(help="Environment variables - list, set, unset, reveal.")

_SOURCE_LABELS = {
    "process": "process env",
    "cwd_file": "project .env",
    "home_file": "AgentOS .env",
    "unset": "",
}


async def _try_gateway(method: str, params: dict[str, Any], *, json_output: bool) -> Any | None:
    """Call *method* on the gateway. Returns ``None`` when none is running.

    A connection failure is the one error worth handling locally — every other
    failure is a real answer from the gateway and should be reported as such,
    not silently retried against the file.
    """
    from agentos.cli import gateway_client as gateway_client_module

    client = gateway_client_module.GatewayClient()
    try:
        await client.connect(default_gateway_url())
    except (SystemExit, ConnectionError, OSError):
        await client.close()
        return None

    try:
        return await client.call(method, params)
    except gateway_client_module.GatewayRPCError as exc:
        emit_error(exc.message, json_output=json_output, code=exc.code, details=exc.data)
        raise typer.Exit(rpc_error_exit_code(exc.code)) from exc
    except (ConnectionError, OSError) as exc:
        emit_error(str(exc), json_output=json_output, code="GATEWAY_UNAVAILABLE")
        raise typer.Exit(1) from exc
    finally:
        await client.close()


def _offline_loader() -> Any:
    """Build a skill loader from config so offline listings still name owners."""
    import os

    from agentos.gateway.config import GatewayConfig
    from agentos.skills.loader import SkillLoader
    from agentos.skills.paths import resolve_skill_layer_dirs

    try:
        config = GatewayConfig.load(os.environ.get("AGENTOS_GATEWAY_CONFIG_PATH"))
        workspace_root = Path(config.workspace_dir) if config.workspace_dir else None
        workspace_override = (
            Path(config.skills.workspace_dir) if config.skills.workspace_dir else None
        )
        layer_dirs = resolve_skill_layer_dirs(
            allow_bundled=config.skills.allow_bundled,
            workspace_root=workspace_root,
            workspace_override=workspace_override,
            managed_override=config.skills.managed_dir,
            extra_dirs=[Path(d) for d in config.skills.extra_dirs],
        )
        return SkillLoader(
            bundled_dir=layer_dirs.bundled_dir,
            workspace_dir=layer_dirs.workspace_dir,
            managed_dir=layer_dirs.managed_dir,
            personal_agents_dir=layer_dirs.personal_agents_dir,
            project_agents_dir=layer_dirs.project_agents_dir,
            extra_dirs=layer_dirs.extra_dirs,
        )
    except Exception:
        # No usable config yet is the normal state before onboarding; the
        # listing degrades to provider entries rather than failing.
        return None


def _available_from(name: str, is_set: bool) -> dict[str, str] | None:
    """Mirror the gateway's availability field for the offline listing."""
    if is_set:
        return None
    from agentos import credential_sources

    source = credential_sources.available_for(name)
    return None if source is None else {"id": source.id, "label": source.label}


def _offline_payload() -> dict[str, Any]:
    """Produce the same shape as ``env.list`` without a gateway."""
    from agentos import env_catalog, env_store

    catalog = env_catalog.build_catalog(
        _offline_loader(), present_names=set(env_store.read_env_file())
    )
    rows = []
    for name, spec in sorted(catalog.items()):
        entry = env_store.resolve_entry(name, secret=spec.secret)
        rows.append(
            {
                "name": name,
                "isSet": entry.is_set,
                "source": entry.source,
                "masked": entry.masked,
                "secret": spec.secret,
                "description": spec.description,
                "url": spec.url,
                "category": spec.category,
                "owner": spec.owner,
                "required": spec.required,
                "writable": entry.writable,
                "restartRequired": spec.restart_required,
                "missing": spec.required and not entry.is_set,
                "availableFrom": _available_from(name, entry.is_set),
            }
        )
    return {
        "envFilePath": str(env_store.env_file_path()),
        "vars": rows,
        "setCount": sum(1 for r in rows if r["isSet"]),
        "totalCount": len(rows),
        "shadowedCount": sum(1 for r in rows if r["source"] == "process"),
    }


def _run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)


@env_app.command("list")
def env_list_cmd(
    missing: bool = typer.Option(False, "--missing", help="Only variables that are not set"),
    category: str = typer.Option("", "--category", help="Filter by category"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """List environment variables AgentOS knows about, without their values."""
    from rich.table import Table

    payload = _run(_try_gateway("env.list", {}, json_output=json_output))
    if payload is None:
        payload = _offline_payload()

    rows = payload.get("vars") or []
    if missing:
        rows = [r for r in rows if not r.get("isSet")]
    if category:
        rows = [r for r in rows if r.get("category") == category.strip().lower()]

    if json_output:
        print_json({**payload, "vars": rows})
        return

    table = Table(show_header=True, title=f"Environment — {payload.get('envFilePath', '')}")
    table.add_column("Name")
    table.add_column("Status")
    table.add_column("Source")
    table.add_column("Value")
    table.add_column("Category")
    table.add_column("Needed by")
    for row in rows:
        status = "set" if row.get("isSet") else ("MISSING" if row.get("missing") else "unset")
        if not row.get("writable"):
            status = f"{status} (locked)"
        available = row.get("availableFrom")
        if available:
            status = f"{status} · in {available.get('label', available.get('id'))}"
        table.add_row(
            str(row.get("name", "")),
            status,
            _SOURCE_LABELS.get(str(row.get("source", "")), str(row.get("source", ""))),
            str(row.get("masked") or ""),
            str(row.get("category", "")),
            str(row.get("owner") or ""),
        )
    console.print(table)

    shadowed = int(payload.get("shadowedCount") or 0)
    if shadowed:
        # Without this line the operator edits the file, sees no change, and
        # has no way to find out why.
        console.print(
            f"[yellow]{shadowed} variable(s) are shadowed by the process environment. "
            "Editing the file will not change them until the export is removed "
            "and the gateway restarts.[/yellow]"
        )


@env_app.command("get")
def env_get_cmd(
    name: str = typer.Argument(..., help="Variable name"),
    reveal: bool = typer.Option(False, "--reveal", help="Print the real value"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the reveal confirmation"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show one variable's state, or its value with --reveal."""
    if reveal:
        if not yes and not json_output:
            typer.confirm(f"Print the real value of {name} to this terminal?", abort=True)
        payload = _run(_try_gateway("env.reveal", {"name": name}, json_output=json_output))
        if payload is None:
            from agentos import env_store

            value = env_store.get_env_value(name)
            if value is None:
                emit_error(f"Environment variable is not set: {name}", json_output=json_output)
                raise typer.Exit(1)
            payload = {"name": name, "value": value}
        if json_output:
            print_json(payload)
        else:
            console.print(payload["value"])
        return

    listing = _run(_try_gateway("env.list", {}, json_output=json_output))
    if listing is None:
        listing = _offline_payload()
    row = next((r for r in listing.get("vars") or [] if r.get("name") == name), None)
    if row is None:
        emit_error(f"Unknown environment variable: {name}", json_output=json_output)
        raise typer.Exit(1)
    if json_output:
        print_json(row)
        return
    console.print(f"{row['name']}: {'set' if row['isSet'] else 'not set'}")
    if row.get("description"):
        console.print(row["description"])
    if row.get("masked"):
        console.print(f"value: {row['masked']}")
    if row.get("url"):
        console.print(f"obtain: {row['url']}")


@env_app.command("import")
def env_import_cmd(
    name: str = typer.Argument(..., help="Variable name"),
    source: str = typer.Option("", "--source", help="Source id (default: the usable one)"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Copy a credential in from a tool that already holds it.

    Nothing is imported without this command being run: a token you granted to
    the GitHub CLI is not automatically something an agent should get.
    """
    from agentos import credential_sources

    source_id = source.strip()
    if not source_id:
        found = credential_sources.available_for(name, refresh=True)
        if found is None:
            candidates = credential_sources.sources_for(name)
            if not candidates:
                emit_error(f"No known source supplies {name}.", json_output=json_output)
            else:
                emit_error(
                    f"No usable source for {name}. " + " ".join(c.hint for c in candidates),
                    json_output=json_output,
                )
            raise typer.Exit(1)
        source_id = found.id

    payload = _run(
        _try_gateway("env.import", {"name": name, "sourceId": source_id}, json_output=json_output)
    )
    applied_live = payload is not None
    if payload is None:
        from agentos import env_store
        from agentos.env_policy import EnvPolicyError

        try:
            value = credential_sources.read_from(name, source_id)
            entry = env_store.set_env_var(name, value)
        except (EnvPolicyError, LookupError, RuntimeError) as exc:
            emit_error(str(exc), json_output=json_output)
            raise typer.Exit(2) from exc
        payload = {"name": entry.name, "isSet": entry.is_set, "importedFrom": source_id}

    if json_output:
        print_json({**payload, "appliedLive": applied_live})
        return

    console.print(f"Imported {name} from {source_id}.")
    console.print(
        "[yellow]This is a copy — it will not update when that source rotates "
        "its credential. Re-import to refresh.[/yellow]"
    )
    if not applied_live:
        console.print(
            "[yellow]No gateway is running — the value applies the next time "
            "AgentOS starts.[/yellow]"
        )


@env_app.command("set")
def env_set_cmd(
    name: str = typer.Argument(..., help="Variable name"),
    value: str = typer.Option("", "--value", help="Value (visible in shell history)"),
    stdin: bool = typer.Option(False, "--stdin", help="Read the value from stdin"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Set an environment variable in ~/.agentos/.env.

    Prefer ``--stdin`` or the interactive prompt for credentials: a value
    passed with ``--value`` lands in shell history and in the process list.
    """
    if stdin:
        resolved = sys.stdin.read().rstrip("\n")
    elif value:
        resolved = value
    elif json_output:
        emit_error("--value or --stdin is required with --json", json_output=True)
        raise typer.Exit(2)
    else:
        resolved = typer.prompt(f"Value for {name}", hide_input=True)

    payload = _run(
        _try_gateway("env.set", {"name": name, "value": resolved}, json_output=json_output)
    )
    applied_live = payload is not None
    if payload is None:
        from agentos import env_store
        from agentos.env_policy import EnvPolicyError

        try:
            entry = env_store.set_env_var(name, resolved)
        except EnvPolicyError as exc:
            emit_error(str(exc), json_output=json_output)
            raise typer.Exit(2) from exc
        payload = {"name": entry.name, "isSet": entry.is_set, "masked": entry.masked}

    if json_output:
        print_json({**payload, "appliedLive": applied_live})
        return

    console.print(f"Set {name}.")
    if not applied_live:
        console.print(
            "[yellow]No gateway is running — the value applies the next time "
            "AgentOS starts.[/yellow]"
        )
    elif payload.get("restartRequired"):
        console.print(
            "[yellow]Restart the gateway for this to take effect: the current "
            "process built its client with the previous value.[/yellow]"
        )


@env_app.command("unset")
def env_unset_cmd(
    name: str = typer.Argument(..., help="Variable name"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Remove an environment variable from ~/.agentos/.env."""
    if not yes and not json_output:
        typer.confirm(f"Remove {name} from the AgentOS .env?", abort=True)

    payload = _run(_try_gateway("env.unset", {"name": name}, json_output=json_output))
    if payload is None:
        from agentos import env_store
        from agentos.env_policy import EnvPolicyError

        try:
            removed = env_store.unset_env_var(name)
        except EnvPolicyError as exc:
            emit_error(str(exc), json_output=json_output)
            raise typer.Exit(2) from exc
        payload = {"name": name, "removed": removed}

    if json_output:
        print_json(payload)
        return
    console.print(f"Removed {name}." if payload.get("removed") else f"{name} was not set.")
