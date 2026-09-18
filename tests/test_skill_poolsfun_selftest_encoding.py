"""Tests for poolsdotfun selftest source file encoding (#2335).

selftest.py reads pools_read.py, poolsfun/plan.py, and pools_write.py to
perform Tier 7 capability-separation assertions. When Path.read_text() is
invoked without an explicit encoding, it defaults to the system code page
(e.g., cp936 / cp932 on CJK Windows), raising UnicodeDecodeError on the
non-ASCII characters present in docstrings and comments.
"""

from __future__ import annotations

import ast
import io
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "agentos"
    / "skills"
    / "bundled"
    / "poolsdotfun-token-launcher"
    / "scripts"
)


def _load_selftest_module():
    """Import selftest module with scripts directory available."""
    entry = str(_SCRIPTS_DIR)
    added = entry not in sys.path
    if added:
        sys.path.insert(0, entry)
    try:
        import selftest

        return selftest
    finally:
        if added:
            sys.path.remove(entry)


@pytest.mark.parametrize(
    "rel_path",
    [
        "pools_read.py",
        "poolsfun/plan.py",
        "pools_write.py",
    ],
)
def test_poolsfun_source_files_fail_on_cp936_without_utf8(rel_path: str) -> None:
    """The source files contain non-ASCII bytes that fail to decode under cp936."""
    target = _SCRIPTS_DIR / rel_path
    raw = target.read_bytes()

    # Raw bytes contain non-ASCII characters
    assert any(b >= 0x80 for b in raw)

    # Must fail to decode under GBK / cp936
    with pytest.raises(UnicodeDecodeError):
        raw.decode("cp936")

    # Decodes cleanly with UTF-8
    assert raw.decode("utf-8")


def test_tier7_import_graph_survives_cjk_code_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """tier7_import_graph must not raise UnicodeDecodeError when default encoding is cp936."""
    orig_read_text = Path.read_text

    def simulated_cjk_read_text(
        self: Path, encoding: str | None = None, errors: str | None = None
    ) -> str:
        # If caller does not specify encoding, simulate cp936 Windows locale
        actual_encoding = "cp936" if encoding is None else encoding
        return orig_read_text(self, encoding=actual_encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", simulated_cjk_read_text)
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    selftest = _load_selftest_module()
    selftest.tier7_import_graph()


def test_selftest_source_reads_explicitly_specify_utf8() -> None:
    """Source read sites in selftest.py must explicitly pass encoding='utf-8'."""
    selftest_path = _SCRIPTS_DIR / "selftest.py"
    tree = ast.parse(selftest_path.read_text(encoding="utf-8"))

    # Find read_text call sites
    read_text_calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "read_text":
                read_text_calls.append(node)

    assert len(read_text_calls) >= 3, "Expected at least 3 read_text calls in selftest.py"

    for call in read_text_calls:
        # Must have an encoding argument matching 'utf-8'
        encoding_val: str | None = None
        for kw in call.keywords:
            if kw.arg == "encoding" and isinstance(kw.value, ast.Constant):
                encoding_val = kw.value.value
        assert encoding_val == "utf-8", (
            f"read_text() call at line {call.lineno} does not pass encoding='utf-8'"
        )


def test_tier7_import_graph_positive_control(monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive control: tier7_import_graph runs cleanly and passes assertions."""
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    selftest = _load_selftest_module()
    selftest.tier7_import_graph()


def test_tier9_chain_constants_survives_cjk_code_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """tier9_chain_constants must not raise UnicodeDecodeError reading pools_write.py."""
    orig_read_text = Path.read_text

    def simulated_cjk_read_text(
        self: Path, encoding: str | None = None, errors: str | None = None
    ) -> str:
        actual_encoding = "cp936" if encoding is None else encoding
        return orig_read_text(self, encoding=actual_encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", simulated_cjk_read_text)
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    selftest = _load_selftest_module()
    selftest.tier9_chain_constants()
