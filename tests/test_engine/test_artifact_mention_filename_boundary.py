"""Issue #1978: the auto-publish backstop must match a written file's name on
a filename boundary, not by plain substring containment.

``data.json`` is a substring of ``metadata.json``; ``out.csv`` sits inside
``checkout.csv`` and ``out.csv.bak``. A reply that names only the longer file
must not deliver the shorter one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentos.engine.artifact_delivery import (
    _text_mentions_written_file,
    auto_publish_omitted_workspace_artifacts,
)
from agentos.tools.types import CallerKind, ToolContext


def _record(workspace: Path, relative: str) -> dict[str, object]:
    target = (workspace / relative).resolve()
    return {
        "path": str(target),
        "relative_path": relative,
        "name": target.name,
        "suffix": target.suffix,
    }


@pytest.mark.parametrize(
    ("final_text", "relative", "expected"),
    [
        # Substring of a longer filename: not a mention.
        ("I refreshed metadata.json with the new schema.", "data.json", False),
        ("Wrote checkout.csv for you.", "out.csv", False),
        ("Saved handout.csv and layout.csv.", "out.csv", False),
        ("See weekly-report.csv.", "report.csv", False),
        ("Backed up to out.csv.bak first.", "out.csv", False),
        ("Attached report_data.json.", "data.json", False),
        # Real mentions keep matching.
        ("Wrote data.json.", "data.json", True),
        ("Wrote data.json", "data.json", True),
        ("Wrote `data.json` for you.", "data.json", True),
        ("Wrote (data.json) for you.", "data.json", True),
        ("Wrote Data.JSON for you.", "data.json", True),
        ("Files: data.json, metadata.json", "data.json", True),
        ("Check out/data.json", "out/data.json", True),
        # A bare name still matches inside a spelled-out path.
        ("Check out/data.json", "data.json", True),
        ("Check out\\data.json", "data.json", True),
        # The relative path and the absolute path are also accepted.
        ("Check sub/out.csv", "sub/out.csv", True),
    ],
)
def test_text_mentions_written_file_boundaries(
    tmp_path: Path, final_text: str, relative: str, expected: bool
) -> None:
    record = _record(tmp_path / "workspace", relative)
    assert _text_mentions_written_file(final_text, record) is expected


def test_absolute_path_mention_matches(tmp_path: Path) -> None:
    record = _record(tmp_path / "workspace", "data.json")
    assert _text_mentions_written_file(f"Wrote {record['path']}", record) is True


def test_backstop_skips_file_whose_name_is_only_a_substring(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "data.json").write_text('{"rows": 1}', encoding="utf-8")

    ctx = ToolContext(
        caller_kind=CallerKind.WEB,
        workspace_dir=str(workspace),
        artifact_media_root=str(tmp_path / "media"),
        artifact_session_id="sess-1",
        session_key="agent:main:webchat:x",
    )
    ctx.workspace_file_writes.append(_record(workspace, "data.json"))

    result = auto_publish_omitted_workspace_artifacts(
        ctx, final_text="I refreshed metadata.json with the new schema."
    )

    assert result.artifacts == []
    assert result.failure_summaries == []
    assert ctx.published_artifacts == []


def test_backstop_still_publishes_a_named_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "data.json").write_text('{"rows": 1}', encoding="utf-8")

    ctx = ToolContext(
        caller_kind=CallerKind.WEB,
        workspace_dir=str(workspace),
        artifact_media_root=str(tmp_path / "media"),
        artifact_session_id="sess-1",
        session_key="agent:main:webchat:x",
    )
    ctx.workspace_file_writes.append(_record(workspace, "data.json"))

    result = auto_publish_omitted_workspace_artifacts(ctx, final_text="I wrote data.json.")

    assert [a["name"] for a in result.artifacts] == ["data.json"]
