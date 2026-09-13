"""Auto-titling a session from its first message.

The model call goes through the auxiliary client, so these tests inject a
stub provider the same way ``test_provider_auxiliary`` does and run against a
real ``SessionManager`` so the title is proven to land in ``display_name``.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from agentos.gateway import session_titler as titler_mod
from agentos.gateway.session_titler import (
    SessionTitler,
    clean_title,
    fallback_title,
    fast_model_hint,
    is_placeholder_name,
    reset_titlers,
    titler_for,
)
from agentos.provider import auxiliary as aux
from agentos.provider.types import DoneEvent, TextDeltaEvent
from agentos.session.manager import SessionManager
from agentos.session.storage import SessionStorage

KEY = "agent:main:webchat:abc12345"

_ENV_KEYS = (
    "AGENTOS_SESSION_TITLE_MODEL",
    "AGENTOS_SESSION_TITLE_PROVIDER",
    "AGENTOS_AUXILIARY_MODEL",
    "AGENTOS_LLM_MODEL",
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    for name in _ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    reset_titlers()
    yield
    reset_titlers()


@pytest_asyncio.fixture
async def manager():
    storage = SessionStorage(":memory:")
    await storage.connect()
    mgr = SessionManager(storage, inject_time_prefix=False)
    yield mgr
    await storage.close()


def _stub_client(monkeypatch: pytest.MonkeyPatch, *texts: str) -> list[dict]:
    """Auxiliary client whose provider replays ``texts`` as one completion."""
    calls: list[dict] = []

    class _Provider:
        async def chat(self, *, messages, config=None, tools=None):
            calls.append({"messages": messages, "config": config})
            for text in texts:
                yield TextDeltaEvent(text=text)
            yield DoneEvent(input_tokens=10, output_tokens=5)

    def factory(**_kwargs):
        return _Provider()

    client = aux.AuxiliaryClient(provider_factory=factory)
    monkeypatch.setattr(titler_mod, "get_auxiliary_client", lambda: client)
    return calls


def _failing_client(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Client:
        async def complete(self, **_kwargs):
            raise aux.AuxiliaryError("no credentials", task="session_title")

    monkeypatch.setattr(titler_mod, "get_auxiliary_client", lambda: _Client())


# ── pure helpers ─────────────────────────────────────────────────────────────


def test_placeholder_detection():
    assert is_placeholder_name(None)
    assert is_placeholder_name("WebChat")
    assert is_placeholder_name("  webchat ")
    assert is_placeholder_name("abc12345", session_id="abc12345-ffff")
    assert not is_placeholder_name("Fix CI flake", session_id="abc12345-ffff")


def test_clean_title_strips_model_noise():
    assert clean_title('Title: "Fix the CI flake."') == "Fix the CI flake"
    assert clean_title("Tiêu đề: Kiểm tra lỗi x-research\nmore") == "Kiểm tra lỗi x-research"
    assert clean_title("one two three four five six seven eight nine ten eleven twelve") == (
        "one two three four five six seven eight nine ten"
    )
    # Vietnamese syllables are whitespace-separated: a 6-word title is ~10 here.
    assert clean_title("Kế hoạch du lịch Đà Nẵng 3 ngày ngân sách") == (
        "Kế hoạch du lịch Đà Nẵng 3 ngày ngân sách"
    )
    assert clean_title("   ") is None


def test_fallback_title_takes_the_first_clause():
    assert fallback_title("Giúp tôi viết email xin nghỉ phép. Cảm ơn!") == (
        "Giúp tôi viết email xin nghỉ phép"
    )
    assert fallback_title("\n\n") is None


# ── titler ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_titles_a_webchat_session_from_the_first_message(manager, monkeypatch):
    calls = _stub_client(monkeypatch, "Kiểm tra ", "lỗi x-research")
    await manager.create(KEY, display_name="WebChat")
    seen: list[tuple[str, dict]] = []

    async def broadcast(key: str, state: dict) -> None:
        seen.append((key, state))

    titler = SessionTitler(manager, broadcast=broadcast)
    assert titler.maybe_schedule(KEY, "Kiểm tra giúp tôi lỗi x-research bị crash") is True
    await titler.drain()

    node = await manager.get_session(KEY)
    assert node is not None and node.display_name == "Kiểm tra lỗi x-research"
    assert seen == [(KEY, {"display_name": node.display_name, "displayName": node.display_name})]
    # The model saw the message under the titling system prompt. The cap
    # leaves room for a reasoning model to think before the (short) answer.
    config = calls[0]["config"]
    assert config.max_tokens == titler_mod.TITLE_MAX_TOKENS
    assert config.max_tokens >= 256
    assert "title" in (config.system or "").lower()


@pytest.mark.asyncio
async def test_never_overwrites_a_name_a_person_chose(manager, monkeypatch):
    calls = _stub_client(monkeypatch, "Something else")
    await manager.create(KEY, display_name="Deep research pricing")

    titler = SessionTitler(manager)
    titler.maybe_schedule(KEY, "hello")
    await titler.drain()

    node = await manager.get_session(KEY)
    assert node is not None and node.display_name == "Deep research pricing"
    assert calls == []


@pytest.mark.asyncio
async def test_rename_during_generation_wins(manager, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    class _Client:
        async def complete(self, **_kwargs):
            started.set()
            await release.wait()
            return aux.AuxResult(text="Model title", provider="x", model="y")

    monkeypatch.setattr(titler_mod, "get_auxiliary_client", lambda: _Client())
    await manager.create(KEY, display_name="WebChat")

    titler = SessionTitler(manager)
    titler.maybe_schedule(KEY, "first message")
    await started.wait()
    await manager.update(KEY, display_name="Mine")
    release.set()
    await titler.drain()

    node = await manager.get_session(KEY)
    assert node is not None and node.display_name == "Mine"


@pytest.mark.asyncio
async def test_falls_back_to_a_heuristic_when_the_model_is_unavailable(manager, monkeypatch):
    _failing_client(monkeypatch)
    await manager.create(KEY)

    titler = SessionTitler(manager)
    titler.maybe_schedule(KEY, "Viết lại email xin nghỉ phép cho sếp. Ngắn thôi.")
    await titler.drain()

    node = await manager.get_session(KEY)
    # The heuristic keeps the whole first clause now that the word limit fits
    # Vietnamese syllables (the old cut left "…phép cho").
    assert node is not None and node.display_name == "Viết lại email xin nghỉ phép cho sếp"


@pytest.mark.asyncio
async def test_skips_internal_run_kinds_and_dedupes_inflight(manager, monkeypatch):
    _stub_client(monkeypatch, "Title")
    await manager.create(KEY)
    titler = SessionTitler(manager)

    assert titler.maybe_schedule(KEY, "hi", run_kind="cron") is False
    assert titler.maybe_schedule(KEY, "hi", run_kind="session_turn") is True
    assert titler.maybe_schedule(KEY, "hi again") is False  # already in flight
    await titler.drain()


@pytest.mark.asyncio
async def test_disabled_titler_does_nothing(manager, monkeypatch):
    calls = _stub_client(monkeypatch, "Title")
    await manager.create(KEY)
    titler = SessionTitler(manager, enabled=False)
    assert titler.maybe_schedule(KEY, "hi") is False
    await titler.drain()
    assert calls == []


def test_titler_for_is_per_manager():
    a, b = object(), object()
    assert titler_for(a) is titler_for(a)
    assert titler_for(a) is not titler_for(b)


# ── model hint ───────────────────────────────────────────────────────────────


def test_fast_model_hint_prefers_the_lowest_text_tier():
    config = {
        "llm": {"provider": "opencap"},
        "agentos_router": {
            "tiers": {
                "c2": {"model": "glm-5.3", "provider": "opencap"},
                "c0": {"model": "deepseek-v4-flash"},
                "image_model": {"model": "gpt-5.6-luna", "image_only": True},
            }
        },
    }
    assert fast_model_hint(config) == ("opencap", "deepseek-v4-flash")


def test_fast_model_hint_skips_image_only_and_missing_tiers():
    config = {
        "agentos_router": {
            "tiers": {"c0": {"model": "", "image_only": True}, "c1": {"model": "fast-1"}}
        }
    }
    assert fast_model_hint(config) == ("", "fast-1")
    assert fast_model_hint({"agentos_router": {"tiers": {}}}) == ("", "")
    assert fast_model_hint(None) == ("", "")


def test_fast_model_hint_yields_to_an_explicit_env_pin(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTOS_SESSION_TITLE_MODEL", "pinned")
    config = {"agentos_router": {"tiers": {"c0": {"model": "fast"}}}}
    assert fast_model_hint(config) == ("", "")


@pytest.mark.asyncio
async def test_hint_reaches_the_auxiliary_call(manager, monkeypatch):
    captured: dict = {}

    class _Client:
        async def complete(self, **kwargs):
            captured.update(kwargs)
            return aux.AuxResult(text="Fast title", provider="p", model="m")

    monkeypatch.setattr(titler_mod, "get_auxiliary_client", lambda: _Client())
    await manager.create(KEY)
    titler = SessionTitler(manager, hint=("opencap", "deepseek-v4-flash"))
    titler.maybe_schedule(KEY, "hi there")
    await titler.drain()
    assert captured["preferred_model"] == "deepseek-v4-flash"
    assert captured["preferred_provider"] == "opencap"
