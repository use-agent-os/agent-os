"""A deliverable's identity is (sha256, name, mime), not bytes alone (#1793).

``publish_artifact``'s in-turn dedup keyed on ``sha256`` only::

    for published in reversed(ctx.published_artifacts):
        if published.get("sha256") != target_sha256:
            continue
        ...
        return {"status": "already_published", ...}

so a second file with identical bytes — two empty files, a template and the
copy made from it, two stub reports — returned ``already_published`` pointing
at the *first* file's artifact id and name, merged with the second file's
``workspace_path``. The second file was never registered in
``ctx.published_artifacts``, so nothing downstream could deliver it: the user
asked for two attachments and received one, named after the wrong file.

``ArtifactStore.find_existing_ref`` — the layer immediately below — has always
keyed on ``(sha256, name, mime)``. These tests pin the in-turn check to the
same triple.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentos.tools.builtin.artifacts import publish_artifact
from agentos.tools.types import CallerKind, ToolContext, current_tool_context

_CONTENT = "shared bytes\n"


def _ctx(tmp_path: Path, *, session: str = "session-1") -> ToolContext:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return ToolContext(
        caller_kind=CallerKind.WEB,
        workspace_dir=str(workspace),
        artifact_media_root=str(tmp_path / "media"),
        artifact_session_id=session,
        session_key=f"agent:main:webchat:{session}",
    )


def _write(ctx: ToolContext, name: str, content: str = _CONTENT) -> Path:
    path = Path(ctx.workspace_dir) / name
    path.write_text(content, encoding="utf-8")
    return path


async def _publish(ctx: ToolContext, path: str, **kwargs: object) -> dict:
    token = current_tool_context.set(ctx)
    try:
        return json.loads(await publish_artifact(path=path, **kwargs))
    finally:
        current_tool_context.reset(token)


# ── The reported case: distinct files, identical bytes ─────────────────────


@pytest.mark.asyncio
async def test_two_files_with_identical_bytes_are_two_deliverables(
    tmp_path: Path,
) -> None:
    """Fails without the fix: the second publish returned first.txt's artifact."""
    ctx = _ctx(tmp_path)
    _write(ctx, "first.txt")
    _write(ctx, "second.txt")

    first = await _publish(ctx, "first.txt")
    second = await _publish(ctx, "second.txt")

    assert first["status"] == "published"
    assert second["status"] == "published"
    assert second["artifact"]["name"] == "second.txt"
    assert second["artifact"]["id"] != first["artifact"]["id"]


@pytest.mark.asyncio
async def test_both_files_are_registered_for_delivery(tmp_path: Path) -> None:
    """The registry is what the reply actually delivers from.

    Fails without the fix: only one entry was ever appended, so the second
    file could not be attached at all.
    """
    ctx = _ctx(tmp_path)
    _write(ctx, "first.txt")
    _write(ctx, "second.txt")

    await _publish(ctx, "first.txt")
    await _publish(ctx, "second.txt")

    assert [item["name"] for item in ctx.published_artifacts] == ["first.txt", "second.txt"]


@pytest.mark.asyncio
async def test_a_whole_batch_of_identical_files_survives(tmp_path: Path) -> None:
    """Three stub reports written from one template — the shape a generator
    produces. Every one of them has to arrive, under its own name."""
    ctx = _ctx(tmp_path)
    for name in ("q1.txt", "q2.txt", "q3.txt"):
        _write(ctx, name, "TBD\n")

    results = [await _publish(ctx, name) for name in ("q1.txt", "q2.txt", "q3.txt")]

    assert [r["status"] for r in results] == ["published"] * 3
    assert [r["artifact"]["name"] for r in results] == ["q1.txt", "q2.txt", "q3.txt"]
    assert len({r["artifact"]["id"] for r in results}) == 3
    assert [item["name"] for item in ctx.published_artifacts] == ["q1.txt", "q2.txt", "q3.txt"]


# ── The name= and mime= overrides are part of the identity ────────────────


@pytest.mark.asyncio
async def test_two_files_stay_distinct_under_explicit_names(tmp_path: Path) -> None:
    """The ``name=`` override does not merge two files either."""
    ctx = _ctx(tmp_path)
    _write(ctx, "first.txt")
    _write(ctx, "second.txt")

    first = await _publish(ctx, "first.txt", name="chapter-one.txt")
    second = await _publish(ctx, "second.txt", name="chapter-two.txt")

    assert first["artifact"]["name"] == "chapter-one.txt"
    assert second["status"] == "published"
    assert second["artifact"]["name"] == "chapter-two.txt"
    assert second["artifact"]["id"] != first["artifact"]["id"]


@pytest.mark.asyncio
async def test_republishing_one_file_under_a_display_name_is_still_one_deliverable(
    tmp_path: Path,
) -> None:
    """Guard: passes either way by design, and it is the reason the name is
    matched against the source basename as well as the requested name.

    A generated image published as ``generated-image.png`` and then handed a
    friendlier display name is one deliverable, not two — the behaviour
    ``test_publish_artifact_tool_is_idempotent_for_existing_turn_artifact``
    already pins. Narrowing the dedup key must not turn that into a second
    upload of the same file.
    """
    ctx = _ctx(tmp_path)
    _write(ctx, "generated-image.png", "png-bytes")

    first = await _publish(ctx, "generated-image.png", name="generated-image.png")
    second = await _publish(ctx, "generated-image.png", name="AgentOS-Mascot.png")

    assert second["status"] == "already_published"
    assert second["artifact"]["id"] == first["artifact"]["id"]
    assert len(ctx.published_artifacts) == 1


@pytest.mark.asyncio
async def test_same_name_and_bytes_under_a_different_mime(tmp_path: Path) -> None:
    """mime is the third element of the triple find_existing_ref keys on.

    The maintainer's scope note asks for name *and* mime; a CSV re-published
    as text/plain is a different deliverable to the store, so the in-turn
    check must not answer for it.
    """
    ctx = _ctx(tmp_path)
    _write(ctx, "rows.csv", "a,b\n1,2\n")

    first = await _publish(ctx, "rows.csv", mime="text/csv")
    second = await _publish(ctx, "rows.csv", mime="text/plain")

    assert first["artifact"]["mime"] == "text/csv"
    assert second["status"] == "published"
    assert second["artifact"]["mime"] == "text/plain"
    assert second["artifact"]["id"] != first["artifact"]["id"]


# ── Dedup must still dedup ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_republishing_the_same_file_is_still_deduped(tmp_path: Path) -> None:
    """Guard: passes either way by design — the point of the in-turn check."""
    ctx = _ctx(tmp_path)
    _write(ctx, "report.txt")

    first = await _publish(ctx, "report.txt")
    again = await _publish(ctx, "report.txt")

    assert again["status"] == "already_published"
    assert again["artifact"]["id"] == first["artifact"]["id"]
    assert len(ctx.published_artifacts) == 1


@pytest.mark.asyncio
async def test_republishing_under_the_same_explicit_name_is_deduped(
    tmp_path: Path,
) -> None:
    """The override has to match on the way *in* as well as the way out."""
    ctx = _ctx(tmp_path)
    _write(ctx, "report.txt")

    first = await _publish(ctx, "report.txt", name="final.txt", mime="text/plain")
    again = await _publish(ctx, "report.txt", name="final.txt", mime="text/plain")

    assert again["status"] == "already_published"
    assert again["artifact"]["id"] == first["artifact"]["id"]
    assert len(ctx.published_artifacts) == 1


@pytest.mark.asyncio
async def test_a_copy_under_the_same_name_in_a_subdirectory_is_deduped(
    tmp_path: Path,
) -> None:
    """Identity is the deliverable's name, not the path it came from.

    Guard: passes either way by design. ``docs/report.txt`` and
    ``report.txt`` publish the same bytes under the same artifact name, so
    they are one deliverable — narrowing the check must not split them.
    """
    ctx = _ctx(tmp_path)
    _write(ctx, "report.txt")
    (Path(ctx.workspace_dir) / "docs").mkdir()
    _write(ctx, "docs/report.txt")

    first = await _publish(ctx, "report.txt")
    again = await _publish(ctx, "docs/report.txt")

    assert again["status"] == "already_published"
    assert again["artifact"]["id"] == first["artifact"]["id"]


@pytest.mark.asyncio
async def test_a_different_session_publishes_its_own_artifact(tmp_path: Path) -> None:
    """Guard: the in-turn registry is per-context and must stay that way."""
    first_ctx = _ctx(tmp_path, session="session-1")
    _write(first_ctx, "report.txt")
    await _publish(first_ctx, "report.txt")

    second_ctx = _ctx(tmp_path, session="session-2")
    second = await _publish(second_ctx, "report.txt")

    assert second["status"] == "published"
    assert len(second_ctx.published_artifacts) == 1
