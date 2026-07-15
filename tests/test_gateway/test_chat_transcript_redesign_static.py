"""Source-pattern tests for the chat transcript redesign.

The chat view renders a single-column transcript (no left/right bubbles).
These tests pin the transcript visual language in chat.css / chat.js.
"""

from pathlib import Path

CHAT_JS = Path("src/agentos/gateway/static/js/views/chat.js")
CHAT_CSS = Path("src/agentos/gateway/static/css/views/chat.css")


def _css() -> str:
    return CHAT_CSS.read_text(encoding="utf-8")


def _js() -> str:
    return CHAT_JS.read_text(encoding="utf-8")


def test_user_turn_is_left_aligned_accent_bar_row() -> None:
    css = _css()
    start = css.index(".chat-msg--user .chat-msg-text,")
    end = css.index("/* Assistant", start)
    block = css[start:end]

    assert "border-left: 2px solid var(--accent);" in block
    assert "background: none;" in block
    assert "box-shadow: none;" in block
    assert "border-radius: 0;" in block
    assert "max-width: 100%;" in block
    assert "margin-left: auto" not in block

    align_start = css.index(".chat-msg--user,")
    align_block = css[align_start : css.index("}", align_start)]
    assert "align-items: flex-start;" in align_block


def test_user_role_label_and_time_are_not_right_aligned() -> None:
    css = _css()
    assert ".msg.user .role-label { text-align: right; }" not in css
    assert ".msg.user .msg-time { text-align: right; }" not in css


def test_assistant_turn_is_bare_prose() -> None:
    css = _css()
    start = css.index(".chat-msg--assistant .chat-msg-text,")
    end = css.index("/* Tool / system */", start)
    block = css[start:end]

    assert "background: none;" in block
    assert "box-shadow: none;" in block
    assert "font-size: var(--fs-md);" in block
    assert "line-height: 1.7;" in block
    assert "max-width: 100%;" in block
    assert "background: var(--bg-surface);" not in block


def test_tool_sidebar_is_fully_removed() -> None:
    assert "chat-sidebar" not in _css()
    assert "chat-sidebar" not in _js()


def test_tool_rows_have_left_rail_and_mono_row_styling() -> None:
    css = _css()
    start = css.index(".chat-tools-collapse {")
    end = css.index(".chat-tools-summary {", start)
    block = css[start:end]
    assert "border-left: 1px solid var(--border);" in block
    assert "border-radius: 0;" in block
    assert "background: none;" in block


def test_tool_status_shows_duration_from_js() -> None:
    js = _js()
    assert "function _fmtToolDuration(ms)" in js
    assert "details.dataset.startedAt" in js
    assert "_setToolSummaryStatus(details, " in js
    assert "_setToolSummaryStatus(details, isError ? 'error' : 'done')" not in js


def test_tool_state_glyphs_come_from_css_state_classes() -> None:
    css = _css()
    assert (
        ".chat-tools-collapse--success > .chat-tools-summary .chat-tools-status::before"
        in css
    )
    assert (
        ".chat-tools-collapse--error > .chat-tools-summary .chat-tools-status::before"
        in css
    )
