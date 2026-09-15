"""apply_patch must round-trip a file's line endings, not normalise them.

Every assertion here is on the file's *bytes* after a full ``_apply_ops`` run.
Calling ``_apply_hunk`` directly and checking its return value passes against
the unfixed code, because ``read_text()`` already folded the endings away.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentos.tools.builtin.patch import _apply_ops, _parse_patch


def _apply(patch_text: str, root: Path) -> tuple[int, int, int]:
    return _apply_ops(_parse_patch(patch_text), root)


_UPDATE_WORLD = """*** Begin Patch
*** Update File: example.txt
@@@ -1,2 +1,2 @@@
 hello
-world
+WORLD
*** End Patch"""


def test_crlf_file_keeps_crlf(tmp_path: Path) -> None:
    target = tmp_path / "example.txt"
    target.write_bytes(b"hello\r\nworld\r\n")

    assert _apply(_UPDATE_WORLD, tmp_path) == (0, 1, 0)
    assert target.read_bytes() == b"hello\r\nWORLD\r\n"


def test_lf_file_keeps_lf(tmp_path: Path) -> None:
    target = tmp_path / "example.txt"
    target.write_bytes(b"hello\nworld\n")

    assert _apply(_UPDATE_WORLD, tmp_path) == (0, 1, 0)
    assert target.read_bytes() == b"hello\nWORLD\n"


def test_crlf_untouched_lines_are_not_rewritten(tmp_path: Path) -> None:
    """A one-line patch must not produce a whole-file diff."""
    target = tmp_path / "example.txt"
    original = b"a\r\nb\r\nc\r\nd\r\ne\r\n"
    target.write_bytes(original)

    patch_text = """*** Begin Patch
*** Update File: example.txt
@@@ -3,1 +3,1 @@@
-c
+C
*** End Patch"""
    assert _apply(patch_text, tmp_path) == (0, 1, 0)
    assert target.read_bytes() == b"a\r\nb\r\nC\r\nd\r\ne\r\n"


def test_crlf_context_lines_match(tmp_path: Path) -> None:
    """Context matching ignores the \\r that CRLF leaves on the line."""
    target = tmp_path / "example.txt"
    target.write_bytes(b"keep\r\ndrop\r\nkeep2\r\n")

    patch_text = """*** Begin Patch
*** Update File: example.txt
@@@ -1,3 +1,2 @@@
 keep
-drop
 keep2
*** End Patch"""
    assert _apply(patch_text, tmp_path) == (0, 1, 0)
    assert target.read_bytes() == b"keep\r\nkeep2\r\n"


def test_added_lines_use_the_file_ending(tmp_path: Path) -> None:
    target = tmp_path / "example.txt"
    target.write_bytes(b"one\r\ntwo\r\n")

    patch_text = """*** Begin Patch
*** Update File: example.txt
@@@ -1,1 +1,3 @@@
 one
+inserted
+also inserted
*** End Patch"""
    assert _apply(patch_text, tmp_path) == (0, 1, 0)
    assert target.read_bytes() == b"one\r\ninserted\r\nalso inserted\r\ntwo\r\n"


@pytest.mark.parametrize(
    "original,expected",
    [
        # CRLF majority -> inserted line is CRLF
        (b"a\r\nb\r\nc\n", b"a\r\ninserted\r\nb\r\nc\n"),
        # LF majority -> inserted line is LF
        (b"a\nb\nc\r\n", b"a\ninserted\nb\nc\r\n"),
        # Tie -> first ending seen wins
        (b"a\r\nb\n", b"a\r\ninserted\r\nb\n"),
        (b"a\nb\r\n", b"a\ninserted\nb\r\n"),
    ],
)
def test_mixed_ending_file_keeps_each_line_and_picks_a_majority(
    tmp_path: Path, original: bytes, expected: bytes
) -> None:
    target = tmp_path / "example.txt"
    target.write_bytes(original)

    patch_text = """*** Begin Patch
*** Update File: example.txt
@@@ -1,1 +1,2 @@@
 a
+inserted
*** End Patch"""
    assert _apply(patch_text, tmp_path) == (0, 1, 0)
    assert target.read_bytes() == expected


def test_file_without_trailing_newline_keeps_its_shape(tmp_path: Path) -> None:
    target = tmp_path / "example.txt"
    target.write_bytes(b"hello\r\nworld")

    assert _apply(_UPDATE_WORLD, tmp_path) == (0, 1, 0)
    # The last line gains an ending because apply_patch always terminates an
    # added line; the ending it gains is the file's own CRLF, not a bare \n.
    assert target.read_bytes() == b"hello\r\nWORLD\r\n"


def test_single_line_file_with_no_ending(tmp_path: Path) -> None:
    target = tmp_path / "example.txt"
    target.write_bytes(b"only")

    patch_text = """*** Begin Patch
*** Update File: example.txt
@@@ -1,1 +1,1 @@@
-only
+ONLY
*** End Patch"""
    assert _apply(patch_text, tmp_path) == (0, 1, 0)
    assert target.read_bytes() == b"ONLY\n"


def test_empty_file_is_not_a_crash(tmp_path: Path) -> None:
    target = tmp_path / "example.txt"
    target.write_bytes(b"")

    patch_text = """*** Begin Patch
*** Update File: example.txt
@@@ -1,0 +1,1 @@@
+first
*** End Patch"""
    assert _apply(patch_text, tmp_path) == (0, 1, 0)
    assert target.read_bytes() == b"first\n"


def test_add_file_emits_lf_on_every_platform(tmp_path: Path) -> None:
    """The patch text is the authority; os.linesep must not leak in."""
    patch_text = """*** Begin Patch
*** Add File: created.txt
+alpha
+beta
*** End Patch"""
    assert _apply(patch_text, tmp_path) == (1, 0, 0)
    # No trailing newline is the parser's behaviour for ``*** Add File`` (the
    # lines are joined, not terminated) — not something the newline handling
    # chose. The assertion is about the separator being LF, not the tail.
    assert (tmp_path / "created.txt").read_bytes() == b"alpha\nbeta"


# ---------------------------------------------------------------------------
# #1907: a context line that stops being the file's last line must gain a
# terminator, or the line appended after it is joined onto it.
# ---------------------------------------------------------------------------


def test_appending_after_an_unterminated_last_line_keeps_the_lines_apart(
    tmp_path: Path,
) -> None:
    target = tmp_path / "example.txt"
    target.write_bytes(b"hello\nworld")

    patch_text = """*** Begin Patch
*** Update File: example.txt
@@@ -2,1 +2,2 @@@
 world
+tail
*** End Patch"""
    assert _apply(patch_text, tmp_path) == (0, 1, 0)
    assert target.read_bytes() == b"hello\nworld\ntail\n"


def test_appending_after_an_unterminated_crlf_last_line_uses_crlf(tmp_path: Path) -> None:
    target = tmp_path / "example.txt"
    target.write_bytes(b"hello\r\nworld")

    patch_text = """*** Begin Patch
*** Update File: example.txt
@@@ -2,1 +2,2 @@@
 world
+tail
*** End Patch"""
    assert _apply(patch_text, tmp_path) == (0, 1, 0)
    assert target.read_bytes() == b"hello\r\nworld\r\ntail\r\n"


def test_create_then_append_does_not_merge_lines(tmp_path: Path) -> None:
    """``*** Add File`` writes no trailing newline, so this is the ordinary path."""
    create = """*** Begin Patch
*** Add File: notes.txt
+alpha
+beta
*** End Patch"""
    assert _apply(create, tmp_path) == (1, 0, 0)
    assert (tmp_path / "notes.txt").read_bytes() == b"alpha\nbeta"

    append = """*** Begin Patch
*** Update File: notes.txt
@@@ -2,1 +2,2 @@@
 beta
+gamma
*** End Patch"""
    assert _apply(append, tmp_path) == (0, 1, 0)
    assert (tmp_path / "notes.txt").read_bytes() == b"alpha\nbeta\ngamma\n"


def test_unterminated_last_line_that_stays_last_is_left_alone(tmp_path: Path) -> None:
    """The format has no ``\\ No newline`` marker, so the file's own shape wins."""
    target = tmp_path / "example.txt"
    target.write_bytes(b"hello\nworld")

    patch_text = """*** Begin Patch
*** Update File: example.txt
@@@ -1,1 +1,1 @@@
-hello
+HELLO
*** End Patch"""
    assert _apply(patch_text, tmp_path) == (0, 1, 0)
    assert target.read_bytes() == b"HELLO\nworld"


def test_unterminated_last_line_moved_up_by_an_insert_before_it(tmp_path: Path) -> None:
    """The context line is not the one being appended to; it is pushed down instead."""
    target = tmp_path / "example.txt"
    target.write_bytes(b"hello\nworld")

    patch_text = """*** Begin Patch
*** Update File: example.txt
@@@ -1,1 +1,2 @@@
 hello
+middle
*** End Patch"""
    assert _apply(patch_text, tmp_path) == (0, 1, 0)
    assert target.read_bytes() == b"hello\nmiddle\nworld"


@pytest.mark.parametrize("separator", ["\f", "\v", "\x1c", "\x85", "\u2028"])
def test_other_splitlines_boundaries_are_not_mistaken_for_unterminated_lines(
    tmp_path: Path, separator: str
) -> None:
    """A form feed (or any other str.splitlines boundary) is not a missing newline."""
    target = tmp_path / "example.txt"
    original = f"alpha\n{separator}\nbeta\ngamma\n".encode()
    target.write_bytes(original)

    patch_text = """*** Begin Patch
*** Update File: example.txt
@@@ -1,1 +1,1 @@@
-alpha
+ALPHA
*** End Patch"""
    assert _apply(patch_text, tmp_path) == (0, 1, 0)
    assert target.read_bytes() == original.replace(b"alpha", b"ALPHA")
