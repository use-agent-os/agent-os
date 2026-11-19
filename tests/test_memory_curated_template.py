"""The text old workspaces were seeded with in MEMORY.md / USER.md is not memory.

New workspaces start with both curated files empty. Existing ones carry the old
instructional templates, either as the whole file (USER.md, or a MEMORY.md the
free-form migration never ran on) or as the eight entries the migration split
the MEMORY.md one into. The store removes that text once, on load, without
touching anything a user or the agent wrote.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agentos.agents.scope import resolve_agent_workspace_dir
from agentos.engine.runtime import TurnRunner
from agentos.gateway.config import GatewayConfig, MemoryConfig
from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.identity.bootstrap import ensure_agent_workspace
from agentos.memory import curated_template
from agentos.memory.curated import ENTRY_DELIMITER, CuratedMemoryStore
from agentos.memory.curated_migration import _split_freeform, migrate_freeform_memory_md
from agentos.memory.curated_template import (
    SHIPPED_TEMPLATES,
    holds_only_bootstrap_template,
    without_bootstrap_template,
)
from agentos.memory.evidence import MemoryEvidenceStore, SessionRef, item_hash
from agentos.memory.manager import MemoryManager
from agentos.memory.retrieval import MemoryRetriever
from agentos.memory.store import LongTermMemoryStore
from agentos.memory.sync_manager import MemorySyncManager
from agentos.memory.turn_capture import TurnCaptureService
from agentos.migration.openclaw import MigrationOptions, OpenClawMigrator

MEMORY_TEMPLATE = SHIPPED_TEMPLATES["memory"][0]
USER_TEMPLATE = SHIPPED_TEMPLATES["user"][0]
# What the free-form migration made of the MEMORY.md template: what every
# workspace that has had a turn carries today.
MIGRATED_MEMORY_TEMPLATE = _split_freeform(MEMORY_TEMPLATE)

# Fragments that appear only in the old templates (the other bootstrap files
# also start with "Use this file for ...").
TEMPLATE_MARKERS = (
    "Use this file for curated durable",
    "Use this file for stable user profile",
    "- Name:",
    "What to call them",
    "Do not store secrets, credentials, private keys",
    "Do not put secrets",
    "durable user preferences that are not identity/profile fields",
)


def _write(path: Path, entries: list[str]) -> None:
    path.write_text(ENTRY_DELIMITER.join(entries), encoding="utf-8")


def _load(root: Path, *, clean: bool = False) -> CuratedMemoryStore:
    """Load as a read-only caller does, or as the turn path does (*clean*)."""
    store = CuratedMemoryStore(memory_dir=root)
    store.load_from_disk(remove_bootstrap_template=clean)
    return store


def _files(root: Path) -> dict[str, bytes]:
    """Every file under *root* (lock files included) and its bytes."""
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def _baks(root: Path) -> list[Path]:
    return sorted(root.glob("*.bak*"))


def _runner(root: Path) -> TurnRunner:
    return TurnRunner(
        provider_selector=None,
        config=SimpleNamespace(
            workspace_dir=str(root),
            memory=SimpleNamespace(source="workspace"),
            tools=SimpleNamespace(profile=None),
        ),
    )


def _prompt(root: Path, agent_id: str = "main") -> str:
    assembled = _runner(root)._assemble_prompt(agent_id, [], session_key=f"agent:{agent_id}:auto")
    if isinstance(assembled, tuple):
        return "\n\n".join(part for part in assembled if part)
    return assembled or ""


def _assert_no_template_text(text: str) -> None:
    for marker in TEMPLATE_MARKERS:
        assert marker not in text, marker


# -- the shipped text ----------------------------------------------------------


def test_the_migrated_template_is_the_eight_entries_measured_live() -> None:
    # Pinned to what a fresh install was measured with (8 entries, 630 chars).
    # If the free-form splitter ever changes, existing workspaces still hold
    # the entries the OLD splitter made: freeze them here before changing it.
    assert len(MIGRATED_MEMORY_TEMPLATE) == 8
    assert len(ENTRY_DELIMITER.join(MIGRATED_MEMORY_TEMPLATE)) == 630
    assert len(USER_TEMPLATE.strip()) == 325


# -- new installs ----------------------------------------------------------------


def test_a_fresh_workspace_uses_no_curated_budget_and_injects_nothing(tmp_path: Path) -> None:
    result = ensure_agent_workspace(tmp_path)

    assert {"MEMORY.md", "USER.md"} <= set(result.created_files)
    assert (tmp_path / "MEMORY.md").read_bytes() == b""
    assert (tmp_path / "USER.md").read_bytes() == b""
    store = _load(tmp_path)
    assert store.usage_for("memory") == "0/4,000"
    assert store.usage_for("user") == "0/2,000"
    assert store.snapshot_block("memory") is None
    assert store.snapshot_block("user") is None
    assert store.write_blocked_reason("memory") is None
    assert store.write_blocked_reason("user") is None

    prompt = _prompt(tmp_path)
    assert "MEMORY (your personal notes)" not in prompt
    assert "USER PROFILE (who the user is)" not in prompt
    _assert_no_template_text(prompt)
    assert _baks(tmp_path) == []


def test_a_fresh_workspace_takes_its_first_writes(tmp_path: Path) -> None:
    ensure_agent_workspace(tmp_path)
    store = _load(tmp_path)

    assert store.add("user", "Name is Ada")["success"] is True
    assert store.add("memory", "Deploys with make deploy")["success"] is True

    assert (tmp_path / "USER.md").read_text(encoding="utf-8") == "Name is Ada"
    assert _baks(tmp_path) == []


def test_a_per_agent_workspace_is_seeded_empty_and_cleaned(tmp_path: Path) -> None:
    config = GatewayConfig(workspace_dir=str(tmp_path), memory=MemoryConfig(source="workspace"))
    fresh = resolve_agent_workspace_dir("research", config)
    ensure_agent_workspace(fresh)
    assert fresh == tmp_path / "agents" / "research"
    assert (fresh / "MEMORY.md").read_bytes() == b""
    assert (fresh / "USER.md").read_bytes() == b""

    # An agent workspace seeded by an older release.
    old = resolve_agent_workspace_dir("writer", config)
    old.mkdir(parents=True)
    _write(old / "MEMORY.md", [*MIGRATED_MEMORY_TEMPLATE, "Writes in British English"])
    (old / "USER.md").write_text(USER_TEMPLATE, encoding="utf-8")

    memory = _runner(tmp_path)._load_memory_md(old)

    assert memory is not None
    assert "Writes in British English" in memory
    _assert_no_template_text(memory)
    assert (old / "MEMORY.md").read_text(encoding="utf-8") == "Writes in British English"
    assert (old / "USER.md").read_bytes() == b""


# -- existing installs -------------------------------------------------------------


def _old_install(root: Path) -> list[str]:
    """Template entries around real ones, as an upgraded workspace holds them."""
    real = ["Dog is called Rex", "Blood type is O+"]
    _write(
        root / "MEMORY.md",
        [*MIGRATED_MEMORY_TEMPLATE[:3], real[0], *MIGRATED_MEMORY_TEMPLATE[3:], real[1]],
    )
    _write(root / "USER.md", [USER_TEMPLATE.strip(), "Prefers tea"])
    return real


def test_a_read_only_load_leaves_the_template_out_and_writes_nothing(tmp_path: Path) -> None:
    real = _old_install(tmp_path)
    before = _files(tmp_path)

    store = _load(tmp_path)

    assert store.entries_for("memory") == real
    assert store.entries_for("user") == ["Prefers tea"]
    joined = ENTRY_DELIMITER.join(real)
    assert store.char_count("memory") == len(joined)
    assert store.usage_for("memory") == f"{len(joined)}/4,000"
    assert store.usage_for("user") == "11/2,000"
    snapshot = store.snapshot_block("memory") or ""
    assert f"{len(joined)}/4,000 chars]" in snapshot
    _assert_no_template_text(snapshot + (store.snapshot_block("user") or ""))
    assert store.write_blocked_reason("memory") is None
    # Byte-identical, and no lock or backup file appeared.
    assert _files(tmp_path) == before


def test_the_first_write_cleans_the_file(tmp_path: Path) -> None:
    real = _old_install(tmp_path)
    store = _load(tmp_path)

    assert store.add("memory", "Works night shifts")["success"] is True
    assert store.remove("user", "Prefers tea")["success"] is True

    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == ENTRY_DELIMITER.join(
        [*real, "Works night shifts"]
    )
    assert (tmp_path / "USER.md").read_bytes() == b""
    assert _baks(tmp_path) == []


def test_a_write_that_changes_nothing_writes_nothing(tmp_path: Path) -> None:
    _old_install(tmp_path)
    store = _load(tmp_path)
    before = _files(tmp_path)

    # A duplicate add and an exact removal of a missing entry commit nothing.
    assert store.add("memory", "Dog is called Rex")["success"] is True
    assert store.remove_exact("user", ["Not there"])["removed"] == []

    assert {k: v for k, v in _files(tmp_path).items() if not k.endswith(".lock")} == {
        k: v for k, v in before.items() if not k.endswith(".lock")
    }


def test_the_turn_path_removes_the_template_once(tmp_path: Path) -> None:
    real = _old_install(tmp_path)

    memory = _runner(tmp_path)._load_memory_md(tmp_path)

    assert memory is not None
    joined = ENTRY_DELIMITER.join(real)
    assert f"{len(joined)}/4,000 chars]" in memory
    _assert_no_template_text(memory)
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == joined
    assert (tmp_path / "USER.md").read_text(encoding="utf-8") == "Prefers tea"
    assert _baks(tmp_path) == []
    # The next write is not refused as drift.
    assert _load(tmp_path).add("memory", "Works night shifts")["success"] is True
    assert _baks(tmp_path) == []


def test_the_second_cleaning_load_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path / "MEMORY.md", [*MIGRATED_MEMORY_TEMPLATE, "Dog is called Rex"])
    (tmp_path / "USER.md").write_text(USER_TEMPLATE, encoding="utf-8")
    _load(tmp_path, clean=True)
    before = _files(tmp_path)

    def no_write(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a clean file was rewritten")

    monkeypatch.setattr(CuratedMemoryStore, "_write_file", staticmethod(no_write))
    store = _load(tmp_path, clean=True)

    assert store.entries_for("memory") == ["Dog is called Rex"]
    assert store.entries_for("user") == []
    assert _files(tmp_path) == before


def test_an_unmigrated_template_file_is_emptied(tmp_path: Path) -> None:
    (tmp_path / "MEMORY.md").write_text(MEMORY_TEMPLATE, encoding="utf-8")
    (tmp_path / "USER.md").write_text(USER_TEMPLATE, encoding="utf-8")
    view = _load(tmp_path)
    assert view.entries_for("memory") == []
    assert view.entries_for("user") == []
    assert (tmp_path / "USER.md").read_text(encoding="utf-8") == USER_TEMPLATE

    store = _load(tmp_path, clean=True)

    assert store.entries_for("memory") == []
    assert store.entries_for("user") == []
    assert (tmp_path / "MEMORY.md").read_bytes() == b""
    assert (tmp_path / "USER.md").read_bytes() == b""
    assert _baks(tmp_path) == []


def test_an_unmigrated_template_never_reaches_the_prompt(tmp_path: Path) -> None:
    # The runtime path runs the free-form migration first, then loads.
    (tmp_path / "MEMORY.md").write_text(MEMORY_TEMPLATE, encoding="utf-8")
    (tmp_path / "USER.md").write_text(USER_TEMPLATE, encoding="utf-8")

    prompt = _prompt(tmp_path)

    _assert_no_template_text(prompt)
    assert "MEMORY (your personal notes)" not in prompt
    assert (tmp_path / "MEMORY.md").read_bytes() == b""
    assert (tmp_path / "USER.md").read_bytes() == b""
    assert not (tmp_path / "memory" / "archive" / "memory-overflow.md").exists()


def test_a_template_written_with_windows_line_endings_is_recognised(tmp_path: Path) -> None:
    (tmp_path / "USER.md").write_bytes(USER_TEMPLATE.replace("\n", "\r\n").encode("utf-8"))
    _write(tmp_path / "MEMORY.md", MIGRATED_MEMORY_TEMPLATE)
    # Normalise first: on Windows the helper already wrote \r\n, and doubling
    # it would give \r\r\n, which reads back as a blank line.
    raw = (tmp_path / "MEMORY.md").read_bytes().replace(b"\r\n", b"\n")
    (tmp_path / "MEMORY.md").write_bytes(raw.replace(b"\n", b"\r\n"))

    store = _load(tmp_path)

    assert store.entries_for("user") == []
    assert store.entries_for("memory") == []


def test_another_shipped_wording_is_recognised_once_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Git history and every released wheel hold a single wording of each
    # template. This pins the extension point: a wording added to
    # SHIPPED_TEMPLATES is handled in every form, exactly like the first.
    older = USER_TEMPLATE.replace("- Pronouns:\n", "").replace(
        "Do not put secrets", "Never put secrets"
    )
    monkeypatch.setitem(curated_template.SHIPPED_TEMPLATES, "user", (USER_TEMPLATE, older))
    curated_template._exact_entries.cache_clear()
    try:
        (tmp_path / "USER.md").write_text(
            older.replace("- Name:", "- Name: Ada").strip() + ENTRY_DELIMITER + "Prefers tea",
            encoding="utf-8",
        )
        assert _load(tmp_path).entries_for("user") == ["- Name: Ada", "Prefers tea"]
    finally:
        curated_template._exact_entries.cache_clear()


# -- user content is never lost ------------------------------------------------------


def test_a_hand_filled_user_template_keeps_the_filled_values(tmp_path: Path) -> None:
    filled = (
        USER_TEMPLATE.replace("- Name:", "- Name: Ada Lovelace")
        .replace("- What to call them:", "- What to call them: Ada")
        .replace("## Context\n", "## Context\n\nWorks on the analytical engine.\n")
    )
    _write(tmp_path / "USER.md", [filled.strip(), "Prefers tea"])

    expected = [
        "- Name: Ada Lovelace\n- What to call them: Ada\n\nWorks on the analytical engine.",
        "Prefers tea",
    ]
    assert _load(tmp_path).entries_for("user") == expected

    store = _load(tmp_path, clean=True)

    assert store.entries_for("user") == expected
    assert (tmp_path / "USER.md").read_text(encoding="utf-8") == ENTRY_DELIMITER.join(expected)
    assert _baks(tmp_path) == []
    # Idempotent: the kept entry is not template text any more.
    assert _load(tmp_path, clean=True).entries_for("user") == expected


def test_a_rewritten_entry_over_the_limit_is_not_drift(tmp_path: Path) -> None:
    # On disk the hand-filled template is over the 2,000-char limit; without
    # the template it is not. Until a write, the file holds the long form,
    # which the store has read itself and must not take for external drift.
    notes = "Works on the analytical engine. " * 55
    filled = USER_TEMPLATE.replace("## Context\n", f"## Context\n\n{notes.strip()}\n").strip()
    assert len(filled) > 2000
    (tmp_path / "USER.md").write_text(filled, encoding="utf-8")
    store = _load(tmp_path)
    assert store.char_count("user") < 2000

    assert store.add("user", "Prefers tea")["success"] is True

    assert (tmp_path / "USER.md").read_text(encoding="utf-8") == ENTRY_DELIMITER.join(
        [notes.strip(), "Prefers tea"]
    )
    assert _baks(tmp_path) == []


def test_an_edited_template_is_left_whole(tmp_path: Path) -> None:
    # A template line was reworded: the entry is the user's, boilerplate and all.
    edited = (
        USER_TEMPLATE.replace("- Name:", "- Name: Ada")
        .replace("Put facts about the user here", "Put facts about Ada here")
        .strip()
    )
    # A template line was deleted.
    trimmed = USER_TEMPLATE.replace("## Context\n\n", "").replace("- Name:", "- Name: Ada").strip()
    for entry in (edited, trimmed):
        (tmp_path / "USER.md").write_text(entry, encoding="utf-8")

        assert _load(tmp_path).entries_for("user") == [entry]
        assert (tmp_path / "USER.md").read_text(encoding="utf-8") == entry


def test_entries_that_only_resemble_template_text_are_kept(tmp_path: Path) -> None:
    memory = [
        # Template sentence rewrapped onto one line.
        "Do not store secrets, credentials, private keys, or ordinary one-off deliverables here.",
        "Examples: the user wants examples in Python",
        "durable user preferences that are not identity/profile fields: dark mode",
        "Use this file for curated durable non-profile facts",
    ]
    user = ["Name:", "Examples:", "- Name: Ada", "Timezone: Europe/London (user said so)"]
    _write(tmp_path / "MEMORY.md", memory)
    _write(tmp_path / "USER.md", user)

    store = _load(tmp_path)

    assert store.entries_for("memory") == memory
    assert store.entries_for("user") == user


def test_a_file_that_does_not_round_trip_is_left_to_the_drift_guard(tmp_path: Path) -> None:
    # A blank line before the § is drift; the store must not rewrite it.
    raw = USER_TEMPLATE.strip() + "\n\n§\nPrefers tea"
    (tmp_path / "USER.md").write_text(raw, encoding="utf-8")

    store = _load(tmp_path, clean=True)

    assert store.entries_for("user") == ["Prefers tea"]
    assert (tmp_path / "USER.md").read_text(encoding="utf-8") == raw
    assert _baks(tmp_path) == []


def test_a_failed_cleanup_does_not_break_the_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "USER.md").write_text(USER_TEMPLATE, encoding="utf-8")

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(CuratedMemoryStore, "_write_file", staticmethod(refuse))
    store = _load(tmp_path, clean=True)

    assert store.load_failed == {}
    assert store.entries_for("user") == []
    assert (tmp_path / "USER.md").read_text(encoding="utf-8") == USER_TEMPLATE


def test_a_failed_cleanup_still_injects_no_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The store leaves the text out; the raw-file fallback used when the
    # store has nothing to inject must not put it back.
    _write(tmp_path / "MEMORY.md", MIGRATED_MEMORY_TEMPLATE)
    (tmp_path / "USER.md").write_text(USER_TEMPLATE, encoding="utf-8")

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(CuratedMemoryStore, "_write_file", staticmethod(refuse))

    assert _runner(tmp_path)._load_memory_md(tmp_path) is None
    prompt = _prompt(tmp_path)
    _assert_no_template_text(prompt)
    assert "MEMORY (your personal notes)" not in prompt


# -- the free-form migration ------------------------------------------------------


def test_template_text_does_not_push_user_notes_into_the_archive(tmp_path: Path) -> None:
    # 80% of a 1,000-char budget is 800: the template's 630 chars used to be
    # kept first and push most of these notes into the overflow archive.
    notes = [f"Note {i}: " + "x" * 120 for i in range(5)]
    (tmp_path / "MEMORY.md").write_text(
        MEMORY_TEMPLATE + "\n" + "\n\n".join(notes) + "\n", encoding="utf-8"
    )

    assert migrate_freeform_memory_md(tmp_path, memory_char_limit=1000) is True

    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == ENTRY_DELIMITER.join(notes)
    assert not (tmp_path / "memory" / "archive" / "memory-overflow.md").exists()


def test_migration_of_a_bare_template_writes_an_empty_file_once(tmp_path: Path) -> None:
    (tmp_path / "MEMORY.md").write_text(MEMORY_TEMPLATE, encoding="utf-8")

    assert migrate_freeform_memory_md(tmp_path, memory_char_limit=4000) is True
    assert (tmp_path / "MEMORY.md").read_bytes() == b""
    assert migrate_freeform_memory_md(tmp_path, memory_char_limit=4000) is False


def test_migration_keeps_lines_added_inside_template_paragraphs(tmp_path: Path) -> None:
    added = MEMORY_TEMPLATE.replace(
        "- stable context that should survive across sessions\n",
        "- stable context that should survive across sessions\n- The cat is called Tom\n",
    )
    (tmp_path / "MEMORY.md").write_text(added, encoding="utf-8")

    migrate_freeform_memory_md(tmp_path, memory_char_limit=4000)

    assert _load(tmp_path).entries_for("memory") == ["The cat is called Tom"]


# -- lineage ------------------------------------------------------------------------


async def test_lineage_counts_drop_only_the_untracked_template_entries(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "memory").mkdir(parents=True)
    db = tmp_path / "memory.db"
    store = LongTermMemoryStore(db)
    await store.initialize()
    evidence = MemoryEvidenceStore(db)
    writes: list[tuple[str, ...]] = []
    provider = SimpleNamespace(notify_memory_write=lambda *args: writes.append(args))
    manager = MemoryManager(
        agent_id="main",
        db_path=db,
        store=store,
        sync_manager=MemorySyncManager(
            store=store, workspace_dir=workspace, memory_dir=workspace / "memory"
        ),
        retriever=MemoryRetriever(store),
        turn_capture=TurnCaptureService(workspace_dir=workspace, turns_dir=tmp_path / "turns"),
        workspace_dir=workspace,
        memory_dir=workspace / "memory",
        evidence=evidence,
        provider_manager=provider,
    )
    try:
        real = ["Deploy window is Friday", "Legacy hand-written fact"]
        _write(workspace / "MEMORY.md", [*MIGRATED_MEMORY_TEMPLATE, *real])
        _write(workspace / "USER.md", [USER_TEMPLATE.strip(), "Prefers tea"])
        await evidence.add_origin(
            kind="curated",
            target="memory",
            item_hash=item_hash("Deploy window is Friday"),
            item_len=len("Deploy window is Friday"),
            writer="tool",
            session=SessionRef(session_key="agent:main:webchat:direct:s1", session_id="s1"),
        )

        before = _files(workspace)

        # `agentos memory curated get` and `agentos memory status` read an
        # uncleaned workspace: same answers as a clean one, nothing written.
        ctx = RpcContext(conn_id="test", config=GatewayConfig(workspace_dir=str(workspace)))
        ctx.memory_managers = {"main": manager}
        got = await get_dispatcher().dispatch(
            "r", "memory.curated.get", {"agentId": "main", "target": "user"}, ctx
        )
        assert got.ok, got.error
        assert got.payload["entries"] == ["Prefers tea"]
        assert got.payload["charCount"] == len("Prefers tea")
        assert got.payload["usage"] == "11/2,000"
        got = await get_dispatcher().dispatch(
            "r", "memory.curated.get", {"agentId": "main", "target": "memory"}, ctx
        )
        assert got.payload["entries"] == real

        status = (await manager.status())["curated"]
        assert _files(workspace) == before

        assert status["memory"] == {
            "entries": 2,
            "usage": f"{len(ENTRY_DELIMITER.join(real))}/4,000",
            "tracked": 1,
            "untracked": 1,
        }
        assert status["user"] == {
            "entries": 1,
            "usage": "11/2,000",
            "tracked": 0,
            "untracked": 1,
        }
        # No origin row points at template text, before or after.
        template_hashes = {item_hash(e) for e in MIGRATED_MEMORY_TEMPLATE}
        assert not template_hashes & await evidence.tracked_item_hashes("curated", "memory")
        assert await evidence.tracked_item_hashes("curated", "user") == set()
        # Nothing was mirrored: no provider ever held a copy of the template.
        assert writes == []

        # The first write (here an operator's) leaves a clean file.
        added = await get_dispatcher().dispatch(
            "r",
            "memory.curated.add",
            {"agentId": "main", "target": "user", "content": "Name is Ada"},
            ctx,
        )
        assert added.ok, added.error
        assert (workspace / "USER.md").read_text(encoding="utf-8") == ENTRY_DELIMITER.join(
            ["Prefers tea", "Name is Ada"]
        )
        assert _baks(workspace) == []
    finally:
        await evidence.close()
        await store.close()


# -- importers ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "text", "expected"),
    [
        ("memory", "", True),
        ("memory", "\n\n  \n", True),
        ("memory", MEMORY_TEMPLATE, True),
        ("memory", ENTRY_DELIMITER.join(MIGRATED_MEMORY_TEMPLATE), True),
        ("user", USER_TEMPLATE, True),
        ("user", USER_TEMPLATE.replace("\n", "\r\n"), True),
        ("user", USER_TEMPLATE.replace("- Name:", "- Name: Ada"), False),
        ("memory", ENTRY_DELIMITER.join([*MIGRATED_MEMORY_TEMPLATE, "Real fact"]), False),
        ("user", MEMORY_TEMPLATE, False),
    ],
)
def test_holds_only_bootstrap_template(target: str, text: str, expected: bool) -> None:
    assert holds_only_bootstrap_template(target, text) is expected


def test_an_old_template_does_not_block_an_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A workspace seeded by an older release that never ran a turn still holds
    # the raw templates; an import must treat them as replaceable, as before.
    source = tmp_path / ".openclaw"
    (source / "workspace").mkdir(parents=True)
    (source / "workspace" / "USER.md").write_text("openclaw real user\n", encoding="utf-8")
    (source / "workspace" / "MEMORY.md").write_text("openclaw memory\n", encoding="utf-8")
    (source / "openclaw.json").write_text("{}", encoding="utf-8")
    home = tmp_path / "agentos-home"
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(home))
    ensure_agent_workspace(home / "workspace")
    (home / "workspace" / "USER.md").write_text(USER_TEMPLATE, encoding="utf-8")
    (home / "workspace" / "MEMORY.md").write_text(MEMORY_TEMPLATE, encoding="utf-8")

    report = OpenClawMigrator(
        MigrationOptions(source=source, config_path=tmp_path / "cfg.toml", apply=True)
    ).migrate()

    for kind in ("user-profile", "memory"):
        item = next(i for i in report["items"] if i["kind"] == kind)
        assert item["status"] == "migrated", item
        assert item["details"].get("replaced_bootstrap_template") is True, item


def test_without_bootstrap_template_keeps_order_and_position() -> None:
    filled = USER_TEMPLATE.replace("- Timezone:", "- Timezone: UTC+1").strip()
    assert without_bootstrap_template("user", ["First", filled, "Last"]) == [
        "First",
        "- Timezone: UTC+1",
        "Last",
    ]
