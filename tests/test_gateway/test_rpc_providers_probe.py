"""``providers.probe``: try a key before it is saved — list models, send one token."""

from __future__ import annotations

from typing import Any

import pytest

from agentos.gateway import rpc_tools
from agentos.gateway.access import CONTROL_ONLY
from agentos.gateway.config import GatewayConfig
from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.provider.selector import ProviderBuildError
from agentos.provider.types import DoneEvent, ErrorEvent, ModelInfo


class _FakeProvider:
    """A provider whose model list and chat verdict the test scripts."""

    def __init__(self, *, models: list[str], chat_error: str | None, api_key: str) -> None:
        self._models = models
        self._chat_error = chat_error
        self.api_key = api_key

    async def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(provider="opencap", model_id=m, display_name=m.upper()) for m in self._models
        ]

    async def chat(self, messages: Any, config: Any):  # noqa: ANN202 - async generator
        if self._chat_error:
            yield ErrorEvent(code="unauthorized", message=self._chat_error)
            return
        yield DoneEvent(input_tokens=1, output_tokens=1)


def _ctx() -> RpcContext:
    return RpcContext(conn_id="test", config=GatewayConfig())


def _install_factory(
    monkeypatch: pytest.MonkeyPatch,
    *,
    models: list[str],
    chat_error: str | None = None,
    build_error: str | None = None,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def build_provider(provider: str, model: str, api_key: str = "", base_url: str = "", **_: Any):
        calls.append(
            {"provider": provider, "model": model, "api_key": api_key, "base_url": base_url}
        )
        if build_error:
            raise ProviderBuildError(build_error)
        return _FakeProvider(models=models, chat_error=chat_error, api_key=api_key)

    monkeypatch.setattr("agentos.provider.selector.build_provider", build_provider)
    return calls


@pytest.mark.asyncio
async def test_probe_is_control_only() -> None:
    entry = get_dispatcher().get_entry("providers.probe")
    assert entry is not None
    assert entry.audiences == CONTROL_ONLY


@pytest.mark.asyncio
async def test_probe_lists_models_and_verifies_the_key_with_one_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_factory(monkeypatch, models=["gpt-5.6-luna", "deepseek-v4-flash"])
    response = await get_dispatcher().dispatch(
        "r", "providers.probe", {"providerId": "opencap", "apiKey": "sk-try"}, _ctx()
    )
    assert response.ok is True, response.error
    payload = response.payload
    assert payload["ok"] is True
    assert payload["error"] is None
    assert [m["id"] for m in payload["models"]] == ["gpt-5.6-luna", "deepseek-v4-flash"]
    assert payload["models"][0]["name"] == "GPT-5.6-LUNA"
    assert payload["latencyMs"] >= 0
    # The draft key was used, and never echoed back.
    assert calls[0]["api_key"] == "sk-try"
    assert "sk-try" not in str(payload)


@pytest.mark.asyncio
async def test_probe_reports_a_rejected_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_factory(monkeypatch, models=["m1"], chat_error="Invalid API key")
    response = await get_dispatcher().dispatch(
        "r", "providers.probe", {"providerId": "opencap", "apiKey": "sk-bad"}, _ctx()
    )
    assert response.payload["ok"] is False
    assert "Invalid API key" in response.payload["error"]
    # The list may still be there (some gateways serve it unauthenticated).
    assert [m["id"] for m in response.payload["models"]] == ["m1"]


@pytest.mark.asyncio
async def test_probe_without_any_key_says_so_without_calling_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_factory(monkeypatch, models=["m1"])
    monkeypatch.delenv("OPENCAP_API_KEY", raising=False)
    response = await get_dispatcher().dispatch(
        "r", "providers.probe", {"providerId": "opencap"}, _ctx()
    )
    assert response.payload["ok"] is False
    assert "No API key" in response.payload["error"]
    assert calls == []


@pytest.mark.asyncio
async def test_probe_falls_back_to_the_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_factory(monkeypatch, models=["m1"])
    monkeypatch.setenv("OPENCAP_API_KEY", "sk-env")
    response = await get_dispatcher().dispatch(
        "r", "providers.probe", {"providerId": "opencap"}, _ctx()
    )
    assert response.payload["ok"] is True
    assert calls[0]["api_key"] == "sk-env"


@pytest.mark.asyncio
async def test_probe_surfaces_a_build_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_factory(monkeypatch, models=[], build_error="unsupported provider build")
    response = await get_dispatcher().dispatch(
        "r", "providers.probe", {"providerId": "opencap", "apiKey": "k"}, _ctx()
    )
    assert response.payload["ok"] is False
    assert "unsupported provider build" in response.payload["error"]


@pytest.mark.asyncio
async def test_probe_rejects_unknown_provider_and_missing_params() -> None:
    bad = await get_dispatcher().dispatch("r", "providers.probe", {"providerId": "nope"}, _ctx())
    assert bad.ok is False
    missing = await get_dispatcher().dispatch("r", "providers.probe", {}, _ctx())
    assert missing.ok is False


def test_probe_chat_maps_a_timeout_to_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    class _Hanging:
        async def chat(self, messages: Any, config: Any):  # noqa: ANN202
            await asyncio.sleep(10)
            yield DoneEvent(input_tokens=0, output_tokens=0)

    error = asyncio.run(rpc_tools._probe_chat(_Hanging(), "m", timeout=0.05))
    assert error is not None and "no answer" in error
