"""Boot must warn when router tiers declare a provider other than llm.provider.

AgentOS routing is single-provider: boot builds ONE provider client from
``llm.provider``; ``agentos_router.tiers[*].provider`` is metadata and never
builds a client. A tier pointing at a different provider silently misroutes
(the runtime degrades such routes to ``llm.model`` on local providers), so
boot must surface the mismatch as a one-time structured warning.
"""

from __future__ import annotations

from types import SimpleNamespace

import agentos.gateway.boot as boot
from agentos.gateway.boot import (
    _router_tier_provider_mismatches,
    _warn_on_tier_provider_mismatch,
)


def _config(*, enabled: bool = True, tiers: dict | None = None, router: bool = True):
    router_ns = SimpleNamespace(enabled=enabled, tiers=tiers if tiers is not None else {})
    return SimpleNamespace(agentos_router=router_ns if router else None)


def test_local_llm_with_cloud_tiers_reports_all_mismatches() -> None:
    config = _config(
        tiers={
            "c0": {"provider": "openrouter", "model": "deepseek/deepseek-v4-flash"},
            "c1": {"provider": "openrouter", "model": "minimax/minimax-m3"},
        }
    )
    assert _router_tier_provider_mismatches(config=config, llm_provider="ollama") == {
        "c0": "openrouter",
        "c1": "openrouter",
    }


def test_cloud_llm_with_different_cloud_tiers_reports_mismatch() -> None:
    config = _config(tiers={"c2": {"provider": "openrouter", "model": "z-ai/glm-5.2"}})
    assert _router_tier_provider_mismatches(config=config, llm_provider="deepseek") == {
        "c2": "openrouter"
    }


def test_disabled_router_reports_nothing() -> None:
    config = _config(enabled=False, tiers={"c0": {"provider": "openrouter", "model": "x"}})
    assert _router_tier_provider_mismatches(config=config, llm_provider="ollama") == {}


def test_missing_router_config_reports_nothing() -> None:
    config = _config(router=False)
    assert _router_tier_provider_mismatches(config=config, llm_provider="ollama") == {}


def test_matching_tiers_after_normalization_report_nothing() -> None:
    config = _config(tiers={"c0": {"provider": " OpenRouter ", "model": "x"}})
    assert _router_tier_provider_mismatches(config=config, llm_provider="openrouter") == {}


def test_tiers_without_provider_key_report_nothing() -> None:
    config = _config(tiers={"c0": {"model": "x"}, "c1": {"provider": "", "model": "y"}})
    assert _router_tier_provider_mismatches(config=config, llm_provider="ollama") == {}


def test_mixed_tiers_report_only_mismatched_entries() -> None:
    config = _config(
        tiers={
            "c0": {"provider": "ollama", "model": "llama3"},
            "c1": {"provider": "openrouter", "model": "minimax/minimax-m3"},
            "c2": {"model": "no-provider-key"},
        }
    )
    assert _router_tier_provider_mismatches(config=config, llm_provider="ollama") == {
        "c1": "openrouter"
    }


class _WarningRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def warning(self, event: str, **kwargs) -> None:
        self.calls.append((event, kwargs))


def test_warn_helper_logs_exactly_one_warning(monkeypatch) -> None:
    recorder = _WarningRecorder()
    monkeypatch.setattr(boot, "log", recorder)
    config = _config(
        tiers={
            "c0": {"provider": "openrouter", "model": "a"},
            "c1": {"provider": "openrouter", "model": "b"},
        }
    )

    _warn_on_tier_provider_mismatch(config, "ollama")

    assert len(recorder.calls) == 1
    event, kwargs = recorder.calls[0]
    assert event == "agentos_router.tier_provider_mismatch"
    assert kwargs["llm_provider"] == "ollama"
    assert kwargs["mismatched_tiers"] == {"c0": "openrouter", "c1": "openrouter"}
    assert "llm.provider" in kwargs["note"]


def test_warn_helper_is_silent_without_mismatch(monkeypatch) -> None:
    recorder = _WarningRecorder()
    monkeypatch.setattr(boot, "log", recorder)
    config = _config(tiers={"c0": {"provider": "ollama", "model": "llama3"}})

    _warn_on_tier_provider_mismatch(config, "ollama")

    assert recorder.calls == []
