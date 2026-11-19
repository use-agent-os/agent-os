from __future__ import annotations

from pathlib import Path

from agentos.identity.bootstrap import ensure_agent_workspace
from agentos.identity.prompt import assemble_system_prompt
from agentos.identity.types import AgentProfile


def test_curated_memory_files_are_seeded_empty(tmp_path: Path) -> None:
    # MEMORY.md and USER.md are curated stores: anything seeded into them is
    # injected as remembered facts and counted against their char budgets.
    # They used to be seeded with instructions and an unfilled profile form.
    ensure_agent_workspace(tmp_path)

    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == ""
    assert (tmp_path / "USER.md").read_text(encoding="utf-8") == ""


def test_default_bootstrap_templates_define_distinct_file_roles() -> None:
    template_dir = Path("src/agentos/identity/templates/bootstrap")

    agents = (template_dir / "AGENTS.md").read_text(encoding="utf-8")
    soul = (template_dir / "SOUL.md").read_text(encoding="utf-8")
    identity = (template_dir / "IDENTITY.md").read_text(encoding="utf-8")
    tools = (template_dir / "TOOLS.md").read_text(encoding="utf-8")

    assert "operating rules" in agents
    assert "Do not store user profile facts here" in agents
    assert "voice, tone, and interaction style" in soul
    assert "Do not store user profile facts, task history, or tool inventories here" in soul
    assert "agent's public-facing name" in identity
    assert "If the user asks to rename the assistant" in identity
    assert "local tool conventions" in tools
    assert "does not register tools, grant permissions, or change tool policy" in tools
    # No template ships for the curated stores any more.
    assert not (template_dir / "MEMORY.md").exists()
    assert not (template_dir / "USER.md").exists()


def test_system_prompt_carries_the_old_curated_template_guidance() -> None:
    # What the old MEMORY.md / USER.md templates told the model must still
    # reach it, from the system prompt rather than from memory entries.
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full"),
        tools=["memory_search", "memory_get", "memory", "memory_save", "write_file"],
    )

    # What belongs in MEMORY.md, and what does not.
    assert "for every other durable fact" in prompt
    assert "preferences, decisions, and constraints" in prompt
    assert "Do not put assistant rename/persona requests into `MEMORY.md`" in prompt
    assert "`memory/YYYY-MM-DD.md` and `memory/**/*.md`" in prompt
    assert "ordinary task deliverables" in prompt
    # What belongs in USER.md.
    assert "a short profile: name, preferred address, pronouns" in prompt
    # No secrets in memory.
    assert "never paste their values into replies, code, or memory files" in prompt


def test_system_prompt_routes_profile_to_user_md() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full"),
        tools=["memory_search", "memory_get", "memory", "write_file", "edit_file", "apply_patch"],
    )

    assert "USER.md" in prompt
    assert "name, preferred address, pronouns" in prompt
    # Timezone is deliberately absent. It is injected ambient context, not
    # something the user stated, and naming it as a USER.md field taught the
    # model to record the host's zone as a fact about the user.
    assert "timezone" not in prompt.lower()
    assert "Never record the runtime environment" in prompt
    # Durable facts route through the `memory` tool, not memory_save / filesystem tools.
    assert "Durable facts go through the `memory` tool" in prompt
    assert "target` = `memory`" in prompt or "`target` = `memory`" in prompt
    assert "`target: user`" in prompt
    assert "Do not use `memory_save` for `USER.md` or `MEMORY.md`" in prompt
    assert "are managed curated stores" in prompt
    assert "Do not create or edit them with `write_file`" in prompt
    assert "decisions, dates, people, preferences, or todos" not in prompt
    assert "prior work, decisions, dated history, todos" in prompt


def test_system_prompt_disambiguates_session_memory_results() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full"),
        tools=["memory_search", "memory_get"],
    )

    assert "By default, `memory_search` searches curated memory source files" in prompt
    assert "source=sessions" in prompt
    assert "source=all" in prompt
    assert "raw turn captures or raw fallback files" in prompt
    assert "For `source: memory` results, use `memory_get`" in prompt
    assert "For `source: sessions` results, use the returned snippet" in prompt
    assert "`sessions/...` paths are virtual index sources" in prompt
    assert "Prefer curated `MEMORY.md`/`memory/**/*.md` facts" in prompt
    assert "not automatically as current truth" in prompt
    assert "include the returned citation or path#line" in prompt
    assert "Do not invent citations" in prompt


def test_system_prompt_routes_exact_transcript_search_to_session_search() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full"),
        tools=["memory_search", "memory_get", "session_search"],
    )

    assert "`session_search`" in prompt
    assert "exact prior chat wording" in prompt
    assert "transcript context" in prompt
    assert "code snippets from persisted sessions" in prompt
    assert "Ordinary recall should start with default curated `memory_search`" in prompt
    assert "debug" not in prompt.lower()


def test_system_prompt_routes_agent_identity_away_from_memory_md() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full"),
        tools=["memory_search", "memory_get", "write_file", "edit_file", "apply_patch"],
    )

    assert "Agent identity: `IDENTITY.md`" in prompt
    assert "Agent persona: `SOUL.md`" in prompt
    assert "Do not put assistant rename/persona requests into `MEMORY.md`" in prompt


def test_system_prompt_only_documents_canonical_tool_names() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full"),
        tools=["image_generate", "sessions_spawn", "sessions_send", "subagents"],
    )

    assert "`image_generate`" in prompt
    assert "generate_image" not in prompt
    assert "spawn_subagent" not in prompt
    assert "send_message" not in prompt


def test_system_prompt_disambiguates_session_send_from_channel_message() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full"),
        tools=["sessions_send", "message"],
    )

    assert "agent-to-agent or session-to-session" in prompt
    assert "`sessions_send`" in prompt
    assert "`message` only for channel adapter delivery" in prompt
    assert "send_message" not in prompt


def test_system_prompt_guides_generated_file_delivery() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full"),
        tools=["execute_code", "publish_artifact", "image_generate"],
    )

    assert "## Generated File Delivery" in prompt
    assert "Do not paste full file source" in prompt
    assert "call `publish_artifact` for the final file" in prompt
    assert "local entry path" in prompt
    assert "Do not invent artifact download URLs" in prompt
    assert "do not call `publish_artifact` again" in prompt
    assert "After `publish_artifact` succeeds" in prompt
    assert "final response" in prompt


def test_system_prompt_limits_file_delivery_when_no_file_authoring_tools() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full"),
        tools=["publish_artifact", "image_generate", "read_file", "glob_search"],
    )

    assert "## Generated File Delivery Limits" in prompt
    assert "already exists in the workspace" in prompt
    assert "file creation is not enabled for this session" in prompt
    assert "surface where file authoring is enabled" in prompt
    assert "Do not paste full file source" in prompt
    assert "create the file in the active workspace" not in prompt


def test_system_prompt_describes_structured_artifact_fallback_limits() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full"),
        tools=["publish_artifact", "create_pptx", "image_generate"],
    )

    assert "## Structured Generated File Delivery" in prompt
    assert "only when the request fits the tool schema" in prompt
    assert "`create_pptx` creates a basic text-only deck" in prompt
    assert "create, send, deliver, or attach" in prompt
    assert "call `create_pptx`" in prompt
    assert "Do not substitute a PDF, CSV, XLSX, Python script, OOXML" in prompt
    assert "full visual deck authoring is not enabled" in prompt
    assert "file creation is not enabled for this session" not in prompt


def test_legacy_image_alias_does_not_enable_image_generation_prompt() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full"),
        tools=["generate_image"],
    )

    assert "MUST call the `image_generate` tool" not in prompt
    assert "Image generation is not available in this session" in prompt


def test_template_no_longer_renders_duplicate_skills_section() -> None:
    prompt = assemble_system_prompt(
        AgentProfile(agent_id="main", prompt_mode="full"),
        tools=["memory_search"],
        skills=["memory"],
    )

    assert "## Skills (mandatory)" not in prompt
    assert "Available skills:" not in prompt
