"""Tests for memory-provider config models and boot wiring (Task B3).

Offline and deterministic. Covers: config defaults + overrides; the disabled
default path (``provider_manager is None``, providers package never imported);
and the enabled-but-unavailable path (provider module missing → boots anyway,
``provider_manager is None``).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from agentos.gateway.config import (
    GatewayConfig,
    Mem0ProviderSettings,
    MemoryProviderSettings,
)

# ── Config models ────────────────────────────────────────────────────────


def test_provider_settings_defaults_disabled() -> None:
    cfg = GatewayConfig()
    assert cfg.memory.provider.name is None
    assert isinstance(cfg.memory.provider, MemoryProviderSettings)


def test_mem0_settings_defaults() -> None:
    s = Mem0ProviderSettings()
    assert s.llm_provider == "ollama"
    assert s.llm_model == "qwen3:4b"
    assert s.llm_base_url == "http://localhost:11434"
    assert s.embedder_provider == "ollama"
    assert s.embedder_model == "embeddinggemma"
    assert s.embedder_base_url == "http://localhost:11434"
    assert s.vector_store_path is None


def test_provider_settings_overrides() -> None:
    cfg = GatewayConfig(
        memory={
            "provider": {
                "name": "mem0",
                "mem0": {
                    "llm_model": "llama3.1:8b",
                    "vector_store_path": "/custom/mem0",
                },
            }
        }
    )
    assert cfg.memory.provider.name == "mem0"
    assert cfg.memory.provider.mem0.llm_model == "llama3.1:8b"
    assert cfg.memory.provider.mem0.vector_store_path == "/custom/mem0"
    # Untouched fields keep defaults.
    assert cfg.memory.provider.mem0.embedder_model == "embeddinggemma"


def test_provider_settings_forbids_extra() -> None:
    with pytest.raises(Exception):
        MemoryProviderSettings(bogus="x")  # type: ignore[call-arg]


# ── Boot wiring ──────────────────────────────────────────────────────────


class _FakeStore:
    providers: list[Any] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        _FakeStore.providers.append(kwargs.get("embedding_provider"))

    async def initialize(self) -> None:
        return None

    async def remove_file(self, rel_path: str) -> None:
        return None

    async def close(self) -> None:
        return None


class _FakeSyncManager:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


def _patch_manager_dependencies(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _FakeStore.providers = []
    monkeypatch.setattr("agentos.memory.store.LongTermMemoryStore", _FakeStore)
    monkeypatch.setattr("agentos.memory.sync_manager.MemorySyncManager", _FakeSyncManager)
    monkeypatch.setattr("agentos.agents.scope.maybe_migrate_legacy_memory", lambda *_: None)
    monkeypatch.setattr(
        "agentos.agents.scope.resolve_agent_memory_db",
        lambda agent_id, state_dir: tmp_path / "state" / agent_id / "memory.db",
    )
    monkeypatch.setattr(
        "agentos.agents.scope.resolve_agent_workspace_dir",
        lambda agent_id, config: tmp_path / "workspace" / agent_id,
    )
    monkeypatch.setattr(
        "agentos.agents.scope.resolve_agent_data_dir",
        lambda agent_id, base=None: tmp_path / "data" / agent_id,
    )
    monkeypatch.setattr(
        "agentos.agents.scope.resolve_agent_memory_dir",
        lambda agent_id: tmp_path / "memory" / agent_id,
    )


async def _build(config: GatewayConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    from agentos.memory.manager import build_memory_managers

    _patch_manager_dependencies(monkeypatch, tmp_path)
    return await build_memory_managers(config, ["main"])


@pytest.mark.asyncio
async def test_disabled_default_leaves_provider_manager_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Default config: no provider_manager and providers package not imported."""
    # Drop any pre-imported providers modules so we can assert the disabled
    # path never (re)imports them.
    for mod in list(sys.modules):
        if mod.startswith("agentos.memory.providers"):
            monkeypatch.delitem(sys.modules, mod, raising=False)

    managers = await _build(GatewayConfig(), monkeypatch, tmp_path)
    try:
        assert managers["main"].provider_manager is None
        assert "agentos.memory.providers.registry" not in sys.modules
        assert "agentos.memory.providers.manager" not in sys.modules
    finally:
        for manager in managers.values():
            await manager.close()


@pytest.mark.asyncio
async def test_enabled_but_module_missing_boots_with_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """provider.name='mem0' but mem0_provider module missing → boots, None."""
    # Fake the extra away so this holds when mem0ai is installed too.
    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a, **k: None if name == "mem0" else real_find_spec(name, *a, **k),
    )
    config = GatewayConfig(memory={"provider": {"name": "mem0"}})
    managers = await _build(config, monkeypatch, tmp_path)
    try:
        assert "main" in managers  # boot succeeded
        assert managers["main"].provider_manager is None
    finally:
        for manager in managers.values():
            await manager.close()
