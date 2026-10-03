"""Issue #3587: a bootstrap file with a BOM reached the system prompt as one.

``identity.workspace`` reads the files in ``BOOTSTRAP_FILENAMES`` -- AGENTS.md,
SOUL.md, IDENTITY.md and the rest -- and what it returns goes into the system
prompt. It read them as plain ``utf-8`` with ``errors="replace"``, while the
other two readers of user-authored Markdown in this codebase already honour a
byte-order mark: ``SKILL.md`` since #2697 and knowledge-base ingest since
#2670.

Windows writes a mark without being asked. PowerShell 5's ``Set-Content
-Encoding UTF8`` prefixes a UTF-8 BOM, and its ``>`` and ``Out-File`` write
UTF-16. The first put ``\\ufeff`` in front of the first character; the second
decoded -- ``errors="replace"`` means it does not fail -- into interleaved
NULs and replacement characters, which is what the agent was handed as its
operating rules, at twice the length against the bootstrap budget.
"""

from __future__ import annotations

import codecs
from pathlib import Path

import pytest

from agentos.identity.workspace import _read_file_sync, load_workspace_files_budgeted
from agentos.memory.ingest import _bom_encoding
from agentos.text_encoding import BOM_PROBE_BYTES, bom_encoding, read_text_honouring_bom

BODY = "# Operating rules\n\nAlways answer in Spanish.\n"

ENCODINGS = [
    pytest.param(BODY.encode("utf-8"), id="utf-8"),
    pytest.param(codecs.BOM_UTF8 + BODY.encode("utf-8"), id="utf-8-bom"),
    pytest.param(BODY.encode("utf-16"), id="utf-16"),
    pytest.param(BODY.encode("utf-16-le"), id="utf-16-le-no-bom-is-not-detectable"),
    pytest.param(BODY.encode("utf-32"), id="utf-32"),
]

DETECTABLE = [p for p in ENCODINGS if "not-detectable" not in (p.id or "")]


# ── the issue's reproduction ───────────────────────────────────────────────


@pytest.mark.parametrize("raw", DETECTABLE)
def test_a_bootstrap_file_reads_back_as_what_was_written(raw: bytes, tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_bytes(raw)

    assert _read_file_sync(path) == BODY


@pytest.mark.parametrize("raw", DETECTABLE)
def test_no_mark_or_mojibake_survives_into_the_prompt(raw: bytes, tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_bytes(raw)

    text = _read_file_sync(path) or ""

    assert "﻿" not in text, "the byte-order mark is not content"
    assert "�" not in text, "nothing failed to decode"
    assert "\x00" not in text
    assert text.splitlines()[0] == "# Operating rules"


@pytest.mark.parametrize("raw", DETECTABLE)
def test_the_file_costs_its_own_length_against_the_budget(raw: bytes, tmp_path: Path) -> None:
    """UTF-16 read as UTF-8 was 92 characters for a 45-character file, so it
    crowded out the bootstrap files after it."""
    path = tmp_path / "AGENTS.md"
    path.write_bytes(raw)

    assert len(_read_file_sync(path) or "") == len(BODY)


@pytest.mark.parametrize("raw", DETECTABLE)
def test_the_budgeted_loader_hands_on_the_clean_text(raw: bytes, tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_bytes(raw)

    loaded = load_workspace_files_budgeted(tmp_path, filenames=["AGENTS.md"])

    rendered = str(loaded)
    assert "Always answer in Spanish." in rendered
    assert "�" not in rendered
    assert "﻿" not in rendered


# ── the shared rule ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (b"", "utf-8-sig"),
        (b"# hi", "utf-8-sig"),
        (codecs.BOM_UTF8 + b"#", "utf-8-sig"),
        (codecs.BOM_UTF16_LE + b"#", "utf-16"),
        (codecs.BOM_UTF16_BE + b"#", "utf-16"),
        (codecs.BOM_UTF32_LE, "utf-32"),
        (codecs.BOM_UTF32_BE, "utf-32"),
    ],
)
def test_bom_encoding_names_the_codec(head: bytes, expected: str) -> None:
    assert bom_encoding(head) == expected


def test_a_utf32_le_file_is_not_mistaken_for_utf16() -> None:
    """UTF-32 LE opens with UTF-16 LE's mark, so a two-byte probe gets it
    wrong -- which is why the probe is four."""
    assert BOM_PROBE_BYTES == 4
    assert codecs.BOM_UTF32_LE.startswith(codecs.BOM_UTF16_LE)
    assert bom_encoding(codecs.BOM_UTF32_LE) == "utf-32"
    assert bom_encoding(codecs.BOM_UTF32_LE[:2]) == "utf-16", "the two-byte read is the trap"


def test_ingest_and_workspace_use_one_definition() -> None:
    """``memory.ingest`` keeps its private name and delegates, so the two
    cannot drift."""
    for head in (b"", codecs.BOM_UTF8, codecs.BOM_UTF16_LE, codecs.BOM_UTF32_BE):
        assert _bom_encoding(head) == bom_encoding(head)


def test_the_shared_module_imports_nothing_from_agentos() -> None:
    """The property that lets every layer reach it."""
    import ast

    source = Path("src/agentos/text_encoding.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }

    assert not any(name.startswith("agentos") for name in names), names


# ── what must not change ───────────────────────────────────────────────────


def test_a_plain_utf8_file_is_byte_for_byte_what_it_was(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_bytes(BODY.encode("utf-8"))

    assert _read_file_sync(path) == BODY


def test_a_missing_file_is_still_none(tmp_path: Path) -> None:
    assert _read_file_sync(tmp_path / "nope.md") is None


def test_an_oversized_file_is_still_refused(tmp_path: Path) -> None:
    from agentos.identity.workspace import _MAX_FILE_BYTES

    path = tmp_path / "AGENTS.md"
    path.write_bytes(b"x" * (_MAX_FILE_BYTES + 1))

    assert _read_file_sync(path) is None


def test_undecodable_bytes_still_degrade_rather_than_raise(tmp_path: Path) -> None:
    """``errors="replace"`` is kept: a bootstrap file must never crash a session."""
    path = tmp_path / "AGENTS.md"
    path.write_bytes(b"# ok \xff\xfe\x00 still readable")

    text = _read_file_sync(path)

    assert text is not None
    assert "still readable" in text


def test_read_text_honouring_bom_defaults_to_strict(tmp_path: Path) -> None:
    path = tmp_path / "x.md"
    path.write_bytes(b"\xff\xff\xff")

    with pytest.raises(UnicodeDecodeError):
        read_text_honouring_bom(path)
