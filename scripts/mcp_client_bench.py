"""Benchmark the MCP client: boot with several stdio servers, and call overlap.

Offline: it spawns the FastMCP fixture server from ``tests/fixtures/mcp``.

    AGENTOS_STATE_DIR=$(mktemp -d) uv run python scripts/mcp_client_bench.py

Reports
* boot: wall time to connect N stdio servers one after another versus through
  the gateway's boot path (``_discover_configured_mcp_servers``);
* overlap: latency of a fast tool call issued while a slow call to the same
  server is in flight (the slow call sleeps ``--slow`` seconds).
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from agentos.gateway.boot import _discover_configured_mcp_servers
from agentos.gateway.config import GatewayConfig, MCPConfig, MCPServerEntry
from agentos.mcp import discovery
from agentos.mcp.stdio import MCPStdioClient
from agentos.mcp.types import MCPServerConfig
from agentos.tools.registry import ToolRegistry

SERVER = (
    Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "mcp" / "fastmcp_demo_server.py"
)


def _entries(count: int) -> list[MCPServerEntry]:
    return [
        MCPServerEntry(
            name=f"bench{i}",
            transport="stdio",
            command=sys.executable,
            args=[str(SERVER), "--extras"],
            connect_timeout_seconds=60,
        )
        for i in range(count)
    ]


async def _boot_sequential(count: int) -> float:
    registry = ToolRegistry()
    started = time.perf_counter()
    for entry in _entries(count):
        await discovery.discover_and_register(MCPServerConfig.from_entry(entry), registry)
    elapsed = time.perf_counter() - started
    await discovery.close_active_clients()
    return elapsed


async def _boot_gateway_path(count: int) -> float:
    registry = ToolRegistry()
    config = SimpleNamespace(
        mcp=MCPConfig(enabled=True, servers=_entries(count)),
        state_dir=GatewayConfig().state_dir,
    )
    started = time.perf_counter()
    await _discover_configured_mcp_servers(config, registry)  # type: ignore[arg-type]
    elapsed = time.perf_counter() - started
    connected = len(discovery.active_clients_snapshot())
    await discovery.close_active_clients()
    from agentos.mcp.supervisor import shutdown_supervisor

    await shutdown_supervisor()
    if connected != count:
        raise RuntimeError(f"only {connected}/{count} servers connected")
    return elapsed


async def _overlap(slow_seconds: float) -> float:
    client = MCPStdioClient(
        MCPServerConfig(
            name="bench", transport="stdio", command=sys.executable, args=[str(SERVER), "--extras"]
        )
    )
    await client.connect()
    try:
        await client.call_tool("search", {"query": "warm"})
        slow = asyncio.create_task(client.call_tool("sleep", {"seconds": slow_seconds}))
        await asyncio.sleep(0.05)  # the slow call is on the wire
        started = time.perf_counter()
        await client.call_tool("search", {"query": "fast"})
        elapsed = time.perf_counter() - started
        await slow
        return elapsed
    finally:
        await client.close()


def _fmt(samples: list[float]) -> str:
    ms = [sample * 1000 for sample in samples]
    return f"median {statistics.median(ms):8.1f} ms  (min {min(ms):.1f}, max {max(ms):.1f})"


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--servers", type=int, default=3)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--slow", type=float, default=1.0)
    args = parser.parse_args()

    sequential = [await _boot_sequential(args.servers) for _ in range(args.rounds)]
    gateway = [await _boot_gateway_path(args.servers) for _ in range(args.rounds)]
    overlap = [await _overlap(args.slow) for _ in range(args.rounds)]

    print(f"boot, {args.servers} stdio servers, one after another : {_fmt(sequential)}")
    print(f"boot, {args.servers} stdio servers, gateway boot path  : {_fmt(gateway)}")
    print(f"fast call during a {args.slow:.1f}s slow call          : {_fmt(overlap)}")


if __name__ == "__main__":
    asyncio.run(main())
