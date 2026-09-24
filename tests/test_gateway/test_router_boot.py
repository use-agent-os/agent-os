from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.engine.types import DoneEvent
from agentos.gateway.boot import (
    _configured_agent_ids,
    _gateway_home,
    _task_runtime_turn_hard_deadline_s,
    _warn_temporary_workspace,
    _warn_workspace_state_mismatch,
    build_services,
    build_task_runtime_run_kwargs,
    dispatch_task_runtime_turn,
    emit_skill_filter_banner,
    preload_agentos_router_runtime,
    validate_agentos_router_runtime,
)
from agentos.gateway.config import AgentEntryConfig, GatewayConfig
from agentos.gateway.diagnostics import DiagnosticsState
from agentos.gateway.routing import build_cli_route_envelope, build_cron_route_envelope
from agentos.onboarding.mutations import upsert_channel
from agentos.scheduler.types import CronJob, JobStatus
from agentos.session.compaction import CompactionConfig
from agentos.tools.registry import ToolRegistry
from agentos.tools.types import CallerKind, ToolContext


def test_gateway_boot_bridges_compaction_notifications_to_session_stream() -> None:
    source = Path("src/agentos/gateway/boot.py").read_text(encoding="utf-8")

    assert "add_compaction_listener" in source
    assert '"session.event.compaction"' in source
    assert "_compaction_listener_remove" in source


def test_task_runtime_default_hard_deadline_is_unbounded() -> None:
    config = GatewayConfig()

    deadline = _task_runtime_turn_hard_deadline_s(config)

    assert deadline is None


def test_task_runtime_hard_deadline_honors_explicit_config() -> None:
    config = GatewayConfig()
    config.task_runtime.turn_hard_deadline_s = 12.5

    assert _task_runtime_turn_hard_deadline_s(config) == 12.5


def test_build_task_runtime_run_kwargs_forwards_fresh_user_session() -> None:
    run = SimpleNamespace(
        agent_id="main",
        attachments=[],
        input_provenance=None,
        run_kind="session_turn",
        no_memory_capture=False,
        fresh_user_session=True,
        ingress_pipeline_steps=(),
        semantic_message=None,
    )

    kwargs = build_task_runtime_run_kwargs(run, tool_context=object(), model="model")

    assert kwargs["fresh_user_session"] is True


def test_gateway_stream_timeouts_allow_long_silent_agent_work() -> None:
    config = GatewayConfig()

    assert config.agent_stream_idle_timeout_seconds == 600.0
    assert config.webui_stream_idle_grace_seconds == 630.0
    assert config.webui_stream_idle_grace_seconds > config.agent_stream_idle_timeout_seconds


def test_compaction_time_budget_defaults_allow_long_chain_work() -> None:
    gateway_config = GatewayConfig()
    compaction_config = CompactionConfig()

    assert gateway_config.compaction.timeout_seconds == 90.0
    assert compaction_config.timeout_seconds == 90.0


def test_gateway_home_uses_configured_state_parent(tmp_path: Path) -> None:
    config = GatewayConfig(
        state_dir=str(tmp_path / "instance" / "state"),
        workspace_dir=str(tmp_path / "instance" / "workspace"),
    )

    assert _gateway_home(config) == tmp_path / "instance"


def test_gateway_home_falls_back_to_config_path_parent(tmp_path: Path) -> None:
    config = GatewayConfig(
        state_dir=None,
        config_path=str(tmp_path / "service" / "config.toml"),
        workspace_dir=str(tmp_path / "service" / "workspace"),
    )

    assert _gateway_home(config) == tmp_path / "service"


class _FakeDreamScheduler:
    def __init__(self, jobs: list[CronJob] | None = None) -> None:
        self.jobs = jobs or []
        self.added: list[dict[str, Any]] = []
        self.paused: list[str] = []

    async def list_jobs(self) -> list[CronJob]:
        return self.jobs

    async def add_job(self, **kwargs: Any) -> None:
        self.added.append(kwargs)

    async def pause_job(self, job_id: str) -> None:
        self.paused.append(job_id)
        for job in self.jobs:
            if job.id == job_id:
                job.status = JobStatus.PAUSED


def test_build_turn_runner_from_services_wires_memory_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeTurnRunner:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    from agentos.gateway import boot

    monkeypatch.setattr("agentos.engine.runtime.TurnRunner", FakeTurnRunner)
    services = SimpleNamespace(
        provider_selector=object(),
        tool_registry=object(),
        session_manager=object(),
        skill_loader=object(),
        usage_tracker=object(),
        config=GatewayConfig(),
        memory_sync_managers={"main": object()},
        memory_retrievers={"main": object()},
        turn_capture_services={"main": object()},
        model_catalog=object(),
    )

    runner = boot.build_turn_runner_from_services(services)

    assert isinstance(runner, FakeTurnRunner)
    assert captured["memory_sync_managers"] is services.memory_sync_managers
    assert captured["memory_retrievers"] is services.memory_retrievers
    assert captured["turn_capture_services"] is services.turn_capture_services
    assert captured["model_catalog"] is services.model_catalog


def test_build_turn_runner_from_services_wires_diagnostics_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeTurnRunner:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("agentos.engine.runtime.TurnRunner", FakeTurnRunner)
    services = SimpleNamespace(
        provider_selector=object(),
        tool_registry=object(),
        session_manager=object(),
        skill_loader=object(),
        usage_tracker=object(),
        config=GatewayConfig(),
    )
    state = DiagnosticsState.from_config(GatewayConfig())

    from agentos.gateway import boot

    runner = boot.build_turn_runner_from_services(services, diagnostics_state=state)

    assert isinstance(runner, FakeTurnRunner)
    assert captured["diagnostics_state"] is state


@pytest.mark.asyncio
async def test_start_gateway_server_shares_diagnostics_state_between_app_and_turn_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_runner: dict[str, Any] = {}

    class FakeTurnRunner:
        def __init__(self, **kwargs: Any) -> None:
            captured_runner.update(kwargs)

        def set_session_lock_provider(self, provider: Any) -> None:
            captured_runner["session_lock_provider"] = provider

    async def fake_build_services(**kwargs: Any) -> Any:
        config = kwargs["config"]

        async def close() -> None:
            return None

        return SimpleNamespace(
            provider_selector=object(),
            tool_registry=object(),
            session_manager=object(),
            skill_loader=object(),
            usage_tracker=object(),
            config=config,
            memory_sync_managers={},
            model_catalog=None,
            memory_retrievers={},
            turn_capture_services={},
            cron_scheduler=None,
            task_runtime=None,
            agent_registry=None,
            memory_managers={},
            memory_stores={},
            _turn_runner_ref=[],
            close=close,
        )

    from agentos.gateway import boot

    monkeypatch.setattr("agentos.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr(boot, "build_services", fake_build_services)
    monkeypatch.setattr(boot, "_setup_file_logging", lambda config: None)
    monkeypatch.setattr(boot, "emit_skill_filter_banner", lambda config: None)
    monkeypatch.setattr(
        "agentos.gateway.pidlock.GatewayPidLock.acquire",
        lambda self: None,
    )
    monkeypatch.setattr(
        "agentos.gateway.pidlock.GatewayPidLock.release",
        lambda self: None,
    )
    config = GatewayConfig(
        state_dir=str(tmp_path / "state"),
        workspace_dir=str(tmp_path / "workspace"),
        control_ui={"enabled": False},
        channels={"channels": []},
        diagnostics_enabled=True,
    )

    server = await boot.start_gateway_server(config=config, run=False)

    try:
        state = server.app.state.diagnostics_state
        assert isinstance(state, DiagnosticsState)
        assert captured_runner["diagnostics_state"] is state
        state.set_runtime(enabled=True, raw=True)
        assert captured_runner["diagnostics_state"].raw_turn_call_enabled() is True
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_start_gateway_server_creates_default_subscription_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_bridge: dict[str, Any] = {}

    class FakeTurnRunner:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def set_session_lock_provider(self, _provider: Any) -> None:
            pass

    class FakeEventBridge:
        def __init__(self, *, subscription_manager: Any, connection_registry: Any) -> None:
            captured_bridge["subscription_manager"] = subscription_manager
            captured_bridge["connection_registry"] = connection_registry

        async def emit(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    async def fake_build_services(**kwargs: Any) -> Any:
        config = kwargs["config"]

        async def close() -> None:
            return None

        return SimpleNamespace(
            provider_selector=object(),
            tool_registry=object(),
            session_manager=object(),
            skill_loader=object(),
            usage_tracker=object(),
            config=config,
            memory_sync_managers={},
            model_catalog=None,
            memory_retrievers={},
            turn_capture_services={},
            cron_scheduler=None,
            task_runtime=None,
            agent_registry=None,
            memory_managers={},
            memory_stores={},
            _turn_runner_ref=[],
            close=close,
        )

    from agentos.gateway import boot
    from agentos.gateway.websocket import SubscriptionManager

    monkeypatch.setattr("agentos.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr("agentos.gateway.event_bridge.EventBridge", FakeEventBridge)
    monkeypatch.setattr(boot, "build_services", fake_build_services)
    monkeypatch.setattr(boot, "_setup_file_logging", lambda config: None)
    monkeypatch.setattr(boot, "emit_skill_filter_banner", lambda config: None)
    monkeypatch.setattr(
        "agentos.gateway.pidlock.GatewayPidLock.acquire",
        lambda self: None,
    )
    monkeypatch.setattr(
        "agentos.gateway.pidlock.GatewayPidLock.release",
        lambda self: None,
    )
    config = GatewayConfig(
        state_dir=str(tmp_path / "state"),
        workspace_dir=str(tmp_path / "workspace"),
        control_ui={"enabled": False},
        channels={"channels": []},
    )

    server = await boot.start_gateway_server(config=config, run=False)

    try:
        assert isinstance(captured_bridge["subscription_manager"], SubscriptionManager)
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_start_gateway_server_schedules_router_preload_after_channels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeTurnRunner:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def set_session_lock_provider(self, _provider: Any) -> None:
            pass

    class FakeChannelManager:
        async def start_all(self) -> dict[str, bool]:
            events.append("channels.start_all")
            return {"feishu": True}

        def start_errors(self) -> dict[str, dict[str, str]]:
            return {}

        async def stop_all(self) -> None:
            return None

    class FakeServer:
        def __init__(self, _config: Any) -> None:
            self.should_exit = False

        async def serve(self) -> None:
            return None

    async def fake_build_services(**kwargs: Any) -> Any:
        config = kwargs["config"]

        async def close() -> None:
            return None

        return SimpleNamespace(
            provider_selector=object(),
            tool_registry=object(),
            session_manager=object(),
            skill_loader=object(),
            usage_tracker=object(),
            config=config,
            memory_sync_managers={},
            model_catalog=None,
            memory_retrievers={},
            turn_capture_services={},
            cron_scheduler=None,
            task_runtime=None,
            agent_registry=None,
            memory_managers={},
            memory_stores={},
            _turn_runner_ref=[],
            close=close,
        )

    def fake_create_background_task(coro: Any) -> Any:
        code = getattr(coro, "cr_code", None)
        name = getattr(code, "co_name", "")
        if name == "preload_agentos_router_runtime":
            events.append("router.preload.scheduled")
        elif name == "serve":
            events.append("server.serve.scheduled")
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        return __import__("asyncio").create_task(__import__("asyncio").sleep(0))

    from agentos.gateway import boot

    monkeypatch.setattr("agentos.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr(boot, "build_services", fake_build_services)
    monkeypatch.setattr(boot, "_setup_file_logging", lambda config: None)
    monkeypatch.setattr(boot, "emit_skill_filter_banner", lambda config: None)
    monkeypatch.setattr(boot, "create_background_task", fake_create_background_task)
    monkeypatch.setattr(boot.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(
        "agentos.gateway.pidlock.GatewayPidLock.acquire",
        lambda self: None,
    )

    config = GatewayConfig(
        state_dir=str(tmp_path / "state"),
        workspace_dir=str(tmp_path / "workspace"),
        control_ui={"enabled": False},
        channels={"channels": []},
    )
    config.agentos_router.enabled = True

    server = await boot.start_gateway_server(
        config=config,
        channel_manager=FakeChannelManager(),
        run=True,
    )

    try:
        assert events.index("channels.start_all") < events.index("router.preload.scheduled")
    finally:
        await server.close()


def test_start_gateway_server_passes_tls_files_to_uvicorn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_config: dict[str, Any] = {}

    class FakeTurnRunner:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def set_session_lock_provider(self, _provider: Any) -> None:
            pass

    class FakeUvicornConfig:
        def __init__(self, **kwargs: Any) -> None:
            captured_config.update(kwargs)

    class FakeServer:
        def __init__(self, _config: Any) -> None:
            self.should_exit = False

        async def serve(self) -> None:
            return None

    async def fake_build_services(**kwargs: Any) -> Any:
        config = kwargs["config"]

        async def close() -> None:
            return None

        return SimpleNamespace(
            provider_selector=object(),
            tool_registry=object(),
            session_manager=object(),
            skill_loader=object(),
            usage_tracker=object(),
            config=config,
            memory_sync_managers={},
            model_catalog=None,
            memory_retrievers={},
            turn_capture_services={},
            cron_scheduler=None,
            task_runtime=None,
            agent_registry=None,
            memory_managers={},
            memory_stores={},
            _turn_runner_ref=[],
            close=close,
        )

    def fake_create_background_task(coro: Any) -> Any:
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        return asyncio.create_task(asyncio.sleep(0))

    from agentos.gateway import boot

    monkeypatch.setattr("agentos.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr(boot, "build_services", fake_build_services)
    monkeypatch.setattr(boot, "_setup_file_logging", lambda config: None)
    monkeypatch.setattr(boot, "emit_skill_filter_banner", lambda config: None)
    monkeypatch.setattr(boot, "create_background_task", fake_create_background_task)
    monkeypatch.setattr(boot.uvicorn, "Config", FakeUvicornConfig)
    monkeypatch.setattr(boot.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(
        "agentos.gateway.pidlock.GatewayPidLock.acquire",
        lambda self: None,
    )
    monkeypatch.setattr(
        "agentos.gateway.pidlock.GatewayPidLock.release",
        lambda self: None,
    )

    keyfile = str(tmp_path / "gateway.key")
    certfile = str(tmp_path / "gateway.crt")
    config = GatewayConfig(
        state_dir=str(tmp_path / "state"),
        workspace_dir=str(tmp_path / "workspace"),
        control_ui={"enabled": False},
        channels={"channels": []},
        tls={"keyfile": keyfile, "certfile": certfile},
    )

    async def run_case() -> None:
        server = await boot.start_gateway_server(config=config, run=True)

        try:
            assert captured_config["ssl_keyfile"] == keyfile
            assert captured_config["ssl_certfile"] == certfile
        finally:
            await server.close()

    asyncio.run(run_case())


@pytest.mark.asyncio
async def test_start_gateway_server_wires_cron_failure_dispatcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driver-level guard for the production cron failure-destination wire.

    When ``svc.cron_scheduler`` exists, boot must register
    ``DeliveryChain.dispatch_failure_alert`` as the global failure dispatcher
    in ``scheduler.jobs`` so failed cron runs reach the configured FD at
    runtime. Without this wire the dispatch plumbing is dead in production
    even though unit tests cover the hook directly.
    """
    captured: dict[str, Any] = {}

    class FakeTurnRunner:
        def __init__(self, **_kw: Any) -> None: ...

        def set_session_lock_provider(self, _provider: Any) -> None: ...

    class FakeCronScheduler:
        def __init__(self) -> None:
            self.registered: dict[str, Any] = {}

        def register_handler(self, key: str, fn: Any) -> None:
            self.registered[key] = fn

        async def list_jobs(self) -> list:
            return []

    cron_sched = FakeCronScheduler()

    async def fake_build_services(**kwargs: Any) -> Any:
        async def close() -> None:
            return None

        return SimpleNamespace(
            provider_selector=object(),
            tool_registry=object(),
            session_manager=None,
            skill_loader=object(),
            usage_tracker=object(),
            config=kwargs["config"],
            memory_sync_managers={},
            model_catalog=None,
            memory_retrievers={},
            turn_capture_services={},
            cron_scheduler=cron_sched,
            task_runtime=None,
            agent_registry=None,
            memory_managers={},
            memory_stores={},
            _turn_runner_ref=[],
            close=close,
        )

    from agentos.gateway import boot
    from agentos.scheduler import jobs as scheduler_jobs

    def _record_dispatcher(fn: Any) -> None:
        captured["dispatcher"] = fn

    monkeypatch.setattr("agentos.engine.runtime.TurnRunner", FakeTurnRunner)
    monkeypatch.setattr(boot, "build_services", fake_build_services)
    monkeypatch.setattr(boot, "_setup_file_logging", lambda config: None)
    monkeypatch.setattr(boot, "emit_skill_filter_banner", lambda config: None)
    monkeypatch.setattr(scheduler_jobs, "set_failure_dispatcher", _record_dispatcher)
    monkeypatch.setattr("agentos.gateway.pidlock.GatewayPidLock.acquire", lambda self: None)
    monkeypatch.setattr("agentos.gateway.pidlock.GatewayPidLock.release", lambda self: None)

    config = GatewayConfig(
        state_dir=str(tmp_path / "state"),
        workspace_dir=str(tmp_path / "workspace"),
        control_ui={"enabled": False},
        channels={"channels": []},
    )

    server = await boot.start_gateway_server(config=config, run=False)
    try:
        assert callable(captured.get("dispatcher")), (
            "set_failure_dispatcher was not called during boot — the cron "
            "failure-destination wire is missing from gateway/boot.py"
        )
        # The wire must register DeliveryChain.dispatch_failure_alert
        # (a bound method), not some unrelated callable.
        assert getattr(captured["dispatcher"], "__name__", "") == "dispatch_failure_alert"
        # Handler factories ran, confirming the wire ran inside the cron-init
        # branch (not just by coincidence).
        assert set(cron_sched.registered) >= {
            "agent_run",
            "static_message",
            "system_event",
        }
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_build_services_registers_session_search_tool(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "agentos.sandbox.integration.configure_runtime",
        lambda *args, **kwargs: SimpleNamespace(effective=SimpleNamespace(as_dict=lambda: {})),
    )

    captured_memory_kwargs: dict[str, Any] = {}

    async def fake_build_memory_managers(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        captured_memory_kwargs.update(_kwargs)
        return {}

    monkeypatch.setattr(
        "agentos.memory.manager.build_memory_managers",
        fake_build_memory_managers,
    )
    registry = ToolRegistry()
    config = GatewayConfig(
        state_dir=str(tmp_path / "state"),
        workspace_dir=str(tmp_path / "workspace"),
        control_ui={"enabled": False},
        channels={"channels": []},
        mcp={"enabled": False},
    )

    services = await build_services(
        config=config,
        tool_registry=registry,
        session_db_path=str(tmp_path / "sessions.sqlite"),
    )
    try:
        session_search = registry.get("session_search")
        assert session_search is not None
        assert "Full-text search across persisted session transcripts" in (
            session_search.spec.description
        )
        assert "defaults to curated memory source files" in (session_search.spec.description)
        assert "use source=sessions or source=all" in session_search.spec.description
        default_names = {
            tool["name"] for tool in await registry.list_tools(caller_kind=CallerKind.AGENT)
        }
        configured_names = {
            tool.name
            for tool in registry.to_tool_definitions(
                ToolContext(
                    caller_kind=CallerKind.AGENT,
                    surfaced_tools={"session_search"},
                )
            )
        }
        channel_names = {
            tool.name
            for tool in registry.to_tool_definitions(ToolContext(caller_kind=CallerKind.CHANNEL))
        }
        assert session_search.spec.exposed_by_default is False
        assert "session_search" not in default_names
        assert "session_search" in configured_names
        assert "session_search" not in channel_names

        await services.session_manager.create("agent:main:main")
        await services.session_manager.append_message(
            "agent:main:main",
            "user",
            "needle transcript detail",
        )

        output = await session_search.handler(query="needle", limit=5)

        assert "needle" in output
        assert "agent:main:main" in output
        assert captured_memory_kwargs["session_storage"] is services.session_manager.storage
    finally:
        await services.close()


def test_router_boot_validation_logs_resolved_judge_for_llm_judge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    infos: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "agentos.gateway.boot.log.info",
        lambda event, **kwargs: infos.append({"event": event, **kwargs}),
    )
    config = GatewayConfig()
    # Default strategy is now v4_phase3 (local ML router); select the judge
    # explicitly so boot validation resolves and logs the judge target.
    config.agentos_router.strategy = "llm_judge"

    validate_agentos_router_runtime(config)

    resolved = [record for record in infos if record["event"] == "router.judge_resolved"]
    assert resolved
    assert resolved[0]["model"]
    assert resolved[0]["source"] == "auto"


def test_router_boot_validation_warns_when_judge_unresolvable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "agentos.gateway.boot.log.warning",
        lambda event, **kwargs: warnings.append({"event": event, **kwargs}),
    )
    config = GatewayConfig()
    # Default strategy is now v4_phase3; select the judge so this exercises the
    # judge-resolution path (v4 validation checks the bundle, not the judge).
    config.agentos_router.strategy = "llm_judge"
    config.agentos_router.tiers = {}
    # No tiers to route to → router step is a no-op, but boot validation
    # must degrade to a warning instead of blocking startup.
    validate_agentos_router_runtime(config)

    assert any(record["event"] == "router.judge_unresolved" for record in warnings)


def test_router_boot_validation_warns_when_judge_provider_lacks_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Findings #2/#4: an AUTO judge that resolves (from a tier's own provider
    field) to a provider different from llm.provider has no credential source
    and degrades to judge_unavailable every turn. Boot must warn
    (router.judge_no_credentials) rather than log a healthy resolution."""
    infos: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "agentos.gateway.boot.log.info",
        lambda event, **kwargs: infos.append({"event": event, **kwargs}),
    )
    monkeypatch.setattr(
        "agentos.gateway.boot.log.warning",
        lambda event, **kwargs: warnings.append({"event": event, **kwargs}),
    )
    # llm.provider not a router tier profile id → default openrouter tiers
    # are kept, so the AUTO judge resolves cross-provider.
    config = GatewayConfig(llm={"provider": "anthropic", "model": "claude-x", "api_key": "sk"})
    # Default strategy is now v4_phase3; select the judge so this exercises the
    # cross-provider judge-credential check rather than v4 bundle validation.
    config.agentos_router.strategy = "llm_judge"
    assert config.agentos_router.tiers["c0"]["provider"] == "openrouter"

    validate_agentos_router_runtime(config)

    assert any(record["event"] == "router.judge_no_credentials" for record in warnings)
    assert not any(record["event"] == "router.judge_resolved" for record in infos)


def test_router_boot_validation_warns_on_missing_pilot_bundle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # When the pilot bundle is missing, boot validation only warns
    # (build_services.agentos_router_bundle_missing) and never raises, because
    # require_router_runtime defaults False — routing degrades to the default
    # tier at runtime instead of blocking startup. A nonexistent
    # pilot_artifact_dir forces the missing-bundle path deterministically.
    warnings: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "agentos.gateway.boot.log.warning",
        lambda event, **kwargs: warnings.append({"event": event, **kwargs}),
    )
    config = GatewayConfig()
    config.agentos_router.strategy = "pilot-v1"
    config.agentos_router.pilot.pilot_artifact_dir = str(tmp_path / "does-not-exist")

    validate_agentos_router_runtime(config)

    assert any(
        record["event"] == "build_services.agentos_router_bundle_missing" for record in warnings
    )


def test_router_boot_validation_raises_on_missing_pilot_bundle_when_required(
    tmp_path: Path,
) -> None:
    # With require_router_runtime=True a missing pilot bundle is fatal: boot
    # validation raises so the operator cannot silently run degraded.
    config = GatewayConfig()
    config.agentos_router.strategy = "pilot-v1"
    config.agentos_router.pilot.pilot_artifact_dir = str(tmp_path / "does-not-exist")
    config.agentos_router.require_router_runtime = True

    with pytest.raises(RuntimeError, match="pilot-v1 router assets"):
        validate_agentos_router_runtime(config)


@pytest.mark.asyncio
async def test_preload_router_runtime_does_not_resolve_judge_for_pilot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registry-driven gate: pilot-v1's ``uses_judge`` is False, so preload
    must not resolve/log a judge target for it. Regression for the finding
    where boot preload compared ``strategy_name != "v4_phase3"`` instead of
    consulting the strategy registry, treating pilot-v1 as a judge strategy."""
    infos: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "agentos.gateway.boot.log.info",
        lambda event, **kwargs: infos.append({"event": event, **kwargs}),
    )
    monkeypatch.setattr(
        "agentos.gateway.boot.log.warning",
        lambda event, **kwargs: warnings.append({"event": event, **kwargs}),
    )
    monkeypatch.setattr(
        "agentos.gateway.boot._preload_agentos_router_strategy",
        lambda router_cfg, llm_cfg=None: object(),
    )
    config = GatewayConfig()
    config.agentos_router.enabled = True
    config.agentos_router.strategy = "pilot-v1"

    await preload_agentos_router_runtime(config)

    assert not any(record["event"] == "router.judge_resolved" for record in infos)
    assert not any(
        record["event"] in {"router.judge_unresolved", "router.judge_no_credentials"}
        for record in warnings
    )


@pytest.mark.asyncio
async def test_preload_router_runtime_still_resolves_judge_for_llm_judge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sibling case: llm_judge's ``uses_judge`` is True, so preload must still
    resolve and log the judge target for it."""
    infos: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "agentos.gateway.boot.log.info",
        lambda event, **kwargs: infos.append({"event": event, **kwargs}),
    )
    monkeypatch.setattr(
        "agentos.gateway.boot._preload_agentos_router_strategy",
        lambda router_cfg, llm_cfg=None: object(),
    )
    config = GatewayConfig()
    config.agentos_router.enabled = True
    config.agentos_router.strategy = "llm_judge"

    await preload_agentos_router_runtime(config)

    resolved = [record for record in infos if record["event"] == "router.judge_resolved"]
    assert resolved
    assert resolved[0]["model"]


def test_skill_filter_banner_accepts_tokenizers_without_transformers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from agentos.memory.embedding import LocalEmbeddingProvider

    def fake_find_spec(name: str):
        if name in {"onnxruntime", "tokenizers"}:
            return object()
        if name == "transformers":
            return None
        raise AssertionError(name)

    monkeypatch.setattr("importlib.util.find_spec", fake_find_spec)
    monkeypatch.setattr(
        LocalEmbeddingProvider,
        "_bundled_onnx_dir",
        classmethod(lambda cls, model_name: tmp_path),
    )

    emit_skill_filter_banner(
        SimpleNamespace(filter_enabled=True, filter_strategy="semantic", filter_embedding_model="")
    )

    assert "ONNX embedding backend not available" not in caplog.text


@pytest.mark.asyncio
async def test_build_services_fails_fast_for_explicit_remote_memory_without_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "agentos.sandbox.integration.configure_runtime",
        lambda *args, **kwargs: SimpleNamespace(effective=SimpleNamespace(as_dict=lambda: {})),
    )
    config = GatewayConfig(
        state_dir=str(tmp_path / "state"),
        workspace_dir=str(tmp_path / "workspace"),
        memory={"embedding": {"provider": "openai"}},
    )

    with pytest.raises(ValueError, match="memory.embedding.remote.api_key"):
        await build_services(config=config)


def test_configured_agent_ids_include_enabled_registry_agents_and_channels() -> None:
    result = upsert_channel(
        GatewayConfig(
            agents=[
                AgentEntryConfig(id="ops"),
                AgentEntryConfig(id="disabled", enabled=False),
            ]
        ),
        entry_payload={
            "type": "slack",
            "name": "work",
            "token": "x",
            "signing_secret": "ss",
            "agent_id": "channel",
        },
    )

    assert _configured_agent_ids(result.config) == ["channel", "main", "ops"]


def test_workspace_state_mismatch_emits_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[dict[str, Any]] = []
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(tmp_path / "gateway-3"))
    monkeypatch.setenv(
        "AGENTOS_GATEWAY_CONFIG_PATH",
        str(tmp_path / "gateway-3" / "config.toml"),
    )
    monkeypatch.setattr(
        "agentos.gateway.boot.log.warning",
        lambda event, **kwargs: warnings.append({"event": event, **kwargs}),
    )
    config = GatewayConfig(
        state_dir=str(tmp_path / "gateway-3" / "state"),
        workspace_dir=str(tmp_path / "gateway-1" / "workspace"),
        config_path=str(tmp_path / "gateway-3" / "config.toml"),
    )

    _warn_workspace_state_mismatch(config)

    assert warnings
    assert warnings[0]["event"] == "build_services.workspace_state_mismatch"
    assert "AGENTOS_STATE_DIR" in warnings[0]["expected_roots"]


def test_workspace_under_temp_dir_emits_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A temp workspace makes every ordinary write "outside the workspace".

    The log line is the evidence a user report of an out-of-workspace
    approval naming ``.../T/tmpXXXX/ws`` was missing.
    """
    warnings: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "agentos.gateway.boot.log.warning",
        lambda event, **kwargs: warnings.append({"event": event, **kwargs}),
    )
    monkeypatch.setattr("agentos.gateway.boot.tempfile.gettempdir", lambda: str(tmp_path))

    _warn_temporary_workspace(GatewayConfig(workspace_dir=str(tmp_path / "tmpabc123" / "ws")))
    assert [w["event"] for w in warnings] == ["build_services.workspace_in_temp_dir"]
    assert warnings[0]["workspace"] == str((tmp_path / "tmpabc123" / "ws").resolve())

    warnings.clear()
    _warn_temporary_workspace(GatewayConfig(workspace_dir=str(tmp_path.parent / "workspace")))
    _warn_temporary_workspace(GatewayConfig(workspace_dir=None))
    assert warnings == []


async def test_task_runtime_turn_uses_agent_registry_model_when_session_has_no_model() -> None:
    class RecordingTurnRunner:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def run(self, message: str, session_key: str, **kwargs: Any):
            self.calls.append(kwargs)
            yield DoneEvent()

    class SessionManager:
        async def get_session(self, session_key: str) -> Any:
            return SimpleNamespace(model=None)

    events: list[tuple[str, str, dict[str, Any]]] = []

    async def emit(session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        events.append((session_key, event_name, payload))

    config = GatewayConfig(
        agents=[AgentEntryConfig(id="ops", model="agent/default")],
        agent_stream_heartbeat_interval_seconds=0.0,
        agent_stream_idle_timeout_seconds=1.0,
    )
    run = SimpleNamespace(
        agent_id="ops",
        task_id="task-1",
        session_key="agent:ops:task-runtime",
        message="hello",
        envelope=build_cli_route_envelope(
            session_key="agent:ops:task-runtime",
            agent_id="ops",
        ),
        attachments=[],
        input_provenance={},
        run_kind="interactive",
        no_memory_capture=False,
        ingress_pipeline_steps=[],
        semantic_message=None,
        stream_event_sink=None,
    )
    runner = RecordingTurnRunner()

    await dispatch_task_runtime_turn(
        run,
        config=config,
        session_manager=SessionManager(),
        turn_runner=runner,
        event_emitter=emit,
    )

    assert runner.calls[0]["model"] == "agent/default"


@pytest.mark.asyncio
async def test_task_runtime_turn_applies_cron_job_tool_policy() -> None:
    class RecordingTurnRunner:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def run(self, message: str, session_key: str, **kwargs: Any):
            self.calls.append(kwargs)
            yield DoneEvent()

    events: list[tuple[str, str, dict[str, Any]]] = []

    async def emit(session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        events.append((session_key, event_name, payload))

    job = CronJob(
        id="cron-policy",
        name="Policy",
        payload={"kind": "agent_turn", "agent_id": "ops"},
        tool_policy={
            "profile": "minimal",
            "also_allow": ["memory_search", "exec_command"],
            "deny": ["web_fetch"],
        },
    )
    run = SimpleNamespace(
        agent_id="ops",
        task_id="task-1",
        session_key="cron:cron-policy:run:1",
        message="hello",
        envelope=build_cron_route_envelope(
            job,
            session_key="cron:cron-policy:run:1",
            agent_id="ops",
        ),
        attachments=[],
        input_provenance={},
        run_kind="cron_turn",
        no_memory_capture=False,
        ingress_pipeline_steps=[],
        semantic_message=None,
        stream_event_sink=None,
    )
    runner = RecordingTurnRunner()

    await dispatch_task_runtime_turn(
        run,
        config=GatewayConfig(),
        session_manager=None,
        turn_runner=runner,
        event_emitter=emit,
    )

    tool_context = runner.calls[0]["tool_context"]
    assert tool_context.allowed_tools == {"session_status"}
    assert "exec_command" in tool_context.denied_tools
    assert "web_fetch" in tool_context.denied_tools


@pytest.mark.asyncio
async def test_task_runtime_turn_keeps_cron_tool_boundary() -> None:
    class RecordingTurnRunner:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def run(self, message: str, session_key: str, **kwargs: Any):
            self.calls.append(kwargs)
            yield DoneEvent()

    async def emit(session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        return None

    job = CronJob(
        id="cron-restricted",
        name="Restricted",
        payload={"kind": "agent_turn", "agent_id": "ops"},
        tool_policy={
            "profile": "minimal",
            "also_allow": ["memory_search", "exec_command"],
            "deny": ["web_fetch"],
        },
    )
    run = SimpleNamespace(
        agent_id="ops",
        task_id="task-1",
        session_key="cron:cron-owner:run:1",
        message="hello",
        envelope=build_cron_route_envelope(
            job,
            session_key="cron:cron-owner:run:1",
            agent_id="ops",
        ),
        attachments=[],
        input_provenance={},
        run_kind="cron_turn",
        no_memory_capture=False,
        ingress_pipeline_steps=[],
        semantic_message=None,
        stream_event_sink=None,
    )
    runner = RecordingTurnRunner()

    await dispatch_task_runtime_turn(
        run,
        config=GatewayConfig(),
        session_manager=None,
        turn_runner=runner,
        event_emitter=emit,
    )

    tool_context = runner.calls[0]["tool_context"]
    assert tool_context.allowed_tools == {"session_status"}
    assert tool_context.tool_policy == job.tool_policy
    assert "exec_command" in tool_context.denied_tools


@pytest.mark.asyncio
async def test_task_runtime_turn_honours_cron_job_elevation() -> None:
    """The task-runtime path — not just the legacy fallback — must carry the
    opt-in through to the ToolContext the turn actually runs with."""

    class RecordingTurnRunner:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def run(self, message: str, session_key: str, **kwargs: Any):
            self.calls.append(kwargs)
            yield DoneEvent()

    async def emit(session_key: str, event_name: str, payload: dict[str, Any]) -> None:
        return None

    job = CronJob(
        id="cron-elevated",
        name="Elevated",
        payload={"kind": "agent_turn", "agent_id": "ops"},
        tool_policy={"elevated": "bypass"},
    )
    run = SimpleNamespace(
        agent_id="ops",
        task_id="task-1",
        session_key="cron:cron-elevated:run:1",
        message="hello",
        envelope=build_cron_route_envelope(
            job,
            session_key="cron:cron-elevated:run:1",
            agent_id="ops",
        ),
        attachments=[],
        input_provenance={},
        run_kind="cron_turn",
        no_memory_capture=False,
        ingress_pipeline_steps=[],
        semantic_message=None,
        stream_event_sink=None,
    )
    runner = RecordingTurnRunner()

    await dispatch_task_runtime_turn(
        run,
        config=GatewayConfig(),
        session_manager=None,
        turn_runner=runner,
        event_emitter=emit,
    )

    tool_context = runner.calls[0]["tool_context"]
    assert tool_context.elevated == "bypass"
    assert "exec_command" in (tool_context.allowed_tools or set())
    assert "cron" in tool_context.denied_tools


@pytest.mark.asyncio
async def test_start_gateway_server_wires_hooks(tmp_path) -> None:
    from agentos.engine.hooks import NoopCompactionHook, NoopToolHook, NoopTurnHook
    from agentos.gateway.boot import start_gateway_server

    th = NoopTurnHook()
    ch = NoopCompactionHook()
    toh = NoopToolHook()

    config = GatewayConfig(
        state_dir=str(tmp_path / "state"),
        workspace_dir=str(tmp_path / "workspace"),
        control_ui={"enabled": False},
        channels={"channels": []},
        mcp={"enabled": False},
    )

    server = await start_gateway_server(
        config=config,
        run=False,
        turn_hooks=[th],
        compaction_hooks=[ch],
        tool_hooks=[toh],
    )
    try:
        svc = server._services
        assert svc is not None
        assert svc.turn_hooks == [th]
        assert svc.compaction_hooks == [ch]
        assert svc.tool_hooks == [toh]

        assert hasattr(svc, "_turn_runner_ref")
        runner = svc._turn_runner_ref[0]
        assert th in runner._turn_hooks
        assert runner._compaction_hooks == (ch,)
        assert runner._tool_hooks == (toh,)
    finally:
        await server.close()
