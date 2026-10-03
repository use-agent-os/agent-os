"""Safe Markdown-to-HTML rendering for Telegram Bot API messages."""

from __future__ import annotations

import html
import re
from collections.abc import Callable

# GFM's delimiter cell is *one or more* hyphens with an optional leading
# and/or trailing colon, so `-`, `--`, `:-`, `-:` and `:-:` are all valid.
# Demanding three eliminated the compact spellings, and a table written that
# way was not recognised as a table at all: the raw pipes and dashes were
# delivered to the reader as prose.
_TABLE_DELIMITER_RE = re.compile(r"^:?-+:?$")
# A fence opens with three or more backticks or tildes followed by an info
# string, which CommonMark takes to be the rest of the line: its first word is
# the language, anything after it is attributes this renderer has no use for.
# A backtick info string may not contain a backtick (that is what keeps a
# ``` code span unambiguous); a tilde one may contain anything, backticks
# included, which is the reason to reach for `~~~` at all. The info string is
# otherwise free-form (`c#`, `vb.net`, `.env`, `text/x-python`), so the
# language is sanitised for the class attribute separately rather than by
# refusing the fence -- a refused opener left the *closing* fence to open a
# block that swallowed the rest of the message. The closing fence must use the
# same character, be at least as long as the opener and carry no info string,
# so a ```` block can quote a ``` block verbatim and a ~~~ block a ``` one.
_FENCE_OPEN_RE = re.compile(
    r"^\s*(?:(?P<ticks>`{3,})(?P<tick_info>[^`]*)|(?P<tildes>~{3,})(?P<tilde_info>.*))$"
)
_FENCE_LANGUAGE_MAX_LENGTH = 32
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(?P<text>.+?)\s*#*\s*$")
_ORDERED_LIST_RE = re.compile(r"^(?P<indent>\s*)(?P<number>\d+)[.)]\s+(?P<text>.+)$")
_UNORDERED_LIST_RE = re.compile(r"^(?P<indent>\s*)[-+*]\s+(?P<text>.+)$")
# The destination may carry one level of balanced parentheses (CommonMark), so
# a Wikipedia disambiguator or a `#method_(args)` anchor is kept whole instead
# of being cut at the first `)` with the remainder rendered as text after the
# anchor. Deeper nesting is left as literal text rather than a truncated link.
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://(?:[^\s()<]|\([^\s()<]*\))+)\)")
_BARE_URL_RE = re.compile(r"(https?://(?:[^\s()<]|\([^\s()<]*\))+)")
# CommonMark's blockquote marker: up to 3 leading spaces, `>`, then at most
# one space before the content. `>quote` (no space) and `>` alone (an empty
# quote line, used to separate paragraphs within one quote) both match.
_BLOCKQUOTE_RE = re.compile(r"^ {0,3}>[ ]?(?P<text>.*)$")


def _find_closing_backtick_run(text: str, start: int, length: int) -> int:
    """Index of the next backtick run of *exactly* ``length``, at or after ``start``.

    CommonMark closes a code span on a backtick run of the same length as the
    opener -- not on any run that merely contains one. ``str.find`` cannot
    express that: searching for a one-backtick marker matches the first
    backtick of a two-backtick run, which is how ``` ` `` ` ``` (a span quoting
    a longer run, the ordinary way to show a literal backtick) came out as two
    empty spans with the quoted backticks deleted. Runs that are the wrong
    length are content, so they are skipped whole rather than a character at a
    time -- otherwise the scan would land inside the run it just rejected.
    """
    cursor = start
    while cursor < len(text):
        if text[cursor] != "`":
            cursor += 1
            continue
        run_end = cursor
        while run_end < len(text) and text[run_end] == "`":
            run_end += 1
        if run_end - cursor == length:
            return cursor
        cursor = run_end
    return -1


def _is_escaped(text: str, index: int) -> bool:
    """True when the character at *index* is preceded by an odd backslash run.

    CommonMark consumes a backslash together with the ASCII punctuation it
    escapes, so an escaped backtick is literal and cannot open a code span.
    Counting the whole run keeps a double backslash before a backtick a real
    delimiter: there the first backslash escapes the second, so the backtick
    itself is unescaped.
    """
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def _replace_code_spans(text: str) -> tuple[str, list[str]]:
    """Replace balanced Markdown code spans with private placeholders.

    A backslash-escaped backtick is literal and cannot *open* a span. The scan
    for the closer stays raw: matching the reference implementation, an
    escaped backtick still closes the span it sits in.
    """
    chunks: list[str] = []
    output: list[str] = []
    cursor = 0
    while cursor < len(text):
        if text[cursor] != "`" or _is_escaped(text, cursor):
            output.append(text[cursor])
            cursor += 1
            continue
        marker_end = cursor
        while marker_end < len(text) and text[marker_end] == "`":
            marker_end += 1
        marker = text[cursor:marker_end]
        closing = _find_closing_backtick_run(text, marker_end, len(marker))
        if closing < 0:
            output.append(marker)
            cursor = marker_end
            continue
        content = text[marker_end:closing]
        # CommonMark: drop one space from each end only when both are present
        # and the span is not all spaces, so `` ` ` `` stays a single space and
        # `` `  x  ` `` keeps one on each side instead of being stripped bare.
        if len(content) >= 2 and content[0] == " " and content[-1] == " " and content.strip():
            content = content[1:-1]
        placeholder = f"\x00TG_CODE_{len(chunks)}\x00"
        chunks.append(f"<code>{html.escape(content)}</code>")
        output.append(placeholder)
        cursor = closing + len(marker)
    return "".join(output), chunks


#: Builtins whose ``dir()`` enumerates the dunder protocol. Deriving the set
#: rather than typing it out means it tracks the interpreter: a dunder added in
#: a later Python is covered the day the runtime knows about it, and nobody has
#: to remember to extend a literal list.
_DUNDER_SOURCE_TYPES: tuple[type, ...] = (
    object,
    type,
    str,
    bytes,
    list,
    dict,
    set,
    tuple,
    int,
    float,
    complex,
    BaseException,
    slice,
    range,
    property,
    staticmethod,
    classmethod,
)

#: Dunders that live on no builtin type, so ``dir()`` cannot find them: module
#: attributes, the async protocol, and names defined by the standard library
#: rather than by the interpreter.
_EXTRA_DUNDER_NAMES = frozenset(
    {
        # module and package attributes
        "all", "builtins", "cached", "debug", "file", "loader", "main",
        "package", "path", "spec", "version", "future", "test", "author",
        # async protocol
        "aenter", "aexit", "aiter", "anext", "await",
        # class body and dataclass machinery
        "post_init", "slots", "weakref", "match_args", "dataclass_fields",
        # context manager and misc protocols the builtins do not carry
        "enter", "exit", "del", "getattr", "next", "index", "fspath",
        "copy", "deepcopy", "length_hint", "getstate", "setstate",
    }
)  # fmt: skip


def _python_dunder_names() -> frozenset[str]:
    """Inner names of the Python dunders this formatter must not eat.

    ``__init__`` written with ordinary whitespace around it is structurally
    identical to an intentional single-word ``__bold__`` -- both are a
    delimiter run with non-word characters outside it -- so the two can only be
    told apart by what sits between the underscores. Scoped to names Python
    actually defines, so ordinary emphasis like ``__also__`` still bolds.
    """
    derived: set[str] = set()
    for source in _DUNDER_SOURCE_TYPES:
        derived.update(
            name[2:-2]
            for name in dir(source)
            if name.startswith("__") and name.endswith("__") and len(name) > 4
        )
    return frozenset(derived | _EXTRA_DUNDER_NAMES)


_DUNDER_NAMES = _python_dunder_names()

#: ``(?:(?!__).)+?`` rather than ``.+?``: a delimiter run inside the span
#: would mean this opener had reached past a nearer one. See
#: :data:`_BOLD_ASTERISK_RE` for what that cost.
_BOLD_UNDERSCORE_RE = re.compile(r"__(?=\S)((?:(?!__).)+?)(?<=\S)__")

#: CommonMark pairs each closing delimiter run with the *nearest* opener
#: before it. A lazy ``.+?`` pairs the nearest *closer* with the first
#: opener instead, which is not the same thing when a run in between can
#: open but not close: in ``Use **/*.py to match **all** Python files`` the
#: run before ``all`` has a space in front of it, so ``(?<=\S)`` rejects it
#: as a closer, the match ran on to the one after ``all`` and swallowed the
#: real bold span -- ``<b>/*.py to match **all</b>``. Forbidding the
#: delimiter run inside the span makes the first opener fail here, the regex
#: moves on, and ``**all**`` matches on its own, which is what CommonMark
#: produces. A globstar is ordinary prose for an agent (#3543).
_BOLD_ASTERISK_RE = re.compile(r"\*\*(?=\S)((?:(?!\*\*).)+?)(?<=\S)\*\*")
_STRIKE_RE = re.compile(r"~~(?=\S)((?:(?!~~).)+?)(?<=\S)~~")
_BOLD_ITALIC_ASTERISK_RE = re.compile(r"\*\*\*(?=\S)((?:(?!\*\*\*).)+?)(?<=\S)\*\*\*")
_BOLD_ITALIC_UNDERSCORE_RE = re.compile(r"(?<!\w)___(?=[^\s_])((?:(?!___).)+?)(?<=[^\s_])___(?!\w)")

#: Tags this module emitted. Matching the name lets :func:`_tags_balanced`
#: tell a span that carries a whole element from one that cuts across it.
_EMITTED_TAG_RE = re.compile(r"<(/?)([a-z]+)[^>]*>")


def _tags_balanced(fragment: str) -> bool:
    """True when every tag *fragment* contains is opened and closed inside it.

    An emphasis pass runs over text an earlier pass has already marked up --
    the link pass parks ``<a href=...>`` before any of them, and each
    emphasis pass leaves its own tags behind. The patterns are plain ``re``
    and their ``.+?`` spans those tags happily, so a delimiter inside an
    element could pair with one outside it and the output came out
    interleaved: ``**a*b** *i*`` rendered as ``<b>a<i>b</b> *i</i>``.
    Telegram requires properly nested entities and answers ``400 Bad
    Request: can't parse entities``; two of the four send paths retry as
    plain text and lose all formatting, and ``edit()`` and the document
    caption have no retry at all, so the message is simply not delivered
    (#2032, #2308, #3543).

    A span whose tags do not balance is therefore not a span. Only the
    structure matters here, not validity: the fragment was produced by this
    module, so there is nothing to sanitise, just a question of whether the
    delimiters sit inside one element or straddle two.
    """
    open_tags: list[str] = []
    for match in _EMITTED_TAG_RE.finditer(fragment):
        closing, name = match.group(1), match.group(2)
        if closing:
            if not open_tags or open_tags.pop() != name:
                return False
        else:
            open_tags.append(name)
    return not open_tags


def _apply_inline(
    pattern: re.Pattern[str],
    text: str,
    build: Callable[[re.Match[str]], str | None],
) -> str:
    """``pattern.sub``, with the two outcomes a plain ``sub`` cannot express.

    *build* returns ``None`` to decline a match it recognised but does not
    want to rewrite -- a Python dunder, say. That is a real match, so it is
    consumed and the scan continues after it, exactly as returning
    ``match.group(0)`` from a ``sub`` callback did.

    A match whose span does not have balanced tags is a different case: it
    is not a pair at all, and consuming it would hide the closer from an
    opener that *can* reach it. The scan resumes one character past the
    opener instead, so ``<b>a*b</b> *i*`` still finds ``*i*``.
    """
    out: list[str] = []
    position = 0
    while (match := pattern.search(text, position)) is not None:
        if not _tags_balanced(match.group(1)):
            out.append(text[position : match.start() + 1])
            position = match.start() + 1
            continue
        replacement = build(match)
        out.append(text[position : match.start()])
        out.append(match.group(0) if replacement is None else replacement)
        position = match.end()
    out.append(text[position:])
    return "".join(out)


#: Same word-boundary guards as the italic pass in :func:`_render_inline`, so
#: ``snake_case`` survives the table-label strip too.
_ITALIC_UNDERSCORE_RE = re.compile(r"(?<!\w)_(?=[^\s_])(.+?)(?<=[^\s_])_(?!\w)")

#: Same pattern as the asterisk-italic pass in :func:`_render_inline`. The
#: lookarounds keep it off ``**bold**``; the ``**`` strip runs first anyway, so
#: ``***both***`` reaches this as ``*both*``.
#: Both ends carry both guards, so neither can land inside a ``**`` run:
#: CommonMark reads a delimiter *run*, and one asterisk of a pair is not a
#: single-asterisk delimiter. The underscore pattern below has always had
#: this -- ``(?=[^\s_])`` and ``(?<=[^\s_])`` are the same rule -- and the
#: asterisk one only half of it, which is why a leftover globstar opened a
#: span (``**/*.py`` -> ``<i>*/</i>.py``) and why the closing run of
#: ``f(*args, **kwargs)`` was read as a closer, giving
#: ``f(<i>args, *</i>kwargs)`` (#3543).
_ITALIC_ASTERISK_RE = re.compile(r"(?<!\*)\*(?!\*)(?=\S)(.+?)(?<=\S)(?<!\*)\*(?!\*)")


def _is_python_dunder(content: str) -> bool:
    return content in _DUNDER_NAMES


def _bold_underscore_html(match: re.Match[str]) -> str | None:
    """``None`` declines the match, which ``_apply_inline`` leaves as written."""
    if _is_python_dunder(match.group(1)):
        return None
    return f"<b>{match.group(1)}</b>"


def _bold_underscore_strip(match: re.Match[str]) -> str:
    if _is_python_dunder(match.group(1)):
        return match.group(0)
    return match.group(1)


#: CommonMark: a backslash before ASCII punctuation makes that character
#: literal -- the escape is consumed and the character must not act as a
#: delimiter. Applied to the raw text before ``html.escape``, so a parked
#: ``\<`` is restored as ``&lt;`` and cannot smuggle markup through.
_ESCAPED_PUNCT_RE = re.compile(r"\\([!-/:-@\[-`{-~])")


def _render_inline(text: str) -> str:
    protected, code_chunks = _replace_code_spans(text)
    escapes: list[str] = []

    def _park_escape(match: re.Match[str]) -> str:
        # Parked before the URL passes below, so an escaped bracket cannot
        # open a link and a backslash inside a destination never reaches the
        # emphasis passes; parked before `html.escape`, so the character
        # cannot smuggle markup either.
        escapes.append(match.group(1))
        return f"\x00TG_ESC_{len(escapes) - 1}\x00"

    protected = _ESCAPED_PUNCT_RE.sub(_park_escape, protected)
    rendered = html.escape(protected)
    hrefs: list[str] = []
    bare_urls: list[str] = []

    def _park_href(match: re.Match[str]) -> str:
        # Park the URL before the inline passes below run. They match `**`,
        # `__`, `~~` and `*` anywhere in the string, so a URL carrying those
        # was rewritten inside the attribute -- `foo__bar__baz` came out as
        # `foo<b>bar</b>baz` and Telegram rejected the message with
        # "can't find end tag of href".
        #
        # Only the URL is parked. The link *text* stays exposed on purpose:
        # `[**bold**](url)` is meant to render bold, and hiding the whole
        # anchor would silently drop that.
        hrefs.append(match.group(2))
        return f'<a href="\x00TG_HREF_{len(hrefs) - 1}\x00">{match.group(1)}</a>'

    def _park_bare_url(match: re.Match[str]) -> str:
        bare_urls.append(match.group(1))
        return f"\x00TG_URL_{len(bare_urls) - 1}\x00"

    rendered = _LINK_RE.sub(_park_href, rendered)
    rendered = _BARE_URL_RE.sub(_park_bare_url, rendered)
    # Every pass below runs through `_apply_inline`, not `re.sub`, because by
    # now the text already carries tags -- the link pass above parked
    # `<a href=...>`, and each pass leaves its own behind. A `.+?` spans those
    # happily, so a delimiter inside an element could pair with one outside it
    # and the result was interleaved rather than nested, which Telegram
    # refuses outright. `_apply_inline` drops a match whose span straddles a
    # tag and leaves the closer available to an opener that can reach it.
    #
    # `***both***` is one run, not a bold run next to an italic one, and it has
    # to be consumed before the `**` pass gets to it. Left to the passes below,
    # the bold pass took the first two markers and handed the capture the third
    # (`<b>*both</b>*`), then the italic pass paired that stray marker with the
    # trailing one *across* the closing tag: `<b><i>both</b></i>`.
    rendered = _apply_inline(
        _BOLD_ITALIC_ASTERISK_RE, rendered, lambda m: f"<b><i>{m.group(1)}</i></b>"
    )
    rendered = _apply_inline(
        _BOLD_ITALIC_UNDERSCORE_RE, rendered, lambda m: f"<b><i>{m.group(1)}</i></b>"
    )
    rendered = _apply_inline(_BOLD_ASTERISK_RE, rendered, lambda m: f"<b>{m.group(1)}</b>")
    # Not a blanket rewrite: `__init__` is a delimiter run with whitespace on
    # both sides, exactly like an intentional single-word `__bold__`, so the
    # content is what decides. A Python dunder is declined (Issue #2076).
    rendered = _apply_inline(_BOLD_UNDERSCORE_RE, rendered, _bold_underscore_html)
    rendered = _apply_inline(_STRIKE_RE, rendered, lambda m: f"<s>{m.group(1)}</s>")
    rendered = _apply_inline(_ITALIC_ASTERISK_RE, rendered, lambda m: f"<i>{m.group(1)}</i>")
    # Word-boundary guards keep `snake_case_identifiers` intact: an opening `_`
    # must not follow a word character and a closing one must not precede one.
    rendered = _apply_inline(_ITALIC_UNDERSCORE_RE, rendered, lambda m: f"<i>{m.group(1)}</i>")
    # Restore in reverse order of protection: code spans were parked first, so
    # they come back last and a restored code span is never rescanned. Escapes
    # are restored after the URLs and hrefs that may still carry one.
    for index, url in enumerate(bare_urls):
        rendered = rendered.replace(f"\x00TG_URL_{index}\x00", url)
    for index, href in enumerate(hrefs):
        rendered = rendered.replace(f"\x00TG_HREF_{index}\x00", href)
    for index, char in enumerate(escapes):
        rendered = rendered.replace(f"\x00TG_ESC_{index}\x00", html.escape(char))
    for index, chunk in enumerate(code_chunks):
        rendered = rendered.replace(f"\x00TG_CODE_{index}\x00", chunk)
    return rendered


def _plain_inline(text: str) -> str:
    """Remove common inline Markdown markers for table labels."""
    # Same hazard as `_render_inline`, with a worse outcome: the marker strip
    # below is a plain `str.replace`, so a URL containing `__`, `**` or `~~`
    # lost those characters outright and the reader was handed a link that does
    # not resolve. Park the URLs, strip the markers, put them back.
    hrefs: list[str] = []
    escapes: list[str] = []

    def _park_href(match: re.Match[str]) -> str:
        hrefs.append(match.group(2))
        return f"{match.group(1)} (\x00TG_HREF_{len(hrefs) - 1}\x00)"

    def _park_escape(match: re.Match[str]) -> str:
        # Same rule as `_render_inline`: consume `\<punct>` so the character
        # stays literal and cannot be stripped as a delimiter below. A
        # backslash before a non-punctuation character (`C:\Users`, `\d+`) is
        # left alone.
        escapes.append(match.group(1))
        return f"\x00TG_ESC_{len(escapes) - 1}\x00"

    text = _LINK_RE.sub(_park_href, text)
    text = _ESCAPED_PUNCT_RE.sub(_park_escape, text)
    text = text.replace("`", "")
    # `__` goes through the regex rather than `str.replace`: a blanket strip ate
    # the delimiters of `__init__` and handed the reader `init`, with not even a
    # tag left to hint that something had been removed.
    text = _BOLD_UNDERSCORE_RE.sub(_bold_underscore_strip, text)
    for marker in ("**", "~~"):
        text = text.replace(marker, "")
    # `_italic_` was never stripped here, so a header written with
    # underscore-italics kept its delimiters while its bold and strike
    # neighbours lost theirs. The sibling of the #1931 fix, which only reached
    # `_render_inline`.
    text = _ITALIC_UNDERSCORE_RE.sub(r"\1", text)
    # And the other spelling of italic, for the same reason: `*Metric*` kept
    # its asterisks inside the `<b>` wrapper while `_Metric_` lost its
    # underscores (#2964).
    text = _ITALIC_ASTERISK_RE.sub(r"\1", text)
    for index, href in enumerate(hrefs):
        text = text.replace(f"\x00TG_HREF_{index}\x00", href)
    for index, char in enumerate(escapes):
        text = text.replace(f"\x00TG_ESC_{index}\x00", char)
    return text.strip()


def _split_table_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith(r"\|"):
        stripped = stripped[:-1]

    cells: list[str] = []
    current: list[str] = []
    escaped = False
    code_marker_length = 0
    cursor = 0
    while cursor < len(stripped):
        char = stripped[cursor]
        if escaped:
            current.append(char)
            escaped = False
            cursor += 1
            continue
        if char == "\\":
            escaped = True
            current.append(char)
            cursor += 1
            continue
        if char == "`":
            marker_end = cursor
            while marker_end < len(stripped) and stripped[marker_end] == "`":
                marker_end += 1
            marker_length = marker_end - cursor
            if code_marker_length == 0:
                code_marker_length = marker_length
            elif code_marker_length == marker_length:
                code_marker_length = 0
            current.append(stripped[cursor:marker_end])
            cursor = marker_end
            continue
        if char == "|" and code_marker_length == 0:
            cells.append("".join(current).strip().replace(r"\|", "|"))
            current = []
        else:
            current.append(char)
        cursor += 1
    cells.append("".join(current).strip().replace(r"\|", "|"))
    return cells


def _is_table_start(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines) or "|" not in lines[index]:
        return False
    header = _split_table_row(lines[index])
    delimiter = _split_table_row(lines[index + 1])
    return (
        len(header) >= 2
        and len(header) == len(delimiter)
        and all(_TABLE_DELIMITER_RE.fullmatch(cell) for cell in delimiter)
    )


def _normalize_row(row: list[str], column_count: int) -> list[str]:
    """Pad short rows or truncate long ones to exactly *column_count* cells."""
    if len(row) < column_count:
        return row + [""] * (column_count - len(row))
    return row[:column_count]


def _render_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    clean_headers = [_plain_inline(header) for header in headers]
    column_count = len(headers)
    if column_count == 2:
        rendered = [f"<b>{html.escape(clean_headers[0])} — {html.escape(clean_headers[1])}</b>"]
        for row in rows:
            normalised = _normalize_row(row, column_count)
            label = normalised[0]
            value = normalised[1]
            clean_label = _plain_inline(label)
            if clean_label:
                rendered.append(f"<b>{html.escape(clean_label)}:</b> {_render_inline(value)}")
            elif value:
                rendered.append(_render_inline(value))
        return rendered

    rendered = [f"<b>{' · '.join(html.escape(header) for header in clean_headers)}</b>"]
    for row in rows:
        normalised = _normalize_row(row, column_count)
        cells = [
            f"<b>{html.escape(header)}:</b> {_render_inline(value)}"
            for header, value in zip(clean_headers, normalised)
            if value
        ]
        if cells:
            rendered.append(" · ".join(cells))
    return rendered


def _fence_language(info: str) -> str:
    """Return the language a fence info string declares, safe for a class attribute."""
    words = info.split()
    if not words:
        return ""
    return html.escape(words[0][:_FENCE_LANGUAGE_MAX_LENGTH], quote=True)


def _closing_fence_re(fence: str) -> re.Pattern[str]:
    return re.compile(rf"^\s*{re.escape(fence[0])}{{{len(fence)},}}\s*$")


def render_telegram_html(markdown: str) -> str:
    """Render a safe, mobile-friendly Telegram HTML subset from Markdown."""
    lines = markdown.splitlines()
    rendered: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        fence = _FENCE_OPEN_RE.match(line)
        if fence:
            if fence.group("ticks") is not None:
                marker, info = fence.group("ticks"), fence.group("tick_info")
            else:
                marker, info = fence.group("tildes"), fence.group("tilde_info")
            language = _fence_language(info)
            closing_fence = _closing_fence_re(marker)
            code_lines: list[str] = []
            index += 1
            while index < len(lines) and not closing_fence.match(lines[index]):
                code_lines.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            code = html.escape("\n".join(code_lines))
            if language:
                rendered.append(f'<pre><code class="language-{language}">{code}</code></pre>')
            else:
                rendered.append(f"<pre>{code}</pre>")
            continue

        if _is_table_start(lines, index):
            headers = _split_table_row(line)
            rows: list[list[str]] = []
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                row = _split_table_row(lines[index])
                rows.append(row)
                index += 1
            rendered.extend(_render_table(headers, rows))
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            # Telegram HTML forbids nested tags of the same type (<b> inside <b>).
            # Headings are wrapped in <b>...</b>, so redundant inner bold tags are removed.
            heading_text = (
                _render_inline(heading.group("text")).replace("<b>", "").replace("</b>", "")
            )
            rendered.append(f"<b>{heading_text}</b>")
            index += 1
            continue
        quote = _BLOCKQUOTE_RE.match(line)
        if quote:
            # Telegram's <blockquote> is a multiline element
            # (<blockquote>line 1\nline 2</blockquote>); one tag per line
            # renders as a stack of separate quote bubbles instead of one
            # contiguous quote, so consecutive quote lines — including bare
            # `>` lines that separate paragraphs within the quote — are
            # gathered and joined inside a single tag.
            quote_lines = [quote.group("text")]
            index += 1
            while index < len(lines):
                next_quote = _BLOCKQUOTE_RE.match(lines[index])
                if not next_quote:
                    break
                quote_lines.append(next_quote.group("text"))
                index += 1
            rendered.append(
                "<blockquote>"
                + "\n".join(_render_inline(quote_line) for quote_line in quote_lines)
                + "</blockquote>"
            )
            continue
        ordered = _ORDERED_LIST_RE.match(line)
        if ordered:
            rendered.append(
                f"{ordered.group('indent')}{ordered.group('number')}. "
                f"{_render_inline(ordered.group('text'))}"
            )
            index += 1
            continue
        unordered = _UNORDERED_LIST_RE.match(line)
        if unordered:
            rendered.append(
                f"{unordered.group('indent')}• {_render_inline(unordered.group('text'))}"
            )
            index += 1
            continue
        rendered.append(_render_inline(line))
        index += 1
    return "\n".join(rendered)
