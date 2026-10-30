"""``tool_search``: find deferred tools and load them into the session.

The request only carries the tools a typical turn needs; the rest are listed by
name in this tool's description (see :mod:`agentos.tools.deferred`). Calling it
loads the matches, so they are on the next request and every one after it for
the rest of the session. On a stock gateway it stands in for 27 tools, taking
a request from 66 to 40 tool schemas.
"""

from __future__ import annotations

import json
from typing import Any

from agentos.tools.deferred import TOOL_SEARCH_NAME, summarize_description, surface_for
from agentos.tools.registry import tool
from agentos.tools.types import ToolError, current_tool_context


@tool(
    name=TOOL_SEARCH_NAME,
    description=(
        "Before using the shell or a skill script to make audio, drive a "
        "browser, search X, send raw HTTP requests, or manage projects or skills, "
        "check the list below and load the matching tool. These tools are "
        "installed but not sent with every request, so their parameters are "
        "hidden until loaded. Call this "
        "with the names you need (or keywords for what you want to do); the "
        "matches are loaded for the rest of the session and you can call them on "
        "your next step. Calling a listed tool without loading it first fails."
    ),
    params={
        "query": {
            "type": "string",
            "description": (
                "Keywords or a short phrase for the capability you need, e.g. "
                "'text to speech' or 'git diff'. Optional when names is given."
            ),
        },
        "names": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Exact names from the deferred list to load.",
        },
        "limit": {
            "type": "integer",
            "description": "Most tools a keyword query loads (default 5).",
            "default": 5,
        },
    },
    required=[],
    # It changes the session's tool list, so it runs alone: a deferred tool
    # called in the same batch waits for the load instead of racing it.
    concurrency="exclusive",
    # Shown only when something is deferred: the turn runner adds it to the
    # request together with the catalog of what it can load.
    exposed_by_default=False,
)
async def tool_search(
    query: str = "",
    names: list[str] | None = None,
    limit: int = 5,
) -> str:
    ctx = current_tool_context.get()
    surface = surface_for(ctx.session_key if ctx is not None else None)
    if surface is None:
        raise ToolError(
            "No tools are deferred in this session; every available tool is already loaded."
        )
    requested = [str(name).strip() for name in (names or []) if str(name).strip()]
    if not requested and not str(query or "").strip():
        raise ToolError("Pass names (exact tool names from the deferred list) or a query.")

    matches = surface.search(query, requested, limit=limit)
    newly = await surface.load(rt.spec.name for rt in matches)

    matched = {rt.spec.name for rt in matches}
    unknown = [name for name in requested if name not in matched]
    payload: dict[str, Any] = {
        "loaded": newly,
        "already_loaded": [rt.spec.name for rt in matches if rt.spec.name not in newly],
        "tools": [
            {"name": rt.spec.name, "description": summarize_description(rt.spec.description, 200)}
            for rt in matches
        ],
    }
    if unknown:
        # Either always loaded already, or not available to this session at all;
        # the deferred list is the only thing this tool can load.
        payload["not_deferred"] = unknown
    if matches:
        payload["note"] = (
            "These tools are now available with their full parameters; call them "
            "directly from your next step. They stay loaded for this session."
        )
    else:
        payload["note"] = (
            "No deferred tool matched. Pick names from the deferred list in the "
            "tool_search description, or continue with the tools you already have."
        )
    return json.dumps(payload)
