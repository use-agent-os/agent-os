"""``FlowService``: the one object the gateway, the CLI and agent tools talk to.

Owns the flow documents, the run queue (FIFO, ``[flows].max_concurrent_runs``),
the cache, the media store and the event stream. Events leave through an
injected async ``broadcast(event, payload)``; the gateway passes its control
broadcast, tests pass a recorder, and without one events are only logged.

State lives under ``<state_dir>/flows/``:

``<id>.json``          flow documents
``flows.db``           ``flow_runs`` + ``flow_cache``
``media/``             content-addressed blobs (+ ``thumbs/``)
``outputs/<flow_id>/`` PNG files written by Save nodes
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import structlog

from agentos.flows.adapters import (
    ImageAdapter,
    TextAdapter,
    UnwiredImageAdapter,
    UnwiredTextAdapter,
)
from agentos.flows.executor import ExecutorDeps, RunExecutor, RunHandle, produced_images
from agentos.flows.media import MediaStore
from agentos.flows.models import REGISTRY, ModelEntry, ModelRegistry
from agentos.flows.planner import (
    Analysis,
    analyse,
    apply_inputs,
    build_plan,
    estimate,
)
from agentos.flows.pngmeta import read_flow_metadata
from agentos.flows.schema import node_catalog_wire
from agentos.flows.store import (
    FlowDB,
    FlowStore,
    clean_name,
    is_run_id,
    new_run_id,
    normalize_doc,
)
from agentos.flows.surplus import CATALOG_FILENAME, SurplusCatalog
from agentos.flows.templates import template_doc
from agentos.flows.types import (
    RUN_ORIGINS,
    FlowError,
    ImageRef,
    price_upper_usd,
    utc_now_iso,
)

log = structlog.get_logger(__name__)

Broadcast = Callable[[str, dict[str, Any]], Awaitable[None]]
ArtifactSink = Callable[[Path, str], dict[str, Any] | None]
#: ``(tracker, run_id, session_key, cost_usd, model_id, provider_id)``.
CostRecorder = Callable[[Any, str, str | None, float, str, str], None]

#: ``[image_generation].primary``'s default value (not an explicit preference).
BUILTIN_IMAGE_PRIMARY = "openai/gpt-image-1"
#: Runs allowed to wait in the queue before ``flows.run`` answers ``flows.busy``.
MAX_QUEUED_RUNS = 50
#: Concurrent calls per provider (contract 7.3).
PROVIDER_CONCURRENCY = 2
#: Upload size limit for ``flows.upload`` / ``flows.import_png`` (decoded bytes). Its
#: base64 form must fit the gateway's 25 MiB post-handshake WebSocket frame.
MAX_UPLOAD_BYTES = 18 * 1024 * 1024

_SUMMARY_KEYS = (
    "run_id",
    "flow_id",
    "rev",
    "status",
    "created",
    "started",
    "ended",
    "est_usd",
    "cost_usd",
    "preview",
    "origin",
    "error",
    "image_count",
)


async def _default_broadcast(event: str, payload: dict[str, Any]) -> None:
    """No transport wired (tests, CLI-only use): events are logged, not sent."""
    log.debug("flows.event", name=event, run_id=payload.get("run_id"))


def _cfg(config: Any, name: str, default: Any) -> Any:
    flows = getattr(config, "flows", None)
    value = getattr(flows, name, None) if flows is not None else None
    return default if value is None else value


def default_flows_root(config: Any | None) -> Path:
    state = getattr(config, "state_dir", None) if config is not None else None
    if isinstance(state, str) and state.strip():
        return Path(state).expanduser() / "flows"
    from agentos.paths import state_dir

    return state_dir("flows")


def run_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record.get(key) for key in _SUMMARY_KEYS}


def run_wire(record: dict[str, Any]) -> dict[str, Any]:
    """RunRecord: RunSummary + inputs, graph, nodes, outputs (+ seq)."""
    out = run_summary(record)
    out.update(
        {
            "inputs": record.get("inputs") or {},
            "graph": record.get("graph"),
            "nodes": record.get("nodes") or {},
            "outputs": record.get("outputs") or {},
            "to_node": record.get("to_node"),
            "estimate": record.get("estimate"),
            "seq": record.get("seq") or 0,
            "images": produced_images(
                record.get("graph"), record.get("nodes"), record.get("outputs")
            ),
        }
    )
    return out


def _decode_b64(raw: Any, *, what: str) -> bytes:
    if not isinstance(raw, str) or not raw:
        raise FlowError("flows.invalid", f"{what} must be base64 text")
    text = raw.split(",", 1)[1] if raw.startswith("data:") and "," in raw else raw
    if len(text) > (MAX_UPLOAD_BYTES * 4) // 3 + 8:
        raise FlowError("flows.invalid", f"{what} is larger than {MAX_UPLOAD_BYTES >> 20} MB")
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise FlowError("flows.invalid", f"{what} is not valid base64") from exc


class FlowService:
    def __init__(
        self,
        config: Any | None = None,
        *,
        root: Path | None = None,
        broadcast: Broadcast | None = None,
        image_adapter: ImageAdapter | None = None,
        text_adapter: TextAdapter | None = None,
        models: ModelRegistry | None = None,
        usage_tracker: Any | None = None,
        cost_recorder: CostRecorder | None = None,
    ) -> None:
        self._config = config
        self.root = Path(root) if root is not None else default_flows_root(config)
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = FlowStore(self.root)
        self.db = FlowDB(self.root / "flows.db")
        self.media = MediaStore(self.root / "media")
        self.outputs_root = self.root / "outputs"
        self._broadcast: Broadcast = broadcast or _default_broadcast
        self.image_adapter: ImageAdapter = image_adapter or UnwiredImageAdapter()
        self.text_adapter: TextAdapter = text_adapter or UnwiredTextAdapter()
        self.surplus_catalog = SurplusCatalog(self.root / CATALOG_FILENAME)
        self.models = models or ModelRegistry(self.default_entries, primary=self._primary_model)
        self.usage_tracker = usage_tracker
        self.cost_recorder = cost_recorder
        self._handles: dict[str, RunHandle] = {}
        self._queue: list[str] = []
        self._active: set[str] = set()
        self._slots: dict[str, asyncio.Semaphore] = {}
        self._started = False
        self._closing = False

    # ── wiring ──────────────────────────────────────────────────────────

    def update(
        self,
        *,
        config: Any | None = None,
        broadcast: Broadcast | None = None,
        usage_tracker: Any | None = None,
    ) -> None:
        """Refresh live config / transport without rebuilding state."""
        if config is not None:
            self._config = config
        if broadcast is not None:
            self._broadcast = broadcast
        if usage_tracker is not None:
            self.usage_tracker = usage_tracker

    def primary_model(self) -> str | None:
        """``[image_generation].primary`` of the live config (the ``auto`` preference)."""
        return self._primary_model()

    def _primary_model(self) -> str | None:
        """An explicit ``[image_generation].primary``; the built-in default does not count.

        ``openai/gpt-image-1`` is the field's default and is persisted with every
        config file, so it says nothing about a preference. Without an explicit
        primary, ``auto`` follows registry order: Surplus first.
        """
        image_cfg = getattr(self._config, "image_generation", None)
        primary = getattr(image_cfg, "primary", None)
        if not isinstance(primary, str) or primary == BUILTIN_IMAGE_PRIMARY:
            return None
        return primary

    def default_entries(self) -> tuple[ModelEntry, ...]:
        """Surplus (catalog or snapshot) first, then the static OpenRouter/OpenAI entries."""
        return self.surplus_catalog.entries() + REGISTRY

    @property
    def config(self) -> Any:
        return self._config

    @property
    def enabled(self) -> bool:
        return bool(_cfg(self._config, "enabled", True))

    @property
    def max_concurrent_runs(self) -> int:
        return max(1, int(_cfg(self._config, "max_concurrent_runs", 1)))

    @property
    def agent_cost_limit_usd(self) -> float:
        return float(_cfg(self._config, "agent_cost_limit_usd", 0.50))

    @property
    def cache_max_bytes(self) -> int:
        return max(0, int(_cfg(self._config, "cache_max_mb", 500))) * 1024 * 1024

    def start(self) -> None:
        """Startup recovery (idempotent): runs left queued/running become failed."""
        if self._started:
            return
        self._started = True
        stale = self.db.runs_with_status(("queued", "running"))
        for record in stale:
            nodes = record.get("nodes") or {}
            for state in nodes.values():
                if isinstance(state, dict) and state.get("state") in ("queued", "running"):
                    state.clear()
                    state["state"] = "skipped"
            self.db.update_run(
                record["run_id"],
                status="failed",
                ended=record.get("ended") or utc_now_iso(),
                nodes=nodes,
                error={
                    "code": "flows.interrupted",
                    "message": "The gateway stopped while this run was queued or running",
                },
            )
        if stale:
            log.info("flows.recovered_interrupted_runs", count=len(stale))

    async def close(self) -> None:
        """Stop every run: in-flight ones end ``failed`` / ``flows.interrupted``."""
        self._closing = True
        interrupted = {
            "code": "flows.interrupted",
            "message": "The gateway stopped before this run finished",
        }
        for run_id in list(self._queue):
            handle = self._handles.get(run_id)
            if handle is not None:
                await self._end_unstarted(handle, "failed", dict(interrupted))
        self._queue.clear()
        tasks = []
        # Running runs and stopped runs whose detached calls are still out.
        for handle in list(self._handles.values()):
            if handle.task is not None and not handle.task.done():
                handle.cancel_reason = "shutdown"
                handle.task.cancel()
                tasks.append(handle.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for handle in list(self._handles.values()):
            if handle.record["status"] == "queued":
                await self._end_unstarted(handle, "failed", dict(interrupted))
        self.db.close()

    async def _emit(self, event: str, payload: dict[str, Any]) -> None:
        try:
            await self._broadcast(event, payload)
        except Exception:  # noqa: BLE001 - events are best effort
            log.debug("flows.emit_failed", name=event, exc_info=True)

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise FlowError("flows.unavailable", "Image Flows are disabled ([flows].enabled)")

    # ── schema / models ─────────────────────────────────────────────────

    def node_schemas(self) -> dict[str, Any]:
        return node_catalog_wire()

    def models_wire(self) -> dict[str, Any]:
        return self.models.to_wire()

    # ── documents ───────────────────────────────────────────────────────

    def list_flows(self) -> dict[str, Any]:
        flows = []
        for doc in self.store.all():
            flows.append(
                {
                    "id": doc["id"],
                    "name": doc.get("name") or "",
                    "rev": int(doc.get("rev") or 0),
                    "updated": doc.get("updated"),
                    "node_count": len(doc.get("nodes") or []),
                    "preview": self.db.latest_preview(doc["id"]),
                }
            )
        flows.sort(key=lambda item: str(item.get("updated") or ""), reverse=True)
        return {"flows": flows}

    def get_flow(self, flow_id: str) -> dict[str, Any]:
        return self.store.get(flow_id)

    def resolve_flow(self, ref: str) -> dict[str, Any]:
        """A flow by id or by exact name (for the CLI and agent tools)."""
        if self.store.exists(ref):
            return self.store.get(ref)
        matches = [doc for doc in self.store.all() if doc.get("name") == ref]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            ids = ", ".join(doc["id"] for doc in matches)
            raise FlowError("flows.invalid", f"Several flows are named {ref!r}: {ids}")
        raise FlowError("flows.not_found", f"No flow with id or name {ref!r}")

    async def create_flow(self, name: Any, template: str | None = None) -> dict[str, Any]:
        doc = self.store.create(template_doc(template, clean_name(name)))
        await self._emit("flows.changed", {"flow_id": doc["id"], "rev": doc["rev"]})
        return doc

    async def save_flow(self, doc: Any, base_rev: Any) -> dict[str, Any]:
        if isinstance(base_rev, bool) or not isinstance(base_rev, int):
            raise FlowError("flows.invalid", "base_rev must be an integer")
        saved = self.store.save(doc, base_rev)
        await self._emit("flows.changed", {"flow_id": saved["id"], "rev": saved["rev"]})
        return saved

    async def delete_flow(self, flow_id: str) -> None:
        busy = [
            h.run_id
            for h in self._handles.values()
            if h.flow_id == flow_id and h.record["status"] in ("queued", "running")
        ]
        if busy:
            raise FlowError("flows.busy", f"Flow {flow_id} has runs in progress: {', '.join(busy)}")
        current = self.store.get(flow_id)
        self.store.delete(flow_id)
        await self._emit(
            "flows.changed",
            {"flow_id": flow_id, "rev": int(current.get("rev") or 0), "deleted": True},
        )

    # ── analysis ────────────────────────────────────────────────────────

    def _analyse(self, doc: Any, to_node: str | None) -> tuple[dict[str, Any], Analysis]:
        normalised = normalize_doc(doc)
        analysis = analyse(
            normalised, models=self.models, media_exists=self.media.exists, to_node=to_node
        )
        return normalised, analysis

    def validate(self, doc: Any, to_node: str | None = None) -> dict[str, Any]:
        _, analysis = self._analyse(doc, to_node)
        return {"ok": analysis.ok, "node_errors": analysis.node_errors()}

    def estimate(
        self,
        doc: Any,
        to_node: str | None = None,
        inputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Per-node prices and the total of what would run (cached excluded).

        Node-level problems (a missing prompt, a model without a key) do not
        stop an estimate; a structural one (cycle, bad link) leaves the plan
        empty and the total free.
        """
        normalised = apply_inputs(normalize_doc(doc), inputs)
        _, analysis = self._analyse(normalised, to_node)
        plan = None
        structural = {"unknown_type", "unknown_link", "type_mismatch", "multiple_links", "cycle"}
        if analysis.targets and not any(
            err["code"] in structural for errs in analysis.node_errors().values() for err in errs
        ):
            plan = build_plan(
                analysis,
                run_id="estimate",
                cache_lookup=self.db.cache_peek,
                media_exists=self.media.exists,
            )
        return estimate(analysis, plan)

    # ── runs ────────────────────────────────────────────────────────────

    async def submit_run(
        self,
        flow_id: str,
        *,
        flow: Any | None = None,
        inputs: dict[str, Any] | None = None,
        to_node: str | None = None,
        origin: str = "canvas",
        session_key: str | None = None,
        max_cost_usd: float | None = None,
        artifact_sink: ArtifactSink | None = None,
    ) -> dict[str, Any]:
        """Validate, plan and queue a run; returns ``{"run_id", "status": "queued"}``.

        ``flow`` (optional) is saved first with ``base_rev = flow.rev``.
        ``max_cost_usd`` makes the call refuse with ``flows.cost_gate`` when
        the estimate is above it or unknown (the agent path). Validation
        errors raise ``flows.invalid`` and nothing is queued.
        """
        self._require_enabled()
        if origin not in RUN_ORIGINS:
            raise FlowError("flows.invalid", f"origin must be one of {', '.join(RUN_ORIGINS)}")
        if flow is not None:
            if not isinstance(flow, dict) or flow.get("id") != flow_id:
                raise FlowError("flows.invalid", "flow.id must match flow_id")
            base_rev = flow.get("rev")
            if isinstance(base_rev, bool) or not isinstance(base_rev, int):
                raise FlowError("flows.invalid", "flow.rev must be an integer")
            doc = await self.save_flow(flow, base_rev)
        else:
            doc = self.store.get(flow_id)
        if len(self._queue) >= MAX_QUEUED_RUNS:
            raise FlowError("flows.busy", f"{len(self._queue)} runs are already waiting")

        graph = apply_inputs(doc, inputs)
        normalised, analysis = self._analyse(graph, to_node)
        if not analysis.ok:
            raise FlowError(
                "flows.invalid",
                "The flow has errors",
                details={"node_errors": analysis.node_errors()},
            )
        run_id = new_run_id()
        plan = build_plan(
            analysis,
            run_id=run_id,
            cache_lookup=self.db.cache_get,
            media_exists=self.media.exists,
        )
        est = estimate(analysis, plan)
        total = est["total"]
        upper = price_upper_usd(total)
        if max_cost_usd is not None and (upper is None or upper > max_cost_usd + 1e-9):
            raise FlowError(
                "flows.cost_gate",
                "The estimate is unknown"
                if upper is None
                else f"The estimate ${upper:.2f} is above ${max_cost_usd:.2f}",
                details={"estimate": est, "limit_usd": max_cost_usd},
            )
        nodes: dict[str, dict[str, Any]] = {}
        for node_id in plan.topo:
            state = plan.nodes[node_id].state
            nodes[node_id] = {"state": "cached" if state == "cached" else "queued"}
        record: dict[str, Any] = {
            "run_id": run_id,
            "flow_id": flow_id,
            "rev": int(doc.get("rev") or 0),
            "status": "queued",
            "origin": origin,
            "session_key": session_key,
            "to_node": to_node,
            "created": utc_now_iso(),
            "started": None,
            "ended": None,
            "inputs": dict(inputs or {}),
            "graph": normalised,
            "nodes": nodes,
            "outputs": {},
            "error": None,
            "estimate": est,
            "est_usd": upper,
            "cost_usd": None,
            "preview": None,
            "image_count": 0,
            "seq": 0,
        }
        self.db.insert_run(record)
        handle = RunHandle(
            run_id=run_id,
            flow_id=flow_id,
            flow_name=str(doc.get("name") or flow_id),
            rev=record["rev"],
            record=record,
            plan=plan,
            estimate=est,
            origin=origin,
            session_key=session_key,
            artifact_sink=artifact_sink,
        )
        self._handles[run_id] = handle
        self._queue.append(run_id)
        payload: dict[str, Any] = {
            "run_id": run_id,
            "flow_id": flow_id,
            "seq": handle.next_seq(),
            "status": "queued",
        }
        if upper is not None:
            payload["est_usd"] = upper
        self.db.update_run(run_id, seq=handle.seq)
        await self._emit("flows.run", payload)
        self._pump()
        return {"run_id": run_id, "status": "queued"}

    def _pump(self) -> None:
        while self._queue and len(self._active) < self.max_concurrent_runs and not self._closing:
            run_id = self._queue.pop(0)
            handle = self._handles.get(run_id)
            if handle is None or handle.record["status"] != "queued":
                continue
            self._active.add(run_id)
            handle.task = asyncio.create_task(self._run(handle), name=f"flows.run.{run_id}")

    async def _run(self, handle: RunHandle) -> None:
        executor = RunExecutor(handle, self._deps())
        try:
            await executor.run()
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - a crash must still end the run
            log.warning("flows.run_crashed", run_id=handle.run_id, error=str(exc), exc_info=True)
            handle.record["status"] = "failed"
            handle.record["ended"] = utc_now_iso()
            handle.record["error"] = {"code": "flows.internal", "message": str(exc)}
            self._persist(
                handle,
                {
                    "status": "failed",
                    "ended": handle.record["ended"],
                    "error": handle.record["error"],
                },
            )
            await self._emit(
                "flows.run",
                {
                    "run_id": handle.run_id,
                    "flow_id": handle.flow_id,
                    "seq": handle.next_seq(),
                    "status": "failed",
                    "error": handle.record["error"],
                },
            )
        finally:
            self._active.discard(handle.run_id)
            handle.finished.set()
            self._handles.pop(handle.run_id, None)
            if not self._closing:
                self._pump()

    def _deps(self) -> ExecutorDeps:
        return ExecutorDeps(
            media=self.media,
            models=self.models,
            image_adapter=self.image_adapter,
            text_adapter=self.text_adapter,
            outputs_root=self.outputs_root,
            emit=self._emit,
            persist=self._persist,
            provider_slot=self._provider_slot,
            cache_put=self._cache_put,
            record_cost=self._record_cost,
            on_stopped=self._on_stopped,
        )

    def _on_stopped(self, handle: RunHandle) -> None:
        """A stop ended the run: free its slot now; detached calls finish on their own."""
        self._active.discard(handle.run_id)
        handle.finished.set()
        if not self._closing:
            self._pump()

    def _persist(self, handle: RunHandle, fields: dict[str, Any]) -> None:
        try:
            self.db.update_run(handle.run_id, **fields)
        except Exception:  # noqa: BLE001 - a closed DB at shutdown must not mask the run
            log.debug("flows.persist_failed", run_id=handle.run_id, exc_info=True)

    def _provider_slot(self, provider: str) -> asyncio.Semaphore:
        slot = self._slots.get(provider)
        if slot is None:
            slot = asyncio.Semaphore(PROVIDER_CONCURRENCY)
            self._slots[provider] = slot
        return slot

    def _cache_put(
        self, key: str, node_type: str, outputs: dict[str, Any], blobs: list[str], size: int
    ) -> None:
        self.db.cache_put(key, node_type, outputs, blobs, size)
        for evicted in self.db.cache_evict_to(self.cache_max_bytes):
            for sha in evicted:
                if not self._blob_in_use(sha):
                    self.media.delete(sha)

    def _blob_in_use(self, sha: str) -> bool:
        return (
            self.db.cache_references(sha)
            or self.db.runs_reference(sha)
            or self.store.references(sha)
            or any(sha in str(h.record.get("outputs")) for h in self._handles.values())
        )

    def _record_cost(
        self, handle: RunHandle, node_id: str, cost: float, entry: ModelEntry | None
    ) -> None:
        if self.cost_recorder is None or self.usage_tracker is None or cost <= 0:
            return
        self.cost_recorder(
            self.usage_tracker,
            handle.run_id,
            handle.session_key,
            float(cost),
            entry.api_model if entry is not None else "",
            entry.provider if entry is not None else "",
        )

    async def stop_run(self, run_id: str) -> None:
        if not is_run_id(run_id):
            raise FlowError("flows.not_found", f"No run {run_id!r}")
        handle = self._handles.get(run_id)
        if handle is None:
            if self.db.get_run(run_id) is None:
                raise FlowError("flows.not_found", f"No run {run_id!r}")
            return  # already finished: stopping is a no-op
        if run_id in self._queue:
            self._queue.remove(run_id)
            await self._end_unstarted(handle, "cancelled")
            return
        if handle.record["status"] not in ("queued", "running"):
            return  # already stopped; its detached calls are finishing
        if handle.task is not None and not handle.task.done():
            # The executor ends the run "cancelled" at once and lets provider
            # calls already in flight finish (they are billed either way).
            handle.stop_requested.set()
            try:
                await asyncio.wait_for(handle.finished.wait(), timeout=10)
            except TimeoutError:
                log.warning("flows.stop_slow", run_id=run_id)

    async def _end_unstarted(
        self, handle: RunHandle, status: str, error: dict[str, Any] | None = None
    ) -> None:
        """End a run that never started executing (queued, or cancelled before start)."""
        record = handle.record
        record["status"] = status
        record["ended"] = utc_now_iso()
        record["error"] = error
        for state in record["nodes"].values():
            if state.get("state") in ("queued", "running"):
                state.clear()
                state["state"] = "skipped"
        seq = handle.next_seq()
        self._persist(
            handle,
            {
                "status": status,
                "ended": record["ended"],
                "nodes": record["nodes"],
                "error": error,
                "seq": seq,
            },
        )
        payload: dict[str, Any] = {
            "run_id": handle.run_id,
            "flow_id": handle.flow_id,
            "seq": seq,
            "status": status,
        }
        if error:
            payload["error"] = error
        await self._emit("flows.run", payload)
        handle.finished.set()
        self._handles.pop(handle.run_id, None)
        self._active.discard(handle.run_id)
        if not self._closing:
            self._pump()

    def list_runs(self, flow_id: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        return [run_summary(record) for record in self.db.list_runs(flow_id, limit)]

    def get_run(self, run_id: str) -> dict[str, Any]:
        handle = self._handles.get(run_id)
        if handle is not None:
            return run_wire(handle.record)
        record = self.db.get_run(run_id) if is_run_id(run_id) else None
        if record is None:
            raise FlowError("flows.not_found", f"No run {run_id!r}")
        return run_wire(record)

    async def wait_run(self, run_id: str, timeout: float | None = None) -> dict[str, Any]:
        """Wait until the run ends (or *timeout*), then return its RunRecord."""
        handle = self._handles.get(run_id)
        if handle is not None:
            try:
                await asyncio.wait_for(handle.finished.wait(), timeout=timeout)
            except TimeoutError:
                pass
        return self.get_run(run_id)

    # ── media ───────────────────────────────────────────────────────────

    def media_wire(self, sha: str, max_side: int | None = None) -> dict[str, Any]:
        if max_side is not None:
            data, mime, width, height = self.media.thumbnail(sha, max_side)
        else:
            data, mime = self.media.read(sha)
            ref = self.media.ref(sha)
            width, height = ref.width, ref.height
        return {
            "mime": mime,
            "data_b64": base64.b64encode(data).decode("ascii"),
            "width": width,
            "height": height,
        }

    def upload(self, name: Any, mime: Any, data_b64: Any) -> ImageRef:
        del name  # content-addressed: the name is not kept
        data = _decode_b64(data_b64, what="data_b64")
        if len(data) > MAX_UPLOAD_BYTES:
            raise FlowError("flows.invalid", f"image is larger than {MAX_UPLOAD_BYTES >> 20} MB")
        try:
            return self.media.put(data, str(mime or ""))
        except ValueError as exc:
            raise FlowError("flows.invalid", str(exc)) from exc

    def import_png(self, data_b64: Any) -> dict[str, Any]:
        data = _decode_b64(data_b64, what="data_b64")
        try:
            ref = self.media.put(data)
        except ValueError as exc:
            raise FlowError("flows.invalid", str(exc)) from exc
        meta = read_flow_metadata(data)
        flow = None
        if meta is not None and isinstance(meta.get("graph"), dict):
            try:
                flow = normalize_doc(meta["graph"])
            except FlowError:
                flow = None
        return {"image": ref.to_wire(), "flow": flow}


# ── module singleton ──────────────────────────────────────────────────────

_service: FlowService | None = None


def get_flow_service(
    config: Any | None = None,
    *,
    broadcast: Broadcast | None = None,
    usage_tracker: Any | None = None,
    factory: Callable[[Any], FlowService] | None = None,
) -> FlowService:
    """The process-wide service, created on first use.

    ``factory(config)`` builds it (the gateway passes one that wires real
    adapters, see ``agentos.gateway.flows_wiring``); without one a bare
    service with unwired adapters is built. Creation runs startup recovery.
    Later calls refresh config, transport and tracker without rebuilding
    state (mirrors ``get_trading_service``).
    """
    global _service
    if _service is None:
        _service = factory(config) if factory is not None else FlowService(config)
        _service.update(broadcast=broadcast, usage_tracker=usage_tracker)
        _service.start()
        return _service
    _service.update(config=config, broadcast=broadcast, usage_tracker=usage_tracker)
    return _service


def peek_flow_service() -> FlowService | None:
    return _service


def set_flow_service(service: FlowService | None) -> None:
    global _service
    _service = service


def reset_flow_service() -> None:
    set_flow_service(None)
