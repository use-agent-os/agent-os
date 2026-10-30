"""Starting graphs for ``flows.create``."""

from __future__ import annotations

import copy
from typing import Any

from agentos.flows.types import FLOW_VERSION, FlowError

TEMPLATES = ("blank", "text_to_image", "edit_image", "describe_remix")


def _node(node_id: str, node_type: str, x: int, y: int, **values: Any) -> dict[str, Any]:
    return {"id": node_id, "type": node_type, "pos": [x, y], "title": None, "values": values}


def _link(link_id: str, src: str, out: str, dst: str, inp: str) -> dict[str, Any]:
    return {"id": link_id, "from": [src, out], "to": [dst, inp]}


_GRAPHS: dict[str, dict[str, Any]] = {
    "blank": {"nodes": [], "links": [], "inputs": {}},
    "text_to_image": {
        "nodes": [
            _node(
                "n1",
                "text",
                40,
                80,
                text="A lighthouse on a rocky coast at dusk, warm window light, cinematic",
            ),
            _node("n2", "image", 400, 40, model="auto", count=1, variation="new"),
            _node("n3", "save", 780, 80),
        ],
        "links": [
            _link("l1", "n1", "text", "n2", "prompt"),
            _link("l2", "n2", "image", "n3", "images"),
        ],
        "inputs": {"prompt": "n1.text", "count": "n2.count"},
    },
    "edit_image": {
        "nodes": [
            _node("n1", "load_image", 40, 40, image=None),
            _node("n2", "text", 40, 300, text="Turn this into a watercolor painting"),
            _node("n3", "image", 400, 80, model="auto", count=1, variation="new"),
            _node("n4", "save", 780, 120),
        ],
        "links": [
            _link("l1", "n1", "image", "n3", "images"),
            _link("l2", "n2", "text", "n3", "prompt"),
            _link("l3", "n3", "image", "n4", "images"),
        ],
        "inputs": {"instruction": "n2.text", "count": "n3.count"},
    },
    "describe_remix": {
        "nodes": [
            _node("n1", "load_image", 40, 40, image=None),
            _node("n2", "describe_image", 360, 40, focus="everything"),
            _node("n3", "text", 360, 300, text="the same scene as a retro travel poster"),
            _node("n4", "prompt_writer", 700, 120, style="illustration"),
            _node("n5", "image", 1040, 80, model="auto", count=1, variation="new"),
            _node("n6", "save", 1400, 120),
        ],
        "links": [
            _link("l1", "n1", "image", "n2", "image"),
            _link("l2", "n2", "text", "n4", "context"),
            _link("l3", "n3", "text", "n4", "idea"),
            _link("l4", "n4", "text", "n5", "prompt"),
            _link("l5", "n5", "image", "n6", "images"),
        ],
        "inputs": {"idea": "n3.text", "count": "n5.count"},
    },
}


def template_doc(template: str | None, name: str) -> dict[str, Any]:
    """A new (unsaved, id-less) flow document from *template*."""
    key = template or "blank"
    graph = _GRAPHS.get(key)
    if graph is None:
        raise FlowError(
            "flows.invalid",
            f"Unknown template {key!r}; use one of {', '.join(TEMPLATES)}",
            details={"node_errors": {}},
        )
    doc = copy.deepcopy(graph)
    doc.update({"version": FLOW_VERSION, "name": name, "viewport": {"x": 0, "y": 0, "zoom": 1}})
    return doc
