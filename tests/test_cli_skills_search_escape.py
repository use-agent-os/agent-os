"""CLI `skills search` must not interpret rich markup from remote descriptions."""

from __future__ import annotations

from typing import Any

from typer.testing import CliRunner

from agentos.cli.skills_cmd import skills_app
from agentos.skills.hub.source import SkillMeta


class _StubRouter:
    async def search(
        self, query: str, limit: int = 20, source_id: str | None = None
    ) -> list[SkillMeta]:
        return [
            SkillMeta(
                name="evil",
                description="[red]X[/red] spoof",
                source_id="bankr",
                trust_level="community",
            )
        ]


def test_search_table_renders_remote_markup_literally(monkeypatch: Any) -> None:
    from agentos.skills.hub import defaults

    monkeypatch.setattr(defaults, "get_default_skill_router", lambda: _StubRouter())

    runner = CliRunner()
    result = runner.invoke(skills_app, ["search", "evil"], env={"COLUMNS": "200"})

    assert result.exit_code == 0
    # Escaped markup renders as literal text; unescaped markup would be
    # consumed by rich and "[red]" would not appear in the output.
    assert "[red]X[/red]" in result.output
