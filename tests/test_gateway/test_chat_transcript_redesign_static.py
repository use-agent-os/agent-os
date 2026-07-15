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
