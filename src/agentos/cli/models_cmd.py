"""Model catalog CLI commands."""

from __future__ import annotations

from typing import Any, cast

import typer
from rich.table import Table

from agentos.cli.gateway_rpc import run_gateway_sync
from agentos.cli.output import print_json
from agentos.cli.ui import ACCENT_HEADER, console

app = typer.Typer(help="Inspect available models.")


@app.command("list")
def models_list(
    provider: str | None = typer.Option(None, "--provider", help="Provider filter"),
    capability: list[str] | None = typer.Option(
        None, "--capability", "-c", help="Required capability"
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """List available models from the running gateway."""

    async def _with_client(client) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]],
            await client.list_models(provider=provider, capabilities=capability),
        )

    rows = run_gateway_sync(_with_client, json_output=json_output)
    if json_output:
        print_json(rows)
        return

    table = Table(title="Models", show_header=True, header_style=ACCENT_HEADER)
    table.add_column("ID")
    table.add_column("Provider")
    table.add_column("Context", justify="right")
    table.add_column("Capabilities")
    table.add_column("Input/1k", justify="right")
    table.add_column("Output/1k", justify="right")
    for row in rows:
        pricing = row.get("pricing") or {}
        table.add_row(
            str(row.get("id") or ""),
            str(row.get("provider") or ""),
            str(row.get("contextWindow") or ""),
            ", ".join(str(v) for v in row.get("capabilities") or []),
            str(pricing.get("inputPer1k") or ""),
            str(pricing.get("outputPer1k") or ""),
        )
    console.print(table)
