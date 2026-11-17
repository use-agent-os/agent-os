"""Tests for the mem0 memory provider (Task B5, Plan B).

Strictly offline and deterministic: the real ``mem0`` package is imported by
one test only, to read ``Memory``'s method signatures (skipped when the extra
is absent); no client is ever built from it. ``is_available`` is probed via a monkeypatched
``importlib.util.find_spec`` and the client is injected through the
``client_factory`` seam, so these tests pin OUR config dict and call
arguments — not mem0's parser.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
from pathlib import Path
from typing import Any

import pytest

from agentos.gateway.config import MemoryConfig
from agentos.memory.providers.mem0_provider import Mem0Provider


class _FakeMem0Client:
    """Records ``add`` / ``search`` / ``delete`` calls; ``search`` is scripted.

    ``search`` has mem0 2.x's shape: scope goes in ``filters``, the limit is
    ``top_k``, and a top-level entity keyword is rejected with the same
    ``ValueError`` mem0 2.x raises.
    """

    def __init__(self, search_result: Any = None) -> None:
        self.add_calls: list[dict[str, Any]] = []
        self.search_calls: list[dict[str, Any]] = []
        self.delete_calls: list[str] = []
        self._search_result = search_result if search_result is not None else {"results": []}

    def add(self, **kwargs: Any) -> dict[str, Any]:
        self.add_calls.append(kwargs)
        return {"results": []}

    def search(self, query: str, *, top_k: int = 20, filters: Any = None, **kwargs: Any) -> Any:
        rejected = {"user_id", "agent_id", "run_id"} & set(kwargs)
        if rejected:
            raise ValueError(
                f"Top-level entity parameters {rejected} are not supported in search(). "
                "Use filters={'user_id': '...'} instead."
            )
        self.search_calls.append({"query": query, "filters": filters, "top_k": top_k, **kwargs})
        return self._search_result

    def delete(self, memory_id: str) -> None:
        self.delete_calls.append(memory_id)


class _LegacySearchMem0Client(_FakeMem0Client):
    """mem0 0.1.x / 1.x shape: ``search`` takes ``user_id`` / ``limit`` keywords."""

    def search(  # type: ignore[override]
        self,
        query: str,
        *,
        user_id: str | None = None,
        agent_id: str | None = None,
        run_id: str | None = None,
        limit: int = 100,
        filters: Any = None,
        threshold: float | None = None,
    ) -> Any:
        self.search_calls.append(
            {"query": query, "user_id": user_id, "limit": limit, "filters": filters}
        )
        return self._search_result


class _StoreMixin:
    """Keeps added memories per ``user_id`` and searches only that scope."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def add(self, *, messages: list[dict[str, str]], user_id: str, **_: Any) -> dict[str, Any]:
        for message in messages:
            self.records.append({"memory": message["content"], "user_id": user_id})
        return {"results": []}

    def _scoped(self, user_id: Any) -> dict[str, Any]:
        if not user_id:
            raise ValueError("One of the filters: user_id, agent_id or run_id is required!")
        hits = [{"memory": r["memory"]} for r in self.records if r["user_id"] == user_id]
        return {"results": hits}


class _StoreMem0V2(_StoreMixin):
    def search(self, query: str, *, top_k: int = 20, filters: Any = None, **kwargs: Any) -> Any:
        if {"user_id", "agent_id", "run_id"} & set(kwargs):
            raise ValueError("Top-level entity parameters are not supported in search().")
        return self._scoped((filters or {}).get("user_id"))


class _StoreMem0V1(_StoreMixin):
    def search(
        self, query: str, *, user_id: str | None = None, limit: int = 100, filters: Any = None
    ) -> Any:
        return self._scoped(user_id or (filters or {}).get("user_id"))


def _curated_hit(memory_id: str, target: str, entry: str) -> dict[str, Any]:
    """A search hit for the record a curated mirror wrote for *entry*."""
    from agentos.memory.providers.mem0_provider import _curated_tags

    return {
        "id": memory_id,
        "memory": f"[curated {target}] {entry}",
        "metadata": _curated_tags(target, entry),
    }


def _make_config(**mem0_overrides: Any) -> MemoryConfig:
    cfg = MemoryConfig()
    cfg.provider.name = "mem0"
    for key, value in mem0_overrides.items():
        setattr(cfg.provider.mem0, key, value)
    return cfg


def _make_provider(
    tmp_path: Path,
    *,
    client: _FakeMem0Client | None = None,
    captured_config: list[dict[str, Any]] | None = None,
    captured_telemetry: list[str | None] | None = None,
    **mem0_overrides: Any,
) -> Mem0Provider:
    fake = client or _FakeMem0Client()

    def _factory(config_dict: dict[str, Any]) -> _FakeMem0Client:
        # The factory runs AFTER initialize sets the telemetry env; capture
        # both so tests can assert ordering and dict shape.
        if captured_config is not None:
            captured_config.append(config_dict)
        if captured_telemetry is not None:
            captured_telemetry.append(os.environ.get("MEM0_TELEMETRY"))
        return fake

    return Mem0Provider(
        memory_config=_make_config(**mem0_overrides),
        agent_state_dir=tmp_path,
        client_factory=_factory,
    )


# -- is_available -----------------------------------------------------------


def test_is_available_false_without_mem0_installed(monkeypatch: Any) -> None:
    """mem0 absent (faked, so this holds with the extra installed too) -> False."""
    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a, **k: None if name == "mem0" else real_find_spec(name, *a, **k),
    )
    provider = Mem0Provider(memory_config=_make_config(), agent_state_dir=Path("/tmp/x"))
    assert provider.is_available() is False


def test_is_available_true_when_find_spec_faked(monkeypatch: Any) -> None:
    """A faked ``find_spec`` flips availability without constructing a client."""
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name: object() if name == "mem0" else None
    )
    provider = Mem0Provider(memory_config=_make_config(), agent_state_dir=Path("/tmp/x"))
    assert provider.is_available() is True


def test_is_available_does_not_build_client(tmp_path: Path, monkeypatch: Any) -> None:
    """Availability probing must not touch the client factory (no network)."""
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    built = []
    provider = Mem0Provider(
        memory_config=_make_config(),
        agent_state_dir=tmp_path,
        client_factory=lambda cfg: built.append(cfg),  # type: ignore[func-returns-value]
    )
    provider.is_available()
    assert built == []


# -- initialize: telemetry ordering + config dict shape ---------------------


@pytest.mark.asyncio
async def test_initialize_sets_telemetry_before_factory(tmp_path: Path) -> None:
    captured_telemetry: list[str | None] = []
    provider = _make_provider(tmp_path, captured_telemetry=captured_telemetry)
    await provider.initialize("s1", agent_state_dir=str(tmp_path), platform="cli")
    # The factory observed MEM0_TELEMETRY already set to "false".
    assert captured_telemetry == ["false"]


@pytest.mark.asyncio
async def test_initialize_builds_local_ollama_config_dict(tmp_path: Path) -> None:
    captured: list[dict[str, Any]] = []
    provider = _make_provider(tmp_path, captured_config=captured)
    await provider.initialize("s1", agent_state_dir=str(tmp_path))

    assert len(captured) == 1
    cfg = captured[0]

    assert cfg["llm"] == {
        "provider": "ollama",
        "config": {"model": "qwen3:4b", "ollama_base_url": "http://localhost:11434"},
    }
    assert cfg["embedder"] == {
        "provider": "ollama",
        "config": {"model": "embeddinggemma", "ollama_base_url": "http://localhost:11434"},
    }
    vs = cfg["vector_store"]
    assert vs["provider"] == "qdrant"
    assert vs["config"]["path"] == str(tmp_path / "mem0")
    assert vs["config"]["embedding_model_dims"] == 768
    assert vs["config"]["collection_name"]


@pytest.mark.asyncio
async def test_initialize_honors_settings_overrides(tmp_path: Path) -> None:
    captured: list[dict[str, Any]] = []
    store = tmp_path / "custom-store"
    provider = _make_provider(
        tmp_path,
        captured_config=captured,
        llm_model="llama3.1:8b",
        llm_base_url="http://ollama:11434",
        embedder_model="nomic-embed-text",
        embedder_base_url="http://embed:11434",
        vector_store_path=str(store),
    )
    await provider.initialize("s1", agent_state_dir=str(tmp_path))
    cfg = captured[0]
    assert cfg["llm"]["config"] == {
        "model": "llama3.1:8b",
        "ollama_base_url": "http://ollama:11434",
    }
    assert cfg["embedder"]["config"] == {
        "model": "nomic-embed-text",
        "ollama_base_url": "http://embed:11434",
    }
    assert cfg["vector_store"]["config"]["path"] == str(store)


@pytest.mark.asyncio
async def test_initialize_uses_agent_identity_as_user_id(tmp_path: Path) -> None:
    client = _FakeMem0Client()
    provider = _make_provider(tmp_path, client=client)
    await provider.initialize("s1", agent_state_dir=str(tmp_path), agent_identity="coder")
    await provider.sync_turn("hi", "hey")
    assert client.add_calls[0]["user_id"] == "coder"


@pytest.mark.asyncio
async def test_initialize_user_id_defaults_to_main(tmp_path: Path) -> None:
    client = _FakeMem0Client()
    provider = _make_provider(tmp_path, client=client)
    await provider.initialize("s1", agent_state_dir=str(tmp_path))
    await provider.sync_turn("hi", "hey")
    assert client.add_calls[0]["user_id"] == "main"


# -- sync_turn --------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_turn_adds_both_messages(tmp_path: Path) -> None:
    client = _FakeMem0Client()
    provider = _make_provider(tmp_path, client=client)
    await provider.initialize("s1", agent_state_dir=str(tmp_path), agent_identity="a")
    await provider.sync_turn("what is my name?", "You are Alice.")

    assert len(client.add_calls) == 1
    call = client.add_calls[0]
    assert call["messages"] == [
        {"role": "user", "content": "what is my name?"},
        {"role": "assistant", "content": "You are Alice."},
    ]
    assert call["user_id"] == "a"


@pytest.mark.asyncio
async def test_sync_turn_truncates_megabyte_contents(tmp_path: Path) -> None:
    client = _FakeMem0Client()
    provider = _make_provider(tmp_path, client=client)
    await provider.initialize("s1", agent_state_dir=str(tmp_path))
    big = "x" * 2_000_000
    await provider.sync_turn(big, big)
    msgs = client.add_calls[0]["messages"]
    assert len(msgs[0]["content"]) <= 8192
    assert len(msgs[1]["content"]) <= 8192


@pytest.mark.asyncio
async def test_sync_turn_noop_before_initialize(tmp_path: Path) -> None:
    client = _FakeMem0Client()
    provider = _make_provider(tmp_path, client=client)
    # No initialize → no client → silently skip (never raise on the turn path).
    await provider.sync_turn("hi", "hey")
    assert client.add_calls == []


# -- prefetch ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_prefetch_formats_dict_result_shape(tmp_path: Path) -> None:
    client = _FakeMem0Client(
        search_result={"results": [{"memory": "User is Alice."}, {"memory": "Likes tea."}]}
    )
    provider = _make_provider(tmp_path, client=client)
    await provider.initialize("s1", agent_state_dir=str(tmp_path), agent_identity="a")
    out = await provider.prefetch("who am i?")
    assert out == "- User is Alice.\n- Likes tea."
    assert client.search_calls == [{"query": "who am i?", "filters": {"user_id": "a"}, "top_k": 5}]


@pytest.mark.asyncio
async def test_prefetch_uses_keyword_scope_on_legacy_mem0(tmp_path: Path) -> None:
    client = _LegacySearchMem0Client(search_result={"results": [{"memory": "User is Alice."}]})
    provider = _make_provider(tmp_path, client=client)
    await provider.initialize("s1", agent_state_dir=str(tmp_path), agent_identity="a")
    assert await provider.prefetch("who am i?") == "- User is Alice."
    assert client.search_calls == [
        {"query": "who am i?", "user_id": "a", "limit": 5, "filters": None}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("store_cls", [_StoreMem0V2, _StoreMem0V1], ids=["mem0-2x", "mem0-1x"])
async def test_prefetch_recalls_only_this_agents_memories(
    tmp_path: Path, store_cls: type[_StoreMixin]
) -> None:
    """Both API shapes: each agent recalls its own turns and never another's."""
    store = store_cls()
    alice = Mem0Provider(
        memory_config=_make_config(), agent_state_dir=tmp_path, client_factory=lambda _: store
    )
    bob = Mem0Provider(
        memory_config=_make_config(), agent_state_dir=tmp_path, client_factory=lambda _: store
    )
    await alice.initialize("s1", agent_identity="alice")
    await bob.initialize("s2", agent_identity="bob")

    await alice.sync_turn("I live in Lisbon.", "Noted.")

    assert await alice.prefetch("where do I live?") == "- I live in Lisbon.\n- Noted."
    assert await bob.prefetch("where do I live?") == ""


@pytest.mark.asyncio
async def test_search_shape_is_detected_once(tmp_path: Path, monkeypatch: Any) -> None:
    from agentos.memory.providers import mem0_provider

    calls: list[Any] = []
    real = mem0_provider._detect_search_shape

    def _counting(client: Any) -> Any:
        calls.append(client)
        return real(client)

    monkeypatch.setattr(mem0_provider, "_detect_search_shape", _counting)
    client = _FakeMem0Client()
    provider = _make_provider(tmp_path, client=client)
    await provider.initialize("s1", agent_state_dir=str(tmp_path))
    for _ in range(3):
        await provider.prefetch("q")
    await provider.on_memory_write("remove", "memory", "", {"old_entry": "x"})

    assert calls == [client]
    assert len(client.search_calls) == 4


def test_detect_search_shape_per_mem0_api() -> None:
    from agentos.memory.providers.mem0_provider import _detect_search_shape

    modern = _detect_search_shape(_FakeMem0Client())
    assert (modern.scope_in_filters, modern.limit_param) == (True, "top_k")
    legacy = _detect_search_shape(_LegacySearchMem0Client())
    assert (legacy.scope_in_filters, legacy.limit_param) == (False, "limit")

    class _KwargsOnly:
        def search(self, query: str, **kwargs: Any) -> Any:
            return []

    # An opaque ``**kwargs`` signature gets the current (filters) API.
    opaque = _detect_search_shape(_KwargsOnly())
    assert (opaque.scope_in_filters, opaque.limit_param) == (True, "top_k")


@pytest.mark.asyncio
async def test_prefetch_scope_rejection_is_a_warning_with_a_hint(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A rejected scope must not pass for "no memories" without saying so."""
    from unittest.mock import MagicMock

    from agentos.memory.providers import mem0_provider

    class _Rejecting(_FakeMem0Client):
        def search(self, query: str, **kwargs: Any) -> Any:
            raise ValueError("filters must contain at least one of: user_id, agent_id, run_id.")

    mock_logger = MagicMock()
    monkeypatch.setattr(mem0_provider, "logger", mock_logger)
    provider = _make_provider(tmp_path, client=_Rejecting())
    await provider.initialize("s1", agent_state_dir=str(tmp_path))

    assert await provider.prefetch("q") == ""

    mock_logger.warning.assert_called_once()
    event = mock_logger.warning.call_args.args[0]
    fields = mock_logger.warning.call_args.kwargs
    assert event == "mem0_provider.prefetch_failed"
    assert "filters" in fields["search_shape"]
    assert "mem0_version" in fields
    assert "recall stays empty" in fields["hint"]


@pytest.mark.asyncio
async def test_prefetch_call_binds_to_installed_mem0_signature(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Offline: the real installed ``mem0.Memory`` accepts the exact call we make.

    No client is built (no LLM, embedder, or vector store). A recorder carrying
    the real ``search`` / ``add`` signatures stands in for the client, and each
    recorded call must bind to a NAMED parameter -- mem0 2.x's ``search`` takes
    ``**kwargs``, which is exactly where a stray ``user_id`` or ``limit`` would
    otherwise slip through unnoticed.
    """
    import inspect

    # mem0 creates its config dir at import time; keep it out of $HOME.
    monkeypatch.setenv("MEM0_DIR", str(tmp_path / "mem0-home"))
    monkeypatch.setenv("MEM0_TELEMETRY", "false")
    mem0 = pytest.importorskip("mem0")
    real_search = inspect.signature(mem0.Memory.search)
    real_add = inspect.signature(mem0.Memory.add)
    calls: dict[str, list[dict[str, Any]]] = {"search": [], "add": []}

    class _Recorder:
        def search(self, *args: Any, **kwargs: Any) -> Any:
            calls["search"].append(kwargs)
            return {"results": [{"memory": "recalled"}]}

        def add(self, *args: Any, **kwargs: Any) -> Any:
            calls["add"].append(kwargs)
            return {"results": []}

    recorder = _Recorder()
    # Present the real signatures (minus ``self``) for shape detection.
    for name, sig in (("search", real_search), ("add", real_add)):
        method = getattr(recorder, name)
        params = list(sig.parameters.values())[1:]
        method.__func__.__signature__ = sig.replace(parameters=params)

    provider = Mem0Provider(
        memory_config=_make_config(), agent_state_dir=tmp_path, client_factory=lambda _: recorder
    )
    await provider.initialize("s1", agent_identity="alice")
    assert await provider.prefetch("who am i?") == "- recalled"
    await provider.sync_turn("hi", "hey")
    await provider.on_memory_write("add", "user", "Alice prefers dark mode.")

    def _assert_binds(sig: inspect.Signature, kwargs: dict[str, Any], *args: Any) -> None:
        bound = sig.bind(None, *args, **kwargs)
        stray = {
            name: value
            for name, value in bound.arguments.items()
            if sig.parameters[name].kind is inspect.Parameter.VAR_KEYWORD and value
        }
        assert stray == {}, f"mem0 would not honor {stray}"

    assert calls["search"]
    for kwargs in calls["search"]:
        _assert_binds(real_search, kwargs, "who am i?")
        scope = kwargs.get("filters") or {}
        assert kwargs.get("user_id", scope.get("user_id")) == "alice"
    assert len(calls["add"]) == 2
    for kwargs in calls["add"]:
        _assert_binds(real_add, kwargs)


@pytest.mark.asyncio
async def test_prefetch_formats_bare_list_result_shape(tmp_path: Path) -> None:
    """Older mem0 returned a bare list of hit dicts."""
    client = _FakeMem0Client(search_result=[{"memory": "Fact one."}, {"memory": "Fact two."}])
    provider = _make_provider(tmp_path, client=client)
    await provider.initialize("s1", agent_state_dir=str(tmp_path))
    out = await provider.prefetch("q")
    assert out == "- Fact one.\n- Fact two."


@pytest.mark.asyncio
async def test_prefetch_empty_result_returns_empty_string(tmp_path: Path) -> None:
    client = _FakeMem0Client(search_result={"results": []})
    provider = _make_provider(tmp_path, client=client)
    await provider.initialize("s1", agent_state_dir=str(tmp_path))
    assert await provider.prefetch("q") == ""


@pytest.mark.asyncio
async def test_prefetch_noop_before_initialize(tmp_path: Path) -> None:
    client = _FakeMem0Client()
    provider = _make_provider(tmp_path, client=client)
    assert await provider.prefetch("q") == ""
    assert client.search_calls == []


# -- on_memory_write --------------------------------------------------------


@pytest.mark.asyncio
async def test_on_memory_write_mirrors_add_action(tmp_path: Path) -> None:
    client = _FakeMem0Client()
    provider = _make_provider(tmp_path, client=client)
    await provider.initialize("s1", agent_state_dir=str(tmp_path), agent_identity="a")
    await provider.on_memory_write("add", "user", "Alice prefers dark mode.")

    assert len(client.add_calls) == 1
    call = client.add_calls[0]
    assert call["messages"] == [
        {"role": "user", "content": "[curated user] Alice prefers dark mode."}
    ]
    assert call["user_id"] == "a"
    # Stored verbatim and tagged so a later replace/remove can find it.
    assert call["infer"] is False
    assert call["metadata"]["agentos_curated_target"] == "user"
    assert len(call["metadata"]["agentos_curated_sha256"]) == 64


@pytest.mark.asyncio
async def test_on_memory_write_remove_deletes_only_the_tagged_record(tmp_path: Path) -> None:
    client = _FakeMem0Client(
        search_result={
            "results": [
                _curated_hit("m-old", "memory", "deploys go through staging"),
                # Same text, other target: not this entry's mirror.
                _curated_hit("m-user", "user", "deploys go through staging"),
                # A turn extraction with no curated tags is never deleted.
                {"id": "m-turn", "memory": "deploys go through staging"},
                _curated_hit("m-other", "memory", "something else"),
            ]
        }
    )
    provider = _make_provider(tmp_path, client=client)
    await provider.initialize("s1", agent_state_dir=str(tmp_path), agent_identity="a")

    await provider.on_memory_write(
        "remove",
        "memory",
        "",
        {"old_text": "staging", "old_entry": "deploys go through staging"},
    )

    assert client.delete_calls == ["m-old"]
    assert client.add_calls == []
    assert client.search_calls == [
        {
            "query": "[curated memory] deploys go through staging",
            "filters": {"user_id": "a"},
            "top_k": 20,
        }
    ]


@pytest.mark.asyncio
async def test_on_memory_write_replace_deletes_old_then_adds_new(tmp_path: Path) -> None:
    client = _FakeMem0Client(
        search_result={"results": [_curated_hit("m-old", "memory", "uses npm")]}
    )
    provider = _make_provider(tmp_path, client=client)
    await provider.initialize("s1", agent_state_dir=str(tmp_path))

    await provider.on_memory_write(
        "replace", "memory", "uses pnpm", {"old_text": "npm", "old_entry": "uses npm"}
    )

    assert client.delete_calls == ["m-old"]
    assert [c["messages"][0]["content"] for c in client.add_calls] == [
        "[curated memory] uses pnpm"
    ]


@pytest.mark.asyncio
async def test_on_memory_write_without_old_entry_never_deletes(tmp_path: Path) -> None:
    """A bare ``old_text`` substring is not enough to pick a record to delete."""
    client = _FakeMem0Client(
        search_result={"results": [_curated_hit("m-old", "memory", "uses npm")]}
    )
    provider = _make_provider(tmp_path, client=client)
    await provider.initialize("s1", agent_state_dir=str(tmp_path))

    await provider.on_memory_write("remove", "memory", "", {"old_text": "npm"})
    await provider.on_memory_write("replace", "memory", "uses pnpm", {"old_text": "npm"})

    assert client.search_calls == [] and client.delete_calls == []
    assert len(client.add_calls) == 1


@pytest.mark.asyncio
async def test_on_memory_write_remove_uses_legacy_search_shape(tmp_path: Path) -> None:
    client = _LegacySearchMem0Client(
        search_result=[_curated_hit("m-old", "memory", "uses npm")]
    )
    provider = _make_provider(tmp_path, client=client)
    await provider.initialize("s1", agent_state_dir=str(tmp_path), agent_identity="a")

    await provider.on_memory_write("remove", "memory", "", {"old_entry": "uses npm"})

    assert client.delete_calls == ["m-old"]
    # Detected up front: one search in the keyword shape, no failed first try.
    assert client.search_calls == [
        {"query": "[curated memory] uses npm", "user_id": "a", "limit": 20, "filters": None}
    ]


@pytest.mark.asyncio
async def test_on_memory_write_delete_failure_is_swallowed(tmp_path: Path) -> None:
    class _Boom(_FakeMem0Client):
        def delete(self, memory_id: str) -> None:
            raise RuntimeError("vector store down")

    client = _Boom(search_result={"results": [_curated_hit("m-old", "memory", "uses npm")]})
    provider = _make_provider(tmp_path, client=client)
    await provider.initialize("s1", agent_state_dir=str(tmp_path))

    await provider.on_memory_write(
        "replace", "memory", "uses pnpm", {"old_entry": "uses npm"}
    )

    # The failed delete does not stop the new entry from being mirrored.
    assert len(client.add_calls) == 1


# -- static surfaces --------------------------------------------------------


def test_name_is_mem0(tmp_path: Path) -> None:
    provider = _make_provider(tmp_path)
    assert provider.name == "mem0"


def test_get_tool_schemas_is_empty(tmp_path: Path) -> None:
    provider = _make_provider(tmp_path)
    assert provider.get_tool_schemas() == []


def test_system_prompt_block_is_one_line(tmp_path: Path) -> None:
    provider = _make_provider(tmp_path)
    block = provider.system_prompt_block()
    assert block
    assert "\n" not in block.strip()


@pytest.mark.asyncio
async def test_shutdown_and_session_end_are_noops(tmp_path: Path) -> None:
    provider = _make_provider(tmp_path)
    await provider.initialize("s1", agent_state_dir=str(tmp_path))
    await provider.shutdown()
    await provider.on_session_end([])


# -- registry integration ---------------------------------------------------


def test_registry_create_provider_returns_mem0(monkeypatch: Any, tmp_path: Path) -> None:
    """With ``find_spec`` faked, the registry resolves the real provider.

    This exercises the removed ``type: ignore[import]`` in registry.py: the
    module now exists and imports cleanly.
    """
    from agentos.memory.providers.registry import create_provider

    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name: object() if name == "mem0" else None
    )
    provider = create_provider(
        "mem0",
        memory_config=_make_config(),
        agent_state_dir=tmp_path,
    )
    assert provider is not None
    assert provider.name == "mem0"
    assert provider.is_available() is True
