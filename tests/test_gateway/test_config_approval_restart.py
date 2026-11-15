"""WAL-02: an agent can neither enable chat restart nor promote held edits by restarting.

The audit's chain: an agent-bound (or unbound) Control connection set
``gateway_control.chat_restart = true`` through ``config.set`` (the root was not
operator-only), then called ``gateway.restart`` itself; the restarted gateway
read the whole config file as the operator's, so an agent-planted ``[trading]``
edit the watcher had held became the running guardrail config.

Fixed in two places, both tested here: ``gateway_control`` is refused to every
agent binding, and boot runs operator-only settings on the values the operator
last approved (``gateway.config_approval``), so a restart -- however it is
triggered -- applies no held edit.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import tomli_w

from agentos.gateway.access import ConnectionSurface
from agentos.gateway.agent_surface import AgentBinding
from agentos.gateway.auth import AccessContext
from agentos.gateway.config import GatewayConfig
from agentos.gateway.config_approval import (
    approval_path,
    enforce_approval,
    load_approval,
    record_write,
)
from agentos.gateway.config_commit import reload_config_from_disk
from agentos.gateway.config_persist import set_runtime_overrides
from agentos.gateway.rpc import RpcContext, get_dispatcher

PLANTED = {
    "approval_threshold_usd": 1_000_000_000.0,
    "daily_cap_usd": 1_000_000_000.0,
    "agent_max_price_impact_pct": 100.0,
}


@pytest.fixture(autouse=True)
def _no_runtime_overrides() -> Any:
    set_runtime_overrides({})
    yield
    set_runtime_overrides({})


def _write(path: Path, data: dict[str, Any]) -> None:
    path.write_text(tomli_w.dumps(data), encoding="utf-8")


def _read(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _edit(path: Path, section: str, **values: Any) -> None:
    data = _read(path)
    data.setdefault(section, {}).update(values)
    _write(path, data)


def _boot(path: Path) -> tuple[GatewayConfig, list[str]]:
    """What a (re)started gateway runs on: load the file, then enforce approval."""
    config = GatewayConfig.load(path)
    return config, enforce_approval(config)


def _first_boot(tmp_path: Path) -> Path:
    path = tmp_path / "agentos.toml"
    base = GatewayConfig(state_dir=str(tmp_path / "state")).to_toml_dict()
    base["trading"] = {"approval_threshold_usd": 100.0, "daily_cap_usd": 1000.0}
    _write(path, base)
    config, held = _boot(path)
    assert held == []
    assert approval_path().is_file(), "the first boot adopts the file as the approval"
    assert config.trading.daily_cap_usd == 1000.0
    return path


def _ctx(config: GatewayConfig, via: str | None) -> RpcContext:
    access = AccessContext(
        surface=ConnectionSurface.CONTROL,
        admitted=True,
        credential_verified=via is None,
        agent=AgentBinding(via=via) if via else None,
    )
    return RpcContext(conn_id="c", access=access, config=config)


VIAS = ("token", "process", "window", "unbound")


class TestChatRestartIsTheOperators:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("via", VIAS)
    async def test_agent_cannot_enable_chat_restart(self, tmp_path: Path, via: str) -> None:
        path = _first_boot(tmp_path)
        running, _ = _boot(path)
        dispatcher = get_dispatcher()
        attempts: list[tuple[str, dict[str, Any]]] = [
            ("config.set", {"path": "gateway_control.chat_restart", "value": True}),
            ("config.patch", {"patches": {"gateway_control.chat_restart": True}}),
            ("config.patch", {"patch": {"gateway_control": {"chat_restart": True}}}),
        ]
        for method, params in attempts:
            res = await dispatcher.dispatch("r", method, params, _ctx(running, via))
            assert not res.ok, (method, params)
            assert running.gateway_control.chat_restart is False
        assert _read(path).get("gateway_control", {}).get("chat_restart", False) is False

        # With the toggle still off the agent's restart is refused, as before.
        res = await dispatcher.dispatch(
            "r", "gateway.restart", {"mode": "safe"}, _ctx(running, via)
        )
        assert not res.ok and res.error is not None and res.error.code == "UNAUTHORIZED"

    @pytest.mark.asyncio
    async def test_operator_may_enable_chat_restart(self, tmp_path: Path) -> None:
        path = _first_boot(tmp_path)
        running, _ = _boot(path)
        res = await get_dispatcher().dispatch(
            "r",
            "config.set",
            {"path": "gateway_control.chat_restart", "value": True},
            _ctx(running, None),
        )
        assert res.ok, res.error
        assert running.gateway_control.chat_restart is True
        # The operator's write is the approval: the next boot keeps it.
        restarted, held = _boot(path)
        assert held == [] and restarted.gateway_control.chat_restart is True


class TestRestartDoesNotApplyHeldEdits:
    def test_a_planted_trading_edit_stays_held_across_restarts(self, tmp_path: Path) -> None:
        path = _first_boot(tmp_path)
        # An agent's shell edits the file (OS sandbox off, same user).
        _edit(path, "trading", **PLANTED)
        _edit(path, "gateway_control", chat_restart=True)
        _edit(path, "updates", notify=False)
        for _ in range(2):  # every restart, not only the first
            config, held = _boot(path)
            assert "trading" in held and "gateway_control" in held
            assert config.trading.daily_cap_usd == 1000.0
            assert config.trading.approval_threshold_usd == 100.0
            assert config.gateway_control.chat_restart is False
            # Settings that are not the operator's alone apply as always.
            assert config.updates.notify is False

    def test_the_operators_reload_approves_the_file(self, tmp_path: Path) -> None:
        path = _first_boot(tmp_path)
        running, _ = _boot(path)
        _edit(path, "trading", daily_cap_usd=250.0)
        ctx = SimpleNamespace(config=running, provider_selector=None)
        # The watcher (or an agent's reload) holds it ...
        assert reload_config_from_disk(ctx).held_paths == ("trading.daily_cap_usd",)
        assert _boot(path)[0].trading.daily_cap_usd == 1000.0
        # ... the operator's ``agentos config reload`` applies and approves it.
        reload_config_from_disk(ctx, include_operator_only=True)
        assert running.trading.daily_cap_usd == 250.0
        config, held = _boot(path)
        assert held == [] and config.trading.daily_cap_usd == 250.0

    def test_an_unreadable_approval_refuses_to_boot(self, tmp_path: Path) -> None:
        from agentos.gateway.config import GatewayConfigError

        path = _first_boot(tmp_path)
        approval_path().write_text("not = [toml", encoding="utf-8")
        with pytest.raises(GatewayConfigError):
            _boot(path)


class TestOfflineWriters:
    """CLI/onboarding writes while no gateway owns the state directory."""

    def test_an_operator_cli_write_is_approved(self, tmp_path: Path) -> None:
        path = _first_boot(tmp_path)
        before = _read(path)
        after = _read(path)
        after["trading"]["daily_cap_usd"] = 500.0
        _write(path, after)
        assert record_write(before, after, path) == []
        config, held = _boot(path)
        assert held == [] and config.trading.daily_cap_usd == 500.0

    def test_rewriting_the_file_does_not_launder_a_planted_edit(self, tmp_path: Path) -> None:
        path = _first_boot(tmp_path)
        _edit(path, "trading", **PLANTED)
        before = _read(path)
        # The operator changes something else; the writer re-serializes the
        # whole file, planted values included.
        after = _read(path)
        after.setdefault("updates", {})["notify"] = False
        _write(path, after)
        assert "trading" in record_write(before, after, path)
        config, held = _boot(path)
        assert "trading" in held and config.trading.daily_cap_usd == 1000.0

    def test_an_agent_session_does_not_approve(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentos.gateway.agent_surface import AGENT_TOKEN_ENV

        path = _first_boot(tmp_path)
        approved = load_approval()
        before = _read(path)
        after = _read(path)
        after["trading"]["daily_cap_usd"] = 9_999_999.0
        _write(path, after)
        monkeypatch.setenv(AGENT_TOKEN_ENV, "agent-token")
        assert record_write(before, after, path) == ["trading"]
        assert load_approval() == approved
        assert _boot(path)[0].trading.daily_cap_usd == 1000.0

    def test_a_running_gateway_means_no_offline_approval(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentos.gateway import config_approval

        path = _first_boot(tmp_path)
        before = _read(path)
        after = _read(path)
        after["trading"]["daily_cap_usd"] = 9_999_999.0
        _write(path, after)
        monkeypatch.setattr(config_approval, "owning_gateway_pid", lambda _p: 4242)
        assert record_write(before, after, path) == ["trading"]
        assert _boot(path)[0].trading.daily_cap_usd == 1000.0

    def test_an_invalid_previous_file_approves_nothing(self, tmp_path: Path) -> None:
        path = _first_boot(tmp_path)
        after = _read(path)
        after["trading"]["daily_cap_usd"] = 500.0
        _write(path, after)
        assert record_write({"trading": {"daily_cap_usd": -1}}, after, path) == ["trading"]
