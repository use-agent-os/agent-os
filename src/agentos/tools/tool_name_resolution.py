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

import re
from collections.abc import Iterable
from dataclasses import dataclass

import structlog

from agentos.tools.registry import ToolRegistry
from agentos.tools.types import ToolContext
from agentos.tools.visibility import is_tool_visible

__all__ = ["ToolNameResolution", "resolve_tool_name", "resolve_tool_name_among"]

log = structlog.get_logger("agentos.tools.dispatch")

# Namespaces some providers and chat templates put in front of a function name.
_NAMESPACE_PREFIXES: tuple[str, ...] = ("functions.", "tools.")
# A per-call counter appended to the name, as in ``read_file:0``.
_CALL_COUNTER_RE = re.compile(r":\d+$")
# Every MCP tool is registered as ``mcp_<tool>`` or ``mcp_<server>_<tool>``
# (see ``agentos.mcp.discovery.resolve_tool_name``).
_MCP_NAME_PREFIX = "mcp_"


@dataclass(frozen=True)
class ToolNameResolution:
    """A requested tool name and the registered name it resolved to."""

    requested: str
    resolved: str
    # Which rewrites were needed, in order: ``namespace_prefix``,
    # ``call_counter``, ``loose_match``.
    rules: tuple[str, ...]


def _loose_key(name: str) -> str:
    return name.replace("/", "_").casefold()


def _is_mcp_name(name: str) -> bool:
    return _loose_key(name).startswith(_MCP_NAME_PREFIX)


def _strip_decorations(name: str) -> tuple[str, list[str]]:
    rules: list[str] = []
    stripped = name.strip()
    lowered = stripped.lower()
    for prefix in _NAMESPACE_PREFIXES:
        if lowered.startswith(prefix) and len(stripped) > len(prefix):
            stripped = stripped[len(prefix) :]
            rules.append("namespace_prefix")
            break
    counter = _CALL_COUNTER_RE.search(stripped)
    if counter is not None and counter.start() > 0:
        stripped = stripped[: counter.start()]
        rules.append("call_counter")
    return stripped, rules


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


def resolve_tool_name_among(
    requested: str,
    offered: Iterable[str],
) -> ToolNameResolution | None:
    """Return the single name in ``offered`` that ``requested`` unambiguously means.

    ``offered`` is the set of tool names the caller may use (for a model turn,
    the tool definitions sent with the request). An exact hit is not a
    resolution: callers check that first.
    """

    if not isinstance(requested, str) or not requested.strip():
        return None
    name, rules = _strip_decorations(requested)
    if not name:
        return None

    names = list(dict.fromkeys(offered))
    if name != requested and name in names:
        return ToolNameResolution(requested=requested, resolved=name, rules=tuple(rules))

    if _is_mcp_name(name):
        return None
    key = _loose_key(name)
    candidates = [
        candidate
        for candidate in names
        if not _is_mcp_name(candidate) and _loose_key(candidate) == key
    ]
    if len(candidates) != 1:
        if candidates:
            log.info(
                "dispatch.tool_name_ambiguous",
                requested=requested,
                candidates=sorted(candidates),
            )
        return None
    return ToolNameResolution(
        requested=requested,
        resolved=candidates[0],
        rules=(*rules, "loose_match"),
    )
