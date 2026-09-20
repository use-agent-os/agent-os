from __future__ import annotations

import pytest

from agentos.session.models import SessionNode, SessionStatus


@pytest.mark.parametrize(
    ("status", "expected_active", "expected_terminal"),
    [
        (SessionStatus.RUNNING, True, False),
        ("running", True, False),
        (SessionStatus.DONE, False, True),
        ("done", False, True),
        (SessionStatus.FAILED, False, True),
        ("failed", False, True),
        (SessionStatus.KILLED, False, True),
        ("killed", False, True),
        (SessionStatus.TIMEOUT, False, True),
        ("timeout", False, True),
        ("unknown_custom_status", False, False),
    ],
)
def test_session_node_is_active_and_is_terminal(
    status: str, expected_active: bool, expected_terminal: bool
) -> None:
    node = SessionNode(
        session_key="agent:main:test",
        session_id="test-123",
        status=status,
    )
    assert node.is_active is expected_active
    assert node.is_terminal is expected_terminal
