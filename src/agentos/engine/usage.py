"""Per-session token usage tracking and cost estimation."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from agentos.session.keys import normalize_agent_id
from agentos.util.bounded_registry import BoundedSessionRegistry

from .pricing import calculate_cost_usd, lookup_price

log = structlog.get_logger(__name__)


def parse_session_key_scope(session_key: str) -> tuple[str, str]:
    """Parse (agent_id, channel) from session_key."""
    key = str(session_key or "").strip()
    if key.startswith("subagent:"):
        key = key[9:]
    if not key.startswith("agent:"):
        return "main", "system"

    parts = key.split(":")
    if len(parts) < 3:
        return "main", "system"

    agent_id = normalize_agent_id(parts[1])
    channel = parts[2]
    # Normalize channel names: if it is main/webchat/direct/subagent/etc.
    if channel in {"main", "direct", "subagent"}:
        channel = "system"
    elif channel == "webchat":
        channel = "webchat"
    return agent_id, channel


def _scope_ceiling(config: Any, field: str, scope_id: str) -> float | None:
    """Read one per-scope ceiling from a budgets config, tolerating absence.

    ``config`` is typed loosely because the engine must not import gateway
    config; anything exposing the documented ``[budgets]`` field names works.
    """
    mapping = getattr(config, field, None)
    if not isinstance(mapping, dict):
        return None
    amount = mapping.get(scope_id)
    return float(amount) if amount is not None else None


_current_usage_scope: ContextVar[str | None] = ContextVar(
    "agentos_usage_scope",
    default=None,
)

_current_tool_name: ContextVar[str | None] = ContextVar(
    "agentos_current_tool_name",
    default=None,
)

_current_skill_name: ContextVar[str | None] = ContextVar(
    "agentos_current_skill_name",
    default=None,
)


@contextmanager
def usage_scope(scope_key: str | None) -> Iterator[None]:
    """Attribute UsageTracker.add calls in this context to scope_key."""
    if not scope_key:
        yield
        return
    token = _current_usage_scope.set(scope_key)
    try:
        yield
    finally:
        _current_usage_scope.reset(token)


@dataclass
class ModelUsage:
    """Token usage for a single model within a session."""

    model_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # Provider-billed cost accumulated across every raw provider call attributed
    # to this model. New field appended at the end so existing positional
    # callers (ModelUsage(model_id, in, out)) continue to align. When > 0 the
    # model_breakdown serializer prefers this over the pricing-table estimate,
    # avoiding cache-discount drift in the per-model split.
    billed_cost: float = 0.0
    provider_id: str = ""

    @property
    def cost(self) -> float:
        price = lookup_price(self.model_id, provider_id=self.provider_id)
        return calculate_cost_usd(
            price,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cached_input_tokens=self.cache_read_tokens,
        )


@dataclass
class SessionUsage:
    """Accumulated token usage and cost for a single session."""

    input_tokens: int = 0
    output_tokens: int = 0
    model_id: str = ""
    _per_model: dict[tuple[str, str], ModelUsage] | None = None
    # New cache counters appended at the end so existing positional callers
    # (e.g. SessionUsage(1, 2, "model")) keep aligning with `model_id`.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    provider_id: str = ""

    @property
    def cost(self) -> float:
        """Calculate cost in USD based on pricing table."""
        if self._per_model:
            return sum(m.cost for m in self._per_model.values())
        price = lookup_price(self.model_id, provider_id=self.provider_id)
        return calculate_cost_usd(
            price,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cached_input_tokens=self.cache_read_tokens,
        )

    @property
    def billed_cost(self) -> float:
        """Sum of provider-billed cost across every model in this session.

        Returns 0.0 when no per-model billed data has been captured (e.g.
        provider returned no cost, or session is estimate-only). Callers
        use this to decide whether the session-level row should display
        the actual billed total or fall back to the pricing-table estimate.
        """
        if not self._per_model:
            return 0.0
        return sum(float(getattr(m, "billed_cost", 0.0) or 0.0) for m in self._per_model.values())

    @property
    def total_cost(self) -> float:
        """Best per-session cost: real billed where available, estimate elsewhere.

        Mixed-source sessions need this so the row total doesn't under-report
        the unbilled portion. For each model: prefer ``mu.billed_cost`` when
        > 0, otherwise contribute the pricing-table estimate ``mu.cost``.
        Sum equals the breakdown's per-model ``costUsd`` sum by construction
        (since the breakdown serializer makes the same per-model decision).
        """
        if not self._per_model:
            return self.cost
        return sum(
            (float(getattr(m, "billed_cost", 0.0) or 0.0) or m.cost)
            for m in self._per_model.values()
        )

    @property
    def cost_source(self) -> str:
        """Aggregate cost source for the session row.

        - ``provider_billed``: every per-model entry has a real billed total.
        - ``mixed``: some models billed, others estimate-only.
        - ``agentos_estimate``: no billed data at all, or provider returned
          no cost for any call.
        """
        if not self._per_model:
            return "agentos_estimate"
        billed_count = sum(
            1 for m in self._per_model.values() if float(getattr(m, "billed_cost", 0.0) or 0.0) > 0
        )
        if billed_count == 0:
            return "agentos_estimate"
        if billed_count == len(self._per_model):
            return "provider_billed"
        return "mixed"

    def add(
        self,
        input_tokens: int,
        output_tokens: int,
        model_id: str = "",
        *,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        billed_cost: float = 0.0,
        provider_id: str = "",
    ) -> None:
        """Accumulate token counts, tracking per-model breakdown.

        ``billed_cost`` is the provider-reported real billed cost for this
        accumulation (typically one provider call). Forwarded into the per-model
        ``ModelUsage`` so the breakdown serializer can return the actual billed
        figure instead of the cache-blind pricing-table estimate.
        """
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_read_tokens += cache_read_tokens
        self.cache_write_tokens += cache_write_tokens
        if provider_id:
            self.provider_id = provider_id
        mid = model_id or self.model_id
        if mid:
            if self._per_model is None:
                self._per_model = {}
            resolved_provider = str(provider_id or self.provider_id).strip().lower()
            usage_key = (resolved_provider, mid)
            mu = self._per_model.get(usage_key)
            if mu is None:
                mu = ModelUsage(model_id=mid, provider_id=resolved_provider)
                self._per_model[usage_key] = mu
            mu.input_tokens += input_tokens
            mu.output_tokens += output_tokens
            mu.cache_read_tokens += cache_read_tokens
            mu.cache_write_tokens += cache_write_tokens
            mu.billed_cost += billed_cost

    @staticmethod
    def _breakdown_cost_fields(mu_or_self: ModelUsage | SessionUsage) -> dict:
        """Pick the canonical cost + source for a single breakdown row.

        Prefer the real provider-billed cost when present; otherwise fall back
        to the local pricing-table estimate. This is what lets the WebUI show
        per-model values that actually sum to the row total without prorating.
        """
        billed = float(getattr(mu_or_self, "billed_cost", 0.0) or 0.0)
        estimate = float(mu_or_self.cost or 0.0)
        if billed > 0:
            return {
                "costUsd": round(billed, 6),
                "billedCostUsd": round(billed, 6),
                "estimatedCostUsd": round(estimate, 6),
                "costSource": "provider_billed",
            }
        return {
            "costUsd": round(estimate, 6),
            "billedCostUsd": 0.0,
            "estimatedCostUsd": round(estimate, 6),
            "costSource": "agentos_estimate" if estimate > 0 else "unavailable",
        }

    @property
    def model_breakdown(self) -> list[dict]:
        """Per-model usage breakdown for RPC serialisation."""
        if not self._per_model:
            if self.model_id:
                return [
                    {
                        "model": self.model_id,
                        "provider": self.provider_id,
                        "inputTokens": self.input_tokens,
                        "outputTokens": self.output_tokens,
                        "cacheReadTokens": self.cache_read_tokens,
                        "cacheWriteTokens": self.cache_write_tokens,
                        **SessionUsage._breakdown_cost_fields(self),
                    }
                ]
            return []
        return [
            {
                "model": mu.model_id,
                "provider": mu.provider_id,
                "inputTokens": mu.input_tokens,
                "outputTokens": mu.output_tokens,
                "cacheReadTokens": mu.cache_read_tokens,
                "cacheWriteTokens": mu.cache_write_tokens,
                **SessionUsage._breakdown_cost_fields(mu),
            }
            # Sort by the canonical cost (billed when present, estimate otherwise)
            # so the row order stays predictable even when some models lack
            # billed data.
            for mu in sorted(
                self._per_model.values(),
                key=lambda m: float(getattr(m, "billed_cost", 0.0) or 0.0) or m.cost,
                reverse=True,
            )
        ]


def _clone_session_usage(usage: SessionUsage) -> SessionUsage:
    clone = SessionUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        model_id=usage.model_id,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        provider_id=usage.provider_id,
    )
    if usage._per_model:
        clone._per_model = {
            usage_key: ModelUsage(
                model_id=mu.model_id,
                input_tokens=mu.input_tokens,
                output_tokens=mu.output_tokens,
                cache_read_tokens=mu.cache_read_tokens,
                cache_write_tokens=mu.cache_write_tokens,
                billed_cost=mu.billed_cost,
                provider_id=mu.provider_id,
            )
            for usage_key, mu in usage._per_model.items()
        }
    return clone


def _model_delta_cost(
    *,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    billed_cost: float,
    provider_id: str,
) -> float:
    if billed_cost > 0.0:
        return billed_cost
    price = lookup_price(model_id, provider_id=provider_id)
    return calculate_cost_usd(
        price,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cache_read_tokens,
    )


@dataclass
class SessionTotalsSnapshot:
    """Point-in-time aggregate of a session's token usage and cost.

    Embedded in `DoneEvent` so consumers do not need a follow-up
    `usage.status` RPC to render session totals. `None` on `DoneEvent`
    means "no snapshot available" (legacy replay), distinct from a
    populated snapshot whose numeric fields happen to be zero.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    billed_cost: float = 0.0

    @classmethod
    def from_session(cls, usage: SessionUsage) -> SessionTotalsSnapshot:
        return cls(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            cost_usd=usage.total_cost,
            billed_cost=usage.billed_cost,
        )


_global_usage_tracker: UsageTracker | None = None


class UsageTracker:
    """Tracks per-session token usage and cost."""

    def __init__(
        self,
        default_provider_id: str = "",
        db_path: str | None = None,
        *,
        ledger_db_path: str | None = None,
    ) -> None:
        """
        ``db_path`` backs the detailed ``usage_records`` history.

        ``ledger_db_path`` backs the spend ledger that budget ceilings read.
        It is deliberately a *separate* file from the session database: the
        ledger is written synchronously from the turn hot path, and sharing a
        file with the session store's async writer would make every ledger
        commit contend for that write lock on the event loop. The tracker owns
        this file's schema outright, so it is also not a second schema owner
        inside a migration-managed database.
        """
        global _global_usage_tracker
        self._sessions: dict[str, SessionUsage] = {}
        self._scopes: BoundedSessionRegistry[tuple[str, str], SessionUsage] = (
            BoundedSessionRegistry(max_entries=2000, session_scoped=True)
        )
        self._default_provider_id = str(default_provider_id or "").strip().lower()
        self._db_path = db_path
        self._ledger_db_path = ledger_db_path
        self._session_metadata: BoundedSessionRegistry[str, tuple[str, str]] = (
            BoundedSessionRegistry(max_entries=2000, session_scoped=True)
        )
        self._warned_keys: set[str] = set()
        # In-process mirror of the persisted ledger. Reads take the larger of
        # the two: a dropped write (sqlite busy, disk full) must not be able to
        # under-report spend and silently retire a ceiling.
        self._daily_spend: dict[tuple[str, str, str], float] = {}
        self._daily_spend_day = ""
        self._session_spend: dict[str, float] = {}
        self._session_active_skill: dict[str, str] = {}
        _global_usage_tracker = self

        if self._db_path:
            self._init_db()
        if self._ledger_db_path:
            self._init_ledger_db()

    def _connect(self, path: str) -> sqlite3.Connection:
        """Open a short-lived connection tuned for hot-path writes.

        WAL keeps readers off the write lock and ``synchronous=NORMAL`` drops
        the per-commit fsync, so a ledger write costs well under a
        millisecond. ``busy_timeout`` is deliberately short: this runs on the
        event loop, and blocking every session for seconds to record a cost
        row is worse than dropping the row (the in-process mirror covers it).
        """
        conn = sqlite3.connect(path, timeout=0.25)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=250")
        except Exception as e:  # noqa: BLE001 - pragmas are best-effort
            log.warning("usage_tracker.pragma_failed", error=str(e))
        return conn

    def _init_ledger_db(self) -> None:
        if not self._ledger_db_path:
            return
        conn = self._connect(self._ledger_db_path)
        try:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS spend_ledger (
                        day TEXT,
                        scope_kind TEXT,
                        scope_id TEXT,
                        cost_usd REAL NOT NULL DEFAULT 0.0,
                        PRIMARY KEY (day, scope_kind, scope_id)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS session_spend (
                        session_key TEXT PRIMARY KEY,
                        cost_usd REAL NOT NULL DEFAULT 0.0
                    )
                    """
                )
        except Exception as e:
            log.warning("usage_tracker.ledger_init_failed", error=str(e))
        finally:
            conn.close()

    def _init_db(self) -> None:
        if not self._db_path:
            return
        conn = sqlite3.connect(self._db_path)
        try:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS usage_records (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_key TEXT NOT NULL,
                        agent_id TEXT,
                        channel_type TEXT,
                        tool_name TEXT,
                        skill TEXT,
                        model TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        input_tokens INTEGER NOT NULL DEFAULT 0,
                        output_tokens INTEGER NOT NULL DEFAULT 0,
                        cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                        cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                        cost_usd REAL NOT NULL DEFAULT 0.0,
                        billed_cost_usd REAL NOT NULL DEFAULT 0.0,
                        created_at INTEGER NOT NULL
                    )
                    """
                )
        except Exception as e:
            log.warning("usage_tracker.db_init_failed", error=str(e))
        finally:
            conn.close()

    def _bump_daily_mirror(
        self, day: str, scope_kind: str, scope_id: str, incremental_cost: float
    ) -> None:
        """Accumulate one scope's spend in the in-process ledger mirror."""
        if incremental_cost <= 0.0:
            return
        if day != self._daily_spend_day:
            # Day rolled over. Yesterday's mirror can never be read again
            # (every lookup is keyed by the current UTC day), so drop it
            # rather than let a long-lived gateway accumulate one entry per
            # scope per day forever. The warn-once bookkeeping is dropped with
            # it: daily keys are dead, and a session that spans midnight is
            # worth re-warning about once on the new day.
            self._daily_spend.clear()
            self._warned_keys.clear()
            self._daily_spend_day = day
        ledger_key = (day, scope_kind, scope_id)
        self._daily_spend[ledger_key] = self._daily_spend.get(ledger_key, 0.0) + incremental_cost

    def get_spend(self, day: str, scope_kind: str, scope_id: str) -> float:
        """Return accumulated spend for one ledger scope on one UTC day.

        The persisted row and the in-process mirror are reconciled by taking
        the larger of the two. They diverge only when a write was dropped, and
        in that direction the persisted row under-reports — which would retire
        a ceiling rather than enforce it.
        """
        mirror = self._daily_spend.get((day, scope_kind, scope_id), 0.0)
        if not self._ledger_db_path:
            return mirror
        conn = self._connect(self._ledger_db_path)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT cost_usd FROM spend_ledger "
                "WHERE day = ? AND scope_kind = ? AND scope_id = ?",
                (day, scope_kind, scope_id),
            )
            row = cursor.fetchone()
            return max(mirror, float(row[0])) if row else mirror
        except Exception as e:
            log.warning("usage_tracker.ledger_query_failed", error=str(e))
            return mirror
        finally:
            conn.close()

    def get_session_db_cost(self, session_key: str) -> float:
        """Return the persisted lifetime cost recorded for one session."""
        if not self._ledger_db_path:
            return 0.0
        conn = self._connect(self._ledger_db_path)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT cost_usd FROM session_spend WHERE session_key = ?",
                (session_key,),
            )
            row = cursor.fetchone()
            return float(row[0]) if row else 0.0
        except Exception as e:
            log.warning("usage_tracker.session_spend_query_failed", error=str(e))
            return 0.0
        finally:
            conn.close()

    def get_effective_session_cost(self, session_key: str) -> float:
        """Best known lifetime cost for a session.

        The in-process total covers only what this process has seen. After a
        restart a resumed session starts from zero in memory while the ledger
        still holds what it spent before, so a session ceiling must compare
        against the larger of the two — otherwise a crash-and-respawn hands
        the session a fresh allowance.
        """
        persisted = self._session_spend.get(session_key)
        if persisted is None:
            persisted = self.get_session_db_cost(session_key)
        mem_usage = self._sessions.get(session_key)
        in_memory = mem_usage.total_cost if mem_usage is not None else 0.0
        return max(in_memory, persisted)

    def get_session_scope(self, session_key: str) -> tuple[str, str]:
        meta = self._session_metadata.get(session_key)
        if meta is None:
            meta = parse_session_key_scope(session_key)
            self._session_metadata[session_key] = meta
        return meta

    def check_budget_limits(self, session_key: str, config: Any) -> tuple[bool, str | None]:
        """Evaluate every configured spend ceiling for ``session_key``.

        Returns ``(hard_stop, message)``. ``hard_stop`` True means the caller
        must refuse the work; ``message`` carries operator-facing text for
        either that refusal or — with ``hard_stop`` False — a one-shot warning.

        Hard stops are evaluated across all scopes before warnings, so a
        breached ceiling is never masked by a warning from an earlier scope.
        Warnings fire at most once per scope per day/session so a long-running
        session does not repeat the same alert on every turn.
        """
        if config is None or not getattr(config, "enabled", True):
            return False, None

        agent_id, channel = self.get_session_scope(session_key)
        day = datetime.now(UTC).strftime("%Y-%m-%d")

        # (log_event, label, warn_key, spend, limit, warn). Spend is only read
        # for scopes that actually have a ceiling configured, so the default
        # all-None config costs four dict lookups and no SQLite queries.
        checks: list[tuple[str, str, str, float, float | None, float | None]] = []

        session_limit = getattr(config, "session_limit", None)
        session_warn = getattr(config, "session_warn", None)
        if session_limit is not None or session_warn is not None:
            checks.append(
                (
                    "session",
                    "Session cost",
                    f"session:{session_key}",
                    self.get_effective_session_cost(session_key),
                    session_limit,
                    session_warn,
                )
            )

        daily_limit = getattr(config, "daily_limit", None)
        daily_warn = getattr(config, "daily_warn", None)
        if daily_limit is not None or daily_warn is not None:
            checks.append(
                (
                    "daily",
                    "Daily gateway cost",
                    f"daily:{day}",
                    self.get_spend(day, "gateway", "global"),
                    daily_limit,
                    daily_warn,
                )
            )

        agent_limit = _scope_ceiling(config, "agent_daily_limit", agent_id)
        agent_warn = _scope_ceiling(config, "agent_daily_warn", agent_id)
        if agent_limit is not None or agent_warn is not None:
            checks.append(
                (
                    "agent_daily",
                    f"Daily cost for agent '{agent_id}'",
                    f"agent:{day}:{agent_id}",
                    self.get_spend(day, "agent", agent_id),
                    agent_limit,
                    agent_warn,
                )
            )

        channel_limit = _scope_ceiling(config, "channel_daily_limit", channel)
        channel_warn = _scope_ceiling(config, "channel_daily_warn", channel)
        if channel_limit is not None or channel_warn is not None:
            checks.append(
                (
                    "channel_daily",
                    f"Daily cost for channel '{channel}'",
                    f"channel:{day}:{channel}",
                    self.get_spend(day, "channel", channel),
                    channel_limit,
                    channel_warn,
                )
            )

        for scope, label, _warn_key, spend, limit, _warn in checks:
            if limit is not None and spend >= limit:
                log.warning(
                    "budget.limit_exceeded",
                    scope=scope,
                    session_key=session_key,
                    spend=spend,
                    limit=limit,
                )
                return (
                    True,
                    f"{label} ${spend:,.4f} has reached the ${limit:,.4f} budget limit.",
                )

        for scope, label, warn_key, spend, _limit, warn in checks:
            if warn is None or spend < warn:
                continue
            if warn_key in self._warned_keys:
                continue
            self._warned_keys.add(warn_key)
            log.warning(
                "budget.warning",
                scope=scope,
                session_key=session_key,
                spend=spend,
                threshold=warn,
            )
            return (
                False,
                f"{label} ${spend:,.4f} has reached the ${warn:,.4f} budget warning threshold.",
            )

        return False, None

    def add(
        self,
        session_key: str,
        input_tokens: int,
        output_tokens: int,
        model_id: str = "",
        *,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        billed_cost: float = 0.0,
        provider_id: str = "",
    ) -> None:
        """Record token usage for a session.

        ``billed_cost`` flows through to :py:attr:`ModelUsage.billed_cost` so
        the per-model breakdown can report real provider-billed figures
        instead of the cache-blind pricing-table estimate.
        """
        effective_provider_id = str(provider_id or self._default_provider_id).strip().lower()
        usage = self._sessions.get(session_key)
        if usage is None:
            usage = SessionUsage(model_id=model_id, provider_id=effective_provider_id)
            self._sessions[session_key] = usage
        usage.add(
            input_tokens,
            output_tokens,
            model_id=model_id,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            billed_cost=billed_cost,
            provider_id=effective_provider_id,
        )
        if model_id:
            usage.model_id = model_id
        scope_key = _current_usage_scope.get()
        if scope_key:
            scoped = self._scopes.get((session_key, scope_key))
            if scoped is None:
                scoped = SessionUsage(model_id=model_id, provider_id=effective_provider_id)
                self._scopes[(session_key, scope_key)] = scoped
            scoped.add(
                input_tokens,
                output_tokens,
                model_id=model_id,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                billed_cost=billed_cost,
                provider_id=effective_provider_id,
            )
            if model_id:
                scoped.model_id = model_id

        # Calculate incremental cost
        price = lookup_price(model_id, provider_id=effective_provider_id)
        cost = calculate_cost_usd(
            price,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cache_read_tokens,
        )
        effective_cost = billed_cost if billed_cost > 0.0 else cost

        # Spend ledger — mirrored in memory with or without a DB so budget
        # ceilings apply to an in-memory tracker too.
        agent_id, channel = self.get_session_scope(session_key)
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        ledger_scopes = (("gateway", "global"), ("agent", agent_id), ("channel", channel))
        if effective_cost > 0.0:
            for scope_kind, scope_id in ledger_scopes:
                self._bump_daily_mirror(day, scope_kind, scope_id, effective_cost)
            if session_key not in self._session_spend:
                # Seed from the ledger so a session resumed after a restart
                # keeps counting from what it already spent.
                self._session_spend[session_key] = self.get_session_db_cost(session_key)
            self._session_spend[session_key] += effective_cost
            self._persist_ledger(day, ledger_scopes, session_key, effective_cost)

        if not self._db_path:
            return

        tool_name = _current_tool_name.get()
        scope_key = _current_usage_scope.get()
        if not tool_name and scope_key:
            tool_name = scope_key

        skill = _current_skill_name.get()
        if not skill:
            skill = self._session_active_skill.get(session_key)

        created_at = int(time.time() * 1000)

        conn = self._connect(self._db_path)
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO usage_records (
                        session_key, agent_id, channel_type, tool_name, skill,
                        model, provider, input_tokens, output_tokens,
                        cache_read_tokens, cache_write_tokens, cost_usd, billed_cost_usd,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_key,
                        agent_id,
                        channel,
                        tool_name,
                        skill,
                        model_id,
                        effective_provider_id,
                        input_tokens,
                        output_tokens,
                        cache_read_tokens,
                        cache_write_tokens,
                        cost,
                        billed_cost,
                        created_at,
                    ),
                )
        except Exception as e:
            log.warning("usage_tracker.insert_usage_record_failed", error=str(e))
        finally:
            conn.close()

    def _persist_ledger(
        self,
        day: str,
        ledger_scopes: tuple[tuple[str, str], ...],
        session_key: str,
        incremental_cost: float,
    ) -> None:
        """Write one turn's spend into the durable ledger.

        All four upserts share one connection and one transaction: this runs
        on the turn hot path and sqlite3 is synchronous, so every extra
        connect/commit is event-loop time. A failure here is logged and
        swallowed — the in-process mirror still holds the spend, and
        ``get_spend`` takes the larger of the two, so a dropped write costs
        durability across a restart but never retires a live ceiling.
        """
        if not self._ledger_db_path:
            return
        conn = self._connect(self._ledger_db_path)
        try:
            with conn:
                conn.executemany(
                    """
                    INSERT INTO spend_ledger (day, scope_kind, scope_id, cost_usd)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(day, scope_kind, scope_id) DO UPDATE SET
                        cost_usd = cost_usd + excluded.cost_usd
                    """,
                    [
                        (day, scope_kind, scope_id, incremental_cost)
                        for scope_kind, scope_id in ledger_scopes
                    ],
                )
                conn.execute(
                    """
                    INSERT INTO session_spend (session_key, cost_usd)
                    VALUES (?, ?)
                    ON CONFLICT(session_key) DO UPDATE SET
                        cost_usd = cost_usd + excluded.cost_usd
                    """,
                    (session_key, incremental_cost),
                )
        except Exception as e:
            log.warning("usage_tracker.ledger_write_failed", error=str(e))
        finally:
            conn.close()

    def get(self, session_key: str) -> SessionUsage | None:
        """Return accumulated usage for a session, or None."""
        return self._sessions.get(session_key)

    def session_checkpoint(self, session_key: str) -> SessionUsage | None:
        """Return an immutable-enough copy for later per-turn delta accounting."""
        usage = self._sessions.get(session_key)
        if usage is None:
            return None
        return _clone_session_usage(usage)

    def get_scope(self, session_key: str, scope_key: str) -> SessionUsage | None:
        """Return accumulated usage for a session within one attribution scope."""
        return self._scopes.get((session_key, scope_key))

    def session_snapshot(self, session_key: str) -> SessionTotalsSnapshot | None:
        """Return the current SessionTotalsSnapshot for *session_key*, or None if unknown."""
        usage = self._sessions.get(session_key)
        if usage is None:
            return None
        return SessionTotalsSnapshot.from_session(usage)

    def session_delta_snapshot(
        self,
        session_key: str,
        checkpoint: SessionUsage | None,
    ) -> SessionTotalsSnapshot | None:
        """Return usage added since *checkpoint*.

        Cost is computed from per-model deltas instead of subtracting two
        session totals, because a later provider-billed call can change a
        model's aggregate cost source from estimate to billed.
        """
        usage = self._sessions.get(session_key)
        if usage is None:
            return None
        input_tokens = usage.input_tokens - (checkpoint.input_tokens if checkpoint else 0)
        output_tokens = usage.output_tokens - (checkpoint.output_tokens if checkpoint else 0)
        cache_read_tokens = usage.cache_read_tokens - (
            checkpoint.cache_read_tokens if checkpoint else 0
        )
        cache_write_tokens = usage.cache_write_tokens - (
            checkpoint.cache_write_tokens if checkpoint else 0
        )
        billed_cost = usage.billed_cost - (checkpoint.billed_cost if checkpoint else 0.0)
        cost_usd = 0.0

        if usage._per_model:
            before_models = checkpoint._per_model if checkpoint and checkpoint._per_model else {}
            for usage_key, mu in usage._per_model.items():
                before = before_models.get(usage_key) if before_models else None
                delta_input = mu.input_tokens - (before.input_tokens if before else 0)
                delta_output = mu.output_tokens - (before.output_tokens if before else 0)
                delta_cache_read = mu.cache_read_tokens - (
                    before.cache_read_tokens if before else 0
                )
                delta_billed = mu.billed_cost - (before.billed_cost if before else 0.0)
                if delta_input or delta_output or delta_cache_read or delta_billed:
                    cost_usd += _model_delta_cost(
                        model_id=mu.model_id,
                        input_tokens=max(0, delta_input),
                        output_tokens=max(0, delta_output),
                        cache_read_tokens=max(0, delta_cache_read),
                        billed_cost=max(0.0, delta_billed),
                        provider_id=mu.provider_id or usage.provider_id,
                    )
        else:
            cost_usd = _model_delta_cost(
                model_id=usage.model_id,
                input_tokens=max(0, input_tokens),
                output_tokens=max(0, output_tokens),
                cache_read_tokens=max(0, cache_read_tokens),
                billed_cost=max(0.0, billed_cost),
                provider_id=usage.provider_id,
            )

        return SessionTotalsSnapshot(
            input_tokens=max(0, input_tokens),
            output_tokens=max(0, output_tokens),
            cache_read_tokens=max(0, cache_read_tokens),
            cache_write_tokens=max(0, cache_write_tokens),
            cost_usd=max(0.0, cost_usd),
            billed_cost=max(0.0, billed_cost),
        )

    def get_cost(self, session_key: str) -> float:
        """Return accumulated cost in USD for a session."""
        usage = self._sessions.get(session_key)
        if usage is None:
            return 0.0
        return usage.cost

    def format_usage(self, session_key: str) -> str:
        """Human-readable usage summary for a session."""
        usage = self._sessions.get(session_key)
        if usage is None:
            return "Tokens: 0 in / 0 out | Cost: $0.00"
        return (
            f"Tokens: {usage.input_tokens:,} in / {usage.output_tokens:,} out "
            f"| Cost: ${usage.cost:,.4f}"
        )

    def total_cost(self) -> float:
        """Sum of costs across all sessions."""
        return sum(u.cost for u in self._sessions.values())

    def all_sessions(self) -> dict[str, SessionUsage]:
        """Return all tracked sessions."""
        return dict(self._sessions)

    def query_usage(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        agent_id: str | None = None,
        channel_type: str | None = None,
        tool_name: str | None = None,
        skill: str | None = None,
        session_key: str | None = None,
    ) -> list[dict[str, Any]]:
        if not self._db_path:
            return self._query_in_memory(
                start_date=start_date,
                end_date=end_date,
                agent_id=agent_id,
                channel_type=channel_type,
                tool_name=tool_name,
                skill=skill,
                session_key=session_key,
            )

        clauses = []
        params: list[Any] = []

        if start_date:
            try:
                dt = datetime.strptime(start_date, "%Y-%m-%d")
                ts = int(dt.replace(tzinfo=UTC).timestamp() * 1000)
                clauses.append("created_at >= ?")
                params.append(ts)
            except ValueError:
                pass

        if end_date:
            try:
                dt = datetime.strptime(end_date, "%Y-%m-%d")
                ts = int((dt.replace(tzinfo=UTC).timestamp() + 86400) * 1000) - 1
                clauses.append("created_at <= ?")
                params.append(ts)
            except ValueError:
                pass

        if agent_id:
            clauses.append("agent_id = ?")
            params.append(agent_id)

        if channel_type:
            clauses.append("channel_type = ?")
            params.append(channel_type)

        if tool_name:
            clauses.append("tool_name = ?")
            params.append(tool_name)

        if skill:
            clauses.append("skill = ?")
            params.append(skill)

        if session_key:
            clauses.append("session_key = ?")
            params.append(session_key)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT session_key, agent_id, channel_type, tool_name, skill,
                   model, provider, input_tokens, output_tokens,
                   cache_read_tokens, cache_write_tokens, cost_usd, billed_cost_usd,
                   created_at
            FROM usage_records
            {where}
            ORDER BY created_at DESC
        """

        conn = sqlite3.connect(self._db_path)
        try:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            rows = cursor.fetchall()
            return [
                {
                    "sessionKey": r[0],
                    "agentId": r[1],
                    "channelType": r[2],
                    "toolName": r[3],
                    "skill": r[4],
                    "model": r[5],
                    "provider": r[6],
                    "inputTokens": r[7],
                    "outputTokens": r[8],
                    "cacheReadTokens": r[9],
                    "cacheWriteTokens": r[10],
                    "costUsd": r[11],
                    "billedCostUsd": r[12],
                    "createdAt": r[13],
                }
                for r in rows
            ]
        except Exception as e:
            log.warning("usage_tracker.query_failed", error=str(e))
            return []
        finally:
            conn.close()

    def _query_in_memory(self, **kwargs) -> list[dict[str, Any]]:
        rows = []
        for session_key, usage in self._sessions.items():
            agent_id, channel = self.get_session_scope(session_key)
            if kwargs.get("agent_id") and kwargs["agent_id"] != agent_id:
                continue
            if kwargs.get("channel_type") and kwargs["channel_type"] != channel:
                continue
            if kwargs.get("session_key") and kwargs["session_key"] != session_key:
                continue
            skill = self._session_active_skill.get(session_key)
            if kwargs.get("skill") and kwargs["skill"] != skill:
                continue

            for model_key, mu in (usage._per_model or {}).items():
                provider_id, model_id = model_key
                rows.append(
                    {
                        "sessionKey": session_key,
                        "agentId": agent_id,
                        "channelType": channel,
                        "toolName": None,
                        "skill": skill,
                        "model": model_id,
                        "provider": provider_id,
                        "inputTokens": mu.input_tokens,
                        "outputTokens": mu.output_tokens,
                        "cacheReadTokens": mu.cache_read_tokens,
                        "cacheWriteTokens": mu.cache_write_tokens,
                        "costUsd": mu.cost,
                        "billedCostUsd": mu.billed_cost,
                        "createdAt": int(time.time() * 1000),
                    }
                )
        return rows
