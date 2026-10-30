"""Run one planned flow: schedule, execute, commit (contract section 7.3-7.5).

Ready nodes (Kahn) run concurrently, closest-to-target first; provider calls
are throttled by the service's per-provider slots. The first node error stops
scheduling: in-flight nodes finish (and are cached), everything not started
is ``skipped`` and the run is ``failed``. Cancelling the run task cancels the
in-flight nodes and ends the run ``cancelled`` (or ``failed`` with
``flows.interrupted`` when the gateway is shutting down).

Every run owns its :class:`RunHandle`; nothing here is process-global, so an
event can never be attributed to the wrong run.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import structlog

from agentos.flows.adapters import ImageAdapter, TextAdapter
from agentos.flows.media import MediaStore
from agentos.flows.models import ModelRegistry
from agentos.flows.planner import (
    Plan,
    gather_args,
    input_refs,
    node_key,
    output_digest,
    outputs_to_json,
)
from agentos.flows.schema import NodeContext, NodeResult
from agentos.flows.types import IMAGE, ImageRef, NodeError, price_upper_usd, utc_now_iso

log = structlog.get_logger(__name__)

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass
class RunHandle:
    """In-memory state of one run (the persisted copy lives in ``flow_runs``)."""

    run_id: str
    flow_id: str
    flow_name: str
    rev: int
    record: dict[str, Any]
    plan: Plan
    estimate: dict[str, Any]
    origin: str = "canvas"
    session_key: str | None = None
    artifact_sink: Callable[[Path, str], dict[str, Any] | None] | None = None
    seq: int = 0
    task: asyncio.Task[None] | None = None
    cancel_reason: str | None = None  # "shutdown" (the task was cancelled)
    #: Set by ``stop_run``: the run ends ``cancelled`` at once, in-flight calls finish.
    stop_requested: asyncio.Event = field(default_factory=asyncio.Event)
    #: Set when the run is over for its callers (done, failed, or cancelled).
    finished: asyncio.Event = field(default_factory=asyncio.Event)

    def next_seq(self) -> int:
        """The next event number; the record always carries the last one emitted."""
        self.seq += 1
        self.record["seq"] = self.seq
        return self.seq


@dataclass
class ExecutorDeps:
    media: MediaStore
    models: ModelRegistry
    image_adapter: ImageAdapter
    text_adapter: TextAdapter
    outputs_root: Path
    emit: Emit
    persist: Callable[[RunHandle, dict[str, Any]], None]
    provider_slot: Callable[[str], Any]
    cache_put: Callable[[str, str, dict[str, Any], list[str], int], None]
    record_cost: Callable[[RunHandle, str, float, Any], None]
    #: Called once when a stop has ended the run (frees its queue slot).
    on_stopped: Callable[[RunHandle], None] = lambda handle: None


def wire_outputs(schema_outputs: Any, outputs: dict[str, Any], extra: dict[str, Any]) -> dict:
    """``{"images"?: [ImageRef], "text"?: str}`` (+ Save's ``files``/``artifacts``)."""
    wire: dict[str, Any] = {}
    for spec in schema_outputs:
        if spec.name not in outputs:
            continue
        value = outputs[spec.name]
        if spec.type == IMAGE:
            wire["images"] = [ref.to_wire() for ref in value]
        else:
            wire["text"] = value
    wire.update(extra)
    return wire


def produced_images(
    graph: Any, nodes: dict[str, Any] | None, outputs: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """The images a run made, unique by sha256: Save outputs first, then the rest.

    An ``image`` node made something new only if it executed in this run (state
    ``done``; a ``cached`` node made nothing). A Save output counts only when the
    image it wrote came from such a node: re-saving a cached or uploaded image
    yields a new sha (the embedded run metadata) but no new picture. Provenance
    is positional: Save writes its inputs in link order, one file per image, so
    ``saved[i]`` comes from the i-th incoming image. An executed image that a
    counted Save wrote is represented by the saved file, not listed twice.
    """
    if not isinstance(graph, dict):
        return []
    nodes = nodes or {}
    outputs = outputs or {}
    types = {str(n.get("id")): n.get("type") for n in graph.get("nodes") or []}

    def executed_image(node_id: str) -> bool:
        state = (nodes.get(node_id) or {}).get("state")
        return types.get(node_id) == "image" and state == "done"

    saved: list[dict[str, Any]] = []
    covered: set[str] = set()
    for node_id, node_type in types.items():
        if node_type != "save" or node_id not in outputs:
            continue
        sources: list[tuple[str, dict[str, Any]]] = []
        for link in graph.get("links") or []:
            if link.get("to", [None])[0] == node_id:
                upstream_id = str(link["from"][0])
                upstream = outputs.get(upstream_id) or {}
                sources.extend((upstream_id, ref) for ref in upstream.get("images") or [])
        written = outputs[node_id].get("images") or []
        for index, saved_ref in enumerate(written):
            if index >= len(sources):
                break
            source_id, source_ref = sources[index]
            if executed_image(source_id):
                saved.append(saved_ref)
                covered.add(str(source_ref.get("sha256")))
    produced: list[dict[str, Any]] = []
    for node_id in types:
        if executed_image(node_id) and node_id in outputs:
            produced.extend(
                ref
                for ref in outputs[node_id].get("images") or []
                if ref.get("sha256") not in covered
            )
    unique: dict[str, dict[str, Any]] = {}
    for ref in saved + produced:
        sha = ref.get("sha256")
        if isinstance(sha, str) and sha not in unique:
            unique[sha] = ref
    return list(unique.values())


def _node_cost(plan_price: dict[str, Any] | None, actual: float | None) -> float | None:
    """Provider-reported cost when there is one, else the estimate (range: its max)."""
    if actual is not None:
        return float(actual)
    if plan_price is None:
        return None
    return price_upper_usd(plan_price)


class RunExecutor:
    def __init__(self, handle: RunHandle, deps: ExecutorDeps) -> None:
        self.handle = handle
        self.deps = deps
        self.plan = handle.plan
        self.index = handle.plan.index
        self.nodes: dict[str, dict[str, Any]] = handle.record["nodes"]
        self.outputs_wire: dict[str, dict[str, Any]] = handle.record["outputs"]
        self.outputs_of: dict[str, dict[str, Any]] = {}
        self.digest_of: dict[str, str] = {}
        self._save_counter = 0
        self._failed = False
        self._stopped = False
        self._after_stop = 0  # nodes that completed after the stop

    # ── events / persistence ────────────────────────────────────────────

    async def _emit_run(self, **extra: Any) -> None:
        record = self.handle.record
        payload: dict[str, Any] = {
            "run_id": self.handle.run_id,
            "flow_id": self.handle.flow_id,
            "seq": self.handle.next_seq(),
            "status": record["status"],
        }
        if record.get("est_usd") is not None:
            payload["est_usd"] = record["est_usd"]
        if record.get("cost_usd") is not None:
            payload["cost_usd"] = record["cost_usd"]
        if record.get("error"):
            payload["error"] = record["error"]
        payload.update(extra)
        self._persist()
        await self.deps.emit("flows.run", payload)

    async def _emit_node(self, node_id: str) -> None:
        state = self.nodes[node_id]
        payload: dict[str, Any] = {
            "run_id": self.handle.run_id,
            "flow_id": self.handle.flow_id,
            "seq": self.handle.next_seq(),
            "node_id": node_id,
            **state,
        }
        if state["state"] in ("done", "cached") and node_id in self.outputs_wire:
            payload["outputs"] = self.outputs_wire[node_id]
        self._persist()
        await self.deps.emit("flows.node", payload)

    def _persist(self) -> None:
        record = self.handle.record
        record["seq"] = self.handle.seq
        self.deps.persist(
            self.handle,
            {
                "status": record["status"],
                "started": record.get("started"),
                "ended": record.get("ended"),
                "nodes": record["nodes"],
                "outputs": record["outputs"],
                "error": record.get("error"),
                "cost_usd": record.get("cost_usd"),
                "preview": record.get("preview"),
                "image_count": record.get("image_count", 0),
                "seq": self.handle.seq,
            },
        )

    # ── main loop ───────────────────────────────────────────────────────

    async def run(self) -> None:
        record = self.handle.record
        record["status"] = "running"
        record["started"] = utc_now_iso()
        await self._emit_run()
        for node_id in self.plan.cached:
            node_plan = self.plan.nodes[node_id]
            outputs = node_plan.outputs or {}
            self.outputs_of[node_id] = outputs
            self.digest_of[node_id] = node_plan.digest or output_digest(outputs)
            cls = self.index.node_class(node_id)
            assert cls is not None
            self.outputs_wire[node_id] = wire_outputs(cls.schema.outputs, outputs, {})
            self.nodes[node_id] = {"state": "cached"}
            await self._emit_node(node_id)

        running: dict[asyncio.Task[NodeResult], tuple[str, float]] = {}
        try:
            await self._schedule(running)
        except asyncio.CancelledError:
            await self._cancel_in_flight(running)
            await self._finish_cancelled()
            if self.handle.cancel_reason is None:
                raise
            return
        if self._stopped:
            await self._finish_after_stop()
        else:
            await self._finish()

    async def _schedule(self, running: dict[asyncio.Task[NodeResult], tuple[str, float]]) -> None:
        will_run = self.plan.will_run
        pending = set(will_run)
        order = {node_id: i for i, node_id in enumerate(self.plan.topo)}
        deps = {
            node_id: {u for u in self.index.upstream(node_id) if u in pending}
            for node_id in will_run
        }
        ready = [node_id for node_id in will_run if not deps[node_id]]
        stop_waiter = asyncio.ensure_future(self.handle.stop_requested.wait())
        try:
            await self._loop(running, pending, ready, deps, order, stop_waiter)
        finally:
            stop_waiter.cancel()
        if self._stopped:
            return
        # Anything never started: skipped after an error, or a stall (cannot
        # happen for a validated acyclic plan; guarded so a run never hangs).
        if pending and not self._failed:
            self._fail_run(None, {"code": "flows.stalled", "message": "No node could be scheduled"})
        for node_id in will_run:
            if node_id in pending:
                self.nodes[node_id] = {"state": "skipped"}
                await self._emit_node(node_id)

    async def _loop(
        self,
        running: dict[asyncio.Task[NodeResult], tuple[str, float]],
        pending: set[str],
        ready: list[str],
        deps: dict[str, set[str]],
        order: dict[str, int],
        stop_waiter: asyncio.Future[Any],
    ) -> None:
        while True:
            if self.handle.stop_requested.is_set() and not self._stopped:
                await self._stop_now(pending)
            if not self._failed and not self._stopped:
                ready.sort(key=lambda n: (self.plan.nodes[n].distance, order[n]))
                for node_id in ready:
                    pending.discard(node_id)
                    self.nodes[node_id] = {"state": "running"}
                    await self._emit_node(node_id)
                    task = asyncio.create_task(self._run_node(node_id))
                    running[task] = (node_id, time.monotonic())
                ready.clear()
            if not running:
                break
            waiting: set[asyncio.Future[Any]] = set(running)
            if not self._stopped:
                waiting.add(stop_waiter)
            done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
            if stop_waiter in done:
                done.discard(stop_waiter)
                if not self._stopped:
                    await self._stop_now(pending)
            for future in done:
                node_task = cast("asyncio.Task[NodeResult]", future)
                node_id, started = running.pop(node_task)
                ms = int((time.monotonic() - started) * 1000)
                await self._complete(node_id, node_task, ms)
                if self.nodes[node_id]["state"] != "done":
                    continue
                for other in list(pending):
                    blockers = deps.get(other)
                    if blockers and node_id in blockers:
                        blockers.discard(node_id)
                        if not blockers:
                            ready.append(other)

    async def _stop_now(self, pending: set[str]) -> None:
        """End the run ``cancelled`` now; nodes already running keep going, detached.

        Their provider calls are not aborted (an aborted call is still billed):
        each finishes, is cached, and has its real cost added to the node and to
        the run's ``cost_usd`` afterwards. Nothing new is scheduled.
        """
        self._stopped = True
        for node_id in sorted(pending):
            self.nodes[node_id] = {"state": "skipped"}
            await self._emit_node(node_id)
        pending.clear()
        record = self.handle.record
        record["status"] = "cancelled"
        record["ended"] = utc_now_iso()
        self._summarise_outputs()
        await self._emit_run()
        self.deps.on_stopped(self.handle)

    async def _run_node(self, node_id: str) -> NodeResult:
        node_plan = self.plan.nodes[node_id]
        cls = self.index.node_class(node_id)
        assert cls is not None
        args = gather_args(self.index, node_id, cls.schema, node_plan.values, self.outputs_of)
        if args is None:
            raise NodeError("internal", "An input was not ready")

        async def progress(value: int, maximum: int) -> None:
            state = self.nodes.get(node_id)
            if state is None or state.get("state") != "running":
                return
            state["progress"] = {"value": int(value), "max": int(maximum)}
            await self._emit_node(node_id)

        def save_index() -> int:
            self._save_counter += 1
            return self._save_counter

        ctx = NodeContext(
            run_id=self.handle.run_id,
            flow_id=self.handle.flow_id,
            flow_name=self.handle.flow_name,
            rev=self.handle.rev,
            node_id=node_id,
            media=self.deps.media,
            models=self.deps.models,
            image_adapter=self.deps.image_adapter,
            text_adapter=self.deps.text_adapter,
            outputs_dir=self.deps.outputs_root / self.handle.flow_id,
            graph=self.handle.record["graph"],
            session_key=self.handle.session_key,
            progress=progress,
            provider_slot=self.deps.provider_slot,
            artifact_sink=self.handle.artifact_sink,
            save_index=save_index,
        )
        return await cls().run(ctx, args)

    async def _complete(self, node_id: str, task: asyncio.Task[NodeResult], ms: int) -> None:
        cls = self.index.node_class(node_id)
        assert cls is not None
        try:
            result = task.result()
        except asyncio.CancelledError:
            self.nodes[node_id] = {"state": "skipped", "ms": ms}
            await self._emit_node(node_id)
            return
        except NodeError as failure:
            self._node_error(node_id, failure.to_wire(), ms)
            await self._emit_node(node_id)
            return
        except Exception as exc:  # noqa: BLE001 - a node bug must fail the run, not the loop
            from agentos.redact import redact_sensitive_text

            log.warning("flows.node_crashed", node=node_id, error=str(exc), exc_info=True)
            message = redact_sensitive_text(str(exc)) or exc.__class__.__name__
            self._node_error(node_id, {"code": "internal", "message": message}, ms)
            await self._emit_node(node_id)
            return

        self.outputs_of[node_id] = result.outputs
        digest = output_digest(result.outputs)
        self.digest_of[node_id] = digest
        self.outputs_wire[node_id] = wire_outputs(cls.schema.outputs, result.outputs, result.extra)
        price = self.handle.estimate.get("nodes", {}).get(node_id)
        cost = _node_cost(price, result.cost_usd) if cls.schema.is_api else None
        state: dict[str, Any] = {"state": "done", "ms": ms}
        if cost is not None:
            state["cost_usd"] = round(cost, 6)
            self.handle.record["cost_usd"] = round(
                float(self.handle.record.get("cost_usd") or 0.0) + cost, 6
            )
            try:
                self.deps.record_cost(self.handle, node_id, cost, self.plan.nodes[node_id].entry)
            except Exception:  # noqa: BLE001 - accounting never fails a run
                log.debug("flows.cost_record_failed", node=node_id, exc_info=True)
        self.nodes[node_id] = state
        if not cls.always_run:
            self._commit_cache(node_id, result)
        if self._stopped:
            self._after_stop += 1
            self._summarise_outputs()
        await self._emit_node(node_id)

    def _commit_cache(self, node_id: str, result: NodeResult) -> None:
        cls = self.index.node_class(node_id)
        assert cls is not None
        node_plan = self.plan.nodes[node_id]
        refs = {u: "c:" + self.digest_of[u] for u in self.index.upstream(node_id)}
        key = node_key(
            cls.schema, node_plan.values, input_refs(self.index, node_id, cls.schema, refs)
        )
        blobs: list[str] = []
        size = 0
        for value in result.outputs.values():
            if isinstance(value, list):
                for ref in value:
                    if isinstance(ref, ImageRef):
                        blobs.append(ref.sha256)
                        size += ref.bytes
            elif isinstance(value, str):
                size += len(value.encode("utf-8"))
        try:
            self.deps.cache_put(key, cls.schema.id, outputs_to_json(result.outputs), blobs, size)
        except Exception:  # noqa: BLE001 - a cache write never fails a run
            log.debug("flows.cache_put_failed", node=node_id, exc_info=True)

    def _node_error(self, node_id: str, error: dict[str, Any], ms: int) -> None:
        self.nodes[node_id] = {"state": "error", "ms": ms, "error": error}
        self._fail_run(node_id, error)

    def _fail_run(self, node_id: str | None, error: dict[str, Any]) -> None:
        if self._stopped:
            return  # a detached call failing after Stop does not turn the run into "failed"
        if not self._failed:
            run_error = dict(error)
            if node_id is not None:
                run_error["node_id"] = node_id
            self.handle.record["error"] = run_error
        self._failed = True

    async def _cancel_in_flight(
        self, running: dict[asyncio.Task[NodeResult], tuple[str, float]]
    ) -> None:
        for task in running:
            task.cancel()
        if running:
            await asyncio.gather(*running.keys(), return_exceptions=True)
        for node_id, state in self.nodes.items():
            if state.get("state") in ("running", "queued"):
                self.nodes[node_id] = {"state": "skipped"}
        running.clear()

    def _summarise_outputs(self) -> None:
        record = self.handle.record
        images = produced_images(record.get("graph"), record["nodes"], record["outputs"])
        record["images"] = images
        record["image_count"] = len(images)
        record["preview"] = images[0] if images else None

    async def _finish(self) -> None:
        record = self.handle.record
        record["status"] = "failed" if self._failed else "done"
        record["ended"] = utc_now_iso()
        self._summarise_outputs()
        await self._emit_run()

    async def _finish_after_stop(self) -> None:
        """Detached calls are all back: publish the corrected cost once."""
        if self._after_stop:
            await self._emit_run()
        else:
            self._persist()

    async def _finish_cancelled(self) -> None:
        record = self.handle.record
        if self._stopped:
            # Shutdown while stopped calls were still out: the run stays cancelled.
            pass
        elif self.handle.cancel_reason == "shutdown":
            record["status"] = "failed"
            record["error"] = {
                "code": "flows.interrupted",
                "message": "The gateway stopped while this run was in progress",
            }
        else:
            record["status"] = "cancelled"
        if not self._stopped:
            record["ended"] = utc_now_iso()
        self._summarise_outputs()
        for node_id in list(self.nodes):
            if self.nodes[node_id].get("state") in ("running", "queued"):
                self.nodes[node_id] = {"state": "skipped"}
        try:
            await self._emit_run()
        except Exception:  # noqa: BLE001 - shutdown path: persistence first, events best effort
            log.debug("flows.cancel_emit_failed", exc_info=True)
