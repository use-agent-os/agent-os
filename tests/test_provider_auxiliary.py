from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentos.provider.auxiliary import (
    DEFAULT_AUXILIARY_MODEL,
    AuxiliaryClient,
    AuxiliaryError,
    configure_auxiliary,
    get_auxiliary_client,
)
from agentos.provider.failures import ProviderFailureKind
from agentos.provider.types import DoneEvent, ErrorEvent, Message

_ENV_KEYS = (
    "AGENTOS_VISION_PROVIDER",
    "AGENTOS_VISION_MODEL",
    "AGENTOS_LLM_PROVIDER",
    "AGENTOS_LLM_MODEL",
    "AGENTOS_LLM_PROXY",
    "OPENROUTER_API_KEY",
    "OPENROUTER_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "GEMINI_API_KEY",
    "GEMINI_BASE_URL",
    "GROQ_API_KEY",
    "GROQ_BASE_URL",
    "OLLAMA_BASE_URL",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV_KEYS:
        monkeypatch.delenv(name, raising=False)


def _config(**kwargs: object) -> SimpleNamespace:
    kwargs.setdefault("provider", "")
    kwargs.setdefault("model", "")
    kwargs.setdefault("timeout_seconds", 0.0)
    kwargs.setdefault("tasks", {})
    return SimpleNamespace(**kwargs)


def _stream(*events: object):
    class _Provider:
        async def chat(self, *, messages, config=None, tools=None):
            for event in events:
                yield event

    def factory(**_kwargs: object) -> _Provider:
        return _Provider()

    return factory


# ---------------------------------------------------------------------------
# model resolution
# ---------------------------------------------------------------------------


def test_falls_back_to_the_builtin_model_when_nothing_is_configured() -> None:
    cfg = AuxiliaryClient().provider_config("llm")

    assert cfg.provider == "openrouter"
    assert cfg.model == DEFAULT_AUXILIARY_MODEL


def test_caller_default_beats_the_builtin_model() -> None:
    cfg = AuxiliaryClient().provider_config("llm", default_model="vendor/tiny")

    assert cfg.model == "vendor/tiny"


def test_llm_section_beats_the_caller_default() -> None:
    client = AuxiliaryClient(llm_config=SimpleNamespace(provider="openai", model="gpt-x"))

    cfg = client.provider_config("llm", default_model="vendor/tiny")

    assert cfg.provider == "openai"
    assert cfg.model == "gpt-x"


def test_auxiliary_section_beats_the_llm_section() -> None:
    client = AuxiliaryClient(
        config=_config(provider="anthropic", model="cheap-1"),
        llm_config=SimpleNamespace(provider="openai", model="gpt-x"),
    )

    cfg = client.provider_config("llm")

    assert cfg.provider == "anthropic"
    assert cfg.model == "cheap-1"


def test_caller_hint_beats_the_generic_auxiliary_model() -> None:
    # The hint is capability-aware — a router tier that can actually see an
    # image — so a generic text model configured for all side tasks must not
    # silently take vision's place.
    client = AuxiliaryClient(config=_config(model="text-only-1"))

    cfg = client.provider_config(
        "vision", preferred_provider="openrouter", preferred_model="sees-1"
    )

    assert cfg.model == "sees-1"


def test_per_task_config_beats_the_caller_hint() -> None:
    client = AuxiliaryClient(
        config=_config(tasks={"vision": SimpleNamespace(provider="", model="pinned-1")})
    )

    cfg = client.provider_config("vision", preferred_model="sees-1")

    assert cfg.model == "pinned-1"


def test_task_scoped_env_beats_every_configured_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTOS_VISION_MODEL", "env-pinned-1")
    monkeypatch.setenv("AGENTOS_VISION_PROVIDER", "anthropic")
    client = AuxiliaryClient(
        config=_config(
            model="cheap-1", tasks={"vision": SimpleNamespace(provider="", model="pinned-1")}
        ),
        llm_config=SimpleNamespace(provider="openai", model="gpt-x"),
    )

    cfg = client.provider_config("vision", preferred_model="sees-1")

    assert cfg.provider == "anthropic"
    assert cfg.model == "env-pinned-1"


def test_generic_llm_env_sits_below_the_auxiliary_section(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTOS_LLM_MODEL", "env-generic-1")
    client = AuxiliaryClient(config=_config(model="cheap-1"))

    assert client.provider_config("llm").model == "cheap-1"
    assert AuxiliaryClient().provider_config("llm").model == "env-generic-1"


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------


def test_configured_llm_credentials_are_used_for_the_same_provider() -> None:
    client = AuxiliaryClient(
        llm_config=SimpleNamespace(
            provider="openrouter",
            model="gpt-x",
            api_key="sk-configured",
            base_url="https://router.example/v1",
            proxy="http://proxy.example",
            provider_routing={"gpt-x": "upstream"},
        )
    )

    cfg = client.provider_config("llm")

    assert cfg.api_key == "sk-configured"
    assert cfg.base_url == "https://router.example/v1"
    assert cfg.proxy == "http://proxy.example"
    assert cfg.provider_routing == {"gpt-x": "upstream"}


def test_configured_credentials_are_not_reused_for_a_different_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    client = AuxiliaryClient(
        config=_config(provider="anthropic"),
        llm_config=SimpleNamespace(provider="openrouter", api_key="sk-openrouter"),
    )

    cfg = client.provider_config("llm")

    # An OpenRouter key must never be sent to Anthropic.
    assert cfg.api_key == "sk-ant-env"


def test_api_key_env_indirection_is_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_VENDOR_KEY", "sk-from-env-name")
    client = AuxiliaryClient(
        llm_config=SimpleNamespace(provider="openrouter", api_key="", api_key_env="MY_VENDOR_KEY")
    )

    assert client.provider_config("llm").api_key == "sk-from-env-name"


def test_openrouter_falls_back_to_the_openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")

    cfg = AuxiliaryClient().provider_config("llm")

    assert cfg.provider == "openrouter"
    assert cfg.api_key == "sk-openai"
    assert cfg.base_url == "https://openrouter.ai/api/v1"


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_deltas_are_assembled_into_one_result() -> None:
    client = AuxiliaryClient(
        provider_factory=_stream(
            SimpleNamespace(kind="text_delta", text="hel"),
            SimpleNamespace(kind="text_delta", text="lo"),
            DoneEvent(input_tokens=11, output_tokens=3),
        )
    )

    result = await client.complete(task="llm", messages=[Message(role="user", content="hi")])

    assert result.text == "hello"
    assert result.input_tokens == 11
    assert result.output_tokens == 3


@pytest.mark.asyncio
async def test_stream_error_raises_classified_rather_than_returning_empty_text() -> None:
    client = AuxiliaryClient(
        provider_factory=_stream(ErrorEvent(message="slow down", code="rate_limit_exceeded"))
    )

    with pytest.raises(AuxiliaryError) as excinfo:
        await client.complete(task="llm", messages=[Message(role="user", content="hi")])

    assert excinfo.value.task == "llm"
    assert excinfo.value.kind == ProviderFailureKind.RATE_LIMITED


@pytest.mark.asyncio
async def test_provider_build_failure_is_reported_as_an_auxiliary_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-present")

    def broken_factory(**_kwargs: object):
        raise RuntimeError("backend exploded")

    client = AuxiliaryClient(provider_factory=broken_factory)

    with pytest.raises(AuxiliaryError) as excinfo:
        await client.complete(task="vision", messages=[Message(role="user", content="hi")])

    assert excinfo.value.kind == ProviderFailureKind.AUTH_INVALID
    assert "backend exploded" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_missing_api_key_names_the_provider_and_the_variable_to_set() -> None:
    # Without this guard the request goes out with an empty bearer token and
    # the provider answers "Illegal header value b'Bearer '", which tells an
    # operator nothing about what to fix.
    # No factory is injected, so the real builder — and therefore the guard —
    # is in play. It fires before anything reaches the network.
    client = AuxiliaryClient()

    with pytest.raises(AuxiliaryError) as excinfo:
        await client.complete(task="document", messages=[Message(role="user", content="hi")])

    message = str(excinfo.value)
    assert excinfo.value.kind == ProviderFailureKind.AUTH_INVALID
    assert "openrouter" in message
    assert "OPENROUTER_API_KEY" in message


def test_a_local_backend_needs_no_api_key() -> None:
    # Ollama authenticates by reachability; demanding a key would break it.
    client = AuxiliaryClient(config=_config(provider="ollama", model="llama3"))

    cfg = client.provider_config("document")

    assert cfg.provider == "ollama"
    assert not cfg.api_key
    client._require_credentials("document", cfg)  # must not raise


def test_an_injected_factory_is_trusted_to_handle_its_own_auth() -> None:
    client = AuxiliaryClient(provider_factory=lambda **_kwargs: object())

    cfg = client.provider_config("document")

    assert not cfg.api_key
    client._require_credentials("document", cfg)  # must not raise


@pytest.mark.asyncio
async def test_a_hanging_provider_is_cut_off_by_the_timeout() -> None:
    import asyncio

    class _Hanging:
        async def chat(self, *, messages, config=None, tools=None):
            await asyncio.sleep(30)
            yield DoneEvent()

    client = AuxiliaryClient(provider_factory=lambda **_kwargs: _Hanging())

    with pytest.raises(AuxiliaryError) as excinfo:
        await client.complete(
            task="llm",
            messages=[Message(role="user", content="hi")],
            timeout=0.05,
        )

    assert excinfo.value.kind == ProviderFailureKind.TRANSPORT_TRANSIENT


# ---------------------------------------------------------------------------
# accounting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usage_is_billed_to_the_session_and_kept_separable() -> None:
    from agentos.engine.usage import UsageTracker

    tracker = UsageTracker()
    client = AuxiliaryClient(
        usage_tracker=tracker,
        provider_factory=_stream(
            SimpleNamespace(kind="text_delta", text="ok"),
            DoneEvent(input_tokens=100, output_tokens=20, model="vendor/small"),
        ),
    )

    await client.complete(
        task="vision",
        messages=[Message(role="user", content="hi")],
        session_key="session-1",
    )

    session = tracker.get("session-1")
    assert session is not None
    assert session.input_tokens == 100
    assert session.output_tokens == 20
    # The same tokens are also recorded under a scope, so turn cost and
    # side-task cost stay tellable apart.
    assert ("session-1", "aux:vision") in tracker._scopes


@pytest.mark.asyncio
async def test_a_call_without_a_session_records_nothing_and_still_succeeds() -> None:
    from agentos.engine.usage import UsageTracker

    tracker = UsageTracker()
    client = AuxiliaryClient(
        usage_tracker=tracker,
        provider_factory=_stream(
            SimpleNamespace(kind="text_delta", text="ok"),
            DoneEvent(input_tokens=5, output_tokens=1),
        ),
    )

    result = await client.complete(task="llm", messages=[Message(role="user", content="hi")])

    assert result.text == "ok"
    assert tracker.get("") is None


@pytest.mark.asyncio
async def test_a_failing_tracker_does_not_fail_the_call() -> None:
    class _Broken:
        def add(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("tracker exploded")

    client = AuxiliaryClient(
        usage_tracker=_Broken(),
        provider_factory=_stream(
            SimpleNamespace(kind="text_delta", text="ok"),
            DoneEvent(input_tokens=1, output_tokens=1),
        ),
    )

    result = await client.complete(
        task="llm",
        messages=[Message(role="user", content="hi")],
        session_key="session-1",
    )

    assert result.text == "ok"


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------


def test_configure_replaces_the_shared_client(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentos.provider import auxiliary

    original = auxiliary._client
    try:
        configure_auxiliary(
            _config(model="configured-1"),
            llm_config=SimpleNamespace(provider="openai"),
        )
        assert get_auxiliary_client().provider_config("llm").model == "configured-1"
    finally:
        auxiliary._client = original


def test_configure_keeps_a_tracker_wired_by_an_earlier_call() -> None:
    from agentos.engine.usage import UsageTracker
    from agentos.provider import auxiliary

    original = auxiliary._client
    tracker = UsageTracker()
    try:
        configure_auxiliary(_config(), usage_tracker=tracker)
        # A later config commit carries no tracker; it must not drop the one
        # boot already supplied or side-task cost stops being recorded.
        configure_auxiliary(_config(model="after-commit"))
        assert get_auxiliary_client().usage_tracker is tracker
    finally:
        auxiliary._client = original


def test_provider_config_resolves_credentials_and_default_base_url_for_registered_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-test")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://custom.deepseek.com/v1")

    client = AuxiliaryClient(config=_config(provider="deepseek", model="deepseek-chat"))
    cfg = client.provider_config("vision")

    assert cfg.provider == "deepseek"
    assert cfg.model == "deepseek-chat"
    assert cfg.api_key == "sk-deepseek-test"
    assert cfg.base_url == "https://custom.deepseek.com/v1"


def test_provider_config_falls_back_to_spec_default_base_url_for_registered_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "sk-groq-test")

    client = AuxiliaryClient(config=_config(provider="groq", model="llama-3.3-70b"))
    cfg = client.provider_config("vision")

    assert cfg.provider == "groq"
    assert cfg.model == "llama-3.3-70b"
    assert cfg.api_key == "sk-groq-test"
    assert cfg.base_url == "https://api.groq.com/openai/v1"
