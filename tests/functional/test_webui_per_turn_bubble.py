"""Static-text acceptance: chat.js contains per-turn bubble markers.

These checks do not require a browser or network. They verify the generated
JavaScript source ships the expected per-turn semantics introduced in Phase 3:
- The bubble tooltip uses 'Turn — input:' (per-turn label, not session total).
- The bubble chip argument is 'u.input_tokens | 0' (per-turn source, not the
  session accumulator).
"""

from __future__ import annotations

from pathlib import Path

_CHAT_JS = (
    Path(__file__).parent.parent.parent
    / "src"
    / "agentos"
    / "gateway"
    / "static"
    / "js"
    / "views"
    / "chat.js"
)


def test_chat_js_bubble_tooltip_uses_per_turn_label():
    """chat.js must label the bubble tooltip with 'Turn — input:' so the UI
    clearly shows per-turn semantics to users."""
    assert _CHAT_JS.exists(), f"chat.js not found at {_CHAT_JS}"
    source = _CHAT_JS.read_text(encoding="utf-8")
    assert "Turn — input:" in source, (
        "Expected per-turn tooltip label 'Turn — input:' not found in chat.js"
    )


def test_chat_js_bubble_chip_uses_per_turn_argument():
    """chat.js must pass 'u.input_tokens | 0' (per-turn field) to the bubble
    chip, not the session accumulator, ensuring per-bubble token counts reflect
    only the current turn."""
    assert _CHAT_JS.exists(), f"chat.js not found at {_CHAT_JS}"
    source = _CHAT_JS.read_text(encoding="utf-8")
    assert "u.input_tokens | 0" in source, (
        "Expected per-turn chip argument 'u.input_tokens | 0' not found in chat.js"
    )
