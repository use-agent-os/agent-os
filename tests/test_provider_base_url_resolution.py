"""A provider's key goes to that provider's endpoint, never to OpenRouter's.

``[llm] base_url`` defaulted to OpenRouter's URL whatever ``provider`` said.
The runtime resolves ``env or llm.base_url or the provider's default``, so the
default never applied: with ``provider = "surplus"``, ``api_key_env =
"SURPLUS_API_KEY"`` and no ``base_url``, every request went to
``https://openrouter.ai/api/v1/chat/completions`` with the Surplus key in its
``Authorization`` and ``x-api-key`` headers (OpenRouter answers a key it does
not know with HTTP 401 "Missing Authentication header"). The same held for
every registered provider except OpenRouter, in the gateway and in
``agentos agent`` alike, and ``agentos configure provider`` saved that URL.

Now an unset ``base_url`` is the provider's own endpoint, and a key of
another provider is never sent to an OpenRouter host: a config that still
points such a provider at OpenRouter is refused with a message that says how
to fix it. An OpenRouter key (``sk-or-...``) is OpenRouter's own under any
provider name, so ``provider = "openai"`` with OpenRouter's URL keeps working.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from agentos.gateway.config import GatewayConfig
from agentos.gateway.llm_runtime import resolve_llm_runtime_config
from agentos.onboarding.config_store import load_config
from agentos.onboarding.mutations import upsert_llm_provider
from agentos.provider.registry import list_provider_specs
from agentos.provider.selector import (
    ModelSelector,
    ProviderBuildError,
    ProviderConfig,
    SelectorConfig,
    _build_provider,
)
from agentos.provider.types import Message

OPENROUTER_URL = "https://openrouter.ai/api/v1"
SURPLUS_URL = "https://api.surplusintelligence.ai/v1"
FAKE_KEY = "surplus-test-key-not-real"
OPENROUTER_KEY = "sk-or-v1-test-key-not-real"

_RUNTIME_SPECS = [spec for spec in list_provider_specs() if spec.runtime_supported]
_OTHER_PROVIDERS = [spec for spec in _RUNTIME_SPECS if spec.provider_id != "openrouter"]


@pytest.fixture(autouse=True)
def _no_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTOS_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("AGENTOS_LLM_PROXY", raising=False)
    for spec in _RUNTIME_SPECS:
        if spec.env_key:
            monkeypatch.delenv(spec.env_key.removesuffix("_API_KEY") + "_BASE_URL", raising=False)


def _write_config(tmp_path: Path, body: str) -> GatewayConfig:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return load_config(path)


def _surplus_minimal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GatewayConfig:
    """The reported config: no ``base_url``, the key in ``SURPLUS_API_KEY``."""
    monkeypatch.setenv("SURPLUS_API_KEY", FAKE_KEY)
    return _write_config(
        tmp_path,
        '[llm]\nprovider = "surplus"\nmodel = "glm-5.3"\napi_key_env = "SURPLUS_API_KEY"\n',
    )


def _boot_selector(config: GatewayConfig) -> ModelSelector:
    """The provider selector exactly as ``build_services`` constructs it."""
    runtime = resolve_llm_runtime_config(config)
    base_url = runtime.base_url
    if base_url.endswith("/v1"):
        base_url = base_url[:-3]
    return ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                provider=runtime.provider,
                model=runtime.model,
                api_key=runtime.api_key,
                base_url=base_url,
                proxy=runtime.proxy,
            )
        )
    )


def _record_requests(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        chunk = {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}
        body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("agentos.provider.openai.httpx.AsyncClient", patched)
    return seen


async def _one_turn(selector: ModelSelector) -> None:
    provider = selector.resolve()
    async for _event in provider.chat([Message(role="user", content="hi")]):
        pass


def test_the_reported_surplus_config_talks_to_surplus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _surplus_minimal(tmp_path, monkeypatch)
    seen = _record_requests(monkeypatch)

    asyncio.run(_one_turn(_boot_selector(config)))

    assert len(seen) == 1
    request = seen[0]
    assert request.url.host == "api.surplusintelligence.ai"
    assert str(request.url) == f"{SURPLUS_URL}/chat/completions"
    assert request.headers["authorization"] == f"Bearer {FAKE_KEY}"


@pytest.mark.parametrize("spec", _RUNTIME_SPECS, ids=lambda spec: spec.provider_id)
def test_an_unset_base_url_is_the_providers_own_endpoint(
    spec: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROBE_PROVIDER_KEY", "test-key")
    config = _write_config(
        tmp_path,
        f'[llm]\nprovider = "{spec.provider_id}"\nmodel = "m"\n'
        'api_key_env = "PROBE_PROVIDER_KEY"\n',
    )

    assert config.llm.base_url == spec.default_base_url
    assert resolve_llm_runtime_config(config).base_url == spec.default_base_url


def test_the_default_config_still_uses_openrouter() -> None:
    assert GatewayConfig().llm.provider == "openrouter"
    assert GatewayConfig().llm.base_url == OPENROUTER_URL


def test_an_explicit_base_url_is_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SURPLUS_API_KEY", FAKE_KEY)
    config = _write_config(
        tmp_path,
        '[llm]\nprovider = "surplus"\nmodel = "glm-5.3"\nbase_url = "https://surplus.example/v1"\n',
    )
    assert resolve_llm_runtime_config(config).base_url == "https://surplus.example/v1"


@pytest.mark.parametrize("spec", _OTHER_PROVIDERS, ids=lambda spec: spec.provider_id)
@pytest.mark.parametrize(
    "url", [OPENROUTER_URL, "https://openrouter.ai/api", "https://eu.openrouter.ai/v1"]
)
def test_another_providers_key_is_never_sent_to_openrouter(spec: Any, url: str) -> None:
    config = ProviderConfig(provider=spec.provider_id, model="m", api_key="k", base_url=url)

    with pytest.raises(ProviderBuildError) as excinfo:
        _build_provider(config)

    message = str(excinfo.value)
    assert spec.provider_id in message and "OpenRouter" in message
    assert "base_url" in message


def test_a_stale_openrouter_base_url_fails_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # What ``agentos configure provider`` used to save for Surplus.
    monkeypatch.setenv("SURPLUS_API_KEY", FAKE_KEY)
    config = _write_config(
        tmp_path,
        '[llm]\nprovider = "surplus"\nmodel = "glm-5.3"\napi_key_env = "SURPLUS_API_KEY"\n'
        f'base_url = "{OPENROUTER_URL}"\n',
    )
    seen = _record_requests(monkeypatch)

    with pytest.raises(ProviderBuildError, match=SURPLUS_URL):
        asyncio.run(_one_turn(_boot_selector(config)))
    assert seen == []


def test_an_openrouter_key_may_still_serve_router_tiers_of_another_provider() -> None:
    # A router tier naming another provider runs on the primary's endpoint
    # and key; with an OpenRouter primary that key is OpenRouter's own.
    fallback = ProviderConfig(
        provider="openai",
        model="openai/gpt-5.6",
        api_key="or-key",
        base_url="https://openrouter.ai/api",
        credential_provider="openrouter",
    )
    assert _build_provider(fallback) is not None


def test_a_surplus_key_inherited_by_a_router_tier_is_not_sent_to_openrouter() -> None:
    fallback = ProviderConfig(
        provider="openrouter",
        model="anthropic/claude-haiku-5.5",
        api_key=FAKE_KEY,
        base_url="https://openrouter.ai/api",
        credential_provider="surplus",
    )
    with pytest.raises(ProviderBuildError):
        _build_provider(fallback)


def test_an_openrouter_key_may_go_to_openrouter_under_another_provider_name() -> None:
    # ``provider = "openai"`` with OpenRouter's URL and an OpenRouter key: the
    # OpenAI-compatible way to use OpenRouter. The key is OpenRouter's own.
    config = ProviderConfig(
        provider="openai", model="openai/gpt-5.6", api_key=OPENROUTER_KEY, base_url=OPENROUTER_URL
    )
    assert _build_provider(config) is not None


@pytest.mark.parametrize("explicit", [False, True], ids=["saved", "explicit"])
def test_configure_keeps_an_openrouter_url_that_serves_an_openrouter_key(
    explicit: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Replacing the URL with OpenAI's would send the OpenRouter key to OpenAI.
    monkeypatch.setenv("OPENROUTER_API_KEY", OPENROUTER_KEY)
    config = _write_config(
        tmp_path,
        '[llm]\nprovider = "openai"\nmodel = "openai/gpt-5.6"\n'
        f'api_key_env = "OPENROUTER_API_KEY"\nbase_url = "{OPENROUTER_URL}"\n',
    )

    result = upsert_llm_provider(
        config,
        provider_id="openai",
        model="openai/gpt-5.6-mini",
        base_url="https://openrouter.ai/api" if explicit else "",
    )

    assert "openrouter.ai" in result.config.llm.base_url
    assert result.config.llm.api_key_env == "OPENROUTER_API_KEY"


def test_configure_saves_the_providers_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ``agentos configure provider --provider surplus --api-key-env
    # SURPLUS_API_KEY --model glm-5.3`` on the reported config.
    config = _surplus_minimal(tmp_path, monkeypatch)

    result = upsert_llm_provider(
        config, provider_id="surplus", model="glm-5.3", api_key_env="SURPLUS_API_KEY"
    )

    assert result.config.llm.base_url == SURPLUS_URL


@pytest.mark.parametrize("where", ["llm", "profile"])
def test_configure_replaces_a_saved_openrouter_url_of_another_provider(
    where: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The URL an earlier ``configure`` saved: in ``[llm]`` for the active
    # provider, or in the provider's saved profile when switching back to it.
    monkeypatch.setenv("SURPLUS_API_KEY", FAKE_KEY)
    if where == "llm":
        body = (
            '[llm]\nprovider = "surplus"\nmodel = "glm-5.3"\napi_key_env = "SURPLUS_API_KEY"\n'
            f'base_url = "{OPENROUTER_URL}"\n'
        )
    else:
        body = (
            '[llm]\nprovider = "openrouter"\nmodel = "m"\napi_key = "or-key"\n'
            '[provider_profiles.surplus]\nmodel = "glm-5.3"\napi_key_env = "SURPLUS_API_KEY"\n'
            f'base_url = "{OPENROUTER_URL}"\n'
        )
    config = _write_config(tmp_path, body)

    result = upsert_llm_provider(
        config, provider_id="surplus", model="glm-5.3", api_key_env="SURPLUS_API_KEY"
    )

    assert result.config.llm.base_url == SURPLUS_URL


def test_configure_refuses_an_explicit_openrouter_url_for_another_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _surplus_minimal(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="OpenRouter"):
        upsert_llm_provider(
            config,
            provider_id="surplus",
            model="glm-5.3",
            api_key_env="SURPLUS_API_KEY",
            base_url=OPENROUTER_URL,
        )


def test_an_auxiliary_task_does_not_send_the_llm_key_to_openrouter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Titles, compaction and recall run on the [llm] provider's credentials:
    # a stale OpenRouter base_url there must not carry the Surplus key either.
    from agentos.provider.auxiliary import AuxiliaryClient, AuxiliaryError

    monkeypatch.setenv("SURPLUS_API_KEY", FAKE_KEY)
    config = _write_config(
        tmp_path,
        '[llm]\nprovider = "surplus"\nmodel = "glm-5.3"\napi_key_env = "SURPLUS_API_KEY"\n'
        f'base_url = "{OPENROUTER_URL}"\n',
    )
    seen = _record_requests(monkeypatch)
    client = AuxiliaryClient(llm_config=config.llm)
    cfg = client.provider_config(
        "session_title", preferred_provider="surplus", preferred_model="glm-5.3"
    )
    assert cfg.api_key == FAKE_KEY and "openrouter.ai" in cfg.base_url

    with pytest.raises(AuxiliaryError, match="OpenRouter"):
        asyncio.run(
            client.complete(
                task="session_title",
                messages=[Message(role="user", content="hi")],
                preferred_provider="surplus",
                preferred_model="glm-5.3",
            )
        )
    assert seen == []
