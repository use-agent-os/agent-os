"""Issue #46: ``SessionNode.derived_title`` fills the pre-existing
``getattr(s, "derived_title", None)`` hook that always returned ``None``.

The property is the canonical fallback chain (``display_name`` → ``label``
→ short opaque id) used by ``sessions.list`` and ``sessions.resolve`` so
list rows and toolbar chips show something stable instead of an empty
string.
"""

from __future__ import annotations

from agentos.session.models import SessionNode


def test_derived_title_prefers_display_name() -> None:
    node = SessionNode(
        session_key="agent:main:main:abcd1234",
        session_id="zzz99999",
        display_name="My Chat",
    )

    assert node.derived_title == "My Chat"


def test_derived_title_falls_back_to_label() -> None:
    node = SessionNode(
        session_key="agent:main:main:abcd1234",
        session_id="abcd1234efgh",
        label="Pinned topic",
    )

    assert node.derived_title == "Pinned topic"


def test_derived_title_falls_back_to_short_session_id() -> None:
    node = SessionNode(
        session_key="agent:main:main:zzz",
        session_id="abcd1234efgh5678",
    )

    # No display_name/label → first 8 chars of the opaque session id,
    # matching the ``sessions.preview`` fallback chain in ``rpc_sessions``.
    assert node.derived_title == "abcd1234"


def test_derived_title_returns_none_when_nothing_set() -> None:
    node = SessionNode(session_key="agent:main:main:zzz", session_id="")

    assert node.derived_title is None
