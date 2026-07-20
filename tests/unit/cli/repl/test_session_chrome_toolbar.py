"""Issue #46: bottom-toolbar and assistant-label chrome behavior tests.

Pins:

* ``DEFAULT_ASSISTANT_LABEL`` defaults to ``agentos`` and is overridable
  via the ``AGENTOS_ASSISTANT_LABEL`` env var.
* ``_bottom_toolbar`` renders the friendly session title (when set),
  the short model alias, and the active router tier chip.
* ``_input_header_fragments`` reads the label from ``_toolbar_context``
  instead of the hard-coded ``cap`` literal.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from prompt_toolkit.formatted_text import to_formatted_text

from agentos.cli.tui.terminal import prompt as prompt_module
from agentos.cli.tui.terminal.prompt import (
    DEFAULT_ASSISTANT_LABEL,
    _bottom_toolbar,
    _input_header_fragments,
    _toolbar_context,
)


def _reset_toolbar_context(**overrides: object) -> dict[str, object | None]:
    """Snapshot-safe reset of the module-global toolbar context."""
    saved = dict(_toolbar_context)
    _toolbar_context.update(
        {
            "model": None,
            "session_id": None,
            "session_title": None,
            "router_tier": None,
            "suppress": None,
            "status": None,
            "assistant_label": DEFAULT_ASSISTANT_LABEL,
        }
    )
    _toolbar_context.update(overrides)
    return saved


@pytest.fixture
def isolated_toolbar_context():
    saved = _reset_toolbar_context()
    try:
        yield
    finally:
        _toolbar_context.clear()
        _toolbar_context.update(saved)


def test_default_assistant_label_is_agentos() -> None:
    """The single source of truth for the assistant speaker label.

    Hard-pinning the default here is intentional: a future PR that
    accidentally reintroduces ``"cap"`` as a default will fail this gate.
    """
    assert DEFAULT_ASSISTANT_LABEL == "agentos"


def test_assistant_label_env_override() -> None:
    """``AGENTOS_ASSISTANT_LABEL`` overrides the default at import time.

    Run in a subprocess so the module reload doesn't poison the
    in-process ``_toolbar_context`` reference held by other tests in
    this file (a prior in-process ``importlib.reload`` approach left
    sibling tests operating on a stale module dict).
    """
    script = (
        "from agentos.cli.tui.terminal.prompt import DEFAULT_ASSISTANT_LABEL; "
        "print(DEFAULT_ASSISTANT_LABEL)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={"AGENTOS_ASSISTANT_LABEL": "Hani", "PATH": ""},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "Hani"


def test_bottom_toolbar_renders_title_model_and_tier(isolated_toolbar_context: None) -> None:
    _reset_toolbar_context(
        model="openrouter/anthropic/claude-opus-4-8",
        session_id="agent:main:main:abcd1234",
        session_title="Fix login bug",
        router_tier="c3",
    )

    rendered = to_formatted_text(_bottom_toolbar())
    text = "".join(fragment[1] for fragment in rendered)

    # Title appears in full (preferred over the opaque key segment).
    assert "Fix login bug" in text
    # Model alias is the short tail.
    assert "claude-opus-4-8" in text
    # Active tier pin is visible as ``tier:c3``.
    assert "tier:c3" in text


def test_bottom_toolbar_falls_back_to_short_key_without_title(
    isolated_toolbar_context: None,
) -> None:
    _reset_toolbar_context(
        model="openrouter/anthropic/claude-opus-4-8",
        session_id="agent:main:main:abcd1234",
        session_title=None,
        router_tier=None,
    )

    rendered = to_formatted_text(_bottom_toolbar())
    text = "".join(fragment[1] for fragment in rendered)

    # No title → fall back to the trailing opaque key segment.
    assert "abcd1234" in text
    assert "claude-opus-4-8" in text
    # No tier chip when automatic routing is in effect.
    assert "tier:" not in text


def test_bottom_toolbar_empty_when_nothing_to_show(isolated_toolbar_context: None) -> None:
    _reset_toolbar_context()
    assert _bottom_toolbar().value == ""


def test_input_header_reads_label_from_toolbar_context(isolated_toolbar_context: None) -> None:
    """The waiting header must render whatever label ``_toolbar_context`` carries."""
    from agentos.cli.tui.terminal.stream import WaitingIndicator

    _reset_toolbar_context(
        status=WaitingIndicator(started_at=100.0),
        assistant_label="Hani",
    )

    fragments = to_formatted_text(_input_header_fragments())
    text = "".join(fragment[1] for fragment in fragments)
    assert text.startswith("◢ Hani  ")


def test_input_header_falls_back_to_default_label(isolated_toolbar_context: None) -> None:
    from agentos.cli.tui.terminal.stream import WaitingIndicator

    _reset_toolbar_context(
        status=WaitingIndicator(started_at=100.0),
        assistant_label=None,  # explicitly cleared
    )

    fragments = to_formatted_text(_input_header_fragments())
    text = "".join(fragment[1] for fragment in fragments)
    # Falls back to the module default rather than any hard-coded literal.
    assert text.startswith(f"◢ {DEFAULT_ASSISTANT_LABEL}  ")


def test_sync_session_chrome_from_state_mirrors_fields(isolated_toolbar_context: None) -> None:
    """``sync_session_chrome_from_state`` is the helper slash handlers call
    after mutating state so the toolbar repaints on the next console.print."""
    from agentos.cli.chat.session_state import ChatSessionState

    state = ChatSessionState(
        session_key="agent:main:main:xyz",
        model="openrouter/llm",
        display_name="My Session",
        router_hold_tier="c1",
    )

    prompt_module.sync_session_chrome_from_state(state)

    assert _toolbar_context["session_title"] == "My Session"
    assert _toolbar_context["router_tier"] == "c1"
    assert _toolbar_context["model"] == "openrouter/llm"
    assert _toolbar_context["session_id"] == "agent:main:main:xyz"


def test_sync_session_chrome_clears_tier_when_none(isolated_toolbar_context: None) -> None:
    """``/auto`` clears the tier; the helper must write ``None`` not skip it."""
    from agentos.cli.chat.session_state import ChatSessionState

    _toolbar_context["router_tier"] = "c3"  # stale from prior session
    state = ChatSessionState(
        session_key="agent:main:main:new",
        model="m",
        router_hold_tier=None,
    )

    prompt_module.sync_session_chrome_from_state(state)

    assert _toolbar_context["router_tier"] is None
