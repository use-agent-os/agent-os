"""Knowledge-base ingest of text files that start with a byte-order mark.

Windows writes UTF-16 with a BOM by default (PowerShell 5 ``>``, Notepad's
"Unicode", Excel's "Unicode Text"). Decoded as UTF-8, such a file indexes as
NUL-interleaved noise that no search can match. Everything here is written to
``tmp_path``; nothing touches the network.
"""

from __future__ import annotations

import codecs
from pathlib import Path

import pytest

from agentos.memory.ingest import extract_document_text, ingest_document
from agentos.memory.store import LongTermMemoryStore
from agentos.memory.sync_manager import MemorySyncManager
from agentos.memory.types import MemorySource

LINE = "Production database host is orion-db-7 in Frankfurt — Zürich is standby"


@pytest.mark.parametrize(
    ("encoding", "bom"),
    [
        ("utf-16-le", codecs.BOM_UTF16_LE),
        ("utf-16-be", codecs.BOM_UTF16_BE),
        ("utf-32-le", codecs.BOM_UTF32_LE),
        ("utf-32-be", codecs.BOM_UTF32_BE),
        ("utf-8", codecs.BOM_UTF8),
    ],
    ids=["utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be", "utf-8-sig"],
)
def test_a_file_is_decoded_with_the_encoding_its_bom_names(
    tmp_path: Path, encoding: str, bom: bytes
) -> None:
    path = tmp_path / "servers.log"
    path.write_bytes(bom + f"{LINE}\r\nsecond line\r\n".encode(encoding))

    # Path reads keep their universal-newline translation.
    assert extract_document_text(path) == f"{LINE}\nsecond line\n"


@pytest.mark.parametrize(
    ("encoding", "bom"),
    [
        ("utf-16-le", codecs.BOM_UTF16_LE),
        ("utf-16-be", codecs.BOM_UTF16_BE),
        ("utf-8", codecs.BOM_UTF8),
    ],
    ids=["utf-16-le", "utf-16-be", "utf-8-sig"],
)
def test_uploaded_bytes_are_decoded_with_the_encoding_their_bom_names(
    encoding: str, bom: bytes
) -> None:
    raw = bom + f"{LINE}\n".encode(encoding)

    assert extract_document_text(raw, filename="servers.txt") == f"{LINE}\n"


def test_a_utf8_bom_does_not_hide_front_matter(tmp_path: Path) -> None:
    path = tmp_path / "runbook.md"
    path.write_bytes(codecs.BOM_UTF8 + b"---\ntitle: Runbook\n---\n\nFailover steps.\n")

    text = extract_document_text(path)

    assert text.startswith("---\ntitle: Runbook\n")
    assert "\ufeff" not in text


def test_utf8_without_a_bom_reads_as_before(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_bytes(f"{LINE}\n".encode())

    assert extract_document_text(path) == f"{LINE}\n"
    assert extract_document_text(f"{LINE}\n".encode(), filename="notes.txt") == f"{LINE}\n"


def test_undecodable_bytes_are_still_replaced_rather_than_raised(tmp_path: Path) -> None:
    path = tmp_path / "mixed.txt"
    path.write_bytes(b"caf\xe9 menu\n")

    assert extract_document_text(path) == "caf\ufffd menu\n"
    assert extract_document_text(b"caf\xe9 menu", filename="mixed.txt") == "caf\ufffd menu"


@pytest.mark.asyncio
async def test_knowledge_base_search_finds_text_from_a_utf16_file(tmp_path: Path) -> None:
    utf16 = tmp_path / "servers.log"
    utf16.write_bytes(f"{LINE}\r\n".encode("utf-16"))
    utf8 = tmp_path / "contacts.txt"
    utf8.write_text("On-call escalation goes to Priya", encoding="utf-8")

    store = LongTermMemoryStore(tmp_path / "memory.db")
    await store.initialize()
    try:
        for path in (utf16, utf8):
            result = await ingest_document(store, path, rel_path=f"knowledge_base/{path.name}")
            assert result.status == "indexed"

        # Positive control: the UTF-8 sibling was searchable before the fix too.
        hits, _ = await store.search("escalation", source=MemorySource.knowledge_base)
        assert [hit.path for hit in hits] == ["knowledge_base/contacts.txt"]

        hits, _ = await store.search("Frankfurt", source=MemorySource.knowledge_base)
        assert [hit.path for hit in hits] == ["knowledge_base/servers.log"]
        assert "\x00" not in (hits[0].text or hits[0].snippet)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sync_manager_indexes_a_utf16_file_dropped_into_the_knowledge_base(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    memory_dir = workspace / "memory"
    kb_dir = workspace / "knowledge_base"
    memory_dir.mkdir(parents=True)
    kb_dir.mkdir()
    (kb_dir / "servers.log").write_bytes(f"{LINE}\r\n".encode("utf-16"))

    store = LongTermMemoryStore(tmp_path / "memory.db")
    await store.initialize()
    sync_manager = MemorySyncManager(store=store, workspace_dir=workspace, memory_dir=memory_dir)
    try:
        await sync_manager.start()

        hits, _ = await store.search("orion", source=MemorySource.knowledge_base)
        assert [hit.path for hit in hits] == ["knowledge_base/servers.log"]
    finally:
        await sync_manager.stop()
        await store.close()
