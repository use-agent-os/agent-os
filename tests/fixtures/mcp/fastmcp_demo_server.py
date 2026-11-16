"""Tiny stdio MCP server for MCP client tests, built on the ``mcp`` SDK's FastMCP.

Run as a script: ``python fastmcp_demo_server.py``. Offline and deterministic.
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

server = FastMCP("demo")


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


if __name__ == "__main__":
    server.run("stdio")
