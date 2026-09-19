"""Issue #2897: session_search could not match inside a CJK run and required every term.

Two layers, both in the query/index layer and neither in the data:

* the index used FTS5's default ``unicode61`` tokenizer, which turns a run of
  CJK characters into one token, so the transcript ``我们讨论了数据库迁移计划``
  was findable only by that exact run and never by ``迁移计划`` inside it;
* the query joined its terms with a space, which FTS5 reads as AND, so one
  word the transcript lacks (``for``, ``why``) zeroed the whole query.

The index is now built with ``trigram`` (rebuilt once for an existing
database), terms are joined with ``OR`` and ranked by ``bm25``, terms below
the trigram floor are answered by a ``LIKE`` scan, and snippets are cut in
Python around the first matching term for both paths.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import pytest_asyncio

from agentos.session import storage as storage_module
from agentos.session.manager import SessionManager
from agentos.session.storage import SessionStorage

pytestmark = pytest.mark.skipif(
    sqlite3.sqlite_version_info < (3, 34, 0),
    reason="FTS5 trigram tokenizer needs SQLite 3.34+",
)

DOCKER = "The docker container failed to start because the port was already bound"
POSTGRES = "we discussed the postgres migration plan yesterday"
ZH = "我们讨论了数据库迁移计划"
JA = "昨日、ポスグレの移行計画について話し合いました"
GO = "Go module cache for k8s"
TEXTS = [DOCKER, POSTGRES, ZH, JA, GO]


@pytest_asyncio.fixture
async def storage():
    store = SessionStorage(":memory:")
    await store.connect()
    yield store
    await store.close()


@pytest_asyncio.fixture
async def manager(storage):
    return SessionManager(storage, inject_time_prefix=False)


@pytest_asyncio.fixture
async def session(manager):
    session = await manager.create("agent:main:webchat:aaaa0001", agent_id="main")
    for text in TEXTS:
        await manager.append_message(session.session_key, role="user", content=text)
    return session


async def _hits(storage: SessionStorage, query: str, **kwargs) -> list[str]:
    return [hit["snippet"] for hit in await storage.search_transcript(query, **kwargs)]


# ── the report ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_natural_language_query_finds_the_transcript_that_matches_most_of_it(
    storage, session
) -> None:
    """``for`` is not in the postgres transcript; under AND that was zero hits."""
    hits = await _hits(storage, "migration plan for postgres")

    assert hits
    assert ">>>postgres<<<" in hits[0] and ">>>migration<<<" in hits[0]


@pytest.mark.asyncio
async def test_a_question_with_stop_words_still_finds_its_transcript(storage, session) -> None:
    hits = await _hits(storage, "why did the container not start")

    assert ">>>container<<<" in hits[0]
    assert ">>>start<<<" in hits[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("迁移计划", ZH),  # the last four characters of a seven-character run
        ("数据库迁移", ZH),  # the middle of it
        ("移行計画", JA),
        ("ポスグレ 移行", JA),
        ("移行", JA),  # two characters: below the trigram floor, answered by the scan
        ("計画", JA),
    ],
)
async def test_a_cjk_query_matches_inside_a_run(
    storage, session, query: str, expected: str
) -> None:
    hits = await storage.search_transcript(query)

    assert len(hits) == 1
    assert hits[0]["snippet"].replace(">>>", "").replace("<<<", "") == expected


@pytest.mark.asyncio
async def test_every_query_from_the_report_now_finds_something(storage, session) -> None:
    queries = [
        "migration plan for postgres",
        "why did the container not start",
        "迁移计划",
        "数据库迁移",
        "移行計画",
        "ポスグレ 移行",
    ]
    misses = [q for q in queries if not await storage.search_transcript(q)]

    assert misses == []


# ── ranking ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_transcript_matching_more_terms_ranks_first(storage, session) -> None:
    """OR would be useless without ranking; bm25 puts the fuller match first."""
    hits = await storage.search_transcript("docker container port")

    assert hits[0]["snippet"].count(">>>") == 3


@pytest.mark.asyncio
async def test_a_partial_match_is_returned_after_the_full_one(storage, manager, session) -> None:
    await manager.append_message(session.session_key, role="user", content="the port is open")

    hits = await _hits(storage, "docker container port")

    assert ">>>docker<<<" in hits[0]
    assert any("port is open" in hit.replace(">>>", "").replace("<<<", "") for hit in hits[1:])


# ── the trigram floor ──────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["go", "Go", "k8s"])
async def test_a_short_or_three_character_latin_term_is_found(storage, session, query: str) -> None:
    hits = await _hits(storage, query)

    assert len(hits) == 1
    assert "module cache" in hits[0]


@pytest.mark.asyncio
async def test_a_short_term_is_a_literal_in_the_scan(storage, manager, session) -> None:
    """``_`` is a LIKE wildcard and a word character; as a term it must stay literal."""
    await manager.append_message(session.session_key, role="user", content="use snake_case here")
    await manager.append_message(session.session_key, role="user", content="use snake case here")

    hits = await _hits(storage, "_")

    assert len(hits) == 1
    assert "snake>>>_<<<case" in hits[0]


@pytest.mark.asyncio
async def test_a_short_term_alongside_a_long_one_is_still_marked(storage, session) -> None:
    hits = await _hits(storage, "ポスグレ 移行")

    assert ">>>ポスグレ<<<" in hits[0]
    assert ">>>移行<<<" in hits[0]


def test_fts_query_terms_splits_at_the_floor() -> None:
    indexed, short = SessionStorage.fts_query_terms("go module 移行 計画書 k8s")

    assert indexed == ["module", "計画書", "k8s"]
    assert short == ["go", "移行"]


def test_fts_query_terms_deduplicates_and_caps() -> None:
    indexed, short = SessionStorage.fts_query_terms(
        "alpha alpha beta " + " ".join(f"t{i:03d}" for i in range(30))
    )

    assert indexed[:2] == ["alpha", "beta"]
    assert len(indexed) + len(short) == storage_module._FTS_MAX_TERMS


def test_sanitize_fts_query_joins_with_or_and_quotes_literals() -> None:
    assert (
        SessionStorage.sanitize_fts_query('docker* OR "port" NEAR(x)')
        == '"docker" OR "port" OR "NEAR"'
    )


def test_sanitize_fts_query_matches_nothing_when_no_term_reaches_the_index() -> None:
    assert SessionStorage.sanitize_fts_query("go db") == '""'
    assert SessionStorage.sanitize_fts_query("") == '""'


# ── filters still apply on both paths ──────────────────────────────────────


@pytest.mark.asyncio
async def test_session_and_project_filters_apply_to_the_scan_path(
    storage, manager, session
) -> None:
    other = await manager.create("agent:main:webchat:aaaa0002", agent_id="main")
    await manager.append_message(other.session_key, role="user", content="go elsewhere")

    scoped = await storage.search_transcript("go", session_id=session.session_key)

    assert [hit["session_key"] for hit in scoped] == [session.session_key]


@pytest.mark.asyncio
async def test_session_filter_applies_to_the_fts_path(storage, manager, session) -> None:
    other = await manager.create("agent:main:webchat:aaaa0002", agent_id="main")
    await manager.append_message(other.session_key, role="user", content="postgres elsewhere")

    scoped = await storage.search_transcript("postgres", session_id=other.session_key)

    assert [hit["session_key"] for hit in scoped] == [other.session_key]


@pytest.mark.asyncio
async def test_limit_applies(storage, manager, session) -> None:
    for i in range(5):
        await manager.append_message(session.session_key, role="user", content=f"postgres note {i}")

    assert len(await storage.search_transcript("postgres", limit=3)) == 3
    assert len(await storage.search_transcript("go", limit=1)) == 1


@pytest.mark.asyncio
async def test_results_keep_their_shape(storage, session) -> None:
    hits = await storage.search_transcript("postgres")

    assert set(hits[0]) == {"id", "session_key", "role", "created_at", "snippet"}
    assert hits[0]["role"] == "user"


# ── snippets ───────────────────────────────────────────────────────────────


def test_snippet_marks_every_occurrence_in_the_window() -> None:
    text = "alpha beta alpha gamma"

    assert storage_module._snippet_around(text, ["alpha"]) == ">>>alpha<<< beta >>>alpha<<< gamma"


def test_snippet_is_cut_around_the_first_match_with_ellipses() -> None:
    text = "x" * 500 + " needle " + "y" * 500

    snippet = storage_module._snippet_around(text, ["needle"], radius=20)

    assert snippet.startswith("...") and snippet.endswith("...")
    assert ">>>needle<<<" in snippet
    assert len(snippet) < 60


def test_snippet_is_case_insensitive_like_the_index() -> None:
    assert ">>>Docker<<<" in storage_module._snippet_around("Docker up", ["docker"])


def test_snippet_falls_back_to_the_head_when_no_term_is_present() -> None:
    assert storage_module._snippet_around("plain text", ["absent"]) == "plain text"
    assert storage_module._snippet_around("plain text", []) == "plain text"
    assert storage_module._snippet_around("x" * 50, ["absent"], radius=10) == "x" * 20 + "..."


def test_snippet_of_empty_content_is_empty() -> None:
    assert storage_module._snippet_around("", ["x"]) == ""


# ── the migration ──────────────────────────────────────────────────────────


_OLD_DDL = (
    "CREATE VIRTUAL TABLE transcript_fts "
    "USING fts5(content, content=transcript_entries, content_rowid=id)"
)


def test_the_tokenizer_is_read_from_the_stored_ddl() -> None:
    assert storage_module._fts_tokenizer_of(_OLD_DDL) == "unicode61"
    assert storage_module._fts_tokenizer_of("... tokenize='trigram')") == "trigram"
    assert storage_module._fts_tokenizer_of('... tokenize="Trigram")') == "trigram"
    assert (
        storage_module._fts_tokenizer_of("... tokenize = 'unicode61 remove_diacritics 1')")
        == "unicode61"
    )


@pytest.mark.asyncio
async def test_an_existing_unicode61_index_is_rebuilt_as_trigram(tmp_path: Path) -> None:
    """A database from before the switch keeps its old index under
    ``CREATE ... IF NOT EXISTS``; the migration must replace and rebuild it."""
    db = tmp_path / "sessions.db"
    store = SessionStorage(str(db))
    await store.connect()
    try:
        manager = SessionManager(store, inject_time_prefix=False)
        session = await manager.create("agent:main:webchat:aaaa0001", agent_id="main")
        await manager.append_message(session.session_key, role="user", content=ZH)
        await manager.append_message(session.session_key, role="user", content=POSTGRES)
    finally:
        await store.close()

    # Downgrade the index to what an old install has, with the old triggers.
    con = sqlite3.connect(db)
    for name in ("transcript_fts_ai", "transcript_fts_ad", "transcript_fts_au"):
        con.execute(f"DROP TRIGGER IF EXISTS {name}")
    con.execute("DROP TABLE transcript_fts")
    con.execute(_OLD_DDL)
    con.execute("INSERT INTO transcript_fts(transcript_fts) VALUES ('rebuild')")
    con.commit()
    assert (
        con.execute(
            "SELECT count(*) FROM transcript_fts WHERE transcript_fts MATCH '\"迁移计划\"'"
        ).fetchone()[0]
        == 0
    )
    con.close()

    store = SessionStorage(str(db))
    await store.connect()
    try:
        ddl = await (
            await store.conn.execute("SELECT sql FROM sqlite_master WHERE name = 'transcript_fts'")
        ).fetchone()
        assert "trigram" in str(ddl[0])
        assert len(await store.search_transcript("迁移计划")) == 1
        assert len(await store.search_transcript("postgres migration")) == 1
        # The triggers were recreated: a new entry is indexed.
        manager = SessionManager(store, inject_time_prefix=False)
        session = await manager.create("agent:main:webchat:aaaa0002", agent_id="main")
        await manager.append_message(session.session_key, role="user", content="fresh entry")
        assert len(await store.search_transcript("fresh")) == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_a_trigram_index_is_left_alone_on_reopen(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    for _ in range(2):
        store = SessionStorage(str(db))
        await store.connect()
        await store.close()

    con = sqlite3.connect(db)
    ddl = con.execute("SELECT sql FROM sqlite_master WHERE name = 'transcript_fts'").fetchone()[0]
    con.close()
    assert "trigram" in ddl


@pytest.mark.asyncio
async def test_deleting_an_entry_drops_it_from_the_index(storage, manager, session) -> None:
    """The recreated delete trigger must pass the same text the insert trigger
    indexed, or FTS5's external-content bookkeeping goes stale."""
    hit = (await storage.search_transcript("postgres"))[0]

    await storage.conn.execute("DELETE FROM transcript_entries WHERE id = ?", (hit["id"],))
    await storage.conn.commit()

    assert await storage.search_transcript("postgres") == []
