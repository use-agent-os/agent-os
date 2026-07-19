"""Boot must build a provider selector for local (no-key) providers.

Regression: ``build_services`` only constructed ``provider_selector`` when an
API key was present (``if api_key:``). Local providers — ollama / lm_studio /
ovms — legitimately have no key, so the selector was never built and every
turn failed with ``no_provider``. The registry already models this via
``ProviderSpec.requires_api_key()``; boot must honor it.
"""

from __future__ import annotations

from agentos.gateway.boot import _should_build_provider_selector


def test_local_provider_without_key_builds_selector() -> None:
    # Ollama has no API key but does not require one.
    assert _should_build_provider_selector(provider="ollama", api_key="") is True


def test_lm_studio_without_key_builds_selector() -> None:
    assert _should_build_provider_selector(provider="lm_studio", api_key="") is True


def test_remote_provider_without_key_does_not_build_selector() -> None:
    # Missing key for a key-requiring provider stays no_provider (correct signal).
    assert _should_build_provider_selector(provider="openrouter", api_key="") is False


def test_remote_provider_with_key_builds_selector() -> None:
    assert _should_build_provider_selector(provider="openrouter", api_key="sk-x") is True


def test_unknown_provider_falls_back_to_key_presence() -> None:
    # Unknown provider id must not raise during boot; gate on key presence.
    assert _should_build_provider_selector(provider="totally-made-up", api_key="") is False
    assert _should_build_provider_selector(provider="totally-made-up", api_key="k") is True
