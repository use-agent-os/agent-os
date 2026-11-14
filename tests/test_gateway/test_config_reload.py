"""Config file hot reload (#3697): reload_config_from_disk and the watcher."""

from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import tomli_w

from agentos.gateway.config import GatewayConfig
from agentos.gateway.config_commit import (
    ConfigReloadError,
    commit_config,
    pending_restart_metadata,
    reload_config_from_disk,
)
from agentos.gateway.config_persist import set_runtime_overrides
from agentos.gateway.config_watcher import ConfigWatcher, last_reload_status


@pytest.fixture(autouse=True)
def _no_runtime_overrides() -> Any:
    set_runtime_overrides({})
    yield
    set_runtime_overrides({})


def _write(path: Path, config: GatewayConfig) -> None:
    path.write_text(tomli_w.dumps(config.to_toml_dict()), encoding="utf-8")


def _running(tmp_path: Path) -> tuple[Path, GatewayConfig]:
    path = tmp_path / "agentos.toml"
    _write(path, GatewayConfig(state_dir=str(tmp_path / "state")))
    return path, GatewayConfig.load(path)


def _edit(path: Path, **sections: dict[str, Any]) -> None:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    for name, values in sections.items():
        if isinstance(values, dict):
            data.setdefault(name, {}).update(values)
        else:
            data[name] = values
    path.write_text(tomli_w.dumps(data), encoding="utf-8")


def _ctx(config: GatewayConfig) -> SimpleNamespace:
    return SimpleNamespace(config=config, provider_selector=None)


class TestReloadFromDisk:
    def test_hot_and_restart_paths_are_classified(self, tmp_path: Path) -> None:
        path, running = _running(tmp_path)
        _edit(
            path,
            updates={"notify": False},
            memory={"retrieval_mode": "fts_only"},
        )
        result = reload_config_from_disk(_ctx(running))
        assert "updates.notify" in result.hot_paths
        assert "memory.retrieval_mode" in result.restart_paths
        assert "memory" in result.restart_reasons
        # Applied to the running object in place, restart bookkeeping recorded.
        assert running.updates.notify is False
        pending, reasons = pending_restart_metadata(running)
        assert pending is True
        assert "memory" in reasons

    def test_operator_reload_applies_trading_hot(self, tmp_path: Path) -> None:
        path, running = _running(tmp_path)
        _edit(path, trading={"daily_cap_usd": 50.0})
        result = reload_config_from_disk(_ctx(running), include_operator_only=True)
        assert result.hot_paths == ("trading.daily_cap_usd",)
        assert result.held_paths == ()
        assert running.trading.daily_cap_usd == 50.0

    def test_unaudited_key_waits_for_a_restart(self, tmp_path: Path) -> None:
        path, running = _running(tmp_path)
        _edit(path, sessions={"auto_title": False})
        result = reload_config_from_disk(_ctx(running))
        assert result.restart_paths == ("sessions.auto_title",)
        assert result.hot_paths == ()
        assert "config" in result.restart_reasons

    def test_hot_only_edit_needs_no_restart(self, tmp_path: Path) -> None:
        path, running = _running(tmp_path)
        _edit(path, hooks={"rate_limit_per_minute": 10})
        result = reload_config_from_disk(_ctx(running), include_operator_only=True)
        assert result.hot_paths == ("hooks.rate_limit_per_minute",)
        assert result.restart_reasons == ()
        assert pending_restart_metadata(running) == (False, [])

    def test_invalid_toml_keeps_the_running_config(self, tmp_path: Path) -> None:
        path, running = _running(tmp_path)
        before = running.model_dump()
        path.write_text("this is = = not toml", encoding="utf-8")
        with pytest.raises(ConfigReloadError):
            reload_config_from_disk(_ctx(running))
        assert running.model_dump() == before

    def test_invalid_value_names_the_key(self, tmp_path: Path) -> None:
        path, running = _running(tmp_path)
        _edit(path, gateway={"bind": "everywhere"})
        with pytest.raises(ConfigReloadError) as excinfo:
            reload_config_from_disk(_ctx(running))
        assert "gateway.bind" in str(excinfo.value)
        assert running.gateway.bind is None

    def test_unknown_key_is_refused(self, tmp_path: Path) -> None:
        path, running = _running(tmp_path)
        _edit(path, trading={"no_such_setting": 1})
        with pytest.raises(ConfigReloadError):
            reload_config_from_disk(_ctx(running))

    def test_cli_overrides_keep_winning(self, tmp_path: Path) -> None:
        path, running = _running(tmp_path)
        set_runtime_overrides({"port": None, "host": None, "debug": None})
        running.port = 18804  # what --port set at boot
        _edit(path, trading={"daily_cap_usd": 75.0})
        result = reload_config_from_disk(_ctx(running))
        assert running.port == 18804
        assert "port" not in result.changed_paths

    def test_runtime_only_secret_survives(self, tmp_path: Path) -> None:
        path, running = _running(tmp_path)
        running.auth = running.auth.model_copy(update={"mode": "token", "token": "boot-token"})
        running.mark_runtime_secret("auth.token")
        _edit(path, auth={"mode": "token"}, trading={"daily_cap_usd": 60.0})
        reload_config_from_disk(_ctx(running))
        assert running.auth.token == "boot-token"

    def test_unsafe_public_bind_is_refused(self, tmp_path: Path) -> None:
        path, running = _running(tmp_path)
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        data["host"] = "0.0.0.0"
        data.setdefault("auth", {})["mode"] = "none"
        path.write_text(tomli_w.dumps(data), encoding="utf-8")
        with pytest.raises(ConfigReloadError, match="non-loopback"):
            reload_config_from_disk(_ctx(running))
        assert running.host == "127.0.0.1"

    def test_the_gateways_own_write_changes_nothing(self, tmp_path: Path) -> None:
        path, running = _running(tmp_path)
        updated = running.model_copy(deep=True)
        updated.trading.daily_cap_usd = 42.0
        commit_config(_ctx(running), updated, changed_paths={"trading.daily_cap_usd"})
        result = reload_config_from_disk(_ctx(running))
        assert result.changed is False


class _Sleeper:
    def __init__(self, on_sleep: Any = None) -> None:
        self.calls: list[float] = []
        self.on_sleep = on_sleep

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self.on_sleep is not None:
            self.on_sleep(len(self.calls))


class TestWatcher:
    @pytest.mark.asyncio
    async def test_edit_is_applied_and_announced(self, tmp_path: Path) -> None:
        path, running = _running(tmp_path)
        events: list[tuple[str, dict[str, Any]]] = []

        async def emit(event: str, payload: dict[str, Any]) -> None:
            events.append((event, payload))

        sleeper = _Sleeper()
        watcher = ConfigWatcher(lambda: _ctx(running), emit=emit, sleep=sleeper)
        watcher.start()
        await watcher.stop()  # baseline signature taken; no background loop needed
        _edit(path, updates={"notify": False}, memory={"retrieval_mode": "fts_only"})
        assert await watcher.poll_once() is True
        assert sleeper.calls == [0.5]  # one debounce, the file was already still
        assert running.updates.notify is False
        [(name, payload)] = events
        assert name == "config.changed"
        assert payload["status"] == "applied"
        assert payload["source"] == "file"
        assert "updates.notify" in payload["hot"]
        assert payload["held"] == []
        assert "memory.retrieval_mode" in payload["restartRequired"]
        assert payload["pendingRestart"] is True
        # Nothing changed since: no second reload.
        assert await watcher.poll_once() is False

    @pytest.mark.asyncio
    async def test_debounce_waits_for_the_writer_to_finish(self, tmp_path: Path) -> None:
        path, running = _running(tmp_path)

        def keep_writing(call: int) -> None:
            if call < 3:
                _edit(path, memory={"inject_limit": 10 + call})

        sleeper = _Sleeper(on_sleep=keep_writing)
        watcher = ConfigWatcher(lambda: _ctx(running), sleep=sleeper)
        watcher.start()
        await watcher.stop()
        _edit(path, memory={"inject_limit": 1})
        assert await watcher.poll_once() is True
        assert len(sleeper.calls) == 3
        assert running.memory.inject_limit == 12

    @pytest.mark.asyncio
    async def test_invalid_edit_is_skipped_and_reported(self, tmp_path: Path) -> None:
        from structlog.testing import capture_logs

        path, running = _running(tmp_path)
        events: list[dict[str, Any]] = []

        async def emit(_event: str, payload: dict[str, Any]) -> None:
            events.append(payload)

        watcher = ConfigWatcher(lambda: _ctx(running), emit=emit, sleep=_Sleeper())
        watcher.start()
        await watcher.stop()
        path.write_text("[broken", encoding="utf-8")
        with capture_logs() as logs:
            await watcher.poll_once()
        assert any(entry["event"] == "config.reload_skipped" for entry in logs)
        assert events[0]["status"] == "invalid"
        assert last_reload_status().status == "invalid"

    @pytest.mark.asyncio
    async def test_switched_off_without_a_restart(self, tmp_path: Path) -> None:
        path, running = _running(tmp_path)
        running.gateway.config_reload = False
        watcher = ConfigWatcher(lambda: _ctx(running), sleep=_Sleeper())
        watcher.start()
        await watcher.stop()
        _edit(path, trading={"daily_cap_usd": 5.0})
        assert await watcher.poll_once() is False
        assert running.trading.daily_cap_usd != 5.0


def test_doctor_reports_pending_restart_and_skipped_reload(tmp_path: Path) -> None:
    from agentos.gateway import config_watcher
    from agentos.gateway.config_commit import _record_pending_restart
    from agentos.gateway.rpc_doctor import _config_reload_findings

    config = GatewayConfig()
    _record_pending_restart(config, ["memory"])
    config_watcher._last_status = config_watcher.ReloadStatus(status="invalid", error="x: bad")
    try:
        findings = {f.id: f for f in _config_reload_findings(_ctx(config))}  # type: ignore[arg-type]
    finally:
        config_watcher._last_status = config_watcher.ReloadStatus()
    assert findings["gateway.config.restart_pending"].restart_required is True
    assert findings["gateway.config.reload_skipped"].detail == "x: bad"


@pytest.mark.asyncio
async def test_fixing_an_invalid_file_is_announced(tmp_path: Path) -> None:
    path, running = _running(tmp_path)
    good = path.read_text(encoding="utf-8")
    events: list[dict[str, Any]] = []

    async def emit(_event: str, payload: dict[str, Any]) -> None:
        events.append(payload)

    watcher = ConfigWatcher(lambda: _ctx(running), emit=emit, sleep=_Sleeper())
    watcher.start()
    await watcher.stop()
    path.write_text("[broken", encoding="utf-8")
    await watcher.poll_once()
    path.write_text(good, encoding="utf-8")
    await watcher.poll_once()
    assert [e["status"] for e in events] == ["invalid", "recovered"]
    assert last_reload_status().status == "unchanged"


def test_doctor_wording_does_not_claim_old_values(tmp_path: Path) -> None:
    from agentos.gateway.config_commit import _record_pending_restart
    from agentos.gateway.rpc_doctor import _config_reload_findings

    config = GatewayConfig()
    _record_pending_restart(config, ["config"])
    [finding] = [
        f for f in _config_reload_findings(_ctx(config)) if f.id == "gateway.config.restart_pending"
    ]  # type: ignore[arg-type]
    assert "old values" not in finding.detail
    assert "read them only at startup" in finding.detail


class TestOperatorOnlySettingsAreHeld:
    """An agent's shell can write the file; the watcher must not make it the operator.

    The audit's payload rewrote the trading guardrails and planted a lifecycle
    hook with one heredoc; the watcher applied all four leaves hot.
    """

    _PAYLOAD = {
        "trading": {
            "approval_threshold_usd": 1_000_000.0,
            "daily_cap_usd": 1_000_000.0,
            "agent_max_price_impact_pct": 100.0,
        },
        "hooks": {"lifecycle": {"startup": ["touch HOOK_RAN"]}},
    }

    def _baseline(self, tmp_path: Path) -> tuple[Path, GatewayConfig]:
        path, running = _running(tmp_path)
        _edit(path, trading={"approval_threshold_usd": 100.0, "daily_cap_usd": 1000.0})
        reload_config_from_disk(_ctx(running), include_operator_only=True)
        assert running.trading.daily_cap_usd == 1000.0
        return path, running

    def test_agent_written_guardrails_are_held(self, tmp_path: Path) -> None:
        path, running = self._baseline(tmp_path)
        _edit(path, **self._PAYLOAD, updates={"notify": False})
        result = reload_config_from_disk(_ctx(running))
        assert set(result.held_paths) == {
            "hooks.lifecycle.startup",
            "trading.agent_max_price_impact_pct",
            "trading.approval_threshold_usd",
            "trading.daily_cap_usd",
        }
        # The running guardrails did not move; the harmless part still did.
        assert running.trading.approval_threshold_usd == 100.0
        assert running.trading.daily_cap_usd == 1000.0
        assert running.trading.agent_max_price_impact_pct != 100.0
        assert running.hooks.lifecycle.startup == []
        assert result.changed_paths == ("updates.notify",)
        assert running.updates.notify is False
        # Nothing held is reported as hot or pending a restart either.
        assert not set(result.held_paths) & set(result.hot_paths + result.restart_paths)

    @pytest.mark.parametrize(
        "section",
        [
            {"permissions": {"default_mode": "full"}},
            {"sandbox": {"extra_rw_mounts": ["/"]}},
            {"tools": {"invoke": {"allow": ["exec_command"]}}},
            {"tools": {"deny": []}, "safety": {"injection_scan_mode": "off"}},
            {"workspace_strict": False},
            {"acp": {"permission_mode": "approve-all"}},
        ],
    )
    def test_every_operator_only_root_is_held(self, tmp_path: Path, section: Any) -> None:
        path, running = _running(tmp_path)
        before = running.model_dump()
        _edit(path, **section)
        result = reload_config_from_disk(_ctx(running))
        assert result.held_paths
        assert running.model_dump() == before

    def test_the_trading_service_keeps_the_operators_limits(self, tmp_path: Path) -> None:
        """The audit's sink: get_trading_service re-binds to ctx.config.trading."""
        from agentos.trading.service import get_trading_service

        path, running = self._baseline(tmp_path)
        _edit(path, **self._PAYLOAD)
        reload_config_from_disk(_ctx(running))
        service = get_trading_service(running)
        assert service.config.daily_cap_usd == 1000.0
        assert service.config.approval_threshold_usd == 100.0

    @pytest.mark.asyncio
    async def test_watcher_reports_held_and_doctor_says_how_to_apply(self, tmp_path: Path) -> None:
        from agentos.gateway.rpc_doctor import _config_reload_findings

        path, running = self._baseline(tmp_path)
        events: list[dict[str, Any]] = []

        async def emit(_event: str, payload: dict[str, Any]) -> None:
            events.append(payload)

        watcher = ConfigWatcher(lambda: _ctx(running), emit=emit, sleep=_Sleeper())
        watcher.start()
        await watcher.stop()
        _edit(path, **self._PAYLOAD)
        assert await watcher.poll_once() is True
        [payload] = events
        assert payload["status"] == "held"
        assert "trading.daily_cap_usd" in payload["held"]
        assert payload["appliedNow"] is False
        assert last_reload_status().status == "held"
        assert running.trading.daily_cap_usd == 1000.0
        findings = {f.id: f for f in _config_reload_findings(_ctx(running))}
        held = findings["gateway.config.reload_held"]
        assert any(step.command == "agentos config reload" for step in held.fix_steps)

    @pytest.mark.asyncio
    async def test_config_reload_rpc_is_the_operators_alone(self, tmp_path: Path) -> None:
        from agentos.gateway.access import ConnectionSurface
        from agentos.gateway.agent_surface import AgentBinding
        from agentos.gateway.auth import AccessContext
        from agentos.gateway.rpc import RpcContext, get_dispatcher

        path, running = self._baseline(tmp_path)
        _edit(path, **self._PAYLOAD)
        dispatcher = get_dispatcher()
        for via in ("token", "process", "window", "unbound"):
            access = AccessContext(
                surface=ConnectionSurface.CONTROL,
                admitted=True,
                credential_verified=False,
                agent=AgentBinding(via=via),
            )
            ctx = RpcContext(conn_id="c", access=access, config=running)
            res = await dispatcher.dispatch("c", "config.reload", {}, ctx)
            assert not res.ok and res.error is not None
            assert res.error.code == "config.operator_required"
        assert running.trading.daily_cap_usd == 1000.0

        operator = AccessContext(
            surface=ConnectionSurface.CONTROL, admitted=True, credential_verified=False
        )
        res = await dispatcher.dispatch(
            "c", "config.reload", {}, RpcContext(conn_id="c", access=operator, config=running)
        )
        assert res.ok, res.error
        assert "trading.daily_cap_usd" in res.payload["changed"]
        assert running.trading.daily_cap_usd == 1_000_000.0
        assert running.hooks.lifecycle.startup == ["touch HOOK_RAN"]
        assert last_reload_status().held == []
