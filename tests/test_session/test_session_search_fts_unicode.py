from __future__ import annotations

from agentos.session.storage import SessionStorage


def test_ascii_alphanumeric_passthrough() -> None:
    """ASCII alphanumeric terms pass through unchanged, joined with OR (#2897)."""
    result = SessionStorage.sanitize_fts_query("hello world 123")
    assert result == '"hello" OR "world" OR "123"'


def test_cjk_ideographs_preserved() -> None:
    """CJK ideographs (Chinese, Japanese) are preserved as token characters."""
    result = SessionStorage.sanitize_fts_query("\u4e2d\u6587\u6d4b\u8bd5")
    assert "\u4e2d" in result
    assert "\u6587" in result
    assert "\u6d4b" in result
    assert "\u8bd5" in result


def test_japanese_mixed_preserved() -> None:
    """Japanese mixed script preserved."""
    result = SessionStorage.sanitize_fts_query("\u65e5\u672c\u8a9e\u3066\u3059\u3068")
    assert result == '"\u65e5\u672c\u8a9e\u3066\u3059\u3068"'


def test_accented_latin_preserved() -> None:
    """Accented Latin characters (é, ñ, ü) are preserved."""
    result = SessionStorage.sanitize_fts_query("caf\u00e9 se\u00f1or f\u00fcr")
    assert "\u00e9" in result
    assert "\u00f1" in result
    assert "\u00fc" in result


def test_cyrillic_preserved() -> None:
    """Cyrillic characters are preserved."""
    result = SessionStorage.sanitize_fts_query("\u043f\u0440\u043e\u0431\u0430")
    assert result == '"\u043f\u0440\u043e\u0431\u0430"'


def test_hangul_preserved() -> None:
    """Hangul syllables are preserved."""
    result = SessionStorage.sanitize_fts_query("\ud55c\uad6d\uc5b4")
    assert result == '"\ud55c\uad6d\uc5b4"'


def test_arabic_preserved() -> None:
    """Arabic script is preserved."""
    result = SessionStorage.sanitize_fts_query("\u0627\u062e\u062a\u0628\u0627\u0631")
    assert result == '"\u0627\u062e\u062a\u0628\u0627\u0631"'


def test_mixed_ascii_unicode_preserved() -> None:
    """Mixed ASCII + Unicode query preserves all characters."""
    result = SessionStorage.sanitize_fts_query("hello \u4e16\u754c\u5730\u56fe")
    assert result == '"hello" OR "\u4e16\u754c\u5730\u56fe"'


def test_fts_operators_stripped() -> None:
    """FTS5 operators (*, -, AND, OR, NEAR) are stripped."""
    result = SessionStorage.sanitize_fts_query("hello* -world AND NOT NEAR/5")
    assert '"hello"' in result
    assert '"world"' in result
    assert "*" not in result
    assert '"AND"' in result
    assert '"NOT"' in result
    assert '"NEAR"' in result
    # The only bare operator is the one the builder itself inserts.
    assert result == '"hello" OR "world" OR "AND" OR "NOT" OR "NEAR"'


def test_punctuation_stripped() -> None:
    """Punctuation and special characters are stripped."""
    result = SessionStorage.sanitize_fts_query('hello, world! how\'s "it" going?')
    # ``s`` and ``it`` are below the trigram floor and never reach the index;
    # ``search_transcript`` still marks them in the snippet.
    assert result == '"hello" OR "world" OR "how" OR "going"'


def test_empty_input() -> None:
    """Empty input returns empty string match."""
    result = SessionStorage.sanitize_fts_query("")
    assert result == '""'


def test_whitespace_only() -> None:
    """Whitespace-only input returns empty string match."""
    result = SessionStorage.sanitize_fts_query("   \t\n  ")
    assert result == '""'


def test_token_limit_20() -> None:
    """Input with more than 20 tokens is capped to 20 tokens."""
    result = SessionStorage.sanitize_fts_query(" ".join(f"term{i:02d}" for i in range(25)))
    tokens = result.split(" OR ")
    assert len(tokens) == 20


def test_unicode_token_limit() -> None:
    """Unicode tokens count toward the 20-token limit correctly."""
    tokens_list = [chr(0x4E00 + i) * 3 for i in range(25)]
    query = " ".join(tokens_list)
    result = SessionStorage.sanitize_fts_query(query)
    tokens = result.split(" OR ")
    assert len(tokens) == 20
