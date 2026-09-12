"""``*** Add File:`` blocks must keep their blank lines (#1691).

The strict format prefixes every content line with ``+``, but editors, log
pipelines and most model output strip a lone ``+`` down to ``""``. Skipping
such a line silently rewrote the file the model asked for; treating it as an
empty content line is the only outcome that does not lose data.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from agentos.tools.builtin import patch as patch_tool
from agentos.tools.builtin.patch import AddFile, _parse_patch
from agentos.tools.types import ToolContext, current_tool_context


def _original_async(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
    return fn.__wrapped__.__wrapped__  # type: ignore[attr-defined, no-any-return]


def _add_file_content(patch_text: str) -> str:
    ops = _parse_patch(patch_text)
    assert len(ops) == 1
    assert isinstance(ops[0], AddFile)
    return ops[0].content


def test_bare_blank_line_between_functions_is_kept() -> None:
    patch_text = """*** Begin Patch
*** Add File: sample.py
+def foo():
+    pass

+def bar():
+    pass
*** End Patch"""

    assert _add_file_content(patch_text) == "def foo():\n    pass\n\ndef bar():\n    pass"


def test_bare_and_prefixed_blank_lines_are_equivalent() -> None:
    bare = """*** Begin Patch
*** Add File: a.txt
+one

+two


+three
*** End Patch"""
    prefixed = bare.replace("\n\n+two", "\n+\n+two").replace("\n\n\n+three", "\n+\n+\n+three")
    assert "\n\n" not in prefixed.split("*** Add File")[1]

    assert _add_file_content(bare) == "one\n\ntwo\n\n\nthree"
    assert _add_file_content(bare) == _add_file_content(prefixed)


def test_whitespace_only_line_is_a_blank_line() -> None:
    patch_text = "*** Begin Patch\n*** Add File: a.txt\n+one\n   \n+two\n*** End Patch"

    assert _add_file_content(patch_text) == "one\n\ntwo"


def test_trailing_bare_blanks_before_the_next_marker_are_separators() -> None:
    """A blank that only pads the block off the next marker is formatting."""
    patch_text = """*** Begin Patch
*** Add File: a.txt
+alpha

*** Add File: b.txt
+beta


*** End Patch"""

    ops = _parse_patch(patch_text)
    assert [op.content for op in ops if isinstance(op, AddFile)] == ["alpha", "beta"]


def test_explicit_trailing_plus_line_is_content() -> None:
    """``+`` on its own is an explicit empty line and survives even at the end."""
    patch_text = "*** Begin Patch\n*** Add File: a.txt\n+alpha\n+\n\n*** End Patch"

    assert _add_file_content(patch_text) == "alpha\n"


def test_unprefixed_text_line_is_rejected_not_dropped() -> None:
    patch_text = """*** Begin Patch
*** Add File: sample.py
+def foo():
    pass
*** End Patch"""

    with pytest.raises(ValueError, match=r"Add File: sample\.py.*'\+' prefix.*'    pass'"):
        _parse_patch(patch_text)


@pytest.mark.asyncio
async def test_apply_patch_writes_the_blank_line(tmp_path: Path) -> None:
    token = current_tool_context.set(ToolContext(workspace_dir=str(tmp_path)))
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        result = await apply_patch(
            """*** Begin Patch
*** Add File: sample.py
+def foo():
+    pass

+def bar():
+    pass
*** End Patch"""
        )
    finally:
        current_tool_context.reset(token)

    assert result == "Applied patch: 1 file(s) added"
    assert (tmp_path / "sample.py").read_text(encoding="utf-8") == (
        "def foo():\n    pass\n\ndef bar():\n    pass"
    )
