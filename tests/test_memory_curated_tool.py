"""The single `memory` tool (curated store entry point) + memory_save redirect."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agentos.tools.builtin.memory_tools import create_memory_tools
from agentos.tools.registry import ToolRegistry
from agentos.tools.types import ToolError


class _FakeMemorySaveStore:
    """Minimal store double satisfying memory_save's indexing calls."""

    async def index_file(self, *, path: str, content: str, source) -> int:
        return 1 if content else 0

    async def remove_file(self, path: str) -> None:
        return None

    async def total_size(self) -> int:
        return 0


@pytest.fixture()
def memory_tools_fixture(tmp_path):
    """Build create_memory_tools against a tmp workspace; return name -> handler."""
    registry = ToolRegistry()
    create_memory_tools(
        stores=_FakeMemorySaveStore(),
        retrievers=SimpleNamespace(),
        memory_dir=str(tmp_path),
        registry=registry,
    )
    return {name: registry.get(name).handler for name in registry.list_names()}


async def test_memory_tool_add_and_remove(memory_tools_fixture):
    tools = memory_tools_fixture
    result = json.loads(await tools["memory"](action="add", content="fact A"))
    assert result["success"] is True
    result = json.loads(await tools["memory"](action="remove", old_text="fact A"))
    assert result["success"] is True


async def test_memory_tool_batch_operations(memory_tools_fixture):
    tools = memory_tools_fixture
    result = json.loads(
        await tools["memory"](
            target="memory",
            operations=[{"action": "add", "content": "a"}, {"action": "add", "content": "b"}],
        )
    )
    assert result["success"] is True
    assert result["entry_count"] == 2


async def test_memory_tool_null_target_defaults_to_memory(memory_tools_fixture):
    tools = memory_tools_fixture
    result = json.loads(await tools["memory"](action="add", target=None, content="x"))
    assert result["success"] is True
    assert result["target"] == "memory"


async def test_memory_tool_invalid_target_returns_error(memory_tools_fixture):
    tools = memory_tools_fixture
    result = json.loads(await tools["memory"](action="add", target="bogus", content="x"))
    assert result["success"] is False
    assert "bogus" in result["error"]


async def test_memory_tool_missing_old_text_returns_inventory(memory_tools_fixture):
    tools = memory_tools_fixture
    await tools["memory"](action="add", content="only entry")
    result = json.loads(await tools["memory"](action="replace", content="new"))
    assert result["success"] is False
    assert result["current_entries"] == ["only entry"]


async def test_memory_tool_missing_old_text_on_remove_returns_inventory(memory_tools_fixture):
    tools = memory_tools_fixture
    await tools["memory"](action="add", content="only entry")
    result = json.loads(await tools["memory"](action="remove"))
    assert result["success"] is False
    assert result["current_entries"] == ["only entry"]


async def test_memory_tool_missing_old_text_error_reports_usage(memory_tools_fixture):
    # Hermes parity: the missing-old_text error carries the char-usage string so
    # the model sees how full the store is before it reissues the call.
    tools = memory_tools_fixture
    await tools["memory"](action="add", content="only entry")
    result = json.loads(await tools["memory"](action="replace", content="new"))
    assert result["usage"] == "10/4,000"  # len("only entry") == 10, default limit


async def test_memory_tool_unknown_action_returns_error(memory_tools_fixture):
    tools = memory_tools_fixture
    result = json.loads(await tools["memory"](action="bogus", content="x"))
    assert result["success"] is False
    assert "bogus" in result["error"]


async def test_memory_tool_add_requires_content(memory_tools_fixture):
    tools = memory_tools_fixture
    result = json.loads(await tools["memory"](action="add"))
    assert result["success"] is False


async def test_memory_tool_operations_not_a_list_returns_error(memory_tools_fixture):
    tools = memory_tools_fixture
    result = json.loads(await tools["memory"](action="add", operations="nope"))
    assert result["success"] is False


async def test_memory_tool_user_target_writes_user_md(memory_tools_fixture, tmp_path):
    tools = memory_tools_fixture
    result = json.loads(await tools["memory"](action="add", target="user", content="Name: Key"))
    assert result["success"] is True
    assert result["target"] == "user"
    assert "Name: Key" in (tmp_path / "USER.md").read_text(encoding="utf-8")


async def test_memory_save_rejects_memory_md(memory_tools_fixture):
    tools = memory_tools_fixture
    with pytest.raises(ToolError, match="managed by the `memory` tool"):
        await tools["memory_save"](path="MEMORY.md", content="x", mode="replace")


async def test_memory_save_rejects_memory_md_path_variants(memory_tools_fixture):
    tools = memory_tools_fixture
    # "./MEMORY.md" normalizes to the same target as "MEMORY.md" and must be
    # rejected identically -- not silently accepted via path spelling.
    with pytest.raises(ToolError, match="managed by the `memory` tool"):
        await tools["memory_save"](path="./MEMORY.md", content="x", mode="append")


async def test_memory_save_still_accepts_memory_notes(memory_tools_fixture, tmp_path):
    tools = memory_tools_fixture
    result = await tools["memory_save"](path="memory/notes.md", content="daily note")
    assert "Saved to memory/notes.md" in result
    assert "daily note" in (tmp_path / "memory" / "notes.md").read_text(encoding="utf-8")


async def test_memory_tool_picks_up_budget_change_without_restart(tmp_path):
    """config.patch REPLACES config.memory with a new MemoryConfig instance
    (``_update_config_in_place`` in rpc_config.py does a top-level
    ``setattr(old, "memory", getattr(new, "memory"))`` -- it does not mutate
    the old MemoryConfig's fields in place). A closure that captured the
    ``memory_config`` sub-object directly would be watching an orphaned
    instance forever after the first patch. Only the ROOT ``GatewayConfig``
    object survives a patch and keeps its identity -- so this test drives
    the REAL mutation path (``_update_config_in_place`` against a real
    ``GatewayConfig``) rather than mutating a stand-in sub-object's
    attributes directly, and would fail against the round-1 code that read
    a captured ``memory_config`` sub-object instead of the live root.
    """
    from agentos.gateway.config import GatewayConfig
    from agentos.gateway.rpc_config import _update_config_in_place

    registry = ToolRegistry()
    root_config = GatewayConfig(
        config_path=str(tmp_path / "c.toml"),
        memory={"curated_memory_char_limit": 200, "curated_user_char_limit": 200},
    )
    create_memory_tools(
        stores=_FakeMemorySaveStore(),
        retrievers=SimpleNamespace(),
        memory_dir=str(tmp_path),
        registry=registry,
        memory_config=root_config.memory,
        config_root=root_config,
    )
    tools = {name: registry.get(name).handler for name in registry.list_names()}

    # Fill close to the 200-char limit so a subsequent add overflows it.
    filler = "x" * 190
    result = json.loads(await tools["memory"](action="add", content=filler))
    assert result["success"] is True

    over_budget = json.loads(await tools["memory"](action="add", content="y" * 50))
    assert over_budget["success"] is False

    # Apply the REAL mutation semantics config.patch uses: build a fresh
    # GatewayConfig from a mutated dump and copy fields into the root via
    # _update_config_in_place -- this REPLACES root_config.memory with a new
    # MemoryConfig instance, orphaning any closure that captured the old one.
    mutated_dump = root_config.model_dump()
    mutated_dump["memory"]["curated_memory_char_limit"] = 4000
    mutated_dump["memory"]["curated_user_char_limit"] = 2000
    old_memory_instance = root_config.memory
    _update_config_in_place(root_config, GatewayConfig(**mutated_dump))
    assert root_config.memory is not old_memory_instance, (
        "test setup invariant: config.patch must replace config.memory with a "
        "new instance, or this test isn't exercising the real bug"
    )

    now_fits = json.loads(await tools["memory"](action="add", content="y" * 50))
    assert now_fits["success"] is True


def _patched(root_config, **memory_fields):
    """Apply config.patch's real mutation: replace root.memory with a new instance."""
    from agentos.gateway.config import GatewayConfig
    from agentos.gateway.rpc_config import _update_config_in_place

    dump = root_config.model_dump()
    dump["memory"].update(memory_fields)
    old = root_config.memory
    _update_config_in_place(root_config, GatewayConfig(**dump))
    assert root_config.memory is not old, "setup: patch must replace config.memory"


def _live_tools(tmp_path, **memory_fields):
    from agentos.gateway.config import GatewayConfig

    registry = ToolRegistry()
    root_config = GatewayConfig(config_path=str(tmp_path / "c.toml"), memory=memory_fields)
    create_memory_tools(
        stores=_FakeMemorySaveStore(),
        retrievers=SimpleNamespace(),
        memory_dir=str(tmp_path),
        registry=registry,
        memory_config=root_config.memory,
        config_root=root_config,
    )
    return root_config, {name: registry.get(name).handler for name in registry.list_names()}


async def test_memory_save_applies_a_tightened_file_size_limit_without_restart(tmp_path):
    """#2972: memory_save's size limits read the captured ``memory_config``,
    which config.patch orphans -- so a tightened limit never took effect."""
    root_config, tools = _live_tools(tmp_path, max_file_size_kb=1000)
    await tools["memory_save"](path="memory/a.md", content="x" * 2048, mode="replace")

    _patched(root_config, max_file_size_kb=1)

    with pytest.raises(ToolError, match="per-file limit"):
        await tools["memory_save"](path="memory/b.md", content="x" * 2048, mode="replace")


async def test_memory_save_enforces_a_file_count_enabled_after_boot(tmp_path):
    """A cap switched on after boot was never enforced: the captured config
    still said ``max_files = 0``."""
    root_config, tools = _live_tools(tmp_path, max_files=0)
    await tools["memory_save"](path="memory/one.md", content="first", mode="replace")

    _patched(root_config, max_files=1)

    with pytest.raises(ToolError, match="max file count"):
        await tools["memory_save"](path="memory/two.md", content="second", mode="replace")


async def test_memory_save_honours_a_relaxed_total_limit_without_restart(tmp_path):
    """The other direction: a raised limit must stop refusing writes."""

    class _SizedStore(_FakeMemorySaveStore):
        async def total_size(self) -> int:
            return 50 * 1024

    from agentos.gateway.config import GatewayConfig

    registry = ToolRegistry()
    root_config = GatewayConfig(
        config_path=str(tmp_path / "c.toml"), memory={"max_total_size_kb": 10}
    )
    create_memory_tools(
        stores=_SizedStore(),
        retrievers=SimpleNamespace(),
        memory_dir=str(tmp_path),
        registry=registry,
        memory_config=root_config.memory,
        config_root=root_config,
    )
    tools = {name: registry.get(name).handler for name in registry.list_names()}
    with pytest.raises(ToolError, match="total memory limit"):
        await tools["memory_save"](path="memory/n.md", content="note", mode="replace")

    _patched(root_config, max_total_size_kb=10_000)

    result = await tools["memory_save"](path="memory/n.md", content="note", mode="replace")
    assert "Saved to memory/n.md" in result
