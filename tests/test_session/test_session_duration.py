from __future__ import annotations

from agentos.session.models import SessionNode


def test_duration_ms_uses_explicit_runtime_ms() -> None:
    node = SessionNode(
        session_key="k1",
        created_at=1000,
        started_at=1050,
        ended_at=2000,
        runtime_ms=950,
    )
    assert node.duration_ms == 950


def test_duration_ms_falls_back_to_started_and_ended_at() -> None:
    node = SessionNode(
        session_key="k2",
        created_at=1000,
        started_at=1200,
        ended_at=2500,
        runtime_ms=None,
    )
    assert node.duration_ms == 1300


def test_duration_ms_falls_back_to_created_and_updated_at() -> None:
    node = SessionNode(
        session_key="k3",
        created_at=1000,
        updated_at=1750,
        started_at=None,
        ended_at=None,
        runtime_ms=None,
    )
    assert node.duration_ms == 750


def test_duration_ms_clamped_to_zero_on_clock_skew() -> None:
    node = SessionNode(
        session_key="k4",
        created_at=2000,
        updated_at=1000,
        runtime_ms=None,
    )
    assert node.duration_ms == 0
