"""Declarative node schema: what a node takes, what it gives, how it is drawn.

A node is a :class:`FlowNode` subclass with a :class:`NodeSchema` class
attribute and an async ``run``; :func:`register_node` adds it to the catalog.
``NodeSchema.to_wire`` is exactly the ``flows.nodes`` shape of the contract,
so the desktop renders every node generically from it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from agentos.flows.types import LINK_TYPES, SCHEMA_VERSION

if TYPE_CHECKING:
    from agentos.flows.adapters import ImageAdapter, TextAdapter
    from agentos.flows.media import MediaStore
    from agentos.flows.models import ModelRegistry

WIDGETS = ("text", "textarea", "number", "combo", "toggle", "model", "variation", "image_upload")
GROUPS = ("input", "utility", "ai", "output")
OPTIONS_FROM = ("model.ratios", "model.tiers", "model.sizes", "model.qualities")
SHOW_WHEN_CAPS = ("ratios", "tiers", "sizes", "qualities", "transparent_bg")

_UNSET: Any = object()


@dataclass(frozen=True)
class InputSpec:
    name: str
    type: str | None = None
    widget: str | None = None
    socket: bool | None = None
    optional: bool = False
    multi: bool = False
    default: Any = _UNSET
    min: float | None = None
    max: float | None = None
    step: float | None = None
    options: tuple[str, ...] | None = None
    options_from: str | None = None
    show_when: dict[str, str] | None = None
    placeholder: str | None = None
    tooltip: str | None = None
    advanced: bool = False

    def __post_init__(self) -> None:
        if self.widget is not None and self.widget not in WIDGETS:
            raise ValueError(f"unknown widget {self.widget!r} on input {self.name!r}")
        if self.type is not None and self.type not in LINK_TYPES:
            raise ValueError(f"unknown link type {self.type!r} on input {self.name!r}")
        if self.options_from is not None and self.options_from not in OPTIONS_FROM:
            raise ValueError(f"unknown options_from {self.options_from!r}")
        if self.show_when is not None:
            cap = self.show_when.get("model.has")
            if cap not in SHOW_WHEN_CAPS:
                raise ValueError(f"unknown show_when {self.show_when!r}")
        if self.widget is None and self.type is None:
            raise ValueError(f"input {self.name!r} needs a widget or a type")

    @property
    def has_socket(self) -> bool:
        """A typed input has a socket unless it says ``socket: false``."""
        if self.type is None:
            return False
        return True if self.socket is None else bool(self.socket)

    @property
    def has_default(self) -> bool:
        return self.default is not _UNSET

    def default_value(self) -> Any:
        return None if self.default is _UNSET else self.default

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name}
        if self.type is not None:
            out["type"] = self.type
        if self.widget is not None:
            out["widget"] = self.widget
        out["socket"] = self.has_socket
        out["optional"] = self.optional
        out["multi"] = self.multi
        if self.has_default:
            out["default"] = self.default
        for key in ("min", "max", "step", "placeholder", "tooltip", "options_from"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        if self.options is not None:
            out["options"] = list(self.options)
        if self.show_when is not None:
            out["show_when"] = dict(self.show_when)
        out["advanced"] = self.advanced
        return out


@dataclass(frozen=True)
class OutputSpec:
    name: str
    type: str

    def to_wire(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type}


@dataclass(frozen=True)
class NodeSchema:
    id: str
    display: str
    group: str
    description: str
    inputs: tuple[InputSpec, ...] = ()
    outputs: tuple[OutputSpec, ...] = ()
    is_output: bool = False
    is_api: bool = False
    price_depends_on: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.group not in GROUPS:
            raise ValueError(f"unknown group {self.group!r} on node {self.id!r}")
        names = [spec.name for spec in self.inputs]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate input names on node {self.id!r}")

    def input(self, name: str) -> InputSpec | None:
        for spec in self.inputs:
            if spec.name == name:
                return spec
        return None

    def output(self, name: str) -> OutputSpec | None:
        for spec in self.outputs:
            if spec.name == name:
                return spec
        return None

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display": self.display,
            "group": self.group,
            "description": self.description,
            "is_output": self.is_output,
            "is_api": self.is_api,
            "inputs": [spec.to_wire() for spec in self.inputs],
            "outputs": [spec.to_wire() for spec in self.outputs],
            "price": (
                {"depends_on": list(self.price_depends_on)}
                if self.price_depends_on is not None
                else None
            ),
        }


@dataclass
class NodeResult:
    """What a node run produced.

    ``outputs`` maps output name to value (``str`` for TEXT, a list of
    :class:`~agentos.flows.types.ImageRef` for IMAGE). ``extra`` is merged into
    the node's wire outputs (the Save node reports its ``images`` and ``files``
    there). ``cost_usd`` is the actual cost when known.
    """

    outputs: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)
    cost_usd: float | None = None


@dataclass
class NodeContext:
    """Everything a running node may touch. One per node execution."""

    run_id: str
    flow_id: str
    flow_name: str
    rev: int
    node_id: str
    media: MediaStore
    models: ModelRegistry
    image_adapter: ImageAdapter
    text_adapter: TextAdapter
    outputs_dir: Path
    graph: dict[str, Any]
    session_key: str | None
    progress: Callable[[int, int], Awaitable[None]]
    provider_slot: Callable[[str], Any]
    artifact_sink: Callable[[Path, str], dict[str, Any] | None] | None = None
    save_index: Callable[[], int] | None = None


class FlowNode:
    """Base class of every node type."""

    schema: ClassVar[NodeSchema]
    #: Deterministic, free and local: evaluable at plan time (text, join).
    pure: ClassVar[bool] = False
    #: Never served from the cache (Save writes files every run).
    always_run: ClassVar[bool] = False

    async def run(self, ctx: NodeContext, args: dict[str, Any]) -> NodeResult:
        raise NotImplementedError

    def evaluate(self, args: dict[str, Any]) -> dict[str, Any]:
        """Plan-time evaluation for ``pure`` nodes."""
        raise NotImplementedError


NODE_TYPES: dict[str, type[FlowNode]] = {}


def register_node(cls: type[FlowNode]) -> type[FlowNode]:
    """Class decorator: add a node type to the catalog (order is display order)."""
    schema = getattr(cls, "schema", None)
    if not isinstance(schema, NodeSchema):
        raise TypeError(f"{cls.__name__} has no NodeSchema")
    NODE_TYPES[schema.id] = cls
    return cls


def get_node_type(type_id: str) -> type[FlowNode] | None:
    return NODE_TYPES.get(type_id)


def node_catalog_wire() -> dict[str, Any]:
    """The ``flows.nodes`` result."""
    import agentos.flows.nodes  # noqa: F401 - registers the built-in nodes

    return {
        "version": SCHEMA_VERSION,
        "types": {name: dict(spec) for name, spec in LINK_TYPES.items()},
        "nodes": [cls.schema.to_wire() for cls in NODE_TYPES.values()],
    }
