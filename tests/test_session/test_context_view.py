from __future__ import annotations

from agentos.provider import ContentBlockCompaction
from agentos.session.context_view import (
    build_compaction_context_items,
    build_compaction_context_records,
    build_provider_compaction_context,
)
from agentos.session.models import SessionContextState, SessionSummary


def test_provider_compaction_context_prefers_latest_state_independent_of_input_order() -> None:
    newer = SessionContextState(
        session_id="session",
        session_key="agent:main:ctx",
        provider="anthropic",
        model="claude-opus-4-7",
        state_kind="anthropic_compaction_block",
        payload={"content": "new native state"},
        covered_through_id=9,
        created_at=3000,
        portable=False,
        cacheable=True,
    )
    older = SessionContextState(
        session_id="session",
        session_key="agent:main:ctx",
        provider="anthropic",
        model="claude-opus-4-7",
        state_kind="anthropic_compaction_block",
        payload={"content": "old native state"},
        covered_through_id=7,
        created_at=1000,
        portable=False,
        cacheable=True,
    )

    context = build_provider_compaction_context(
        context_states=[newer, older],
        provider_kind="anthropic",
        now_ms=4000,
    )

    assert context.covered_through_ids == {9}
    assert len(context.messages) == 1
    block = context.messages[0].content[0]
    assert isinstance(block, ContentBlockCompaction)
    assert block.content == "new native state"


def test_compaction_context_items_deduplicate_structured_state_by_latest_coverage() -> None:
    newer = SessionContextState(
        session_id="session",
        session_key="agent:main:ctx",
        provider="portable",
        state_kind="structured_summary_v1",
        payload={
            "schema_version": 1,
            "current_status": "new structured state",
        },
        covered_through_id=7,
        created_at=3000,
        portable=True,
        cacheable=True,
    )
    older = SessionContextState(
        session_id="session",
        session_key="agent:main:ctx",
        provider="portable",
        state_kind="structured_summary_v1",
        payload={
            "schema_version": 1,
            "current_status": "old structured state",
        },
        covered_through_id=7,
        created_at=1000,
        portable=True,
        cacheable=True,
    )
    summary = SessionSummary(
        session_id="session",
        session_key="agent:main:ctx",
        summary_text="plain summary fallback",
        covered_through_id=7,
    )

    items = build_compaction_context_items(
        context_states=[newer, older],
        summaries=[summary],
        now_ms=4000,
    )

    rendered = "\n".join(items)
    assert "new structured state" in rendered
    assert "old structured state" not in rendered
    assert "plain summary fallback" not in rendered


def test_compaction_context_records_expose_correlation_metadata() -> None:
    state = SessionContextState(
        session_id="session",
        session_key="agent:main:ctx",
        provider="portable",
        state_kind="structured_summary_v1",
        payload={
            "schema_version": 1,
            "current_status": "structured state",
            "compaction_id": "cmp_state_1",
        },
        covered_through_id=9,
        portable=True,
        cacheable=True,
    )

    records = build_compaction_context_records(
        context_states=[state],
        summaries=[],
    )

    assert len(records) == 1
    assert records[0].compaction_id == "cmp_state_1"
    assert records[0].source == "context_state"
    assert records[0].covered_through_id == 9


def _summary(compaction_id: str, text: str, covered: int, **payload: object) -> SessionSummary:
    return SessionSummary(
        session_id="session",
        session_key="agent:main:ctx",
        compaction_id=compaction_id,
        summary_text=text,
        summary_payload={"schema_version": 1, "current_status": text, **payload},
        summary_format="structured_v1",
        covered_through_id=covered,
    )


def _state(summary: SessionSummary) -> SessionContextState:
    return SessionContextState(
        session_id="session",
        session_key="agent:main:ctx",
        provider="portable",
        state_kind="structured_summary_v1",
        payload={**(summary.summary_payload or {}), "compaction_id": summary.compaction_id},
        covered_through_id=summary.covered_through_id,
        created_at=1000 + summary.covered_through_id,
        portable=True,
        cacheable=True,
    )


def test_a_revised_summary_replaces_its_predecessors_in_replay() -> None:
    first = _summary("c1", "first", 3)
    second = _summary("c2", "second, revising first", 6, revises_compaction_id="c1")
    third = _summary("c3", "third, revising second", 9, revises_compaction_id="c2")
    summaries = [first, second, third]

    records = build_compaction_context_records(
        context_states=[_state(summary) for summary in summaries],
        summaries=summaries,
        now_ms=5000,
    )

    assert [record.compaction_id for record in records] == ["c3"]
    assert "third, revising second" in records[0].text
    # Plain summary rows (no structured state) follow the same rule.
    texts = build_compaction_context_items(context_states=[], summaries=summaries, now_ms=5000)
    assert texts == ["third, revising second"]


def test_summaries_without_a_revision_marker_replay_as_before() -> None:
    first = _summary("c1", "first", 3)
    second = _summary("c2", "second", 6)

    texts = build_compaction_context_items(context_states=[], summaries=[first, second], now_ms=1)

    assert texts == ["first", "second"]


def test_a_revision_that_is_not_replayed_hides_nothing() -> None:
    first = _summary("c1", "first", 3)
    revision = _summary("c2", "second", 6, revises_compaction_id="c1")

    # The caller filtered the revision out (it is inside the transcript, say):
    # its predecessor is still replayed.
    texts = build_compaction_context_items(context_states=[], summaries=[first], now_ms=1)
    assert texts == ["first"]
    texts = build_compaction_context_items(context_states=[], summaries=[first, revision], now_ms=1)
    assert texts == ["second"]
