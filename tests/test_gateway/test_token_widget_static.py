"""Static tests for the TokenWidget cache hit display contract."""

from pathlib import Path

TOKEN_WIDGET_JS = Path("src/agentos/gateway/static/js/components/token-widget.js")


def test_token_widget_cache_hit_uses_input_denominator() -> None:
    source = TOKEN_WIDGET_JS.read_text(encoding="utf-8")

    assert "cr + (state.input || 0)" not in source
    assert "const input = Number(state.input || 0)" in source
    assert "const cacheRead = Number(state.cacheRead || 0)" in source
    assert "input <= 0" in source
    assert "cacheRead <= 0" in source
    assert "return cacheRead / input" in source


def test_token_widget_cache_hit_clamps_and_warns_on_impossible_counts() -> None:
    source = TOKEN_WIDGET_JS.read_text(encoding="utf-8")

    assert "cacheRead > input" in source
    assert "console.warn" in source
    assert "return 1" in source


def test_token_widget_card_shows_raw_cache_rows_when_present() -> None:
    source = TOKEN_WIDGET_JS.read_text(encoding="utf-8")

    assert "'Cache R'" in source
    assert "'Cache W'" in source
    assert "state.cacheRead > 0" in source
    assert "state.cacheWrite > 0" in source
