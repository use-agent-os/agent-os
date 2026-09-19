"""Auto-publishing of inline artifacts announced on a command's stdout.

A skill script that writes a chart or card payload ends by printing a marker
naming the file. Publishing used to be the model's call, and in practice it
routinely skipped it -- the file was written and the UI never drew. These tests
pin the contract that makes the render deterministic, and the guards that keep
the marker from becoming a way to publish arbitrary files.
"""

from __future__ import annotations

import os
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
async def test_marker_with_crlf_line_endings_publishes_and_preserves_crlf(
    ctx: Any, published: list[dict[str, str]]
) -> None:
    """Windows processes and Python scripts on Windows emit CRLF line endings."""
    out = await publish_inline_artifacts(f"looked up 683 tokens\r\n{marker()}\r\ndone\r\n")
    assert published == [{"path": "apple.cards.json", "mime": CARDS_MIME}]
    assert out.startswith("looked up 683 tokens\r\n")
    assert out.endswith("\r\ndone\r\n")
    assert "publish_artifact path=" not in out
    assert "already rendered for the user" in out


@pytest.mark.asyncio
async def test_markers_with_crlf_and_trailing_whitespace(
    ctx: Any, published: list[dict[str, str]]
) -> None:
    out = await publish_inline_artifacts(f"{marker('a.json')}  \r\n{marker('b.json')}\t\r\n")
    assert [c["path"] for c in published] == ["a.json", "b.json"]
    assert "publish_artifact path=" not in out


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


# ── I/O failures on the announced file (#2892) ──────────────────────────────
#
# publish_artifact hashes and copies the file itself, so a file the finished
# process still holds open (routine on Windows) or one the agent cannot read
# raises PermissionError / OSError from inside it, not ToolError. Only ToolError
# was caught, so exec_command lost its whole stdout to a publish that was
# supposed to be best-effort. These go through the real publish_artifact.


@pytest.fixture
def publishing_ctx(workspace: Path, tmp_path: Path) -> Any:
    """A context publish_artifact accepts, so the failure comes from real I/O."""
    context = ToolContext(
        workspace_dir=str(workspace),
        artifact_media_root=str(tmp_path / "media"),
        artifact_session_id="session-1",
        session_key="agent:main:webchat:aaaa0001",
    )
    token = current_tool_context.set(context)
    yield context
    current_tool_context.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        PermissionError(13, "The process cannot access the file because it is being used"),
        OSError(5, "Input/output error"),
        FileNotFoundError(2, "No such file or directory"),
    ],
    ids=["permission", "io", "vanished-between-exists-and-open"],
)
async def test_an_io_error_while_hashing_is_reported_in_place_of_the_marker(
    publishing_ctx: Any, monkeypatch: pytest.MonkeyPatch, error: OSError
) -> None:
    def _cannot_read(path: Any, **_kwargs: Any) -> str:
        raise error

    monkeypatch.setattr(artifacts_mod, "sha256_file", _cannot_read)

    out = await publish_inline_artifacts(f"results ready\n{marker()}\ndone")

    assert out.startswith("results ready\n")
    assert out.endswith("\ndone")
    assert "publish_artifact path=" not in out
    assert "[inline artifact not published:" in out
    assert str(error) in out


@pytest.mark.asyncio
async def test_an_io_error_on_one_marker_does_not_stop_the_others(
    publishing_ctx: Any, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (workspace / "pear.cards.json").write_text('{"type":"cards","cards":[]}', encoding="utf-8")
    real = artifacts_mod.sha256_file

    def _apple_is_locked(path: Any, **kwargs: Any) -> str:
        if Path(path).name == "apple.cards.json":
            raise PermissionError(13, "locked")
        return real(path, **kwargs)

    monkeypatch.setattr(artifacts_mod, "sha256_file", _apple_is_locked)

    out = await publish_inline_artifacts(
        f"{marker('apple.cards.json')}\n{marker('pear.cards.json')}"
    )

    assert "not published: [Errno 13] locked" in out
    assert "already rendered for the user: pear.cards.json" in out
    assert [a["name"] for a in publishing_ctx.published_artifacts] == ["pear.cards.json"]


@pytest.mark.skipif(os.name == "nt", reason="Windows honours only the read-only bit")
@pytest.mark.asyncio
async def test_an_unreadable_file_is_reported_not_raised(
    publishing_ctx: Any, workspace: Path
) -> None:
    """No stubs: a real 0o000 file makes publish_artifact's own open() fail."""
    if os.geteuid() == 0:
        pytest.skip("root can read anything")
    target = workspace / "apple.cards.json"
    target.chmod(0o000)
    try:
        out = await publish_inline_artifacts(f"ok\n{marker()}")
    finally:
        target.chmod(0o644)

    assert out.startswith("ok\n")
    assert "[inline artifact not published:" in out
    assert "Permission denied" in out


@pytest.mark.asyncio
async def test_a_defect_in_publishing_still_surfaces(
    publishing_ctx: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Best-effort covers what the file system does, not what the code does:
    the handler is (ToolError, OSError), deliberately not Exception."""

    def _bug(path: Any, **_kwargs: Any) -> str:
        raise TypeError("sha256_file() got an unexpected argument")

    monkeypatch.setattr(artifacts_mod, "sha256_file", _bug)

    with pytest.raises(TypeError):
        await publish_inline_artifacts(marker())


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
