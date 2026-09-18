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
from typing import Any, Final

from agentos.attachment_refs import _atomic_write_bytes

DEFAULT_TOOL_RESULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_TOOL_RESULT_DISK_BUDGET_BYTES = 256 * 1024 * 1024
DEFAULT_TOOL_RESULT_RETENTION_SECONDS = 7 * 24 * 60 * 60
DEFAULT_TOOL_RESULT_EXPIRY_INTERVAL_SECONDS: Final[float] = 60.0
TOOL_RESULT_STORE_SESSION_BUCKET = "s"
TOOL_RESULT_CONTENT_NAME = "content.txt"
TOOL_RESULT_META_NAME = "meta.json"

_SAFE_TOKEN_RE = re.compile(r"[^A-Za-z0-9._-]+")
_LAST_EXPIRY_BY_ROOT: Final[dict[Path, float]] = {}


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


class ToolResultStore:
    """Store full raw tool results omitted from provider context."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

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
        expiry_interval_seconds: float | None = DEFAULT_TOOL_RESULT_EXPIRY_INTERVAL_SECONDS,
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

        self._maintain(
            incoming_bytes=size_bytes,
            retention_seconds=retention_seconds,
            disk_budget_bytes=disk_budget_bytes,
            expiry_interval_seconds=expiry_interval_seconds,
        )

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

    def _should_run_expiry(self, interval: float | None) -> bool:
        if interval is None or interval <= 0:
            return True
        now = time.monotonic()
        root_key = self.root.resolve()
        last = _LAST_EXPIRY_BY_ROOT.get(root_key, 0.0)
        if now - last >= interval:
            _LAST_EXPIRY_BY_ROOT[root_key] = now
            return True
        return False

    def _maintain(
        self,
        *,
        incoming_bytes: int,
        retention_seconds: int | None,
        disk_budget_bytes: int | None,
        expiry_interval_seconds: float | None = DEFAULT_TOOL_RESULT_EXPIRY_INTERVAL_SECONDS,
    ) -> None:
        """Run expiry and budget pruning over a single scan of the store.

        Both passes need the same metadata, and ``_iter_records`` walks the
        whole store and JSON-parses every record to produce it. Scanning once
        and sharing the result halves that cost on a path that runs for every
        tool result. Expiry is throttled on a timer rather than running on
        every write. When neither retention is due nor a budget is configured,
        scanning is skipped entirely.
        """
        run_expiry = retention_seconds is not None and self._should_run_expiry(
            expiry_interval_seconds
        )
        if not run_expiry and disk_budget_bytes is None:
            return

        budget: int | None = None
        if disk_budget_bytes is not None:
            budget = max(0, int(disk_budget_bytes))
            # A snapshot larger than the whole budget can never be made to fit,
            # so pruning first only destroys unrelated records on the way to
            # the same error. The caller records a `skipped` metric and carries
            # on, which made that loss silent. Raise before touching anything.
            if incoming_bytes > budget:
                raise ToolResultStoreBudgetError(
                    f"tool result snapshot exceeds disk budget ({incoming_bytes} > {budget})"
                )

        records = self._iter_records()
        if run_expiry and retention_seconds is not None:
            records = self._remove_expired(records, retention_seconds)
        if budget is not None:
            self._prune_to_fit(records, incoming_bytes, budget)

    def _remove_expired(
        self, records: list[_StoredMeta], retention_seconds: int
    ) -> list[_StoredMeta]:
        """Drop records past retention and return the survivors."""
        cutoff = datetime.now(UTC) - timedelta(seconds=max(0, int(retention_seconds)))
        survivors: list[_StoredMeta] = []
        for record in records:
            if record.created_at < cutoff:
                _remove_record_dir(record.record_dir)
            else:
                survivors.append(record)
        return survivors

    def _prune_to_fit(self, records: list[_StoredMeta], incoming_bytes: int, budget: int) -> None:
        ordered = sorted(records, key=lambda item: item.created_at)
        current = sum(record.size_bytes for record in ordered)
        if current + incoming_bytes <= budget:
            return
        for record in ordered:
            _remove_record_dir(record.record_dir)
            current = max(0, current - record.size_bytes)
            if current + incoming_bytes <= budget:
                return


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
