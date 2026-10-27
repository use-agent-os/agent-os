"""Tests for the assistant-text display strip shared with the web console.

The first block ports the web console's vitest cases for the strip
(``frontend/src/views/chat/logic.test.ts``, ``transcript/tools.test.ts``,
``transcript/message.test.ts``) with the same inputs and expected outputs.
"""

from __future__ import annotations

import random
import re

import pytest

from agentos.chat.display_text import (
    AssistantTextStreamSanitizer,
    looks_like_protocol_text_suffix,
    strip_assistant_text,
    strip_directive_tags,
    strip_generated_artifact_markers,
    strip_protocol_text_leak,
)

LP = (
    "[generated artifact omitted: ranges-boar-base-20260927T105305Z.json "
    "(application/vnd.agentos.lp+json)]"
)
DCA = (
    "[generated artifact omitted: mandate-eth-20260928T054800Z.json "
    "(application/vnd.agentos.dca+json)]"
)
LEAK = (
    'Useful answer\n<invoke name="exec_command"><parameter name="command">pwd</parameter></invoke>'
)

# ECMAScript whitespace, which is what JS ``\s`` and ``trim()`` use.
_JS_WS = re.compile("[\t\n\x0b\x0c\r \xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+")


def _squash(text: str) -> str:
    """Collapse whitespace runs and trim, so only whitespace differences vanish."""
    return _JS_WS.sub(" ", text).strip(" ")


def _stream(chunks: list[str]) -> str:
    sanitizer = AssistantTextStreamSanitizer()
    return "".join(sanitizer.feed(chunk) for chunk in chunks) + sanitizer.flush()


# --- logic.test.ts: historyFallbackMessageIdentity / strip pipeline ---------


def test_assistant_fallback_text_is_trimmed() -> None:
    # historyFallbackText('assistant', text) is stripAssistantText(text).trim().
    assert strip_assistant_text("  hi  ").strip() == "hi"


def test_strips_directives_and_generated_artifact_markers_from_assistant_rows() -> None:
    assert strip_directive_tags("[[reply_to: abc]]\nanswer") == "answer"
    assert strip_generated_artifact_markers("answer [generated artifact omitted: chart.png]") == (
        "answer"
    )
    text = "[[reply_to_current]] answer [generated artifact omitted: chart.png]"
    assert strip_assistant_text(text).strip() == "answer"


def test_strips_every_assistant_marker_through_one_shared_helper() -> None:
    assert strip_assistant_text(f"Done.\n\n{LP}") == "Done."
    assert strip_assistant_text(f"[[reply_to_current]] Done. {LP}") == "Done."
    assert strip_assistant_text(LP) == ""
    assert strip_assistant_text(f"Started.\n\n{DCA}") == "Started."


def test_strips_a_positive_scaffold_suffix_but_preserves_angle_bracket_text() -> None:
    assert strip_protocol_text_leak("Useful answer\n<angle brackets> leaked") == "Useful answer"
    assert strip_protocol_text_leak("Use <details> for optional prose") == (
        "Use <details> for optional prose"
    )


# --- tools.test.ts / message.test.ts ----------------------------------------


def test_strips_the_marker_from_text_segments_between_tool_calls() -> None:
    segments = ["Reading the pool.", f"Here is the distribution.\n\n{LP}", LP]
    stripped = [strip_assistant_text(segment) for segment in segments]
    assert [text for text in stripped if text] == [
        "Reading the pool.",
        "Here is the distribution.",
    ]
    assert "generated artifact omitted" not in "".join(stripped)


def test_sanitizes_an_assistant_body_with_a_directive_but_keeps_call_markup() -> None:
    call = '<invoke name="tool"><parameter name="command">x</parameter></invoke>'
    assert strip_assistant_text(f"answer [[reply_to_current]] {call}") == f"answer {call}"


@pytest.mark.parametrize(
    "text",
    [
        LEAK,
        'Done.\n<invoke name="exec_command"><parameter name="command">pwd',
        'Checking.\n<tool_call>{"name":"read_file","arguments":{}}</tool_call>',
        "Ok <minimax:tool_call> x",
        "<function=read_file><parameter=path>a</parameter></function>",
        "<|channel|>commentary to=functions.exec<|message|>{}<|call|>",
    ],
)
def test_tool_call_markup_is_never_hidden(text: str) -> None:
    # The engine cuts the calls it ran; markup that reaches display never ran.
    assert strip_assistant_text(text) == text
    assert _stream(list(text)) == text


# --- JavaScript semantics ---------------------------------------------------


def test_none_and_empty_input_give_empty_text() -> None:
    for strip in (
        strip_directive_tags,
        strip_generated_artifact_markers,
        strip_protocol_text_leak,
        strip_assistant_text,
    ):
        assert strip(None) == ""
        assert strip("") == ""


# Expected values were produced by running logic.ts itself. Each row is
# (text, stripDirectiveTags, stripGeneratedArtifactMarkers,
# stripProtocolTextLeak, stripAssistantText). They pin where Python's own
# semantics would differ: the JS whitespace set (U+FEFF is whitespace, U+0085
# and U+001C are not), ASCII-only case folding (U+017F, U+212A) and ASCII
# word boundaries (U+00E9), the case-sensitive marker guard, "first protocol
# marker decides", and the whole-text whitespace normalisation.
PARITY_CASES = [
    (
        "Done.\ufeff\n<angle brackets> x",
        "Done.\ufeff\n<angle brackets> x",
        "Done.\ufeff\n<angle brackets> x",
        "Done.",
        "Done.",
    ),
    (
        "Done.\x85\n<angle brackets> x",
        "Done.\x85\n<angle brackets> x",
        "Done.\x85\n<angle brackets> x",
        "Done.\x85",
        "Done.\x85",
    ),
    (
        "\x1canswer [generated artifact omitted: x]",
        "\x1canswer [generated artifact omitted: x]",
        "\x1canswer",
        "\x1canswer [generated artifact omitted: x]",
        "\x1canswer",
    ),
    (
        "[[\x1creply_to_current]]x",
        "[[\x1creply_to_current]]x",
        "[[\x1creply_to_current]]x",
        "[[\x1creply_to_current]]x",
        "[[\x1creply_to_current]]x",
    ),
    (
        "[[\ufeffreply_to_current]]x",
        "x",
        "[[\ufeffreply_to_current]]x",
        "[[\ufeffreply_to_current]]x",
        "x",
    ),
    (
        "Answer <detail\u017f><summary>View areas around line 3</summary>",
        "Answer <detail\u017f><summary>View areas around line 3</summary>",
        "Answer <detail\u017f><summary>View areas around line 3</summary>",
        "Answer <detail\u017f><summary>View areas around line 3</summary>",
        "Answer <detail\u017f><summary>View areas around line 3</summary>",
    ),
    (
        "Answer <angle brac\u212aets> leaked",
        "Answer <angle brac\u212aets> leaked",
        "Answer <angle brac\u212aets> leaked",
        "Answer <angle brac\u212aets> leaked",
        "Answer <angle brac\u212aets> leaked",
    ),
    (
        "Answer <effect_calls\xe9 <effect_calls>",
        "Answer <effect_calls\xe9 <effect_calls>",
        "Answer <effect_calls\xe9 <effect_calls>",
        "Answer",
        "Answer",
    ),
    (
        "x [GENERATED ARTIFACT OMITTED: y]",
        "x [GENERATED ARTIFACT OMITTED: y]",
        "x [GENERATED ARTIFACT OMITTED: y]",
        "x [GENERATED ARTIFACT OMITTED: y]",
        "x [GENERATED ARTIFACT OMITTED: y]",
    ),
    (
        "a [GENERATED ARTIFACT OMITTED: y] b [generated artifact omitted: z]",
        "a [GENERATED ARTIFACT OMITTED: y] b [generated artifact omitted: z]",
        "ab",
        "a [GENERATED ARTIFACT OMITTED: y] b [generated artifact omitted: z]",
        "ab",
    ),
    (
        "Use <details> here.\n<angle brackets> x",
        "Use <details> here.\n<angle brackets> x",
        "Use <details> here.\n<angle brackets> x",
        "Use",
        "Use",
    ),
    (
        "Glued[generated artifact omitted: x]text",
        "Glued[generated artifact omitted: x]text",
        "Gluedtext",
        "Glued[generated artifact omitted: x]text",
        "Gluedtext",
    ),
    (
        "[[reply_to: ]]x",
        "x",
        "[[reply_to: ]]x",
        "[[reply_to: ]]x",
        "x",
    ),
    (
        "[[reply_to:]]x",
        "[[reply_to:]]x",
        "[[reply_to:]]x",
        "[[reply_to:]]x",
        "[[reply_to:]]x",
    ),
    (
        "[[\nreply_to_current\n]]x",
        "x",
        "[[\nreply_to_current\n]]x",
        "[[\nreply_to_current\n]]x",
        "x",
    ),
    (
        "[generated artifact omitted:\nfoo] bar",
        "[generated artifact omitted:\nfoo] bar",
        "bar",
        "[generated artifact omitted:\nfoo] bar",
        "bar",
    ),
    (
        "[generated artifact omitted: a\nb] bar",
        "[generated artifact omitted: a\nb] bar",
        "[generated artifact omitted: a\nb] bar",
        "[generated artifact omitted: a\nb] bar",
        "[generated artifact omitted: a\nb] bar",
    ),
    (
        'Page <html><invoke name="x">',
        'Page <html><invoke name="x">',
        'Page <html><invoke name="x">',
        'Page <html><invoke name="x">',
        'Page <html><invoke name="x">',
    ),
    (
        'Page <invoke name="x"> and </invoke>',
        'Page <invoke name="x"> and </invoke>',
        'Page <invoke name="x"> and </invoke>',
        'Page <invoke name="x"> and </invoke>',
        'Page <invoke name="x"> and </invoke>',
    ),
    (
        '<tool_call>{"name": "exec"}</tool_call>',
        '<tool_call>{"name": "exec"}</tool_call>',
        '<tool_call>{"name": "exec"}</tool_call>',
        '<tool_call>{"name": "exec"}</tool_call>',
        '<tool_call>{"name": "exec"}</tool_call>',
    ),
    (
        "Answer <angle brackets> leaked",
        "Answer <angle brackets> leaked",
        "Answer <angle brackets> leaked",
        "Answer",
        "Answer",
    ),
    (
        "Ok <minimax:tool_call> x",
        "Ok <minimax:tool_call> x",
        "Ok <minimax:tool_call> x",
        "Ok <minimax:tool_call> x",
        "Ok <minimax:tool_call> x",
    ),
    (
        "See <details><summary>View areas around lines 3",
        "See <details><summary>View areas around lines 3",
        "See <details><summary>View areas around lines 3",
        "See <details><summary>View areas around lines 3",
        "See <details><summary>View areas around lines 3",
    ),
    (
        "See <details><summary>View areas around line",
        "See <details><summary>View areas around line",
        "See <details><summary>View areas around line",
        "See",
        "See",
    ),
    (
        'Use <invoker name="x"> and <parameter>',
        'Use <invoker name="x"> and <parameter>',
        'Use <invoker name="x"> and <parameter>',
        'Use <invoker name="x"> and <parameter>',
        'Use <invoker name="x"> and <parameter>',
    ),
    (
        "a\r\n\r\n\r\n\r\nb [generated artifact omitted: c]",
        "a\r\n\r\n\r\n\r\nb [generated artifact omitted: c]",
        "a\n\nb",
        "a\r\n\r\n\r\n\r\nb [generated artifact omitted: c]",
        "a\n\nb",
    ),
    (
        "a  \t b\n\n\n\nc [generated artifact omitted: d]",
        "a  \t b\n\n\n\nc [generated artifact omitted: d]",
        "a b\n\nc",
        "a  \t b\n\n\n\nc [generated artifact omitted: d]",
        "a b\n\nc",
    ),
]


@pytest.mark.parametrize(("text", "directive", "marker", "protocol", "assistant"), PARITY_CASES)
def test_matches_the_web_console_strip(
    text: str, directive: str, marker: str, protocol: str, assistant: str
) -> None:
    assert strip_directive_tags(text) == directive
    assert strip_generated_artifact_markers(text) == marker
    assert strip_protocol_text_leak(text) == protocol
    assert strip_assistant_text(text) == assistant


def test_long_whitespace_runs_stay_linear() -> None:
    # The JS marker pattern retries its leading whitespace at every position,
    # which is quadratic; the port removes the same spans in linear time.
    text = "[generated artifact omitted: x] a" + " " * 200_000 + "b"
    assert strip_assistant_text(text) == "a b"


def test_looks_like_protocol_text_suffix() -> None:
    assert looks_like_protocol_text_suffix("<angle brackets>")
    assert looks_like_protocol_text_suffix("<details><summary>View areas around line 4")
    assert not looks_like_protocol_text_suffix('<invoke name="x"></invoke>')
    assert not looks_like_protocol_text_suffix('<parameter name="path">')
    assert not looks_like_protocol_text_suffix("<details> for optional prose")


# --- AssistantTextStreamSanitizer -------------------------------------------


def test_stream_removes_a_directive_split_across_chunks() -> None:
    sanitizer = AssistantTextStreamSanitizer()
    assert sanitizer.feed("[[reply") == ""
    assert sanitizer.feed("_to_current]] Hello") == "Hello"
    assert sanitizer.flush() == ""


def test_stream_removes_a_directive_fed_one_character_at_a_time() -> None:
    assert _stream(list("[[reply_to: msg-42]] Hi there")) == "Hi there"


def test_stream_holds_a_lone_trailing_bracket() -> None:
    sanitizer = AssistantTextStreamSanitizer()
    assert sanitizer.feed("see [") == "see"
    assert sanitizer.feed("1] ok") == " [1] ok"


def test_stream_releases_brackets_that_cannot_become_a_directive() -> None:
    sanitizer = AssistantTextStreamSanitizer()
    assert sanitizer.feed("[[1, 2]") == "[[1, 2]"
    assert sanitizer.feed("[link](x)") == "[link](x)"


def test_stream_releases_a_directive_hold_on_newline_or_past_its_limit() -> None:
    sanitizer = AssistantTextStreamSanitizer()
    assert sanitizer.feed("[[reply_to: a\nb") == "[[reply_to: a\nb"
    long_tag = "[[reply_to: " + "a" * 300
    assert AssistantTextStreamSanitizer().feed(long_tag) == long_tag


def test_stream_removes_a_generated_artifact_marker_split_across_chunks() -> None:
    sanitizer = AssistantTextStreamSanitizer()
    assert sanitizer.feed("Done.\n\n[generated art") == "Done."
    assert sanitizer.feed(LP[len("[generated art") :]) == ""
    assert sanitizer.feed("\n\nNext") == "Next"
    assert sanitizer.flush() == ""


def test_stream_releases_a_marker_hold_on_newline_or_past_its_limit() -> None:
    assert AssistantTextStreamSanitizer().feed("[generated artifact omitted: a\nb") == (
        "[generated artifact omitted: a\nb"
    )
    long_marker = "[generated artifact omitted: " + "a" * 600
    assert AssistantTextStreamSanitizer().feed(long_marker) == long_marker


def test_stream_drops_newlines_only_at_the_start_of_a_segment() -> None:
    sanitizer = AssistantTextStreamSanitizer()
    assert sanitizer.feed("\n\n") == ""
    assert sanitizer.feed("Hi") == "Hi"
    assert sanitizer.feed("\n\nthere") == "\n\nthere"


def test_stream_discards_a_scaffold_leak_and_the_rest_of_the_segment() -> None:
    sanitizer = AssistantTextStreamSanitizer()
    assert sanitizer.feed("Done.\n<ang") == "Done."
    assert sanitizer.feed("le bra") == ""
    assert sanitizer.feed("ckets> x") == ""
    assert sanitizer.feed(" and anything after") == ""
    assert sanitizer.flush() == ""


def test_stream_releases_an_unconfirmed_protocol_hold_at_flush() -> None:
    sanitizer = AssistantTextStreamSanitizer()
    assert sanitizer.feed("Use <details> for") == "Use"
    assert sanitizer.feed(" optional prose") == ""
    assert sanitizer.flush() == " <details> for optional prose"


def test_stream_holds_a_partial_protocol_marker_only_while_it_can_match() -> None:
    sanitizer = AssistantTextStreamSanitizer()
    assert sanitizer.feed("a <") == "a"
    assert sanitizer.feed("b> c <de") == " <b> c"
    assert sanitizer.feed("ck>") == " <deck>"


def test_stream_releases_a_protocol_hold_past_64_kib_and_stops_holding() -> None:
    sanitizer = AssistantTextStreamSanitizer()
    assert sanitizer.feed("Intro <details> ") == "Intro"
    body = "x" * (64 * 1024)
    assert sanitizer.feed(body) == " <details> " + body
    leak = '<parameter name="command">pwd'
    assert sanitizer.feed(leak) == leak


def test_stream_resets_after_flush() -> None:
    sanitizer = AssistantTextStreamSanitizer()
    sanitizer.feed("<angle brackets> x")
    assert sanitizer.flush() == ""
    assert sanitizer.feed("\n\nNext segment") == "Next segment"
    assert sanitizer.flush() == ""


def test_stream_ignores_empty_chunks() -> None:
    sanitizer = AssistantTextStreamSanitizer()
    assert sanitizer.feed("") == ""
    assert sanitizer.feed(None) == ""
    assert sanitizer.flush() == ""


STREAM_FIXTURES = [
    f"Done.\n\n{LP}",
    f"[[reply_to_current]] Done. {LP}",
    LP,
    f"Started.\n\n{DCA}",
    'Done.\n<invoke name="exec_command"><parameter name="command">pwd',
    LEAK,
    "Use <details> for optional prose",
    "[[reply_to: abc]]\nanswer",
    "[[reply_to_current]] answer [generated artifact omitted: chart.png]",
    'answer [[reply_to_current]] <invoke name="tool"><parameter name="command">x'
    "</parameter></invoke>",
    "\n\nHello [[reply_to: msg-1]] world, see arr[0], [[1, 2]] and a < b.\n",
    "Report:\n\n[generated artifact omitted: a.png]\n\n\n\n"
    "More [generated artifact omitted: b.png] end",
    "Glued[generated artifact omitted: x]text and [[reply_to_current]]more",
    "a\n\n[[reply_to: x]][generated artifact omitted: y]<b>",
    "Here <details><summary>View areas around line 12</summary> leaked",
    'Page: <!doctype html><html><body><invoke name="x">',
    "Plain <b>bold</b>, x <= y and an <invoker> word, <details> later",
    '<tool_call>{"name": "exec"}</tool_call>',
    "Answer <angle brackets> leaked",
    "[GENERATED ARTIFACT OMITTED: x] stays without the lowercase guard",
    # A word that only looks complete at a chunk boundary ("line" / "lines").
    "See <details><summary>View areas around lines 3</summary>",
    'Use <invoker name="x"> and </invoke> then <invoke name="y"> as prose',
]


@pytest.mark.parametrize("text", STREAM_FIXTURES)
def test_stream_matches_the_whole_text_strip_for_every_two_way_split(text: str) -> None:
    expected = _squash(strip_assistant_text(text))
    for index in range(len(text) + 1):
        assert _squash(_stream([text[:index], text[index:]])) == expected, index


@pytest.mark.parametrize("text", STREAM_FIXTURES)
def test_stream_matches_the_whole_text_strip_one_character_at_a_time(text: str) -> None:
    assert _squash(_stream(list(text))) == _squash(strip_assistant_text(text))


@pytest.mark.parametrize("text", STREAM_FIXTURES)
def test_stream_matches_the_whole_text_strip_for_random_chunkings(text: str) -> None:
    rng = random.Random(text)
    expected = _squash(strip_assistant_text(text))
    for _ in range(25):
        cuts = sorted(rng.sample(range(1, len(text)), min(len(text) - 1, rng.randint(1, 8))))
        bounds = [0, *cuts, len(text)]
        chunks = [text[start:end] for start, end in zip(bounds, bounds[1:])]
        assert _squash(_stream(chunks)) == expected, chunks
