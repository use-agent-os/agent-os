"""``models.list`` for the providers whose catalog is public (OpenCAP, Surplus).

The gateway fetches the active provider's catalog once, at boot. Two ways that
copy stops matching the provider the Settings pages ask about:

* the provider is switched at runtime (Providers → Surplus): the selector is
  hot-swapped but the catalog still holds the old provider's models;
* the boot fetch failed (Surplus' catalog is ~470 KB / 500+ models and boot
  waits only 5 s).

Both used to return an empty list — the Router tier menus fell back to the five
offline profile models and the Providers form waited on the slow key probe.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from agentos.gateway import rpc_models
from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.provider.model_catalog import ModelCatalog

_SURPLUS_ROWS = [
    {"id": "deepseek-v4.1-flash", "name": "DeepSeek V4.1 Flash", "context_length": 128000},
    {
        "id": "glm-5.3-flash",
        "name": "GLM 5.3 Flash",
        "context_length": 128000,
        "supported_features": ["vision"],
    },
    {"id": "claude-opus-5.5", "name": "Claude Opus 5.5", "context_length": 200000},
]
_OPENCAP_ROWS = [
    {"id": "deepseek-v4.1-flash", "context_length": 64000},
    {"id": "claude-haiku-5.5", "context_length": 200000},
]


@pytest.fixture(autouse=True)
def _fresh_public_cache() -> Any:
    rpc_models.reset_public_catalog_cache()
    yield
    rpc_models.reset_public_catalog_cache()


@pytest.fixture
def surplus_fetches(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Serve Surplus' public catalog from memory and count the upstream fetches."""

    calls: list[str] = []

    async def fake_fetch_surplus(self: ModelCatalog, proxy: str = "") -> dict[str, Any]:
        calls.append(proxy)
        await asyncio.sleep(0.01)
        self._populate_from_surplus(_SURPLUS_ROWS)
        return {"data": _SURPLUS_ROWS}

    monkeypatch.setattr(ModelCatalog, "fetch_surplus", fake_fetch_surplus)
    return calls


def _opencap_catalog() -> ModelCatalog:
    catalog = ModelCatalog()
    catalog._populate_from_opencap(_OPENCAP_ROWS)
    return catalog


def _ctx(active: str, catalog: ModelCatalog) -> RpcContext:
    ctx = RpcContext(conn_id="test")
    ctx.config = SimpleNamespace(llm=SimpleNamespace(provider=active, proxy=""))
    ctx.model_catalog = catalog
    return ctx


def _list(params: dict, ctx: RpcContext) -> list[dict]:
    result = asyncio.run(get_dispatcher().dispatch("r1", "models.list", params, ctx))
    assert result.error is None, result.error
    return result.payload or []


def test_switched_to_surplus_at_runtime_lists_surplus_models(surplus_fetches: list[str]) -> None:
    """Boot catalog is OpenCAP's; the provider was hot-swapped to Surplus."""

    catalog = _opencap_catalog()
    ctx = _ctx("surplus", catalog)

    ids = [m["id"] for m in _list({"provider": "surplus"}, ctx)]

    assert ids == ["deepseek-v4.1-flash", "glm-5.3-flash", "claude-opus-5.5"]
    assert all(m["provider"] == "surplus" for m in _list({"provider": "surplus"}, ctx))


def test_switch_heals_the_runtime_catalog(surplus_fetches: list[str]) -> None:
    """Turns resolve windows/capabilities from this catalog; it must follow the switch."""

    catalog = _opencap_catalog()
    ctx = _ctx("surplus", catalog)

    _list({"provider": "surplus"}, ctx)

    assert {m.provider for m in catalog.list_models()} == {"surplus"}
    assert catalog.resolve_context_window("deepseek-v4.1-flash", "surplus") == 128000


def test_failed_boot_fetch_is_recovered_on_first_list(surplus_fetches: list[str]) -> None:
    catalog = ModelCatalog()  # boot fetch timed out: nothing cached
    ctx = _ctx("surplus", catalog)

    assert len(_list({}, ctx)) == 3
    assert len(catalog) == 3


def test_vision_filter_reads_the_public_catalog_flags(surplus_fetches: list[str]) -> None:
    ctx = _ctx("surplus", _opencap_catalog())

    ids = [m["id"] for m in _list({"provider": "surplus", "capabilities": ["vision"]}, ctx)]

    assert ids == ["glm-5.3-flash"]


def test_inactive_public_provider_is_listed_without_touching_the_runtime_catalog(
    surplus_fetches: list[str],
) -> None:
    """Browsing the Surplus form while OpenCAP is active must not clobber OpenCAP's metadata."""

    catalog = _opencap_catalog()
    ctx = _ctx("opencap", catalog)

    ids = [m["id"] for m in _list({"provider": "surplus"}, ctx)]

    assert len(ids) == 3
    assert {m.provider for m in catalog.list_models()} == {"opencap"}
    assert catalog.resolve_context_window("deepseek-v4.1-flash", "opencap") == 64000


def test_concurrent_lists_share_one_upstream_fetch(surplus_fetches: list[str]) -> None:
    """The Router pane asks for text and vision models at once; Providers asks too."""

    ctx = _ctx("opencap", _opencap_catalog())

    async def burst() -> list[Any]:
        dispatcher = get_dispatcher()
        return await asyncio.gather(
            dispatcher.dispatch("a", "models.list", {"provider": "surplus"}, ctx),
            dispatcher.dispatch(
                "b", "models.list", {"provider": "surplus", "capabilities": ["vision"]}, ctx
            ),
            dispatcher.dispatch("c", "models.list", {"provider": "surplus"}, ctx),
        )

    results = asyncio.run(burst())

    assert [len(r.payload) for r in results] == [3, 1, 3]
    assert len(surplus_fetches) == 1


def test_snapshot_is_reused_until_it_expires(
    surplus_fetches: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx("opencap", _opencap_catalog())
    now = [1000.0]
    monkeypatch.setattr(rpc_models, "_monotonic", lambda: now[0])

    _list({"provider": "surplus"}, ctx)
    _list({"provider": "surplus"}, ctx)
    assert len(surplus_fetches) == 1

    now[0] += rpc_models.PUBLIC_CATALOG_TTL_S + 1
    _list({"provider": "surplus"}, ctx)
    assert len(surplus_fetches) == 2


def test_failed_refresh_serves_the_stale_snapshot(
    surplus_fetches: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx("opencap", _opencap_catalog())
    now = [1000.0]
    monkeypatch.setattr(rpc_models, "_monotonic", lambda: now[0])
    _list({"provider": "surplus"}, ctx)

    async def down(self: ModelCatalog, proxy: str = "") -> dict[str, Any]:
        raise OSError("catalog unreachable")

    monkeypatch.setattr(ModelCatalog, "fetch_surplus", down)
    now[0] += rpc_models.PUBLIC_CATALOG_TTL_S + 1

    assert len(_list({"provider": "surplus"}, ctx)) == 3


def test_active_catalog_that_already_holds_the_provider_is_used_as_is(
    surplus_fetches: list[str],
) -> None:
    catalog = ModelCatalog()
    catalog._populate_from_surplus(_SURPLUS_ROWS[:1])
    ctx = _ctx("surplus", catalog)

    assert [m["id"] for m in _list({"provider": "surplus"}, ctx)] == ["deepseek-v4.1-flash"]
    assert surplus_fetches == []
