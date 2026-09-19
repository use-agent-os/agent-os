"""Issue #2888 (with #2889, #2890, #2891): authored tool refusals reached the model as
"The tool received an invalid argument".

The failure envelope forwards a message only for ``SafeToolUserMessage``
subclasses -- exception text may carry secrets, so anything else is replaced
by a generic line. Four tools raised a plain ``ValueError`` whose text was
authored for the model to act on, and lost it: ``edit_file``'s closest-match
hint and ambiguous line numbers, ``grep_search``'s regex diagnostic, the
project-name reason from ``projects_create`` / ``projects_update``, and
``web_fetch``'s "Cannot resolve hostname".

Every test here drives the tool through the public dispatch path
(``build_tool_handler`` -> ``finalize``), the way the model receives it.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from agentos.sandbox.config import SandboxSettings
from agentos.sandbox.integration import configure_runtime, reset_runtime
from agentos.session.manager import SessionManager
from agentos.session.storage import SessionStorage
from agentos.tool_boundary import ToolCall
from agentos.tools import get_default_registry
from agentos.tools.builtin import projects as projects_tool
from agentos.tools.dispatch import build_tool_handler
from agentos.tools.types import CallerKind, ToolContext

SESSION_KEY = "agent:main:webchat:cafe0001"


@pytest.fixture(autouse=True)
def _runtime(tmp_path: Path):
    """``sandbox=False`` so the ``@sandboxed`` gate runs handlers inline instead
    of refusing fail-closed, as ``test_git_diff_revision`` does."""
    configure_runtime(
        SandboxSettings(sandbox=False, security_grading=False, allow_legacy_mode=True),
        workspace=tmp_path,
    )
    try:
        yield
    finally:
        reset_runtime()


def _call(tool_name: str, arguments: dict[str, Any]) -> ToolCall:
    return ToolCall(tool_use_id=f"{tool_name}-1", tool_name=tool_name, arguments=arguments)


async def _failure(ctx: ToolContext, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Dispatch the call and return the failure envelope the model would read."""
    handler = build_tool_handler(get_default_registry(), ctx)
    result = await handler(_call(tool_name, arguments))
    assert result.is_error is True, result.content
    envelope = json.loads(result.content)
    assert envelope["status"] == "error"
    assert envelope["tool"] == tool_name
    return envelope


def _workspace_ctx(workspace: Path) -> ToolContext:
    return ToolContext(
        caller_kind=CallerKind.CLI,
        channel_kind="cli",
        channel_id="cli:test",
        session_key=SESSION_KEY,
        workspace_dir=str(workspace),
    )


# ── edit_file (#2888) ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_edit_file_closest_match_hint_reaches_the_model(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("def calculate_total(items):\n    return 0\n", encoding="utf-8")

    envelope = await _failure(
        _workspace_ctx(tmp_path),
        "edit_file",
        {
            "path": "sample.py",
            "old_text": "def calculate_grand_totals(a, b, c, d):\n",
            "new_text": "x",
        },
    )

    assert "old_text not found in sample.py" in envelope["user_message"]
    assert "Closest match: line 1" in envelope["user_message"]
    assert "calculate_total" in envelope["user_message"]
    assert "invalid argument" not in envelope["user_message"]
    assert target.read_text(encoding="utf-8") == "def calculate_total(items):\n    return 0\n"


@pytest.mark.asyncio
async def test_edit_file_ambiguous_line_numbers_reach_the_model(tmp_path: Path) -> None:
    (tmp_path / "dup.py").write_text("x = 1\ny = 0\nx = 1\n", encoding="utf-8")

    envelope = await _failure(
        _workspace_ctx(tmp_path),
        "edit_file",
        {"path": "dup.py", "old_text": "x = 1", "new_text": "x = 2"},
    )

    assert "matches 2 locations in dup.py" in envelope["user_message"]
    assert "lines 1, 3" in envelope["user_message"]


@pytest.mark.asyncio
async def test_edit_file_hint_is_still_redacted_on_the_way_out(tmp_path: Path) -> None:
    """The closest-match hint quotes file lines; it must keep the file-read mask."""
    secret = "sk-proj-" + "A" * 24
    (tmp_path / "config.py").write_text(f'OPENAI_API_KEY = "{secret}"\n', encoding="utf-8")

    envelope = await _failure(
        _workspace_ctx(tmp_path),
        "edit_file",
        {"path": "config.py", "old_text": 'OPENAI_API_KEY = "sk-proj-ZZZZ"\n', "new_text": "x"},
    )

    assert "old_text not found" in envelope["user_message"]
    assert secret not in envelope["user_message"]


# ── grep_search (#2890) ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_grep_search_regex_diagnostic_reaches_the_model(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")

    envelope = await _failure(
        _workspace_ctx(tmp_path),
        "grep_search",
        {"pattern": "(unclosed", "path": "."},
    )

    assert envelope["user_message"].startswith("Invalid regex pattern:")
    assert "missing )" in envelope["user_message"]
    assert "invalid argument" not in envelope["user_message"]


# ── projects_create / projects_update (#2889) ──────────────────────────────


@pytest_asyncio.fixture
async def manager():
    store = SessionStorage(":memory:")
    await store.connect()
    mgr = SessionManager(store, inject_time_prefix=False)
    original = projects_tool._session_manager
    projects_tool.set_session_manager(mgr)
    try:
        yield mgr
    finally:
        projects_tool.set_session_manager(original)
        await store.close()


def _agent_ctx() -> ToolContext:
    return ToolContext(caller_kind=CallerKind.AGENT, session_key=SESSION_KEY, agent_id="main")


@pytest.mark.asyncio
async def test_projects_create_empty_name_reason_reaches_the_model(manager) -> None:
    envelope = await _failure(_agent_ctx(), "projects_create", {"name": "   "})

    assert envelope["user_message"] == "Project name cannot be empty"


@pytest.mark.asyncio
async def test_projects_create_duplicate_name_reason_reaches_the_model(manager) -> None:
    await manager.create_project(agent_id="main", name="Research", knowledge="")

    envelope = await _failure(_agent_ctx(), "projects_create", {"name": "research"})

    assert envelope["user_message"] == "Project name already exists: research"


@pytest.mark.asyncio
async def test_projects_update_validation_reason_reaches_the_model(manager) -> None:
    project = await manager.create_project(agent_id="main", name="Research", knowledge="v1")
    await manager.create(SESSION_KEY, agent_id="main", project_id=project["project_id"])

    envelope = await _failure(
        _agent_ctx(),
        "projects_update",
        {"project_id": project["project_id"], "name": "x" * (manager.PROJECT_NAME_MAX_CHARS + 1)},
    )

    assert envelope["user_message"] == (
        f"Project name exceeds {manager.PROJECT_NAME_MAX_CHARS} characters"
    )


@pytest.mark.asyncio
async def test_projects_update_own_project_guard_is_unchanged(manager) -> None:
    """The ToolError refusals in the same handlers keep their existing behaviour."""
    project = await manager.create_project(agent_id="main", name="Other", knowledge="")

    envelope = await _failure(
        _agent_ctx(),
        "projects_update",
        {"project_id": project["project_id"], "knowledge": "injected"},
    )

    assert envelope["error_class"] == "ToolError"


# ── web_fetch (#2891) ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_web_fetch_unresolvable_host_is_a_fetch_failure_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _no_such_host(*_args: Any, **_kwargs: Any) -> list[Any]:
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", _no_such_host)
    handler = build_tool_handler(get_default_registry(), _agent_ctx())

    result = await handler(
        _call("web_fetch", {"url": "https://nonexistent.invalid/page", "extract_mode": "text"})
    )

    assert result.is_error is False, result.content
    payload = json.loads(result.content)
    assert payload["status"] == 0
    assert payload["error"] == "Cannot resolve hostname: nonexistent.invalid"
    assert payload["url"] == "https://nonexistent.invalid/page"
    assert payload["extract_mode"] == "text"
    assert payload["text"] == ""


@pytest.mark.asyncio
async def test_web_fetch_policy_refusals_still_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blocked destination is a refusal, not an unreachable URL: it keeps its
    SSRFBlockedError and the envelope's policy message."""
    envelope = await _failure(_agent_ctx(), "web_fetch", {"url": "http://169.254.169.254/"})

    assert envelope["error_class"] == "SSRFBlockedError"
    assert "network safety policy" in envelope["user_message"]


@pytest.mark.asyncio
async def test_web_fetch_scheme_refusal_still_raises() -> None:
    envelope = await _failure(_agent_ctx(), "web_fetch", {"url": "ftp://example.com/x"})

    assert envelope["error_class"] == "UnsupportedURLSchemeError"
    assert "http://" in envelope["user_message"]
