"""WAL-11: the engine's own loop enforces the [trading] settings that are live now.

A config write (``config.set`` / ``config.reload``) replaces the running
config's ``trading`` section with a new object and reports it applied without
a restart. The service used to hold the section object it was built with, so
the unattended loop -- DCA buys, trigger fires -- kept signing on the settings
from before the change until some wallet/trading RPC happened to re-bind it.
These tests change the settings through the real config paths with no trading
RPC in between and drive the loop body (``housekeep``) directly.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import tomli_w

from agentos.gateway.config import GatewayConfig
from agentos.gateway.config_commit import reload_config_from_disk
from agentos.gateway.config_persist import set_runtime_overrides
from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.trading.service import TradingError
from tests.test_trading import test_dca as _dca
from tests.test_trading.test_dca import HOUR, T0, World, _settle

# The DCA world (a funded wallet on a pinned clock) and its stable ids.
world = _dca.world
ids = _dca.ids


@pytest.fixture(autouse=True)
def _no_runtime_overrides() -> Any:
    set_runtime_overrides({})
    yield
    set_runtime_overrides({})


def _running(world: World) -> GatewayConfig:
    config = world.service._gateway_config
    assert isinstance(config, GatewayConfig)
    return config


def _on_disk(world: World, tmp_path: Path) -> Path:
    """Give the running config a file, as a gateway started from one has."""
    running = _running(world)
    path = tmp_path / "agentos.toml"
    running.config_path = str(path)
    path.write_text(tomli_w.dumps(running.to_toml_dict()), encoding="utf-8")
    return path


def _edit_trading(path: Path, **values: Any) -> None:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    data.setdefault("trading", {}).update(values)
    path.write_text(tomli_w.dumps(data), encoding="utf-8")


def _operator_reload(world: World) -> None:
    """The operator's ``agentos config reload`` (operator-only roots applied)."""
    ctx = SimpleNamespace(config=_running(world), provider_selector=None)
    result = reload_config_from_disk(ctx, include_operator_only=True)
    assert result.applied_now and not result.held_paths


async def _operator_set(world: World, path: str, value: Any) -> None:
    ctx = RpcContext(conn_id="operator", config=_running(world))
    res = await get_dispatcher().dispatch(
        "operator", "config.set", {"path": path, "value": value}, ctx
    )
    assert res.ok, res.error
    assert res.payload["restartRequired"] is False


async def _loop_tick(world: World, advance: float) -> None:
    world.clock.now += advance
    await world.service.housekeep()
    await _settle(world.service)


async def test_reload_switching_trading_off_stops_the_next_dca_buy(
    world: World, tmp_path: Path
) -> None:
    path = _on_disk(world, tmp_path)
    before = _running(world).trading
    mandate_id = (await world.create(every_seconds=HOUR))["mandate"]["id"]
    await _loop_tick(world, 0)
    assert [r["status"] for r in world.runs(mandate_id)] == ["filled"]
    signed = len(world.chain.sent)

    _edit_trading(path, enabled=False)
    _operator_reload(world)
    # The section object was replaced; the engine must not keep the old one.
    assert _running(world).trading is not before
    assert world.service.config is _running(world).trading

    await _loop_tick(world, HOUR)
    await _loop_tick(world, HOUR)
    assert len(world.chain.sent) == signed, "a DCA buy was signed after trading was switched off"
    assert [r["status"] for r in world.runs(mandate_id)] == ["filled"]
    row = world.row(mandate_id)
    # Nothing counted against the mandate: it resumes as it was.
    assert row["status"] == "active" and row["bad_streak"] == 0

    _edit_trading(path, enabled=True)
    _operator_reload(world)
    await _loop_tick(world, 0)
    assert len(world.chain.sent) == signed + 1


async def test_config_set_daily_cap_zero_rejects_the_next_dca_buy(
    world: World, tmp_path: Path
) -> None:
    _on_disk(world, tmp_path)
    mandate_id = (await world.create(every_seconds=HOUR))["mandate"]["id"]
    await _loop_tick(world, 0)
    signed = len(world.chain.sent)

    await _operator_set(world, "trading.daily_cap_usd", 0)
    await _loop_tick(world, HOUR)
    assert len(world.chain.sent) == signed
    runs = world.runs(mandate_id)
    assert runs[0]["status"] == "skipped" and runs[0]["reason"].startswith("daily_cap")


async def test_config_set_lowered_threshold_parks_the_next_dca_buy(
    world: World, tmp_path: Path
) -> None:
    _on_disk(world, tmp_path)
    mandate_id = (await world.create(every_seconds=HOUR))["mandate"]["id"]
    await _loop_tick(world, 0)
    signed = len(world.chain.sent)

    await _operator_set(world, "trading.approval_threshold_usd", 5)
    await _loop_tick(world, HOUR)
    assert len(world.chain.sent) == signed
    assert world.runs(mandate_id)[0]["status"] == "parked"


async def test_an_order_already_parked_is_not_signed_once_trading_is_off(
    world: World, tmp_path: Path
) -> None:
    """The off switch is checked at the signing step, not only at order creation."""
    path = _on_disk(world, tmp_path)
    mandate_id = (await world.create(usd_per_run=150, cap_usd=600, every_seconds=HOUR))["mandate"][
        "id"
    ]
    world.set_price(2000.0, usd=150.0)
    await _loop_tick(world, 0)
    run = world.runs(mandate_id)[0]
    assert run["status"] == "parked"
    signed = len(world.chain.sent)

    _edit_trading(path, enabled=False)
    _operator_reload(world)
    with pytest.raises(TradingError) as err:
        await world.service.approve(run["order_id"], wait=True)
    assert err.value.code == "trading.disabled"
    assert len(world.chain.sent) == signed
    order = world.service.ledger.get_order(run["order_id"])
    assert order is not None and order["status"] == "awaiting_approval"


async def test_the_signing_step_refuses_while_trading_is_off(world: World, tmp_path: Path) -> None:
    """Fail closed for an execution already past every earlier check."""
    path = _on_disk(world, tmp_path)
    mandate_id = (await world.create(every_seconds=HOUR))["mandate"]["id"]
    _edit_trading(path, enabled=False)
    _operator_reload(world)
    # Bypass housekeep's own check: the run starts, the signer must refuse.
    await world.service.dca_run_due()
    await _settle(world.service)
    assert world.chain.sent == []
    assert all(r["status"] != "filled" for r in world.runs(mandate_id))


def test_bridge_client_follows_the_live_aggregator_base_url(world: World) -> None:
    first = world.service._bridge
    assert world.service._bridge is first
    assert first.base_url == "https://agg.useagentos.dev"
    _running(world).trading = _running(world).trading.model_copy(
        update={"aggregator_base_url": "https://dex.example.test"}
    )
    second = world.service._bridge
    assert second is not first
    assert second.base_url == "https://dex.example.test"


def test_a_config_that_still_sets_relay_base_url_keeps_loading(world: World) -> None:
    # The key is unused since the bridge moved behind AgentOS DEX, but a file
    # that carries it must not stop the gateway from booting.
    updated = _running(world).trading.model_copy(
        update={"relay_base_url": "https://relay.example.test"}
    )
    _running(world).trading = updated
    assert world.service._bridge.base_url == "https://agg.useagentos.dev"


def test_trusted_bridges_add_the_operators_spenders(world: World) -> None:
    from agentos.trading.bridge import AGENTOS_BRIDGE

    assert world.service._trusted_bridges() == frozenset({AGENTOS_BRIDGE})
    extra = "0x" + "ab" * 20
    _running(world).trading = _running(world).trading.model_copy(
        update={"aggregator_trusted_spenders": [extra]}
    )
    assert world.service._trusted_bridges() == frozenset({AGENTOS_BRIDGE, extra})


def test_clock_constants_are_shared() -> None:
    # Guards the fixture import above: the DCA world runs on this clock.
    assert T0 > 0 and HOUR == 3_600
