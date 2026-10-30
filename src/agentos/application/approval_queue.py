"""Approval queue with single-process persistent state."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from agentos.application import approval_delivery
from agentos.application.approval_events import (
    APPROVAL_REQUESTED,
    APPROVAL_RESOLVED,
    approval_requested_payload,
    approval_resolved_payload,
    notify_approval_event,
)
from agentos.application.gateway_pidlock import live_gateway_pid
from agentos.paths import state_dir
from agentos.util.bounded_registry import BoundedRegistry

VALID_APPROVAL_MODES = frozenset({"auto-approve", "auto-deny", "prompt"})
VALID_ELEVATED_MODES = frozenset({"on", "bypass", "full"})


@dataclass
class ApprovalSettings:
    mode: str = "prompt"
    allow_patterns: list[str] = field(default_factory=list)
    deny_patterns: list[str] = field(default_factory=list)


@dataclass
class PendingApproval:
    approval_id: str
    namespace: str  # "exec" or "plugin"
    params: dict
    created_at: float = field(default_factory=time.time)
    resolved: bool = False
    approved: bool = False
    consumed: bool = False
    #: Optional answer data attached by the resolver (ACP elicitation forms
    #: carry their field values here). Never part of ``params``.
    payload: dict | None = None
    #: Per-row lifespan end (epoch seconds); ``None`` = ``created_at`` plus
    #: the queue's ``default_timeout``.
    expires_at: float | None = None
    #: Sessions whose viewers should see this request: the requesting
    #: session, then its subagent / coding-agent ancestors (nearest first).
    audience_session_keys: tuple[str, ...] = ()
    #: How it was resolved once it is: ``approved``, ``denied``, ``expired``
    #: or ``cancelled``; ``None`` while pending (and on a re-read row).
    decision: str | None = None
    _event: asyncio.Event = field(default_factory=asyncio.Event)


_DEFAULT_APPROVAL_QUEUE_PATH = state_dir("approval_queue.sqlite")

#: Set by processes that can show an approval prompt to a human themselves
#: (the CLI: standalone chat prompts inline, ``agentos agent`` prints the
#: envelope). Such a process may share a state directory with a running
#: gateway. Anything else that shares one — a test, a probe script, a stray
#: import of the tool layer — has no surface, so its rows would only ever show
#: up as prompts in the gateway's UI, asked of a user who never issued the
#: command. ``request()`` refuses those.
_LOCAL_APPROVAL_SURFACE = False


def claim_local_approval_surface() -> None:
    """Declare that this process can present approval prompts itself."""
    global _LOCAL_APPROVAL_SURFACE
    _LOCAL_APPROVAL_SURFACE = True


class ApprovalQueueOwnedByGatewayError(RuntimeError):
    """A live gateway owns this queue's state directory and we have no surface."""

    def __init__(self, *, db_path: Path, gateway_pid: int, namespace: str) -> None:
        self.db_path = db_path
        self.gateway_pid = gateway_pid
        self.namespace = namespace
        super().__init__(
            f"Refusing to queue a {namespace!r} approval in {db_path}: gateway pid "
            f"{gateway_pid} owns that state directory and would show this prompt to its "
            "user. Point AGENTOS_STATE_DIR at a directory of this process's own, or send "
            "the request through the running gateway."
        )


#: How long a resolved row stays in the table before it is pruned. Long
#: enough that an approved row is always consumed first, short enough that
#: the queue file stops growing for the lifetime of the install.
DEFAULT_RESOLVED_RETENTION_SECONDS = 24 * 60 * 60.0

# A row without ``expires_at`` lives ``default_timeout`` from creation (the
# original rule); a row with one lives until then. Parameters: the
# default-timeout cutoff, then "now".
_EXPIRED_SQL = (
    "((expires_at IS NULL AND created_at <= ?) OR (expires_at IS NOT NULL AND expires_at <= ?))"
)
# Resolved rows are pruned once both creation and lifespan end are older than
# the retention window, so a long-lived row is never pruned before it expires.
_PRUNABLE_SQL = "MAX(created_at, COALESCE(expires_at, created_at)) <= ?"

#: Namespaces whose resolve can carry "remember this for the session"
#: (``allow_always`` / ``remember_intent`` / a bypass elevation) as a flag in
#: the row's params instead of the exec intent cache or the session's
#: elevated mode.
SESSION_REMEMBER_NAMESPACES = frozenset({"acp"})
#: Namespaces whose resolve never changes the session's elevated mode.
#: ``flow`` is an agent's Image Flow spend (``flow_run``): approved once or
#: denied, never turned into a session elevation (and, not being ``exec``, never
#: remembered as an "always allow" intent).
NO_ELEVATION_NAMESPACES = frozenset({"acp", "acp_elicitation", "flow"})


class ApprovalQueue:
    def __init__(
        self,
        default_timeout: float = 300.0,
        *,
        db_path: str | None = None,
        poll_interval: float = 0.25,
        resolved_retention: float = DEFAULT_RESOLVED_RETENTION_SECONDS,
    ):
        self._pending: dict[str, PendingApproval] = {}
        self._timeout = default_timeout
        self._poll_interval = max(0.01, float(poll_interval))
        # A row can be resolved right up to the end of its lifespan and is
        # consumed shortly after, so the window is never shorter than that:
        # an explicit ``resolved_retention`` below ``default_timeout`` is
        # raised to it.
        self._resolved_retention = max(float(resolved_retention), float(default_timeout))
        self._global_settings = ApprovalSettings()
        # Node ids are an operational set rather than a per-session one, so
        # the ceiling is a backstop nothing reaches; eviction falls back to
        # the global settings, which is the same answer an unconfigured
        # node already gets. Note the direction: a node pinned to a stricter
        # policy than the global one (say ``auto-deny``) would fail *open* to
        # the global setting if it were ever evicted, which is why the ceiling
        # is sized so it cannot be.
        self._node_settings: BoundedRegistry[str, ApprovalSettings] = BoundedRegistry(
            name="ApprovalQueue._node_settings",
        )
        self._session_elevated_modes: BoundedRegistry[str, str] = BoundedRegistry(
            name="ApprovalQueue._session_elevated_modes",
            session_of=lambda key, _value: key,
        )

        self._db_path = Path(db_path or os.fspath(_DEFAULT_APPROVAL_QUEUE_PATH))
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self._db_path,
            timeout=30.0,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._init_schema()
        self._sweep()
        self._load_pending()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS approval_queue (
                approval_id   TEXT PRIMARY KEY,
                namespace     TEXT NOT NULL,
                params        TEXT NOT NULL,
                created_at    REAL NOT NULL,
                resolved      INTEGER NOT NULL DEFAULT 0,
                approved      INTEGER NOT NULL DEFAULT 0,
                consumed      INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_approval_namespace_status
            ON approval_queue(namespace, resolved);
            """
        )
        # Additive columns (ACP bridge): a resolver payload and a per-row
        # lifespan. Older queue files get them on first open.
        columns = {
            str(row["name"]) for row in self._conn.execute("PRAGMA table_info(approval_queue)")
        }
        if "payload" not in columns:
            self._conn.execute("ALTER TABLE approval_queue ADD COLUMN payload TEXT")
        if "expires_at" not in columns:
            self._conn.execute("ALTER TABLE approval_queue ADD COLUMN expires_at REAL")
        if "audience" not in columns:
            self._conn.execute("ALTER TABLE approval_queue ADD COLUMN audience TEXT")
        self._conn.commit()

    def _serialize_params(self, params: dict | None) -> str:
        return json.dumps(params or {}, ensure_ascii=False, sort_keys=True)

    def _deserialize_params(self, raw: str | bytes | bytearray) -> dict:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw or "{}")
        except json.JSONDecodeError:
            parsed = {}
        return parsed if isinstance(parsed, dict) else {}

    def _row_to_entry(self, row: sqlite3.Row) -> PendingApproval:
        aid = str(row["approval_id"])
        existing = self._pending.get(aid)
        keys = row.keys()
        raw_payload = row["payload"] if "payload" in keys else None
        raw_expires = row["expires_at"] if "expires_at" in keys else None
        raw_audience = row["audience"] if "audience" in keys else None
        return PendingApproval(
            approval_id=aid,
            namespace=str(row["namespace"]),
            params=self._deserialize_params(row["params"]),
            created_at=float(row["created_at"]),
            resolved=bool(row["resolved"]),
            approved=bool(row["approved"]),
            consumed=bool(row["consumed"]),
            payload=self._deserialize_params(raw_payload) if raw_payload is not None else None,
            expires_at=float(raw_expires) if raw_expires is not None else None,
            audience_session_keys=self._deserialize_audience(raw_audience),
            _event=existing._event if existing is not None else asyncio.Event(),
        )

    @staticmethod
    def _deserialize_audience(raw: object) -> tuple[str, ...]:
        if raw is None:
            return ()
        try:
            parsed = json.loads(str(raw))
        except (TypeError, ValueError):
            return ()
        if not isinstance(parsed, list):
            return ()
        return tuple(str(item) for item in parsed if isinstance(item, str) and item)

    def _load_pending(self) -> None:
        self._pending = {}
        for row in self._conn.execute(
            "SELECT approval_id, namespace, params, created_at, resolved, approved, consumed, "
            "payload, expires_at, audience "
            "FROM approval_queue WHERE resolved = 0"
        ):
            entry = self._row_to_entry(row)
            self._pending[entry.approval_id] = entry

    def _get_row(self, approval_id: str) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            self._conn.execute(
                "SELECT approval_id, namespace, params, created_at, resolved, approved, consumed, "
                "payload, expires_at, audience "
                "FROM approval_queue WHERE approval_id = ?",
                (approval_id,),
            ).fetchone(),
        )

    def _sweep(self) -> None:
        """Deny approvals whose lifespan elapsed and prune old resolved rows.

        ``wait()`` denies an approval once ``created_at + default_timeout``
        has passed, but only while a caller is waiting. The shell tool waits
        in bounded slices and gives up, and the process may restart, so
        without this sweep an aged-out row stayed pending forever and the
        Web UI kept showing a prompt nothing could act on. Runs at startup and
        on the write/read paths that shape the pending set.
        """
        now = time.time()
        expired_before = now - self._timeout
        prune_before = now - self._resolved_retention
        # Look before taking the write lock: the Web UI polls list_pending(),
        # and in the common case there is nothing to do, so the poll must stay
        # a plain read rather than queue behind a writer in another process.
        due = self._conn.execute(
            "SELECT 1 FROM approval_queue "
            f"WHERE (resolved = 0 AND {_EXPIRED_SQL}) "
            f"OR (resolved = 1 AND {_PRUNABLE_SQL}) "
            "LIMIT 1",
            (expired_before, now, prune_before),
        ).fetchone()
        if due is None:
            return
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            expired = [
                str(row["approval_id"])
                for row in self._conn.execute(
                    f"SELECT approval_id FROM approval_queue WHERE resolved = 0 AND {_EXPIRED_SQL}",
                    (expired_before, now),
                ).fetchall()
            ]
            if expired:
                self._conn.execute(
                    "UPDATE approval_queue SET resolved = 1, approved = 0 "
                    f"WHERE resolved = 0 AND {_EXPIRED_SQL}",
                    (expired_before, now),
                )
            pruned = [
                str(row["approval_id"])
                for row in self._conn.execute(
                    "SELECT approval_id FROM approval_queue "
                    f"WHERE resolved = 1 AND {_PRUNABLE_SQL}",
                    (prune_before,),
                ).fetchall()
            ]
            if pruned:
                self._conn.execute(
                    f"DELETE FROM approval_queue WHERE resolved = 1 AND {_PRUNABLE_SQL}",
                    (prune_before,),
                )
            self._conn.commit()
        except BaseException:
            # Leaving the transaction open would make every later
            # BEGIN IMMEDIATE on this connection fail.
            self._conn.rollback()
            raise
        for approval_id in expired:
            entry = self._pending.get(approval_id)
            if entry is not None:
                entry.resolved = True
                entry.approved = False
                # Wake an in-process waiter the same way a resolve would.
                entry._event.set()
            self._notify_resolved(
                approval_id,
                namespace=entry.namespace if entry is not None else None,
                decision="expired",
                resolved_by="timeout",
                entry=entry,
            )
        for approval_id in pruned:
            self._pending.pop(approval_id, None)

    def _foreign_live_gateway(self) -> int | None:
        if _LOCAL_APPROVAL_SURFACE:
            return None
        return live_gateway_pid(self._db_path.parent)

    def request(
        self,
        namespace: str = "exec",
        params: dict | None = None,
        *,
        ttl: float | None = None,
    ) -> str:
        """Queue an approval; ``ttl`` (seconds) overrides ``default_timeout``
        as this row's lifespan."""
        gateway_pid = self._foreign_live_gateway()
        if gateway_pid is not None:
            raise ApprovalQueueOwnedByGatewayError(
                db_path=self._db_path, gateway_pid=gateway_pid, namespace=namespace
            )
        self._sweep()
        payload = self._serialize_params(params or {})
        audience = approval_delivery.audience_for((params or {}).get("sessionKey"))
        audience_json = json.dumps(list(audience)) if audience else None
        while True:
            approval_id = uuid.uuid4().hex[:12]
            now = time.time()
            expires_at = now + max(0.0, float(ttl)) if ttl is not None else None
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "INSERT INTO approval_queue "
                    "(approval_id, namespace, params, created_at, resolved, approved, consumed, "
                    "expires_at, audience) "
                    "VALUES (?, ?, ?, ?, 0, 0, 0, ?, ?)",
                    (approval_id, namespace, payload, now, expires_at, audience_json),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                self._conn.rollback()
                continue
            break

        entry = PendingApproval(
            approval_id=approval_id,
            namespace=namespace,
            params=params or {},
            created_at=now,
            expires_at=expires_at,
            audience_session_keys=audience,
        )
        self._pending[approval_id] = entry
        self._notify_requested(entry)
        return approval_id

    def lifespan_end(self, entry: PendingApproval) -> float:
        """Epoch seconds at which ``entry`` stops being answerable."""
        if entry.expires_at is not None:
            return entry.expires_at
        return entry.created_at + self._timeout

    def get(self, approval_id: str) -> PendingApproval:
        row = self._get_row(approval_id)
        if row is None:
            raise KeyError(f"Approval not found: {approval_id}")
        entry = self._row_to_entry(row)
        self._pending[approval_id] = entry
        return entry

    async def wait(self, approval_id: str, timeout: float | None = None) -> bool:
        entry = self.get(approval_id)
        if entry.resolved:
            return entry.approved
        t = timeout if timeout is not None else self._timeout
        now = time.monotonic()
        deadline = now + t
        # The approval's overall lifespan (created_at + default_timeout) is a
        # wall-clock fact, but it is compared against a monotonic deadline, so
        # convert it once here rather than re-reading time.time() after the
        # loop. Windows' wall clock ticks at ~15.6ms; read afresh after a 20ms
        # monotonic wait it can still show the approval as younger than its
        # lifespan and leave a timed-out approval pending forever.
        lifespan_end = (
            entry.expires_at if entry.expires_at is not None else entry.created_at + self._timeout
        )
        lifespan_deadline = now + max(0.0, lifespan_end - time.time())
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(
                    entry._event.wait(),
                    timeout=min(self._poll_interval, remaining),
                )
            except TimeoutError:
                pass
            entry = self.get(approval_id)
            if entry.resolved:
                return entry.approved

        if time.monotonic() < lifespan_deadline:
            # The caller's own timeout elapsed, but the approval's overall
            # lifespan (created_at + default_timeout) hasn't -- leave it
            # pending instead of denying it, so a human operator can still
            # resolve it after this bounded wait call returns.
            return False
        return self._deny_on_timeout_if_unresolved(approval_id)

    def _deny_on_timeout_if_unresolved(self, approval_id: str) -> bool:
        self._conn.execute("BEGIN IMMEDIATE")
        row = self._get_row(approval_id)
        if row is None:
            self._conn.rollback()
            raise KeyError(f"Approval not found: {approval_id}")
        entry = self._row_to_entry(row)
        if entry.resolved:
            self._conn.rollback()
            self._pending[approval_id] = entry
            entry._event.set()
            return entry.approved
        self._conn.execute(
            "UPDATE approval_queue "
            "SET resolved = 1, approved = 0 "
            "WHERE approval_id = ? AND resolved = 0",
            (approval_id,),
        )
        self._conn.commit()
        entry = self.get(approval_id)
        entry._event.set()
        self._pending[approval_id] = entry
        self._notify_resolved(
            approval_id,
            namespace=entry.namespace,
            decision="expired",
            resolved_by="timeout",
            entry=entry,
        )
        return entry.approved

    def resolve(
        self,
        approval_id: str,
        approved: bool,
        *,
        allow_always: bool = False,
        remember_intent: bool = False,
        elevated_mode: str | None = None,
        payload: dict | None = None,
        resolved_by: str | None = None,
        decision: str | None = None,
    ) -> None:
        """Resolve a pending approval.

        ``payload`` (optional) is answer data stored beside the row (ACP
        elicitation answers); read it back with :meth:`take_payload`.
        ``resolved_by`` names who decided, for the ``approval.resolved`` event
        (default ``operator``); ``decision`` overrides its ``approved`` /
        ``denied`` (a denial nobody chose, such as ``cancelled``).
        """
        self._conn.execute("BEGIN IMMEDIATE")
        row = self._get_row(approval_id)
        if row is None:
            self._conn.rollback()
            raise KeyError(f"Approval not found: {approval_id}")
        entry = self._row_to_entry(row)
        if entry.resolved:
            self._conn.rollback()
            self._pending[approval_id] = entry
            entry._event.set()
            if entry.approved == approved:
                return
            raise ValueError(f"Approval already resolved: {approval_id}")

        # Fold the elevation into the persisted params: get()/status() re-read
        # the row, so anything written only onto the in-memory entry is lost on
        # the very next lookup (and on restart).
        params = dict(entry.params)
        if entry.namespace in NO_ELEVATION_NAMESPACES:
            # An ACP prompt is not an exec prompt: "bypass" on it means "stop
            # asking for this harness session", never "elevate the session".
            if (
                approved
                and entry.namespace in SESSION_REMEMBER_NAMESPACES
                and (allow_always or remember_intent or elevated_mode in ("bypass", "full"))
            ):
                params["rememberDecision"] = True
            elevated_mode = None
        if approved and elevated_mode in VALID_ELEVATED_MODES:
            params["elevatedMode"] = elevated_mode
        payload_json = self._serialize_params(payload) if isinstance(payload, dict) else None
        cursor = self._conn.execute(
            "UPDATE approval_queue "
            "SET resolved = 1, approved = ?, params = ?, payload = COALESCE(?, payload) "
            "WHERE approval_id = ? AND resolved = 0",
            (1 if approved else 0, self._serialize_params(params), payload_json, approval_id),
        )
        if cursor.rowcount != 1:
            self._conn.rollback()
            entry = self.get(approval_id)
            if entry.resolved:
                entry._event.set()
                if entry.approved == approved:
                    return
                raise ValueError(f"Approval already resolved: {approval_id}")
            raise ValueError(f"Approval could not be resolved: {approval_id}")
        self._conn.commit()

        entry = self.get(approval_id)
        entry.approved = bool(approved)
        entry.resolved = True
        entry._event.set()
        self._pending[approval_id] = entry
        self._notify_resolved(
            approval_id,
            namespace=entry.namespace,
            decision=decision or ("approved" if approved else "denied"),
            resolved_by=resolved_by or "operator",
            entry=entry,
        )

        if approved and elevated_mode in VALID_ELEVATED_MODES:
            session_key = str(entry.params.get("sessionKey") or "").strip()
            if session_key:
                self.set_elevated_mode(session_key, elevated_mode)

        if approved and entry.namespace == "exec" and (allow_always or remember_intent):
            self._persist_command_intent(entry.params, allow_always=allow_always)

    def _notify_requested(self, entry: PendingApproval) -> None:
        """The one place a new row is announced: the ``approval.requested``
        event for consoles, then delivery (Web Push, the parent's channel)."""
        notify_approval_event(
            APPROVAL_REQUESTED,
            approval_requested_payload(
                approval_id=entry.approval_id,
                namespace=entry.namespace,
                params=entry.params,
                created_at=entry.created_at,
                # A row without its own lifespan still ends: at the default.
                expires_at=self.lifespan_end(entry),
                mode=self._global_settings.mode,
                audience_session_keys=entry.audience_session_keys,
            ),
        )
        approval_delivery.on_requested(entry)

    def _notify_resolved(
        self,
        approval_id: str,
        *,
        namespace: str | None,
        decision: str,
        resolved_by: str,
        entry: PendingApproval | None = None,
    ) -> None:
        """The one place a resolution is announced, whatever decided it: the
        ``approval.resolved`` event, then delivery, which replaces a pushed
        prompt (also when it ``expired`` or was ``cancelled``)."""
        if entry is None or namespace is None:
            row = self._get_row(approval_id)
            if entry is None and row is not None:
                entry = self._row_to_entry(row)
            if namespace is None:
                namespace = str(row["namespace"]) if row is not None else "exec"
        notify_approval_event(
            APPROVAL_RESOLVED,
            approval_resolved_payload(
                approval_id=approval_id,
                namespace=namespace,
                decision=decision,
                resolved_by=resolved_by,
                resolved_at=time.time(),
            ),
        )
        if entry is not None:
            entry.decision = decision
            approval_delivery.on_resolved(entry)

    def take_payload(self, approval_id: str) -> dict | None:
        """Return the resolver payload once and clear it from the row.

        Elicitation answers can hold secrets; they are read by the waiting
        bridge and must not linger in the queue file for the retention window.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT payload FROM approval_queue WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            raw = row["payload"] if row is not None else None
            if raw is not None:
                self._conn.execute(
                    "UPDATE approval_queue SET payload = NULL WHERE approval_id = ?",
                    (approval_id,),
                )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        if row is None:
            raise KeyError(f"Approval not found: {approval_id}")
        entry = self._pending.get(approval_id)
        if entry is not None:
            entry.payload = None
        return self._deserialize_params(raw) if raw is not None else None

    def _persist_command_intent(self, params: dict, allow_always: bool = False) -> None:
        if not isinstance(params, dict):
            return
        command = str(params.get("command") or "")
        if not command:
            return
        # Same params, same scope as the elevated mode resolved just above:
        # the grant belongs to the session that was prompted.
        session_key = str(params.get("sessionKey") or "")
        try:
            from agentos.application.intent_cache import get_intent_cache

            cache = get_intent_cache()
            if allow_always:
                cache.record_always(command, session_key=session_key)
            else:
                cache.record(command, session_key=session_key)
        except Exception:  # pragma: no cover — cache path is best-effort
            return

    def consume(self, approval_id: str) -> None:
        self._conn.execute("BEGIN IMMEDIATE")
        row = self._get_row(approval_id)
        if row is None:
            self._conn.rollback()
            raise KeyError(f"Approval not found: {approval_id}")
        entry = self._row_to_entry(row)
        if not entry.resolved or not entry.approved:
            self._conn.rollback()
            raise ValueError(f"Approval is not approved: {approval_id}")
        if entry.consumed:
            self._conn.rollback()
            raise ValueError(f"Approval already consumed: {approval_id}")
        cursor = self._conn.execute(
            "UPDATE approval_queue "
            "SET consumed = 1 "
            "WHERE approval_id = ? AND resolved = 1 AND approved = 1 AND consumed = 0",
            (approval_id,),
        )
        if cursor.rowcount != 1:
            self._conn.rollback()
            entry = self.get(approval_id)
            if entry.consumed:
                raise ValueError(f"Approval already consumed: {approval_id}")
            raise ValueError(f"Approval is not approved: {approval_id}")
        self._conn.commit()
        entry = self.get(approval_id)
        self._pending.pop(approval_id, None)

    def status(self, approval_id: str) -> dict:
        entry = self.get(approval_id)
        return {
            "id": entry.approval_id,
            "namespace": entry.namespace,
            "params": entry.params,
            "created_at": entry.created_at,
            "resolved": entry.resolved,
            "approved": entry.approved,
            "consumed": entry.consumed,
            "expires_at": entry.expires_at,
            "has_payload": entry.payload is not None,
            "audience_session_keys": list(entry.audience_session_keys),
        }

    def list_pending(self, namespace: str | None = None) -> list[dict]:
        self._sweep()
        if namespace:
            rows = self._conn.execute(
                "SELECT approval_id, namespace, params, created_at, expires_at, audience "
                "FROM approval_queue "
                "WHERE resolved = 0 AND namespace = ?",
                (namespace,),
            )
        else:
            rows = self._conn.execute(
                "SELECT approval_id, namespace, params, created_at, expires_at, audience "
                "FROM approval_queue "
                "WHERE resolved = 0",
            )
        return [
            {
                "id": str(row["approval_id"]),
                "namespace": str(row["namespace"]),
                "params": self._deserialize_params(row["params"]),
                "created_at": float(row["created_at"]),
                "expires_at": (float(row["expires_at"]) if row["expires_at"] is not None else None),
                "audience_session_keys": list(self._deserialize_audience(row["audience"])),
            }
            for row in rows
        ]

    def set_elevated_mode(self, session_key: str, mode: str | None) -> None:
        key = session_key.strip()
        if not key:
            raise ValueError("session_key is required")
        if mode in (None, "", "off"):
            self._session_elevated_modes.pop(key, None)
            return
        if mode not in VALID_ELEVATED_MODES:
            raise ValueError("mode must be one of: on, bypass, full, off")
        self._session_elevated_modes[key] = mode

    def get_elevated_mode(self, session_key: str | None) -> str | None:
        key = (session_key or "").strip()
        if not key:
            return None
        return self._session_elevated_modes.get(key)

    def cancel_pending_for_session(
        self, session_key: str, *, resolved_by: str = "abort"
    ) -> list[str]:
        """Deny every pending approval of ``session_key`` (any namespace).

        For a run that was stopped while it waited on a prompt: nothing will
        ever consume the answer, so the prompt must not stay on screen. Emits
        ``approval.resolved`` with ``decision: "cancelled"``. Returns the ids.
        """
        key = session_key.strip()
        if not key:
            return []
        cancelled: list[str] = []
        for row in self._conn.execute(
            "SELECT approval_id, namespace, params, created_at, resolved, approved, consumed, "
            "payload, expires_at, audience "
            "FROM approval_queue WHERE resolved = 0",
        ).fetchall():
            entry = self._row_to_entry(row)
            owner = entry.params.get("sessionKey") or entry.params.get("session_key")
            if str(owner or "").strip() != key:
                continue
            try:
                self.resolve(
                    entry.approval_id, False, resolved_by=resolved_by, decision="cancelled"
                )
            except (KeyError, ValueError):
                continue
            cancelled.append(entry.approval_id)
        return cancelled

    def sweep(self) -> None:
        """Expire rows whose lifespan ended and prune old resolved ones.

        Each expiry emits ``approval.resolved`` (``decision: "expired"``). The
        gateway runs this on a timer: a prompt nobody waits on any more (its
        run was stopped, the process restarted) would otherwise only expire
        when something next read the queue.
        """
        self._sweep()

    def resolve_pending_for_session(
        self,
        session_key: str,
        *,
        approved: bool,
        elevated_mode: str | None = None,
    ) -> int:
        key = session_key.strip()
        if not key:
            return 0
        count = 0
        for row in self._conn.execute(
            "SELECT approval_id, namespace, params, created_at, resolved, approved, consumed, "
            "payload, expires_at, audience "
            "FROM approval_queue "
            "WHERE resolved = 0 AND namespace = 'exec'",
        ).fetchall():
            entry = self._row_to_entry(row)
            if str(entry.params.get("sessionKey") or "").strip() != key:
                continue
            self.resolve(
                entry.approval_id,
                approved,
                elevated_mode=elevated_mode,
                resolved_by="session_elevation",
            )
            count += 1
        return count

    def get_settings(self, node_id: str | None = None) -> ApprovalSettings:
        settings = self._node_settings.get(node_id) if node_id else self._global_settings
        if settings is None:
            settings = self._global_settings
        return ApprovalSettings(
            mode=settings.mode,
            allow_patterns=list(settings.allow_patterns),
            deny_patterns=list(settings.deny_patterns),
        )

    def has_node_settings(self, node_id: str) -> bool:
        return node_id in self._node_settings

    def set_settings(
        self,
        mode: str,
        allow_patterns: list[str] | None = None,
        deny_patterns: list[str] | None = None,
        node_id: str | None = None,
    ) -> ApprovalSettings:
        if mode not in VALID_APPROVAL_MODES:
            raise ValueError(f"mode must be one of: {', '.join(sorted(VALID_APPROVAL_MODES))}")
        settings = ApprovalSettings(
            mode=mode,
            allow_patterns=list(allow_patterns or []),
            deny_patterns=list(deny_patterns or []),
        )
        if node_id is None:
            self._global_settings = settings
        else:
            self._node_settings[node_id] = settings
        return settings

    def close(self) -> None:
        self._conn.close()


_queue: ApprovalQueue | None = None


def get_approval_queue() -> ApprovalQueue:
    global _queue
    if _queue is None:
        _queue = ApprovalQueue()
    return _queue


def reset_approval_queue() -> None:
    global _queue
    if _queue is not None:
        path = _queue._db_path
        _queue.close()
        _queue = None
    else:
        path = _DEFAULT_APPROVAL_QUEUE_PATH
    try:
        path.unlink()
    except FileNotFoundError:
        pass
