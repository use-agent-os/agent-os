"""Log and trace retention sweeper for ~/.agentos/logs.

Enforces age-based (TTL) and maximum total size caps across all logs, decision
records, trace JSONLs, safety logs, and raw call transcripts.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

import structlog

from agentos.observability.trace import _default_log_dir

log = structlog.get_logger(__name__)

# Default exempt or protected file basenames
DEFAULT_LOG_PATTERNS: tuple[str, ...] = (
    "decisions-*.jsonl",
    "traces-*.jsonl",
    "safety-*.jsonl",
    "turn-calls-*.jsonl",
    "agentos.log*",
    "*.jsonl",
    "*.log",
)


@dataclass(frozen=True)
class LogPruneResult:
    """Summary outcome of one log retention sweep."""

    files_examined: int
    files_pruned: int
    bytes_freed: int
    duration_ms: int
    capped: bool = False
    error: str | None = None


def prune_expired_log_files(
    *,
    log_dir: Path | str | None = None,
    retention_days: int = 14,
    max_total_bytes: int = 500 * 1024 * 1024,
    cap_per_sweep: int = 500,
    debounce_seconds: float = 60.0,
    patterns: tuple[str, ...] = DEFAULT_LOG_PATTERNS,
) -> LogPruneResult:
    """Remove log files older than ``retention_days`` or exceeding ``max_total_bytes``.

    Args:
        log_dir: Directory containing log files (defaults to AGENTOS_LOG_DIR or ~/.agentos/logs).
        retention_days: TTL in days. Files older than (now - retention_days * 86400) are removed.
            <=0 disables TTL deletion.
        max_total_bytes: Maximum total byte budget for log files in directory.
            Oldest files are removed first if total size exceeds this. <=0 disables budget pruning.
        cap_per_sweep: Maximum number of files to delete in a single pass.
        debounce_seconds: Grace period preventing deletion of newly written files.
        patterns: Glob patterns of log files to include in retention tracking.

    Returns:
        LogPruneResult containing metrics for pruned files and bytes freed.
    """
    started = time.monotonic()
    target_dir = Path(log_dir) if log_dir is not None else _default_log_dir()

    if not target_dir.is_dir():
        return LogPruneResult(
            files_examined=0,
            files_pruned=0,
            bytes_freed=0,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    now = time.time()
    cutoff_mtime = now - (retention_days * 86400) if retention_days > 0 else 0.0
    debounce_cutoff = now - max(0.0, debounce_seconds)

    # Collect matching candidate files with their stat info
    candidates: dict[Path, tuple[float, int]] = {}
    for pat in patterns:
        for p in target_dir.glob(pat):
            if p in candidates:
                continue
            try:
                st = p.stat()
                if not p.is_file():
                    continue
                candidates[p] = (st.st_mtime, st.st_size)
            except (FileNotFoundError, PermissionError, OSError):
                continue

    examined = len(candidates)
    pruned = 0
    bytes_freed = 0
    capped = False

    # 1. TTL Age-based pruning
    if retention_days > 0:
        for p, (mtime, size) in list(candidates.items()):
            if pruned >= cap_per_sweep:
                capped = True
                break
            if mtime < cutoff_mtime and mtime < debounce_cutoff:
                try:
                    p.unlink(missing_ok=True)
                    pruned += 1
                    bytes_freed += size
                    del candidates[p]
                    log.info("log_retention.ttl_pruned", path=str(p), size=size)
                except OSError as exc:
                    log.warning("log_retention.unlink_failed", path=str(p), error=str(exc))

    # 2. Total size budget pruning (oldest first)
    if max_total_bytes > 0 and candidates:
        total_size = sum(size for _, size in candidates.values())
        if total_size > max_total_bytes:
            # Sort remaining files by mtime ascending (oldest first)
            sorted_by_age = sorted(candidates.items(), key=lambda item: item[1][0])
            for p, (mtime, size) in sorted_by_age:
                if total_size <= max_total_bytes or pruned >= cap_per_sweep:
                    if total_size > max_total_bytes and pruned >= cap_per_sweep:
                        capped = True
                    break
                if mtime >= debounce_cutoff:
                    # Do not prune files written within debounce window
                    continue
                try:
                    p.unlink(missing_ok=True)
                    pruned += 1
                    bytes_freed += size
                    total_size -= size
                    del candidates[p]
                    log.info("log_retention.budget_pruned", path=str(p), size=size)
                except OSError as exc:
                    log.warning("log_retention.unlink_failed", path=str(p), error=str(exc))

    duration_ms = int((time.monotonic() - started) * 1000)
    return LogPruneResult(
        files_examined=examined,
        files_pruned=pruned,
        bytes_freed=bytes_freed,
        duration_ms=duration_ms,
        capped=capped,
    )


class LogRetentionSweeper:
    """Periodic background task that sweeps ~/.agentos/logs for expired log files."""

    def __init__(
        self,
        log_dir: Path | str | None = None,
        *,
        retention_days: int = 14,
        max_total_bytes: int = 500 * 1024 * 1024,
        sweep_interval_s: float = 3600.0,
    ) -> None:
        self.log_dir = Path(log_dir) if log_dir is not None else _default_log_dir()
        self.retention_days = retention_days
        self.max_total_bytes = max_total_bytes
        self.sweep_interval_s = sweep_interval_s
        self._last_sweep = 0.0

    async def maybe_sweep(self) -> LogPruneResult | None:
        """Run sweep if ``sweep_interval_s`` has elapsed since last sweep."""
        now = time.monotonic()
        if now - self._last_sweep < self.sweep_interval_s:
            return None
        self._last_sweep = now
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: prune_expired_log_files(
                log_dir=self.log_dir,
                retention_days=self.retention_days,
                max_total_bytes=self.max_total_bytes,
            ),
        )

    async def run_loop(self, stop_event: asyncio.Event | None = None) -> None:
        """Continuous background sweep loop."""
        while stop_event is None or not stop_event.is_set():
            try:
                await self.maybe_sweep()
            except Exception as exc:
                log.warning("log_retention.sweep_error", error=str(exc))
            try:
                await asyncio.sleep(min(self.sweep_interval_s, 60.0))
            except asyncio.CancelledError:
                break
