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
