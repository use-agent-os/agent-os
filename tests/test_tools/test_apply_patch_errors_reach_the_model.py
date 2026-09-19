"""Issue #2977: every apply_patch diagnostic reached the model as
"The tool received an invalid argument".

The failure envelope forwards a message only for ``SafeToolUserMessage``
subclasses; ``apply_patch`` raised plain ``ValueError`` at every parse and
apply site, so the marker, the offending line, the hunk header, the path and
the mismatched context were all discarded before the model saw them -- and a
retry had nothing to correct against. ``PatchError`` (a ``SafeToolError``
that is still a ``ValueError``) carries the text through. The one message
that quotes a line of the target file -- a context mismatch -- is passed
through ``redact_file_output`` on its way out.

Every test here drives the tool through the public dispatch path
(``build_tool_handler`` -> ``finalize``), the way the model receives it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentos.sandbox.config import SandboxSettings
from agentos.sandbox.integration import configure_runtime, reset_runtime
from agentos.tool_boundary import ToolCall
from agentos.tools import get_default_registry
from agentos.tools.builtin import patch as patch_tool
from agentos.tools.builtin.patch import PatchError, _parse_patch, _plan_ops
from agentos.tools.dispatch import build_tool_handler
from agentos.tools.types import CallerKind, SafeToolError, ToolContext, current_tool_context

AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


def _update(path: str, header: str, *lines: str) -> str:
    body = "".join(f"{line}\n" for line in lines)
    return f"*** Begin Patch\n*** Update File: {path}\n{header}\n{body}*** End Patch\n"


@pytest.fixture(autouse=True)
def _runtime(tmp_path: Path):
    """``sandbox=False`` so the ``@sandboxed`` gate runs the handler inline."""
    configure_runtime(
        SandboxSettings(sandbox=False, security_grading=False, allow_legacy_mode=True),
        workspace=tmp_path,
    )
    try:
        yield
    finally:
        reset_runtime()


async def _failure(workspace: Path, patch: str) -> dict[str, Any]:
    ctx = ToolContext(
        caller_kind=CallerKind.CLI,
        channel_kind="cli",
        channel_id="cli:test",
        session_key="agent:main:webchat:cafe0001",
        workspace_dir=str(workspace),
    )
    handler = build_tool_handler(get_default_registry(), ctx)
    result = await handler(
        ToolCall(tool_use_id="patch-1", tool_name="apply_patch", arguments={"patch": patch})
    )
    assert result.is_error is True, result.content
    envelope = json.loads(result.content)
    assert envelope["status"] == "error"
    assert envelope["tool"] == "apply_patch"
    return envelope


# ── each site, as the model reads it ───────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("patch", "expected"),
    [
        pytest.param(
            "*** Update File: app.py\n@@@ -1,1 +1,1 @@@\n-a\n+b\n*** End Patch\n",
            "Missing '*** Begin Patch' marker",
            id="begin-marker",
        ),
        pytest.param(
            "*** Begin Patch\n*** Update File: app.py\n@@@ -1,1 +1,1 @@@\n-a\n+b\n",
            "Missing '*** End Patch' marker",
            id="end-marker",
        ),
        pytest.param(
            "*** Begin Patch\n*** End Patch\n",
            "No operations found between '*** Begin Patch' and '*** End Patch'",
            id="no-operations",
        ),
        pytest.param(
            "*** Begin Patch\n*** Add File: notes.txt\nhello\n*** End Patch\n",
            "Invalid line in '*** Add File: notes.txt' block (expected a '+' prefix): 'hello'",
            id="add-file-line",
        ),
        pytest.param(
            _update("app.py", "@@@ nope @@@", "-a", "+b"),
            "Invalid hunk header: '@@@ nope @@@'",
            id="hunk-header",
        ),
        pytest.param(
            _update("../../outside.py", "@@@ -1,1 +1,1 @@@", "-a", "+b"),
            "resolves outside patch root",
            id="path-traversal",
        ),
        pytest.param(
            _update("app.py", "@@@ -1,3 +1,3 @@@", "-print('old')", "-x", "-y", "+z"),
            "Update File: app.py: Hunk context/delete at line 2 exceeds file length",
            id="past-end-of-file",
        ),
        pytest.param(
            _update("app.py", "@@@ -1,1 +1,1 @@@", "-print('WRONG')", "+print('new')"),
            "Update File: app.py: Context mismatch at line 1: "
            "expected \"print('WRONG')\", got \"print('old')\"",
            id="context-mismatch",
        ),
    ],
)
async def test_the_diagnostic_reaches_the_model(tmp_path: Path, patch: str, expected: str) -> None:
    target = tmp_path / "app.py"
    target.write_text("print('old')\n", encoding="utf-8")

    envelope = await _failure(tmp_path, patch)

    assert expected in envelope["user_message"]
    assert "invalid argument" not in envelope["user_message"]
    assert envelope["error_class"] == "PatchError"
    assert envelope["retry_allowed"] is False
    assert target.read_text(encoding="utf-8") == "print('old')\n"


@pytest.mark.asyncio
async def test_a_context_mismatch_does_not_quote_a_secret_from_the_file(tmp_path: Path) -> None:
    """The mismatch message carries the line the file actually holds; that is a
    file-read channel and gets the same mask as read_file."""
    (tmp_path / "credentials").write_text(
        f"aws_secret_access_key = {AWS_SECRET}\n", encoding="utf-8"
    )
    patch = (
        "*** Begin Patch\n*** Update File: credentials\n@@@ -1,1 +1,1 @@@\n"
        "-aws_secret_access_key = NOPE\n+aws_secret_access_key = NEW\n*** End Patch\n"
    )

    envelope = await _failure(tmp_path, patch)

    assert "Context mismatch at line 1" in envelope["user_message"]
    assert AWS_SECRET not in envelope["user_message"]
    assert "aws_secret_access_key = NOPE" in envelope["user_message"]


@pytest.mark.asyncio
async def test_a_missing_update_target_keeps_its_file_not_found_shape(tmp_path: Path) -> None:
    """Only the ValueError sites change; an OSError keeps its class and message."""
    patch = "*** Begin Patch\n*** Update File: ghost.py\n@@@ -1,1 +1,1 @@@\n-a\n+b\n*** End Patch\n"

    envelope = await _failure(tmp_path, patch)

    assert envelope["error_class"] == "FileNotFoundError"


# ── the type keeps every existing contract ────────────────────────────────


def test_patch_error_is_both_a_safe_tool_error_and_a_value_error() -> None:
    with pytest.raises(ValueError):
        _parse_patch("no markers here")
    with pytest.raises(SafeToolError):
        _parse_patch("no markers here")
    with pytest.raises(PatchError) as excinfo:
        _parse_patch("no markers here")
    assert excinfo.value.user_message == "Missing '*** Begin Patch' marker"


def test_plan_ops_keeps_the_op_label_on_a_patch_error(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('old')\n", encoding="utf-8")
    ops = _parse_patch(
        "*** Begin Patch\n*** Update File: app.py\n@@@ -1,1 +1,1 @@@\n-nope\n+b\n*** End Patch\n"
    )

    with pytest.raises(PatchError) as excinfo:
        _plan_ops(ops, tmp_path)

    assert str(excinfo.value).startswith("Update File: app.py: Context mismatch")
    assert excinfo.value.user_message == str(excinfo.value)


def test_plan_ops_redacts_with_the_target_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mask is path-aware (a ``.env`` is not a ``.py``); the op's own path is what it sees."""
    seen: list[Any] = []
    real = patch_tool.redact_file_output

    def spy(text: str, *, path: Any = None) -> str:
        seen.append(path)
        return real(text, path=path)

    monkeypatch.setattr(patch_tool, "redact_file_output", spy)
    (tmp_path / ".env").write_text("TOKEN=abc\n", encoding="utf-8")
    ops = _parse_patch(_update(".env", "@@@ -1,1 +1,1 @@@", "-NOPE=1", "+X=2"))

    with pytest.raises(PatchError):
        _plan_ops(ops, tmp_path)

    assert seen == [".env"]


def test_a_well_formed_patch_still_applies(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('old')\n", encoding="utf-8", newline="\n")
    token = current_tool_context.set(ToolContext(workspace_dir=str(tmp_path)))
    try:
        ops = _parse_patch(_update("app.py", "@@@ -1,1 +1,1 @@@", "-print('old')", "+print('new')"))
        staged, counts = _plan_ops(ops, tmp_path)
    finally:
        current_tool_context.reset(token)

    assert counts == (0, 1, 0)
    assert staged[0].content == "print('new')\n"
