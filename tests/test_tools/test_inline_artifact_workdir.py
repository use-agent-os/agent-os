"""An inline artifact marker names a file relative to where the command ran.

``exec_command`` auto-publishes ``publish_artifact path=<file> mime=...``
markers, but handed the marker path to ``publish_artifact`` as-is, and that
tool resolves a relative path against the workspace root. A skill run with
``workdir`` writes its card relative to that directory (``chain_stocks``
defaults to ``<SYMBOL>.cards.json`` in the working directory), so the
publisher looked in the wrong place. With no file there, the card never
rendered. With an older card of the same name at the root, that *stale* card
was published and the tool reported "already rendered for the user".

These tests run real commands through ``exec_command`` and read back the bytes
the artifact store actually holds.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agentos.sandbox.config import SandboxSettings
from agentos.sandbox.integration import configure_runtime, reset_runtime
from agentos.tools.builtin.shell import exec_command
from agentos.tools.types import ToolContext, current_tool_context

CARDS_MIME = "application/vnd.agentos.cards+json"


def _card(price: str) -> str:
    return json.dumps({"type": "cards", "cards": [{"title": "AAPL", "price": price}]})


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "reports").mkdir(parents=True)
    return ws


@pytest.fixture
def ctx(workspace: Path, tmp_path: Path) -> Iterator[ToolContext]:
    configure_runtime(
        SandboxSettings(sandbox=False, security_grading=False, allow_legacy_mode=True),
        workspace=workspace,
    )
    context = ToolContext(
        workspace_dir=str(workspace),
        artifact_media_root=str(tmp_path / "media"),
        artifact_session_id="s1",
        session_key="agent:main:main",
    )
    token = current_tool_context.set(context)
    yield context
    current_tool_context.reset(token)
    reset_runtime()


def _exec() -> Any:
    fn: Any = exec_command
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _write_card_command(relative: str, price: str) -> str:
    """A command that writes a card relative to its cwd and prints the marker.

    The card JSON is built inside the child, so the command line carries only a
    path and a bare number: nothing that ``sh`` and ``cmd.exe`` quote differently.
    """
    script = (
        "import json, pathlib, sys; "
        "p = pathlib.Path(sys.argv[1]); p.parent.mkdir(parents=True, exist_ok=True); "
        "p.write_text(json.dumps({'type': 'cards', "
        "'cards': [{'title': 'AAPL', 'price': sys.argv[2]}]})); "
        f"print('publish_artifact path=' + sys.argv[1] + ' mime={CARDS_MIME}')"
    )
    return f'"{sys.executable}" -c "{script}" {relative} {price}'


def _published_prices(ctx: ToolContext, media: Path) -> list[str]:
    prices = []
    for artifact in ctx.published_artifacts:
        stored = next(
            p
            for p in media.rglob("*")
            if p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() == artifact["sha256"]
        )
        prices.append(json.loads(stored.read_text())["cards"][0]["price"])
    return prices


# --- Fail on main: the marker path was resolved against the workspace root ---


async def test_workdir_card_is_published_instead_of_a_stale_root_card(
    ctx: ToolContext, workspace: Path, tmp_path: Path
) -> None:
    (workspace / "AAPL.cards.json").write_text(_card("100"), encoding="utf-8")

    out = await _exec()(command=_write_card_command("AAPL.cards.json", "230"), workdir="reports")

    assert "already rendered for the user" in out
    assert _published_prices(ctx, tmp_path / "media") == ["230"]


async def test_workdir_card_is_published_when_the_root_has_none(
    ctx: ToolContext, tmp_path: Path
) -> None:
    out = await _exec()(command=_write_card_command("AAPL.cards.json", "230"), workdir="reports")

    assert "not published" not in out
    assert _published_prices(ctx, tmp_path / "media") == ["230"]


async def test_absolute_workdir_resolves_the_same_way(
    ctx: ToolContext, workspace: Path, tmp_path: Path
) -> None:
    (workspace / "AAPL.cards.json").write_text(_card("100"), encoding="utf-8")

    await _exec()(
        command=_write_card_command("AAPL.cards.json", "230"),
        workdir=str(workspace / "reports"),
    )

    assert _published_prices(ctx, tmp_path / "media") == ["230"]


async def test_nested_marker_path_is_taken_from_the_workdir(
    ctx: ToolContext, workspace: Path, tmp_path: Path
) -> None:
    (workspace / "cards").mkdir()
    (workspace / "cards" / "AAPL.cards.json").write_text(_card("100"), encoding="utf-8")

    await _exec()(command=_write_card_command("cards/AAPL.cards.json", "230"), workdir="reports")

    assert _published_prices(ctx, tmp_path / "media") == ["230"]


async def test_a_path_climbing_out_of_workdir_but_not_the_workspace_is_honoured(
    ctx: ToolContext, tmp_path: Path
) -> None:
    """``../x`` from ``reports`` is ``x`` at the root, which is inside the workspace."""
    out = await _exec()(command=_write_card_command("../AAPL.cards.json", "230"), workdir="reports")

    assert "outside workspace" not in out
    assert _published_prices(ctx, tmp_path / "media") == ["230"]


# --- Guards: pass on main and with the fix, by design ---------------------


async def test_without_workdir_the_root_card_is_published_as_before(
    ctx: ToolContext, tmp_path: Path
) -> None:
    """Guard: the default cwd is the workspace root, so nothing changes."""
    await _exec()(command=_write_card_command("AAPL.cards.json", "230"))

    assert _published_prices(ctx, tmp_path / "media") == ["230"]


async def test_a_path_escaping_the_workspace_is_still_refused(ctx: ToolContext) -> None:
    """Guard: containment is still ``publish_artifact``'s, whatever the cwd."""
    out = await _exec()(
        command=_write_card_command("../../outside.cards.json", "230"), workdir="reports"
    )

    assert "inline artifact not published" in out
    assert "outside workspace" in out
    assert ctx.published_artifacts == []


async def test_absolute_marker_path_is_used_as_printed(
    ctx: ToolContext, workspace: Path, tmp_path: Path
) -> None:
    """Guard: only relative paths are re-based on the cwd."""
    target = workspace / "AAPL.cards.json"

    await _exec()(command=_write_card_command(str(target), "230"), workdir="reports")

    assert _published_prices(ctx, tmp_path / "media") == ["230"]
    assert not (workspace / "reports" / "AAPL.cards.json").exists()


async def test_marker_note_still_names_the_path_the_script_printed(
    ctx: ToolContext, tmp_path: Path
) -> None:
    """Guard: the note the model reads keeps the script's own wording."""
    out = await _exec()(command=_write_card_command("AAPL.cards.json", "230"))

    assert "already rendered for the user: AAPL.cards.json." in out
