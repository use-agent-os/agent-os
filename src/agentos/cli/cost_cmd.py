"""Usage/cost CLI commands."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, cast

import typer
from rich.table import Table

from agentos.cli.gateway_rpc import run_gateway_sync
from agentos.cli.output import print_json
from agentos.cli.ui import ACCENT_HEADER, console

app = typer.Typer(help="Inspect usage and estimated cost.")


@app.callback(invoke_without_command=True)
def cost(
    by_model: bool = typer.Option(False, "--by-model", help="Group aggregate rows by model"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    csv_output: bool = typer.Option(False, "--csv", help="Emit machine-readable CSV"),
    start_date: str = typer.Option(None, "--start-date", help="Filter by start date (YYYY-MM-DD)"),
    end_date: str = typer.Option(None, "--end-date", help="Filter by end date (YYYY-MM-DD)"),
    agent_id: str = typer.Option(None, "--agent-id", help="Filter by agent ID"),
    channel_type: str = typer.Option(None, "--channel-type", help="Filter by channel type"),
    tool_name: str = typer.Option(None, "--tool-name", help="Filter by tool name"),
    skill: str = typer.Option(None, "--skill", help="Filter by skill name"),
    export_path: str = typer.Option(None, "--export", help="Path to export results (JSON/CSV)"),
) -> None:
    """Show aggregate usage/cost from the running gateway."""

    params = {}
    if start_date:
        params["startDate"] = start_date
    if end_date:
        params["endDate"] = end_date
    if agent_id:
        params["agentId"] = agent_id
    if channel_type:
        params["channelType"] = channel_type
    if tool_name:
        params["toolName"] = tool_name
    if skill:
        params["skill"] = skill

    async def _run(client) -> dict[Any, Any]:
        return cast(dict[Any, Any], await client.usage_cost(params))

    payload = run_gateway_sync(_run, json_output=(json_output or csv_output or bool(export_path)))

    rows = payload.get("breakdown", [])

    if export_path:
        path = Path(export_path)
        is_csv = csv_output or path.suffix.lower() == ".csv"
        if is_csv:
            import io

            f = io.StringIO()
            writer = csv.writer(f)
            writer.writerow(
                [
                    "Session",
                    "Model",
                    "Provider",
                    "Agent",
                    "Channel",
                    "Tool",
                    "Skill",
                    "Input",
                    "Output",
                    "Cost",
                    "CreatedAt",
                ]
            )
            for row in rows:
                writer.writerow(
                    [
                        row.get("session") or row.get("sessionKey") or "",
                        row.get("model") or "",
                        row.get("provider") or "",
                        row.get("agent_id") or row.get("agentId") or "",
                        row.get("channel")
                        or row.get("channel_type")
                        or row.get("channelType")
                        or "",
                        row.get("tool_name") or row.get("toolName") or "",
                        row.get("skill") or "",
                        int(row.get("input_tokens") or row.get("inputTokens") or 0),
                        int(row.get("output_tokens") or row.get("outputTokens") or 0),
                        float(row.get("cost_usd") or row.get("costUsd") or 0.0),
                        int(row.get("created_at") or row.get("createdAt") or 0),
                    ]
                )
            path.write_text(f.getvalue(), encoding="utf-8")
        else:
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        console.print(f"Exported usage data to {export_path}")
        return

    if csv_output:
        import io

        f = io.StringIO()
        writer = csv.writer(f)
        writer.writerow(
            [
                "Session",
                "Model",
                "Provider",
                "Agent",
                "Channel",
                "Tool",
                "Skill",
                "Input",
                "Output",
                "Cost",
                "CreatedAt",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.get("session") or row.get("sessionKey") or "",
                    row.get("model") or "",
                    row.get("provider") or "",
                    row.get("agent_id") or row.get("agentId") or "",
                    row.get("channel") or row.get("channel_type") or row.get("channelType") or "",
                    row.get("tool_name") or row.get("toolName") or "",
                    row.get("skill") or "",
                    int(row.get("input_tokens") or row.get("inputTokens") or 0),
                    int(row.get("output_tokens") or row.get("outputTokens") or 0),
                    float(row.get("cost_usd") or row.get("costUsd") or 0.0),
                    int(row.get("created_at") or row.get("createdAt") or 0),
                ]
            )
        console.print(f.getvalue().strip())
        return

    if by_model:
        grouped: dict[str, dict[str, float]] = defaultdict(
            lambda: {"input": 0, "output": 0, "cost": 0.0}
        )
        for row in rows:
            model = row.get("model") or "unknown"
            grouped[model]["input"] += int(row.get("input_tokens") or row.get("inputTokens") or 0)
            grouped[model]["output"] += int(
                row.get("output_tokens") or row.get("outputTokens") or 0
            )
            grouped[model]["cost"] += float(row.get("cost_usd") or row.get("costUsd") or 0.0)
        if json_output:
            print_json(
                {
                    "byModel": [
                        {
                            "model": model,
                            "inputTokens": int(data["input"]),
                            "outputTokens": int(data["output"]),
                            "costUsd": data["cost"],
                        }
                        for model, data in sorted(grouped.items())
                    ],
                    "totalCostUsd": payload.get("totalCostUsd"),
                }
            )
            return
        table = Table(title="Cost by Model", show_header=True, header_style=ACCENT_HEADER)
        table.add_column("Model")
        table.add_column("Input", justify="right")
        table.add_column("Output", justify="right")
        table.add_column("Cost", justify="right")
        for model, data in sorted(grouped.items()):
            table.add_row(
                model,
                f"{int(data['input']):,}",
                f"{int(data['output']):,}",
                f"${data['cost']:.6f}",
            )
        console.print(table)
        return

    if json_output:
        print_json(payload)
        return

    table = Table(title="Cost", show_header=True, header_style=ACCENT_HEADER)
    table.add_column("Session")
    table.add_column("Model")
    table.add_column("Agent")
    table.add_column("Channel")
    table.add_column("Tool")
    table.add_column("Skill")
    table.add_column("Input", justify="right")
    table.add_column("Output", justify="right")
    table.add_column("Cost", justify="right")
    for row in rows:
        table.add_row(
            str(row.get("session") or row.get("sessionKey") or ""),
            str(row.get("model") or ""),
            str(row.get("agent_id") or row.get("agentId") or ""),
            str(row.get("channel") or row.get("channel_type") or row.get("channelType") or ""),
            str(row.get("tool_name") or row.get("toolName") or ""),
            str(row.get("skill") or ""),
            f"{int(row.get('input_tokens') or row.get('inputTokens') or 0):,}",
            f"{int(row.get('output_tokens') or row.get('outputTokens') or 0):,}",
            f"${float(row.get('cost_usd') or row.get('costUsd') or 0.0):.6f}",
        )
    console.print(table)
    console.print(f"[dim]total: ${float(payload.get('totalCostUsd') or 0.0):.6f}[/dim]")
