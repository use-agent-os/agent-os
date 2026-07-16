"""Curated MEMORY.md / USER.md system-prompt injection via frozen snapshot."""

import logging
from types import SimpleNamespace

from agentos.engine.runtime import TurnRunner
from agentos.memory.curated import ENTRY_DELIMITER, CuratedMemoryStore


def _runner(tmp_path, **memory_kwargs):
    return TurnRunner(
        provider_selector=None,
        config=SimpleNamespace(
            workspace_dir=str(tmp_path),
            memory=SimpleNamespace(source="workspace", **memory_kwargs),
            tools=SimpleNamespace(profile=None),
        ),
    )


def _prompt_text(assembled) -> str:
    if isinstance(assembled, tuple):
        return "\n\n".join(part for part in assembled if part)
    return assembled or ""


def test_curated_memory_block_with_usage_header_lands_in_prompt(tmp_path):
    (tmp_path / "AGENTS.md").write_text("agents\n", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text(
        f"User deploys with make deploy{ENTRY_DELIMITER}Prod region is us-east-1",
        encoding="utf-8",
    )
    runner = _runner(tmp_path)
    metadata: dict[str, object] = {}

    assembled = runner._assemble_prompt(
        "main", [], session_key="agent:main:auto", prompt_metadata=metadata
    )

    prompt = _prompt_text(assembled)
    assert "MEMORY (your personal notes)" in prompt
    assert "chars]" in prompt  # usage header
    assert "User deploys with make deploy" in prompt
    assert metadata["memory_md_present"] is True


def test_user_block_lands_when_user_md_has_entries(tmp_path):
    (tmp_path / "AGENTS.md").write_text("agents\n", encoding="utf-8")
    (tmp_path / "USER.md").write_text("Name is Ada", encoding="utf-8")
    runner = _runner(tmp_path)

    assembled = runner._assemble_prompt("main", [], session_key="agent:main:auto")

    prompt = _prompt_text(assembled)
    assert "USER PROFILE (who the user is)" in prompt
    assert "Name is Ada" in prompt
    # USER.md must appear exactly once (curated block only, not also the raw
    # workspace-files copy).
    assert prompt.count("Name is Ada") == 1


def test_migration_runs_before_first_load(tmp_path):
    (tmp_path / "AGENTS.md").write_text("agents\n", encoding="utf-8")
    # Free-form MEMORY.md (bullets, no §-delimiter) as written pre-curation.
    (tmp_path / "MEMORY.md").write_text(
        "# Notes\n\n- Prefers dark mode\n- Uses zsh", encoding="utf-8"
    )
    runner = _runner(tmp_path)

    assembled = runner._assemble_prompt("main", [], session_key="agent:main:auto")

    prompt = _prompt_text(assembled)
    assert "Prefers dark mode" in prompt
    assert "Uses zsh" in prompt
    # Migration rewrote the file into §-delimited entries.
    on_disk = (tmp_path / "MEMORY.md").read_text()
    assert "§" in on_disk
    assert on_disk.count("§") == 1  # two entries


def test_no_memory_files_yields_no_memory_block(tmp_path):
    (tmp_path / "AGENTS.md").write_text("agents\n", encoding="utf-8")
    runner = _runner(tmp_path)
    metadata: dict[str, object] = {}

    assembled = runner._assemble_prompt(
        "main", [], session_key="agent:main:auto", prompt_metadata=metadata
    )

    prompt = _prompt_text(assembled)
    assert "MEMORY (your personal notes)" not in prompt
    assert "USER PROFILE" not in prompt
    assert metadata["memory_md_present"] is False


def test_user_block_dropped_when_over_inject_limit(tmp_path):
    (tmp_path / "AGENTS.md").write_text("agents\n", encoding="utf-8")
    # Memory block alone fits comfortably; memory+user together would not.
    (tmp_path / "MEMORY.md").write_text(
        "User deploys with make deploy", encoding="utf-8"
    )
    (tmp_path / "USER.md").write_text("Name is Ada Lovelace", encoding="utf-8")
    runner = _runner(tmp_path, inject_limit=200)
    metadata: dict[str, object] = {}

    memory_md = runner._load_memory_md(tmp_path)

    assert memory_md is not None
    assert "MEMORY (your personal notes)" in memory_md
    assert "User deploys with make deploy" in memory_md
    assert "USER PROFILE (who the user is)" not in memory_md
    assert "Name is Ada Lovelace" not in memory_md
    # Never sliced mid-block: no trailing "..." truncation marker.
    assert "..." not in memory_md

    # Also confirm the assembled prompt still carries the memory block and
    # correctly reports it as present via metadata.
    assembled = runner._assemble_prompt(
        "main", [], session_key="agent:main:auto", prompt_metadata=metadata
    )
    prompt = _prompt_text(assembled)
    assert "MEMORY (your personal notes)" in prompt
    assert metadata["memory_md_present"] is True


def test_default_inject_limit_keeps_full_memory_and_user_blocks_untruncated(tmp_path):
    """At default char budgets (4000 memory + 2000 user), the default
    inject_limit must have enough headroom for both full blocks plus header
    overhead -- the block-boundary truncation (drop-whole-block) path must
    stay unreachable at defaults so a full user profile never starves out.
    """
    (tmp_path / "AGENTS.md").write_text("agents\n", encoding="utf-8")
    # Build entries that fill each store close to its default char limit
    # (4000 for memory, 2000 for user) without exceeding it.
    memory_entries = [f"Memory fact number {i:03d} about the project setup" for i in range(60)]
    user_entries = [f"User preference number {i:03d} noted during onboarding" for i in range(30)]

    memory_content = ENTRY_DELIMITER.join(memory_entries)
    while len(memory_content) < 3800:
        memory_entries.append("Additional filler memory entry to approach the char budget")
        memory_content = ENTRY_DELIMITER.join(memory_entries)
    assert len(memory_content) <= 4000

    user_content = ENTRY_DELIMITER.join(user_entries)
    while len(user_content) < 1800:
        user_entries.append("Additional filler user entry to approach the char budget")
        user_content = ENTRY_DELIMITER.join(user_entries)
    assert len(user_content) <= 2000

    (tmp_path / "MEMORY.md").write_text(memory_content, encoding="utf-8")
    (tmp_path / "USER.md").write_text(user_content, encoding="utf-8")

    # No inject_limit override at all -- exercise the real fallback default
    # (SimpleNamespace has no inject_limit attribute, so runtime.py's
    # ``getattr(..., "inject_limit", 4000)``-style fallback applies).
    runner = _runner(tmp_path)

    memory_md = runner._load_memory_md(tmp_path)

    assert memory_md is not None
    assert "MEMORY (your personal notes)" in memory_md
    assert "USER PROFILE (who the user is)" in memory_md
    assert memory_entries[0] in memory_md
    assert user_entries[0] in memory_md
    # Never sliced mid-block.
    assert "..." not in memory_md


def _fill_to_exact_budget(store: CuratedMemoryStore, target: str, filler: str) -> None:
    """Add entries via the store API until the next one would overflow.

    Builds the target store's on-disk content to sit as close as possible to
    (but never over) its char budget, so tests exercise the true worst case
    the production default must tolerate -- not an approximation with slack.
    """
    i = 0
    while True:
        candidate = f"{filler} {i:04d}"
        entries = store.entries_for(target)
        prospective = len(ENTRY_DELIMITER.join([*entries, candidate]))
        if prospective > store._char_limit(target):
            break
        result = store.add(target, candidate)
        assert result["success"], result
        i += 1


def test_default_inject_limit_fits_both_blocks_at_exact_default_budgets(tmp_path):
    """Both curated stores filled to their EXACT default char budgets (4000
    memory / 2000 user) must both still fit under the default inject_limit.

    Regression test for a shortfall where the joined block (content +
    header/separator overhead for both blocks) came out to 6304 chars at
    full default budgets while inject_limit defaulted to 6200 -- silently
    dropping the USER PROFILE block at default config with no override
    needed to trigger it.
    """
    (tmp_path / "AGENTS.md").write_text("agents\n", encoding="utf-8")

    builder = CuratedMemoryStore(
        memory_dir=tmp_path, memory_char_limit=4000, user_char_limit=2000
    )
    builder.load_from_disk()
    _fill_to_exact_budget(builder, "memory", "Memory fact about the project setup")
    _fill_to_exact_budget(builder, "user", "User preference noted during onboarding")

    assert builder._char_count("memory") <= 4000
    assert builder._char_count("user") <= 2000
    # Sanity: we actually got close to the budget, not just a token amount.
    assert builder._char_count("memory") > 3900
    assert builder._char_count("user") > 1900

    # No inject_limit override -- exercise the real default from GatewayConfig
    # (runtime.py's ``getattr(..., "inject_limit", 6400)`` fallback).
    runner = _runner(tmp_path)

    memory_md = runner._load_memory_md(tmp_path)
    assert memory_md is not None
    assert "MEMORY (your personal notes)" in memory_md
    assert "USER PROFILE (who the user is)" in memory_md
    assert "..." not in memory_md

    metadata: dict[str, object] = {}
    assembled = runner._assemble_prompt(
        "main", [], session_key="agent:main:auto", prompt_metadata=metadata
    )
    prompt = _prompt_text(assembled)
    assert "MEMORY (your personal notes)" in prompt
    assert "USER PROFILE (who the user is)" in prompt


def test_dropped_user_block_never_leaks_raw_user_md_via_workspace_files(tmp_path):
    """When a tiny inject_limit forces the curated user block to drop, the
    raw unsanitized USER.md must not re-enter the prompt through the
    workspace-files block either.

    Regression test for a security hole: popping USER.md out of
    ``workspace_files`` was gated on the rendered curated header being
    present in ``memory_text``. When the user block gets budget-dropped,
    that condition is false, so the raw file used to leak back in
    unsanitized via the workspace-files ("Workspace Files") block.
    """
    (tmp_path / "AGENTS.md").write_text("agents\n", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text(
        "User deploys with make deploy", encoding="utf-8"
    )
    raw_user_secret = "Name is Ada Lovelace; SSN 000-11-2222; hates being called Ada"
    (tmp_path / "USER.md").write_text(raw_user_secret, encoding="utf-8")

    # Small enough that the memory block fits but the user block is dropped
    # whole at the injection boundary (see test_user_block_dropped_when_over_inject_limit).
    runner = _runner(tmp_path, inject_limit=200)
    metadata: dict[str, object] = {}

    assembled = runner._assemble_prompt(
        "main", [], session_key="agent:main:auto", prompt_metadata=metadata
    )
    prompt = _prompt_text(assembled)

    assert "USER PROFILE (who the user is)" not in prompt
    # The raw file content must not leak in anywhere -- not via the curated
    # block, and not via a raw workspace-files re-entry of USER.md.
    assert raw_user_secret not in prompt
    assert "Ada Lovelace" not in prompt


def test_oversized_memory_block_falls_back_to_slice(tmp_path, caplog):
    (tmp_path / "AGENTS.md").write_text("agents\n", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text(
        "User deploys with make deploy and prefers verbose logs during rollout",
        encoding="utf-8",
    )
    runner = _runner(tmp_path, inject_limit=10)
    # `runtime.log` is a structlog logger, which by default renders straight
    # to stdout rather than through stdlib `logging` — so `caplog` (which
    # patches stdlib logging) cannot observe it here. No other test in this
    # codebase asserts on a structlog warning via caplog (checked
    # tests/test_memory_flush.py's caplog usage: that module logs via plain
    # `logging.getLogger`, not structlog). We still set the level in case
    # structlog is ever wired to stdlib logging, but assert on the
    # unambiguous, directly-observable slice behavior.
    caplog.set_level(logging.WARNING, logger="agentos.engine.runtime")

    memory_md = runner._load_memory_md(tmp_path)

    assert memory_md is not None
    assert memory_md.endswith("\n...")
    # Sliced (legacy behavior) to roughly the tiny limit, not the full block.
    assert len(memory_md) < 50
