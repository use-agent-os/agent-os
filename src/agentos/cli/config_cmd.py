"""Config command — get/set configuration values."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

import typer
from rich.markup import escape
from rich.table import Table

from agentos.cli.ui import ACCENT_HEADER, ACCENT_MARKUP, console

app = typer.Typer(help="Manage AgentOS configuration.")


@app.command("get")
def config_get(
    key: str = typer.Argument("", help="Config key to get (empty = show all)"),
    config_path: Path | None = typer.Option(None, "--config", help="Override config path."),
) -> None:
    """Get a configuration value."""
    from agentos.gateway.config import GatewayConfig

    cfg = GatewayConfig.load(config_path or os.environ.get("AGENTOS_GATEWAY_CONFIG_PATH"))
    data = cfg.to_public_dict()

    if key:
        # Support dot-notation: auth.mode
        val = _get_key(data, key)
        if val is _MISSING:
            console.print(f"[red]Key not found: {key}[/red]")
            raise typer.Exit(1)
        console.print(f"[{ACCENT_MARKUP}]{escape(key)}[/] = [green]{escape(repr(val))}[/green]")
    else:
        table = Table(title="Gateway Config", show_header=True, header_style=ACCENT_HEADER)
        table.add_column("Key")
        table.add_column("Value")
        _add_flat(table, data)
        console.print(table)


_MISSING = object()


def _get_key(data: dict[str, Any], key: str) -> Any:
    val: Any = data
    for part in key.split("."):
        if isinstance(val, dict) and part in val:
            val = val[part]
        else:
            return _MISSING
    return val


@app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Config key (dot-notation)"),
    value: str = typer.Argument(..., help="Value to set"),
    config_path: Path | None = typer.Option(None, "--config", help="Persist to config path."),
) -> None:
    """Set a configuration value (env-var backed, prints export command)."""
    if config_path is not None:
        from agentos.gateway.config import GatewayConfig
        from agentos.onboarding.config_store import load_config, persist_config

        cfg = load_config(config_path)
        data = cfg.to_toml_dict()
        if not _set_key(data, key, _parse_config_value(value)):
            console.print(f"[red]Key not found: {escape(key)}[/red]")
            raise typer.Exit(1)
        try:
            updated = GatewayConfig.model_validate(data)
        except Exception as exc:  # noqa: BLE001 - show config validation errors as CLI input errors.
            console.print(f"[red]Invalid value for {escape(key)}:[/red] {escape(str(exc))}")
            raise typer.Exit(2) from exc
        persist = persist_config(updated, path=config_path, restart_required=True)
        console.print(f"[{ACCENT_MARKUP}]Config:[/] {persist.path}")
        if persist.backup_path:
            console.print(f"[dim]Backup:[/dim] {persist.backup_path}")
        console.print("[yellow]Restart the gateway to apply this setting.[/yellow]")
        return

    from agentos.gateway.config import GatewayConfig

    data = GatewayConfig().to_toml_dict()
    parts = key.split(".")
    skill_config_map = len(parts) >= 3 and parts[0] == "skills" and parts[1] == "config"
    if skill_config_map or not (_get_key(data, key) is not _MISSING or _is_declared_key(key)):
        console.print(f"[red]Key not found: {escape(key)}[/red]")
        raise typer.Exit(1)

    env_key = "AGENTOS_GATEWAY_" + key.upper().replace(".", "__")
    console.print("[dim]To persist this setting, export:[/dim]")
    console.print(f"  [bold]export {env_key}={value}[/bold]")


def _parse_config_value(value: str) -> Any:
    try:
        return tomllib.loads(f"value = {value}\n")["value"]
    except tomllib.TOMLDecodeError:
        return value


def _is_declared_key(key: str) -> bool:
    """True when the config model declares *key*, whatever its current value.

    ``to_toml_dict()`` is ``model_dump(exclude_none=True)``, so a declared key
    that is currently null is missing from it and looks exactly like a typo.
    The model itself is the authority on which keys exist; asking it keeps a
    key the operator can read with ``config get`` writable with ``config set``.
    """
    from agentos.gateway.config import GatewayConfig

    node: Any = GatewayConfig().model_dump()
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def _set_key(data: dict[str, Any], key: str, value: Any) -> bool:
    """Set a dotted key on a TOML dict.

    Unknown keys are rejected so typos do not persist. ``skills.config.*`` is
    the documented free-form skill-settings map; ``to_toml_dict`` omits it when
    empty, so this path must create missing intermediate dicts — and so must a
    declared key whose value is currently null, which is absent from the TOML
    view for the same reason.
    """
    cursor: Any = data
    parts = key.split(".")
    skill_config_map = len(parts) >= 3 and parts[0] == "skills" and parts[1] == "config"
    create = skill_config_map or _is_declared_key(key)
    for part in parts[:-1]:
        if not isinstance(cursor, dict):
            return False
        if part not in cursor:
            if not create:
                return False
            cursor[part] = {}
        cursor = cursor[part]
    if not isinstance(cursor, dict):
        return False
    if parts[-1] not in cursor and not create:
        return False
    cursor[parts[-1]] = value
    return True


def _add_flat(table: Table, data: dict, prefix: str = "") -> None:
    for k, v in data.items():
        full_key = f"{prefix}{k}" if not prefix else f"{prefix}.{k}"
        if isinstance(v, dict):
            _add_flat(table, v, full_key)
        else:
            table.add_row(escape(full_key), escape(str(v)))
