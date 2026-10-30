"""Shared doubles for the Image Flows tests: no network, no keys."""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator
from typing import Any

import pytest

from agentos.flows.adapters import AdapterRequest, AdapterResult, TextResult
from agentos.flows.models import ModelEntry, ModelRegistry
from agentos.flows.service import FlowService

COLORS = ["teal", "orange", "navy", "gold", "plum", "olive", "salmon", "black", "white"]


def png_bytes(color: str = "teal", size: tuple[int, int] = (32, 24)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeImageAdapter:
    """Records calls; every call returns ``n`` distinct images."""

    def __init__(self, *, delay: float = 0.0, cost: float | None = 0.05) -> None:
        self.calls: list[tuple[str, AdapterRequest]] = []
        self.delay = delay
        self.cost = cost
        self.active: dict[str, int] = {}
        self.peak: dict[str, int] = {}
        self.fail_with: BaseException | None = None
        self.empty_text: str | None = None
        self.gate: asyncio.Event | None = None
        self._counter = 0

    async def generate(self, entry: ModelEntry, request: AdapterRequest) -> AdapterResult:
        self.calls.append((entry.id, request))
        provider = entry.provider
        self.active[provider] = self.active.get(provider, 0) + 1
        self.peak[provider] = max(self.peak.get(provider, 0), self.active[provider])
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.fail_with is not None:
                raise self.fail_with
            if self.empty_text is not None:
                return AdapterResult(images=[], text=self.empty_text)
            images = []
            for _ in range(request.n):
                self._counter += 1
                images.append((png_bytes(COLORS[self._counter % len(COLORS)]), "image/png"))
            return AdapterResult(images=images, cost_usd=self.cost)
        finally:
            self.active[provider] -= 1


class FakeTextAdapter:
    def __init__(self, reply: str = "a detailed prompt") -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    async def complete(self, **kwargs: Any) -> TextResult:
        self.calls.append(kwargs)
        return TextResult(text=self.reply)


def fake_auth(*available: str):
    def probe(provider: str) -> tuple[bool, str | None]:
        if provider in available:
            return True, None
        return False, f"{provider.upper()}_API_KEY is not set"

    return probe


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, event: str, payload: dict[str, Any]) -> None:
        self.events.append((event, payload))

    def for_run(self, run_id: str) -> list[tuple[str, dict[str, Any]]]:
        return [(e, p) for e, p in self.events if p.get("run_id") == run_id]


@pytest.fixture
def images() -> FakeImageAdapter:
    return FakeImageAdapter()


@pytest.fixture
def texts() -> FakeTextAdapter:
    return FakeTextAdapter()


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def models() -> ModelRegistry:
    return ModelRegistry(auth=fake_auth("openrouter", "openai"))


@pytest.fixture
async def service(
    tmp_path, images: FakeImageAdapter, texts: FakeTextAdapter, recorder: Recorder, models
) -> AsyncIterator[FlowService]:
    svc = FlowService(
        None,
        root=tmp_path / "flows",
        broadcast=recorder,
        image_adapter=images,
        text_adapter=texts,
        models=models,
    )
    svc.start()
    yield svc
    await svc.close()


def node(node_id: str, node_type: str, **values: Any) -> dict[str, Any]:
    return {"id": node_id, "type": node_type, "pos": [0, 0], "values": values}


def link(link_id: str, src: str, out: str, dst: str, inp: str) -> dict[str, Any]:
    return {"id": link_id, "from": [src, out], "to": [dst, inp]}


def doc(nodes: list[dict[str, Any]], links: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "version": 1,
        "id": "flw_test0001",
        "name": "Test",
        "rev": 1,
        "nodes": nodes,
        "links": links,
        "inputs": {},
    }
    out.update(extra)
    return out


def t2i_doc(**image_values: Any) -> dict[str, Any]:
    values = {"model": "openrouter/nano-banana-2", "count": 1, "variation": "keep"}
    values.update(image_values)
    return doc(
        [node("t", "text", text="a lighthouse"), node("i", "image", **values), node("s", "save")],
        [link("l1", "t", "text", "i", "prompt"), link("l2", "i", "image", "s", "images")],
        inputs={"prompt": "t.text", "count": "i.count"},
    )


async def stored(service: FlowService, graph: dict[str, Any]) -> dict[str, Any]:
    """Create a flow and save *graph* into it; returns the stored doc."""
    created = await service.create_flow(graph.get("name") or "Test")
    body = dict(graph)
    body["id"] = created["id"]
    return await service.save_flow(body, created["rev"])
