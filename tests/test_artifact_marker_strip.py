"""``strip_artifact_markers_from_text`` removes the marker and nothing else.

The stripper feeds the ``chat.history`` RPC and channel delivery, so what it
leaves behind is what a user reads. Two ways it used to get that wrong:

* the match ended at the first ``]``, so a name with its own brackets
  (``Q3 Report [Draft].pdf``) left the marker's tail in the text (#2942) --
  and ending at the *last* ``]`` on the line instead deletes whatever follows
  the marker, such as a Markdown link;
* it consumed whitespace, newlines included, on both sides, so the text
  either side of a marker was glued together (``Line oneLine two``).
"""

from __future__ import annotations

import pytest

from agentos.artifacts import artifact_marker, strip_artifact_markers_from_text


def _marker(name: str, mime: str = "application/pdf") -> str:
    return artifact_marker({"name": name, "mime": mime})


def test_a_bracket_in_the_name_does_not_leave_the_marker_tail() -> None:
    text = "Here is the file.\n" + _marker("Q3 Report [Draft].pdf")
    assert strip_artifact_markers_from_text(text) == "Here is the file."


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "Report: " + _marker("q3.pdf") + " and read [the docs](https://ex.com).",
            "Report: and read [the docs](https://ex.com).",
            id="markdown-link-after-marker",
        ),
        pytest.param(
            "Done. " + _marker("Report (final) [v2].pdf") + " See notes [here].",
            "Done. See notes [here].",
            id="parens-and-brackets-in-name",
        ),
        pytest.param(
            _marker("a [1].png", "image/png") + " " + _marker("b.png", "image/png") + " done [ok]",
            "done [ok]",
            id="two-markers-then-text",
        ),
        pytest.param(
            "x " + _marker("notes[v2].md", "artifact") + " y [z]",
            "x y [z]",
            id="mime-fallback",
        ),
    ],
)
def test_text_after_the_marker_is_kept(text: str, expected: str) -> None:
    """A later ``]`` on the line belongs to the user's text, not the marker."""
    assert strip_artifact_markers_from_text(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "Line one\n" + _marker("chart.png", "image/png") + "\nLine two",
            "Line one\nLine two",
            id="marker-on-its-own-line",
        ),
        pytest.param(
            "First.\n\n" + _marker("c.png", "image/png") + "\n\nSecond.",
            "First.\n\nSecond.",
            id="marker-between-paragraphs",
        ),
        pytest.param(
            "Before " + _marker("x.png", "image/png") + " after",
            "Before after",
            id="marker-inside-a-line",
        ),
        pytest.param(
            "a\r\n" + _marker("x.png", "image/png") + "\r\nb",
            "a\nb",
            id="crlf",
        ),
    ],
)
def test_the_text_either_side_is_not_glued_together(text: str, expected: str) -> None:
    assert strip_artifact_markers_from_text(text) == expected


def test_a_marker_alone_strips_to_nothing() -> None:
    assert strip_artifact_markers_from_text(_marker("x.png", "image/png")) == ""


def test_text_without_a_marker_is_returned_unchanged() -> None:
    assert strip_artifact_markers_from_text("plain [text] here") == "plain [text] here"
