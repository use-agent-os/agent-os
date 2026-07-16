"""Curated MEMORY.md / USER.md system-prompt injection via frozen snapshot."""

import logging
from types import SimpleNamespace

from agentos.engine.runtime import TurnRunner
from agentos.memory.curated import ENTRY_DELIMITER


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
