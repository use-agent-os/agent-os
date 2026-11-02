"""Every surface answers "what is this skill" from the one inventory builder.

``skills.list``, ``skills.status``, ``skills.get``, ``agentos skills list`` and
the agent's own ``skill_list`` each used to assemble a row by hand, and only one
of them read the lockfile. The result was three different answers to the same
question about the same skill on the same machine. These tests pin the join.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentos.cli import skills_cmd
from agentos.gateway import rpc_skills
from agentos.gateway.rpc import RpcContext
from agentos.skills.loader import SkillLoader
from agentos.tools.builtin import skill_tools as skill_tools_module
from agentos.tools.registry import get_default_registry

SKILL_NAME = "ledger-watch"
IDENTIFIER = "https://github.com/BankrBot/skills/tree/main/ledger-watch"


def _write_skill(managed_dir: Path) -> None:
    skill_dir = managed_dir / SKILL_NAME
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {SKILL_NAME}\n"
        "description: Watch a ledger address.\n"
        "publisher:\n"
        "  id: bankr\n"
        "---\n"
        "Body.\n",
        encoding="utf-8",
    )


def _write_lockfile(path: Path, managed_dir: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "installed": {
                    SKILL_NAME: {
                        "source": "bankr",
                        "identifier": IDENTIFIER,
                        "version": "1.2.0",
                        # Pinned UTC instant — never the system clock.
                        "installed_at": "2026-01-01T00:00:00Z",
                        "path": str(managed_dir / SKILL_NAME),
                        "source_trust": "trusted",
                        "scan_verdict": "safe",
                        "publisher_id": "bankr",
                    }
                },
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def hub_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SkillLoader]:
    """One hub-installed skill, visible to every surface through real paths."""
    state = tmp_path / "state"
    managed = state / "skills"
    _write_skill(managed)
    _write_lockfile(state / "skills-lock.json", managed)

    monkeypatch.setenv("AGENTOS_STATE_DIR", str(state))
    monkeypatch.setenv("AGENTOS_SKILLS_MANAGED_DIR", str(managed))
    # Keep the sweep to the one skill under test: the bundled layer is 40 more.
    monkeypatch.setenv("AGENTOS_SKILLS_ALLOW_BUNDLED", "false")
    monkeypatch.delenv("AGENTOS_GATEWAY_CONFIG_PATH", raising=False)

    loader = SkillLoader(managed_dir=managed, snapshot_path=tmp_path / "snapshot.json")
    previous_loader = skill_tools_module._loader
    skill_tools_module.create_skill_tools(loader)
    try:
        yield loader
    finally:
        skill_tools_module._loader = previous_loader


async def _skill_list_text() -> str:
    registered = get_default_registry().get("skill_list")
    assert registered is not None
    return str(await registered.handler())


def _row(rows: list[dict], name: str = SKILL_NAME) -> dict:
    return next(row for row in rows if row["name"] == name)


def test_the_gateway_surfaces_agree_on_one_skill(hub_install: SkillLoader) -> None:
    async def run() -> None:
        ctx = RpcContext(conn_id="test", skill_loader=hub_install)

        listed = _row((await rpc_skills._handle_skills_list(None, ctx))["skills"])
        status = _row(await rpc_skills._handle_skills_status(None, ctx))
        got = await rpc_skills._handle_skills_get({"name": SKILL_NAME}, ctx)

        # ``skills.get`` adds the body; everything else must be identical.
        body_only = {"content", "base_dir"}
        assert {k: v for k, v in got.items() if k not in body_only} == listed
        assert status == listed

        assert listed["publisher"] == {
            "id": "bankr",
            "name": "Bankr",
            "url": "https://github.com/BankrBot/skills",
            "logo": "",
        }
        assert listed["acquisition"]["kind"] == "hub"
        assert listed["acquisition"]["source_id"] == "bankr"
        assert listed["acquisition"]["identifier"] == IDENTIFIER
        assert listed["acquisition"]["version"] == "1.2.0"
        assert listed["acquisition"]["installed_at"] == "2026-01-01T00:00:00Z"
        assert listed["acquisition"]["source_trust"] == "trusted"
        assert listed["acquisition"]["scan_verdict"] == "safe"
        assert listed["availability"] == {"offered": True, "reason": "", "detail": ""}
        # ``layer`` keeps meaning location, not provenance.
        assert listed["layer"] == "managed"

    asyncio.run(run())


def test_the_cli_and_the_agent_report_the_same_acquisition_as_the_gateway(
    hub_install: SkillLoader,
) -> None:
    async def run() -> None:
        ctx = RpcContext(conn_id="test", skill_loader=hub_install)
        listed = _row((await rpc_skills._handle_skills_list(None, ctx))["skills"])

        cli = _row(skills_cmd._load_skill_rows())
        assert cli["publisher"] == listed["publisher"]
        assert cli["acquisition"] == listed["acquisition"]
        assert cli["eligible"] == listed["eligible"]
        assert cli["description"] == listed["description"]
        assert cli["layer"] == listed["layer"]
        # The CLI has no chat session, so it reports no availability rather
        # than inventing one.
        assert "availability" not in cli

        agent_text = await _skill_list_text()
        assert f"- {SKILL_NAME}: Watch a ledger address." in agent_text
        assert "[not offered]" not in agent_text

    asyncio.run(run())


def test_a_hub_install_is_removable_and_a_local_skill_is_not(
    hub_install: SkillLoader,
) -> None:
    """Only a lockfile entry pointing at the managed dir earns Remove/Update."""

    async def run() -> None:
        assert hub_install.managed_dir is not None
        hand_copied = hub_install.managed_dir / "hand-copied"
        hand_copied.mkdir()
        (hand_copied / "SKILL.md").write_text(
            "---\nname: hand-copied\ndescription: Copied by hand.\n---\nBody.\n",
            encoding="utf-8",
        )
        hub_install.invalidate_cache()

        ctx = RpcContext(conn_id="test", skill_loader=hub_install)
        rows = (await rpc_skills._handle_skills_list(None, ctx))["skills"]

        hub = _row(rows)["acquisition"]
        assert (hub["removable"], hub["updatable"]) == (True, True)

        local_row = _row(rows, "hand-copied")
        local = local_row["acquisition"]
        assert local["kind"] == "local"
        assert (local["removable"], local["updatable"]) == (False, False)
        assert local_row["publisher"]["id"] == ""

    asyncio.run(run())


def test_the_agent_is_told_which_listed_skills_it_will_never_be_offered(
    hub_install: SkillLoader,
) -> None:
    """``skill_list`` used to advertise skills the agent can never invoke.

    It listed every loaded skill with no hint that five of the bundled ones set
    ``disable-model-invocation``, so the agent would announce a capability, try
    to use it, and find nothing."""

    async def run() -> None:
        assert hub_install.managed_dir is not None
        user_only = hub_install.managed_dir / "receipts"
        user_only.mkdir()
        (user_only / "SKILL.md").write_text(
            "---\n"
            "name: receipts\n"
            "description: Print receipts.\n"
            "disable-model-invocation: true\n"
            "---\n"
            "Body.\n",
            encoding="utf-8",
        )
        hub_install.invalidate_cache()

        text = await _skill_list_text()

        assert "  - receipts: Print receipts." in text
        assert "[not offered]" in text
        assert "disable-model-invocation" in text
        # The offered skill keeps its plain one-liner.
        assert f"  - {SKILL_NAME}: Watch a ledger address.\n" in text + "\n"

    asyncio.run(run())


def test_a_bundled_skill_is_never_removable() -> None:
    """Shipped skills live in the wheel; offering Remove would be a dead button."""

    async def run() -> None:
        bundled = Path(__file__).resolve().parent.parent / "src" / "agentos" / "skills" / "bundled"
        loader = SkillLoader(bundled_dir=bundled)
        ctx = RpcContext(conn_id="test", skill_loader=loader)
        rows = (await rpc_skills._handle_skills_list(None, ctx))["skills"]

        acquisitions = [row["acquisition"] for row in rows]
        assert acquisitions, "the bundled layer should not be empty"
        assert {a["kind"] for a in acquisitions} == {"shipped"}
        assert not any(a["removable"] or a["updatable"] for a in acquisitions)

        # Branding reaches the wire, and only through the allowlist: every
        # branded row carries the allowlisted record for its publisher, and no
        # other bundled skill carries one.
        branded = {row["name"]: row["publisher"]["name"] for row in rows if row["publisher"]["id"]}
        assert branded == {
            "musebook": "Muse",
            "robinhood-agentic-trading": "Robinhood",
            "robinhood-chain-stocks": "Robinhood",
            "robinhood-rwa-addresses": "Robinhood",
        }

    asyncio.run(run())


def test_disabling_tools_is_reflected_on_the_skills_page(hub_install: SkillLoader) -> None:
    """With ``[tools] enabled = false`` the page must not claim a tool-gated skill works.

    The turn narrows to no tools at all, so a ``requires_tools`` skill is
    withheld from the agent. Answering the row against the full process registry
    would show it as offered — the Skills-page-versus-chat disagreement this
    change exists to remove.
    """
    skill_dir = hub_install.managed_dir / "needs-a-tool" if hub_install.managed_dir else None
    assert skill_dir is not None
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: needs-a-tool\n"
        "description: Needs a tool.\n"
        "metadata:\n"
        "  agentos:\n"
        "    requires_tools: [web_search]\n"
        "---\n"
        "Body.\n",
        encoding="utf-8",
    )
    hub_install.invalidate_cache()

    class _Tools:
        enabled = False

    class _Config:
        tools = _Tools()

    async def run() -> None:
        enabled_ctx = RpcContext(conn_id="test", skill_loader=hub_install)
        disabled_ctx = RpcContext(conn_id="test", skill_loader=hub_install, config=_Config())

        enabled = _row(
            (await rpc_skills._handle_skills_list(None, enabled_ctx))["skills"], "needs-a-tool"
        )
        disabled = _row(
            (await rpc_skills._handle_skills_list(None, disabled_ctx))["skills"], "needs-a-tool"
        )

        assert enabled["availability"]["offered"] is True
        assert disabled["availability"]["offered"] is False
        assert disabled["availability"]["reason"] == "tool_gate"
        assert disabled["availability"]["detail"]

    asyncio.run(run())
