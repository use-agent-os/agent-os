"""Issue #2310: ``grep_search`` searched binary files and pasted them back.

``read_file`` has always refused binary and Office documents through
``_looks_binary``. ``grep_search`` had no such guard: every file the walk
reached was read with ``errors="replace"``, so a pattern matching bytes inside
a ``.docx`` or an ``.exe`` emitted a line of replacement characters and raw
control bytes straight into the model's context.

Measured on main, one recursive search of a ten-file directory returned seven
binary matches carrying **230 NUL bytes** and 22 replacement characters.

Two things beyond the plain extension check are pinned here.

*The sample window.* ``_looks_binary`` inspects only the first 8 KiB, so a
file whose binary payload follows a long ASCII header -- a text-heavy PDF is
the everyday case -- passes it. The content actually being searched is
therefore checked as well.

*Silence is the wrong answer.* ``grep_search``'s own gate comment says a bare
"No matches" is not self-correcting, because the model reads it as "the symbol
does not exist" and stops looking. A file that was skipped rather than
searched has to say so.
"""

from __future__ import annotations

import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from agentos.tools.builtin import filesystem as fs
from agentos.tools.types import CallerKind, ToolContext, current_tool_context

REPLACEMENT = "�"


@contextmanager
def _tool_context(workspace: Path) -> Iterator[None]:
    token = current_tool_context.set(
        ToolContext(
            caller_kind=CallerKind.CLI,
            channel_kind="cli",
            channel_id="cli:test",
            workspace_dir=str(workspace),
            workspace_strict=True,
        )
    )
    try:
        yield
    finally:
        current_tool_context.reset(token)


def _office_zip(path: Path, member: str) -> None:
    """A real Office file is a zip whose central directory holds member names
    as plain text, which is how a search term matches inside one."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(member, "<w:t>needle</w:t>")


def _text_heavy_pdf(path: Path) -> None:
    """A PDF whose first 8 KiB are pure ASCII, with the stream after it."""
    body = bytearray(b"%PDF-1.7\n% a long ASCII header\n")
    for index in range(400):
        body += f"{index} 0 obj\n<< /Type /Page /Label (needle {index}) >>\nendobj\n".encode()
    body += b"stream\n\x00\x01\x02\xff\xfe needle \x00\x00\nendstream\n%%EOF\n"
    path.write_bytes(bytes(body))


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "notes.txt").write_text("the needle appears here\n", encoding="utf-8")
    (tmp_path / "code.py").write_text("# needle in source\nx = 1\n", encoding="utf-8")
    (tmp_path / "data.csv").write_text("id,name\n1,needle\n", encoding="utf-8")

    _office_zip(tmp_path / "report.docx", "word/needle_document.xml")
    _office_zip(tmp_path / "sheet.xlsx", "xl/needle_sheet.xml")
    _office_zip(tmp_path / "deck.pptx", "ppt/needle_slide.xml")
    _office_zip(tmp_path / "bundle.zip", "needle_inside.txt")

    (tmp_path / "app.bin").write_bytes(b"\x7fELF\x00\x00\x00needle\x00\xff\xfe\x00binary")
    (tmp_path / "tool.exe").write_bytes(b"MZ\x90\x00\x03needle\x00\x00\xff")
    (tmp_path / "blob").write_bytes(b"\x00\x01\x02needle\x00\x03")
    return tmp_path


# --------------------------------------------------------------------------
# The reported defect.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_recursive_search_returns_only_the_text_files(tree: Path) -> None:
    with _tool_context(tree):
        out = await fs.grep_search("needle", path=str(tree))

    assert "notes.txt" in out
    assert "code.py" in out
    assert "data.csv" in out
    for binary in ("report.docx", "sheet.xlsx", "deck.pptx", "bundle.zip", "app.bin", "tool.exe"):
        assert binary not in out, f"{binary} leaked into the results"


@pytest.mark.asyncio
async def test_no_control_bytes_reach_the_model(tree: Path) -> None:
    """The cost the issue is about: what lands in the context window."""
    with _tool_context(tree):
        out = await fs.grep_search("needle", path=str(tree))

    assert "\x00" not in out
    assert REPLACEMENT not in out
    assert all(character >= " " or character == "\n" for character in out)


@pytest.mark.parametrize(
    "name",
    ["report.docx", "sheet.xlsx", "deck.pptx", "bundle.zip", "app.bin", "tool.exe", "blob"],
)
@pytest.mark.asyncio
async def test_each_binary_file_is_skipped_on_its_own(tree: Path, name: str) -> None:
    with _tool_context(tree):
        out = await fs.grep_search("needle", path=str(tree / name))

    assert "Skipped binary file" in out
    assert name in out


@pytest.mark.parametrize("name", ["notes.txt", "code.py", "data.csv"])
@pytest.mark.asyncio
async def test_each_text_file_is_still_searched(tree: Path, name: str) -> None:
    with _tool_context(tree):
        out = await fs.grep_search("needle", path=str(tree / name))

    assert name in out
    assert "Skipped" not in out


# --------------------------------------------------------------------------
# The 8 KiB sample window, which an extension check alone does not close.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_binary_payload_after_a_long_ascii_header_is_skipped(tmp_path: Path) -> None:
    """``_looks_binary`` only sees the first 8 KiB. Checking the extension and
    that sample leaves this file looking like text."""
    _text_heavy_pdf(tmp_path / "manual.pdf")
    sample = fs._read_binary_sample(tmp_path / "manual.pdf")
    assert fs._looks_binary(sample, tmp_path / "manual.pdf") is None, (
        "fixture must be one the sample check passes, or it proves nothing"
    )

    with _tool_context(tmp_path):
        out = await fs.grep_search("needle", path=str(tmp_path))

    assert "\x00" not in out
    assert "manual.pdf" not in out


@pytest.mark.asyncio
async def test_the_same_payload_without_a_telling_extension_is_skipped(tmp_path: Path) -> None:
    _text_heavy_pdf(tmp_path / "archive.dat")

    with _tool_context(tmp_path):
        out = await fs.grep_search("needle", path=str(tmp_path))

    assert "\x00" not in out
    assert "archive.dat" not in out


@pytest.mark.asyncio
async def test_a_nul_byte_just_past_the_sample_window_is_caught(tmp_path: Path) -> None:
    payload = b"needle\n" + b"a" * 9000 + b"\x00needle\n"
    (tmp_path / "late.log").write_bytes(payload)

    with _tool_context(tmp_path):
        out = await fs.grep_search("needle", path=str(tmp_path))

    assert "late.log" not in out
    assert "\x00" not in out


# --------------------------------------------------------------------------
# A skipped file must not read as "the symbol does not exist".
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_single_binary_file_says_it_was_skipped(tmp_path: Path) -> None:
    (tmp_path / "sample.bin").write_bytes(b"\x00\x01needle\x00")

    with _tool_context(tmp_path):
        out = await fs.grep_search("needle", path=str(tmp_path / "sample.bin"))

    assert "No matches" not in out
    assert "Skipped binary file" in out
    assert "sample.bin" in out


@pytest.mark.asyncio
async def test_the_skip_message_names_the_reason(tmp_path: Path) -> None:
    (tmp_path / "book.docx").write_bytes(b"PK\x03\x04needle")

    with _tool_context(tmp_path):
        out = await fs.grep_search("needle", path=str(tmp_path / "book.docx"))

    assert ".docx Office document" in out


@pytest.mark.asyncio
async def test_a_recursive_search_reports_how_many_files_it_skipped(tmp_path: Path) -> None:
    (tmp_path / "a.bin").write_bytes(b"\x00needle")
    (tmp_path / "b.exe").write_bytes(b"MZ\x00needle")
    (tmp_path / "unrelated.txt").write_text("nothing here\n", encoding="utf-8")

    with _tool_context(tmp_path):
        out = await fs.grep_search("needle", path=str(tmp_path))

    assert "No matches for 'needle'" in out
    assert "skipped 2 binary files" in out


@pytest.mark.asyncio
async def test_one_skipped_file_is_reported_in_the_singular(tmp_path: Path) -> None:
    (tmp_path / "a.bin").write_bytes(b"\x00needle")

    with _tool_context(tmp_path):
        out = await fs.grep_search("needle", path=str(tmp_path))

    assert "skipped 1 binary file)" in out


@pytest.mark.asyncio
async def test_nothing_is_added_when_nothing_was_skipped(tmp_path: Path) -> None:
    """The existing contract for an ordinary miss must not shift."""
    (tmp_path / "a.txt").write_text("nothing here\n", encoding="utf-8")

    with _tool_context(tmp_path):
        out = await fs.grep_search("needle", path=str(tmp_path))

    assert out == "No matches for 'needle'"


@pytest.mark.asyncio
async def test_a_successful_search_gains_no_skip_noise(tree: Path) -> None:
    """Six binary files are skipped here, but there are real matches, so the
    result stays exactly the match list."""
    with _tool_context(tree):
        out = await fs.grep_search("needle", path=str(tree))

    assert "skipped" not in out.lower()
    assert len(out.splitlines()) == 3


# --------------------------------------------------------------------------
# The skip decision itself.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("a.docx", b"PK\x03\x04"),
        ("a.xlsx", b"PK\x03\x04"),
        ("a.pptx", b"PK\x03\x04"),
        ("a.doc", b"\xd0\xcf\x11\xe0"),
        ("a.xls", b"\xd0\xcf\x11\xe0"),
        ("a.ppt", b"\xd0\xcf\x11\xe0"),
        ("a.zip", b"PK\x03\x04"),
        ("a.tar", b"junk"),
        ("a.gz", b"\x1f\x8b"),
        ("a.bz2", b"BZh"),
        ("a.7z", b"7z\xbc\xaf"),
        ("a.rar", b"Rar!"),
        ("a.exe", b"MZ"),
        ("a.dmg", b"koly"),
        ("a.bin", b"junk"),
    ],
)
def test_every_guarded_extension_is_skipped(tmp_path: Path, name: str, payload: bytes) -> None:
    target = tmp_path / name
    target.write_bytes(payload)

    assert fs._grep_skip_reason(target) is not None


@pytest.mark.parametrize(
    "name", ["a.txt", "a.py", "a.md", "a.csv", "a.tsv", "a.json", "a.yaml", "a.toml", "README"]
)
def test_ordinary_text_extensions_are_not_skipped(tmp_path: Path, name: str) -> None:
    target = tmp_path / name
    target.write_text("plain text\n", encoding="utf-8")

    assert fs._grep_skip_reason(target) is None


def test_the_extension_check_needs_no_file_content(tmp_path: Path) -> None:
    """A guarded extension is decided before the file is opened, so a large
    archive costs no read at all."""
    target = tmp_path / "huge.zip"
    target.write_bytes(b"")

    assert fs._grep_skip_reason(target) == ".zip binary/container file"


def test_an_extensionless_file_is_judged_by_its_bytes(tmp_path: Path) -> None:
    text = tmp_path / "LICENSE"
    text.write_text("MIT\n", encoding="utf-8")
    binary = tmp_path / "core"
    binary.write_bytes(b"\x7fELF\x00\x00")

    assert fs._grep_skip_reason(text) is None
    assert fs._grep_skip_reason(binary) == "contains NUL bytes"


# --------------------------------------------------------------------------
# Interaction with the rest of grep_search.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_include_glob_still_filters_text_files(tree: Path) -> None:
    with _tool_context(tree):
        out = await fs.grep_search("needle", path=str(tree), include="*.py")

    assert "code.py" in out
    assert "notes.txt" not in out


@pytest.mark.asyncio
async def test_an_include_glob_naming_only_binaries_says_they_were_skipped(tree: Path) -> None:
    """Asking for ``*.docx`` and being told "No matches" would suggest the term
    is absent, when the file was never opened."""
    with _tool_context(tree):
        out = await fs.grep_search("needle", path=str(tree), include="*.docx")

    assert "skipped 1 binary file" in out


@pytest.mark.asyncio
async def test_max_results_still_caps_the_output(tmp_path: Path) -> None:
    for index in range(10):
        (tmp_path / f"f{index}.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "x.bin").write_bytes(b"\x00needle")

    with _tool_context(tmp_path):
        out = await fs.grep_search("needle", path=str(tmp_path), max_results=3)

    assert len(out.splitlines()) == 3


@pytest.mark.asyncio
async def test_an_invalid_regex_still_raises(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")

    with _tool_context(tmp_path), pytest.raises(ValueError, match="Invalid regex"):
        await fs.grep_search("(unclosed", path=str(tmp_path))


@pytest.mark.asyncio
async def test_a_missing_path_still_raises(tmp_path: Path) -> None:
    with _tool_context(tmp_path), pytest.raises(FileNotFoundError):
        await fs.grep_search("needle", path=str(tmp_path / "nope"))


@pytest.mark.asyncio
async def test_an_empty_file_is_searched_not_skipped(tmp_path: Path) -> None:
    (tmp_path / "empty.txt").write_bytes(b"")

    with _tool_context(tmp_path):
        out = await fs.grep_search("needle", path=str(tmp_path))

    assert out == "No matches for 'needle'"


@pytest.mark.asyncio
async def test_a_utf8_file_with_non_ascii_text_is_still_searched(tmp_path: Path) -> None:
    """Skipping must key on control bytes, not on being non-ASCII."""
    (tmp_path / "notes.md").write_text("找到 needle 了\n", encoding="utf-8")

    with _tool_context(tmp_path):
        out = await fs.grep_search("needle", path=str(tmp_path))

    assert "notes.md" in out
    assert "找到" in out


@pytest.mark.asyncio
async def test_a_utf16_file_is_skipped(tmp_path: Path) -> None:
    """UTF-16 is full of NUL bytes and decodes to mojibake under UTF-8, so it
    is the same pollution the issue reports."""
    (tmp_path / "wide.txt").write_bytes("needle\n".encode("utf-16"))

    with _tool_context(tmp_path):
        out = await fs.grep_search("needle", path=str(tmp_path))

    assert "wide.txt" not in out
    assert "\x00" not in out
