"""Shared value types for Image Flows.

Kept dependency-free so the schema, store and executor can import it without
pulling in providers or the gateway.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

#: The two link types. Compatibility is exact string equality.
TEXT = "TEXT"
IMAGE = "IMAGE"
LINK_TYPES: dict[str, dict[str, str]] = {
    TEXT: {"color": "#c4c4cc"},
    IMAGE: {"color": "#60a5fa"},
}

#: Wire / storage format version of a flow document and of the node schema.
FLOW_VERSION = 1
SCHEMA_VERSION = 1

RUN_STATUSES = ("queued", "running", "done", "failed", "cancelled")
NODE_STATES = ("queued", "running", "cached", "done", "error", "skipped")
RUN_ORIGINS = ("canvas", "agent", "cli")


class FlowError(Exception):
    """A coded error the RPC layer maps one-to-one onto its error shape.

    ``code`` is one of ``flows.not_found``, ``flows.conflict``,
    ``flows.invalid``, ``flows.busy``, ``flows.unavailable``,
    ``flows.cost_gate`` (and ``flows.operator_required`` at the RPC layer).
    """

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class NodeError(Exception):
    """A node failed while running. ``code`` is short and machine readable."""

    def __init__(self, code: str, message: str, *, input: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.input = input

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.input:
            out["input"] = self.input
        return out


@dataclass(frozen=True)
class ImageRef:
    """A content-addressed image in the flows media store."""

    sha256: str
    mime: str
    width: int
    height: int
    bytes: int

    def to_wire(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "mime": self.mime,
            "width": self.width,
            "height": self.height,
            "bytes": self.bytes,
        }

    @classmethod
    def from_wire(cls, raw: Any) -> ImageRef | None:
        """Parse a wire ImageRef; ``None`` when it is not one."""
        if not isinstance(raw, dict):
            return None
        sha = raw.get("sha256")
        if not isinstance(sha, str) or not _is_hex_digest(sha):
            return None
        try:
            return cls(
                sha256=sha.lower(),
                mime=str(raw.get("mime") or "image/png"),
                width=int(raw.get("width") or 0),
                height=int(raw.get("height") or 0),
                bytes=int(raw.get("bytes") or 0),
            )
        except (TypeError, ValueError):
            return None


def _is_hex_digest(value: str) -> bool:
    if len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _is_hex_digest(value)


def utc_now_iso(ts: float | None = None) -> str:
    """``2026-10-29T10:00:00Z`` for *ts* (default now)."""
    moment = datetime.fromtimestamp(ts if ts is not None else time.time(), tz=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# ── prices ─────────────────────────────────────────────────────────────────


def price_free() -> dict[str, Any]:
    return {"kind": "free", "approximate": False}


def price_usd(value: float, *, approximate: bool = True) -> dict[str, Any]:
    return {"kind": "usd", "usd": round(float(value), 6), "approximate": approximate}


def price_range(low: float, high: float, *, approximate: bool = True) -> dict[str, Any]:
    if abs(high - low) < 1e-12:
        return price_usd(low, approximate=approximate)
    return {
        "kind": "range",
        "min": round(float(low), 6),
        "max": round(float(high), 6),
        "approximate": approximate,
    }


def price_unknown() -> dict[str, Any]:
    return {"kind": "unknown", "approximate": True}


def price_bounds(price: dict[str, Any]) -> tuple[float, float] | None:
    """``(min, max)`` in USD, or ``None`` for an unknown price."""
    kind = price.get("kind")
    if kind == "free":
        return 0.0, 0.0
    if kind == "usd":
        value = float(price.get("usd") or 0.0)
        return value, value
    if kind == "range":
        return float(price.get("min") or 0.0), float(price.get("max") or 0.0)
    return None


def sum_prices(prices: list[dict[str, Any]]) -> dict[str, Any]:
    """Total of *prices* (section 5 of the contract).

    Any ``unknown`` makes the total ``unknown``; it then carries the known
    part as ``min`` when that part is not zero, so a client can still say
    "at least $x". Free-only totals are ``free``.
    """
    low = high = 0.0
    unknown = False
    approximate = False
    paid = False
    for price in prices:
        bounds = price_bounds(price)
        if bounds is None:
            unknown = True
            continue
        if price.get("kind") != "free":
            paid = True
        approximate = approximate or bool(price.get("approximate"))
        low += bounds[0]
        high += bounds[1]
    if unknown:
        out = price_unknown()
        if low > 0:
            out["min"] = round(low, 6)
        return out
    if not paid:
        return price_free()
    return price_range(low, high, approximate=approximate)


def price_upper_usd(price: dict[str, Any]) -> float | None:
    """The figure a cost gate compares: the max of a range, ``None`` if unknown."""
    bounds = price_bounds(price)
    return None if bounds is None else bounds[1]
