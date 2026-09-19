from __future__ import annotations

import html
import re
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


def test_multiline_blockquote_is_grouped_into_a_single_tag() -> None:
    """Issue #1532: Telegram's <blockquote> is a multiline element -- one tag
    per source line rendered as a stack of separate quote bubbles instead of
    one contiguous quote."""
    rendered = render_telegram_html("> Line 1\n> Line 2")

    assert rendered == "<blockquote>Line 1\nLine 2</blockquote>"


def test_blockquote_empty_line_separates_paragraphs_without_leaking_raw_gt() -> None:
    """A bare `>` line inside a quote must stay part of the quote as an empty
    line, not fall through to inline rendering and leak a raw `&gt;`."""
    rendered = render_telegram_html("> Paragraph 1\n>\n> Paragraph 2")

    assert rendered == "<blockquote>Paragraph 1\n\nParagraph 2</blockquote>"
    assert "&gt;" not in rendered


def test_blockquote_marker_without_a_space_is_recognized() -> None:
    rendered = render_telegram_html(">no space")

    assert rendered == "<blockquote>no space</blockquote>"


@pytest.mark.parametrize("indent", ["", " ", "  ", "   "])
def test_blockquote_allows_up_to_three_leading_spaces(indent: str) -> None:
    rendered = render_telegram_html(f"{indent}> quote")

    assert rendered == "<blockquote>quote</blockquote>"


def test_four_leading_spaces_is_not_a_blockquote_marker() -> None:
    """Four or more leading spaces is CommonMark's indented-code-block
    territory, not a blockquote -- the line falls through to plain
    (escaped) inline rendering, same as before this fix."""
    rendered = render_telegram_html("    > not a quote")

    assert rendered == "    &gt; not a quote"


def test_blockquote_stops_at_the_first_non_quote_line() -> None:
    rendered = render_telegram_html("> quoted\nnot quoted")

    assert rendered == "<blockquote>quoted</blockquote>\nnot quoted"


def test_blockquote_content_still_gets_inline_formatting() -> None:
    rendered = render_telegram_html("> **bold** and `code`")

    assert rendered == "<blockquote><b>bold</b> and <code>code</code></blockquote>"


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


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("This is _italic text_ in markdown.", "This is <i>italic text</i> in markdown."),
        ("_lead_ and _tail_", "<i>lead</i> and <i>tail</i>"),
        ("(_parenthesised_)", "(<i>parenthesised</i>)"),
        ("_multi_word_run_", "<i>multi_word_run</i>"),
        ("**bold** and _italic_ and *also*", "<b>bold</b> and <i>italic</i> and <i>also</i>"),
    ],
)
def test_single_underscore_renders_italic(markdown: str, expected: str) -> None:
    """`_text_` is the most common italic shape in LLM output; it reached
    Telegram as raw underscores while `*text*` and `__text__` rendered."""
    assert render_telegram_html(markdown) == expected


@pytest.mark.parametrize(
    "markdown",
    [
        "call snake_case_identifier here",
        "use _private and _internal names",
        "the value_ trailing_ ones",
        "__init__ style dunder",
        "a _ lone underscore _ pair",
        "no _italic_here because it continues",
        "sha_a1_b2 and sha_c3_d4",
    ],
)
def test_single_underscore_leaves_identifiers_alone(markdown: str) -> None:
    """Intraword underscores are not emphasis (CommonMark), so identifiers
    with several underscores must not sprout <i> tags.

    Every case now round-trips unchanged. The ``__init__`` one used to need a
    ``.replace("__init__", "<b>init</b>")`` here, which was this test working
    around the ``__bold__`` pass eating the dunder rather than asserting that
    it should — the stated contract was always the ``<i>`` check above. #2076
    made the identifier survive whole, so the concession is gone.
    """
    rendered = render_telegram_html(markdown)
    assert "<i>" not in rendered
    assert rendered == markdown


def test_single_underscore_does_not_touch_a_parked_link_or_code_span() -> None:
    rendered = render_telegram_html("[t](https://x.test/_a_b_) and `_code_` and _em_")
    assert rendered == (
        '<a href="https://x.test/_a_b_">t</a> and <code>_code_</code> and <i>em</i>'
    )


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("Press ` ` to jump.", "Press <code> </code> to jump."),
        ("run `  test  ` now", "run <code> test </code> now"),
        ("run ` test ` now", "run <code>test</code> now"),
        ("run `test ` now", "run <code>test </code> now"),
        ("run ` test` now", "run <code> test</code> now"),
        ("blank `   ` span", "blank <code>   </code> span"),
    ],
)
def test_code_span_keeps_interior_whitespace(markdown: str, expected: str) -> None:
    """CommonMark strips one leading and one trailing space only when both are
    present and the span is not all spaces; `.strip()` collapsed `` ` ` `` to
    an empty <code></code>."""
    assert render_telegram_html(markdown) == expected


# ---------------------------------------------------------------------------
# Issue #2003: balanced parentheses in a link destination; fence info strings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://en.wikipedia.org/wiki/Foo_(bar)",
        "https://docs.python.org/3/library/stdtypes.html#str.split_(sep)",
        "https://docs.python.org/3/library/stdtypes.html#str.split_(sep)?a=1&b=2",
        "https://example.com/a_(b)/c",
    ],
)
def test_link_href_keeps_balanced_parentheses(url: str) -> None:
    """Issue #2003: `_LINK_RE` cut the destination at the first `)`, so a
    Wikipedia disambiguator or a `#method_(args)` anchor produced an href to a
    page that does not exist plus a stray `)` (and anything after it) rendered
    as text after the anchor."""
    rendered = render_telegram_html(f"[test]({url})")

    assert rendered == f'<a href="{html.escape(url, quote=True)}">test</a>'


def test_link_with_parentheses_keeps_surrounding_text() -> None:
    rendered = render_telegram_html("see [x](https://en.wikipedia.org/wiki/Foo_(bar)) now")

    assert rendered == 'see <a href="https://en.wikipedia.org/wiki/Foo_(bar)">x</a> now'


def test_link_with_deeper_nesting_falls_back_to_literal_text() -> None:
    """One level of balanced parentheses is supported; deeper nesting renders
    the construct as literal text rather than as a truncated link."""
    rendered = render_telegram_html("[x](https://example.com/p_(q_(r)))")

    assert "<a " not in rendered
    assert "https://example.com/p_(q_(r)))" in rendered


def test_table_label_link_keeps_balanced_parentheses() -> None:
    """`_plain_inline` shares `_LINK_RE`, so the table path lost the same
    characters."""
    markdown = "| Name | Ref |\n| --- | --- |\n| a | [w](https://en.wikipedia.org/wiki/Foo_(bar)) |"
    rendered = render_telegram_html(markdown)

    assert 'href="https://en.wikipedia.org/wiki/Foo_(bar)"' in rendered
    assert "Foo_(bar))" not in rendered


@pytest.mark.parametrize(
    ("info", "expected_class"),
    [
        ("c#", "c#"),
        ("f#", "f#"),
        ("vb.net", "vb.net"),
        (".env", ".env"),
        ("text/x-python", "text/x-python"),
        ("python {.numberLines startFrom=1}", "python"),
        ("  rust  ", "rust"),
    ],
)
def test_fence_with_unusual_info_string_is_recognised(info: str, expected_class: str) -> None:
    """Issue #2005 (consolidated into #2003): `_FENCE_RE` rejected info strings
    outside `[A-Za-z0-9_+-]`, so the opening fence rendered as literal text and
    the *closing* fence opened a block that swallowed the rest of the message."""
    rendered = render_telegram_html(f"```{info}\nx\n```\n\n# Heading after\n\nbody text")

    assert rendered == (
        f'<pre><code class="language-{expected_class}">x</code></pre>\n\n'
        "<b>Heading after</b>\n\nbody text"
    )


def test_fence_language_is_escaped_for_the_class_attribute() -> None:
    rendered = render_telegram_html('```a"b<c\nx\n```')

    assert rendered == '<pre><code class="language-a&quot;b&lt;c">x</code></pre>'
    assert '"language-a"' not in rendered


def test_fence_language_is_capped_in_length() -> None:
    rendered = render_telegram_html(f"```{'l' * 80}\nx\n```")

    assert rendered == f'<pre><code class="language-{"l" * 32}">x</code></pre>'


def test_longer_fence_can_wrap_a_shorter_one() -> None:
    """A fence closes only on a run at least as long as the one that opened it
    (CommonMark), so a ```` block may quote a ``` block verbatim."""
    rendered = render_telegram_html("````md\n```\ninner\n```\n````\n\nafter")

    expected = '<pre><code class="language-md">```\ninner\n```</code></pre>\n\nafter'
    assert rendered == expected


def test_closing_fence_with_info_string_does_not_close() -> None:
    rendered = render_telegram_html("```\ncode\n```python\nstill code\n```\n\nafter")

    assert rendered == "<pre>code\n```python\nstill code</pre>\n\nafter"


def test_unterminated_fence_still_runs_to_the_end() -> None:
    rendered = render_telegram_html("```c#\nx\ny")

    assert rendered == '<pre><code class="language-c#">x\ny</code></pre>'


def test_link_with_unbalanced_parenthesis_falls_back_to_literal_text() -> None:
    """An unbalanced `(` used to yield a link truncated at it; now the whole
    construct stays literal text, the same fallback as deeper nesting."""
    rendered = render_telegram_html("[x](https://a.test/foo_(bar)")

    assert "<a " not in rendered
    assert "https://a.test/foo_(bar)" in rendered


def test_fence_info_string_with_a_backtick_does_not_open_a_block() -> None:
    """CommonMark: a backtick fence's info string may not contain a backtick."""
    rendered = render_telegram_html("```py `x`\nstill text")

    assert "<pre>" not in rendered
    assert "<code>x</code>" in rendered


# Issue #2022: a `~~~` fence is CommonMark too. Unrecognised, its body fell
# through to the inline passes and a code block was rendered as prose.

_FENCE_BODY = "**not bold** and [not a link](https://x.test) and <not html>"


def test_tilde_fence_renders_the_same_block_as_a_backtick_fence() -> None:
    tilde = render_telegram_html("~~~\n" + _FENCE_BODY + "\n~~~")
    backtick = render_telegram_html("```\n" + _FENCE_BODY + "\n```")

    assert tilde == backtick
    assert tilde == "<pre>**not bold** and [not a link](https://x.test) and &lt;not html&gt;</pre>"


def test_tilde_fence_carries_an_info_string() -> None:
    rendered = render_telegram_html("~~~python\nx = 1\n~~~")

    assert rendered == '<pre><code class="language-python">x = 1</code></pre>'


def test_tilde_fence_info_string_may_contain_backticks() -> None:
    """Unlike a backtick fence, a tilde fence's info string is unrestricted."""
    rendered = render_telegram_html("~~~py `x`\ncode\n~~~")

    assert rendered == '<pre><code class="language-py">code</code></pre>'


def test_tilde_fence_does_not_close_on_backticks() -> None:
    rendered = render_telegram_html("~~~python\nx\n```\ny\n~~~\n\nafter")

    assert rendered == '<pre><code class="language-python">x\n```\ny</code></pre>\n\nafter'


def test_backtick_fence_does_not_close_on_tildes() -> None:
    rendered = render_telegram_html("```\nx\n~~~\ny\n```\n\nafter")

    assert rendered == "<pre>x\n~~~\ny</pre>\n\nafter"


def test_tilde_fence_can_wrap_a_backtick_block_verbatim() -> None:
    rendered = render_telegram_html("~~~\n```\ninner\n```\n~~~")

    assert rendered == "<pre>```\ninner\n```</pre>"


def test_longer_tilde_fence_can_wrap_a_shorter_one() -> None:
    rendered = render_telegram_html("~~~~\n~~~\ninner\n~~~\n~~~~\n\nafter")

    assert rendered == "<pre>~~~\ninner\n~~~</pre>\n\nafter"


def test_unterminated_tilde_fence_runs_to_the_end() -> None:
    rendered = render_telegram_html("~~~\nx\ny")

    assert rendered == "<pre>x\ny</pre>"


def _entities_are_properly_nested(html_text: str) -> bool:
    """Telegram rejects a message whose entities are not properly nested."""
    stack: list[str] = []
    for closing, name in re.findall(r"<(/?)([a-zA-Z-]+)[^>]*>", html_text):
        if closing:
            if not stack or stack.pop() != name:
                return False
        else:
            stack.append(name)
    return not stack


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("~~gone~~", "<s>gone</s>"),
        ("a ~~b~~ c", "a <s>b</s> c"),
    ],
)
def test_two_tildes_are_still_strikethrough(markdown: str, expected: str) -> None:
    assert render_telegram_html(markdown) == expected


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("***both***", "<b><i>both</i></b>"),
        ("___both___", "<b><i>both</i></b>"),
        ("a***b***c", "a<b><i>b</i></b>c"),
        ("***a b***", "<b><i>a b</i></b>"),
    ],
)
def test_triple_marker_emphasis_nests_properly(markdown: str, expected: str) -> None:
    """``***x***`` is one run, not a bold run beside an italic one.

    Consumed by the ``**`` pass first, the third marker was left behind and the
    ``*`` pass then paired it with the trailing one across the closing tag,
    producing ``<b><i>x</b></i>``. Telegram's parser requires properly nested
    entities, and this adapter sends ``parse_mode=HTML`` with no plain-text
    retry, so the reply was refused rather than rendered.
    """
    rendered = render_telegram_html(markdown)

    assert rendered == expected
    assert _entities_are_properly_nested(rendered)


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("**bold**", "<b>bold</b>"),
        ("*italic*", "<i>italic</i>"),
        ("__bold__", "<b>bold</b>"),
        ("_italic_", "<i>italic</i>"),
        ("**a** *b*", "<b>a</b> <i>b</i>"),
        ("**a *b* c**", "<b>a <i>b</i> c</b>"),
        ("snake_case_name", "snake_case_name"),
        ("`***c***`", "<code>***c***</code>"),
    ],
)
def test_the_single_and_double_marker_runs_are_unchanged(markdown: str, expected: str) -> None:
    """The new pass must not take over anything the existing passes handled."""
    assert render_telegram_html(markdown) == expected


def test_a_triple_marker_run_beside_a_bold_run() -> None:
    rendered = render_telegram_html("***a*** and **b**")

    assert rendered == "<b><i>a</i></b> and <b>b</b>"
    assert _entities_are_properly_nested(rendered)
