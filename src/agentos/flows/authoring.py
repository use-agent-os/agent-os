"""Flow authoring helpers: the agent spec format, auto-layout and change summaries.

The **agent spec** is a flow without layout, written the way a model can type it::

    {"name": "Poster",
     "nodes": [{"id": "idea", "type": "text", "values": {"text": "a lighthouse"}},
               {"id": "gen", "type": "image", "title": "Render",
                "values": {"model": "surplus/nano-banana-2", "aspect": "3:2"}}],
     "links": [["idea.text", "gen.prompt"]],
     "inputs": {"idea": "idea.text"}}

Links are ``"<node>.<output>" -> "<node>.<input>"`` pairs; node ids are the
author's own. :func:`parse_spec` checks the *shape* (every error names the node
or the link); graph semantics stay with the planner's validator.
"""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from agentos.flows.schema import NODE_TYPES
from agentos.flows.types import FLOW_VERSION, FlowError

COLUMN_WIDTH = 320
#: Retained for callers; vertical placement now uses estimated heights.
ROW_HEIGHT = 260
ORIGIN = (40, 40)
NODE_WIDTH = 280
NODE_GAP = 48
HEADER_HEIGHT = 44
PORT_HEIGHT = 24
WIDGET_HEIGHT = 36
TEXTAREA_LINE_HEIGHT = 20
TEXTAREA_PADDING = 16
TEXTAREA_MIN_LINES = 2
TEXTAREA_MAX_LINES = 8
CHARS_PER_LINE = 40
PREVIEW_HEIGHT = 200
PREVIEW_TYPES = frozenset({"load_image", "image", "save"})
SPEC_NODE_KEYS = frozenset({"id", "type", "title", "values", "pos"})
SPEC_KEYS = frozenset({"name", "nodes", "links", "inputs"})
NODE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,39}$")
FLOW_KEY = "_flow"
MAX_SUMMARY_CHARS = 300


def _node_types() -> dict[str, Any]:
    import agentos.flows.nodes  # noqa: F401 - registers the built-in nodes

    return NODE_TYPES


# ── auto-layout ───────────────────────────────────────────────────────────


def _has_pos(node: dict[str, Any]) -> bool:
    pos = node.get("pos")
    return (
        isinstance(pos, list)
        and len(pos) == 2
        and all(isinstance(v, int | float) and not isinstance(v, bool) for v in pos)
    )


def _edges(doc: dict[str, Any], ids: set[str]) -> list[tuple[str, str]]:
    edges = []
    for link in doc.get("links") or []:
        try:
            src, dst = str(link["from"][0]), str(link["to"][0])
        except (KeyError, IndexError, TypeError):
            continue
        if src in ids and dst in ids and src != dst:
            edges.append((src, dst))
    return edges


def _depths(order: list[str], edges: list[tuple[str, str]]) -> dict[str, int]:
    """Longest-path depth from the sources (nodes on a cycle stay at 0)."""
    indegree = {node_id: 0 for node_id in order}
    outgoing: dict[str, list[str]] = {node_id: [] for node_id in order}
    for src, dst in edges:
        indegree[dst] += 1
        outgoing[src].append(dst)
    depth = {node_id: 0 for node_id in order}
    queue = deque(node_id for node_id in order if indegree[node_id] == 0)
    while queue:
        node_id = queue.popleft()
        for dst in outgoing[node_id]:
            depth[dst] = max(depth[dst], depth[node_id] + 1)
            indegree[dst] -= 1
            if indegree[dst] == 0:
                queue.append(dst)
    return depth


def estimate_height(node: dict[str, Any]) -> int:
    """Rough rendered height of a node, from its schema and values (canvas units).

    Header + one row per port line + one row per widget; a textarea grows with
    its text (about ``CHARS_PER_LINE`` characters a line, capped); nodes that
    show image previews (``load_image``, ``image``, ``save``) add ``PREVIEW_HEIGHT``.
    Widgets that depend on the model (``show_when``) count only when set.
    """
    cls = _node_types().get(str(node.get("type")))
    if cls is None:
        return HEADER_HEIGHT + 2 * WIDGET_HEIGHT
    schema = cls.schema
    values = node.get("values") or {}
    sockets = sum(1 for spec_ in schema.inputs if spec_.has_socket)
    height = HEADER_HEIGHT + max(sockets, len(schema.outputs)) * PORT_HEIGHT
    for spec_ in schema.inputs:
        if spec_.widget is None:
            continue
        if spec_.show_when is not None and spec_.name not in values:
            continue
        if spec_.widget == "textarea":
            text = str(values.get(spec_.name, spec_.default_value()) or "")
            lines = -(-len(text) // CHARS_PER_LINE)
            lines = min(TEXTAREA_MAX_LINES, max(TEXTAREA_MIN_LINES, lines))
            height += lines * TEXTAREA_LINE_HEIGHT + TEXTAREA_PADDING
        else:
            height += WIDGET_HEIGHT
    if schema.id in PREVIEW_TYPES:
        height += PREVIEW_HEIGHT
    return height


def layout(doc: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *doc* where every node has a ``pos``.

    Nodes that already have one keep it. The others go in columns by
    longest-path depth (``COLUMN_WIDTH`` apart, starting at the leftmost
    existing node), level with their first positioned upstream node, else
    below the last node of their column in link order. Placement is
    height-aware (:func:`estimate_height`, ``NODE_GAP`` between nodes): a
    node that would overlap another moves below it. Every node placed here is
    marked ``"auto_pos": true`` so a client can re-flow it by measured size.
    Deterministic for a given document.
    """
    out: dict[str, Any] = json.loads(json.dumps(doc))
    nodes = [n for n in out.get("nodes") or [] if isinstance(n, dict)]
    order = [str(n.get("id")) for n in nodes]
    by_id = {str(n.get("id")): n for n in nodes}
    edges = _edges(out, set(order))
    depth = _depths(order, edges)
    heights = {node_id: estimate_height(by_id[node_id]) for node_id in order}
    # (x, top, bottom) of every placed node.
    placed: list[tuple[float, float, float]] = [
        (n["pos"][0], n["pos"][1], n["pos"][1] + heights[str(n.get("id"))])
        for n in nodes
        if _has_pos(n)
    ]
    origin_x = min((p[0] for p in placed), default=ORIGIN[0])
    origin_y = min((p[1] for p in placed), default=ORIGIN[1])

    first_link: dict[str, int] = {}
    for index, (src, dst) in enumerate(edges):
        first_link.setdefault(src, index)
        first_link.setdefault(dst, index)
    doc_index = {node_id: i for i, node_id in enumerate(order)}
    upstream: dict[str, list[str]] = {node_id: [] for node_id in order}
    for src, dst in edges:
        upstream[dst].append(src)
    column_next: dict[int, float] = {}

    def blocker(x: float, top: float, bottom: float) -> float | None:
        """The lowest bottom among nodes this slot would overlap, else ``None``."""
        hits = [
            pb
            for px, pt, pb in placed
            if abs(px - x) < NODE_WIDTH and top < pb + NODE_GAP and pt < bottom + NODE_GAP
        ]
        return max(hits) if hits else None

    missing = sorted(
        (node_id for node_id in order if not _has_pos(by_id[node_id])),
        key=lambda n: (depth[n], first_link.get(n, len(edges)), doc_index[n]),
    )
    for node_id in missing:
        column = depth[node_id]
        x = origin_x + column * COLUMN_WIDTH
        anchor = next((u for u in upstream[node_id] if _has_pos(by_id[u])), None)
        y = by_id[anchor]["pos"][1] if anchor is not None else column_next.get(column, origin_y)
        height = heights[node_id]
        while (bottom := blocker(x, y, y + height)) is not None:
            y = bottom + NODE_GAP
        by_id[node_id]["pos"] = [x, y]
        by_id[node_id]["auto_pos"] = True
        placed.append((x, y, y + height))
        column_next[column] = max(column_next.get(column, origin_y), y + height + NODE_GAP)
    return out


# ── change summaries ──────────────────────────────────────────────────────


def _link_pairs(doc: dict[str, Any]) -> set[tuple[str, str, str, str]]:
    pairs = set()
    for link in doc.get("links") or []:
        try:
            pairs.add(
                (str(link["from"][0]), str(link["from"][1]), str(link["to"][0]), str(link["to"][1]))
            )
        except (KeyError, IndexError, TypeError):
            continue
    return pairs


def _show(value: Any) -> str:
    """A short, readable form of a widget value for a change summary."""
    if value is None:
        return "unset"
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, dict):
        return "image" if "sha256" in value else "{…}"
    if isinstance(value, list):
        return "[…]"
    text = " ".join(str(value).split())
    return text if len(text) <= SHORT_TEXT_CHARS else text[: SHORT_TEXT_CHARS - 1] + "…"


#: Text values up to this long (single line) are shown whole in a summary.
SHORT_TEXT_CHARS = 40
#: Characters of unchanged context kept before / after a changed span.
WINDOW_CONTEXT = 10
#: Longest window shown per side of a text change.
WINDOW_CHARS = 40


def _common_affixes(a: str, b: str) -> tuple[int, int]:
    """Lengths of the common prefix and (non-overlapping) common suffix of *a* and *b*."""
    limit = min(len(a), len(b))
    prefix = 0
    while prefix < limit and a[prefix] == b[prefix]:
        prefix += 1
    suffix = 0
    while suffix < limit - prefix and a[-1 - suffix] == b[-1 - suffix]:
        suffix += 1
    return prefix, suffix


def _window(text: str, prefix: int, suffix: int) -> str:
    """The changed span of *text* with a little context, quoted, ellipses where trimmed.

    A text that fits in ``WINDOW_CHARS`` (a short changed line) is shown whole.
    """
    if len(text) <= WINDOW_CHARS:
        return json.dumps(text, ensure_ascii=False)
    start = max(0, prefix - WINDOW_CONTEXT)
    stop = min(len(text), len(text) - suffix + WINDOW_CONTEXT)
    clipped = stop - start > WINDOW_CHARS
    if clipped:
        stop = start + WINDOW_CHARS
    body = text[start:stop]
    lead = "…" if start > 0 else ""
    tail = "…" if stop < len(text) else ""
    return json.dumps(f"{lead}{body}{tail}", ensure_ascii=False)


def _text_change(old: str, new: str) -> str:
    """``OLD → NEW`` showing only the part that differs (``line N`` for multi-line text)."""
    label = ""
    if "\n" in old or "\n" in new:
        old_lines, new_lines = old.split("\n"), new.split("\n")
        head = 0
        while head < min(len(old_lines), len(new_lines)) and old_lines[head] == new_lines[head]:
            head += 1
        tail = 0
        while (
            tail < min(len(old_lines), len(new_lines)) - head
            and old_lines[-1 - tail] == new_lines[-1 - tail]
        ):
            tail += 1
        old_span = old_lines[head : len(old_lines) - tail]
        new_span = new_lines[head : len(new_lines) - tail]
        last = head + max(len(old_span), len(new_span))
        label = f"line {head + 1} " if last <= head + 1 else f"lines {head + 1}-{last} "
        old, new = "\n".join(old_span), "\n".join(new_span)
    prefix, suffix = _common_affixes(old, new)
    return f"{label}{_window(old, prefix, suffix)} → {_window(new, prefix, suffix)}"


def _change(old: Any, new: Any) -> str:
    """How a value changed, for a summary line."""
    if isinstance(old, str) and isinstance(new, str):
        multi = "\n" in old or "\n" in new
        if multi or len(old) > SHORT_TEXT_CHARS or len(new) > SHORT_TEXT_CHARS:
            return _text_change(old, new)
    return f"{_show(old)} → {_show(new)}"


def diff_summary(old: dict[str, Any] | None, new: dict[str, Any]) -> str:
    """One line from *old* to *new*: added / removed nodes, changed values
    (``gen.aspect 16:9 → 1:1``), link counts, rename, public inputs."""
    if not old:
        nodes = len(new.get("nodes") or [])
        links = len(new.get("links") or [])
        return f"created with {nodes} node{'s' * (nodes != 1)} and {links} link{'s' * (links != 1)}"
    old_nodes = {str(n.get("id")): n for n in old.get("nodes") or []}
    new_nodes = {str(n.get("id")): n for n in new.get("nodes") or []}
    parts: list[str] = []
    if (old.get("name") or "") != (new.get("name") or ""):
        parts.append(f'renamed to "{new.get("name")}"')
    added = [f"{i} ({new_nodes[i].get('type')})" for i in new_nodes if i not in old_nodes]
    removed = [f"{i} ({old_nodes[i].get('type')})" for i in old_nodes if i not in new_nodes]
    if added:
        parts.append("added " + ", ".join(added))
    if removed:
        parts.append("removed " + ", ".join(removed))
    changed: list[str] = []
    for node_id, node in new_nodes.items():
        before = old_nodes.get(node_id)
        if before is None:
            continue
        for key in ("type", "title"):
            if before.get(key) != node.get(key):
                changed.append(f"{node_id}.{key} {_show(before.get(key))} → {_show(node.get(key))}")
        old_values, new_values = before.get("values") or {}, node.get("values") or {}
        for key in sorted(set(old_values) | set(new_values)):
            if old_values.get(key) != new_values.get(key):
                changed.append(
                    f"{node_id}.{key} {_change(old_values.get(key), new_values.get(key))}"
                )
    if changed:
        parts.append("changed " + ", ".join(changed))
    old_links, new_links = _link_pairs(old), _link_pairs(new)
    plus, minus = len(new_links - old_links), len(old_links - new_links)
    if plus or minus:
        parts.append(f"links +{plus} -{minus}")
    if (old.get("inputs") or {}) != (new.get("inputs") or {}):
        parts.append("public inputs changed")
    if not parts:
        moved = any(
            old_nodes[i].get("pos") != new_nodes[i].get("pos") for i in new_nodes if i in old_nodes
        )
        if moved or old.get("viewport") != new.get("viewport"):
            return "layout only"
        return "no changes"
    summary = "; ".join(parts)
    return summary if len(summary) <= MAX_SUMMARY_CHARS else summary[: MAX_SUMMARY_CHARS - 1] + "…"


# ── agent spec ────────────────────────────────────────────────────────────


@dataclass
class ParsedSpec:
    name: str | None
    nodes: list[dict[str, Any]]
    links: list[tuple[str, str, str, str]]
    inputs: dict[str, str]
    #: ``(node_id, value)`` of ``load_image`` values that are references to resolve.
    image_refs: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


class _Errors:
    def __init__(self) -> None:
        self.by_node: dict[str, list[dict[str, Any]]] = {}

    def add(self, key: str, code: str, message: str, *, input: str | None = None) -> None:
        entry: dict[str, Any] = {"code": code, "message": message}
        if input:
            entry["input"] = input
        self.by_node.setdefault(key, []).append(entry)

    def raise_if_any(self, message: str = "The flow spec has errors") -> None:
        if self.by_node:
            raise FlowError("flows.invalid", message, details={"node_errors": self.by_node})


def spec_invalid(node_errors: dict[str, list[dict[str, Any]]], message: str) -> FlowError:
    return FlowError("flows.invalid", message, details={"node_errors": node_errors})


def _split_end(raw: Any) -> tuple[str, str] | None:
    if not isinstance(raw, str) or raw.count(".") != 1:
        return None
    node_id, name = raw.split(".")
    return (node_id, name) if node_id and name else None


def is_image_reference(value: Any) -> bool:
    """``{"attachment": ...}`` or ``{"path": ...}``: resolved by the tool layer."""
    return isinstance(value, dict) and ("attachment" in value or "path" in value)


def parse_spec(spec: Any, *, require_name: bool) -> ParsedSpec:
    """Check the shape of an agent spec; every problem is reported at once.

    Errors are keyed by node id (``"_flow"`` for the spec itself) and name the
    offending node, link (by index and text) or public input.
    """
    types = _node_types()
    errors = _Errors()
    if not isinstance(spec, dict):
        raise spec_invalid(
            {FLOW_KEY: [{"code": "invalid_spec", "message": "The spec must be an object"}]},
            "The flow spec has errors",
        )
    for key in sorted(set(spec) - SPEC_KEYS):
        errors.add(
            FLOW_KEY, "unknown_key", f"Unknown spec key {key!r} (use name, nodes, links, inputs)"
        )
    name = spec.get("name")
    if name is not None and (not isinstance(name, str) or not name.strip()):
        errors.add(FLOW_KEY, "invalid_name", "name must be a non-empty string")
        name = None
    if require_name and name is None:
        errors.add(FLOW_KEY, "required", "name is required")

    raw_nodes = spec.get("nodes")
    nodes: list[dict[str, Any]] = []
    image_refs: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    if not isinstance(raw_nodes, list) or not raw_nodes:
        errors.add(FLOW_KEY, "required", "nodes must be a non-empty list")
        raw_nodes = []
    for index, raw in enumerate(raw_nodes):
        if not isinstance(raw, dict):
            errors.add(FLOW_KEY, "invalid_node", f"nodes[{index}] must be an object")
            continue
        node_id = raw.get("id")
        if not isinstance(node_id, str) or not NODE_ID_RE.match(node_id):
            errors.add(
                FLOW_KEY,
                "invalid_id",
                f"nodes[{index}].id {node_id!r} must start with a letter and use only "
                "letters, digits, '_' or '-' (at most 40)",
            )
            continue
        if node_id in seen:
            errors.add(node_id, "duplicate_id", f"Node id {node_id!r} is used twice")
            continue
        seen.add(node_id)
        for key in sorted(set(raw) - SPEC_NODE_KEYS):
            errors.add(
                node_id, "unknown_key", f"Unknown node key {key!r} (use id, type, title, values)"
            )
        node_type = raw.get("type")
        cls = types.get(node_type) if isinstance(node_type, str) else None
        if cls is None:
            errors.add(
                node_id,
                "unknown_type",
                f"Unknown node type {node_type!r}; use one of {', '.join(types)}",
            )
        values = raw.get("values") or {}
        if not isinstance(values, dict):
            errors.add(node_id, "invalid_value", "values must be an object")
            values = {}
        if cls is not None:
            known = [spec_.name for spec_ in cls.schema.inputs]
            for key in values:
                if key not in known:
                    errors.add(
                        node_id,
                        "unknown_input",
                        f"{node_type} has no input {key!r} (inputs: {', '.join(known)})",
                        input=str(key),
                    )
        title = raw.get("title")
        if title is not None and not isinstance(title, str):
            errors.add(node_id, "invalid_value", "title must be a string")
            title = None
        if node_type == "load_image" and is_image_reference(values.get("image")):
            image_refs.append((node_id, values["image"]))
        node: dict[str, Any] = {"id": node_id, "type": node_type, "title": title, "values": values}
        if _has_pos(raw):
            node["pos"] = list(raw["pos"])
        nodes.append(node)

    links: list[tuple[str, str, str, str]] = []
    raw_links = spec.get("links") or []
    if not isinstance(raw_links, list):
        errors.add(FLOW_KEY, "invalid_link", "links must be a list of [from, to] pairs")
        raw_links = []
    for index, raw in enumerate(raw_links):
        text = json.dumps(raw, ensure_ascii=False)
        if not isinstance(raw, list | tuple) or len(raw) != 2:
            errors.add(
                FLOW_KEY,
                "invalid_link",
                f'links[{index}] {text} must be ["node.output", "node.input"]',
            )
            continue
        src, dst = _split_end(raw[0]), _split_end(raw[1])
        if src is None or dst is None:
            errors.add(
                FLOW_KEY,
                "invalid_link",
                f'links[{index}] {text} must be ["node.output", "node.input"]',
            )
            continue
        missing = [end[0] for end in (src, dst) if end[0] not in seen]
        if missing:
            key = dst[0] if dst[0] in seen else FLOW_KEY
            errors.add(
                key,
                "unknown_link",
                f"links[{index}] {text} names an unknown node {missing[0]!r}",
                input=dst[1] if key != FLOW_KEY else None,
            )
            continue
        links.append((src[0], src[1], dst[0], dst[1]))

    inputs: dict[str, str] = {}
    raw_inputs = spec.get("inputs") or {}
    if not isinstance(raw_inputs, dict):
        errors.add(FLOW_KEY, "invalid_inputs", 'inputs must map a public name to "node.input"')
        raw_inputs = {}
    node_types = {n["id"]: n["type"] for n in nodes}
    for public, target in raw_inputs.items():
        end = _split_end(target)
        if end is None or end[0] not in node_types:
            errors.add(
                FLOW_KEY,
                "invalid_input",
                f'inputs[{public!r}] = {target!r} must be "node.input" of a node in this spec',
            )
            continue
        cls = types.get(node_types[end[0]])
        spec_ = cls.schema.input(end[1]) if cls is not None else None
        if cls is not None and (spec_ is None or spec_.widget is None):
            errors.add(
                end[0],
                "invalid_input",
                f"inputs[{public!r}]: {node_types[end[0]]}.{end[1]} has no value to expose",
                input=end[1],
            )
            continue
        inputs[str(public)] = f"{end[0]}.{end[1]}"
    errors.raise_if_any()
    return ParsedSpec(
        name=name.strip() if isinstance(name, str) else None,
        nodes=nodes,
        links=links,
        inputs=inputs,
        image_refs=image_refs,
    )


def spec_to_doc(parsed: ParsedSpec, *, name: str) -> dict[str, Any]:
    """A flow document (positions only where the spec gave them) from a parsed spec."""
    return {
        "version": FLOW_VERSION,
        "name": name,
        "nodes": [dict(node) for node in parsed.nodes],
        "links": [
            {"id": f"l{index}", "from": [src, out], "to": [dst, inp]}
            for index, (src, out, dst, inp) in enumerate(parsed.links, start=1)
        ],
        "inputs": dict(parsed.inputs),
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }


def doc_to_spec(doc: dict[str, Any]) -> dict[str, Any]:
    """The agent spec of a stored flow (no layout)."""
    nodes = []
    for node in doc.get("nodes") or []:
        item: dict[str, Any] = {"id": node.get("id"), "type": node.get("type")}
        if node.get("title"):
            item["title"] = node["title"]
        item["values"] = dict(node.get("values") or {})
        nodes.append(item)
    links = [
        [f"{link['from'][0]}.{link['from'][1]}", f"{link['to'][0]}.{link['to'][1]}"]
        for link in doc.get("links") or []
    ]
    return {
        "name": doc.get("name") or "",
        "nodes": nodes,
        "links": links,
        "inputs": dict(doc.get("inputs") or {}),
    }


def public_inputs(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """``[{"name", "target", "value", "widget"}]`` for the flow's public inputs."""
    types = _node_types()
    by_id = {str(n.get("id")): n for n in doc.get("nodes") or []}
    out = []
    for public, target in (doc.get("inputs") or {}).items():
        node_id, _, input_name = str(target).partition(".")
        node = by_id.get(node_id) or {}
        cls = types.get(str(node.get("type")))
        spec_ = cls.schema.input(input_name) if cls is not None else None
        values = node.get("values") or {}
        value = values.get(input_name, spec_.default_value() if spec_ is not None else None)
        out.append(
            {
                "name": public,
                "target": target,
                "value": value,
                "widget": spec_.widget if spec_ is not None else None,
            }
        )
    return out


# ── compact catalog for agents ────────────────────────────────────────────


def _compact_input(spec_: Any) -> dict[str, Any]:
    item: dict[str, Any] = {"name": spec_.name}
    if spec_.type is not None and spec_.has_socket:
        item["link"] = spec_.type
    if spec_.widget is not None:
        item["widget"] = spec_.widget
    if spec_.options is not None:
        item["options"] = list(spec_.options)
    if spec_.options_from is not None:
        item["options_from"] = spec_.options_from
    if spec_.has_default and spec_.default not in (None, ""):
        item["default"] = spec_.default
    if spec_.min is not None or spec_.max is not None:
        item["range"] = [spec_.min, spec_.max]
    if spec_.optional:
        item["optional"] = True
    if spec_.multi:
        item["multi"] = True
    return item


def compact_nodes() -> list[dict[str, Any]]:
    return [
        {
            "type": cls.schema.id,
            "description": cls.schema.description,
            "inputs": [_compact_input(spec_) for spec_ in cls.schema.inputs],
            "outputs": [{"name": out.name, "link": out.type} for out in cls.schema.outputs],
        }
        for cls in _node_types().values()
    ]
