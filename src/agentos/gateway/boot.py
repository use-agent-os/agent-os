"""Boot sequence orchestration for the gateway."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import secrets
import signal
import socket
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agentos.engine.usage import UsageTracker
    from agentos.memory.manager import MemoryManager
    from agentos.memory.store import LongTermMemoryStore
    from agentos.memory.sync_manager import (
        MemorySyncManager as MemoryFileWatcher,  # SyncManager replaces watcher
    )
    from agentos.provider.model_catalog import ModelCatalog
    from agentos.provider.selector import ModelSelector
    from agentos.scheduler import SchedulerEngine
    from agentos.session.manager import SessionManager
    from agentos.skills.loader import SkillLoader
    from agentos.tools.registry import ToolRegistry

import structlog
import uvicorn
from starlette.applications import Starlette

from agentos.agents.scope import resolve_agent_model, resolve_agent_workspace_dir
from agentos.asyncio_utils import create_background_task
from agentos.engine.usage import UsageTracker as _UsageTracker
from agentos.gateway.app import create_gateway_app
from agentos.gateway.config import (
    GatewayConfig,
    GatewayConfigError,
    enforce_public_bind_auth_guard,
    is_public_bind,
)
from agentos.gateway.llm_runtime import resolve_llm_runtime_config
from agentos.gateway.protocol import MAX_PAYLOAD_BYTES
from agentos.gateway.rpc import get_dispatcher
from agentos.gateway.session_events import build_sessions_changed_payload
from agentos.gateway.session_lifecycle import (
    TaskLifecycleEvent,
    apply_task_lifecycle_to_session,
    session_status_for_task_status,
)
from agentos.gateway.session_services import get_session_storage
from agentos.gateway.session_streams import get_session_streams
from agentos.gateway.websocket import get_registry
from agentos.gateway.ws_transport import GatewayWebSocketProtocol
from agentos.paths import default_agentos_home
from agentos.permissions import configured_cron_default_elevated, configured_default_elevated
from agentos.router_tiers import DEFAULT_ROUTER_STRATEGY
from agentos.session.terminal_reply import build_terminal_reply, sanitize_agent_error

log = structlog.get_logger(__name__)


_DEBUG_FILE_HANDLER_ATTR = "_agentos_debug_file_handler"
_ENABLED_VALUES = {"1", "true", "yes", "on"}
_DISABLED_VALUES = {"0", "false", "no", "off"}
_LOG_LEVELS = {
    "CRITICAL": logging.CRITICAL,
    "FATAL": logging.FATAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
    "TRACE": logging.DEBUG,
    "NOTSET": logging.NOTSET,
}


def _resolve_migrations_dir() -> Path:
    """Locate yoyo migrations in env override, installed package, or checkout."""

    env_dir = os.environ.get("AGENTOS_MIGRATIONS_DIR")
    if env_dir:
        candidate = Path(env_dir)
        if any(candidate.glob("V*.py")):
            return candidate

    try:
        from importlib import resources as importlib_resources

        package_dir = importlib_resources.files("agentos").joinpath("_migrations")
        if package_dir.is_dir():
            path = Path(str(package_dir))
            if any(path.glob("V*.py")):
                return path
    except Exception:
        pass

    repo_dir = Path(__file__).resolve().parents[3] / "migrations"
    if any(repo_dir.glob("V*.py")):
        return repo_dir

    raise RuntimeError(
        "agentos migrations directory not found "
        "(checked AGENTOS_MIGRATIONS_DIR, agentos/_migrations, "
        "and repo migrations/)"
    )


class TaskRuntimeStreamError(RuntimeError):
    """Terminal error raised after a turn stream emits an error event."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        terminal_reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.terminal_reason = terminal_reason


# fmt: off
def _make_channel_rpc_context_factory(svc: ServiceContainer, config: GatewayConfig, *, subscription_manager: Any, channel_manager_ref: Any, turn_runner: Any, heartbeat_service: Any, diagnostics_state: Any | None = None) -> Any:  # noqa: E501
    from agentos.channels.command_registry import build_channel_rpc_context

    def _factory(envelope: Any) -> Any:
        names = ("session_manager", "provider_selector", "tool_registry", "usage_tracker", "skill_loader", "cron_scheduler", "task_runtime", "heartbeat_loop", "agent_registry", "memory_managers", "memory_stores", "memory_retrievers")  # noqa: E501
        return build_channel_rpc_context(
            envelope,
            gateway_config=config,
            **{name: getattr(svc, name) for name in names},
            subscription_manager=subscription_manager,
            channel_manager=channel_manager_ref(),
            turn_runner=turn_runner,
            heartbeat_service=heartbeat_service,
            diagnostics_state=diagnostics_state,
        )

    return _factory
# fmt: on


def _interval_h_to_schedule(interval_h: int) -> tuple[Any, str]:
    """Map an hour interval to a structured (kind, value) schedule pair.

    Aligns to a clean cron expression when 24 divides evenly; otherwise falls
    back to a raw interval-in-seconds for the EVERY kind.
    """
    from agentos.scheduler.types import ScheduleKind

    if interval_h > 0 and 24 % interval_h == 0:
        return ScheduleKind.CRON, f"0 */{interval_h} * * *"
    return ScheduleKind.EVERY, str(interval_h * 3600)


async def _list_scheduler_jobs(scheduler: Any) -> list[Any]:
    list_jobs = getattr(scheduler, "list_jobs", None)
    if not callable(list_jobs):
        return []
    try:
        result = list_jobs()
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:  # noqa: BLE001
        log.warning("boot.dream.list_jobs_failed", error=str(exc))
        return []
    return result if isinstance(result, list) else []


async def _pause_orphaned_dream_crons(
    *,
    scheduler: Any,
    agent_ids: list[str] | tuple[str, ...],
) -> None:
    """Pause `memory_dream:*` cron rows left behind by an older install.

    Dream consolidation was removed, but its cron jobs live in the scheduler
    database, not in config. Without this they would keep firing against a
    handler that no longer exists -- failing on every tick, forever. Pausing
    is preferred over deleting so an operator can still see what happened.
    """
    jobs = await _list_scheduler_jobs(scheduler)
    stale = [job for job in jobs if str(getattr(job, "name", "")).startswith("memory_dream:")]
    if stale:
        await _pause_dream_crons(
            scheduler=scheduler,
            jobs=stale,
            reason="dream_removed",
        )


async def _pause_dream_crons(*, scheduler: Any, jobs: list[Any], reason: str) -> None:
    """Pause managed Dream cron jobs so persisted rows cannot bypass config."""
    pause_job = getattr(scheduler, "pause_job", None)
    update_job = getattr(scheduler, "update_job", None)
    for job in jobs:
        status = getattr(getattr(job, "status", None), "value", getattr(job, "status", ""))
        if status in {"paused", "disabled", "deleted"}:
            continue
        job_id = getattr(job, "id", None)
        if not job_id:
            continue
        try:
            if callable(pause_job):
                result = pause_job(job_id)
            elif callable(update_job):
                result = update_job(job_id, enabled=False)
            else:
                continue
            if inspect.isawaitable(result):
                await result
            log.info(
                "boot.dream.paused",
                job_id=job_id,
                name=getattr(job, "name", ""),
                reason=reason,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "boot.dream.pause_failed",
                job_id=job_id,
                reason=reason,
                error=str(exc),
            )


@dataclass
class ServiceContainer:
    """Typed container for initialized services. Returned by build_services().

    WARNING: build_services() mutates module-level state:
    - tools.builtin.memory_tools (create_memory_tools)
    - tools.builtin.skill_tools (create_skill_tools)
    - tools.builtin.control (set_gateway_config, set_scheduler)
    - search.providers (configure_search)
    - tools.builtin.x_search (configure_x_search)
    Do not call build_services() twice in the same process without
    understanding these side effects.
    """

    config: GatewayConfig
    provider_selector: ModelSelector | None = None
    tool_registry: ToolRegistry | None = None
    session_manager: SessionManager | None = None
    skill_loader: SkillLoader | None = None
    usage_tracker: UsageTracker | None = None
    cron_scheduler: SchedulerEngine | None = None
    model_catalog: ModelCatalog | None = None
    agent_registry: Any = None
    memory_managers: dict[str, MemoryManager] = field(default_factory=dict)
    # Legacy per-tier dicts. These are derived views over
    # `memory_managers` populated in build_services(); direct ServiceContainer
    # constructors (e.g. tests) may still set them independently. Once all
    # consumers use `memory_managers`, these legacy fields can be removed.
    memory_stores: dict[str, LongTermMemoryStore] = field(default_factory=dict)
    memory_sync_managers: dict[str, MemoryFileWatcher] = field(default_factory=dict)
    memory_watchers: list[MemoryFileWatcher] = field(default_factory=list)
    memory_retrievers: dict[str, Any] = field(default_factory=dict)
    turn_capture_services: dict[str, Any] = field(default_factory=dict)
    # External memory provider managers (Plan B), keyed by agent_id. Empty
    # unless a provider is configured AND available at boot — a derived view
    # over `memory_managers` (each manager's `.provider_manager`).
    memory_provider_managers: dict[str, Any] = field(default_factory=dict)
    task_runtime: Any = None
    turn_recovery: Any = None
    acp_manager: Any = None
    heartbeat_loop: Any = None
    heartbeat_watcher: Any = None
    log_retention_sweeper: Any = None
    log_retention_task: asyncio.Task | None = None
    otlp_trace_sink: Any = None
    _compaction_listener_remove: Callable[[], None] | None = None
    _approval_broadcaster: Any = None
    turn_hooks: Sequence[Any] | None = None
    compaction_hooks: Sequence[Any] | None = None
    tool_hooks: Sequence[Any] | None = None

    # Backward-compat alias — returns the "main" store (or None).
    @property
    def memory_store(self) -> LongTermMemoryStore | None:
        return self.memory_stores.get("main")

    async def close(self) -> None:
        """Teardown async resources. Idempotent — safe to call twice.

        Ordering rule: scheduled producers (heartbeat watcher/loop and the
        cron scheduler) MUST stop before the memory tier closes; otherwise
        an in-flight cron job or heartbeat tick can drive TurnRunner ->
        TurnCaptureService.capture_turn against an already-closed store.
        """
        if self.log_retention_task is not None:
            self.log_retention_task.cancel()
            try:
                await self.log_retention_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self.log_retention_task = None

        if self.otlp_trace_sink is not None:
            try:
                from agentos.observability.trace import unregister_trace_sink

                unregister_trace_sink(self.otlp_trace_sink)
                await self.otlp_trace_sink.close()
            except Exception:
                pass
            self.otlp_trace_sink = None

        remove_compaction_listener = getattr(self, "_compaction_listener_remove", None)
        if callable(remove_compaction_listener):
            try:
                remove_compaction_listener()
            except Exception:
                pass
            self._compaction_listener_remove = None

        approval_broadcaster = getattr(self, "_approval_broadcaster", None)
        if approval_broadcaster is not None:
            try:
                approval_broadcaster.uninstall()
            except Exception:
                log.warning("approval_broadcast.uninstall_failed", exc_info=True)
            self._approval_broadcaster = None

        # ── 1. Stop scheduled producers (no further writes after this) ──
        if self.heartbeat_watcher is not None:
            try:
                await self.heartbeat_watcher.stop()
            except Exception:
                pass
        if self.heartbeat_loop is not None:
            try:
                await self.heartbeat_loop.stop()
            except Exception:
                pass
        if self.cron_scheduler is not None:
            try:
                await self.cron_scheduler.stop()
            except Exception:
                pass
            store = getattr(self.cron_scheduler, "_store", None)
            if store is not None and hasattr(store, "close"):
                try:
                    await store.close()
                except Exception:
                    pass
        if self.task_runtime is not None:
            try:
                await self.task_runtime.shutdown()
            except Exception:
                pass
            try:
                from agentos.tools.builtin.sessions import set_task_runtime

                set_task_runtime(None)
            except Exception:
                pass
        if self.acp_manager is not None:
            try:
                from agentos.gateway.acp_wiring import stop_acp

                await stop_acp(self.acp_manager)
            except Exception:
                pass
            self.acp_manager = None

        # ── 2. Tear down memory tier through MemoryManager ──
        # In real boot, the legacy `memory_watchers` / `memory_stores` below
        # are the SAME object identities as those reachable via memory_managers,
        # so the subsequent loops are no-op double-stops/closes (both sync_manager
        # and store close are idempotent — see memory/store.py:642 and
        # memory/sync_manager.py:104). Direct ServiceContainer constructors that
        # only populate the legacy fields (e.g. tests) still get torn down by the
        # legacy paths.
        #
        # Retrievers run BEFORE managers so any in-flight search cleanup runs
        # before the underlying DB connection is closed. Per-retriever timeout
        # prevents one wedged retriever from stalling the entire shutdown.
        for retriever in self.memory_retrievers.values():
            try:
                await asyncio.wait_for(retriever.close(), timeout=5.0)
            except (TimeoutError, Exception) as e:  # noqa: BLE001 — fail-open shutdown
                log.warning("retriever_close_failed_or_timed_out", error=str(e))
        for mgr in self.memory_managers.values():
            try:
                await mgr.close()
            except Exception:
                pass
        for watcher in self.memory_watchers:
            try:
                await watcher.stop()
            except Exception:
                pass
        for store in self.memory_stores.values():
            try:
                await store.close()
            except Exception:
                pass
        if self.session_manager is not None:
            storage = get_session_storage(self.session_manager)
            if storage and hasattr(storage, "close"):
                try:
                    await storage.close()
                except Exception:
                    pass

        # ── 3. Close browser sessions ──
        # Reached by the one-shot `agentos agent` path, which is the only caller
        # of this method. The long-running gateway never calls it, so the server
        # closes browsers from its ASGI shutdown hook instead (gateway/app.py).
        try:
            from agentos.tools.agent_browser import close_all_sessions

            close_all_sessions()
        except Exception:
            pass


# Server boot timestamp (set once at first start)
_boot_time_ms: int = 0


def _configured_agent_ids(
    config: GatewayConfig,
    extra: list[str] | None = None,
) -> list[str]:
    """Return agent ids declared by config plus the default main agent.

    ``extra`` lets a caller (e.g. the one-shot CLI runner) opt in additional
    runtime agent ids that are not declared in ``config.channels`` so the
    memory manager / workspace seeding still build per-agent resources for
    them. Legacy ``default`` aliases to the canonical ``main`` agent.
    """
    from agentos.session.keys import normalize_agent_id

    declared = {
        normalize_agent_id(getattr(e, "agent_id", "main")) for e in config.channels.channels
    }
    declared.add("main")
    for entry in getattr(config, "agents", []):
        if getattr(entry, "enabled", True):
            declared.add(normalize_agent_id(getattr(entry, "id", "")))
    if extra:
        declared.update(normalize_agent_id(a) for a in extra if a)
    return sorted(declared)


def _path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _resolved_path(raw: str | None) -> Path | None:
    if not raw:
        return None
    try:
        return Path(raw).expanduser().resolve(strict=False)
    except (OSError, ValueError):
        return None


def _warn_workspace_state_mismatch(config: GatewayConfig) -> None:
    workspace = _resolved_path(getattr(config, "workspace_dir", None))
    if workspace is None:
        return

    expected_roots: dict[str, Path] = {}
    env_state = _resolved_path(os.environ.get("AGENTOS_STATE_DIR"))
    if env_state is not None:
        expected_roots["AGENTOS_STATE_DIR"] = env_state
    env_config = _resolved_path(os.environ.get("AGENTOS_GATEWAY_CONFIG_PATH"))
    if env_config is not None:
        expected_roots["AGENTOS_GATEWAY_CONFIG_PATH"] = env_config.parent
    config_state = _resolved_path(getattr(config, "state_dir", None))
    if config_state is not None:
        expected_roots["config.state_dir"] = config_state.parent
    config_path = _resolved_path(getattr(config, "config_path", None))
    if config_path is not None:
        expected_roots["config.config_path"] = config_path.parent

    mismatches = {
        source: str(root)
        for source, root in expected_roots.items()
        if not _path_is_relative_to(workspace, root)
    }
    if not mismatches:
        return
    log.warning(
        "build_services.workspace_state_mismatch",
        workspace=str(workspace),
        state_dir=getattr(config, "state_dir", None),
        config_path=getattr(config, "config_path", None),
        expected_roots=mismatches,
    )


def _warn_temporary_workspace(config: GatewayConfig) -> None:
    """Log loudly when the workspace lives under the OS temp directory.

    A temp workspace is never what an operator meant: everything the agent
    writes will vanish, and every ordinary path (memory files, the user's
    own files) counts as "outside the workspace", so routine writes start
    asking for approval. That shipped once as an unexplained prompt naming
    ``/var/folders/.../T/tmpXXXX/ws`` on a user's machine; this line is the
    evidence that report was missing.
    """
    workspace = _resolved_path(getattr(config, "workspace_dir", None))
    if workspace is None:
        return
    temp_root = _resolved_path(tempfile.gettempdir())
    if temp_root is None or not _path_is_relative_to(workspace, temp_root):
        return
    log.warning(
        "build_services.workspace_in_temp_dir",
        workspace=str(workspace),
        temp_dir=str(temp_root),
        config_path=getattr(config, "config_path", None),
    )


def _ensure_configured_agent_workspaces(
    config: GatewayConfig,
    *,
    extra_agent_ids: list[str] | None = None,
) -> None:
    """Seed bootstrap templates for explicitly configured agent workspaces."""
    if not config.workspace_dir:
        return

    from agentos.identity.bootstrap import ensure_agent_workspace

    for agent_id in _configured_agent_ids(config, extra_agent_ids):
        result = ensure_agent_workspace(resolve_agent_workspace_dir(agent_id, config))
        log.info(
            "build_services.agent_workspace_ready",
            agent_id=agent_id,
            workspace=str(result.workspace_dir),
            created_files=list(result.created_files),
            bootstrap_seeded=result.bootstrap_seeded,
            bootstrap_completed=result.bootstrap_completed,
        )


async def mirror_cron_result_to_session(
    session_manager: Any,
    origin_session_key: str,
    text: str,
    provenance: dict,
) -> Any | None:
    """Append a cron result to the session the job was created from.

    Returns the transcript entry, or ``None`` when there is nothing to mirror
    into — no session manager, or the origin session no longer exists.

    The existence check is the point: ``append_message`` raises ``KeyError``
    for a missing session, and a cron job routinely outlives the chat it was
    created from (the web UI stamps ``originSessionKey`` onto every reminder).
    Without it, replacing that chat made the job fail on every subsequent run.
    """
    if session_manager is None:
        return None
    if await session_manager.get_session(origin_session_key) is None:
        return None
    return await session_manager.append_message(
        origin_session_key,
        role="assistant",
        content=text,
        provenance=provenance,
    )


def _state_path(config: GatewayConfig, filename: str) -> Path:
    state_root = Path(config.state_dir or default_agentos_home() / "state")
    return state_root / filename


def _gateway_home(config: GatewayConfig) -> Path:
    state_root = _resolved_path(getattr(config, "state_dir", None))
    if state_root is not None:
        return state_root.parent

    config_path = _resolved_path(getattr(config, "config_path", None))
    if config_path is not None:
        return config_path.parent

    return default_agentos_home()


def _task_runtime_max_concurrency(config: GatewayConfig) -> int:
    return int(config.task_runtime.max_concurrency)


def _task_runtime_max_pending_per_session(config: GatewayConfig) -> int:
    return int(config.task_runtime.max_pending_per_session)


def _task_runtime_turn_hard_deadline_s(config: GatewayConfig) -> float | None:
    configured = getattr(config.task_runtime, "turn_hard_deadline_s", None)
    if configured is None:
        return None
    return float(configured)


_USER_MESSAGE_PROVENANCE_KINDS = frozenset({"web_message", "channel_message", "cli_message"})


def _end_once_intent_grants_for_user_turn(run: Any) -> None:
    """Expire the session's ``once`` destructive-intent approvals for a new user turn.

    ``IntentApprovalCache`` documents ``once`` as lasting until the session's next
    user message. ``sessions.send`` clears them only on its no-runtime fallback,
    which the gateway never takes; every turn goes through ``TaskRuntime``, so this
    is the one place a user message from the web UI, a channel or the CLI reaches.
    """
    provenance = getattr(run, "input_provenance", None)
    if not isinstance(provenance, dict) or provenance.get("kind") not in (
        _USER_MESSAGE_PROVENANCE_KINDS
    ):
        return
    try:
        from agentos.sandbox.intent_cache import get_intent_cache

        get_intent_cache().clear_scope("once", session_key=run.session_key)
    except Exception:  # pragma: no cover - never block a turn on the cache
        log.debug("intent_cache.clear_once_failed", exc_info=True)


async def dispatch_task_runtime_turn(
    run: Any,
    *,
    config: Any,
    session_manager: Any,
    turn_runner: Any,
    event_emitter: Any,
) -> None:
    """Drive ``turn_runner.run`` for one ``TaskRun``.

    Pure coroutine extracted from ``build_services``'s
    ``_task_runtime_turn_handler`` closure. Module-level so a
    boot-wiring regression test can drive it with a fake ``turn_runner``
    and capture every kwarg actually flowing into ``turn_runner.run``
    (including the ``semantic_message`` regression surface).
    """
    from agentos.gateway.routing import tool_context_from_envelope

    workspace_dir = resolve_agent_workspace_dir(run.agent_id, config)
    workspace_strict = getattr(config, "workspace_strict", None)
    if not isinstance(workspace_strict, bool):
        workspace_strict = bool(workspace_dir)
    tool_context = tool_context_from_envelope(
        run.envelope,
        workspace_dir=str(workspace_dir),
        workspace_strict=workspace_strict,
        default_elevated=configured_default_elevated(config),
    )
    if (
        tool_context.channel_admission is not None
        and tool_context.channel_admission_validator is not None
        and not tool_context.channel_admission_validator(tool_context.channel_admission)
    ):
        raise PermissionError("channel pairing was revoked before the turn started")
    tool_context.task_id = run.task_id
    from agentos.gateway.turn_recovery import apply_restart_recovery_tool_policy

    apply_restart_recovery_tool_policy(tool_context, run.envelope)
    _end_once_intent_grants_for_user_turn(run)
    session = None
    if session_manager is not None and hasattr(session_manager, "get_session"):
        session = await session_manager.get_session(run.session_key)
    if _is_acp_session_row(session):
        # ACP coding-harness session: the turn runs in an external harness,
        # streamed through the same event path. Silence is not failure, so no
        # idle timeout; heartbeats keep the UI showing a live run.
        from agentos.gateway.acp_dispatch import acp_turn_stream

        await _emit_task_runtime_stream_events(
            acp_turn_stream(run, session=session, config=config, session_manager=session_manager),
            run.session_key,
            event_emitter,
            idle_timeout=None,
            heartbeat_interval=_optional_positive_timeout(
                config, "agent_stream_heartbeat_interval_seconds", 15.0
            ),
            stream_event_sink=getattr(run, "stream_event_sink", None),
            show_thinking=bool(getattr(getattr(config, "control_ui", None), "show_thinking", True)),
            run_id=_run_id(run),
            terminal_publications=getattr(run, "terminal_publications", None),
        )
        return
    run_kwargs = build_task_runtime_run_kwargs(
        run,
        tool_context=tool_context,
        model=resolve_agent_model(
            run.agent_id,
            config,
            session_model=getattr(session, "model", None),
        ),
    )
    raw_stream = turn_runner.run(run.message, run.session_key, **run_kwargs)
    stream_idle_timeout = _optional_positive_timeout(
        config, "agent_stream_idle_timeout_seconds", 600.0
    )
    heartbeat_interval = _optional_positive_timeout(
        config, "agent_stream_heartbeat_interval_seconds", 15.0
    )
    try:
        await _emit_task_runtime_stream_events(
            raw_stream,
            run.session_key,
            event_emitter,
            idle_timeout=stream_idle_timeout,
            heartbeat_interval=heartbeat_interval,
            stream_event_sink=getattr(run, "stream_event_sink", None),
            show_thinking=bool(getattr(getattr(config, "control_ui", None), "show_thinking", True)),
            run_id=_run_id(run),
            terminal_publications=getattr(run, "terminal_publications", None),
        )
    except TaskRuntimeStreamError as exc:
        if exc.code in {
            "provider_request_budget_exhausted",
            "provider_request_too_large",
            "current_turn_context_exhausted",
            # A budget-refused turn never reached the model, so the user
            # message persisted ahead of the run would otherwise linger as a
            # reply-less turn and feed back into later context.
            "budget_exceeded",
        }:
            message_id = getattr(run, "persisted_user_message_id", None)
            remove_message = getattr(session_manager, "remove_message", None)
            if isinstance(message_id, str) and message_id and callable(remove_message):
                try:
                    removed = remove_message(run.session_key, message_id)
                    if inspect.isawaitable(removed):
                        removed = await removed
                    if removed:
                        log.info(
                            "task_runtime.user_message_rolled_back",
                            session_key=run.session_key,
                            message_id=message_id,
                            reason=exc.code,
                        )
                except Exception as rb_exc:  # noqa: BLE001 - preserve terminal error
                    log.warning(
                        "task_runtime.user_message_rollback_failed",
                        session_key=run.session_key,
                        message_id=message_id,
                        reason=exc.code,
                        error=str(rb_exc),
                    )
        raise


def _run_id(run: Any) -> str | None:
    task_id = getattr(run, "task_id", None)
    return task_id if isinstance(task_id, str) and task_id else None


def _is_acp_session_row(session: Any) -> bool:
    origin = getattr(session, "origin", None) if session is not None else None
    return isinstance(origin, dict) and origin.get("kind") == "acp"


def build_task_runtime_run_kwargs(
    run: Any,
    *,
    tool_context: Any,
    model: str | None,
) -> dict[str, Any]:
    """Build kwargs for ``turn_runner.run`` from a ``TaskRun``.

    Pure helper extracted from ``_task_runtime_turn_handler`` so the
    boot-level link of semantic message forwarding is directly
    testable: a regression that drops ``semantic_message`` forwarding
    here is caught by ``test_boot_task_runtime_kwargs.py`` without
    requiring a live gateway.
    """
    ingress_steps = list(run.ingress_pipeline_steps) or None
    kwargs: dict[str, Any] = {
        "tool_context": tool_context,
        "agent_id": run.agent_id,
        "model": model,
        "attachments": run.attachments,
        "input_provenance": run.input_provenance,
        "run_kind": run.run_kind,
        "no_memory_capture": run.no_memory_capture,
        "fresh_user_session": bool(getattr(run, "fresh_user_session", False)),
        "ingress_pipeline_steps": ingress_steps,
    }
    if run.semantic_message is not None:
        # Prefetch query shape: channels carry the raw user text
        # separately from the (potentially stamped) persisted message.
        # Only forward when set so web/CLI legacy paths keep
        # ``TurnRunner.run`` falling back to ``message`` as semantic input.
        kwargs["semantic_message"] = run.semantic_message
    persisted_user_message_id = getattr(run, "persisted_user_message_id", None)
    if isinstance(persisted_user_message_id, str) and persisted_user_message_id:
        # The user row sessions.send persisted for this turn; the history load
        # takes it out wherever it sits (follow-ups land before the previous
        # turn's reply) instead of trimming whatever row came last.
        kwargs["persisted_user_message_id"] = persisted_user_message_id
    return kwargs


def build_cron_result_payload(
    origin_session_key: str,
    text: str,
    entry: Any,
) -> dict[str, Any]:
    """Build the WS payload for a ``session.event.cron_result`` broadcast.

    Pure helper extracted from the cron-forwarder closure so the wire
    contract is testable by gate 4 without spinning up a live gateway.
    The web frontend at ``chat.js:727`` and any other ``cron_result``
    subscriber relies on these exact keys.
    """
    return {
        "sessionKey": origin_session_key,
        "message": {
            "role": "assistant",
            "text": text,
            "timestamp": getattr(entry, "created_at", None),
            "provenanceKind": getattr(entry, "provenance_kind", None),
            "provenanceSourceTool": getattr(entry, "provenance_source_tool", None),
            "provenanceSourceSessionKey": getattr(entry, "provenance_source_session_key", None),
        },
    }


def _task_run_status_for_session_change(event: TaskLifecycleEvent) -> str:
    status = getattr(event.task_status, "value", str(event.task_status))
    if event.phase == "running":
        return "running"
    if status == "succeeded":
        return "idle"
    if status == "abandoned":
        return "interrupted"
    if status in {"failed", "timeout", "cancelled"}:
        return status
    return "idle"


def _task_state_for_session_change(event: TaskLifecycleEvent) -> dict[str, Any]:
    status = getattr(event.task_status, "value", str(event.task_status))
    task: dict[str, Any] = {
        "task_id": event.task_id,
        "status": "running" if event.phase == "running" else status,
    }
    if event.terminal_reason:
        task["terminal_reason"] = event.terminal_reason
    if event.phase == "terminal" and status != "succeeded":
        task["terminal_message"] = build_terminal_reply(
            {
                "status": status,
                "terminal_reason": event.terminal_reason,
                "error_class": event.error_class,
                "error_message": event.error_message,
            }
        )
    return task


def _make_task_session_lifecycle_listener(
    *,
    session_manager: Any,
    event_emitter: Any,
) -> Any:
    async def _listener(event: TaskLifecycleEvent) -> None:
        if event.run_kind in ("subagent", "acp"):
            # A child run's session row is not written here: the subagent
            # announce owns it (it finishes the row and leaves a steered ACP
            # turn's session running for its replacement). The run state still
            # has to reach clients, or a child stays "Working" after its run
            # ended -- so publish it, scoped to the child session.
            await _emit_child_run_state(event, event_emitter=event_emitter)
            return
        if event.phase == "terminal" and event.has_followup:
            # More work is queued on the session (an interrupt's replacement
            # turn): marking the row terminal now left it "killed" while the
            # replacement ran, and the replacement's own end could no longer
            # move it, so no sessions.changed ever said it finished. The
            # replacement's lifecycle reports the session instead.
            return
        changed = await apply_task_lifecycle_to_session(
            event,
            session_manager=session_manager,
        )
        if not changed and event.phase == "running":
            return
        reason = "task_running" if event.phase == "running" else "task_terminal"
        session_status = session_status_for_task_status(event.task_status)
        task_state = _task_state_for_session_change(event)
        state_field = "active_task" if event.phase == "running" else "last_task"
        # A run's end is always announced, also when the row was already
        # terminal (a kill finished it first): the run state is news even if
        # the row status is not.
        await event_emitter(
            event.session_key,
            "sessions.changed",
            build_sessions_changed_payload(
                event.session_key,
                reason,
                status=getattr(session_status, "value", session_status) if changed else None,
                run_status=_task_run_status_for_session_change(event),
                **{state_field: task_state},
            ),
        )

    return _listener


#: Stream events that end a text segment: text held back by the display strip
#: is released (or dropped) before them, as the console starts a new segment.
_TEXT_SEGMENT_BOUNDARY_EVENTS = frozenset(
    {"tool_use_start", "tool_result", "artifact", "done", "error"}
)


async def _emit_child_run_state(event: TaskLifecycleEvent, *, event_emitter: Any) -> None:
    """``sessions.changed`` for a child session's run transition, row untouched."""
    if event.phase == "terminal" and event.has_followup:
        # The session has more work queued (a steer's replacement turn): its
        # own ``task.running`` reports the state; a terminal here would read
        # as "stopped" while the replacement runs.
        return
    reason = "task_running" if event.phase == "running" else "task_terminal"
    state_field = "active_task" if event.phase == "running" else "last_task"
    await event_emitter(
        event.session_key,
        "sessions.changed",
        build_sessions_changed_payload(
            event.session_key,
            reason,
            run_status=_task_run_status_for_session_change(event),
            run_kind=event.run_kind,
            **{state_field: _task_state_for_session_change(event)},
        ),
    )


def _optional_positive_timeout(config: Any, attr: str, default: float) -> float | None:
    raw = getattr(config, attr, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = default
    return value if value > 0 else None


async def _emit_task_runtime_stream_events(
    raw_stream: Any,
    session_key: str,
    event_emitter: Any,
    *,
    idle_timeout: float | None = 180.0,
    heartbeat_interval: float | None = None,
    stream_event_sink: Any = None,
    show_thinking: bool = True,
    run_id: str | None = None,
    terminal_publications: Any = None,
) -> None:
    """Emit turn events and fail the task if the stream reports an error.

    ``show_thinking`` mirrors ``control_ui.show_thinking``: when off, model
    reasoning is neither streamed nor carried on the ``done`` event.

    ``run_id`` (the task id) tags the terminal ``done`` / ``error`` frame,
    and with ``terminal_publications`` (the runtime's reservation, see
    :mod:`agentos.gateway.terminal_events`) makes it exactly-once: a run an
    abort already holds, or whose terminal was already published, does not
    get a second one.

    Streamed text is display-clean: directives, generated-artifact markers and
    leaked tool-protocol text are stripped here (``agentos.chat.display_text``),
    so no client has to, and a frame never carries half a marker. Text a
    marker might still complete is held until the next chunk or the end of the
    text segment (a tool call or the turn's end).
    """
    from dataclasses import asdict, is_dataclass

    from agentos.chat.display_text import AssistantTextStreamSanitizer, strip_assistant_text
    from agentos.engine.stream_wrappers import wrap_stream
    from agentos.gateway.terminal_events import terminal_state

    display_text = AssistantTextStreamSanitizer()

    async def _flush_display_text() -> None:
        held = display_text.flush()
        if held:
            await event_emitter(session_key, "session.event.text_delta", {"text": held})

    error_message: str | None = None
    error_code: str | None = None
    terminal_reason: str | None = None
    async for event in wrap_stream(
        raw_stream,
        idle_timeout=idle_timeout,
        heartbeat_interval=heartbeat_interval,
        heartbeat_message="Agent run is still active",
    ):
        if stream_event_sink is not None:
            try:
                result = stream_event_sink(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                log.debug(
                    "task_runtime.stream_event_sink_failed",
                    session_key=session_key,
                    event_kind=getattr(event, "kind", event.__class__.__name__),
                    exc_info=True,
                )
        if is_dataclass(event):
            event_dict = asdict(event)
        else:
            event_dict = {
                key: value
                for key, value in getattr(event, "__dict__", {}).items()
                if not key.startswith("_")
            }
        event_kind = event_dict.pop("kind", getattr(event, "kind", event.__class__.__name__))
        if not show_thinking:
            if event_kind == "thinking":
                continue
            if event_kind == "done":
                event_dict.pop("reasoning_content", None)
        if event_kind == "text_delta":
            raw_text = event_dict.get("text")
            cleaned = display_text.feed(raw_text if isinstance(raw_text, str) else "")
            if not cleaned:
                continue
            event_dict["text"] = cleaned
        elif event_kind in _TEXT_SEGMENT_BOUNDARY_EVENTS:
            await _flush_display_text()
        if event_kind == "done" and isinstance(event_dict.get("text"), str):
            event_dict["text"] = strip_assistant_text(event_dict["text"])
        if event_kind == "error":
            raw_message = event_dict.get("message")
            error_message = (
                raw_message if isinstance(raw_message, str) and raw_message else "Agent error"
            )
            code = event_dict.get("code")
            error_code = str(code) if code else None
            code_text = str(code or "").lower()
            is_timeout = "timeout" in code_text or "stream idle" in error_message.lower()
            is_output_truncated = code_text == "provider_output_truncated"
            is_cancelled = "cancelled" in code_text
            terminal_reason = (
                "timeout"
                if is_timeout
                else "cancelled"
                if is_cancelled
                else "output_truncated"
                if is_output_truncated
                else "error"
            )
            terminal_payload = {
                "status": "timeout" if is_timeout else "cancelled" if is_cancelled else "failed",
                "terminal_reason": terminal_reason,
                "error_class": code,
                "error_message": error_message,
            }
            safe_error_code, safe_error_message = sanitize_agent_error(
                terminal_payload,
                fallback_error_class=error_code,
                fallback_error_message=error_message,
            )
            if safe_error_code == "provider_request_too_large":
                error_code = safe_error_code
                event_dict["code"] = safe_error_code
                terminal_payload["error_class"] = safe_error_code
                terminal_payload["error_message"] = safe_error_message
            terminal_message = build_terminal_reply(terminal_payload)
            event_dict["message"] = terminal_message
            event_dict["terminal_message"] = terminal_message
            event_dict["terminal_reason"] = terminal_payload["terminal_reason"]
            event_dict["error_message"] = safe_error_message
        event_name = f"session.event.{event_kind}"
        emit = True
        if event_kind in ("done", "error"):
            event_dict["state"] = terminal_state(event_name, event_dict)
            if run_id:
                event_dict["run_id"] = run_id
                event_dict["task_id"] = run_id
                if terminal_publications is not None:
                    emit = terminal_publications.try_publish(run_id, event_name, event_dict)
        if emit:
            await event_emitter(session_key, event_name, event_dict)
        if event_kind == "error":
            message = event_dict.get("error_message")
            error_message = message if isinstance(message, str) and message else "Agent error"
    await _flush_display_text()
    if error_message is not None:
        raise TaskRuntimeStreamError(
            error_message,
            code=error_code,
            terminal_reason=terminal_reason,
        )


def _env_bool(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in _ENABLED_VALUES:
        return True
    if value in _DISABLED_VALUES:
        return False
    return None


def _resolve_log_level(config: GatewayConfig) -> int:
    raw = os.environ.get("AGENTOS_LOG_LEVEL") or config.log_level
    return _LOG_LEVELS.get(str(raw).strip().upper(), logging.DEBUG)


def _remove_debug_file_handlers(root: logging.Logger) -> None:
    agentos_logger = logging.getLogger("agentos")
    for handler in list(root.handlers):
        if getattr(handler, _DEBUG_FILE_HANDLER_ATTR, False):
            previous_level = getattr(handler, "_agentos_previous_logger_level", None)
            root.removeHandler(handler)
            handler.close()
            if isinstance(previous_level, int):
                agentos_logger.setLevel(previous_level)


#: structlog's own processor chain, saved before the tee is spliced in so
#: turning file logging off puts it back exactly as it was.
_structlog_processors_before_tee: list[Any] | None = None

_STRUCTLOG_LEVELS = {
    "critical": logging.CRITICAL,
    "exception": logging.ERROR,
    "error": logging.ERROR,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}


def _tee_structlog_to_stdlib(logger: Any, method_name: str, event_dict: dict) -> dict:
    """Copy a structlog event into stdlib logging, then leave it untouched.

    structlog prints to stderr through its own ``PrintLogger``, which never
    reaches the stdlib handlers — so the file handler below saw only half the
    gateway's logs. The half it missed included every ``delivery.*`` warning,
    which is exactly what you go looking for after a cron run reports "delivery
    failed".

    Runs as the *last* processor before the renderer, so ``exc_info`` has
    already been rendered into ``exception`` and the tee copies the traceback
    rather than re-deriving it.

    Events land under ``agentos.events`` rather than the emitting module:
    structlog's ``PrintLogger`` does not carry a name, and this codebase already
    namespaces its event keys (``delivery.channel_failed``), so recovering the
    module would cost a callsite walk to say what the event already says.
    """
    name = str(event_dict.get("logger") or "agentos.events")
    level = _STRUCTLOG_LEVELS.get(method_name, logging.INFO)
    target = logging.getLogger(name)
    if target.isEnabledFor(level):
        event = str(event_dict.get("event", ""))
        pairs = " ".join(
            f"{key}={value}"
            for key, value in event_dict.items()
            if key not in ("event", "logger", "level", "timestamp", "exception")
        )
        exception = event_dict.get("exception")
        message = " ".join(part for part in (event, pairs) if part)
        if exception:
            message = f"{message}\n{exception}"
        target.log(level, message)
    return event_dict


def _install_structlog_tee() -> None:
    """Splice :func:`_tee_structlog_to_stdlib` in ahead of structlog's renderer."""
    global _structlog_processors_before_tee
    import structlog

    processors = list(structlog.get_config()["processors"])
    if _tee_structlog_to_stdlib in processors:
        return
    _structlog_processors_before_tee = processors
    spliced = [*processors[:-1], _tee_structlog_to_stdlib, processors[-1]]
    structlog.configure(processors=spliced)


def _remove_structlog_tee() -> None:
    global _structlog_processors_before_tee
    if _structlog_processors_before_tee is None:
        return
    import structlog

    structlog.configure(processors=_structlog_processors_before_tee)
    _structlog_processors_before_tee = None


def _apply_structlog_level(config: GatewayConfig) -> None:
    """Let structlog through at the gateway's configured level.

    The CLI entry point installs an INFO threshold so a one-shot command does
    not print every debug event it brushes past (#2896). The gateway is the
    process those events are *for* -- ``log_level`` defaults to DEBUG and the
    file tee below copies each event into ``debug.log`` -- so it sets the
    threshold from its own config before the first event is logged.
    """
    import structlog

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(_resolve_log_level(config)),
    )


def _setup_file_logging(config: GatewayConfig | None = None) -> None:
    """Configure structlog + stdlib logging to write to a debug.log file."""
    config = config or GatewayConfig()
    _apply_structlog_level(config)
    root = logging.getLogger()
    _remove_debug_file_handlers(root)

    enabled = _env_bool("AGENTOS_LOG_FILE_ENABLED")
    if enabled is None:
        enabled = config.log_file_enabled
    if not enabled:
        _remove_structlog_tee()
        return

    log_dir = Path(os.environ.get("AGENTOS_LOG_DIR", str(default_agentos_home() / "logs")))
    log_level = _resolve_log_level(config)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "debug.log"
        file_handler = RotatingFileHandler(
            str(log_file),
            maxBytes=config.log_file_max_bytes,
            backupCount=config.log_file_backup_count,
            encoding="utf-8",
        )
    except OSError as exc:
        logging.getLogger(__name__).warning("file logging disabled: %s", exc)
        return
    setattr(file_handler, _DEBUG_FILE_HANDLER_ATTR, True)
    agentos_logger = logging.getLogger("agentos")
    setattr(file_handler, "_agentos_previous_logger_level", agentos_logger.level)
    file_handler.setLevel(log_level)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )

    root.addHandler(file_handler)
    agentos_logger.setLevel(log_level)
    _install_structlog_tee()


#: How long a SIGTERM / SIGINT shutdown lets in-flight turns finish.
SIGNAL_DRAIN_TIMEOUT_S = 30.0
#: After a second signal: how long cancelled turns get to unwind.
FORCE_DRAIN_TIMEOUT_S = 2.0
#: How long shutdown waits for all WebSocket closing handshakes together.
WS_CLOSE_TIMEOUT_S = 2.0
#: uvicorn's own bound on waiting for open HTTP connections at shutdown.
UVICORN_GRACEFUL_SHUTDOWN_S = 5
#: How long close() waits for uvicorn to finish (its graceful wait included)
#: before cancelling it.
UVICORN_STOP_TIMEOUT_S = 10.0


@dataclass
class GatewayServer:
    """Handle returned after gateway startup. Provides close() method."""

    app: Starlette
    config: GatewayConfig
    _server: uvicorn.Server | None = field(default=None, repr=False)
    _task: asyncio.Task | None = field(default=None, repr=False)
    _channel_manager: Any = field(default=None, repr=False)
    _services: ServiceContainer | None = field(default=None, repr=False)
    _background_completion_manager: Any = field(default=None, repr=False)
    # The process-wide pid lock, released explicitly before an in-place
    # restart (``atexit`` does not run across ``execve``).
    _pid_lock: Any = field(default=None, repr=False)
    # ``gateway.restart.GatewayRestarter`` once wired; ``run_gateway`` awaits
    # its task after the server task ends so the exec happens outside the loop.
    _restarter: Any = field(default=None, repr=False)
    # Approval delivery wired at boot (Web Push, parent-chat relay); undone on close.
    _web_push: Any = field(default=None, repr=False)
    # ``gateway.config_watcher.ConfigWatcher`` for the active config file.
    _config_watcher: Any = field(default=None, repr=False)
    _closed: bool = field(default=False, repr=False)
    # Signal-driven shutdown (``install_signal_handlers``): the first SIGTERM
    # / SIGINT starts ``close()`` in ``_shutdown_task``; a second sets
    # ``_forced``, which cuts the drain short.
    _shutdown_task: asyncio.Task | None = field(default=None, repr=False)
    _signal_count: int = field(default=0, repr=False)
    _signal_loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False)
    _installed_signals: tuple[int, ...] = field(default=(), repr=False)
    _forced: bool = field(default=False, repr=False)
    _force_event: asyncio.Event | None = field(default=None, repr=False)
    #: ``gateway.readiness.GatewayReadiness``: what ``/ready``, ``/startupz``
    #: and the WebSocket handshake report (the supervisor contract).
    readiness: Any = field(default=None, repr=False)
    #: ``gateway.boot_lifecycle.BootTracker``: this boot's ledger row + safe mode.
    _boot_tracker: Any = field(default=None, repr=False)
    #: ``gateway.maintenance.GatewayMaintenance``: thaw detection + health sweep.
    _maintenance: Any = field(default=None, repr=False)

    @property
    def shutdown_task(self) -> asyncio.Task | None:
        """The ``close()`` a shutdown signal started, if any (``run_gateway`` awaits it)."""
        return self._shutdown_task

    def install_signal_handlers(self, loop: asyncio.AbstractEventLoop | None = None) -> bool:
        """Route SIGTERM and SIGINT to an orderly :meth:`close`; a second signal forces.

        Every non-interactive stop sends SIGTERM (``agentos gateway stop``, the
        desktop supervisor, launchd, systemd), so it must run the same
        sequence as Ctrl+C: drain turns, stop channels, broadcast
        ``shutdown``, close sockets, stop uvicorn. Returns ``False`` where the
        loop cannot own signals (a worker thread, Windows); the caller then
        leaves uvicorn's own handling in place.
        """
        if threading.current_thread() is not threading.main_thread():
            return False
        target = loop if loop is not None else asyncio.get_running_loop()
        installed: list[int] = []
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                target.add_signal_handler(sig, self._on_shutdown_signal, int(sig))
            except (NotImplementedError, RuntimeError, ValueError):
                continue
            installed.append(int(sig))
        self._signal_loop = target
        self._installed_signals = tuple(installed)
        return bool(installed)

    def remove_signal_handlers(self) -> None:
        loop = self._signal_loop
        if loop is None or loop.is_closed():
            self._installed_signals = ()
            return
        for sig in self._installed_signals:
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
        self._installed_signals = ()

    def _on_shutdown_signal(self, signum: int) -> None:
        self._signal_count += 1
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = str(signum)
        if self._signal_count == 1:
            log.info("gateway.shutdown_signal", signal=name, drain_timeout_s=SIGNAL_DRAIN_TIMEOUT_S)
            self.request_shutdown(f"signal:{name}")
            return
        log.warning("gateway.shutdown_forced", signal=name, drain_timeout_s=FORCE_DRAIN_TIMEOUT_S)
        self.force_shutdown()

    def request_shutdown(self, reason: str = "shutdown") -> asyncio.Task:
        """Start the orderly shutdown once; later calls return the same task.

        Stops admitting turns and reports not-ready first, as an in-place
        restart does. A restart already draining keeps draining but will not
        exec a new image: the process was asked to stop.
        """
        if self._shutdown_task is None:
            restarter = self._restarter
            cancel_exec = getattr(restarter, "cancel_exec", None)
            if callable(cancel_exec):
                cancel_exec(reason)
            try:
                self.app.state.gateway_ready = False
            except Exception:  # noqa: BLE001 - a stub app in tests
                pass
            runtime = getattr(self._services, "task_runtime", None)
            stop = getattr(runtime, "stop_admission", None)
            if callable(stop):
                stop()
            self._shutdown_task = asyncio.get_running_loop().create_task(
                self.close(reason, drain_timeout=SIGNAL_DRAIN_TIMEOUT_S),
                name="gateway-signal-shutdown",
            )
        return self._shutdown_task

    def force_shutdown(self) -> None:
        """Cut a running drain short: cancel in-flight turns now.

        uvicorn is only told to exit, not to force-exit: a forced uvicorn
        skips the ASGI lifespan shutdown (browser sessions stay open, and the
        abandoned lifespan task logs a CancelledError traceback at exit). Its
        own wait for open connections is bounded by
        ``timeout_graceful_shutdown`` instead.
        """
        self._forced = True
        if self._force_event is not None:
            self._force_event.set()
        if self._server is not None:
            self._server.should_exit = True

    async def _drain(self, work: Any) -> bool:
        """Await ``work`` unless a forced shutdown arrives first.

        Returns ``True`` when forced; ``work`` keeps running in that case so a
        cancellation it starts can unwind (the caller bounds it).
        """
        task = asyncio.ensure_future(work)
        if self._forced:
            return not task.done()
        if self._force_event is None:
            self._force_event = asyncio.Event()
        forced = asyncio.ensure_future(self._force_event.wait())
        try:
            await asyncio.wait({task, forced}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            forced.cancel()
        if task.done():
            task.result()
            return False
        return True

    async def close(
        self,
        reason: str = "shutdown",
        *,
        drain_timeout: float = 30.0,
        cancel_in_flight: bool = False,
        restart_expected_ms: int | None = None,
    ) -> None:
        """Gracefully shut down: stop channels, broadcast shutdown, close WS, stop server.

        ``drain_timeout`` bounds how long in-flight turns may run on. With
        ``cancel_in_flight`` they are cancelled at once instead of drained
        (the ``/restart force`` path); :meth:`force_shutdown` (a second
        signal) does the same to a drain already under way. A second call is
        a no-op: the ``CancelledError`` branch in ``run_gateway`` and a
        restart in progress must not close the same services twice.

        ``restart_expected_ms`` rides on the ``shutdown`` event: an estimate
        of when this gateway serves again (the in-place restart), or ``None``
        for a terminal stop (a signal, ``Ctrl+C``). The boot is recorded clean
        at the end, so the crash-loop breaker never counts a deliberate stop.
        """
        if self._closed:
            return
        self._closed = True
        if self.readiness is not None:
            self.readiness.begin_drain()
        if self._boot_tracker is not None:
            self._boot_tracker.note_stopping(reason)
        if self._maintenance is not None:
            try:
                await self._maintenance.stop()
            except Exception:
                log.debug("gateway.maintenance_stop_failed", exc_info=True)
            from agentos.gateway.maintenance import get_maintenance, set_maintenance

            if get_maintenance() is self._maintenance:
                set_maintenance(None)
        if self._config_watcher is not None:
            try:
                await self._config_watcher.stop()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass
            self._config_watcher = None
        # Restart recovery stops first: a resume still waiting must not start
        # now. What it had not finished is picked up again on the next boot.
        recovery = getattr(self._services, "turn_recovery", None)
        if recovery is not None:
            try:
                await recovery.stop()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                log.debug("gateway.turn_recovery_stop_failed", exc_info=True)
        # Drain in-flight turns FIRST so replies are not lost.
        # task_runtime.shutdown() waits for all running turns to complete before
        # returning; only then do we stop channel delivery.
        if self._services is not None and self._services.task_runtime is not None:
            runtime = self._services.task_runtime
            try:
                if cancel_in_flight:
                    await runtime.shutdown(cancel=True, timeout=drain_timeout)
                else:
                    graceful = asyncio.ensure_future(
                        runtime.shutdown(graceful=True, graceful_timeout=drain_timeout)
                    )
                    if await self._drain(graceful):
                        await runtime.shutdown(cancel=True, timeout=FORCE_DRAIN_TIMEOUT_S)
                        try:
                            await asyncio.wait_for(graceful, timeout=FORCE_DRAIN_TIMEOUT_S)
                        except TimeoutError:
                            graceful.cancel()
            except Exception:
                pass

        # Lifecycle hooks run once the turns have drained -- nothing is still
        # writing the state they flush -- and while channels and services are
        # still up (bounded: 10 s pre-restart, 5 s shutdown; never raises).
        from agentos.gateway.lifecycle_hooks import (
            PRE_RESTART,
            run_lifecycle_hooks,
            run_shutdown_hooks_once,
        )

        config = getattr(self, "config", None)
        if reason.startswith("restart"):
            await run_lifecycle_hooks(PRE_RESTART, config=config, reason=reason)
        await run_shutdown_hooks_once(config, reason)

        if self._background_completion_manager is not None:
            try:
                timeout = FORCE_DRAIN_TIMEOUT_S if self._forced else drain_timeout
                closing = asyncio.ensure_future(
                    self._background_completion_manager.close(timeout=timeout)
                )
                if await self._drain(closing):
                    try:
                        await asyncio.wait_for(closing, timeout=FORCE_DRAIN_TIMEOUT_S)
                    except TimeoutError:
                        closing.cancel()
            except Exception:
                log.debug("gateway.background_completion_close_failed", exc_info=True)
            try:
                from agentos.gateway.subagent_announce import set_background_completion_manager

                set_background_completion_manager(None)
            except Exception:
                pass
            self._background_completion_manager = None

        # Stop channels after task_runtime is drained (no in-flight turns remain)
        if self._channel_manager is not None:
            await self._channel_manager.stop_all()
            log.info("gateway.channels_stopped")

        registry = get_registry()
        await registry.broadcast(
            "shutdown", {"reason": reason, "restart_expected_ms": restart_expected_ms}
        )
        # The broadcast only queues the event; let each writer put it on the
        # wire before the socket is closed under it (bounded per connection).
        await asyncio.gather(
            *(conn.flush(timeout=1.0) for conn in registry.all()), return_exceptions=True
        )

        # Close all active WS connections, concurrently and bounded: a client
        # that stopped reading must not hold the shutdown on its closing
        # handshake.
        connections = registry.all()
        if connections:
            _done, stuck = await asyncio.wait(
                [asyncio.ensure_future(conn.close()) for conn in connections],
                timeout=WS_CLOSE_TIMEOUT_S,
            )
            for task in stuck:
                task.cancel()

        # Close MCP clients
        try:
            from agentos.mcp.discovery import close_active_clients

            await close_active_clients()
            log.info("gateway.mcp_clients_closed")
        except ImportError:
            pass

        if self._server is not None:
            self._server.should_exit = True

        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=UVICORN_STOP_TIMEOUT_S)
            except TimeoutError:
                self._task.cancel()

        if self._services is not None:
            await self._services.close()

        _unwire_approval_delivery(self._web_push)
        self._web_push = None
        if self._boot_tracker is not None:
            self._boot_tracker.mark_clean(reason)
            from agentos.gateway.boot_lifecycle import get_boot_tracker, set_boot_tracker

            if get_boot_tracker() is self._boot_tracker:
                set_boot_tracker(None)
        if self.readiness is not None:
            from agentos.gateway.readiness import get_gateway_readiness, set_gateway_readiness

            if get_gateway_readiness() is self.readiness:
                set_gateway_readiness(None)

        self.remove_signal_handlers()
        log.info("gateway.stopped", reason=reason, forced=self._forced)


@dataclass
class ApprovalDeliveryWiring:
    push: Any = None
    relay: Any = None


def wire_approval_delivery(
    config: GatewayConfig, *, channel_manager_ref: Callable[[], Any] | None = None
) -> ApprovalDeliveryWiring:
    """Point the approval queue at session lineage, Web Push and the parent-chat relay.

    The queue asks for a request's audience (its session plus subagent /
    coding-agent ancestors) through a read-only view of ``sessions.db``; the
    channel custody check and the relay read delivery routes from the same
    view. The relay sends a child session's prompt to the chat its parent is
    delivered to.
    """
    from agentos.application import approval_delivery
    from agentos.gateway.approval_queue import get_approval_queue
    from agentos.gateway.session_index import SessionIndex, set_session_index
    from agentos.gateway.web_push import get_web_push_service

    index = SessionIndex(_state_path(config, "sessions.db"))
    set_session_index(index)
    approval_delivery.set_lineage_resolver(index.parents)
    try:
        service = get_web_push_service(config)
    except Exception:  # noqa: BLE001 - push is best-effort, never a boot blocker
        log.warning("gateway.web_push_unavailable", exc_info=True)
        service = None
    if service is not None:
        service._loop = asyncio.get_running_loop()
        service._lifespan_end = lambda entry: get_approval_queue().lifespan_end(entry)
        approval_delivery.register_listener(service)
    relay = None
    if channel_manager_ref is not None:
        from agentos.gateway.approval_channel_relay import ChannelApprovalRelay

        relay = ChannelApprovalRelay(channel_manager_ref, loop=asyncio.get_running_loop())
        approval_delivery.register_listener(relay)
    return ApprovalDeliveryWiring(push=service, relay=relay)


def _unwire_approval_delivery(wiring: Any) -> None:
    from agentos.application import approval_delivery
    from agentos.gateway.session_index import set_session_index
    from agentos.gateway.web_push import set_web_push_service

    service = getattr(wiring, "push", None)
    relay = getattr(wiring, "relay", None)
    if relay is not None:
        approval_delivery.unregister_listener(relay)
    if service is not None:
        approval_delivery.unregister_listener(service)
        try:
            service.close()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass
        set_web_push_service(None)
    approval_delivery.set_lineage_resolver(None)
    set_session_index(None)


def bind_listen_socket(host: str, port: int) -> socket.socket:
    """Bind one TCP listener the way uvicorn does for its own ``host``/``port``."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host.strip("[]"), port))
    except OSError as exc:
        sock.close()
        raise ValueError(f"cannot listen on {host}:{port}: {exc}") from exc
    sock.set_inheritable(True)
    return sock


def emit_skill_filter_banner(skills_cfg: Any) -> None:
    """One-line startup warning when the ONNX embedding backend is
    unreachable but a non-lexical filter strategy is configured.

    Required runtime: ``onnxruntime`` + ``tokenizers`` +
    the bundled BGE ONNX dir (or a configured override). All three
    ship via ``uv sync --extra recommended``. The previous non-ONNX
    fallback was removed — there is now exactly one backend.

    The banner fires only when filter_enabled=true, strategy ≠ lexical,
    AND the ONNX path is incomplete. Uses stdlib :mod:`logging` so
    operators see it on the standard ``WARNING`` logger and so tests
    can assert on it via ``caplog``.
    """
    import importlib.util
    import logging

    log_std = logging.getLogger("agentos.gateway.boot")

    if not getattr(skills_cfg, "filter_enabled", False):
        return
    if getattr(skills_cfg, "filter_strategy", "lexical") == "lexical":
        return

    onnx_ok = False
    try:
        if (
            importlib.util.find_spec("onnxruntime") is not None
            and importlib.util.find_spec("tokenizers") is not None
        ):
            from agentos.memory.embedding import LocalEmbeddingProvider

            model_name = getattr(
                skills_cfg, "filter_embedding_model", LocalEmbeddingProvider.DEFAULT_MODEL
            )
            onnx_ok = LocalEmbeddingProvider._bundled_onnx_dir(model_name) is not None
    except ImportError:
        onnx_ok = False

    if onnx_ok:
        return

    log_std.warning(
        "ONNX embedding backend not available; filter_strategy=%r will run "
        "lexical-only. Install via `uv sync --extra recommended` to get "
        "onnxruntime + tokenizers, and verify the bundled BGE ONNX dir "
        "is present.",
        getattr(skills_cfg, "filter_strategy", "lexical"),
    )


def _log_resolved_judge(config: GatewayConfig, router_cfg: Any) -> None:
    """Resolve and log the LLM judge target (spec D2/D4 observability)."""
    from agentos.agentos_router.llm_judge import (
        judge_provider_has_credentials,
        resolve_judge_target,
    )

    llm_cfg = getattr(config, "llm", None)
    target = resolve_judge_target(router_cfg, llm_cfg)
    if target is None:
        log.warning(
            "router.judge_unresolved",
            strategy=getattr(router_cfg, "strategy", None),
        )
        return
    provider, model, source = target
    # A local-endpoint judge (source="local") carries its own credentials via
    # judge_base_url / judge_api_key; surface the base_url (never the api key).
    base_url = (
        str(getattr(router_cfg, "judge_base_url", "") or "").strip() if source == "local" else None
    )
    if not judge_provider_has_credentials(provider, llm_cfg, source):
        # The judge resolved to a provider that does not match llm.provider, so
        # it has no credential source (tier entries carry no credentials) and
        # every turn degrades to judge_unavailable. Warn instead of logging a
        # healthy resolution (findings #2/#4).
        log.warning(
            "router.judge_no_credentials",
            provider=provider,
            model=model,
            source=source,
            llm_provider=str(getattr(llm_cfg, "provider", "") or ""),
        )
        return
    log.info(
        "router.judge_resolved",
        provider=provider,
        model=model,
        source=source,
        base_url=base_url,
    )


def _should_build_provider_selector(*, provider: str, api_key: str) -> bool:
    """Whether boot has enough to construct a runtime ``provider_selector``.

    True when an API key is present, or when the provider does not require one
    (local providers — ollama / lm_studio / ovms). Key-requiring providers with
    no key stay ``False`` so the turn runner surfaces ``no_provider``, the
    correct signal for a missing credential. Unknown provider ids fall back to
    key presence rather than raising during boot.
    """
    if api_key:
        return True
    from agentos.provider.registry import UnknownProviderError, get_provider_spec

    try:
        spec = get_provider_spec(provider)
    except UnknownProviderError:
        return False
    return not spec.requires_api_key()


def _router_tier_provider_mismatches(*, config: GatewayConfig, llm_provider: str) -> dict[str, str]:
    """Router tiers whose declared provider differs from the runtime provider.

    Routing is single-provider: boot builds ONE client from ``llm.provider``;
    ``agentos_router.tiers[*].provider`` is metadata and never builds a client.
    A tier pointing elsewhere silently misroutes (local providers 404 and the
    engine degrades such routes to ``llm.model``), so boot surfaces the
    mismatch. Returns ``{tier_name: tier_provider}`` for the offending tiers;
    tiers without a non-empty ``provider`` value do not participate.
    """
    router_cfg = getattr(config, "agentos_router", None)
    if router_cfg is None or not getattr(router_cfg, "enabled", False):
        return {}
    tiers = getattr(router_cfg, "tiers", None)
    if not isinstance(tiers, dict):
        return {}
    runtime_provider = str(llm_provider).strip().lower()
    mismatched: dict[str, str] = {}
    for tier_name, tier in tiers.items():
        if not isinstance(tier, dict):
            continue
        tier_provider = str(tier.get("provider") or "").strip()
        if tier_provider and tier_provider.lower() != runtime_provider:
            mismatched[str(tier_name)] = tier_provider
    return mismatched


def _warn_on_tier_provider_mismatch(config: GatewayConfig, llm_provider: str) -> None:
    """One-time boot warning for router tiers that point at a foreign provider."""
    mismatched = _router_tier_provider_mismatches(config=config, llm_provider=llm_provider)
    if not mismatched:
        return
    log.warning(
        "agentos_router.tier_provider_mismatch",
        llm_provider=llm_provider,
        mismatched_tiers=mismatched,
        note=(
            "tiers only select the MODEL; requests always go through "
            "llm.provider — mismatched tiers are degraded to llm.model "
            "on local providers"
        ),
    )


def validate_agentos_router_runtime(config: GatewayConfig) -> None:
    """Validate router runtime prerequisites for the configured strategy.

    Registry-driven (``agentos.router_strategies``): a strategy whose
    ``requires_local_assets`` is set runs its ``asset_probe`` and reports any
    missing files. A missing asset only warns (routing degrades to the default
    tier) unless ``require_router_runtime`` is set, in which case it raises. A
    ``uses_judge`` strategy needs no local assets: resolve and log the judge
    target instead.
    """
    from agentos.router_strategies import get_strategy_info, resolve_strategy_id

    router_cfg = getattr(config, "agentos_router", None)
    if router_cfg is None or not getattr(router_cfg, "enabled", False):
        return

    strategy = resolve_strategy_id(getattr(router_cfg, "strategy", DEFAULT_ROUTER_STRATEGY))
    info = get_strategy_info(strategy)
    if info is not None and info.requires_local_assets and info.asset_probe is not None:
        missing = info.asset_probe(router_cfg)
        if missing:
            message = f"missing {strategy} router assets: {missing}"
            if getattr(router_cfg, "require_router_runtime", False):
                raise RuntimeError(message)
            log.warning(
                "build_services.agentos_router_bundle_missing",
                strategy=strategy,
                missing=missing,
            )
            return
        log.info(
            "build_services.agentos_router_bundle_ready",
            strategy=strategy,
        )
        return
    if info is not None and info.uses_judge:
        _log_resolved_judge(config, router_cfg)
        return
    if info is not None and info.requires_remote_credentials and info.credential_probe is not None:
        # A remote-credential strategy (Jev) needs no local assets and no
        # judge; its only preflight is "is there a key?". Missing → warn (every
        # turn degrades to the default tier) unless require_router_runtime.
        problem = info.credential_probe(router_cfg)
        if problem:
            message = f"{strategy} router credentials missing: {problem}"
            if getattr(router_cfg, "require_router_runtime", False):
                raise RuntimeError(message)
            log.warning(
                "build_services.agentos_router_credentials_missing",
                strategy=strategy,
                problem=problem,
            )
            return
        log.info("build_services.agentos_router_ready", strategy=strategy)


def _preload_agentos_router_strategy(router_cfg: Any, llm_cfg: Any = None) -> object:
    from agentos.engine.steps.agentos_router import preload_strategy

    return preload_strategy(router_cfg, llm_cfg)


async def preload_agentos_router_runtime(config: GatewayConfig) -> None:
    router_cfg = getattr(config, "agentos_router", None)
    if router_cfg is None or not getattr(router_cfg, "enabled", False):
        return

    strategy_name = str(
        getattr(router_cfg, "strategy", DEFAULT_ROUTER_STRATEGY) or DEFAULT_ROUTER_STRATEGY
    ).strip()
    try:
        log.info("gateway.agentos_router_preload_started", strategy=strategy_name)
        await asyncio.to_thread(
            _preload_agentos_router_strategy,
            router_cfg,
            getattr(config, "llm", None),
        )
        from agentos.router_strategies import get_strategy_info

        info = get_strategy_info(strategy_name)
        if info is not None and info.uses_judge:
            _log_resolved_judge(config, router_cfg)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "gateway.agentos_router_preload_failed",
            strategy=strategy_name,
            error=str(exc),
        )


async def _discover_configured_mcp_servers(
    config: GatewayConfig,
    tool_registry: ToolRegistry,
) -> None:
    """Connect configured MCP servers without starting an interactive OAuth flow."""
    if not config.mcp.enabled:
        return
    if not config.mcp.servers:
        log.info("build_services.mcp_enabled_no_servers")
        return

    from agentos.mcp.discovery import discover_and_register
    from agentos.mcp.streamable_http import FileOAuthStorage
    from agentos.mcp.types import MCPServerConfig

    timeout = config.mcp.connect_timeout_seconds
    for entry in config.mcp.servers:
        try:
            mcp_cfg = MCPServerConfig(
                name=entry.name,
                transport=entry.transport,
                command=entry.command,
                args=entry.args,
                url=entry.url,
                env=entry.env,
                headers=entry.headers,
                oauth=entry.oauth,
                state_dir=config.state_dir,
                tool_timeout_seconds=entry.tool_timeout_seconds,
            )
            if mcp_cfg.oauth and mcp_cfg.url:
                storage = FileOAuthStorage(mcp_cfg.name, mcp_cfg.url, mcp_cfg.state_dir)
                if not await storage.is_authenticated():
                    log.info(
                        "build_services.mcp_server_authentication_required",
                        server=entry.name,
                    )
                    continue

            names = await asyncio.wait_for(
                discover_and_register(mcp_cfg, tool_registry),
                timeout=timeout,
            )
            log.info(
                "build_services.mcp_server_registered",
                server=entry.name,
                tools=len(names),
            )
        except TimeoutError:
            log.warning(
                "build_services.mcp_server_timeout",
                server=entry.name,
                timeout=timeout,
            )
        except Exception as exc:
            log.warning(
                "build_services.mcp_server_failed",
                server=entry.name,
                error=str(exc),
            )


async def build_services(
    config: GatewayConfig | None = None,
    session_manager: Any = None,
    provider_selector: Any = None,
    tool_registry: Any = None,
    usage_tracker: Any = None,
    session_db_path: str = ":memory:",
    extra_agent_ids: list[str] | None = None,
    seed_agent_workspaces: bool = True,
    turn_hooks: Sequence[Any] | None = None,
    compaction_hooks: Sequence[Any] | None = None,
    tool_hooks: Sequence[Any] | None = None,
    start_scheduler: bool = True,
) -> ServiceContainer:
    """Initialize reusable services without any gateway-specific side effects.

    ``start_scheduler=False`` builds the cron scheduler without starting its
    timer (or its startup catch-up): the gateway's safe mode starts it later.

    This is the standalone entry point for service construction. It builds
    all the pieces that both the ASGI gateway and the CLI ``--standalone``
    path need: session storage, provider selector, tool registry, memory,
    skills, scheduler, search, and MCP discovery.

    Parameters that are *None* are auto-constructed from *config* defaults.
    Pass explicit instances to override (useful for tests and embedding).

    Returns a populated :class:`ServiceContainer`.
    """
    # ── Load .env files (cwd/.env > ~/.agentos/.env, never override existing) ──
    from agentos.env import load_env

    load_env()

    # ── Config ──────────────────────────────────────────────────────
    if config is None:
        config = GatewayConfig.load(os.environ.get("AGENTOS_GATEWAY_CONFIG_PATH"))
        if config.config_path:
            log.info("build_services.config_loaded", path=config.config_path)
    _warn_workspace_state_mismatch(config)
    _warn_temporary_workspace(config)

    validate_agentos_router_runtime(config)
    from agentos.memory.embedding_resolver import resolve_memory_embedding

    resolve_memory_embedding(config.memory, local_available=lambda *_: False)
    if seed_agent_workspaces:
        _ensure_configured_agent_workspaces(config, extra_agent_ids=extra_agent_ids)

    # Inject config into the Control tool module (needed by gateway and standalone).
    from agentos.tools.builtin.control import set_gateway_config

    set_gateway_config(config)

    from agentos.tools.ssrf import configure_trusted_fake_ip_cidrs

    configure_trusted_fake_ip_cidrs(config.tools.trusted_fake_ip_cidrs)

    # ── Sandbox runtime ─────────────────────────────────────────────
    # validate_combination emits structured warnings; configure_runtime
    # assembles the backend + gate + ledger so tool handlers can call
    # through the ``@sandboxed`` decorator.
    try:
        from agentos.sandbox.integration import configure_runtime

        effective = configure_runtime(
            config.sandbox,
            workspace=Path(config.workspace_dir) if config.workspace_dir else None,
        )
        log.info(
            "build_services.sandbox_ready",
            **effective.effective.as_dict(),
        )
    except Exception as e:  # pragma: no cover - boot diagnostics only
        log.exception("build_services.sandbox_configure_failed", error=str(e))
        raise

    # ── Schema migrations (before any DB connects) ──────────────────
    # Runs pending migrations on the session DB before SessionStorage opens it,
    # so SQLModel-backed tables (SessionNode, TranscriptEntry, SessionSummary)
    # see the expected columns. Skipped for in-memory DBs (CLI standalone) —
    # yoyo would operate on a separate in-memory connection from storage.
    # Migration failures propagate: code ships behind the migration, never
    # ahead of it — silently booting on an out-of-date schema is worse than
    # failing loud.
    if session_db_path != ":memory:":
        from agentos.persistence.migrator import apply_pending

        if "://" not in session_db_path:
            Path(session_db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        migrations_dir = _resolve_migrations_dir()
        try:
            applied = apply_pending(session_db_path, migrations_dir)
        except Exception as exc:
            # A schema that cannot migrate is a config-class failure: the
            # supervisor contract says exit 78, repair, then retry.
            raise GatewayConfigError(
                f"schema migration failed for {session_db_path}: {type(exc).__name__}: {exc}"
            ) from exc
        if applied:
            log.info("build_services.migrations_applied", count=len(applied), ids=applied)

    # ── Agent registry (built early so SessionManager can resolve agent configs) ─
    from agentos.agents.registry import AgentRegistry

    agent_registry = AgentRegistry(config)

    # ── Session manager ─────────────────────────────────────────────
    if session_manager is None:
        from agentos.session.manager import SessionManager
        from agentos.session.storage import SessionStorage

        Path(session_db_path).parent.mkdir(parents=True, exist_ok=True)
        storage = SessionStorage(session_db_path)
        await storage.connect()
        session_manager = SessionManager(
            storage,
            agent_registry=agent_registry,
            checkpoint_workspace_dir=config.workspace_dir,
        )

    # Wire session manager into tool layer (like set_scheduler, set_gateway_config)
    from agentos.tools.builtin.sessions import (
        set_gateway_config as _set_sessions_gateway_config,
    )
    from agentos.tools.builtin.sessions import set_session_manager

    set_session_manager(session_manager)
    _set_sessions_gateway_config(config)

    from agentos.tools.builtin.projects import (
        set_session_manager as _set_projects_session_manager,
    )

    _set_projects_session_manager(session_manager)
    session_storage = get_session_storage(session_manager)

    # Wire agent registry into the agents_list tool surface.
    from agentos.tools.builtin.agents import set_agent_registry as _set_agent_registry_tool

    _set_agent_registry_tool(agent_registry)

    # ── Provider selector ───────────────────────────────────────────
    llm_runtime = resolve_llm_runtime_config(config)
    api_key = llm_runtime.api_key
    resolved_base = llm_runtime.base_url
    proxy = llm_runtime.proxy
    if provider_selector is None:
        if _should_build_provider_selector(provider=llm_runtime.provider, api_key=api_key):
            from agentos.provider.circuit_breaker import (
                BreakerSettings,
                ProviderCircuitBreaker,
            )
            from agentos.provider.selector import (
                ModelSelector,
                ProviderConfig,
                SelectorConfig,
            )

            if resolved_base.endswith("/v1"):
                resolved_base = resolved_base[:-3]
            breaker_settings = BreakerSettings.from_config(
                getattr(getattr(config, "llm", None), "circuit_breaker", None)
            )
            provider_selector = ModelSelector(
                SelectorConfig(
                    primary=ProviderConfig(
                        provider=llm_runtime.provider,
                        model=llm_runtime.model,
                        api_key=api_key,
                        base_url=resolved_base,
                        proxy=proxy,
                        provider_routing=llm_runtime.provider_routing,
                    )
                ),
                breaker=ProviderCircuitBreaker(breaker_settings),
            )
            log.info(
                "build_services.provider_ready",
                provider=llm_runtime.provider,
                model=llm_runtime.model,
            )

    # Routing is single-provider: warn once when enabled router tiers declare a
    # provider that differs from the one client boot just selected.
    _warn_on_tier_provider_mismatch(config, llm_runtime.provider)

    # ── Model catalog (boot order: after provider selector) ──────────
    # Keep a catalog for every provider so direct-provider runtime paths still
    # get static fallback capabilities (for example DeepSeek v4 thinking
    # replay) even when only OpenRouter performs a remote model-list fetch.
    from agentos.provider.model_catalog import ModelCatalog

    model_catalog = ModelCatalog()
    if api_key and config.llm.provider == "openrouter":
        try:
            await asyncio.wait_for(
                model_catalog.fetch_openrouter(api_key, resolved_base, proxy),
                timeout=5.0,
            )
            log.info("build_services.model_catalog_ready", count=len(model_catalog))
        except Exception as e:
            log.warning("build_services.model_catalog_failed", error=str(e))

        try:
            from agentos.engine.pricing import refresh_live_prices

            pricing_models = {str(config.llm.model)} if config.llm.model else set()
            router_cfg = getattr(config, "agentos_router", None)
            if router_cfg is not None:
                for tier_cfg in getattr(router_cfg, "tiers", {}).values():
                    model_id = tier_cfg.get("model") if isinstance(tier_cfg, dict) else None
                    if model_id:
                        pricing_models.add(str(model_id))
            await asyncio.to_thread(
                refresh_live_prices,
                pricing_models,
                f"{resolved_base.rstrip('/')}/v1",
            )
            log.info("build_services.pricing_cache_ready", count=len(pricing_models))
        except Exception as e:
            log.warning("build_services.pricing_cache_failed", error=str(e))
    elif config.llm.provider == "bankr":
        # The Bankr gateway serves a live model catalog; fetch it so model
        # metadata (context/output/vision) reflects the current gateway state.
        try:
            await asyncio.wait_for(
                model_catalog.fetch_bankr(resolved_base, api_key, proxy),
                timeout=5.0,
            )
            log.info("build_services.model_catalog_ready", count=len(model_catalog))
        except Exception as e:
            log.warning("build_services.model_catalog_failed", error=str(e))
    elif config.llm.provider == "opencap":
        # OpenCAP publishes an unauthenticated catalog with model limits and
        # modalities. Failure is non-fatal; static capability fallbacks remain.
        try:
            catalog_data = await asyncio.wait_for(
                model_catalog.fetch_opencap(proxy),
                timeout=5.0,
            )
            log.info("build_services.model_catalog_ready", count=len(model_catalog))
        except Exception as e:
            log.warning("build_services.model_catalog_failed", error=str(e))
        else:
            # Reuse the same response for usage estimates so the first completed
            # turn does not issue a duplicate synchronous catalog request.
            try:
                from agentos.engine.pricing import seed_opencap_price_cache

                price_count = seed_opencap_price_cache(catalog_data)
                log.info("build_services.pricing_cache_ready", count=price_count)
            except Exception as e:
                log.warning("build_services.pricing_cache_failed", error=str(e))
    elif config.llm.provider == "surplus":
        # Surplus publishes an unauthenticated marketplace catalog carrying
        # per-token rates that move with seller competition, so the boot fetch
        # doubles as the price seed. Failure is non-fatal; static capability
        # fallbacks remain.
        try:
            catalog_data = await asyncio.wait_for(
                model_catalog.fetch_surplus(proxy),
                timeout=5.0,
            )
            log.info("build_services.model_catalog_ready", count=len(model_catalog))
        except Exception as e:
            log.warning("build_services.model_catalog_failed", error=str(e))
        else:
            # Reuse the same response for usage estimates so the first completed
            # turn does not issue a duplicate synchronous catalog request.
            try:
                from agentos.engine.pricing import seed_surplus_price_cache

                price_count = seed_surplus_price_cache(catalog_data)
                log.info("build_services.pricing_cache_ready", count=price_count)
            except Exception as e:
                log.warning("build_services.pricing_cache_failed", error=str(e))

    # ── Tool registry ───────────────────────────────────────────────
    if tool_registry is None:
        from agentos.tools.registry import get_default_registry

        tool_registry = get_default_registry()

    try:
        from agentos.tools.builtin.session_search import create_session_search_tool

        if session_storage is not None:
            create_session_search_tool(session_storage, registry=tool_registry)
            log.info("build_services.session_search_tool_registered")
        else:
            log.warning("build_services.session_search_tool_skipped", reason="storage_unavailable")
    except Exception as e:
        log.warning("build_services.session_search_tool_failed", error=str(e))

    try:
        from agentos.tools.builtin.media import configure_audio, configure_image_generation

        configure_image_generation(
            config.image_generation,
            llm_config=config.llm,
            agentos_router_config=config.agentos_router,
        )
        configure_audio(config.audio)
    except Exception as e:
        log.warning("build_services.image_generation_config_failed", error=str(e))

    # ── Memory tools (boot order 18) — per-agent stores ──────────────
    # Pre-bind to empty defaults so the ServiceContainer init below and
    # the deferred TurnRunner-ref callback both work even if the try
    # block aborts.
    memory_managers: dict[str, MemoryManager] = {}
    memory_stores: dict[str, Any] = {}
    memory_retrievers: dict[str, Any] = {}
    memory_sync_managers: dict[str, Any] = {}
    turn_capture_services: dict[str, Any] = {}
    memory_provider_managers: dict[str, Any] = {}
    memory_watchers: list[Any] = []
    _turn_runner_ref: list = []
    try:
        from agentos.memory.manager import build_memory_managers
        from agentos.tools.builtin.memory_tools import create_memory_tools

        agent_ids = _configured_agent_ids(config, extra_agent_ids)
        # Provider tools that collide with a live builtin tool name are skipped
        # (resolves TODO(B4) in memory/manager.py): the tool registry's names
        # are only known here, so pass them into the provider-manager build.
        reserved_tool_names = set(tool_registry.list_names()) if tool_registry else set()
        memory_managers = await build_memory_managers(
            config,
            agent_ids,
            session_storage=session_storage,
            reserved_tool_names=reserved_tool_names,
        )

        # Derive legacy per-tier views from the managers. These remain in
        # `ServiceContainer` until downstream consumers
        # (TurnRunner, CLI, memory_tools) onto `memory_managers` directly.
        memory_stores = {aid: m.store for aid, m in memory_managers.items()}
        memory_retrievers = {aid: m.retriever for aid, m in memory_managers.items()}
        memory_sync_managers = {aid: m.sync_manager for aid, m in memory_managers.items()}
        turn_capture_services = {aid: m.turn_capture for aid, m in memory_managers.items()}
        # External memory provider managers (Plan B) — only agents whose
        # provider was configured AND available at boot appear here.
        memory_provider_managers = {
            aid: m.provider_manager
            for aid, m in memory_managers.items()
            if m.provider_manager is not None
        }
        memory_watchers = [m.sync_manager for m in memory_managers.values()]

        # Deferred callback: TurnRunner doesn't exist yet, so we capture a
        # mutable list ref that start_gateway_server() will populate later.
        def _on_memory_write(agent_id: str) -> None:
            if _turn_runner_ref:
                _turn_runner_ref[0].refresh_memory_snapshot(agent_id)

        if memory_stores and memory_retrievers:
            create_memory_tools(
                stores=memory_stores,
                retrievers=memory_retrievers,
                memory_base=config.state_dir,
                registry=tool_registry,
                memory_source=getattr(config.memory, "source", "state"),
                on_memory_write=_on_memory_write,
                memory_config=config.memory,
                config_root=config,
                workspace_base=config.workspace_dir
                if getattr(config.memory, "source", "state") == "workspace"
                else None,
                provider_managers=memory_provider_managers or None,
            )
            log.info("build_services.memory_tools_registered", agents=list(memory_stores))
            # TODO(B4/B5): expose provider-routed tools. Every configured
            # provider manager already computes its exposable schemas
            # (``get_tool_schemas()`` / ``handle_tool_call``) with reserved
            # builtin names skipped. Registering them into ``tool_registry``
            # needs a per-agent routing handler (the registry is global; the
            # active agent is resolved from ``current_tool_context``, as the
            # builtin memory tools do). Deferred as an additive step — no
            # in-tree provider ships tools yet (mem0 arrives in B5). Left
            # unexposed with this marker rather than forcing invasive
            # per-agent tool plumbing now.
            for _agent_id, _pm in (memory_provider_managers or {}).items():
                if _pm.get_tool_schemas():
                    log.info(
                        "build_services.memory_provider_tools_unexposed",
                        agent_id=_agent_id,
                        tool_count=len(_pm.get_tool_schemas()),
                    )
    except Exception as e:
        log.warning("build_services.memory_tools_failed", error=str(e))

    # ── Skill loader (boot order 19) ────────────────────────────────
    skill_loader = None
    try:
        from agentos.skills.loader import SkillLoader
        from agentos.skills.paths import resolve_skill_layer_dirs

        workspace_root_raw = getattr(config, "workspace_dir", None)
        workspace_root = Path(workspace_root_raw) if workspace_root_raw else None
        workspace_override = (
            Path(config.skills.workspace_dir) if config.skills.workspace_dir else None
        )
        layer_dirs = resolve_skill_layer_dirs(
            allow_bundled=config.skills.allow_bundled,
            workspace_root=workspace_root,
            workspace_override=workspace_override,
            managed_override=config.skills.managed_dir,
            extra_dirs=[Path(d) for d in config.skills.extra_dirs],
        )
        skill_loader = SkillLoader(
            bundled_dir=layer_dirs.bundled_dir,
            workspace_dir=layer_dirs.workspace_dir,
            managed_dir=layer_dirs.managed_dir,
            personal_agents_dir=layer_dirs.personal_agents_dir,
            project_agents_dir=layer_dirs.project_agents_dir,
            extra_dirs=layer_dirs.extra_dirs,
        )
        log.info(
            "build_services.skill_loader_initialized",
            bundled_dir=str(layer_dirs.bundled_dir),
        )

        # Register skill_list and skill_view tools
        from agentos.tools.builtin.skill_tools import create_skill_tools

        create_skill_tools(skill_loader)
        log.info("build_services.skill_tools_registered")
    except Exception as e:
        log.warning("build_services.skill_loader_failed", error=str(e))

    # ── Cron scheduler (boot order 20) ──────────────────────────────
    cron_scheduler = None
    try:
        from agentos.scheduler import JobStore, SchedulerEngine

        scheduler_db = Path(
            os.environ.get("AGENTOS_SCHEDULER_DB", str(_state_path(config, "scheduler.db")))
        )
        scheduler_db.parent.mkdir(parents=True, exist_ok=True)
        job_store = JobStore(db_path=str(scheduler_db))
        await job_store.open()
        cron_scheduler = SchedulerEngine(
            store=job_store,
            session_store=storage,  # SessionStorage instance from session manager boot
            config={
                "max_concurrent_runs": int(os.environ.get("AGENTOS_CRON_MAX_CONCURRENT", "3")),
                "max_catchup_jobs": int(os.environ.get("AGENTOS_CRON_MAX_CATCHUP", "5")),
                "session_retention": int(os.environ.get("AGENTOS_CRON_SESSION_RETENTION", "86400")),
                "startup_agent_delay_seconds": float(
                    getattr(
                        getattr(config, "scheduler", None), "startup_agent_delay_seconds", 120.0
                    )
                ),
            },
        )
        if start_scheduler:
            await cron_scheduler.start()
        # Inject into the Control tool module so `cron` can dispatch to the scheduler.
        from agentos.tools.builtin.control import set_scheduler

        set_scheduler(cron_scheduler)
        log.info(
            "build_services.cron_scheduler_started"
            if start_scheduler
            else "build_services.cron_scheduler_deferred"
        )
    except Exception as e:
        log.warning("build_services.cron_scheduler_failed", error=str(e))

    # ── Usage tracker ───────────────────────────────────────────────
    # The spend ledger that budget ceilings read lives in its own state file,
    # not in the session DB: it is written synchronously from the turn hot
    # path, and sharing a file with the session store's async writer would put
    # every ledger commit behind that write lock on the event loop. An
    # in-memory session DB (CLI standalone / tests) means no durable state
    # directory is in play, so the tracker stays in-memory too.
    if usage_tracker is None:
        ledger_db_path: str | None = None
        if session_db_path != ":memory:":
            ledger_db = _state_path(config, "spend_ledger.db")
            try:
                ledger_db.parent.mkdir(parents=True, exist_ok=True)
                ledger_db_path = str(ledger_db)
            except OSError as e:
                # A ledger we cannot create must not stop the gateway; budgets
                # fall back to in-process accounting for this run.
                log.warning("build_services.spend_ledger_unavailable", error=str(e))
        usage_tracker = _UsageTracker(
            default_provider_id=config.llm.provider,
            ledger_db_path=ledger_db_path,
        )

    # ── Auxiliary LLM client (needs the tracker above to bill side tasks) ──
    try:
        from agentos.provider.auxiliary import configure_auxiliary

        configure_auxiliary(
            config.auxiliary,
            llm_config=config.llm,
            usage_tracker=usage_tracker,
        )
    except Exception as e:
        log.warning("build_services.auxiliary_config_failed", error=str(e))

    # ── Bounded registry ceilings ───────────────────────────────────
    try:
        from agentos.util.bounded_registry import configure_registry_limits

        configure_registry_limits(
            session_max_entries=config.registry_session_max_entries,
            cache_max_entries=config.registry_cache_max_entries,
            cache_ttl_seconds=config.registry_cache_ttl_seconds,
        )
    except Exception as e:
        log.warning("build_services.registry_limits_failed", error=str(e))

    # ── Search provider (brave > duckduckgo fallback) ───────────────
    try:
        import agentos.search.providers.brave  # noqa: F401 — registers provider
        import agentos.search.providers.duckduckgo  # noqa: F401 — registers provider
        import agentos.search.providers.tavily  # noqa: F401 — registers provider
        from agentos.search.registry import get_provider_spec
        from agentos.tools.builtin.web import configure_search

        provider = config.search_provider
        search_api_key = config.search_api_key
        if not search_api_key:
            env_key = config.search_api_key_env or get_provider_spec(provider).env_key
            search_api_key = os.environ.get(env_key, "") if env_key else ""
        # Auto-select: use brave if key is available and provider is default
        if provider == "duckduckgo":
            if search_api_key or os.environ.get("BRAVE_SEARCH_API_KEY"):
                provider = "brave"

        configure_search(
            provider_name=provider,
            max_results=config.search_max_results,
            api_key=search_api_key,
            proxy=config.search_proxy,
            use_env_proxy=config.search_use_env_proxy,
            fallback_policy=config.search_fallback_policy,
            diagnostics=config.search_diagnostics,
        )
        log.info("build_services.search_provider_initialized", provider=provider)
    except Exception as e:
        log.warning("build_services.search_provider_failed", error=str(e))

    # ── X (Twitter) search via xAI ──────────────────────────────────
    try:
        from agentos.tools.builtin.x_search import configure_x_search, x_search_available

        configure_x_search(config.x_search)
        log.info("build_services.x_search_initialized", available=x_search_available())
    except Exception as e:
        log.warning("build_services.x_search_failed", error=str(e))

    # ── Browser automation via agent-browser ────────────────────────
    try:
        from agentos.tools.builtin.browser import browser_available, configure_browser

        configure_browser(config.browser)
        log.info("build_services.browser_initialized", available=browser_available())
    except Exception as e:
        log.warning("build_services.browser_failed", error=str(e))

    # ── MCP discovery (boot order 22) ───────────────────────────────
    await _discover_configured_mcp_servers(config, tool_registry)

    # ── Observability: OTLP trace export & log retention sweeper ────
    otlp_trace_sink = None
    if (
        getattr(config, "observability", None)
        and config.observability.otlp_enabled
        and config.observability.otlp_endpoint
    ):
        try:
            from agentos import __version__
            from agentos.observability.otlp import OtlpTraceSink
            from agentos.observability.trace import register_trace_sink

            otlp_trace_sink = OtlpTraceSink(
                endpoint=config.observability.otlp_endpoint,
                headers=config.observability.otlp_headers,
                service_name=config.observability.otlp_service_name,
                service_version=__version__,
            )
            otlp_trace_sink.start()
            register_trace_sink(otlp_trace_sink)
            log.info(
                "build_services.otlp_trace_sink_registered",
                endpoint=config.observability.otlp_endpoint,
            )
        except Exception as e:
            log.warning("build_services.otlp_trace_sink_failed", error=str(e))

    log_retention_sweeper = None
    log_retention_task = None
    if getattr(config, "observability", None) and (
        config.observability.log_retention_days > 0
        or config.observability.log_retention_max_total_mb > 0
    ):
        try:
            from agentos.observability.retention import LogRetentionSweeper
            from agentos.observability.trace import default_log_dir

            log_retention_sweeper = LogRetentionSweeper(
                log_dir=default_log_dir(),
                retention_days=config.observability.log_retention_days,
                max_total_bytes=config.observability.log_retention_max_total_mb * 1024 * 1024,
                sweep_interval_s=config.observability.log_retention_sweep_interval_s,
            )
            try:
                log_retention_task = create_background_task(log_retention_sweeper.run_loop())
            except RuntimeError:
                pass
            log.info("build_services.log_retention_sweeper_started")
        except Exception as e:
            log.warning("build_services.log_retention_sweeper_failed", error=str(e))

    svc = ServiceContainer(
        config=config,
        provider_selector=provider_selector,
        tool_registry=tool_registry,
        session_manager=session_manager,
        skill_loader=skill_loader,
        usage_tracker=usage_tracker,
        cron_scheduler=cron_scheduler,
        model_catalog=model_catalog,
        agent_registry=agent_registry,
        memory_managers=memory_managers,
        memory_stores=memory_stores,
        memory_sync_managers=memory_sync_managers,
        memory_watchers=memory_watchers,
        memory_retrievers=memory_retrievers,
        turn_capture_services=turn_capture_services,
        memory_provider_managers=memory_provider_managers,
        turn_hooks=turn_hooks,
        compaction_hooks=compaction_hooks,
        tool_hooks=tool_hooks,
        log_retention_sweeper=log_retention_sweeper,
        log_retention_task=log_retention_task,
        otlp_trace_sink=otlp_trace_sink,
    )
    # Attach deferred callback ref so start_gateway_server can wire TurnRunner
    svc._turn_runner_ref = _turn_runner_ref  # type: ignore[attr-defined]
    return svc


def build_turn_runner_from_services(
    svc: Any,
    *,
    config: GatewayConfig | None = None,
    diagnostics_state: Any | None = None,
) -> Any:
    """Build a TurnRunner with every service-backed memory integration wired.

    Provides a standalone per-session lock dict for CLI/standalone paths (no
    TaskRuntime).  When the caller is the gateway boot path, the boot wiring
    overrides ``task_runtime._get_session_lock_for_turn`` so both classes
    share a single lock per session.
    """
    import asyncio as _asyncio

    from agentos.engine.runtime import TurnRunner

    resolved_config = config if config is not None else svc.config
    # Standalone lock dict for CLI / test paths (no TaskRuntime involved).
    # Gateway path replaces this with task_runtime._get_session_lock_for_turn
    # immediately after task_runtime is constructed.
    _standalone_locks: dict[str, _asyncio.Lock] = {}

    def _standalone_lock_provider(session_key: str) -> _asyncio.Lock:
        return _standalone_locks.setdefault(session_key, _asyncio.Lock())

    runner = TurnRunner(
        provider_selector=svc.provider_selector,
        tool_registry=svc.tool_registry,
        session_manager=svc.session_manager,
        skill_loader=svc.skill_loader,
        usage_tracker=svc.usage_tracker,
        config=resolved_config,
        memory_sync_managers=getattr(svc, "memory_sync_managers", None) or None,
        model_catalog=getattr(svc, "model_catalog", None),
        memory_retrievers=getattr(svc, "memory_retrievers", None) or None,
        turn_capture_services=getattr(svc, "turn_capture_services", None) or None,
        memory_provider_managers=getattr(svc, "memory_provider_managers", None) or None,
        session_lock_provider=_standalone_lock_provider,
        diagnostics_state=diagnostics_state,
        # Hook registries forwarded from services when present so any user-registered
        # TurnHook / CompactionHook / ToolHook instance flows through to TurnRunner.
        turn_hooks=getattr(svc, "turn_hooks", None),
        compaction_hooks=getattr(svc, "compaction_hooks", None),
        tool_hooks=getattr(svc, "tool_hooks", None),
    )
    ref = getattr(svc, "_turn_runner_ref", None)
    if isinstance(ref, list):
        ref.clear()
        ref.append(runner)
    return runner


async def _start_all_channels(channel_manager: Any) -> dict[str, bool]:
    """Start every channel adapter and log each outcome."""
    results: dict[str, bool] = await channel_manager.start_all()
    return await _log_channel_results(channel_manager, results)


async def _log_channel_results(channel_manager: Any, results: dict[str, bool]) -> dict[str, bool]:
    start_errors_fn = getattr(channel_manager, "start_errors", None)
    start_errors = start_errors_fn() if start_errors_fn is not None else {}
    for name, ok in results.items():
        if ok:
            log.info("gateway.channel_started", channel=name)
        else:
            details = start_errors.get(name, {})
            log.warning(
                "gateway.channel_failed",
                channel=name,
                error_type=details.get("error_type"),
                error=details.get("error"),
                exception=details.get("exception"),
            )
    return results


async def _router_preload_sidecar(config: GatewayConfig, readiness: Any, name: str) -> None:
    """Preload the router runtime; ``/startupz`` turns 200 once this settles."""
    try:
        await preload_agentos_router_runtime(config)
    except Exception as exc:
        readiness.settle_sidecar(name, error=f"{type(exc).__name__}: {exc}")
        raise
    readiness.settle_sidecar(name)


async def start_gateway_server(
    port: int | None = None,
    config: GatewayConfig | None = None,
    session_manager: Any = None,
    provider_selector: Any = None,
    tool_registry: Any = None,
    subscription_manager: Any = None,
    channel_manager: Any = None,
    usage_tracker: Any = None,
    run: bool = True,
    turn_hooks: Sequence[Any] | None = None,
    compaction_hooks: Sequence[Any] | None = None,
    tool_hooks: Sequence[Any] | None = None,
    boot_tracker: Any = None,
    extra_listen_hosts: Sequence[str] = (),
) -> GatewayServer:
    """
    ``extra_listen_hosts`` are further addresses the same app listens on
    (``[gateway] bind = "tailnet"`` adds ``127.0.0.1`` beside the tailnet IP).

    Boot sequence:
    1. Load/validate config
    2. Ensure auth token exists
    3. Record the boot; decide safe mode (crash-loop breaker)
    4. Build ASGI app
    5. Start uvicorn server, then the boot sidecars (channels, router preload)

    ``boot_tracker`` overrides the crash-loop breaker (tests). By default a
    real ``gateway run`` (``run=True``) records into the state directory's
    ledger and an embedded boot records nothing.
    """
    from agentos.gateway.readiness import (
        SIDECAR_CHANNELS,
        SIDECAR_ROUTER,
        GatewayReadiness,
        set_gateway_readiness,
    )

    # Created first so the boot duration it measures (the in-place restart's
    # ``restart_expected_ms``) covers the whole boot.
    readiness = GatewayReadiness()

    # ── Gateway-specific config handling ─────────────────────────────
    if config is None:
        config = GatewayConfig.load(os.environ.get("AGENTOS_GATEWAY_CONFIG_PATH"))

    # Apply runtime port override
    if port is not None:
        config = config.model_copy(update={"port": port})

    # Fail closed on auth.mode="none" + non-loopback bind (issue #18, V3).
    # Runs before any side effect (file logging, PID lock, services) so a
    # refused start leaves no partial state.
    enforce_public_bind_auth_guard(config)
    # A Control UI on a public address needs a declared browser origin, or
    # every console request would be refused by the origin policy.
    from agentos.gateway.origin_policy import enforce_public_bind_origin_guard

    enforce_public_bind_origin_guard(config)

    _setup_file_logging(config)
    if config.config_path:
        log.info("gateway.config_loaded", path=config.config_path)

    if subscription_manager is None:
        from agentos.gateway.websocket import SubscriptionManager

        subscription_manager = SubscriptionManager()

    # Gateway-specific: set env var for other components to discover
    os.environ["AGENTOS_GATEWAY_PORT"] = str(config.port)

    # The desktop hands the gateway an operator secret at spawn; read it now
    # and scrub it so no child process inherits it (see gateway.agent_surface).
    from agentos.gateway.agent_surface import ensure_operator_file, get_agent_surface

    get_agent_surface().load_operator_secret_from_env()
    # And write our own for the local CLI (0600 under wallets/, rotated every
    # boot). Without it the CLI on this machine is bound as an agent, which
    # fails safe, so an unwritable file is a warning rather than a refusal.
    try:
        ensure_operator_file()
    except OSError as exc:
        log.warning("gateway.operator_file_unwritable", error=str(exc))

    # Gateway-specific: ensure auth token exists
    if config.auth.mode == "token" and not config.auth.token:
        token = secrets.token_urlsafe(32)
        config.auth = config.auth.model_copy(update={"token": token})
        config.mark_runtime_secret("auth.token")
        log.info("gateway.auth_token_generated")

    # Gateway-specific: resolve the built React Control UI (boot order 17)
    if config.control_ui.enabled:
        from agentos.gateway.control_ui import _DIST_DIR
        from agentos.health.control_ui import build_hint, control_ui_boot_warning

        bundle_warning = control_ui_boot_warning(_DIST_DIR / "index.html")
        if bundle_warning is not None:
            log.warning(bundle_warning, path=str(_DIST_DIR), hint=build_hint())
        log.info(
            "gateway.control_ui.resolved",
            base_path=config.control_ui.base_path,
            dist=str(_DIST_DIR),
        )
    else:
        log.info("gateway.control_ui.disabled")

    # Surface lexical degradation when the operator enabled filter_enabled=true
    # with a strategy that needs the local ONNX embedding backend.
    emit_skill_filter_banner(config.skills)

    # ── PID file lock ───────────────────────────────────────────────
    # Prevents two gateway instances from sharing the same STATE_DIR.
    # Must run before build_services so the lock is held before any DB work.
    from agentos.gateway.pidlock import GatewayPidLock

    _pid_lock = GatewayPidLock(_state_path(config, ""))
    _pid_lock.acquire()

    # ── Crash-loop breaker ───────────────────────────────────────────
    # Recorded right after the pid lock (one gateway per state dir) and
    # before any service: a crash anywhere below still counts as unclean.
    from agentos.gateway.boot_lifecycle import BootTracker, set_boot_tracker

    if boot_tracker is None:
        boot_tracker = (
            BootTracker.for_state_dir(_state_path(config, "")) if run else BootTracker(None)
        )
    safe_mode = boot_tracker.start()
    readiness.attach_safe_mode(boot_tracker.snapshot)
    set_boot_tracker(boot_tracker)
    set_gateway_readiness(readiness)
    # What safe mode holds back until it clears: every producer of automatic
    # turns (channels, cron, heartbeat). The control plane starts regardless.
    held_back = {"scheduler": safe_mode.active, "heartbeat": safe_mode.active}

    # ── Reusable service initialization via build_services ───────────
    svc = await build_services(
        config=config,
        session_manager=session_manager,
        provider_selector=provider_selector,
        tool_registry=tool_registry,
        usage_tracker=usage_tracker,
        session_db_path=str(_state_path(config, "sessions.db")),
        turn_hooks=turn_hooks,
        compaction_hooks=compaction_hooks,
        tool_hooks=tool_hooks,
        # Started below, once its handlers are registered: a catch-up that
        # ran first would fail missed jobs with "No handler registered".
        start_scheduler=False,
    )

    # Record boot time for uptime calculation (gateway-specific)
    global _boot_time_ms
    _boot_time_ms = int(time.time() * 1000)

    log.info(
        "gateway.starting",
        host=config.host,
        port=config.port,
        auth_mode=config.auth.mode,
    )

    # ── Diagnostics runtime state ───────────────────────────────────
    from agentos.gateway.diagnostics import DiagnosticsState

    diagnostics_state = DiagnosticsState.from_config(config)

    # ── TurnRunner (shared agent orchestration layer) ────────────────
    turn_runner = build_turn_runner_from_services(
        svc,
        config=config,
        diagnostics_state=diagnostics_state,
    )

    # Lazy ref for channel_manager — cron handler captures it via closure,
    # populated after channel_manager is constructed below.
    _cm_holder: list = [None]

    # The cron tool checks a requested delivery channel against the configured
    # ones, so a model cannot save a job aimed at a channel that does not exist.
    from agentos.tools.builtin.control import set_channel_manager_ref

    set_channel_manager_ref(lambda: _cm_holder[0])

    from agentos.scheduler.heartbeat import (
        HeartbeatConfigWatcher,
        HeartbeatRunner,
    )
    from agentos.scheduler.heartbeat_loop import HeartbeatLoop
    from agentos.scheduler.heartbeat_service import HeartbeatService

    heartbeat_service = HeartbeatService(
        turn_runner=turn_runner,
        session_storage=get_session_storage(svc.session_manager) or svc.session_manager,
        channel_manager_ref=lambda: _cm_holder[0],
    )
    heartbeat_loop = HeartbeatLoop(
        config=config,
        heartbeat_service=heartbeat_service,
    )

    from agentos.gateway.background_completion import BackgroundCompletionManager
    from agentos.gateway.event_bridge import EventBridge
    from agentos.gateway.subagent_announce import set_background_completion_manager
    from agentos.gateway.task_runtime import TaskRun, TaskRuntime

    runtime_event_bridge = EventBridge(
        subscription_manager=subscription_manager,
        connection_registry=get_registry(),
    )

    from agentos.engine.cache_break_monitor import add_compaction_listener

    def _emit_runtime_compaction_event(
        session_key: str,
        payload: dict[str, Any],
    ) -> None:
        event_payload = dict(payload or {})
        event_payload.setdefault("status", "completed")
        event_payload.setdefault("source", "automatic")
        emit_coro = runtime_event_bridge.emit(
            session_key,
            "session.event.compaction",
            event_payload,
        )
        try:
            create_background_task(emit_coro)
        except RuntimeError:
            emit_coro.close()

    svc._compaction_listener_remove = add_compaction_listener(_emit_runtime_compaction_event)

    # Approval queue changes reach the consoles as ``approval.requested`` /
    # ``approval.resolved`` instead of waiting for a REST poll.
    from agentos.gateway.approval_broadcast import install_approval_broadcaster

    svc._approval_broadcaster = install_approval_broadcaster()

    background_completion_manager = BackgroundCompletionManager(
        session_manager=svc.session_manager,
        event_emitter=runtime_event_bridge.emit,
        channel_manager_ref=lambda: _cm_holder[0],
    )
    set_background_completion_manager(background_completion_manager)

    async def _subagent_completion_listener(event: Any) -> None:
        from agentos.gateway.subagent_announce import announce_subagent_completion

        await announce_subagent_completion(
            event,
            session_manager=svc.session_manager,
            event_emitter=runtime_event_bridge.emit,
            channel_manager=_cm_holder[0],
            task_runtime=task_runtime,
        )

    async def _task_runtime_turn_handler(run: TaskRun) -> None:
        await dispatch_task_runtime_turn(
            run,
            config=config,
            session_manager=svc.session_manager,
            turn_runner=turn_runner,
            event_emitter=runtime_event_bridge.emit,
        )

    task_runtime = TaskRuntime(
        storage=get_session_storage(svc.session_manager) or svc.session_manager,
        turn_handler=_task_runtime_turn_handler,
        event_emitter=runtime_event_bridge.emit,
        terminal_listener=_subagent_completion_listener,
        lifecycle_listener=_make_task_session_lifecycle_listener(
            session_manager=svc.session_manager,
            event_emitter=runtime_event_bridge.emit,
        ),
        max_concurrency=_task_runtime_max_concurrency(config),
        max_pending_per_session=_task_runtime_max_pending_per_session(config),
        subagent_reserved_slots=int(
            getattr(getattr(config, "subagents", None), "subagent_reserved_slots", 0)
        ),
        turn_hard_deadline_s=_task_runtime_turn_hard_deadline_s(config),
        pending_overflow_policy=getattr(
            config.task_runtime, "pending_overflow_policy", "reject_newest"
        ),
    )
    # Wire task_runtime's short write-lock provider into turn_runner.
    turn_runner.set_session_lock_provider(task_runtime._get_session_lock_for_turn)
    svc.task_runtime = task_runtime
    # Wire the runtime into SessionManager so kill_session can cascade-cancel.
    attach_runtime = getattr(svc.session_manager, "attach_task_runtime", None)
    if callable(attach_runtime):
        attach_runtime(task_runtime)
    from agentos.tools.builtin.sessions import set_task_runtime

    set_task_runtime(task_runtime)

    # Turns a restart or crash cut short: found and resumed once channels are
    # up (started below). Listening from now on, so a user message that
    # arrives first cancels the resume of its session.
    from agentos.gateway.restart import peek_request
    from agentos.gateway.turn_recovery import TurnRecovery, set_turn_recovery

    # Read before the restart announce consumes it: the session that asked
    # for this restart does not get its turn resumed.
    previous_restart = peek_request(config) if run else None
    svc.turn_recovery = TurnRecovery(
        restart_requested_by=(
            previous_restart.origin.session_key if previous_restart is not None else None
        ),
        storage=get_session_storage(svc.session_manager) or svc.session_manager,
        session_manager=svc.session_manager,
        task_runtime=task_runtime,
        auto_resume=bool(getattr(config.task_runtime, "resume_interrupted_turns", True)),
        channel_manager_ref=lambda: _cm_holder[0],
        safe_mode_active=lambda: boot_tracker.safe_mode_active,
    )
    set_turn_recovery(svc.turn_recovery)

    # ACP coding harnesses: reap processes a crashed gateway left behind and
    # publish the session manager. Degrades to "ACP unavailable", never fails boot.
    try:
        from agentos.gateway.acp_wiring import start_acp

        svc.acp_manager = await start_acp(config, session_manager=svc.session_manager)
    except Exception:  # noqa: BLE001
        log.warning("acp.boot_failed", exc_info=True)

    # Resolve HEARTBEAT.md path; instantiate Runner + Watcher;
    # start Watcher BEFORE the Loop so the first tick already sees any
    # frontmatter overrides. ``reload_now()`` runs synchronously at start.
    heartbeat_runner = HeartbeatRunner()
    workspace_dir = config.workspace_dir or ""
    md_path_setting = getattr(config.heartbeat, "config_path", None)
    if md_path_setting:
        heartbeat_md_path = Path(md_path_setting).expanduser()
    elif workspace_dir:
        heartbeat_md_path = Path(workspace_dir).expanduser() / "HEARTBEAT.md"
    else:
        heartbeat_md_path = Path.home() / ".agentos" / "workspace" / "HEARTBEAT.md"
    heartbeat_watcher = HeartbeatConfigWatcher(
        heartbeat_runner,
        heartbeat_md_path,
        loop_listener=heartbeat_loop.apply_overrides,
    )
    await heartbeat_watcher.start()
    svc.heartbeat_watcher = heartbeat_watcher

    if not held_back["heartbeat"]:
        await heartbeat_loop.start()
    svc.heartbeat_loop = heartbeat_loop

    # Register cron agent_run handler (DI-based, no monkey-patch)
    if svc.cron_scheduler is not None:
        from agentos.scheduler.delivery import DeliveryChain
        from agentos.scheduler.handlers import (
            make_agent_run_handler,
            make_script_run_handler,
            make_static_message_handler,
            make_system_event_handler,
        )
        from agentos.scheduler.heartbeat_service import HeartbeatService

        async def _cron_ws_emitter(topic: str, event: str, payload: dict) -> int:
            """Targeted WS push with per-connection error isolation."""
            _registry = get_registry()
            _sub_mgr = subscription_manager
            if _sub_mgr is None:
                return 0
            conn_ids = _sub_mgr.get_topic_subscribers(topic)
            conn_ids |= _sub_mgr.get_topic_subscribers("cron:*")
            sent = 0
            for conn_id in conn_ids:
                conn = _registry.get(conn_id)
                if conn:
                    try:
                        await conn.send_event(event, payload)
                        sent += 1
                    except Exception:
                        pass
            return sent

        async def _session_forwarder(
            origin_session_key: str,
            text: str,
            provenance: dict,
        ) -> bool:
            """Mirror a cron result into the originating session's transcript.

            Returns ``False`` when the origin session no longer exists so the
            delivery chain can treat it as "nothing to mirror" instead of a
            failed run. See :func:`mirror_cron_result_to_session`.
            """
            entry = await mirror_cron_result_to_session(
                svc.session_manager,
                origin_session_key,
                text,
                provenance,
            )
            if entry is None:
                return False

            _sub_mgr = subscription_manager
            if _sub_mgr is None:
                return True

            payload = build_cron_result_payload(origin_session_key, text, entry)

            _registry = get_registry()
            stream_payload = get_session_streams().record(
                origin_session_key,
                "session.event.cron_result",
                payload,
            )
            for conn_id in _sub_mgr.get_message_subscribers(origin_session_key):
                conn = _registry.get(conn_id)
                if conn:
                    try:
                        await conn.send_event("session.event.cron_result", stream_payload)
                    except Exception:
                        pass

            sessions_changed_payload = get_session_streams().record(
                origin_session_key,
                "sessions.changed",
                build_sessions_changed_payload(origin_session_key, "cron_result"),
            )
            for conn_id in (
                _sub_mgr.get_message_subscribers(origin_session_key)
                | _sub_mgr.get_session_subscribers()
            ):
                conn = _registry.get(conn_id)
                if conn:
                    try:
                        await conn.send_event("sessions.changed", sessions_changed_payload)
                    except Exception:
                        pass

            return True

        async def _emit_session_event(
            session_key: str,
            event_name: str,
            payload: dict[str, Any],
        ) -> None:
            _sub_mgr = subscription_manager
            if _sub_mgr is None:
                return

            _registry = get_registry()
            stream_payload = get_session_streams().record_if_replayable(
                session_key, event_name, payload
            )
            conn_ids = _sub_mgr.get_message_subscribers(session_key)
            if event_name.startswith("sessions."):
                conn_ids |= _sub_mgr.get_session_subscribers()

            for conn_id in conn_ids:
                conn = _registry.get(conn_id)
                if conn:
                    try:
                        await conn.send_event(event_name, stream_payload)
                    except Exception:
                        pass

        from agentos.gateway.rpc_cron import _job_to_wire

        delivery_chain = DeliveryChain(
            channel_manager_ref=lambda: _cm_holder[0],
            ws_emitter=_cron_ws_emitter,
            session_forwarder=_session_forwarder,
            job_serializer=lambda job: _job_to_wire(job, config),
        )
        # cron.run.finished goes out once the run and the job's health are on
        # record, so a client re-reading cron.list on it sees the new state.
        add_run_listener = getattr(svc.cron_scheduler, "add_run_listener", None)
        if callable(add_run_listener):
            add_run_listener(delivery_chain.publish_run_finished)

        # Plug DeliveryChain.dispatch_failure_alert into execute_with_timeout
        # so every failed cron run (agent_run raise, system_event raise,
        # TimeoutError, generic exception) reaches the job's configured
        # FailureDestination at runtime. Without this wire the dispatch
        # plumbing is dead in production even though unit tests cover the
        # hook directly.
        from agentos.scheduler.jobs import set_failure_dispatcher

        set_failure_dispatcher(delivery_chain.dispatch_failure_alert)

        def _cron_workspace_resolver(agent_id: str) -> tuple[str | None, bool]:
            workspace_dir = resolve_agent_workspace_dir(agent_id, config)
            workspace_strict = getattr(config, "workspace_strict", None)
            if not isinstance(workspace_strict, bool):
                workspace_strict = bool(workspace_dir)
            return str(workspace_dir), workspace_strict

        agent_handler = make_agent_run_handler(
            delivery_chain=delivery_chain,
            turn_runner_ref=lambda: turn_runner,
            session_manager_ref=lambda: svc.session_manager,
            task_runtime_ref=lambda: task_runtime,
            workspace_resolver=_cron_workspace_resolver,
            default_elevated=lambda: configured_default_elevated(config),
            cron_default_elevated=lambda: configured_cron_default_elevated(config),
        )
        system_handler = make_system_event_handler(
            delivery_chain=delivery_chain,
            turn_runner_ref=lambda: turn_runner,
            session_manager_ref=lambda: svc.session_manager,
            session_event_emitter=_emit_session_event,
            heartbeat_service_ref=lambda: heartbeat_service,
            heartbeat_loop_ref=lambda: heartbeat_loop,
            workspace_resolver=_cron_workspace_resolver,
            default_elevated=lambda: configured_default_elevated(config),
            cron_default_elevated=lambda: configured_cron_default_elevated(config),
        )
        static_handler = make_static_message_handler(delivery_chain=delivery_chain)
        script_handler = make_script_run_handler(delivery_chain=delivery_chain)
        svc.cron_scheduler.register_handler("agent_run", agent_handler)
        svc.cron_scheduler.register_handler("static_message", static_handler)
        svc.cron_scheduler.register_handler("system_event", system_handler)
        svc.cron_scheduler.register_handler("script_run", script_handler)
        log.info("gateway.cron_handler_registered", handler_key="agent_run")
        log.info("gateway.cron_handler_registered", handler_key="static_message")
        log.info("gateway.cron_handler_registered", handler_key="system_event")
        log.info("gateway.cron_handler_registered", handler_key="script_run")
        # Dream was removed; pause any cron rows an older install left behind
        # so they cannot keep firing against a handler that no longer exists.
        await _pause_orphaned_dream_crons(
            scheduler=svc.cron_scheduler,
            agent_ids=_configured_agent_ids(config),
        )
        # Every handler is registered: now the startup catch-up and the timer
        # may run (unless safe mode holds them; its resume hook starts them).
        start_scheduler = getattr(svc.cron_scheduler, "start", None)
        if not held_back["scheduler"] and callable(start_scheduler):
            await start_scheduler()
            log.info("gateway.cron_scheduler_started")

    # Build channel adapters (don't start yet -- app doesn't exist)
    webhook_routes: list = []
    if channel_manager is None and config.channels.channels:
        from agentos.channels.manager import ChannelManager
        from agentos.gateway.event_bridge import EventBridge

        event_bridge = EventBridge(
            subscription_manager=subscription_manager,
            connection_registry=get_registry(),
        )
        channel_rpc_context_factory = _make_channel_rpc_context_factory(
            svc,
            config,
            subscription_manager=subscription_manager,
            channel_manager_ref=lambda: _cm_holder[0],
            turn_runner=turn_runner,
            heartbeat_service=heartbeat_service,
            diagnostics_state=diagnostics_state,
        )
        channel_manager = ChannelManager.from_config(
            config.channels.channels,
            turn_runner=turn_runner,
            session_manager=svc.session_manager,
            event_bridge=event_bridge,
            config=config,
            task_runtime=task_runtime,
            rpc_dispatcher=get_dispatcher(),
            channel_rpc_context_factory=channel_rpc_context_factory,
        )
        webhook_routes = channel_manager.collect_webhook_routes()
        # Populate lazy ref so cron handler can deliver to channels
        _cm_holder[0] = channel_manager
        log.info(
            "gateway.channels_built",
            count=len(config.channels.channels),
            webhooks=len(webhook_routes),
        )

    # Ensure lazy ref covers pre-injected channel_manager too
    if channel_manager is not None:
        _cm_holder[0] = channel_manager

    # ── ASGI app ─────────────────────────────────────────────────────
    app = create_gateway_app(
        config,
        session_manager=svc.session_manager,
        provider_selector=svc.provider_selector,
        tool_registry=svc.tool_registry,
        subscription_manager=subscription_manager,
        channel_manager=channel_manager,
        usage_tracker=svc.usage_tracker,
        skill_loader=svc.skill_loader,
        cron_scheduler=svc.cron_scheduler,
        turn_runner=turn_runner,
        task_runtime=task_runtime,
        heartbeat_service=heartbeat_service,
        heartbeat_loop=heartbeat_loop,
        agent_registry=svc.agent_registry,
        diagnostics_state=diagnostics_state,
        memory_managers=svc.memory_managers,
        memory_stores=svc.memory_stores,
        memory_retrievers=svc.memory_retrievers,
        extra_routes=webhook_routes or None,
    )
    app.state.gateway_ready = False
    app.state.readiness = readiness
    app.state.boot_tracker = boot_tracker
    readiness.attach_active_turns(lambda: int(getattr(task_runtime, "_global_in_flight", 0)))
    if svc.turn_recovery is not None:
        readiness.attach_turn_recovery(svc.turn_recovery.snapshot)

    server_handle = GatewayServer(app=app, config=config)
    server_handle._channel_manager = channel_manager
    server_handle._services = svc
    server_handle._background_completion_manager = background_completion_manager
    server_handle._pid_lock = _pid_lock
    server_handle.readiness = readiness
    server_handle._boot_tracker = boot_tracker
    # Registered before uvicorn listens, so a handshake that lands while the
    # channels are still connecting is told to retry (STARTUP_SIDECARS).
    readiness.begin_sidecar(SIDECAR_CHANNELS)

    if run:
        # In-place restart (``/restart``, ``gateway.restart``, the ``gateway``
        # tool, the CLI fallback). Wired only for a real ``gateway run``: an
        # embedded boot has no ``run_gateway`` to perform the exec.
        from agentos.gateway.restart import GatewayRestarter, set_restarter

        restarter = GatewayRestarter(server_handle, config, pid_lock=_pid_lock)
        server_handle._restarter = restarter
        set_restarter(restarter)

        uvicorn_kwargs: dict[str, Any] = {
            "app": app,
            "host": config.host,
            "port": config.port,
            "log_level": "info" if not config.debug else "debug",
            "access_log": config.debug,
            # The post-handshake ceiling ``hello-ok.policy.max_payload``
            # advertises. The protocol class holds every connection to
            # MAX_PREAUTH_PAYLOAD_BYTES until its handshake succeeds, refusing
            # a larger pre-auth message before reading it.
            "ws_max_size": MAX_PAYLOAD_BYTES,
            "ws": GatewayWebSocketProtocol,
            "timeout_graceful_shutdown": UVICORN_GRACEFUL_SHUTDOWN_S,
        }
        if config.tls.keyfile and config.tls.certfile:
            uvicorn_kwargs["ssl_keyfile"] = config.tls.keyfile
            uvicorn_kwargs["ssl_certfile"] = config.tls.certfile
        uv_config = uvicorn.Config(
            **uvicorn_kwargs,
        )
        server = uvicorn.Server(uv_config)
        server_handle._server = server
        if server_handle.install_signal_handlers():
            # The gateway owns SIGTERM/SIGINT now. uvicorn's own capture would
            # record the signal, return from serve() and re-raise it once the
            # handlers are restored: the process died before close() ran.
            setattr(server, "capture_signals", contextlib.nullcontext)

        listen_hosts = list(dict.fromkeys([config.host, *extra_listen_hosts]))
        if len(listen_hosts) > 1:
            # One app, several listeners: uvicorn serves pre-bound sockets.
            sockets = [bind_listen_socket(host, config.port) for host in listen_hosts]
            task = create_background_task(server.serve(sockets=sockets))
        else:
            task = create_background_task(server.serve())
        server_handle._task = task

        # Warn loudly before the normal started line so operators
        # see the network-exposure notice even on info-level log streams.
        if is_public_bind(config.host):
            log.warning(
                "gateway.bind.public",
                host=config.host,
                port=config.port,
                message=(
                    "gateway bound to a wildcard address; reachable from "
                    "every interface. Opt-in required — only expose behind "
                    "a trusted reverse proxy or VPN."
                ),
            )
        log.info(
            "gateway.started",
            host=config.host,
            port=config.port,
            extra_hosts=list(extra_listen_hosts) or None,
        )

    # Start channels (after app is ready to receive webhooks)
    channel_start_error: str | None = None
    if channel_manager is not None and not safe_mode.active:
        started = await _start_all_channels(channel_manager)
        failed_at_boot = sorted(name for name, ok in started.items() if not ok)
        if failed_at_boot:
            errors_fn = getattr(channel_manager, "start_errors", None)
            errors = errors_fn() if callable(errors_fn) else {}
            channel_start_error = "; ".join(
                f"{name}: {errors.get(name, {}).get('error_type', 'error')}: "
                f"{errors.get(name, {}).get('error', '')}".rstrip(": ")
                for name in failed_at_boot
            )
    elif channel_manager is not None:
        log.warning(
            "gateway.safe_mode.channels_held",
            count=len(list(channel_manager.items())),
            hint="run 'agentos channels start --yes' to start them now",
        )
    readiness.settle_sidecar(SIDECAR_CHANNELS, error=channel_start_error)
    failed_channels = getattr(channel_manager, "failed_channels", None)
    if callable(failed_channels):
        # Live, not a boot snapshot: a channel the sweep brings back drops out.
        readiness.attach_failing_channels(lambda: list(failed_channels()))

    async def _resume_after_safe_mode(reason: str) -> None:
        """Start what safe mode held back; runs once, when it clears."""
        if channel_manager is not None:
            # Not start_all: an operator may have started one channel by name
            # while safe mode held (channels.start <name>).
            start_stopped = getattr(channel_manager, "start_stopped", None)
            if callable(start_stopped):
                await _log_channel_results(channel_manager, await start_stopped())
            else:
                await _start_all_channels(channel_manager)
        if held_back["scheduler"] and svc.cron_scheduler is not None:
            held_back["scheduler"] = False
            await svc.cron_scheduler.start()
        if held_back["heartbeat"]:
            held_back["heartbeat"] = False
            await heartbeat_loop.start()
        log.info("gateway.safe_mode_resumed", reason=reason)

    boot_tracker.set_resume_hook(_resume_after_safe_mode)

    if run:
        server_handle._web_push = wire_approval_delivery(
            config, channel_manager_ref=lambda: _cm_holder[0]
        )
        if config.config_path:
            from types import SimpleNamespace

            from agentos.gateway.config_watcher import ConfigWatcher, broadcast_to_control

            watcher = ConfigWatcher(
                lambda: SimpleNamespace(config=config, provider_selector=svc.provider_selector),
                emit=broadcast_to_control,
            )
            watcher.start()
            server_handle._config_watcher = watcher
        from agentos.gateway.lifecycle_hooks import STARTUP, run_lifecycle_hooks

        create_background_task(run_lifecycle_hooks(STARTUP, config=config, reason="boot"))
        readiness.begin_sidecar(SIDECAR_ROUTER)
        create_background_task(_router_preload_sidecar(config, readiness, SIDECAR_ROUTER))
        # If the previous image restarted in place on request, tell the chat
        # that asked. Channels are up by now, which is what the send needs.
        from agentos.gateway.restart import announce_restart_completion

        create_background_task(
            announce_restart_completion(
                config,
                channel_manager=channel_manager,
                session_manager=svc.session_manager,
            )
        )
        if svc.turn_recovery is not None:
            svc.turn_recovery.start()

        # Thaw detection, the channel health sweep and safe-mode
        # re-evaluation share one 30 s tick (gateway/maintenance.py).
        from agentos.gateway.maintenance import GatewayMaintenance, set_maintenance

        maintenance = GatewayMaintenance(
            channel_manager_ref=lambda: _cm_holder[0],
            scheduler_ref=lambda: svc.cron_scheduler,
            safe_mode_active=lambda: boot_tracker.safe_mode_active,
            on_tick=[boot_tracker.heartbeat, boot_tracker.reevaluate],
            broadcast=get_registry().broadcast,
        )
        maintenance.start()
        server_handle._maintenance = maintenance
        set_maintenance(maintenance)

    app.state.gateway_ready = True
    readiness.mark_serving()
    return server_handle
