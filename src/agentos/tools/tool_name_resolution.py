"""Map a near-miss tool name onto the one registered tool it clearly means.

Models, weaker ones especially, decorate the tool name they were given: a
``functions.`` or ``tools.`` namespace in front, a ``:0`` call counter behind,
different capitalisation, or a ``/`` where our names use ``_``. An exact-match
lookup turns each of those into a wasted iteration on a ``ToolNotFound``.

:func:`resolve_tool_name` runs only after the exact lookup missed. It undoes
those decorations and accepts the result when exactly one tool the caller can
see matches; anything else (no match, or two tools that differ only by case)
leaves the call to fail as before.

Tools provided by MCP servers are never matched loosely. Their names embed the
server's own tool name, which is case-sensitive and may legitimately contain
``/`` or ``:``, and a loose match could hop from one server's namespace to
another's. They are reachable by their exact name, or by their exact name
wrapped in a namespace prefix or call counter.
"""

from __future__ import annotations

from agentos.provider.tool_names import ToolNameResolution, resolve_tool_name_among
from agentos.tools.registry import ToolRegistry
from agentos.tools.types import ToolContext
from agentos.tools.visibility import is_tool_visible

__all__ = ["ToolNameResolution", "resolve_tool_name", "resolve_tool_name_among"]


def resolve_tool_name(
    requested: str,
    registry: ToolRegistry,
    ctx: ToolContext | None,
) -> ToolNameResolution | None:
    """Return the single visible tool ``requested`` unambiguously names, if any.

    Call this only when ``registry.get(requested)`` missed. Candidates are the
    tools visible to ``ctx``, so a resolution never reaches a tool the caller
    could not have called by name in the first place.
    """

    if not isinstance(requested, str) or not requested.strip():
        return None
    visible = [rt.spec.name for rt in registry.all_tools() if is_tool_visible(rt, ctx)]
    return resolve_tool_name_among(requested, visible)
