"""Issue #2122: the video-merger concat manifest was written unescaped.

``f.write(f"file '{os.path.abspath(v)}'\\n")`` put a raw path inside single
quotes. FFmpeg parses that line with ``av_get_token``: inside single quotes
everything is literal until the *next* single quote, and outside quotes a
backslash escapes the following character. So a filename containing ``'``
closes the directive early, the remainder is re-read unquoted, and a Windows
path's backslashes are then eaten -- ``C:\\Users\\...`` becomes ``C:Users...``
and ffmpeg reports "Impossible to open".

These tests do not assert on the exact bytes written. They re-parse the line
with a port of ``av_get_token`` and assert the path FFmpeg would actually open,
because that is the property that matters and it survives a change of escaping
strategy.
"""

from __future__ import annotations

import ast
import importlib.util
import ntpath
import posixpath
import sys
from pathlib import Path

import pytest

_SRC = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "video-merger"
    / "src"
    / "video_merger.py"
)


def _merger_module():
    spec = importlib.util.spec_from_file_location("video_merger_under_test", _SRC)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def ff_get_token(text: str) -> str:
    """Port of FFmpeg's ``av_get_token`` (libavutil/avstring.c), space-terminated.

    This is the function the concat demuxer uses to read the argument of a
    ``file`` directive, so running the written line back through it answers the
    only question that matters: which path does ffmpeg end up opening?

    Faithful to the original in the two respects this issue turns on:
    outside quotes ``\\x`` yields ``x``; inside single quotes every character is
    copied verbatim until the closing quote.
    """
    out: list[str] = []
    i = 0
    text = text.lstrip(" \t")
    while i < len(text) and text[i] not in " \t\n\r":
        char = text[i]
        i += 1
        if char == "\\" and i < len(text):
            out.append(text[i])
            i += 1
        elif char == "'":
            while i < len(text) and text[i] != "'":
                out.append(text[i])
                i += 1
            if i < len(text):
                i += 1  # consume the closing quote
        else:
            out.append(char)
    return "".join(out)


def parsed_path(entry: str) -> str:
    """The path ffmpeg would open, given a full ``file '...'`` line."""
    assert entry.endswith("\n"), "a concat entry must be newline terminated"
    directive, _, argument = entry.rstrip("\n").partition(" ")
    assert directive == "file"
    return ff_get_token(argument)


# ── the oracle itself, so a green suite cannot rest on a broken parser ──────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("'/tmp/a.mp4'", "/tmp/a.mp4"),
        ("/tmp/a.mp4", "/tmp/a.mp4"),
        # A drive path under a project directory, not under a home directory:
        # `test_public_release_hygiene` rejects anything shaped like a
        # contributor's home path, and the semantics pinned here are identical
        # whichever directory the file sits in.
        (r"C:\Videos\a.mp4", "C:Videosa.mp4"),  # unquoted: backslashes are eaten
        (r"'C:\Videos\a.mp4'", r"C:\Videos\a.mp4"),  # quoted: verbatim
        ("'it'\\''s.mp4'", "it's.mp4"),  # the close/escape/reopen form
        ("'unterminated", "unterminated"),
    ],
)
def test_the_tokenizer_port_matches_ffmpeg_semantics(raw: str, expected: str) -> None:
    assert ff_get_token(raw) == expected


# ── what the merger writes ──────────────────────────────────────────────────


@pytest.fixture()
def concat_entry():
    return _merger_module().VideoMerger.concat_entry


@pytest.mark.parametrize(
    "name",
    [
        "plain.mp4",
        "01_user's_intro.mp4",
        "it's a 'quoted' clip.mp4",
        "''.mp4",
        "trailing'.mp4",
        "'leading.mp4",
        "with space.mp4",
        "back\\slash.mp4",
        "unicode_日本語.mp4",
        "hash#and%percent.mp4",
    ],
)
def test_every_written_entry_parses_back_to_the_real_path(
    concat_entry, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    """The property the whole fix exists for, over the awkward names."""
    target = tmp_path / name
    monkeypatch.setattr("os.path.abspath", lambda p: str(p))

    parsed = parsed_path(concat_entry(str(target)))

    assert parsed == str(target).replace("\\", "/")


def test_a_windows_path_survives_instead_of_losing_its_separators(
    concat_entry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported symptom: ``C:\\Users\\...`` opened as ``C:Users...``."""
    windows_path = r"C:\Videos\Segments\01_clip.mp4"
    monkeypatch.setattr("os.path.abspath", lambda p: p)

    parsed = parsed_path(concat_entry(windows_path))

    assert parsed == "C:/Videos/Segments/01_clip.mp4"
    assert "C:Videos" not in parsed


def test_a_single_quote_does_not_end_the_directive_early(
    concat_entry, monkeypatch: pytest.MonkeyPatch
) -> None:
    quoted = "/videos/01_user's_intro.mp4"
    monkeypatch.setattr("os.path.abspath", lambda p: p)

    entry = concat_entry(quoted)

    assert parsed_path(entry) == quoted
    assert entry.endswith("'\n"), "the directive must still be closed"


def test_a_windows_path_that_also_contains_a_quote(
    concat_entry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both faults at once, which is where a fix for only one of them shows."""
    path = r"C:\Videos\Bob's Clips\01.mp4"
    monkeypatch.setattr("os.path.abspath", lambda p: p)

    assert parsed_path(concat_entry(path)) == "C:/Videos/Bob's Clips/01.mp4"


def test_a_plain_posix_path_is_written_unchanged(
    concat_entry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The common case must not acquire escaping it does not need."""
    monkeypatch.setattr("os.path.abspath", lambda p: p)

    assert concat_entry("/videos/01.mp4") == "file '/videos/01.mp4'\n"


def test_the_entry_is_newline_terminated(concat_entry, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two clips on one line is one unreadable directive, not two files."""
    monkeypatch.setattr("os.path.abspath", lambda p: p)

    assert concat_entry("/videos/01.mp4").endswith("\n")


def test_a_relative_path_is_still_made_absolute(
    concat_entry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``-safe 0`` accepts absolute paths; the manifest lives in a temp dir, so a
    relative one would resolve against the wrong directory."""
    monkeypatch.setattr("os.path.abspath", lambda p: "/resolved/" + p)

    assert parsed_path(concat_entry("clip.mp4")) == "/resolved/clip.mp4"


def test_the_old_format_would_have_failed_the_same_assertions() -> None:
    """Pins *why* this changed: the previous line, re-parsed, does not come back.

    Without this the suite could pass against code that never had the bug, and
    the regression it guards would be invisible.
    """
    windows = r"C:\Videos\01.mp4"
    quoted = "/videos/user's.mp4"

    old_windows = f"file '{windows}'\n"
    old_quoted = f"file '{quoted}'\n"

    # A quoted backslash path does survive av_get_token untouched: inside
    # quotes the backslash is literal. So the issue's Windows symptom is only
    # reachable once something else has already closed the quote -- which is
    # what the apostrophe below does. Converting to forward slashes is still
    # worth doing, because it removes the escape character entirely rather
    # than relying on the quoting to keep holding.
    assert parsed_path(old_windows) == windows

    # The apostrophe is the live fault, and it does not merely truncate: the
    # quote closes, `s.mp4` is read unquoted, and the trailing quote opens a
    # new empty section. ffmpeg is handed a path that looks plausible and is
    # simply the wrong file -- the apostrophe has vanished.
    assert parsed_path(old_quoted) == "/videos/users.mp4"
    assert parsed_path(old_quoted) != quoted


# ── both call sites use it ──────────────────────────────────────────────────


def test_both_concat_manifests_go_through_the_escaper() -> None:
    """`merge` and `merge_chunks` each build their own manifest; fixing one and
    leaving the other is the obvious way for this bug to come back."""
    source = _SRC.read_text(encoding="utf-8")

    assert source.count("self.concat_entry(v)") == 2
    assert "f.write(f\"file '{os.path.abspath(v)}'" not in source


def test_the_helper_is_importable_without_ffmpeg_installed() -> None:
    """The escaping is pure string work and must be testable on a machine with
    no ffmpeg, which is how this suite runs in CI."""
    merger = _merger_module()

    assert callable(merger.VideoMerger.concat_entry)


@pytest.mark.parametrize("joiner", [posixpath, ntpath])
def test_paths_built_with_either_separator_land_on_forward_slashes(
    concat_entry, monkeypatch: pytest.MonkeyPatch, joiner
) -> None:
    monkeypatch.setattr("os.path.abspath", lambda p: p)
    path = joiner.join("videos", "segments", "01.mp4")

    assert "\\" not in parsed_path(concat_entry(path))


# ── #2431: the manifest is written in UTF-8, whatever the locale ────────────
#
# ``NamedTemporaryFile(mode='w')`` encodes with the system default -- GBK or
# CP1252 on many Windows hosts -- so a filename like ``1_场景一.mp4`` raised
# ``UnicodeEncodeError`` before ffmpeg was ever run. FFmpeg reads the manifest
# as UTF-8, so that is the only encoding the writer may use.


CJK_NAME = "1_场景一.mp4"


def _manifest_calls():
    """Every ``NamedTemporaryFile(mode='w', ...)`` call in the merger source."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != "NamedTemporaryFile":
            continue
        keywords = {k.arg: k.value for k in node.keywords}
        mode = keywords.get("mode")
        if isinstance(mode, ast.Constant) and mode.value == "w":
            calls.append(keywords)
    return calls


def test_both_manifests_are_opened_as_utf8() -> None:
    """The structural half: each text-mode temp file names its encoding."""
    calls = _manifest_calls()

    assert len(calls) == 2, "merge() and _merge_single_chunk() each write a manifest"
    for keywords in calls:
        encoding = keywords.get("encoding")
        assert isinstance(encoding, ast.Constant) and encoding.value == "utf-8"


def test_a_cjk_filename_survives_the_manifest_round_trip(
    concat_entry, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The behavioural half: written with the encoding the code now uses, the
    entry reads back as the same path -- byte-exactly what ffmpeg will open."""
    import tempfile

    monkeypatch.setattr("os.path.abspath", lambda p: p)
    entry = concat_entry(CJK_NAME)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8", dir=tmp_path
    ) as handle:
        handle.write(entry)
        written = Path(handle.name)

    assert parsed_path(written.read_text(encoding="utf-8")) == CJK_NAME


@pytest.mark.parametrize("locale_encoding", ["cp1252", "gbk", "ascii"])
def test_the_default_locale_encoding_was_the_failure(
    concat_entry, monkeypatch: pytest.MonkeyPatch, locale_encoding: str
) -> None:
    """Documents the mechanism: the same entry under the encodings a
    non-UTF-8 Windows host defaults to. ``gbk`` can encode the CJK name (and,
    less obviously, kana and Cyrillic) but not an emoji; ``cp1252`` and
    ``ascii`` cannot encode the CJK name at all -- and none of them is what
    ffmpeg reads."""
    import codecs

    monkeypatch.setattr("os.path.abspath", lambda p: p)
    sample = CJK_NAME if locale_encoding != "gbk" else "🎬 take 2.mp4"

    with pytest.raises(UnicodeEncodeError):
        codecs.encode(concat_entry(sample), locale_encoding)
    codecs.encode(concat_entry(sample), "utf-8")  # the fix's encoding always can


@pytest.mark.parametrize(
    "name",
    [CJK_NAME, "épisode_1.mp4", "Zürich's clip.mp4", "🎬 take 2.mp4", "Пример.mp4"],
)
def test_non_ascii_names_still_parse_back_exactly(
    concat_entry, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Escaping and encoding are independent: a non-ASCII name that also
    carries a quote must survive both."""
    monkeypatch.setattr("os.path.abspath", lambda p: p)

    assert parsed_path(concat_entry(name)) == name


def test_a_non_ascii_windows_path_with_a_quote(
    concat_entry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("os.path.abspath", lambda p: p)
    raw = r"C:\视频\O'Brien\1_场景一.mp4"

    assert parsed_path(concat_entry(raw)) == "C:/视频/O'Brien/1_场景一.mp4"
