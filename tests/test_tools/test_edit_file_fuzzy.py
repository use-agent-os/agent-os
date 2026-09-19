from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from agentos.tools.builtin import filesystem as fs
from agentos.tools.types import CallerKind, SafeToolError, ToolContext, current_tool_context


def _original_async(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
    """Unwrap the @tool and @sandboxed decorators to reach the implementation."""

    return fn.__wrapped__.__wrapped__  # type: ignore[attr-defined, no-any-return]


edit_file = _original_async(fs.edit_file)


@contextmanager
def tool_context(workspace: Path) -> Iterator[None]:
    token = current_tool_context.set(
        ToolContext(
            caller_kind=CallerKind.CLI,
            channel_kind="cli",
            channel_id="cli:test",
            workspace_dir=str(workspace),
        )
    )
    try:
        yield
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_exact_edit_keeps_the_original_result_wording(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("value = 1\n", encoding="utf-8")

    with tool_context(tmp_path):
        result = await edit_file(str(target), "value = 1", "value = 2")

    # No marker on the exact path — the common case reads exactly as before.
    assert result == f"Edited {target}: replaced 9 chars with 9 chars"
    assert target.read_text(encoding="utf-8") == "value = 2\n"


@pytest.mark.asyncio
async def test_indent_drift_edits_successfully_and_names_the_strategy(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_text(
        "class A:\n    def run(self):\n        value = 1\n        return value\n",
        encoding="utf-8",
    )

    with tool_context(tmp_path):
        result = await edit_file(
            str(target),
            "    value = 1\n    return value\n",
            "    value = 2\n    return value * 2\n",
        )

    assert "[match=indent_agnostic]" in result
    assert target.read_text(encoding="utf-8") == (
        "class A:\n    def run(self):\n        value = 2\n        return value * 2\n"
    )


@pytest.mark.asyncio
async def test_smart_quote_drift_edits_successfully(tmp_path: Path) -> None:
    target = tmp_path / "notes.py"
    target.write_text("title = \u201chello\u201d\n", encoding="utf-8")

    with tool_context(tmp_path):
        result = await edit_file(str(target), 'title = "hello"', 'title = "bye"')

    assert "[match=unicode_normalized]" in result
    assert target.read_text(encoding="utf-8") == 'title = "bye"\n'


@pytest.mark.asyncio
async def test_ambiguous_edit_reports_the_matching_lines(tmp_path: Path) -> None:
    target = tmp_path / "dup.py"
    target.write_text("x = 1\ny = 0\nx = 1\n", encoding="utf-8")

    with tool_context(tmp_path):
        with pytest.raises(SafeToolError) as excinfo:
            await edit_file(str(target), "x = 1", "x = 2")

    message = str(excinfo.value)
    assert "matches 2 locations" in message
    assert "lines 1, 3" in message
    # A rejected edit must leave the file untouched.
    assert target.read_text(encoding="utf-8") == "x = 1\ny = 0\nx = 1\n"


@pytest.mark.asyncio
async def test_missing_text_error_carries_a_closest_match_hint(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("def calculate_total(items):\n    return 0\n", encoding="utf-8")

    with tool_context(tmp_path):
        with pytest.raises(SafeToolError) as excinfo:
            await edit_file(
                str(target),
                "def calculate_grand_totals(a, b, c, d):\n",
                "x",
            )

    message = str(excinfo.value)
    assert "old_text not found" in message
    assert "Closest match: line 1" in message


@pytest.mark.asyncio
async def test_missing_file_still_reports_file_not_found(tmp_path: Path) -> None:
    with tool_context(tmp_path):
        with pytest.raises(FileNotFoundError):
            await edit_file(str(tmp_path / "absent.py"), "a", "b")
