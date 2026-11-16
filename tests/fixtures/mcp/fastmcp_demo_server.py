"""Tiny stdio MCP server for MCP client tests, built on the ``mcp`` SDK's FastMCP.

Run as a script: ``python fastmcp_demo_server.py``. Offline and deterministic.

``--extras`` adds what the plain server leaves out so older tests keep their
exact tool list: server ``instructions``, annotated tools, a ``sleep`` tool, a
tool that changes the catalog (``notifications/tools/list_changed``), one
resource and one prompt. ``--many N`` registers ``N`` more trivial tools.
"""

from __future__ import annotations

import asyncio
import os
import sys

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

EXTRAS = "--extras" in sys.argv
INSTRUCTIONS = "Demo corpus server. Use search before read_file; paths are corpus-relative."

server = FastMCP("demo", instructions=INSTRUCTIONS if EXTRAS else None)


@server.tool()
def search(query: str) -> str:
    """Search the demo corpus."""
    return f"results for {query}"


@server.tool()
def read_file(path: str) -> str:
    """Read a demo file."""
    return f"contents of {path}"


@server.tool()
def read_dir(path: str) -> str:
    """List a demo directory."""
    return f"listing of {path}"


@server.tool()
def delete_all() -> str:
    """A tool a filter should keep away from the model."""
    return "deleted"


@server.tool()
def where() -> str:
    """Report the server process's working directory."""
    return os.getcwd()


if EXTRAS:

    @server.tool()
    async def sleep(seconds: float, label: str = "") -> str:
        """Sleep, then report. Stands in for a slow tool."""
        await asyncio.sleep(seconds)
        return f"slept {label or seconds}"

    @server.tool(
        title="Corpus statistics",
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False),
    )
    def stats() -> str:
        """Count the documents in the demo corpus."""
        return "3 documents"

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def wipe() -> str:
        """Erase the demo corpus."""
        return "wiped"

    @server.tool()
    async def grow(ctx: Context) -> str:  # type: ignore[type-arg]
        """Add a tool to the catalog and announce the change."""

        def grown() -> str:
            """A tool that appeared after connect."""
            return "grown"

        server.add_tool(grown, name="grown")
        await ctx.session.send_tool_list_changed()
        return "added grown"

    @server.resource("demo://readme")
    def readme() -> str:
        """The corpus readme."""
        return "demo readme body"

    @server.prompt()
    def summarize(topic: str) -> str:
        """Ask for a summary of a topic."""
        return f"Summarize {topic} in one line."


if "--many" in sys.argv:
    for _index in range(int(sys.argv[sys.argv.index("--many") + 1])):

        def _bulk(value: str = "", _n: int = _index) -> str:
            return f"bulk {_n} {value}"

        server.add_tool(_bulk, name=f"bulk_{_index:02d}", description=f"Bulk demo tool {_index}.")


if __name__ == "__main__":
    server.run("stdio")
