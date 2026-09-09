from __future__ import annotations

from typing import Any

import pytest

from agentos.channels._telegram_formatting import _plain_inline, render_telegram_html
from agentos.channels.telegram import TelegramApiError, TelegramChannel, TelegramChannelConfig
from agentos.channels.types import OutgoingMessage


def test_telegram_markdown_renders_bold_code_and_two_column_table() -> None:
    markdown = """Skill dùng `agentos channels list`.

AgentOS có **1 channel**:

| Thông tin | Giá trị |
| --- | --- |
| **Tên** | `telegram-test` |
| **Trạng thái** | ✅ Enabled |
"""

    rendered = render_telegram_html(markdown)

    assert "<code>agentos channels list</code>" in rendered
    assert "AgentOS có <b>1 channel</b>:" in rendered
    assert "<b>Thông tin — Giá trị</b>" in rendered
    assert "<b>Tên:</b> <code>telegram-test</code>" in rendered
    assert "<b>Trạng thái:</b> ✅ Enabled" in rendered
    assert "| --- |" not in rendered
    assert "**" not in rendered
    assert "`" not in rendered


def test_telegram_markdown_escapes_html_and_preserves_code_blocks() -> None:
    markdown = """# Result <safe>

Use **care & caution** with `x < 2`.

```python
if x < 2:
    print("&")
```
"""

    rendered = render_telegram_html(markdown)

    assert "<b>Result &lt;safe&gt;</b>" in rendered
    assert "Use <b>care &amp; caution</b> with <code>x &lt; 2</code>." in rendered
    assert (
        '<pre><code class="language-python">'
        "if x &lt; 2:\n    print(&quot;&amp;&quot;)</code></pre>"
    ) in rendered


def test_telegram_send_payload_auto_renders_html() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="token"))

    payload = channel._build_send_payload(  # noqa: SLF001
        OutgoingMessage(content="**Ready**: `agentos status`", reply_to="42")
    )

    assert payload == {
        "chat_id": "42",
        "text": "<b>Ready</b>: <code>agentos status</code>",
        "parse_mode": "HTML",
    }


def test_telegram_send_payload_respects_explicit_parse_mode_override() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="token"))

    payload = channel._build_send_payload(  # noqa: SLF001
        OutgoingMessage(
            content="*caller-owned*",
            reply_to="42",
            metadata={"parse_mode": "MarkdownV2"},
        )
    )

    assert payload["text"] == "*caller-owned*"
    assert payload["parse_mode"] == "MarkdownV2"


def test_telegram_send_payload_can_explicitly_disable_rendering() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="token"))

    payload = channel._build_send_payload(  # noqa: SLF001
        OutgoingMessage(content="**literal**", reply_to="42", metadata={"parse_mode": ""})
    )

    assert payload["text"] == "**literal**"
    assert "parse_mode" not in payload


@pytest.mark.asyncio
async def test_telegram_send_falls_back_to_plain_text_on_entity_parse_error() -> None:
    channel = TelegramChannel(TelegramChannelConfig(token="token"))
    calls: list[tuple[str, dict[str, Any] | None]] = []

    async def fake_api(method: str, payload: dict[str, Any] | None = None) -> dict[str, int]:
        calls.append((method, dict(payload or {})))
        if len(calls) == 1:
            raise TelegramApiError("Bad Request: can't parse entities")
        return {"message_id": 7}

    channel._api = fake_api  # type: ignore[method-assign]  # noqa: SLF001

    result = await channel.send(
        OutgoingMessage(content="**Ready**: `agentos status`", reply_to="42")
    )

    assert result == {"message_id": 7}
    assert calls[0][1] == {
        "chat_id": "42",
        "text": "<b>Ready</b>: <code>agentos status</code>",
        "parse_mode": "HTML",
    }
    assert calls[1][1] == {
        "chat_id": "42",
        "text": "**Ready**: `agentos status`",
    }


# ── Issue #1031: ragged table rows ──────────────────────────────────────


def test_two_column_table_with_short_row_pads_missing_cell() -> None:
    """A 2-column table row with only 1 cell should be padded, not dropped."""
    markdown = (
        "| Header A | Header B |\n"
        "| --- | --- |\n"
        "| Row 1 Only |\n"
        "| x | y |\n"
    )
    rendered = render_telegram_html(markdown)

    # Both rows must appear — the old `break` dropped "| x | y |".
    assert "<b>Header A — Header B</b>" in rendered
    assert "<b>Row 1 Only:</b>" in rendered
    assert "<b>x:</b> y" in rendered
    # No raw pipe characters should leak through.
    assert "| x | y |" not in rendered
    assert "| Row 1 Only |" not in rendered


def test_three_column_table_with_short_row_pads_missing_cells() -> None:
    """A 3-column table row missing trailing cells should be padded."""
    markdown = (
        "| A | B | C |\n"
        "| --- | --- | --- |\n"
        "| only-a |\n"
        "| x | y | z |\n"
    )
    rendered = render_telegram_html(markdown)

    assert "<b>A · B · C</b>" in rendered
    # The short row has only column A filled; B and C are empty → filtered out.
    assert "<b>A:</b> only-a" in rendered
    # The well-formed row after the ragged one must also render.
    assert "<b>A:</b> x" in rendered
    assert "<b>B:</b> y" in rendered
    assert "<b>C:</b> z" in rendered
    assert "| x | y | z |" not in rendered


def test_table_row_with_extra_columns_is_truncated() -> None:
    """A row with more cells than headers should be truncated, not break."""
    markdown = (
        "| A | B |\n"
        "| --- | --- |\n"
        "| 1 | 2 | 3 | 4 |\n"
        "| x | y |\n"
    )
    rendered = render_telegram_html(markdown)

    assert "<b>A — B</b>" in rendered
    # Extra columns (3, 4) should be silently truncated.
    assert "<b>1:</b> 2" in rendered
    assert "<b>x:</b> y" in rendered
    assert "3" not in rendered
    assert "4" not in rendered


def test_mixed_ragged_rows_all_render_without_raw_pipes() -> None:
    """Mix of short, exact, and long rows — none should leak raw Markdown."""
    markdown = (
        "| Name | Status | Notes |\n"
        "| --- | --- | --- |\n"
        "| alpha | ok | fine |\n"
        "| beta |\n"
        "| gamma | fail | bad | extra |\n"
        "| delta | ok | good |\n"
    )
    rendered = render_telegram_html(markdown)

    # All four data rows must be rendered (no break/abort).
    assert "<b>Name:</b> alpha" in rendered
    assert "<b>Status:</b> ok" in rendered
    assert "<b>Notes:</b> fine" in rendered
    assert "<b>Name:</b> beta" in rendered
    assert "<b>Name:</b> gamma" in rendered
    assert "<b>Status:</b> fail" in rendered
    assert "<b>Notes:</b> bad" in rendered
    assert "<b>Name:</b> delta" in rendered
    # "extra" from the long row should be truncated.
    assert "extra" not in rendered
    # No raw pipe characters.
    assert "|" not in rendered


@pytest.mark.parametrize(
    ("url", "marker"),
    [
        ("https://example.com/foo__bar__baz", "__"),
        ("https://example.com/a**b**c", "**"),
        ("https://example.com/a~~b~~c", "~~"),
        ("https://example.com/a*b*c", "*"),
    ],
)
def test_link_href_survives_inline_formatting_markers(url: str, marker: str) -> None:
    """A URL is an attribute value, not a place to look for Markdown.

    The inline passes matched `**`, `__`, `~~` and `*` anywhere in the string,
    so they rewrote the characters inside `href="..."`. Telegram then rejected
    the whole message with "can't find end tag of href", which loses the reply
    rather than degrading it.
    """
    rendered = render_telegram_html(f"[test]({url})")

    assert rendered == f'<a href="{url}">test</a>'
    assert "<b>" not in rendered
    assert "<i>" not in rendered
    assert "<s>" not in rendered


def test_two_links_with_markers_both_survive() -> None:
    """The placeholders are per-link, so several on one line stay distinct."""
    rendered = render_telegram_html("[a](https://x.test/a__b__c) and [c](https://y.test/d__e__f)")

    assert rendered == (
        '<a href="https://x.test/a__b__c">a</a> and <a href="https://y.test/d__e__f">c</a>'
    )


def test_formatting_around_a_link_still_renders() -> None:
    """Parking the URL must not disarm the inline passes for the rest."""
    rendered = render_telegram_html("**bold** then [t](https://x.test/a__b__c)")

    assert rendered == '<b>bold</b> then <a href="https://x.test/a__b__c">t</a>'


def test_formatting_inside_link_text_still_renders() -> None:
    """Only the URL is protected -- the link text is still Markdown.

    Hiding the whole anchor would have been the simpler fix and would have
    silently dropped this: `[**bold**](url)` is meant to come out bold.
    """
    assert render_telegram_html("[**bold** link](https://x.test/)") == (
        '<a href="https://x.test/"><b>bold</b> link</a>'
    )
    assert render_telegram_html("[*em*](https://x.test/)") == (
        '<a href="https://x.test/"><i>em</i></a>'
    )


def test_plain_inline_keeps_the_url_intact() -> None:
    """The table-label path strips markers with `str.replace`.

    That is worse than the HTML path: the characters are removed outright, so
    `foo__bar__baz` became `foobarbaz` and the reader got a link that does not
    resolve rather than a message Telegram refuses.
    """
    assert _plain_inline("[t](https://x.test/a__b__c)") == "t (https://x.test/a__b__c)"
    assert _plain_inline("[t](https://x.test/a~~b~~c)") == "t (https://x.test/a~~b~~c)"
    assert _plain_inline("[t](https://x.test/a**b**c)") == "t (https://x.test/a**b**c)"


def test_plain_inline_still_strips_markers_outside_a_url() -> None:
    """The stripping it exists for keeps working."""
    assert _plain_inline("**bold** and __also__ and ~~gone~~") == "bold and also and gone"


def test_a_url_inside_a_code_span_is_untouched() -> None:
    """Code spans were already protected; that must not regress."""
    assert render_telegram_html("`https://x.test/a__b__c`") == (
        "<code>https://x.test/a__b__c</code>"
    )


def test_multiline_blockquote_renders_as_single_tag() -> None:
    markdown = "> Line 1\n> Line 2\n> Line 3"
    rendered = render_telegram_html(markdown)

    assert rendered == "<blockquote>Line 1\nLine 2\nLine 3</blockquote>"


def test_blockquote_with_empty_quote_line_preserves_internal_paragraphs() -> None:
    markdown = "> Paragraph 1\n>\n> Paragraph 2"
    rendered = render_telegram_html(markdown)

    assert rendered == "<blockquote>Paragraph 1\n\nParagraph 2</blockquote>"
    assert "&gt;" not in rendered


def test_blockquote_syntax_variations_and_inline_formatting() -> None:
    markdown = (
        "> **Note**: check `status`\n"
        ">visit [AgentOS](https://agentos.dev)\n"
        "  >   indented quote"
    )
    rendered = render_telegram_html(markdown)

    assert rendered == (
        "<blockquote><b>Note</b>: check <code>status</code>\n"
        'visit <a href="https://agentos.dev">AgentOS</a>\n'
        "  indented quote</blockquote>"
    )


def test_distinct_blockquotes_separated_by_blank_lines_remain_separate() -> None:
    markdown = "> First block\n\n> Second block"
    rendered = render_telegram_html(markdown)

    assert rendered == (
        "<blockquote>First block</blockquote>\n\n"
        "<blockquote>Second block</blockquote>"
    )
