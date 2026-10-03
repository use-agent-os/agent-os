"""Reading text files the way the tools that wrote them encoded them.

Windows writes a byte-order mark by default and nobody asks it to. PowerShell
5's ``Set-Content -Encoding UTF8`` prefixes a UTF-8 BOM; its ``>`` and
``Out-File`` write UTF-16; Notepad's "Unicode" and Excel's "Unicode Text" do
the same. Read as plain ``utf-8``, the first puts ``\\ufeff`` in front of the
first character and the second decodes to interleaved NULs and replacement
characters -- and with ``errors="replace"`` it decodes rather than failing, so
nothing reports a problem.

``utf-8-sig`` reads a file with no BOM exactly as ``utf-8`` does, and every
codec named here drops the mark itself, so honouring a BOM costs nothing for
the files that do not have one.

At the package root because three subsystems need it and no two of them are
in the same layer: knowledge-base ingest (#2670), the ``SKILL.md`` loader
(#2697) and workspace bootstrap files (#3587). This module imports nothing
from ``agentos``.
"""

from __future__ import annotations

import codecs
from pathlib import Path

__all__ = ["BOM_PROBE_BYTES", "bom_encoding", "read_text_honouring_bom"]

#: Enough to recognise every mark below. UTF-32's is four bytes, and its
#: little-endian form opens with UTF-16's, so a two-byte probe would read a
#: UTF-32 file as UTF-16.
BOM_PROBE_BYTES = 4


def bom_encoding(head: bytes) -> str:
    """The codec for text starting with *head*: whatever its byte-order mark names.

    ``utf-8-sig`` when there is no mark, which is ``utf-8`` for such a file.
    """
    if head.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return "utf-32"
    if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    return "utf-8-sig"


def read_text_honouring_bom(path: Path, *, errors: str = "strict") -> str:
    """Read *path* as text, decoding it as its byte-order mark says to.

    The mark is consumed, so the first character of the result is the first
    character of the document.
    """
    with path.open("rb") as handle:
        head = handle.read(BOM_PROBE_BYTES)
    return path.read_text(encoding=bom_encoding(head), errors=errors)
