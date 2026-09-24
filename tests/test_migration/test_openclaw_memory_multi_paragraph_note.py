"""``agentos migrate openclaw --apply``: a daily note is one block, not one paragraph.

When the destination ``MEMORY.md`` already holds the user's own content, the
migrator appends the imported blocks that are not already there. A "block" is a
paragraph, except that a daily-memory entry --
``## Imported daily memory: <name>`` followed by the note -- is meant to stay
glued so header and body are never deduped against each other.

Only the *first* body paragraph was glued on. A note whose body has more than
one paragraph -- a heading and a line under it is enough, and that is how
people write notes -- was therefore shredded: its opening paragraph carried the
header, and the rest became loose paragraphs. If that opening paragraph matched
anything already in the destination (a bare ``## Preferences`` heading matches),
the header went with it and the note's facts landed at the end of the file with
no provenance, reading as part of whichever section happened to precede them.

The migration reports ``migrated`` either way, and it is a one-shot rewrite of
the user's memory file.

Every assertion here runs the migrator through ``OpenClawMigrator.migrate()``,
the call behind ``agentos migrate openclaw``, not the merge helper.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentos.migration.openclaw import MigrationOptions, OpenClawMigrator


def _openclaw_source(root: Path, notes: dict[str, str]) -> Path:
    source = root / ".openclaw"
    memory_dir = source / "workspace" / "memory"
    memory_dir.mkdir(parents=True)
    for name, body in notes.items():
        (memory_dir / name).write_text(body, encoding="utf-8")
    (source / "openclaw.json").write_text("{}", encoding="utf-8")
    return source


def _destination(root: Path, monkeypatch: pytest.MonkeyPatch, existing: str) -> Path:
    home = root / "agentos-home"
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(home))
    (home / "workspace").mkdir(parents=True)
    destination = home / "workspace" / "MEMORY.md"
    destination.write_text(existing, encoding="utf-8")
    return destination


def _migrate(root: Path, source: Path) -> dict:
    report = OpenClawMigrator(
        MigrationOptions(source=source, config_path=root / "cfg.toml", apply=True)
    ).migrate()
    return next(item for item in report["items"] if item["kind"] == "memory")


def test_a_multi_paragraph_note_keeps_its_header_when_its_first_paragraph_is_shared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _openclaw_source(
        tmp_path,
        {"day-two.md": "## Preferences\n\nDeploy window is Tuesday 09:00 Jakarta.\n"},
    )
    destination = _destination(
        tmp_path, monkeypatch, "## Preferences\n\nUser likes concise answers.\n"
    )

    item = _migrate(tmp_path, source)
    text = destination.read_text(encoding="utf-8")

    assert item["status"] == "migrated"
    assert "## Imported daily memory: day-two.md" in text, (
        "the note's provenance header was dropped because its first paragraph "
        "matched a heading the destination already had"
    )
    assert (
        "## Imported daily memory: day-two.md\n\n## Preferences\n\n"
        "Deploy window is Tuesday 09:00 Jakarta." in text
    ), "the note must stay contiguous under its own header"
    assert "User likes concise answers." in text, "existing memory is preserved verbatim"


def test_a_later_paragraph_of_a_note_is_not_deduped_away_on_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _openclaw_source(
        tmp_path,
        {"day-three.md": "Sprint retro moved.\n\nThe staging database is read-only.\n"},
    )
    destination = _destination(
        tmp_path, monkeypatch, "Unrelated note.\n\nThe staging database is read-only.\n"
    )

    item = _migrate(tmp_path, source)
    text = destination.read_text(encoding="utf-8")

    assert item["status"] == "migrated"
    assert (
        "## Imported daily memory: day-three.md\n\nSprint retro moved.\n\n"
        "The staging database is read-only." in text
    ), "a note is appended whole; its second paragraph is not a dedupe unit of its own"


def test_two_notes_do_not_absorb_each_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _openclaw_source(
        tmp_path,
        {
            "day-one.md": "Alpha heading\n\nAlpha detail.\n",
            "day-two.md": "Beta heading\n\nBeta detail.\n",
        },
    )
    destination = _destination(tmp_path, monkeypatch, "Existing agentos note.\n")

    item = _migrate(tmp_path, source)
    text = destination.read_text(encoding="utf-8")

    assert item["details"]["new_blocks_appended"] == 2, "one block per imported note"
    assert (
        "## Imported daily memory: day-one.md\n\nAlpha heading\n\nAlpha detail.\n\n"
        "## Imported daily memory: day-two.md\n\nBeta heading\n\nBeta detail." in text
    )


# --- Guards: green before and after the fix, so they prove nothing on their own. ---


def test_positive_control_a_single_paragraph_note_still_dedupes_against_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without this the assertions above could pass on a merge that never dedupes."""
    source = _openclaw_source(
        tmp_path,
        {"day-one.md": "shared fact\n", "day-two.md": "brand new fact\n"},
    )
    destination = _destination(
        tmp_path,
        monkeypatch,
        "Unique agentos note.\n\n## Imported daily memory: day-one.md\n\nshared fact\n",
    )

    item = _migrate(tmp_path, source)
    text = destination.read_text(encoding="utf-8")

    assert item["details"]["new_blocks_appended"] == 1
    assert item["details"]["deduplicated_blocks_vs_existing"] >= 1
    assert text.count("shared fact") == 1
    assert "brand new fact" in text


def test_guard_re_running_the_migration_still_dedupes_to_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _openclaw_source(
        tmp_path,
        {"day-two.md": "## Preferences\n\nDeploy window is Tuesday 09:00 Jakarta.\n"},
    )
    destination = _destination(
        tmp_path, monkeypatch, "## Preferences\n\nUser likes concise answers.\n"
    )

    _migrate(tmp_path, source)
    after_first = destination.read_text(encoding="utf-8")
    item = _migrate(tmp_path, source)

    assert item["status"] == "skipped"
    assert item["details"]["deduplicated_against_existing"] is True
    assert destination.read_text(encoding="utf-8") == after_first
