"""Persistent raw tool-result storage for provider-context projections."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agentos.attachment_refs import _atomic_write_bytes

DEFAULT_TOOL_RESULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_TOOL_RESULT_DISK_BUDGET_BYTES = 256 * 1024 * 1024
DEFAULT_TOOL_RESULT_RETENTION_SECONDS = 7 * 24 * 60 * 60
DEFAULT_TOOL_RESULT_EXPIRY_SWEEP_INTERVAL_SECONDS = 60.0
TOOL_RESULT_STORE_SESSION_BUCKET = "s"
TOOL_RESULT_CONTENT_NAME = "content.txt"
TOOL_RESULT_META_NAME = "meta.json"

_SAFE_TOKEN_RE = re.compile(r"[^A-Za-z0-9._-]+")


class ToolResultStoreBudgetError(ValueError):
    """Raised when a raw tool-result snapshot exceeds store budgets."""


@dataclass(frozen=True)
class ToolResultRecord:
    handle: str
    tool_use_id: str
    tool_name: str
    session_id: str
    session_key: str
    agent_id: str
    sha256: str
    chars: int
    size_bytes: int
    created_at: str
    content: str


@dataclass(frozen=True)
class _StoredMeta:
    handle: str
    session_id: str
    created_at: datetime
    size_bytes: int
    record_dir: Path


@dataclass
class _ScanState:
    """What the last full scan of one store root established.

    ``usage_bytes`` is an upper bound between scans: every successful write
    adds to it and nothing ever subtracts without a rescan, so a record
    removed behind our back only costs one extra scan, never an overshoot.
    """

    usage_bytes: int | None = None
    last_sweep_monotonic: float | None = None


# Keyed by store root. The engine builds a fresh ``ToolResultStore`` for every
# write, so the sweep clock and the usage bound cannot live on the instance.
_scan_state_by_root: dict[str, _ScanState] = {}


class ToolResultStore:
    """Store full raw tool results omitted from provider context.

    Expiry and disk-budget accounting both need a full walk of the store
    (``rglob`` plus a ``json.loads`` per record). That walk used to run twice
    on every ``write`` — once per pass — which put work proportional to the
    whole store on the turn loop for each tool call (#2126). Now a write
    scans at most once, the expiry sweep runs on a timer, and the budget
    check consults a cached usage bound so a store comfortably under budget
    writes without scanning at all.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        expiry_sweep_interval_seconds: float = DEFAULT_TOOL_RESULT_EXPIRY_SWEEP_INTERVAL_SECONDS,
    ) -> None:
        self.root = Path(root)
        self._expiry_sweep_interval_seconds = max(0.0, float(expiry_sweep_interval_seconds))

    def write(
        self,
        content: str,
        *,
        tool_use_id: str,
        tool_name: str,
        session_id: str,
        session_key: str,
        agent_id: str,
        max_bytes: int | None = DEFAULT_TOOL_RESULT_MAX_BYTES,
        disk_budget_bytes: int | None = DEFAULT_TOOL_RESULT_DISK_BUDGET_BYTES,
        retention_seconds: int | None = DEFAULT_TOOL_RESULT_RETENTION_SECONDS,
    ) -> ToolResultRecord:
        session_id = _validate_non_empty("session_id", session_id)
        session_key = _validate_non_empty("session_key", session_key)
        agent_id = _validate_non_empty("agent_id", agent_id)
        payload = content.encode("utf-8")
        size_bytes = len(payload)
        if size_bytes == 0:
            raise ToolResultStoreBudgetError("tool result snapshot is empty")
        if max_bytes is not None and size_bytes > max_bytes:
            raise ToolResultStoreBudgetError(
                f"tool result snapshot exceeds per-result budget ({size_bytes} > {max_bytes})"
            )

        state = self._scan_state()
        if disk_budget_bytes is not None:
            _check_fits_budget(size_bytes, disk_budget_bytes)
        records: list[_StoredMeta] | None = None
        if retention_seconds is not None and self._expiry_sweep_due(state):
            records = self._scan(state)
            records = self._remove_expired(records, state, retention_seconds)
            state.last_sweep_monotonic = time.monotonic()
        if disk_budget_bytes is not None:
            if records is None and not _fits_cached_usage(state, size_bytes, disk_budget_bytes):
                records = self._scan(state)
            if records is not None:
                self._prune_to_fit(records, state, size_bytes, disk_budget_bytes)

        sha = hashlib.sha256(payload).hexdigest()
        created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        for _attempt in range(5):
            handle = f"tr-{secrets.token_hex(16)}"
            record_dir = self._record_dir(handle, session_id=session_id)
            try:
                record_dir.mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                continue
            record = ToolResultRecord(
                handle=handle,
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                session_id=session_id,
                session_key=session_key,
                agent_id=agent_id,
                sha256=sha,
                chars=len(content),
                size_bytes=size_bytes,
                created_at=created_at,
                content=content,
            )
            try:
                _atomic_write_bytes(record_dir / TOOL_RESULT_CONTENT_NAME, payload)
                _atomic_write_bytes(
                    record_dir / TOOL_RESULT_META_NAME,
                    json.dumps(
                        {
                            "handle": record.handle,
                            "tool_use_id": record.tool_use_id,
                            "tool_name": record.tool_name,
                            "session_id": record.session_id,
                            "session_key": record.session_key,
                            "agent_id": record.agent_id,
                            "sha256": record.sha256,
                            "chars": record.chars,
                            "size_bytes": record.size_bytes,
                            "created_at": record.created_at,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ).encode("utf-8"),
                )
            except BaseException:
                _remove_record_dir(record_dir)
                raise
            if state.usage_bytes is not None:
                state.usage_bytes += size_bytes
            return record
        raise FileExistsError("could not allocate unique tool result handle")

    def read(self, handle: str, *, session_id: str) -> ToolResultRecord:
        session_id = _validate_non_empty("session_id", session_id)
        normalized = _validate_handle(handle)
        record_dir = self._record_dir(normalized, session_id=session_id)
        meta_path = record_dir / TOOL_RESULT_META_NAME
        content_path = record_dir / TOOL_RESULT_CONTENT_NAME
        meta: dict[str, Any] = json.loads(meta_path.read_text(encoding="utf-8"))
        content = content_path.read_text(encoding="utf-8")
        payload = content.encode("utf-8")
        sha = hashlib.sha256(payload).hexdigest()
        if meta.get("session_id") != session_id:
            raise ValueError("tool result session mismatch")
        if sha != meta.get("sha256"):
            raise ValueError("tool result hash mismatch")
        size_bytes = int(meta.get("size_bytes") or 0)
        if size_bytes != len(payload):
            raise ValueError("tool result size mismatch")
        return ToolResultRecord(
            handle=normalized,
            tool_use_id=str(meta.get("tool_use_id") or ""),
            tool_name=str(meta.get("tool_name") or ""),
            session_id=str(meta.get("session_id") or session_id),
            session_key=str(meta.get("session_key") or ""),
            agent_id=str(meta.get("agent_id") or ""),
            sha256=sha,
            chars=len(content),
            size_bytes=len(payload),
            created_at=str(meta.get("created_at") or ""),
            content=content,
        )

    def _record_dir(self, handle: str, *, session_id: str) -> Path:
        normalized = _validate_handle(handle)
        return (
            self.root
            / TOOL_RESULT_STORE_SESSION_BUCKET
            / _safe_token(_validate_non_empty("session_id", session_id))
            / normalized[3:5]
            / normalized
        )

    def _scan_state(self) -> _ScanState:
        key = str(self.root.resolve())
        state = _scan_state_by_root.get(key)
        if state is None:
            state = _ScanState()
            _scan_state_by_root[key] = state
        return state

    def _scan(self, state: _ScanState) -> list[_StoredMeta]:
        """One full walk; refreshes the usage bound with what is really there."""
        records = self._iter_records()
        state.usage_bytes = sum(record.size_bytes for record in records)
        return records

    def _expiry_sweep_due(self, state: _ScanState) -> bool:
        if state.last_sweep_monotonic is None:
            return True
        return time.monotonic() - state.last_sweep_monotonic >= self._expiry_sweep_interval_seconds

    def _iter_records(self) -> list[_StoredMeta]:
        root = self.root / TOOL_RESULT_STORE_SESSION_BUCKET
        if not root.exists():
            return []
        records: list[_StoredMeta] = []
        for meta_path in root.rglob(TOOL_RESULT_META_NAME):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                handle = _validate_handle(str(meta.get("handle") or ""))
                session_id = _validate_non_empty("session_id", meta.get("session_id"))
                created_at = _parse_created_at(str(meta.get("created_at") or ""))
                size_bytes = int(meta.get("size_bytes") or 0)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            records.append(
                _StoredMeta(
                    handle=handle,
                    session_id=session_id,
                    created_at=created_at,
                    size_bytes=max(0, size_bytes),
                    record_dir=meta_path.parent,
                )
            )
        return records

    def _disk_usage_bytes(self) -> int:
        total = 0
        for record in self._iter_records():
            total += record.size_bytes
        return total

    def _remove_expired(
        self,
        records: list[_StoredMeta],
        state: _ScanState,
        retention_seconds: int,
    ) -> list[_StoredMeta]:
        """Drop expired records from disk and from ``records``; return the survivors."""
        cutoff = datetime.now(UTC) - timedelta(seconds=max(0, int(retention_seconds)))
        kept: list[_StoredMeta] = []
        for record in records:
            if record.created_at < cutoff:
                _remove_record_dir(record.record_dir)
                continue
            kept.append(record)
        state.usage_bytes = sum(record.size_bytes for record in kept)
        return kept

    def _prune_to_fit(
        self,
        records: list[_StoredMeta],
        state: _ScanState,
        incoming_bytes: int,
        disk_budget_bytes: int,
    ) -> None:
        budget = max(0, int(disk_budget_bytes))
        _check_fits_budget(incoming_bytes, budget)
        ordered = sorted(records, key=lambda item: item.created_at)
        current = sum(record.size_bytes for record in ordered)
        if current + incoming_bytes <= budget:
            return
        # Free a little more than strictly needed. Pruning to exactly the
        # budget leaves the store sitting on the line, and every following
        # write would pay for a full scan to shave off one more record.
        target = budget - _prune_headroom_bytes(budget)
        for record in ordered:
            _remove_record_dir(record.record_dir)
            current = max(0, current - record.size_bytes)
            if current + incoming_bytes <= target:
                break
        state.usage_bytes = current


def _check_fits_budget(incoming_bytes: int, disk_budget_bytes: int) -> None:
    # A snapshot larger than the whole budget can never be made to fit, so
    # pruning first only destroys unrelated records on the way to the same
    # error. The caller records a `skipped` metric and carries on, which
    # made that loss silent.
    budget = max(0, int(disk_budget_bytes))
    if incoming_bytes > budget:
        raise ToolResultStoreBudgetError(
            f"tool result snapshot exceeds disk budget ({incoming_bytes} > {budget})"
        )


def _fits_cached_usage(state: _ScanState, incoming_bytes: int, disk_budget_bytes: int) -> bool:
    if state.usage_bytes is None:
        return False
    return state.usage_bytes + incoming_bytes <= max(0, int(disk_budget_bytes))


def _prune_headroom_bytes(budget: int) -> int:
    return min(budget // 16, 16 * 1024 * 1024)


def _parse_created_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _validate_handle(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("tr-"):
        raise ValueError("tool result handle is invalid")
    suffix = value[3:]
    if len(suffix) != 32 or any(ch not in "0123456789abcdef" for ch in suffix):
        raise ValueError("tool result handle is invalid")
    return value


def _validate_non_empty(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _safe_token(value: str) -> str:
    token = _SAFE_TOKEN_RE.sub("-", value.strip()).strip(".-")
    return token[:80] or "session"


def _remove_record_dir(record_dir: Path) -> None:
    for path in sorted(record_dir.glob("*"), reverse=True):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        record_dir.rmdir()
    except OSError:
        pass
