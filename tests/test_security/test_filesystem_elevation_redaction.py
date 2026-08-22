from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from agentos.tools.builtin import filesystem as fs
from agentos.tools.types import CallerKind, ToolContext, current_tool_context


@contextmanager
def tool_context(
    workspace: Path,
    *,
    strict: bool = False,
    elevated_mode: str | None = None,
) -> Iterator[None]:
    token = current_tool_context.set(
        ToolContext(
            caller_kind=CallerKind.CLI,
            channel_kind="cli",
            channel_id="cli:test",
            workspace_dir=str(workspace),
            workspace_strict=strict,
            elevated=elevated_mode,
        )
    )
    try:
        yield
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_read_file_redacts_credentials_even_in_elevated_full_mode(tmp_path: Path) -> None:
    fake_token = "sk-proj-" + "A" * 32
    target = tmp_path / "credentials"
    target.write_text(f"[default]\napi_key = {fake_token}\n", encoding="utf-8")

    with tool_context(tmp_path, strict=False, elevated_mode="full"):
        content = await fs.read_file(str(target))

    assert fake_token not in content
    assert "«redacted:sk-p…»" in content or "«redacted»" in content
    assert "api_key =" in content


@pytest.mark.asyncio
async def test_grep_search_redacts_credentials(tmp_path: Path) -> None:
    fake_token = "sk-ant-" + "B" * 32
    target = tmp_path / "config.env"
    target.write_text(f"ANTHROPIC_KEY={fake_token}\nDEBUG=true\n", encoding="utf-8")

    with tool_context(tmp_path, strict=False, elevated_mode="full"):
        result = await fs.grep_search("ANTHROPIC_KEY", path=str(target))

    assert fake_token not in result
    assert "«redacted:sk-a…»" in result or "«redacted»" in result


@pytest.mark.asyncio
async def test_read_spreadsheet_redacts_credentials(tmp_path: Path) -> None:
    fake_token = "sk-or-v1-" + "C" * 40
    target = tmp_path / "keys.csv"
    target.write_text(f"service,key\nopenrouter,{fake_token}\n", encoding="utf-8")

    with tool_context(tmp_path, strict=False, elevated_mode="full"):
        result = await fs.read_spreadsheet(str(target))

    assert fake_token not in result
    assert "«redacted:sk-o…»" in result or "«redacted»" in result
    assert "openrouter" in result
