"""A payload the pinned SDK cannot model is not a failed tool call (#2020).

``MCPStdioClient.call_tool`` validates the whole ``tools/call`` result into the
SDK's ``CallToolResult`` and turned every validation failure into a tool error::

    except Exception as exc:
        return MCPToolResult(content=str(exc), is_error=True)

so a call the server completed fine was reported to the model as having failed,
with a pydantic validation dump where the tool's answer should be. Two things
reach that branch without anything being wrong:

* a structured-only result that omits ``content`` — the same information as
  ``{"content": [], "structuredContent": {...}}``, which succeeds, so two
  spellings of one result gave opposite outcomes;
* a content block this SDK version does not know. ``pyproject`` allows
  ``mcp>=1.2.0``, whose ``CallToolResult.content`` union predates
  ``AudioContent`` and ``ResourceLink``, so a spec-compliant server can hand a
  block the installed SDK cannot parse.

``MCPSessionClient`` never re-validates, so it was unaffected either way — the
two transports disagreed again, which #1558's triage note asked to avoid.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from agentos.mcp.stdio import MCPStdioClient
from agentos.mcp.types import MCPServerConfig, MCPToolResult

_STRUCTURED_NO_CONTENT = {"structuredContent": {"rows": 3}}
_STRUCTURED_EMPTY_CONTENT = {"content": [], "structuredContent": {"rows": 3}}
_UNKNOWN_BLOCK = {"content": [{"type": "widget", "spec": {"kind": "table"}}]}
_UNKNOWN_BLOCK_FAILED = {"content": [{"type": "widget", "spec": {}}], "isError": True}
_TEXT = {"content": [{"type": "text", "text": "42 rows"}]}
_MIXED_UNKNOWN = {
    "content": [
        {"type": "text", "text": "chart follows"},
        {"type": "widget", "spec": {"kind": "chart"}},
    ]
}


async def _call(payload: Any, monkeypatch: pytest.MonkeyPatch) -> MCPToolResult:
    client = MCPStdioClient(MCPServerConfig(name="demo", transport="stdio", command="demo"))

    async def _send_request(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": 1, "result": payload}

    monkeypatch.setattr(client, "_send_request", _send_request)
    return await client.call_tool("run_report", {})


# ── A successful call must not be reported as a failure ────────────────────


@pytest.mark.asyncio
async def test_structured_only_result_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fails without the fix: is_error=True, content='1 validation error …'."""
    result = await _call(_STRUCTURED_NO_CONTENT, monkeypatch)

    assert result.is_error is False
    assert json.loads(result.content) == {"rows": 3}


@pytest.mark.asyncio
async def test_both_spellings_of_a_structured_result_agree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The omitted and the empty ``content`` array carry the same result."""
    without = await _call(_STRUCTURED_NO_CONTENT, monkeypatch)
    with_empty = await _call(_STRUCTURED_EMPTY_CONTENT, monkeypatch)

    assert without == with_empty


@pytest.mark.asyncio
async def test_an_unmodellable_block_is_kept_not_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fails without the fix: 13 validation errors replaced the tool's output."""
    result = await _call(_UNKNOWN_BLOCK, monkeypatch)

    assert result.is_error is False
    assert json.loads(result.content) == {"type": "widget", "spec": {"kind": "table"}}


@pytest.mark.asyncio
async def test_the_model_never_sees_a_validation_dump(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The content handed to the model is the tool's answer, not pydantic's."""
    for payload in (_STRUCTURED_NO_CONTENT, _UNKNOWN_BLOCK, _MIXED_UNKNOWN):
        result = await _call(payload, monkeypatch)
        assert "validation error" not in result.content
        assert "CallToolResult" not in result.content


@pytest.mark.asyncio
async def test_text_alongside_an_unmodellable_block_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One unknown block must not cost the rest of the result."""
    result = await _call(_MIXED_UNKNOWN, monkeypatch)

    first, second = result.content.split("\n", 1)
    assert first == "chart follows"
    assert json.loads(second)["type"] == "widget"


# ── The server's own verdict still rules ───────────────────────────────────


@pytest.mark.asyncio
async def test_is_error_survives_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tolerating the payload must not swallow a failure the server declared."""
    result = await _call(_UNKNOWN_BLOCK_FAILED, monkeypatch)

    assert result.is_error is True


@pytest.mark.asyncio
async def test_a_jsonrpc_error_is_still_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard: passes either way by design — the transport-level branch above
    this one is untouched."""
    client = MCPStdioClient(MCPServerConfig(name="demo", transport="stdio", command="demo"))

    async def _send_request(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "no such tool"}}

    monkeypatch.setattr(client, "_send_request", _send_request)
    result = await client.call_tool("run_report", {})

    assert result.is_error is True
    assert result.content == "no such tool"


# ── Everything #1559 shipped keeps working ────────────────────────────────


@pytest.mark.asyncio
async def test_a_plain_text_result_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard: passes either way by design."""
    result = await _call(_TEXT, monkeypatch)

    assert result == MCPToolResult(content="42 rows", is_error=False)


@pytest.mark.asyncio
async def test_a_declared_failure_is_still_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard: the half #1558 was filed for — isError on a modellable payload."""
    result = await _call(
        {"content": [{"type": "text", "text": "quota exceeded"}], "isError": True}, monkeypatch
    )

    assert result.is_error is True
    assert result.content == "quota exceeded"


@pytest.mark.asyncio
async def test_an_image_block_still_renders_through_the_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard: a payload the SDK *can* model keeps taking the SDK path, whose
    rendering the session transports share."""
    result = await _call(
        {"content": [{"type": "image", "data": "aGk=", "mimeType": "image/png"}]}, monkeypatch
    )

    assert result.is_error is False
    assert "image/png" in result.content
    assert '"annotations":null' in result.content  # SDK model_dump_json, not raw wire json


@pytest.mark.asyncio
async def test_a_non_dict_result_does_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    result = await _call(["unexpected"], monkeypatch)

    assert result == MCPToolResult(content="", is_error=False)


# ── End to end over a real subprocess server ──────────────────────────────


_STRUCTURED_ONLY_SERVER = """
import json
import sys

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message["method"]
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {}}
    elif method == "tools/call":
        result = {"structuredContent": {"rows": 3, "ok": True}}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\\n")
    sys.stdout.flush()
"""


@pytest.mark.asyncio
async def test_end_to_end_structured_only_server(tmp_path: Path) -> None:
    """Fails without the fix: a working server's every call came back an error."""
    server = tmp_path / "server.py"
    server.write_text(_STRUCTURED_ONLY_SERVER)
    client = MCPStdioClient(
        MCPServerConfig(name="demo", transport="stdio", command=sys.executable, args=[str(server)])
    )

    try:
        await client.connect()
        result = await client.call_tool("run_report", {})
    finally:
        await client.close()

    assert result.is_error is False
    assert json.loads(result.content) == {"rows": 3, "ok": True}
