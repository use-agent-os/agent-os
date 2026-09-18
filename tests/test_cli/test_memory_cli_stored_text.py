"""``agentos memory …`` prints stored memory as it is stored.

Rich parses ``[...]`` as markup and ``:name:`` as an emoji code in everything
``console.print`` and a ``Table`` cell render. The memory commands handed it
stored text directly, so a Markdown checkbox ``- [x]`` printed as ``-  ``, a link
label ``[docs](url)`` lost its label, ``list[int]`` became ``list``, and a
``[/]`` anywhere raised ``MarkupError``. Session names had the same defect and
were fixed by escaping at every render site; these are the memory render sites.

The gateway transport is stubbed; everything from the payload onwards is the
real command.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from agentos.cli.main import app

runner = CliRunner()

# Wide enough that no table cell below wraps: the assertions look for whole strings.
_WIDE = {"COLUMNS": "200"}

STORED = (
    "# Project notes\n"
    "- [x] shipped the v2 migration\n"
    "- [ ] follow up with [ops] about the backup window\n"
    "Deploy runbook: see [runbook](https://wiki.example.com/deploy)\n"
    "Config type: dict[str, list[int]]  :warning: rotate keys monthly\n"
    "a stray [/] closing tag and [bold]literal[/bold] text"
)


class _Gateway:
    payloads: dict[str, Any] = {}

    async def connect(self, url: str, *, token: Any = None) -> None:
        return None

    async def close(self) -> None:
        return None

    async def call(self, method: str, params: dict | None = None) -> Any:
        return type(self).payloads[method]


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> type[_Gateway]:
    _Gateway.payloads = {}
    monkeypatch.setattr("agentos.cli.gateway_client.GatewayClient", _Gateway)
    return _Gateway


def _run(*argv: str) -> str:
    result = runner.invoke(app, list(argv), env=_WIDE)
    assert result.exit_code == 0, (result.stdout, result.exception)
    return result.stdout


def test_memory_show_prints_the_file_verbatim(gateway: type[_Gateway]) -> None:
    gateway.payloads["memory.show"] = {"path": "MEMORY.md", "content": STORED}

    out = _run("memory", "show", "MEMORY.md")

    assert out == STORED + "\n"


def test_memory_show_does_not_wrap_a_long_line(gateway: type[_Gateway]) -> None:
    """Redirected output is not a terminal; Rich hard-wrapped it at 80 columns."""
    line = "x" * 150
    gateway.payloads["memory.show"] = {"path": "MEMORY.md", "content": line}

    result = runner.invoke(app, ["memory", "show", "MEMORY.md"])

    assert result.exit_code == 0, result.stdout
    assert result.stdout.splitlines() == [line]


def test_memory_show_keeps_its_own_truncation_label(gateway: type[_Gateway]) -> None:
    """The CLI's own markup still renders; only the stored text is taken literally."""
    gateway.payloads["memory.show"] = {"content": "- [x] done", "truncated": True}

    out = _run("memory", "show", "MEMORY.md")

    assert out.splitlines() == ["- [x] done", "... truncated"]


def test_raw_fallback_show_prints_the_receipt_verbatim(gateway: type[_Gateway]) -> None:
    gateway.payloads["memory.raw_fallbacks.show"] = {"content": STORED}

    out = _run("memory", "raw-fallbacks", "show", "memory/.raw_fallbacks/x.md")

    assert out == STORED + "\n"


def test_curated_get_prints_each_entry_verbatim(gateway: type[_Gateway]) -> None:
    entries = ["- [x] shipped the v2 migration", "prefers dict[str, int] :warning:", "a [/] b"]
    gateway.payloads["memory.curated.get"] = {"usage": "96/4000", "entries": entries}

    out = _run("memory", "curated", "get")

    assert out.splitlines() == [
        "MEMORY.md (96/4000)",
        "  1. - [x] shipped the v2 migration",
        "  2. prefers dict[str, int] :warning:",
        "  3. a [/] b",
    ]


def test_memory_search_table_shows_path_and_snippet_verbatim(gateway: type[_Gateway]) -> None:
    gateway.payloads["memory.search"] = {
        "results": [
            {
                "source": "knowledge_base",
                "path": "knowledge_base/[draft] plan.md",
                "startLine": 2,
                "endLine": 3,
                "score": 0.91,
                "snippet": "- [x] see [docs](https://x.test) :warning: [/]",
            }
        ]
    }

    out = _run("memory", "search", "plan")

    assert "knowledge_base/[draft] plan.md" in out
    assert "- [x] see [docs](https://x.test) :warning: [/]" in out


def test_memory_list_table_shows_paths_verbatim(gateway: type[_Gateway]) -> None:
    gateway.payloads["memory.list"] = {
        "files": [{"path": "memory/[wip] notes.md", "source": "memory", "lineCount": 3}]
    }

    out = _run("memory", "list")

    assert "memory/[wip] notes.md" in out


def test_raw_fallback_list_shows_path_and_reason_verbatim(gateway: type[_Gateway]) -> None:
    gateway.payloads["memory.raw_fallbacks.list"] = {
        "files": [{"path": "memory/.raw_fallbacks/[x].md", "reason": "embedder [red] down"}]
    }

    out = _run("memory", "raw-fallbacks", "list")

    assert "memory/.raw_fallbacks/[x].md" in out
    assert "embedder [red] down" in out


def test_ingest_table_shows_path_and_error_verbatim(gateway: type[_Gateway]) -> None:
    gateway.payloads["memory.knowledge_base.ingest"] = {
        "results": [
            {
                "path": "knowledge_base/[v2] spec.md",
                "status": "error",
                "error": "cannot parse [/] header",
            }
        ]
    }

    out = _run("memory", "ingest", "spec.md")

    assert "knowledge_base/[v2] spec.md" in out
    assert "cannot parse [/] header" in out


def test_memory_show_json_was_already_verbatim(gateway: type[_Gateway]) -> None:
    gateway.payloads["memory.show"] = {"path": "MEMORY.md", "content": STORED}

    out = _run("memory", "show", "MEMORY.md", "--json")

    assert json.loads(out)["content"] == STORED
