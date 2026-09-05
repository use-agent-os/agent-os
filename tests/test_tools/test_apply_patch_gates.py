from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentos.gateway.approval_queue import get_approval_queue, reset_approval_queue
from agentos.sandbox import sensitive_paths
from agentos.tools.builtin import patch as patch_tool
from agentos.tools.registry import get_default_registry
from agentos.tools.types import (
    InteractionMode,
    ToolContext,
    ToolError,
    current_tool_context,
)


def _original_async(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
    return fn.__wrapped__.__wrapped__  # type: ignore[attr-defined, no-any-return]


@pytest.fixture(autouse=True)
def _reset_approval_queue():
    reset_approval_queue()
    yield
    reset_approval_queue()


def test_apply_patch_schema_exposes_optional_approval_id() -> None:
    registered = get_default_registry().get("apply_patch")

    assert registered is not None
    assert "approval_id" in registered.spec.parameters
    assert "approval_id" not in registered.spec.required


@pytest.mark.asyncio
async def test_apply_patch_blocks_sensitive_path(tmp_path: Path) -> None:
    token = current_tool_context.set(ToolContext(workspace_dir=str(tmp_path)))
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        result = await apply_patch(
            """*** Begin Patch
*** Add File: .env
+TOKEN=secret
*** End Patch"""
        )
    finally:
        current_tool_context.reset(token)

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "sensitive_path"
    assert not (tmp_path / ".env").exists()


@pytest.mark.asyncio
async def test_apply_patch_blocks_sensitive_key_file_suffix(tmp_path: Path) -> None:
    token = current_tool_context.set(ToolContext(workspace_dir=str(tmp_path)))
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        result = await apply_patch(
            """*** Begin Patch
*** Add File: id_rsa
+secret
*** End Patch"""
        )
    finally:
        current_tool_context.reset(token)

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["sensitive_path"] == "/id_rsa"
    assert not (tmp_path / "id_rsa").exists()


@pytest.mark.asyncio
async def test_apply_patch_blocks_workspace_write_deny_glob(tmp_path: Path) -> None:
    token = current_tool_context.set(
        ToolContext(
            workspace_dir=str(tmp_path),
            workspace_write_deny_globs=["blocked/**"],
        )
    )
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        result = await apply_patch(
            """*** Begin Patch
*** Add File: blocked/generated.txt
+nope
*** End Patch"""
        )
    finally:
        current_tool_context.reset(token)

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "workspace_write_deny"
    assert payload["matched_pattern"] == "blocked/**"
    assert not (tmp_path / "blocked" / "generated.txt").exists()


@pytest.mark.asyncio
async def test_apply_patch_allows_workspace_under_sensitive_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(sensitive_paths, "_SENSITIVE_PREFIXES", (str(tmp_path),))
    monkeypatch.setattr(
        sensitive_paths,
        "_WORKSPACE_PARENT_EXCEPTION_MARKERS",
        (str(tmp_path),),
    )
    token = current_tool_context.set(ToolContext(workspace_dir=str(workspace)))
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        result = await apply_patch(
            """*** Begin Patch
*** Add File: docs/plan.md
+hello
*** End Patch"""
        )
    finally:
        current_tool_context.reset(token)

    assert result == "Applied patch: 1 file(s) added"
    assert (workspace / "docs" / "plan.md").read_text(encoding="utf-8") == "hello"


@pytest.mark.asyncio
async def test_apply_patch_workspace_exception_keeps_leaf_secret_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(sensitive_paths, "_SENSITIVE_PREFIXES", (str(tmp_path),))
    monkeypatch.setattr(
        sensitive_paths,
        "_WORKSPACE_PARENT_EXCEPTION_MARKERS",
        (str(tmp_path),),
    )
    token = current_tool_context.set(ToolContext(workspace_dir=str(workspace)))
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        result = await apply_patch(
            """*** Begin Patch
*** Add File: .env
+TOKEN=secret
*** End Patch"""
        )
    finally:
        current_tool_context.reset(token)

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "sensitive_path"
    assert not (workspace / ".env").exists()


@pytest.mark.asyncio
async def test_apply_patch_workspace_escape_requests_patch_level_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("old\n", encoding="utf-8")
    monkeypatch.setattr(patch_tool, "_default_patch_root", lambda: tmp_path.resolve())
    token = current_tool_context.set(ToolContext(workspace_dir=str(workspace)))
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        result = await apply_patch(
            """*** Begin Patch
*** Update File: outside.txt
@@@ -1,1 +1,1 @@@
-old
+new
*** End Patch"""
        )
    finally:
        current_tool_context.reset(token)

    payload = json.loads(result)
    assert payload["status"] == "approval_required"
    assert payload["approval_id"]
    assert payload["outside_paths"] == [str(outside.resolve())]
    assert payload["ops"] == [
        {"op": "update", "path": "outside.txt", "resolved_path": str(outside.resolve())}
    ]
    assert payload["workspace"] == str(workspace.resolve())
    assert payload["patch_root"] == str(tmp_path.resolve())
    pending = get_approval_queue().list_pending("exec")
    assert len(pending) == 1
    params = pending[0]["params"]
    assert params["toolName"] == "apply_patch"
    assert params["args"]["fingerprint"]
    assert params["args"]["outside_paths"] == [str(outside.resolve())]
    assert outside.read_text(encoding="utf-8") == "old\n"


@pytest.mark.asyncio
async def test_apply_patch_approved_reentry_applies_and_consumes_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("old\n", encoding="utf-8")
    monkeypatch.setattr(patch_tool, "_default_patch_root", lambda: tmp_path.resolve())
    token = current_tool_context.set(
        ToolContext(
            workspace_dir=str(workspace),
            session_key="agent:main:test",
            agent_id="main",
        )
    )
    patch = """*** Begin Patch
*** Update File: outside.txt
@@@ -1,1 +1,1 @@@
-old
+new
*** End Patch"""
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        first = json.loads(await apply_patch(patch))
        approval_id = first["approval_id"]
        get_approval_queue().resolve(approval_id, True)
        result = await apply_patch(patch, approval_id=approval_id)
        with pytest.raises(ToolError, match="already consumed"):
            await apply_patch(patch, approval_id=approval_id)
    finally:
        current_tool_context.reset(token)

    assert result == "Applied patch: 1 file(s) modified"
    assert outside.read_text(encoding="utf-8") == "new\n"
    assert get_approval_queue().get(approval_id).consumed is True


@pytest.mark.asyncio
async def test_apply_patch_rejects_mismatched_approval_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("old\n", encoding="utf-8")
    monkeypatch.setattr(patch_tool, "_default_patch_root", lambda: tmp_path.resolve())
    token = current_tool_context.set(ToolContext(workspace_dir=str(workspace)))
    patch = """*** Begin Patch
*** Update File: outside.txt
@@@ -1,1 +1,1 @@@
-old
+new
*** End Patch"""
    changed_patch = """*** Begin Patch
*** Update File: outside.txt
@@@ -1,1 +1,1 @@@
-old
+evil
*** End Patch"""
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        first = json.loads(await apply_patch(patch))
        approval_id = first["approval_id"]
        get_approval_queue().resolve(approval_id, True)
        with pytest.raises(ToolError, match="does not match"):
            await apply_patch(changed_patch, approval_id=approval_id)
    finally:
        current_tool_context.reset(token)

    assert outside.read_text(encoding="utf-8") == "old\n"


@pytest.mark.asyncio
async def test_apply_patch_pending_and_denied_approval_do_not_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("old\n", encoding="utf-8")
    monkeypatch.setattr(patch_tool, "_default_patch_root", lambda: tmp_path.resolve())
    token = current_tool_context.set(ToolContext(workspace_dir=str(workspace)))
    patch = """*** Begin Patch
*** Update File: outside.txt
@@@ -1,1 +1,1 @@@
-old
+new
*** End Patch"""
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        first = json.loads(await apply_patch(patch))
        approval_id = first["approval_id"]
        pending = json.loads(await apply_patch(patch, approval_id=approval_id))
        get_approval_queue().resolve(approval_id, False)
        denied = json.loads(await apply_patch(patch, approval_id=approval_id))
    finally:
        current_tool_context.reset(token)

    assert pending["status"] == "approval_pending"
    assert denied["status"] == "approval_denied"
    assert outside.read_text(encoding="utf-8") == "old\n"


@pytest.mark.asyncio
async def test_apply_patch_rejects_foreign_namespace_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("old\n", encoding="utf-8")
    monkeypatch.setattr(patch_tool, "_default_patch_root", lambda: tmp_path.resolve())
    token = current_tool_context.set(ToolContext(workspace_dir=str(workspace)))
    patch = """*** Begin Patch
*** Update File: outside.txt
@@@ -1,1 +1,1 @@@
-old
+new
*** End Patch"""
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        first = json.loads(await apply_patch(patch))
        params = get_approval_queue().get(first["approval_id"]).params
        foreign_id = get_approval_queue().request("plugin", params=params)
        get_approval_queue().resolve(foreign_id, True)
        with pytest.raises(ToolError, match="exec namespace"):
            await apply_patch(patch, approval_id=foreign_id)
    finally:
        current_tool_context.reset(token)

    assert outside.read_text(encoding="utf-8") == "old\n"


@pytest.mark.asyncio
async def test_apply_patch_sensitive_path_blocks_even_with_approval_id(tmp_path: Path) -> None:
    approval_id = get_approval_queue().request(
        "exec",
        {
            "toolName": "apply_patch",
            "command": "apply_patch pretend",
            "args": {},
        },
    )
    get_approval_queue().resolve(approval_id, True)
    token = current_tool_context.set(ToolContext(workspace_dir=str(tmp_path)))
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        result = await apply_patch(
            """*** Begin Patch
*** Add File: .env
+TOKEN=secret
*** End Patch""",
            approval_id=approval_id,
        )
    finally:
        current_tool_context.reset(token)

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "sensitive_path"
    assert not (tmp_path / ".env").exists()


@pytest.mark.asyncio
async def test_apply_patch_rejects_foreign_posix_path_on_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(patch_tool, "os", SimpleNamespace(name="nt"), raising=False)
    token = current_tool_context.set(ToolContext(workspace_dir=str(tmp_path)))
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        with pytest.raises(ToolError, match="foreign_host_path"):
            await apply_patch(
                """*** Begin Patch
*** Add File: /Users/a1/Desktop/report.txt
+new
*** End Patch"""
            )
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_apply_patch_rejects_foreign_windows_path_on_posix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(patch_tool, "os", SimpleNamespace(name="posix"), raising=False)
    token = current_tool_context.set(ToolContext(workspace_dir=str(tmp_path)))
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        with pytest.raises(ToolError, match="foreign_host_path"):
            await apply_patch(
                """*** Begin Patch
*** Add File: C:\\Users\\a1\\Desktop\\report.txt
+new
*** End Patch"""
            )
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_apply_patch_elevated_full_skips_outside_workspace_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("old\n", encoding="utf-8")
    monkeypatch.setattr(patch_tool, "_default_patch_root", lambda: tmp_path.resolve())
    token = current_tool_context.set(ToolContext(workspace_dir=str(workspace), elevated="full"))
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        result = await apply_patch(
            """*** Begin Patch
*** Update File: outside.txt
@@@ -1,1 +1,1 @@@
-old
+new
*** End Patch"""
        )
    finally:
        current_tool_context.reset(token)

    assert result == "Applied patch: 1 file(s) modified"
    assert outside.read_text(encoding="utf-8") == "new\n"
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_apply_patch_unattended_bypass_skips_outside_workspace_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("old\n", encoding="utf-8")
    monkeypatch.setattr(patch_tool, "_default_patch_root", lambda: tmp_path.resolve())
    token = current_tool_context.set(
        ToolContext(
            workspace_dir=str(workspace),
            elevated="bypass",
            interaction_mode=InteractionMode.UNATTENDED,
        )
    )
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        result = await apply_patch(
            """*** Begin Patch
*** Update File: outside.txt
@@@ -1,1 +1,1 @@@
-old
+new
*** End Patch"""
        )
    finally:
        current_tool_context.reset(token)

    assert result == "Applied patch: 1 file(s) modified"
    assert outside.read_text(encoding="utf-8") == "new\n"
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_apply_patch_add_file_refuses_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "existing.txt"
    target.write_text("old\n", encoding="utf-8")
    token = current_tool_context.set(ToolContext(workspace_dir=str(tmp_path)))
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        with pytest.raises(FileExistsError, match="File already exists"):
            await apply_patch(
                """*** Begin Patch
*** Add File: existing.txt
+new
*** End Patch"""
            )
    finally:
        current_tool_context.reset(token)
    assert target.read_text(encoding="utf-8") == "old\n"


@pytest.mark.asyncio
async def test_apply_patch_records_workspace_file_writes_for_add_and_update(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "existing.csv"
    existing.write_text("a,b\n1,2\n", encoding="utf-8")
    ctx = ToolContext(workspace_dir=str(tmp_path))
    token = current_tool_context.set(ctx)
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        result = await apply_patch(
            """*** Begin Patch
*** Add File: created.json
+{"hello": "world"}
*** Update File: existing.csv
@@@ -1,2 +1,3 @@@
 a,b
 1,2
+3,4
*** End Patch"""
        )
    finally:
        current_tool_context.reset(token)

    assert "1 file(s) added, 1 file(s) modified" in result
    written_relative_paths = [w["relative_path"] for w in ctx.workspace_file_writes]
    assert "created.json" in written_relative_paths
    assert "existing.csv" in written_relative_paths


@pytest.mark.asyncio
async def test_apply_patch_supports_hunk_header_without_explicit_counts(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "hello.txt"
    existing.write_text("line1\n", encoding="utf-8")
    token = current_tool_context.set(ToolContext(workspace_dir=str(tmp_path)))
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        result = await apply_patch(
            """*** Begin Patch
*** Update File: hello.txt
@@@ -1 +1,2 @@@
 line1
+line2
*** End Patch"""
        )
    finally:
        current_tool_context.reset(token)

    assert result == "Applied patch: 1 file(s) modified"
    assert existing.read_text(encoding="utf-8") == "line1\nline2\n"


@pytest.mark.parametrize(
    "header",
    [
        "@@@",
        "@@@ @@@",
        "@@@ -abc +def @@@",
        "@@@ - +1 @@@",
        "@@@ -1 + @@@",
        "@@@ -1,2 @@@",
        "@@@ +1,2 @@@",
        "@@@ -1 +1",
        "-1 +1 @@@",
        "@@@ -1, +1,2 @@@",
        "@@@ -1,2 +1, @@@",
        "",
        "not a header at all",
    ],
)
def test_parse_hunk_header_rejects_malformed_input(header: str) -> None:
    from agentos.tools.builtin.patch import _parse_hunk_header

    with pytest.raises(ValueError, match="Invalid hunk header"):
        _parse_hunk_header(header)


@pytest.mark.asyncio
async def test_apply_patch_handles_crlf_file_line_endings(tmp_path: Path) -> None:
    target = tmp_path / "crlf_file.txt"
    target.write_bytes(b"first line\r\nsecond line\r\n")
    token = current_tool_context.set(ToolContext(workspace_dir=str(tmp_path)))
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        result = await apply_patch(
            """*** Begin Patch
*** Update File: crlf_file.txt
@@@ -1,2 +1,3 @@@
 first line
-second line
+modified line
+added line
*** End Patch"""
        )
    finally:
        current_tool_context.reset(token)

    assert result == "Applied patch: 1 file(s) modified"
    assert target.read_bytes() == b"first line\r\nmodified line\r\nadded line\r\n"


@pytest.mark.asyncio
async def test_apply_patch_preserves_lf_file_line_endings_on_all_platforms(tmp_path: Path) -> None:
    target = tmp_path / "lf_file.txt"
    target.write_bytes(b"first line\nsecond line\n")
    token = current_tool_context.set(ToolContext(workspace_dir=str(tmp_path)))
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        result = await apply_patch(
            """*** Begin Patch
*** Update File: lf_file.txt
@@@ -1,2 +1,3 @@@
 first line
-second line
+modified line
+added line
*** End Patch"""
        )
    finally:
        current_tool_context.reset(token)

    assert result == "Applied patch: 1 file(s) modified"
    assert target.read_bytes() == b"first line\nmodified line\nadded line\n"


def test_apply_hunk_crlf_matching_and_preservation() -> None:
    from agentos.tools.builtin.patch import Hunk, _apply_hunk

    file_lines = ["header\r\n", "remove_me\r\n", "footer\r\n"]
    hunk = Hunk(
        old_start=1,
        old_count=3,
        new_start=1,
        new_count=3,
        lines=[" header", "-remove_me", "+added_line", " footer"],
    )
    result = _apply_hunk(file_lines, hunk)
    assert result == ["header\r\n", "added_line\r\n", "footer\r\n"]
