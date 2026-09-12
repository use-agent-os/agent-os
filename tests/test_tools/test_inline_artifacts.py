"""Auto-publishing of inline artifacts announced on a command's stdout.

A skill script that writes a chart or card payload ends by printing a marker
naming the file. Publishing used to be the model's call, and in practice it
routinely skipped it -- the file was written and the UI never drew. These tests
pin the contract that makes the render deterministic, and the guards that keep
the marker from becoming a way to publish arbitrary files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentos.tools.builtin import artifacts as artifacts_mod
from agentos.tools.builtin.artifacts import (
    INLINE_ARTIFACT_MIME_PREFIX,
    publish_inline_artifacts,
)
from agentos.tools.types import ToolContext, ToolError, current_tool_context

CARDS_MIME = "application/vnd.agentos.cards+json"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "apple.cards.json").write_text('{"type":"cards","cards":[]}', encoding="utf-8")
    return tmp_path


@pytest.fixture
def ctx(workspace: Path) -> Any:
    context = ToolContext(workspace_dir=str(workspace))
    token = current_tool_context.set(context)
    yield context
    current_tool_context.reset(token)


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """Record publish_artifact calls instead of touching the artifact store."""
    calls: list[dict[str, str]] = []

    async def _fake(path: str, name: str | None = None, mime: str | None = None) -> str:
        calls.append({"path": path, "mime": mime or ""})
        return "{}"

    monkeypatch.setattr(artifacts_mod, "publish_artifact", _fake)
    return calls


def marker(path: str = "apple.cards.json", mime: str = CARDS_MIME) -> str:
    return f"publish_artifact path={path} mime={mime}"


# ── The happy path ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_marker_publishes_without_the_model_asking(
    ctx: Any, published: list[dict[str, str]]
) -> None:
    out = await publish_inline_artifacts(marker())
    assert published == [{"path": "apple.cards.json", "mime": CARDS_MIME}]
    assert "publish_artifact path=" not in out


@pytest.mark.asyncio
async def test_marker_is_replaced_by_a_do_not_republish_note(
    ctx: Any, published: list[dict[str, str]]
) -> None:
    out = await publish_inline_artifacts(marker())
    assert "already rendered for the user" in out
    assert "Do not call publish_artifact" in out


@pytest.mark.asyncio
async def test_surrounding_output_is_preserved(ctx: Any, published: list[dict[str, str]]) -> None:
    out = await publish_inline_artifacts(f"looked up 683 tokens\n{marker()}\ndone")
    assert out.startswith("looked up 683 tokens\n")
    assert out.endswith("\ndone")


@pytest.mark.asyncio
async def test_several_markers_each_publish(ctx: Any, published: list[dict[str, str]]) -> None:
    out = await publish_inline_artifacts(f"{marker('a.json')}\n{marker('b.json')}")
    assert [c["path"] for c in published] == ["a.json", "b.json"]
    assert "publish_artifact path=" not in out


@pytest.mark.asyncio
async def test_crlf_terminated_marker_publishes_and_keeps_its_line_ending(
    ctx: Any, published: list[dict[str, str]]
) -> None:
    """Python's text stdout ends lines with ``\\r\\n`` on Windows (issue #1732)."""
    out = await publish_inline_artifacts(f"looked up 683 tokens\r\n{marker()}\r\ndone\r\n")
    assert published == [{"path": "apple.cards.json", "mime": CARDS_MIME}]
    assert "publish_artifact path=" not in out
    assert out.startswith("looked up 683 tokens\r\n")
    assert out.endswith("Do not call publish_artifact for it.]\r\ndone\r\n")


@pytest.mark.asyncio
async def test_crlf_marker_with_trailing_whitespace_publishes(
    ctx: Any, published: list[dict[str, str]]
) -> None:
    out = await publish_inline_artifacts(f"{marker()} \t\r\n")
    assert [c["path"] for c in published] == ["apple.cards.json"]
    assert out.endswith("\r\n")


@pytest.mark.asyncio
async def test_several_crlf_markers_each_publish(ctx: Any, published: list[dict[str, str]]) -> None:
    out = await publish_inline_artifacts(f"{marker('a.json')}\r\n{marker('b.json')}\r\n")
    assert [c["path"] for c in published] == ["a.json", "b.json"]
    assert "publish_artifact path=" not in out
    assert out.count("\r\n") == 2


# ── Guards ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_only_agentos_inline_mimes_auto_publish(
    ctx: Any, published: list[dict[str, str]]
) -> None:
    """A plain file still needs a deliberate call, so stray output cannot leak one."""
    out = await publish_inline_artifacts(marker("secrets.zip", "application/zip"))
    assert published == []
    # Left intact: the model may still publish it on purpose.
    assert marker("secrets.zip", "application/zip") in out
    assert INLINE_ARTIFACT_MIME_PREFIX == "application/vnd.agentos."


@pytest.mark.asyncio
async def test_marker_must_own_its_line(ctx: Any, published: list[dict[str, str]]) -> None:
    """Prose that merely mentions the marker must not publish anything."""
    for text in (
        f"run `{marker()}` to publish it",
        f"the script prints {marker()} at the end",
    ):
        assert await publish_inline_artifacts(text) == text
    assert published == []


@pytest.mark.asyncio
async def test_a_capped_number_publishes_per_command(
    ctx: Any, published: list[dict[str, str]]
) -> None:
    out = await publish_inline_artifacts("\n".join(marker(f"c{i}.json") for i in range(9)))
    assert len(published) == artifacts_mod._MAX_INLINE_ARTIFACTS_PER_CALL
    # The overflow is reported, not silently dropped.
    assert "too many in one command" in out


@pytest.mark.asyncio
async def test_publish_failure_is_reported_not_raised(
    ctx: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shell command must not fail because a publish did not work out."""

    async def _boom(path: str, name: str | None = None, mime: str | None = None) -> str:
        raise ToolError("artifact path is outside workspace: ../../etc/passwd")

    monkeypatch.setattr(artifacts_mod, "publish_artifact", _boom)
    out = await publish_inline_artifacts(f"ok\n{marker('../../etc/passwd')}")
    assert out.startswith("ok\n")
    assert "not published" in out
    assert "outside workspace" in out


# ── Cheap exits ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_output_without_the_marker_is_returned_unchanged(
    ctx: Any, published: list[dict[str, str]]
) -> None:
    for text in ("", "exit_code=0", "no markers here at all"):
        assert await publish_inline_artifacts(text) == text
    assert published == []


@pytest.mark.asyncio
async def test_no_tool_context_is_a_no_op(published: list[dict[str, str]]) -> None:
    """CLI/test callers have no workspace; the marker just stays as text."""
    assert await publish_inline_artifacts(marker()) == marker()
    assert published == []
