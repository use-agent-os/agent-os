"""CLI commands for running AgentOS as an inbound MCP server."""

from __future__ import annotations

import typer

from agentos.cli.url_utils import normalize_gateway_url

app = typer.Typer(help="Run the AgentOS MCP server bridge.")


@app.command("run")
def run_mcp_server(
    gateway_url: str = typer.Option(
        "ws://localhost:18791/ws",
        "--gateway",
        envvar="AGENTOS_GATEWAY_URL",
        help="AgentOS gateway URL to bridge to.",
    ),
) -> None:
    """Run a stdio MCP server exposing AgentOS session workflows."""

    from agentos.mcp_server.bridge import AgentOSMCPBridge
    from agentos.mcp_server.server import create_mcp_server

    bridge = AgentOSMCPBridge(gateway_url=normalize_gateway_url(gateway_url))
    try:
        mcp = create_mcp_server(bridge)
    except RuntimeError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc

    mcp.run(transport="stdio")
