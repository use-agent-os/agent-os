"""Issue #3543: emphasis pairing in render_telegram_html.

Two defects, both in ``_render_inline``'s six emphasis passes.

**Crossed tags.** The passes are independent ``re.sub`` calls over the whole
string, and by the time any of them runs the text already carries tags -- the
link pass parks ``<a href=...>`` first, and each pass leaves its own behind.
A ``.+?`` spans those happily, so a delimiter inside an element paired with
one outside it: ``**a*b** *i*`` came out ``<b>a<i>b</b> *i</i>``. Telegram
requires properly nested entities and answers ``400 Bad Request: can't parse
entities``; two of the four send paths retry as plain text and lose all
formatting, and ``edit()`` and the document caption have no retry at all, so
there the message is not delivered. #2032 reported this for ``***`` and
``___`` and was fixed with a dedicated pre-pass for those two spellings; every
other overlapping pair still reached it.

**Pairing with the wrong opener.** A lazy ``.+?`` pairs the nearest *closer*
with the *first* opener, where CommonMark pairs each closer with the nearest
opener before it. In ``Use **/*.py to match **all** Python files`` the run
before ``all`` cannot close (a space precedes it), so the match ran on to the
one after ``all`` and swallowed the real bold span.

Expected output is taken from ``markdown-it-py``, a declared dependency of
this project, in its ``commonmark`` preset.
"""

from __future__ import annotations

import itertools
import re

import pytest

from agentos.channels._telegram_formatting import render_telegram_html

_TAG_RE = re.compile(r"<(/?)([a-z]+)[^>]*>")


def _properly_nested(markup: str) -> bool:
    """What Telegram's entity parser requires of the HTML it is sent."""
    stack: list[str] = []
    for match in _TAG_RE.finditer(markup):
        closing, name = match.group(1), match.group(2)
        if closing:
            if not stack or stack.pop() != name:
                return False
        else:
            stack.append(name)
    return not stack


# ── crossed tags ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # The issue's first repro. CommonMark: <strong>a*b</strong> <em>i</em>.
        ("**a*b** *i*", "<b>a*b</b> <i>i</i>"),
        ("*a**b* **b**", "<i>a**b</i> <b>b</b>"),
        # The stray `*` cannot reach out of the <b> to pair with the trailing one.
        ("**bold with *italic** trailing*", "<b>bold with *italic</b> trailing*"),
    ],
)
def test_an_overlapping_pair_does_not_cross_a_tag(source: str, expected: str) -> None:
    assert render_telegram_html(source) == expected


@pytest.mark.parametrize(
    "source",
    [
        "**a*b** *i*",
        "*a**b* **b**",
        "**bold with *italic** trailing*",
        "**a ~~a**b**~~",
        "**a [**t**](https://x.test/a_b__c)",
        "*a**b* [t](https://x.test)**b**",
        "** **a*b**",
        "a** **a*b**",
        "*a**b* ~~a**b**~~",
    ],
)
def test_every_known_interleaving_now_nests(source: str) -> None:
    assert _properly_nested(render_telegram_html(source))


def test_a_delimiter_cannot_pair_across_a_link_boundary() -> None:
    """The link pass parks its tags before any emphasis pass runs, so an
    anchor is the first thing a ``.+?`` could straddle."""
    markup = render_telegram_html("**a [**t**](https://x.test/a_b__c)")

    assert _properly_nested(markup)
    assert '<a href="https://x.test/a_b__c">' in markup, "the URL is still intact"


def test_the_whole_cross_product_of_inline_shapes_nests() -> None:
    """A sweep rather than a list, because the failure was combinatorial:
    1615 of these 17576 produced interleaved tags before this change."""
    atoms = [
        "**b**", "*i*", "__u__", "~~s~~", "`c`", "[t](https://x.test)", "***x***",
        "plain", "a_b_c", "**a*b**", "*a**b*", "~~a**b**~~", "**a `c` b**",
        "[**t**](https://x.test/a_b__c)", "_i_", "<b>", "&", "**", "*", "`",
        "**a", "a**", "[t](https://x.test)**b**", "**/*.py", "___y___", "~~",
    ]  # fmt: skip

    offenders = [
        " ".join(combo)
        for combo in itertools.product(atoms, repeat=3)
        if not _properly_nested(render_telegram_html(" ".join(combo)))
    ]

    assert offenders == []


# ── pairing with the nearest opener ────────────────────────────────────────


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # The issue's second repro, and what markdown-it's commonmark preset
        # renders it as.
        (
            "Use **/*.py to match **all** Python files",
            "Use **/*.py to match <b>all</b> Python files",
        ),
        ("**a**b**", "<b>a</b>b**"),
        ("**one** and **two**", "<b>one</b> and <b>two</b>"),
    ],
)
def test_a_closer_pairs_with_the_nearest_opener(source: str, expected: str) -> None:
    assert render_telegram_html(source) == expected


@pytest.mark.parametrize(
    "source",
    ["Use **/*.py anywhere", "Match **/* then stop", "Run `ls **/*.ts` now", "src/**"],
)
def test_a_globstar_in_prose_is_left_alone(source: str) -> None:
    """One asterisk of a ``**`` run cannot open a single-asterisk span on its
    own, so a leftover globstar stays readable instead of becoming ``<i>*/</i>``."""
    assert render_telegram_html(source) == source.replace("`ls **/*.ts`", "<code>ls **/*.ts</code>")


@pytest.mark.parametrize(
    "source",
    [
        "The signature is f(*args, **kwargs)",
        "Pass *args and **kwargs through",
        "def f(*a, **kw): pass",
    ],
)
def test_a_python_signature_is_not_italicised(source: str) -> None:
    """The same rule on the closing side, and the one that shows up most.

    ``f(*args, **kwargs)`` rendered as ``f(<i>args, *</i>kwargs)``: the
    ``*`` of ``*args`` opened a span and the *second* asterisk of ``**``
    closed it, taking one of the pair with it. A Python signature in prose
    is ordinary traffic for a coding agent.
    """
    assert render_telegram_html(source) == source


# ── what must not change ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("**bold**", "<b>bold</b>"),
        ("*italic*", "<i>italic</i>"),
        ("__bold__", "<b>bold</b>"),
        ("_italic_", "<i>italic</i>"),
        ("~~struck~~", "<s>struck</s>"),
        ("***both***", "<b><i>both</i></b>"),
        ("___both___", "<b><i>both</i></b>"),
        ("**bold with *italic* inside**", "<b>bold with <i>italic</i> inside</b>"),
        ("*italic with **bold** inside*", "<i>italic with <b>bold</b> inside</i>"),
        ("~~struck with **bold** inside~~", "<s>struck with <b>bold</b> inside</s>"),
        ("__init__ and **bold**", "__init__ and <b>bold</b>"),
        ("snake_case_name", "snake_case_name"),
        ("5_000_000", "5_000_000"),
        ("2 * 3 * 4 = **24**", "2 * 3 * 4 = <b>24</b>"),
        ("a*b and **bold**", "a*b and <b>bold</b>"),
    ],
)
def test_ordinary_formatting_is_unchanged(source: str, expected: str) -> None:
    assert render_telegram_html(source) == expected


def test_a_dunder_is_still_declined_and_still_consumed() -> None:
    """``__init__`` is left as written, and the scan continues past it rather
    than re-entering it -- ``_init<b>x</b>`` would be a new way to be wrong."""
    assert render_telegram_html("__init__x__") == "__init__x__"
    assert render_telegram_html("__init__ then __bold__") == "__init__ then <b>bold</b>"


def test_a_code_span_still_shields_its_contents() -> None:
    assert render_telegram_html("`**not bold**`") == "<code>**not bold**</code>"


def test_a_link_with_markers_in_its_url_still_resolves() -> None:
    markup = render_telegram_html("[t](https://x.test/a_b__c)")

    assert markup == '<a href="https://x.test/a_b__c">t</a>'


def test_bold_link_text_still_renders_bold() -> None:
    markup = render_telegram_html("[**title**](https://x.test/p)")

    assert markup == '<a href="https://x.test/p"><b>title</b></a>'
    assert _properly_nested(markup)


@pytest.mark.parametrize(
    "source",
    [
        "# Head **bold**",
        "> quoted **b** and *i*",
        "- item **b**",
        "1. item *i*",
        "| a | b |\n| - | - |\n| **x** | *y* |",
        "```py\ncode **not bold**\n```",
    ],
)
def test_the_block_shapes_still_nest(source: str) -> None:
    assert _properly_nested(render_telegram_html(source))
