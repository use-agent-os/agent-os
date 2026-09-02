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
from agentos.observability.decision_log import _default_log_dir
from agentos.observability.savings_pdf import render_savings_pdf
from agentos.observability.savings_report import SavingsReport, build_savings_report

app = typer.Typer(help="Inspect usage and estimated cost.")


@app.callback(invoke_without_command=True)
def cost(
    ctx: typer.Context,
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

    # The group is invoke_without_command, so this callback also runs ahead of
    # every subcommand. Subcommands own their own output (and `savings` reads
    # the local decision log, with no gateway to talk to).
    if ctx.invoked_subcommand is not None:
        return

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
        path.parent.mkdir(parents=True, exist_ok=True)
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


@app.command("savings")
def savings(
    pdf: str = typer.Option(None, "--pdf", help="Write a branded PDF report to this path"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    csv_output: bool = typer.Option(False, "--csv", help="Emit machine-readable CSV"),
    start_date: str = typer.Option(None, "--start-date", help="Filter by start date (YYYY-MM-DD)"),
    end_date: str = typer.Option(None, "--end-date", help="Filter by end date (YYYY-MM-DD)"),
    log_dir: str = typer.Option(None, "--log-dir", help="Decision-log directory to read"),
) -> None:
    """Report what the Pilot Router saved against the baseline model.

    Reads the local decision log (``~/.agentos/logs/decisions-*.jsonl``), so it
    works with the gateway stopped. Routing only — the other savings mechanisms
    are excluded so the number is attributable to the router.
    """

    directory = Path(log_dir) if log_dir else _default_log_dir()
    report = build_savings_report(directory, start_date=start_date, end_date=end_date)

    if pdf:
        path = render_savings_pdf(report, Path(pdf))
        if json_output:
            print_json({**_savings_payload(report), "pdfPath": str(path)})
        else:
            # soft_wrap keeps the path on one line so it stays copy-pasteable.
            console.print(f"Wrote Pilot Router savings report to {path}", soft_wrap=True)
        return

    if json_output:
        print_json(_savings_payload(report))
        return

    if csv_output:
        import io

        f = io.StringIO()
        writer = csv.writer(f)
        writer.writerow(
            [
                "RequestedModel",
                "RoutedModel",
                "Turns",
                "AvgSavingsPct",
                "AvgConfidence",
                "SavedUsd",
            ]
        )
        for row in report.by_route:
            writer.writerow(
                [
                    row.requested_model,
                    row.routed_model,
                    row.turns,
                    "" if row.avg_savings_pct is None else f"{row.avg_savings_pct:.2f}",
                    "" if row.avg_confidence is None else f"{row.avg_confidence:.4f}",
                    f"{row.savings_usd:.6f}",
                ]
            )
        console.print(f.getvalue().strip())
        return

    _render_savings_table(report)


def _savings_payload(report: SavingsReport) -> dict[str, Any]:
    """Camel-case the report for the CLI JSON contract."""

    return {
        "startDate": report.start_date,
        "endDate": report.end_date,
        "turnsTotal": report.turns_total,
        "turnsRouted": report.turns_routed,
        "turnsRerouted": report.turns_rerouted,
        "turnsKept": report.turns_kept,
        "turnsAtTopTier": report.turns_at_top_tier,
        "actualCostUsd": report.actual_cost_usd,
        "routingSavingsUsd": report.routing_savings_usd,
        "topTierCostUsd": report.top_tier_cost_usd,
        "savingsPct": report.savings_pct,
        "avgConfidence": report.avg_confidence,
        "tokensInput": report.tokens_input,
        "tokensOutput": report.tokens_output,
        "byRoute": [
            {
                "requestedModel": row.requested_model,
                "routedModel": row.routed_model,
                "turns": row.turns,
                "avgSavingsPct": row.avg_savings_pct,
                "avgConfidence": row.avg_confidence,
                "savingsUsd": row.savings_usd,
            }
            for row in report.by_route
        ],
        "byDay": [
            {
                "date": row.date,
                "turns": row.turns,
                "savingsUsd": row.savings_usd,
                "actualCostUsd": row.actual_cost_usd,
            }
            for row in report.by_day
        ],
    }


def _render_savings_table(report: SavingsReport) -> None:
    """Print the savings summary and the per-route breakdown."""

    window = (
        f"{report.start_date} to {report.end_date}"
        if report.start_date and report.end_date
        else "no routed turns in window"
    )
    console.print(
        f"[bold]Pilot Router savings[/bold] [dim]({window})[/dim]\n"
        f"  saved       [bold]${report.routing_savings_usd:,.2f}[/bold] "
        f"({report.savings_pct:.1f}% of the top-tier bill)\n"
        f"  actual      ${report.actual_cost_usd:,.2f}\n"
        f"  top tier    ${report.top_tier_cost_usd:,.2f}\n"
        f"  turns       {report.turns_routed:,} routed of {report.turns_total:,} logged "
        f"({report.turns_rerouted:,} moved off the request, {report.turns_kept:,} kept, "
        f"{report.turns_at_top_tier:,} on the top tier)\n"
        f"[dim]  Baseline is the priciest model in \\[router.tiers]; input tokens only.[/dim]"
    )

    if not report.by_route:
        return

    table = Table(title="Savings by Route", show_header=True, header_style=ACCENT_HEADER)
    table.add_column("Requested")
    table.add_column("Routed To")
    table.add_column("Turns", justify="right")
    table.add_column("Avg %", justify="right")
    table.add_column("Avg Conf", justify="right")
    table.add_column("Saved", justify="right")
    for row in report.by_route:
        table.add_row(
            row.requested_model,
            row.routed_model,
            f"{row.turns:,}",
            "-" if row.avg_savings_pct is None else f"{row.avg_savings_pct:.1f}%",
            "-" if row.avg_confidence is None else f"{row.avg_confidence:.2f}",
            f"${row.savings_usd:,.2f}",
        )
    console.print(table)
