"""Persistent DM pairing store shared by channel adapters and operator clients.

The behavior mirrors Hermes Agent's pairing boundary: cryptographic 8-character
codes, one-hour expiry, per-sender request throttling, a bounded pending queue,
failed-code lockout, and durable per-channel approved-user state.  JSON is used
intentionally so operators can inspect and back up the access boundary without
opening the main gateway config.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from agentos.paths import default_agentos_home

PAIRING_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
PAIRING_CODE_LENGTH = 8
PAIRING_CODE_TTL_S = 60 * 60
PAIRING_REQUEST_RATE_LIMIT_S = 10 * 60
PAIRING_MAX_PENDING = 3
PAIRING_MAX_FAILED_ATTEMPTS = 5
PAIRING_LOCKOUT_S = 60 * 60

PairingRequestStatus = Literal[
    "created",
    "pending",
    "approved",
    "rate_limited",
    "pending_limit",
]


class PairingStoreError(RuntimeError):
    """Base error for persistent channel pairing operations."""


class InvalidPairingCodeError(PairingStoreError):
    def __init__(self, *, attempts_remaining: int, locked_until: float = 0.0) -> None:
        self.attempts_remaining = max(0, attempts_remaining)
        self.locked_until = locked_until
        if locked_until:
            message = "Pairing approvals are locked for one hour after repeated invalid codes"
        else:
            message = (
                "Invalid or expired pairing code "
                f"({self.attempts_remaining} attempts remaining)"
            )
        super().__init__(message)


class PairingApprovalLockedError(PairingStoreError):
    def __init__(self, locked_until: float) -> None:
        self.locked_until = locked_until
        super().__init__("Pairing approvals are temporarily locked")


@dataclass(frozen=True, slots=True)
class PairingRequestResult:
    status: PairingRequestStatus
    code: str = ""
    created: bool = False
    retry_after_s: int = 0


def pairing_root() -> Path:
    return default_agentos_home() / "pairing"


class ChannelPairingStore:
    """File-backed pairing state scoped by configured channel account name."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.root = root or pairing_root()
        self._now = now
        self._thread_lock = threading.RLock()

    @staticmethod
    def _scope_key(channel_name: str) -> str:
        name = str(channel_name or "").strip()
        if not name:
            raise ValueError("channel_name is required")
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-.") or "channel"
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
        return f"{slug[:48]}-{digest}"

    def _pending_path(self, channel_name: str) -> Path:
        return self.root / f"{self._scope_key(channel_name)}-pending.json"

    def _approved_path(self, channel_name: str) -> Path:
        return self.root / f"{self._scope_key(channel_name)}-approved.json"

    @property
    def _control_path(self) -> Path:
        return self.root / "_rate_limits.json"

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.root, 0o700)
        lock_path = self.root / ".pairing.lock"
        with self._thread_lock, lock_path.open("a+b") as lock_file:
            with contextlib.suppress(OSError):
                os.chmod(lock_path, 0o600)
            def unlock() -> None:
                return None

            if os.name == "posix":
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

                def unlock() -> None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

            elif os.name == "nt":  # pragma: no cover - exercised on Windows CI.
                import msvcrt

                if lock_file.seek(0, os.SEEK_END) == 0:
                    lock_file.write(b"\0")
                    lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]

                def unlock() -> None:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            try:
                yield
            finally:
                unlock()

    @staticmethod
    def _read(path: Path, *, collection_key: str) -> dict[str, Any]:
        if not path.exists():
            return {"version": 1, collection_key: []}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PairingStoreError(f"Cannot read pairing state: {path.name}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get(collection_key), list):
            raise PairingStoreError(f"Invalid pairing state: {path.name}")
        return payload

    @staticmethod
    def _read_control(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {"version": 1, "channels": {}}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PairingStoreError("Cannot read pairing rate-limit state") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("channels"), dict):
            raise PairingStoreError("Invalid pairing rate-limit state")
        return payload

    def _write(self, path: Path, payload: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, path)
            os.chmod(path, 0o600)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise

    @staticmethod
    def _profile(sender_id: str, profile: dict[str, Any] | None) -> dict[str, str]:
        data = profile or {}
        return {
            "sender_id": str(sender_id).strip(),
            "username": str(data.get("username") or ""),
            "display_name": str(data.get("display_name") or ""),
            "chat_id": str(data.get("chat_id") or ""),
        }

    @staticmethod
    def _cleanup_pending(requests: list[dict[str, Any]], now: float) -> bool:
        active = [item for item in requests if float(item.get("expires_at") or 0) > now]
        if len(active) == len(requests):
            return False
        requests[:] = active
        return True

    @staticmethod
    def _channel_control(control: dict[str, Any], scope: str) -> dict[str, Any]:
        channels = control.setdefault("channels", {})
        raw: dict[str, Any] = channels.setdefault(scope, {})
        raw.setdefault("last_requests", {})
        raw.setdefault("failed_attempts", 0)
        raw.setdefault("locked_until", 0.0)
        return raw

    @staticmethod
    def _new_code(existing: set[str]) -> str:
        while True:
            code = "".join(
                secrets.choice(PAIRING_CODE_ALPHABET)
                for _ in range(PAIRING_CODE_LENGTH)
            )
            if code not in existing:
                return code

    def request(
        self,
        channel_name: str,
        sender_id: str,
        *,
        profile: dict[str, Any] | None = None,
    ) -> PairingRequestResult:
        sender_id = str(sender_id or "").strip()
        if not sender_id:
            raise ValueError("sender_id is required")
        now = self._now()
        scope = self._scope_key(channel_name)
        with self._locked():
            approved_doc = self._read(self._approved_path(channel_name), collection_key="users")
            if any(str(item.get("sender_id") or "") == sender_id for item in approved_doc["users"]):
                return PairingRequestResult(status="approved")

            pending_path = self._pending_path(channel_name)
            pending_doc = self._read(pending_path, collection_key="requests")
            requests = pending_doc["requests"]
            pending_changed = self._cleanup_pending(requests, now)
            existing = next(
                (item for item in requests if str(item.get("sender_id") or "") == sender_id),
                None,
            )
            if existing is not None:
                if pending_changed:
                    self._write(pending_path, pending_doc)
                return PairingRequestResult(status="pending", code=str(existing["code"]))

            control = self._read_control(self._control_path)
            channel_control = self._channel_control(control, scope)
            last_requests = channel_control["last_requests"]
            last_request = float(last_requests.get(sender_id) or 0)
            elapsed = now - last_request
            if last_request and elapsed < PAIRING_REQUEST_RATE_LIMIT_S:
                if pending_changed:
                    self._write(pending_path, pending_doc)
                return PairingRequestResult(
                    status="rate_limited",
                    retry_after_s=max(1, int(PAIRING_REQUEST_RATE_LIMIT_S - elapsed)),
                )
            if len(requests) >= PAIRING_MAX_PENDING:
                if pending_changed:
                    self._write(pending_path, pending_doc)
                return PairingRequestResult(status="pending_limit")

            code = self._new_code({str(item.get("code") or "") for item in requests})
            requests.append(
                {
                    **self._profile(sender_id, profile),
                    "code": code,
                    "created_at": now,
                    "expires_at": now + PAIRING_CODE_TTL_S,
                }
            )
            last_requests[sender_id] = now
            self._write(pending_path, pending_doc)
            self._write(self._control_path, control)
            return PairingRequestResult(status="created", code=code, created=True)

    def snapshot(self, channel_name: str) -> dict[str, Any]:
        now = self._now()
        scope = self._scope_key(channel_name)
        with self._locked():
            pending_path = self._pending_path(channel_name)
            pending_doc = self._read(pending_path, collection_key="requests")
            if self._cleanup_pending(pending_doc["requests"], now):
                self._write(pending_path, pending_doc)
            approved_doc = self._read(self._approved_path(channel_name), collection_key="users")
            control = self._read_control(self._control_path)
            channel_control = self._channel_control(control, scope)
            locked_until = float(channel_control.get("locked_until") or 0)
            return {
                "pending": [dict(item) for item in pending_doc["requests"]],
                "approved": [dict(item) for item in approved_doc["users"]],
                "locked_until": locked_until if locked_until > now else 0.0,
            }

    def is_approved(self, channel_name: str, sender_id: str) -> bool:
        sender_id = str(sender_id or "").strip()
        if not sender_id:
            return False
        with self._locked():
            approved_doc = self._read(self._approved_path(channel_name), collection_key="users")
            return any(
                str(item.get("sender_id") or "") == sender_id
                for item in approved_doc["users"]
            )

    def approve(self, channel_name: str, code: str) -> dict[str, Any]:
        normalized_code = str(code or "").strip().upper()
        if not normalized_code:
            raise ValueError("pairing code is required")
        now = self._now()
        scope = self._scope_key(channel_name)
        with self._locked():
            control = self._read_control(self._control_path)
            channel_control = self._channel_control(control, scope)
            locked_until = float(channel_control.get("locked_until") or 0)
            if locked_until > now:
                raise PairingApprovalLockedError(locked_until)
            if locked_until:
                channel_control["locked_until"] = 0.0
                channel_control["failed_attempts"] = 0

            pending_path = self._pending_path(channel_name)
            pending_doc = self._read(pending_path, collection_key="requests")
            requests = pending_doc["requests"]
            self._cleanup_pending(requests, now)
            match = next(
                (
                    item
                    for item in requests
                    if str(item.get("code") or "").upper() == normalized_code
                ),
                None,
            )
            if match is None:
                failures = int(channel_control.get("failed_attempts") or 0) + 1
                channel_control["failed_attempts"] = failures
                locked = 0.0
                if failures >= PAIRING_MAX_FAILED_ATTEMPTS:
                    locked = now + PAIRING_LOCKOUT_S
                    channel_control["locked_until"] = locked
                self._write(pending_path, pending_doc)
                self._write(self._control_path, control)
                raise InvalidPairingCodeError(
                    attempts_remaining=PAIRING_MAX_FAILED_ATTEMPTS - failures,
                    locked_until=locked,
                )

            requests.remove(match)
            sender_id = str(match.get("sender_id") or "")
            approved_path = self._approved_path(channel_name)
            approved_doc = self._read(approved_path, collection_key="users")
            approved_doc["users"] = [
                item
                for item in approved_doc["users"]
                if str(item.get("sender_id") or "") != sender_id
            ]
            approved_doc["users"].append(
                {
                    **self._profile(sender_id, match),
                    "approved_at": now,
                }
            )
            channel_control["failed_attempts"] = 0
            channel_control["locked_until"] = 0.0
            self._write(pending_path, pending_doc)
            self._write(approved_path, approved_doc)
            self._write(self._control_path, control)
            return dict(match)

    def deny(self, channel_name: str, sender_id: str) -> dict[str, Any]:
        sender_id = str(sender_id or "").strip()
        if not sender_id:
            raise ValueError("sender_id is required")
        with self._locked():
            pending_path = self._pending_path(channel_name)
            pending_doc = self._read(pending_path, collection_key="requests")
            match = next(
                (
                    item
                    for item in pending_doc["requests"]
                    if str(item.get("sender_id") or "") == sender_id
                ),
                None,
            )
            if match is None:
                raise KeyError(f"Pairing request not found: {sender_id}")
            pending_doc["requests"].remove(match)
            self._write(pending_path, pending_doc)
            return dict(match)

    def revoke(self, channel_name: str, sender_id: str) -> dict[str, Any]:
        sender_id = str(sender_id or "").strip()
        if not sender_id:
            raise ValueError("sender_id is required")
        with self._locked():
            approved_path = self._approved_path(channel_name)
            approved_doc = self._read(approved_path, collection_key="users")
            match = next(
                (
                    item
                    for item in approved_doc["users"]
                    if str(item.get("sender_id") or "") == sender_id
                ),
                None,
            )
            if match is None:
                raise KeyError(f"Approved sender not found: {sender_id}")
            approved_doc["users"].remove(match)
            self._write(approved_path, approved_doc)
            return dict(match)

    def clear_pending(self, channel_name: str) -> int:
        with self._locked():
            pending_path = self._pending_path(channel_name)
            pending_doc = self._read(pending_path, collection_key="requests")
            count = len(pending_doc["requests"])
            pending_doc["requests"] = []
            self._write(pending_path, pending_doc)
            return count


__all__ = [
    "ChannelPairingStore",
    "InvalidPairingCodeError",
    "PAIRING_CODE_LENGTH",
    "PAIRING_CODE_TTL_S",
    "PAIRING_LOCKOUT_S",
    "PAIRING_MAX_FAILED_ATTEMPTS",
    "PAIRING_MAX_PENDING",
    "PAIRING_REQUEST_RATE_LIMIT_S",
    "PairingApprovalLockedError",
    "PairingRequestResult",
    "PairingStoreError",
    "pairing_root",
]
