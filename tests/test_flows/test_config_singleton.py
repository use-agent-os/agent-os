"""``[flows]`` config section and the process-wide service."""

from __future__ import annotations

from pathlib import Path

from agentos.flows.service import (
    FlowService,
    get_flow_service,
    peek_flow_service,
    reset_flow_service,
)
from agentos.gateway.config import FlowsConfig, GatewayConfig


def test_flows_config_defaults_and_env(monkeypatch) -> None:
    config = FlowsConfig()
    assert (
        config.enabled,
        config.max_concurrent_runs,
        config.agent_cost_limit_usd,
        config.cache_max_mb,
    ) == (True, 1, 0.50, 500)
    monkeypatch.setenv("AGENTOS_FLOWS_MAX_CONCURRENT_RUNS", "3")
    monkeypatch.setenv("AGENTOS_FLOWS_ENABLED", "false")
    config = FlowsConfig()
    assert config.max_concurrent_runs == 3 and config.enabled is False


def test_gateway_config_has_a_flows_section_omitted_when_default(tmp_path) -> None:
    gateway = GatewayConfig(state_dir=str(tmp_path / "state"))
    assert gateway.flows == FlowsConfig()
    service = FlowService(gateway)
    assert service.root == Path(tmp_path / "state" / "flows")
    assert service.max_concurrent_runs == 1 and service.cache_max_bytes == 500 * 1024 * 1024
    assert service.agent_cost_limit_usd == 0.50
    service.db.close()


async def test_singleton_is_built_once_and_refreshed(tmp_path) -> None:
    reset_flow_service()
    try:
        first = GatewayConfig(state_dir=str(tmp_path / "state"))
        service = get_flow_service(first)
        assert peek_flow_service() is service
        second = GatewayConfig(state_dir=str(tmp_path / "other"))
        second.flows.max_concurrent_runs = 2

        async def broadcast(event, payload):
            return None

        assert get_flow_service(second, broadcast=broadcast) is service
        assert service.max_concurrent_runs == 2
        assert service.root == Path(tmp_path / "state" / "flows")  # state is not rebuilt
        await service.close()
    finally:
        reset_flow_service()
