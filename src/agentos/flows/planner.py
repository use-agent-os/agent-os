"""Validate and plan a flow: no provider is called here.

1. **Index** the document: known links, incoming/outgoing maps, link errors.
2. **Validate** (contract section 7.1): every error at once, keyed by node id
   (``"_flow"`` for errors that belong to no node).
3. **Plan** (7.2): a cache key per node, then walk up from the targets; a
   cached node stops the walk, so its ancestors are not scheduled.
4. **Estimate**: price per node and the total of what will actually run.

Cache keys are a Merkle hash of ``{type, values, inputs}``. An input refers to
its upstream by the upstream's *output digest* when the upstream's outputs
are already known (cached, or a pure node evaluated here), else by the
upstream's key. Content references let a locked Image (``variation: keep``)
reuse the image the last ``new`` run produced without serving stale
descendants after a failed run.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from agentos.flows.models import ModelEntry, ModelRegistry
from agentos.flows.schema import NODE_TYPES, FlowNode, InputSpec, NodeSchema
from agentos.flows.types import (
    IMAGE,
    FlowError,
    ImageRef,
    NodeError,
    price_free,
    price_unknown,
    price_usd,
    sum_prices,
)

FLOW_KEY = "_flow"
#: Rough cost of one auxiliary LLM call (prompt writer, describe image).
LLM_NODE_ESTIMATE_USD = 0.002


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Link:
    id: str
    src: str
    out: str
    dst: str
    inp: str


class Errors:
    def __init__(self) -> None:
        self.by_node: dict[str, list[dict[str, Any]]] = {}

    def add(self, node_id: str, code: str, message: str, *, input: str | None = None) -> None:
        entry: dict[str, Any] = {"code": code, "message": message}
        if input:
            entry["input"] = input
        bucket = self.by_node.setdefault(node_id, [])
        if entry not in bucket:
            bucket.append(entry)

    def __bool__(self) -> bool:
        return bool(self.by_node)


@dataclass
class GraphIndex:
    nodes: dict[str, dict[str, Any]]
    order: list[str]
    links: list[Link]
    incoming: dict[str, dict[str, list[Link]]]
    outgoing: dict[str, list[Link]]

    def schema(self, node_id: str) -> NodeSchema | None:
        cls = NODE_TYPES.get(str(self.nodes[node_id].get("type")))
        return cls.schema if cls is not None else None

    def node_class(self, node_id: str) -> type[FlowNode] | None:
        return NODE_TYPES.get(str(self.nodes[node_id].get("type")))

    def upstream(self, node_id: str) -> list[str]:
        seen: list[str] = []
        for links in self.incoming.get(node_id, {}).values():
            for link in links:
                if link.src not in seen:
                    seen.append(link.src)
        return seen


def index_graph(doc: dict[str, Any], errors: Errors) -> GraphIndex:
    import agentos.flows.nodes  # noqa: F401 - registers the built-in nodes

    nodes: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for node in doc.get("nodes") or []:
        node_id = str(node.get("id"))
        nodes[node_id] = node
        order.append(node_id)
        if node.get("type") not in NODE_TYPES:
            errors.add(node_id, "unknown_type", f"Unknown node type {node.get('type')!r}")
    links: list[Link] = []
    incoming: dict[str, dict[str, list[Link]]] = {}
    outgoing: dict[str, list[Link]] = {}
    for raw in doc.get("links") or []:
        link = Link(
            id=str(raw.get("id")),
            src=str(raw["from"][0]),
            out=str(raw["from"][1]),
            dst=str(raw["to"][0]),
            inp=str(raw["to"][1]),
        )
        owner = link.dst if link.dst in nodes else (link.src if link.src in nodes else FLOW_KEY)
        if link.src not in nodes or link.dst not in nodes:
            missing = link.src if link.src not in nodes else link.dst
            errors.add(owner, "unknown_link", f"Link {link.id} points at a missing node {missing}")
            continue
        src_cls = NODE_TYPES.get(str(nodes[link.src].get("type")))
        dst_cls = NODE_TYPES.get(str(nodes[link.dst].get("type")))
        if src_cls is None or dst_cls is None:
            continue  # the unknown_type error already says it
        out_spec = src_cls.schema.output(link.out)
        in_spec = dst_cls.schema.input(link.inp)
        if out_spec is None:
            errors.add(
                link.dst,
                "unknown_link",
                f"{src_cls.schema.display} has no output {link.out!r}",
                input=link.inp,
            )
            continue
        if in_spec is None or not in_spec.has_socket:
            errors.add(
                link.dst,
                "unknown_link",
                f"{dst_cls.schema.display} has no input socket {link.inp!r}",
                input=link.inp,
            )
            continue
        if out_spec.type != in_spec.type:
            errors.add(
                link.dst,
                "type_mismatch",
                f"{link.inp} takes {in_spec.type}, not {out_spec.type}",
                input=link.inp,
            )
            continue
        bucket = incoming.setdefault(link.dst, {}).setdefault(link.inp, [])
        if bucket and not in_spec.multi:
            errors.add(
                link.dst,
                "multiple_links",
                f"{link.inp} accepts one connection",
                input=link.inp,
            )
            continue
        bucket.append(link)
        outgoing.setdefault(link.src, []).append(link)
        links.append(link)
    return GraphIndex(nodes=nodes, order=order, links=links, incoming=incoming, outgoing=outgoing)


def find_cycle_nodes(index: GraphIndex) -> set[str]:
    """Nodes on (or downstream of) a cycle: whatever Kahn's algorithm cannot drain."""
    indegree = {node_id: 0 for node_id in index.nodes}
    for link in index.links:
        indegree[link.dst] += 1
    queue = deque(node_id for node_id, deg in indegree.items() if deg == 0)
    drained: set[str] = set()
    while queue:
        node_id = queue.popleft()
        drained.add(node_id)
        for link in index.outgoing.get(node_id, []):
            indegree[link.dst] -= 1
            if indegree[link.dst] == 0:
                queue.append(link.dst)
    return set(index.nodes) - drained


def targets_for(index: GraphIndex, to_node: str | None, errors: Errors) -> list[str]:
    if to_node:
        if to_node not in index.nodes:
            errors.add(FLOW_KEY, "unknown_node", f"No node {to_node!r} to run to")
            return []
        return [to_node]
    sinks = [node_id for node_id in index.order if not index.outgoing.get(node_id)]
    if not sinks:
        errors.add(FLOW_KEY, "empty", "The flow has no nodes")
    return sinks


def ancestors(index: GraphIndex, targets: list[str]) -> set[str]:
    scope: set[str] = set()
    stack = list(targets)
    while stack:
        node_id = stack.pop()
        if node_id in scope:
            continue
        scope.add(node_id)
        stack.extend(index.upstream(node_id))
    return scope


# ── values ────────────────────────────────────────────────────────────────


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _hidden(spec: InputSpec, entry: ModelEntry | None) -> bool:
    if spec.show_when is None:
        return False
    cap = spec.show_when.get("model.has")
    return entry is None or not (cap and entry.has_cap(cap))


@dataclass
class Resolved:
    values: dict[str, Any]
    entry: ModelEntry | None = None


def resolve_values(
    node_id: str,
    schema: NodeSchema,
    raw_values: dict[str, Any],
    models: ModelRegistry,
    *,
    linked: set[str],
    errors: Errors | None,
    check_availability: bool = True,
) -> Resolved:
    """Apply defaults, resolve ``auto`` and per-model options, drop hidden widgets.

    Widget values of linked inputs are dropped too: the link wins. With
    ``errors`` given, every widget problem is recorded.
    """
    bag = errors if errors is not None else Errors()
    raw = raw_values if isinstance(raw_values, dict) else {}
    values: dict[str, Any] = {}
    entry: ModelEntry | None = None
    model_spec = next((s for s in schema.inputs if s.widget == "model"), None)
    if model_spec is not None:
        requested = raw.get(model_spec.name, model_spec.default_value())
        entry = models.resolve(requested)
        if entry is None:
            if requested in (None, "", "auto"):
                bag.add(
                    node_id,
                    "no_default_model",
                    "No image model is available: add a Surplus, OpenRouter or OpenAI API key",
                    input=model_spec.name,
                )
            else:
                bag.add(
                    node_id, "unknown_model", f"Unknown model {requested!r}", input=model_spec.name
                )
        else:
            if check_availability:
                available, reason = models.availability(entry)
                if not available:
                    bag.add(
                        node_id,
                        "model_unavailable",
                        f"{entry.display} is not available: {reason}",
                        input=model_spec.name,
                    )
            values[model_spec.name] = entry.id

    for spec in schema.inputs:
        if spec.widget is None or spec.widget == "model":
            continue
        if spec.name in linked and spec.has_socket:
            continue
        if _hidden(spec, entry):
            continue
        given = spec.name in raw and raw[spec.name] is not None
        value = raw[spec.name] if given else spec.default_value()
        widget = spec.widget
        if widget in ("text", "textarea"):
            if value is None:
                value = ""
            if not isinstance(value, str):
                bag.add(node_id, "invalid_value", f"{spec.name} must be text", input=spec.name)
                value = str(value)
        elif widget == "number":
            if isinstance(value, float) and value.is_integer() and (spec.step or 1) >= 1:
                value = int(value)
            if not _is_number(value):
                bag.add(node_id, "invalid_value", f"{spec.name} must be a number", input=spec.name)
                value = spec.default_value()
            elif (spec.min is not None and value < spec.min) or (
                spec.max is not None and value > spec.max
            ):
                bag.add(
                    node_id,
                    "out_of_range",
                    f"{spec.name} must be between {spec.min:g} and {spec.max:g}"
                    if spec.min is not None and spec.max is not None
                    else f"{spec.name} is out of range",
                    input=spec.name,
                )
        elif widget == "toggle":
            if not isinstance(value, bool):
                bag.add(node_id, "invalid_value", f"{spec.name} must be on or off", input=spec.name)
                value = bool(spec.default_value())
        elif widget in ("combo", "variation"):
            options: tuple[str, ...] = ()
            if spec.options is not None:
                options = spec.options
            elif spec.options_from is not None and entry is not None:
                options = entry.options_for(spec.options_from)
            if options and value not in options:
                if given:
                    bag.add(
                        node_id,
                        "invalid_option",
                        f"{spec.name} must be one of {', '.join(options)}",
                        input=spec.name,
                    )
                value = options[0]
        elif widget == "image_upload":
            ref = ImageRef.from_wire(value) if value is not None else None
            if value is not None and ref is None:
                bag.add(node_id, "invalid_value", "Not an uploaded image", input=spec.name)
            value = ref
        values[spec.name] = value
    return Resolved(values=values, entry=entry)


def _filled(spec: InputSpec, value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def static_image_count(index: GraphIndex, resolved: dict[str, Resolved], node_id: str) -> int:
    """How many images a node's IMAGE output will carry (known before running)."""
    node_type = index.nodes[node_id].get("type")
    if node_type == "load_image":
        return 1
    if node_type == "image":
        res = resolved.get(node_id)
        count = res.values.get("count", 1) if res else 1
        return int(count) if _is_number(count) else 1
    return 0


def incoming_image_count(
    index: GraphIndex, resolved: dict[str, Resolved], node_id: str, input_name: str
) -> int:
    return sum(
        static_image_count(index, resolved, link.src)
        for link in index.incoming.get(node_id, {}).get(input_name, [])
    )


# ── validation ────────────────────────────────────────────────────────────


@dataclass
class Analysis:
    index: GraphIndex
    errors: Errors
    targets: list[str]
    scope: set[str]
    resolved: dict[str, Resolved] = field(default_factory=dict)
    cycle: set[str] = field(default_factory=set)

    @property
    def ok(self) -> bool:
        return not self.errors

    def node_errors(self) -> dict[str, list[dict[str, Any]]]:
        return self.errors.by_node


def analyse(
    doc: dict[str, Any],
    *,
    models: ModelRegistry,
    media_exists: Any,
    to_node: str | None = None,
) -> Analysis:
    errors = Errors()
    index = index_graph(doc, errors)
    cycle = find_cycle_nodes(index)
    for node_id in sorted(cycle):
        errors.add(node_id, "cycle", "This node is part of a loop")
    targets = targets_for(index, to_node, errors)
    scope = ancestors(index, targets)
    analysis = Analysis(index=index, errors=errors, targets=targets, scope=scope, cycle=cycle)

    for node_id in index.order:
        schema = index.schema(node_id)
        if schema is None:
            continue
        linked = set(index.incoming.get(node_id, {}))
        in_scope = node_id in scope
        res = resolve_values(
            node_id,
            schema,
            index.nodes[node_id].get("values") or {},
            models,
            linked=linked,
            errors=errors if in_scope else None,
        )
        analysis.resolved[node_id] = res

    for node_id in index.order:
        if node_id not in scope:
            continue
        schema = index.schema(node_id)
        if schema is None:
            continue
        res = analysis.resolved[node_id]
        linked = set(index.incoming.get(node_id, {}))
        for spec in schema.inputs:
            if spec.optional or spec.name in linked:
                continue
            if spec.widget is None:
                errors.add(node_id, "required", f"Connect {spec.name}", input=spec.name)
                continue
            if spec.widget == "model" or _hidden(spec, res.entry):
                continue
            if not _filled(spec, res.values.get(spec.name)):
                errors.add(node_id, "required", f"{spec.name} is required", input=spec.name)
        if schema.id == "load_image":
            upload = res.values.get("image")
            if isinstance(upload, ImageRef) and not media_exists(upload.sha256):
                errors.add(node_id, "missing_image", "The uploaded image is gone", input="image")
        if schema.id == "image" and res.entry is not None:
            refs = incoming_image_count(index, analysis.resolved, node_id, "images")
            if refs > 0 and not res.entry.edit:
                errors.add(
                    node_id,
                    "edit_unsupported",
                    f"{res.entry.display} cannot edit images",
                    input="images",
                )
            if refs == 0 and not res.entry.t2i:
                errors.add(
                    node_id,
                    "t2i_unsupported",
                    f"{res.entry.display} needs at least one input image",
                    input="images",
                )
            if refs > res.entry.max_images:
                errors.add(
                    node_id,
                    "too_many_images",
                    f"{res.entry.display} takes at most {res.entry.max_images} images, got {refs}",
                    input="images",
                )
    return analysis


# ── plan ──────────────────────────────────────────────────────────────────


def output_digest(outputs: dict[str, Any]) -> str:
    """Content hash of a node's outputs (image sha256s, text)."""
    return sha(canonical({"outputs": outputs_to_json(outputs)}))


def outputs_to_json(outputs: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, value in outputs.items():
        if isinstance(value, list):
            out[name] = [v.to_wire() if isinstance(v, ImageRef) else v for v in value]
        else:
            out[name] = value
    return out


def outputs_from_json(schema: NodeSchema, raw: dict[str, Any]) -> dict[str, Any] | None:
    out: dict[str, Any] = {}
    for spec in schema.outputs:
        if spec.name not in raw:
            return None
        value = raw[spec.name]
        if spec.type == IMAGE:
            if not isinstance(value, list):
                return None
            refs = [ImageRef.from_wire(item) for item in value]
            if any(ref is None for ref in refs):
                return None
            out[spec.name] = refs
        else:
            out[spec.name] = str(value)
    return out


def _key_values(schema: NodeSchema, values: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, value in values.items():
        if schema.id == "image" and name == "variation":
            continue
        out[name] = value.sha256 if isinstance(value, ImageRef) else value
    return out


@dataclass
class NodePlan:
    node_id: str
    type: str
    values: dict[str, Any]
    entry: ModelEntry | None
    key: str
    stable_key: str
    state: str = "run"  # "run" | "cached" | "idle" (outside the walk)
    outputs: dict[str, Any] | None = None  # known before running (cached or pure)
    digest: str | None = None
    distance: int = 0
    refs: int = 0


@dataclass
class Plan:
    run_id: str
    targets: list[str]
    topo: list[str]
    nodes: dict[str, NodePlan]
    index: GraphIndex

    @property
    def will_run(self) -> list[str]:
        return [node_id for node_id in self.topo if self.nodes[node_id].state == "run"]

    @property
    def cached(self) -> list[str]:
        return [node_id for node_id in self.topo if self.nodes[node_id].state == "cached"]


def node_key(
    schema: NodeSchema,
    values: dict[str, Any],
    inputs: dict[str, list[str] | None],
) -> str:
    return sha(
        canonical({"type": schema.id, "values": _key_values(schema, values), "inputs": inputs})
    )


def input_refs(
    index: GraphIndex, node_id: str, schema: NodeSchema, ref_of: dict[str, str]
) -> dict[str, list[str] | None]:
    refs: dict[str, list[str] | None] = {}
    incoming = index.incoming.get(node_id, {})
    for spec in schema.inputs:
        if not spec.has_socket:
            continue
        links = incoming.get(spec.name)
        if not links:
            refs[spec.name] = None
            continue
        flat: list[str] = []
        for link in links:
            flat.extend([ref_of[link.src], link.out])
        refs[spec.name] = flat
    return refs


def gather_args(
    index: GraphIndex,
    node_id: str,
    schema: NodeSchema,
    values: dict[str, Any],
    outputs_of: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Node args from upstream outputs + widget values; ``None`` if an input is unknown."""
    args = dict(values)
    for name, links in index.incoming.get(node_id, {}).items():
        spec = schema.input(name)
        if spec is None:
            continue
        collected: list[Any] = []
        text: str | None = None
        for link in links:
            upstream = outputs_of.get(link.src)
            if upstream is None or link.out not in upstream:
                return None
            value = upstream[link.out]
            if spec.type == IMAGE:
                collected.extend(value)
            else:
                text = str(value)
        args[name] = collected if spec.type == IMAGE else (text or "")
    return args


def build_plan(
    analysis: Analysis,
    *,
    run_id: str,
    cache_lookup: Any,
    media_exists: Any,
) -> Plan:
    """Keys, cache hits and the walk from the targets. ``analysis`` must be ok."""
    index = analysis.index
    scope = analysis.scope
    topo = [node_id for node_id in _topological(index) if node_id in scope]
    plans: dict[str, NodePlan] = {}
    ref_of: dict[str, str] = {}
    known: dict[str, dict[str, Any]] = {}
    for node_id in topo:
        cls = index.node_class(node_id)
        assert cls is not None
        schema = cls.schema
        res = analysis.resolved[node_id]
        inputs = input_refs(index, node_id, schema, ref_of)
        stable = node_key(schema, res.values, inputs)
        key = stable
        if cls.always_run or (schema.id == "image" and res.values.get("variation") == "new"):
            key = sha(f"{stable}:{run_id}")
        plan = NodePlan(
            node_id=node_id,
            type=schema.id,
            values=res.values,
            entry=res.entry,
            key=key,
            stable_key=stable,
            refs=incoming_image_count(index, analysis.resolved, node_id, "images")
            if schema.id == "image"
            else 0,
        )
        hit = None if cls.always_run else cache_lookup(key)
        if hit is not None:
            decoded = outputs_from_json(schema, hit.get("outputs") or {})
            blobs = [str(b) for b in hit.get("artifact_ids") or []]
            if decoded is not None and all(media_exists(b) for b in blobs):
                plan.outputs = decoded
                plan.state = "cached"
        if plan.outputs is None and cls.pure:
            args = gather_args(index, node_id, schema, res.values, known)
            if args is not None:
                try:
                    plan.outputs = cls().evaluate(args)
                except NodeError:
                    plan.outputs = None
        if plan.outputs is not None:
            known[node_id] = plan.outputs
            plan.digest = output_digest(plan.outputs)
            ref_of[node_id] = "c:" + plan.digest
        else:
            ref_of[node_id] = "k:" + key
        plans[node_id] = plan

    # Walk up from the targets; a cached node stops the walk.
    for plan in plans.values():
        if plan.state != "cached":
            plan.state = "idle"
    distance: dict[str, int] = {}
    queue = deque((target, 0) for target in analysis.targets)
    while queue:
        node_id, dist = queue.popleft()
        if node_id in distance:
            continue
        distance[node_id] = dist
        plan = plans[node_id]
        plan.distance = dist
        if plan.state == "cached":
            continue
        plan.state = "run"
        for upstream in index.upstream(node_id):
            queue.append((upstream, dist + 1))
    for plan in plans.values():
        if plan.node_id not in distance:
            plan.state = "idle"
    walked = [node_id for node_id in topo if node_id in distance]
    return Plan(run_id=run_id, targets=analysis.targets, topo=walked, nodes=plans, index=index)


def _topological(index: GraphIndex) -> list[str]:
    indegree = {node_id: 0 for node_id in index.nodes}
    for link in index.links:
        indegree[link.dst] += 1
    queue = deque(node_id for node_id in index.order if indegree[node_id] == 0)
    order: list[str] = []
    while queue:
        node_id = queue.popleft()
        order.append(node_id)
        for link in index.outgoing.get(node_id, []):
            indegree[link.dst] -= 1
            if indegree[link.dst] == 0:
                queue.append(link.dst)
    return order


# ── estimate ──────────────────────────────────────────────────────────────


def node_price(
    schema: NodeSchema,
    res: Resolved,
    *,
    refs: int,
) -> dict[str, Any]:
    if schema.price_depends_on is None:
        return price_free()
    if schema.id == "image":
        if res.entry is None:
            return price_unknown()
        count = res.values.get("count", 1)
        return res.entry.estimate(
            res.values, count=int(count) if _is_number(count) else 1, refs=refs
        )
    return price_usd(LLM_NODE_ESTIMATE_USD, approximate=True)


def estimate(analysis: Analysis, plan: Plan | None) -> dict[str, Any]:
    """Prices for every known node; the total covers ``will_run`` minus ``cached``."""
    index = analysis.index
    prices: dict[str, dict[str, Any]] = {}
    for node_id in index.order:
        schema = index.schema(node_id)
        res = analysis.resolved.get(node_id)
        if schema is None or res is None:
            continue
        refs = incoming_image_count(index, analysis.resolved, node_id, "images")
        prices[node_id] = node_price(schema, res, refs=refs)
    will_run = plan.will_run if plan is not None else []
    cached = plan.cached if plan is not None else []
    total = sum_prices([prices[node_id] for node_id in will_run if node_id in prices])
    return {"nodes": prices, "total": total, "will_run": will_run, "cached": cached}


# ── public inputs ─────────────────────────────────────────────────────────


def _coerce(spec: InputSpec | None, value: Any) -> Any:
    if spec is None or not isinstance(value, str):
        return value
    if spec.widget == "number":
        try:
            number = float(value)
        except ValueError:
            return value
        return int(number) if number.is_integer() else number
    if spec.widget == "toggle":
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off"):
            return False
    return value


def apply_inputs(doc: dict[str, Any], inputs: dict[str, Any] | None) -> dict[str, Any]:
    """Return a copy of *doc* with its public inputs overridden.

    Only names declared in ``doc["inputs"]`` may be set; anything else is
    ``flows.invalid``. Number and toggle widgets accept their string forms
    (``"4"``, ``"true"``) so the CLI can pass ``--input count=4``.
    """
    import agentos.flows.nodes  # noqa: F401 - registers the built-in nodes

    if not inputs:
        return doc
    if not isinstance(inputs, dict):
        raise FlowError("flows.invalid", "inputs must be an object", details={"node_errors": {}})
    public = doc.get("inputs") or {}
    copy: dict[str, Any] = json.loads(json.dumps(doc))
    by_id = {str(node.get("id")): node for node in copy.get("nodes") or []}
    for name, value in inputs.items():
        target = public.get(name)
        if not isinstance(target, str) or "." not in target:
            allowed = ", ".join(sorted(public)) or "none"
            raise FlowError(
                "flows.invalid",
                f"{name!r} is not a public input of this flow (public inputs: {allowed})",
                details={"node_errors": {}},
            )
        node_id, _, input_name = target.partition(".")
        node = by_id.get(node_id)
        if node is None:
            raise FlowError(
                "flows.invalid",
                f"public input {name!r} points at a missing node {node_id}",
                details={"node_errors": {}},
            )
        cls = NODE_TYPES.get(str(node.get("type")))
        spec = cls.schema.input(input_name) if cls is not None else None
        if spec is None or spec.widget is None:
            raise FlowError(
                "flows.invalid",
                f"public input {name!r} points at {target}, which has no value to set",
                details={"node_errors": {}},
            )
        node.setdefault("values", {})[input_name] = _coerce(spec, value)
    return copy
