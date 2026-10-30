"""Persistence: flow documents as JSON files, runs and the cache in SQLite.

``<root>/<flow_id>.json``  one canonical document per flow (layout + logic)
``<root>/flows.db``        ``flow_runs`` (history) and ``flow_cache`` (LRU by bytes)
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from agentos.flows.types import FLOW_VERSION, FlowError, utc_now_iso

_FLOW_ID_RE = re.compile(r"^flw_[a-z0-9]{4,32}$")
_RUN_ID_RE = re.compile(r"^run_[a-z0-9]{4,32}$")
MAX_NAME_CHARS = 120
MAX_NODES = 500


def new_flow_id() -> str:
    return "flw_" + secrets.token_hex(4)


def new_run_id() -> str:
    return "run_" + secrets.token_hex(6)


def is_flow_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_FLOW_ID_RE.match(value))


def is_run_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_RUN_ID_RE.match(value))


def _invalid(message: str) -> FlowError:
    return FlowError("flows.invalid", message, details={"node_errors": {}})


def _json_safe(value: Any, where: str) -> Any:
    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError) as exc:
        raise _invalid(f"{where} is not JSON-serialisable: {exc}") from exc


def clean_name(raw: Any) -> str:
    name = " ".join(str(raw or "").split())
    if not name:
        raise _invalid("name must not be empty")
    return name[:MAX_NAME_CHARS]


def normalize_doc(raw: Any) -> dict[str, Any]:
    """Check the *shape* of a flow document and return a normalised copy.

    This is not graph validation (an unfinished graph must still save); it
    only refuses what cannot be a flow document: wrong types, duplicate ids,
    malformed links. Unknown keys on the document and on nodes are kept.
    """
    if not isinstance(raw, dict):
        raise _invalid("flow must be an object")
    doc: dict[str, Any] = _json_safe(raw, "flow")
    nodes = doc.get("nodes", [])
    links = doc.get("links", [])
    if not isinstance(nodes, list) or not isinstance(links, list):
        raise _invalid("flow.nodes and flow.links must be arrays")
    if len(nodes) > MAX_NODES:
        raise _invalid(f"a flow may have at most {MAX_NODES} nodes")
    node_ids: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict):
            raise _invalid("every node must be an object")
        node_id, node_type = node.get("id"), node.get("type")
        if not isinstance(node_id, str) or not node_id:
            raise _invalid("every node needs a string id")
        if node_id in node_ids:
            raise _invalid(f"duplicate node id {node_id!r}")
        if not isinstance(node_type, str) or not node_type:
            raise _invalid(f"node {node_id!r} needs a string type")
        node_ids.add(node_id)
        values = node.get("values")
        if values is None:
            node["values"] = {}
        elif not isinstance(values, dict):
            raise _invalid(f"node {node_id!r}: values must be an object")
        pos = node.get("pos", [0, 0])
        if (
            not isinstance(pos, list)
            or len(pos) != 2
            or not all(isinstance(v, int | float) and not isinstance(v, bool) for v in pos)
        ):
            raise _invalid(f"node {node_id!r}: pos must be [x, y]")
        node["pos"] = pos
        node.setdefault("title", None)
    link_ids: set[str] = set()
    for link in links:
        if not isinstance(link, dict):
            raise _invalid("every link must be an object")
        link_id = link.get("id")
        if not isinstance(link_id, str) or not link_id:
            raise _invalid("every link needs a string id")
        if link_id in link_ids:
            raise _invalid(f"duplicate link id {link_id!r}")
        link_ids.add(link_id)
        for end in ("from", "to"):
            pair = link.get(end)
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or not all(isinstance(v, str) and v for v in pair)
            ):
                raise _invalid(f"link {link_id!r}: {end} must be [node_id, name]")
    inputs = doc.get("inputs", {})
    if not isinstance(inputs, dict):
        raise _invalid("flow.inputs must be an object")
    for public, target in inputs.items():
        if not isinstance(target, str) or "." not in target:
            raise _invalid(f"flow.inputs[{public!r}] must be '<node_id>.<input>'")
    viewport = doc.get("viewport", {"x": 0, "y": 0, "zoom": 1})
    if not isinstance(viewport, dict):
        raise _invalid("flow.viewport must be an object")
    doc["version"] = FLOW_VERSION
    doc["nodes"] = nodes
    doc["links"] = links
    doc["inputs"] = inputs
    doc["viewport"] = viewport
    return doc


class FlowStore:
    """Flow documents on disk. Writes are atomic and rev-checked."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.RLock()

    def _path(self, flow_id: str) -> Path:
        if not is_flow_id(flow_id):
            raise FlowError("flows.not_found", f"no flow {flow_id!r}")
        return self.root / f"{flow_id}.json"

    def _write(self, doc: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(doc["id"])
        tmp = path.with_suffix(f".json.tmp{os.getpid()}")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def get(self, flow_id: str) -> dict[str, Any]:
        path = self._path(flow_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise FlowError("flows.not_found", f"no flow {flow_id!r}") from exc
        except (OSError, ValueError) as exc:
            raise FlowError("flows.not_found", f"flow {flow_id!r} is unreadable: {exc}") from exc
        if not isinstance(raw, dict):
            raise FlowError("flows.not_found", f"flow {flow_id!r} is unreadable")
        return raw

    def exists(self, flow_id: str) -> bool:
        return is_flow_id(flow_id) and self._path(flow_id).is_file()

    def create(self, doc: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            flow_id = new_flow_id()
            while self.exists(flow_id):
                flow_id = new_flow_id()
            stored = normalize_doc(doc)
            stored["id"] = flow_id
            stored["name"] = clean_name(doc.get("name"))
            stored["rev"] = 1
            stored["updated"] = utc_now_iso()
            self._write(stored)
            return stored

    def save(self, doc: Any, base_rev: int) -> dict[str, Any]:
        stored = normalize_doc(doc)
        flow_id = stored.get("id")
        if not isinstance(flow_id, str):
            raise _invalid("flow.id is required")
        with self._lock:
            current = self.get(flow_id)
            current_rev = int(current.get("rev") or 0)
            if current_rev != base_rev:
                raise FlowError(
                    "flows.conflict",
                    f"flow {flow_id} is at rev {current_rev}, not {base_rev}",
                    details={"rev": current_rev, "flow": current},
                )
            stored["name"] = clean_name(stored.get("name") or current.get("name"))
            stored["rev"] = base_rev + 1
            stored["updated"] = utc_now_iso()
            self._write(stored)
            return stored

    def delete(self, flow_id: str) -> None:
        with self._lock:
            path = self._path(flow_id)
            if not path.is_file():
                raise FlowError("flows.not_found", f"no flow {flow_id!r}")
            path.unlink()

    def all(self) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        docs = []
        for path in sorted(self.root.glob("flw_*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(raw, dict) and is_flow_id(raw.get("id")):
                docs.append(raw)
        return docs

    def references(self, sha: str) -> bool:
        """Whether any stored flow document mentions the blob *sha*."""
        if not self.root.is_dir():
            return False
        for path in self.root.glob("flw_*.json"):
            try:
                if sha in path.read_text(encoding="utf-8"):
                    return True
            except OSError:
                continue
        return False


_SCHEMA = """
CREATE TABLE IF NOT EXISTS flow_runs (
    run_id TEXT PRIMARY KEY,
    flow_id TEXT NOT NULL,
    rev INTEGER NOT NULL,
    status TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'canvas',
    session_key TEXT,
    to_node TEXT,
    created TEXT NOT NULL,
    created_ts REAL NOT NULL,
    started TEXT,
    ended TEXT,
    inputs_json TEXT NOT NULL DEFAULT '{}',
    graph_json TEXT NOT NULL,
    node_state_json TEXT NOT NULL DEFAULT '{}',
    outputs_json TEXT NOT NULL DEFAULT '{}',
    error_json TEXT,
    est_json TEXT,
    est_usd REAL,
    cost_usd REAL,
    preview_json TEXT,
    image_count INTEGER NOT NULL DEFAULT 0,
    seq INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS flow_runs_flow ON flow_runs (flow_id, created_ts);
CREATE INDEX IF NOT EXISTS flow_runs_status ON flow_runs (status);
CREATE TABLE IF NOT EXISTS flow_cache (
    key TEXT PRIMARY KEY,
    node_type TEXT NOT NULL,
    outputs_json TEXT NOT NULL,
    artifact_ids TEXT NOT NULL DEFAULT '[]',
    bytes INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    last_hit REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS flow_cache_lru ON flow_cache (last_hit);
"""

_RUN_JSON_COLUMNS = {
    "inputs": "inputs_json",
    "graph": "graph_json",
    "nodes": "node_state_json",
    "outputs": "outputs_json",
    "error": "error_json",
    "estimate": "est_json",
    "preview": "preview_json",
}


class FlowDB:
    """``flows.db``: run history and the node cache. Thread-safe, synchronous."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            os.fspath(self.path), timeout=30.0, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        with self._lock:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── runs ───────────────────────────────────────────────────────────

    def insert_run(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO flow_runs (run_id, flow_id, rev, status, origin, session_key, "
                "to_node, created, created_ts, inputs_json, graph_json, node_state_json, "
                "outputs_json, est_json, est_usd, seq) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record["run_id"],
                    record["flow_id"],
                    int(record["rev"]),
                    record["status"],
                    record.get("origin") or "canvas",
                    record.get("session_key"),
                    record.get("to_node"),
                    record["created"],
                    time.time(),
                    json.dumps(record.get("inputs") or {}),
                    json.dumps(record["graph"]),
                    json.dumps(record.get("nodes") or {}),
                    json.dumps(record.get("outputs") or {}),
                    json.dumps(record.get("estimate")),
                    record.get("est_usd"),
                    int(record.get("seq") or 0),
                ),
            )

    def update_run(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        columns: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            column = _RUN_JSON_COLUMNS.get(key)
            if column is not None:
                columns.append(f"{column} = ?")
                values.append(None if value is None else json.dumps(value))
            elif key in {"status", "started", "ended", "est_usd", "cost_usd", "image_count", "seq"}:
                columns.append(f"{key} = ?")
                values.append(value)
            else:
                raise KeyError(key)
        values.append(run_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE flow_runs SET {', '.join(columns)} WHERE run_id = ?",  # noqa: S608
                values,
            )

    @staticmethod
    def _decode_run(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        out: dict[str, Any] = {
            "run_id": data["run_id"],
            "flow_id": data["flow_id"],
            "rev": data["rev"],
            "status": data["status"],
            "origin": data["origin"],
            "session_key": data["session_key"],
            "to_node": data["to_node"],
            "created": data["created"],
            "started": data["started"],
            "ended": data["ended"],
            "est_usd": data["est_usd"],
            "cost_usd": data["cost_usd"],
            "image_count": data["image_count"],
            "seq": data["seq"],
        }
        for key, column in _RUN_JSON_COLUMNS.items():
            raw = data.get(column)
            out[key] = json.loads(raw) if raw else None
        return out

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM flow_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return self._decode_run(row) if row is not None else None

    def list_runs(self, flow_id: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        sql = "SELECT * FROM flow_runs"
        params: list[Any] = []
        if flow_id:
            sql += " WHERE flow_id = ?"
            params.append(flow_id)
        sql += " ORDER BY created_ts DESC, rowid DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._decode_run(row) for row in rows]

    def runs_with_status(self, statuses: Iterable[str]) -> list[dict[str, Any]]:
        wanted = list(statuses)
        marks = ", ".join("?" for _ in wanted)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM flow_runs WHERE status IN ({marks})",  # noqa: S608
                wanted,
            ).fetchall()
        return [self._decode_run(row) for row in rows]

    def latest_preview(self, flow_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT preview_json FROM flow_runs WHERE flow_id = ? AND preview_json IS NOT NULL "
                "AND preview_json != 'null' ORDER BY created_ts DESC, rowid DESC LIMIT 1",
                (flow_id,),
            ).fetchone()
        if row is None or not row[0]:
            return None
        value = json.loads(row[0])
        return value if isinstance(value, dict) else None

    def delete_runs(self, flow_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM flow_runs WHERE flow_id = ?", (flow_id,))

    def runs_reference(self, sha: str) -> bool:
        pattern = f"%{sha}%"
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM flow_runs WHERE outputs_json LIKE ? OR graph_json LIKE ? LIMIT 1",
                (pattern, pattern),
            ).fetchone()
        return row is not None

    # ── cache ──────────────────────────────────────────────────────────

    def cache_get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT outputs_json, artifact_ids FROM flow_cache WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE flow_cache SET last_hit = ? WHERE key = ?", (time.time(), key)
            )
        return {"outputs": json.loads(row[0]), "artifact_ids": json.loads(row[1])}

    def cache_peek(self, key: str) -> dict[str, Any] | None:
        """Like :meth:`cache_get` without touching the LRU clock (estimates)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT outputs_json, artifact_ids FROM flow_cache WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        return {"outputs": json.loads(row[0]), "artifact_ids": json.loads(row[1])}

    def cache_put(
        self,
        key: str,
        node_type: str,
        outputs: dict[str, Any],
        artifact_ids: list[str],
        size: int,
    ) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO flow_cache "
                "(key, node_type, outputs_json, artifact_ids, bytes, created, last_hit) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key, node_type, json.dumps(outputs), json.dumps(artifact_ids), size, now, now),
            )

    def cache_delete(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM flow_cache WHERE key = ?", (key,))

    def cache_total_bytes(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COALESCE(SUM(bytes), 0) FROM flow_cache").fetchone()
        return int(row[0] or 0)

    def cache_evict_to(self, max_bytes: int) -> list[list[str]]:
        """Drop least-recently-hit entries until the cache fits; returns their blobs."""
        evicted: list[list[str]] = []
        with self._lock:
            total = self.cache_total_bytes()
            if total <= max_bytes:
                return evicted
            rows = self._conn.execute(
                "SELECT key, bytes, artifact_ids FROM flow_cache ORDER BY last_hit ASC"
            ).fetchall()
            for key, size, artifact_ids in rows:
                if total <= max_bytes:
                    break
                self._conn.execute("DELETE FROM flow_cache WHERE key = ?", (key,))
                total -= int(size or 0)
                evicted.append(list(json.loads(artifact_ids or "[]")))
        return evicted

    def cache_references(self, sha: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM flow_cache WHERE artifact_ids LIKE ? LIMIT 1", (f"%{sha}%",)
            ).fetchone()
        return row is not None
