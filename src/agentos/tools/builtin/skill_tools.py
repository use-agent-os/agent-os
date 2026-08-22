"""Skill tools — agent-accessible skill discovery, viewing, and management.

Registered at boot time when a SkillLoader is available.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from agentos.result_budget import register_persisted_result_budget
from agentos.skills.hub.defaults import (
    build_default_skill_installer,
    get_default_skill_router,
    installed_skill_identifiers,
    installed_skill_names,
)
from agentos.skills.install_kinds import (
    MANUAL_INSTALL_KINDS,
    InstallSpecError,
    build_install_argv,
    normalize_install_kind,
)
from agentos.skills.types import SkillInstallSpec, SkillLayer
from agentos.tools.registry import tool
from agentos.tools.types import ToolError

if TYPE_CHECKING:
    from agentos.skills.loader import SkillLoader

logger = structlog.get_logger(__name__)

# Module-level reference set at boot
_loader: SkillLoader | None = None

# Layers that user may mutate — workspace only
_MUTABLE_LAYERS = frozenset({SkillLayer.WORKSPACE})

# Valid skill name pattern: lowercase alphanumeric + hyphens
_SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9\-]{0,62}$")
_INSTALL_OUTPUT_LIMIT = 4_000
# Room after the view ceiling for the outline index and the trailing notes.
_SKILL_VIEW_PERSIST_HEADROOM = 4_000
_INSTALL_TIMEOUT_SECONDS = 120.0


def _sanitize_yaml_value(value: str) -> str:
    """Strip characters that could inject YAML structure."""
    return value.replace("\n", " ").replace("\r", " ").strip()


def _render_skill_md(
    name: str,
    description: str,
    content: str,
    triggers: list[str] | None = None,
) -> str:
    """Render a SKILL.md file from parts."""
    safe_desc = _sanitize_yaml_value(description)
    lines = ["---", f"name: {name}", f"description: {safe_desc}"]
    if triggers:
        lines.append("triggers:")
        for t in triggers:
            lines.append(f"  - {_sanitize_yaml_value(t)}")
    lines.append("---")
    lines.append("")
    lines.append(content)
    return "\n".join(lines)


def _cap_output(value: bytes | str, limit: int = _INSTALL_OUTPUT_LIMIT) -> str:
    if isinstance(value, bytes):
        text = value.decode(errors="replace")
    else:
        text = value
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return f"{text[:limit]}\n... truncated {omitted} characters"


def _argv_for_install_spec(spec: SkillInstallSpec) -> list[str]:
    """The command for an install spec, or a ToolError explaining why not.

    The command itself comes from :mod:`agentos.skills.install_kinds`, the same
    builder the Skills page runs and renders.
    """
    kind = normalize_install_kind(spec.kind)
    if kind in MANUAL_INSTALL_KINDS:
        raise ToolError(f"Install kind '{kind}' needs elevated privileges and cannot be run here")
    try:
        return build_install_argv(spec)
    except InstallSpecError as exc:
        raise ToolError(str(exc)) from exc


def _find_install_spec(skill_name: str, install_id: str) -> SkillInstallSpec:
    if install_id.startswith("-"):
        raise ToolError(f"Unsafe install value for install_id: {install_id}")
    if _loader is None:
        raise ToolError("Skill loader not available")

    skill = _loader.get_by_name(skill_name)
    if skill is None:
        raise ToolError(f"Skill not found: {skill_name}")
    if skill.metadata is None or not skill.metadata.install:
        raise ToolError(f"Skill has no install metadata: {skill_name}")

    for index, spec in enumerate(skill.metadata.install):
        fallback_id = f"{spec.kind}-{index}"
        if spec.id == install_id or (not spec.id and install_id == fallback_id):
            return spec
    raise ToolError(f"Install spec not found for skill '{skill_name}': {install_id}")


def _community_result_to_dict(
    row: Any,
    installed: set[str],
    installed_identifiers: set[str] | None = None,
) -> dict[str, Any]:
    # Names are matched against installed names and identifiers against
    # installed identifiers — never across the two, which would flag a catalog
    # row whose name happens to equal a different skill's identifier.
    identifier = getattr(row, "identifier", "") or getattr(row, "name", "")
    name = getattr(row, "name", "")
    identifiers = installed_identifiers if installed_identifiers is not None else installed
    return {
        "name": name,
        "description": getattr(row, "description", ""),
        "version": getattr(row, "version", ""),
        "author": getattr(row, "author", ""),
        "source": getattr(row, "source_id", ""),
        "trust_level": getattr(row, "trust_level", ""),
        "identifier": identifier,
        "provider": getattr(row, "provider", ""),
        "category": getattr(row, "category", ""),
        "homepage": getattr(row, "homepage", ""),
        "installed": identifier in identifiers or name in installed,
    }


def _local_match(loader: SkillLoader, query: str) -> dict[str, Any] | None:
    """What an installed skill named like ``query`` already offers, or ``None``.

    A skill that declares ``requires`` disappears from the prompt until its
    binary and variables are present, so from the model's side an unconfigured
    bundled skill and a skill that was never installed look identical. Asked to
    install one, it goes shopping on a hub — and a same-named catalog row
    installs over it, because managed outranks bundled. Answering the search
    with the local truth is what stops that, so this runs before the network
    call and its result is emitted ahead of the results list.
    """
    from agentos.skills.inventory import build_skill_inventory
    from agentos.tools.registry import get_default_registry

    wanted = query.strip().casefold()
    if not wanted:
        return None
    try:
        rows = build_skill_inventory(
            loader,
            available_tools=set(get_default_registry().list_names()),
        )
    except Exception:  # pragma: no cover — a hint is never worth failing the search on
        logger.debug("skill_search_community.local_probe_failed", exc_info=True)
        return None

    row = next((r for r in rows if r.spec.name.casefold() == wanted), None)
    if row is None:
        near = [r.spec.name for r in rows if wanted in r.spec.name.casefold()][:3]
        return {"similar_installed": near} if near else None

    report = row.eligibility
    payload: dict[str, Any] = {
        "name": row.spec.name,
        "layer": str(row.spec.layer),
        "eligible": report.eligible,
    }
    if report.eligible:
        payload["note"] = (
            f"'{row.spec.name}' is already installed and ready to use. Read it with "
            f"skill_view instead of installing anything."
        )
        return payload

    missing = [f"{b} (binary)" for b in report.missing_bins]
    missing += [f"{e} (env var)" for e in report.missing_env]
    if missing:
        payload["missing"] = missing
    if report.install_hints:
        payload["install_hints"] = [hint.command for hint in report.install_hints]
    if report.missing_env_detail:
        payload["needs_env"] = [
            {"name": d.name, "description": d.description, "url": d.url}
            for d in report.missing_env_detail
        ]
    payload["note"] = (
        f"'{row.spec.name}' is already installed at the {row.spec.layer} layer — it is "
        f"not missing, it is unconfigured. Fix what 'missing' lists rather than "
        f"installing a hub skill. A hub skill of the same name lands in the managed "
        f"layer, which outranks this one, and would replace it for every session."
    )
    return payload


async def _run_install_argv(argv: list[str]) -> tuple[int, str, str, bool]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ToolError(f"Install command not found: {argv[0]}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=_INSTALL_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return -1, "", "Timed out", True
    return proc.returncode or 0, _cap_output(stdout), _cap_output(stderr), False


def _setup_surface() -> str:
    """Return where this call is coming from: ``channel``, ``unattended``, or ``interactive``.

    A missing credential is answerable in very different ways depending on who
    is on the other end, and the tool is the only place that knows.
    """
    from agentos.tools.types import CallerKind, InteractionMode, current_tool_context

    ctx = current_tool_context.get()
    if ctx is None:
        return "interactive"
    if ctx.caller_kind is CallerKind.CHANNEL:
        return "channel"
    if ctx.interaction_mode is InteractionMode.UNATTENDED:
        return "unattended"
    return "interactive"


def _import_offer(name: str) -> str:
    """Return an import suggestion when the credential already exists elsewhere."""
    try:
        from agentos import credential_sources

        source = credential_sources.available_for(name)
    except Exception:  # pragma: no cover - discovery must never break a skill load
        return ""
    if source is None:
        return ""
    return f"{name} is already available from {source.label} — `agentos env import {name}`."


def _register_skill_env_passthrough(skill: Any) -> None:
    """Let a viewed skill's declared variables reach sandboxed child processes.

    ``execute_code`` forwards almost nothing by default, so a skill that wraps
    a third-party API could not read its own key there. Declaring it under
    ``metadata.requires.env`` is the supported alternative to the pattern this
    replaces — pasting the key into the command, or writing it to a file and
    running that. AgentOS's own provider credentials are refused at
    registration, so a skill cannot use this to reach them.
    """
    meta = getattr(skill, "metadata", None)
    requires = getattr(meta, "requires", None) if meta is not None else None
    declared = getattr(requires, "env", None) or []
    names = [getattr(item, "name", "") or str(item) for item in declared]
    if not names:
        return
    try:
        from agentos.tools.env_passthrough import register_env_passthrough

        # Bundled skills ship in the wheel, so their declaration is AgentOS's
        # own; anything installed from a hub is not, and cannot name a
        # credential the runtime authenticates with.
        trusted = getattr(skill, "layer", None) == SkillLayer.BUNDLED
        refused = register_env_passthrough(names, trusted=trusted)
    except Exception:  # pragma: no cover - passthrough must never break a read
        logger.debug(
            "skill_env_passthrough_failed", skill=getattr(skill, "name", ""), exc_info=True
        )
        return
    if refused:
        logger.warning(
            "skill_env_passthrough_refused",
            skill=getattr(skill, "name", ""),
            names=refused,
        )


def _skill_setup_note(skill: Any) -> str:
    """Return a ``[Skill setup note: ...]`` block when a requirement is unmet.

    The skill still loads. An agent that knows what is missing can do the parts
    that work and say plainly which parts cannot — more useful than refusing to
    open the skill at all.

    What the note tells it to do depends on who is listening. A secret cannot
    be collected over Telegram without landing in a chat log, and an unattended
    cron run has nobody to collect it from; in both cases promising an action
    would be a lie.
    """
    from agentos.skills.eligibility import EligibilityContext, diagnose_eligibility

    report = diagnose_eligibility(skill, EligibilityContext.auto())
    if report.eligible:
        return ""

    lines: list[str] = []
    for declared in report.missing_env_detail:
        detail = f" — {declared.description}" if declared.description else ""
        where = f" Obtain one at {declared.url}." if declared.url else ""
        lines.append(f"{declared.name} is not set{detail}.{where}")
        offer = _import_offer(declared.name)
        if offer:
            lines.append(offer)
    for binary in report.missing_bins:
        lines.append(f"{binary} is not installed.")
    if not lines:
        # Ineligible for a reason the operator cannot act on here (wrong OS,
        # explicitly disabled). Saying nothing beats inventing an instruction.
        return ""

    surface = _setup_surface()
    if surface == "channel":
        lines.append(
            "This is a chat channel, so a secret cannot be entered here — it would "
            "be stored in the conversation. Ask the operator to set it from the "
            "AgentOS Environment screen or with `agentos env set <NAME>`."
        )
    elif surface == "unattended":
        lines.append(
            "This run is unattended, so nothing can be entered now. Continue with "
            "what works and state clearly which parts do not."
        )
    else:
        lines.append(
            "Ask the user to set it from the AgentOS Environment screen or with "
            "`agentos env set <NAME>`, then retry. Continue meanwhile with "
            "whatever does not depend on it, and say what will not work."
        )
    body = " ".join(lines)
    return f"\n\n[Skill setup note: {body}]"


def _skill_dir_note(skill: Any) -> str:
    """Return a leading ``[Skill directory: ...]`` line, or ``""``.

    A skill body says where its own scripts are; nothing else in a session
    does. Without this the model reads ``python3 .../scripts/lp_read.py``,
    looks for that path under the workspace, finds nothing, and concludes the
    skill is not installed — so state the directory outright, whether or not
    the body happened to name it.
    """
    base_dir = str(getattr(skill, "base_dir", "") or "")
    if not base_dir:
        return ""
    return f"[Skill directory: {base_dir}]\n\n"


def _skill_config_block(skill: Any) -> str:
    """Return the skill's configured settings as a trailing block, or ``""``.

    Reads the gateway config the control tools already hold rather than
    loading it again. Any failure yields an empty string: a missing config
    block is a smaller problem than a skill that will not open.
    """
    try:
        from agentos.skills.config_vars import render_skill_config_block
        from agentos.tools.builtin.control import _gateway_config

        if _gateway_config is None:
            return ""
        return render_skill_config_block(
            skill,
            _gateway_config,
            config_path=str(getattr(_gateway_config, "config_path", "") or ""),
        )
    except Exception:  # pragma: no cover - never block reading a skill
        logger.debug("skill_view.config_block_failed", exc_info=True)
        return ""


def create_skill_tools(loader: SkillLoader) -> None:
    """Register skill tools (list, view, create, edit, delete) with the global registry."""
    global _loader
    _loader = loader

    @tool(
        name="skill_list",
        description="List all available skills with name, description, and eligibility.",
    )
    async def skill_list() -> str:
        if _loader is None:
            return "No skill loader available."

        from agentos.skills.availability import REASON_INELIGIBLE
        from agentos.skills.inventory import build_skill_inventory
        from agentos.tools.registry import get_default_registry

        # Same builder the gateway and the CLI render from, so the agent can no
        # longer believe something about a skill that the Skills page denies.
        rows = build_skill_inventory(
            _loader,
            available_tools=set(get_default_registry().list_names()),
        )
        if not rows:
            return "No skills installed."

        lines = [f"Available skills ({len(rows)}):"]
        for row in sorted(rows, key=lambda r: r.spec.name):
            s = row.spec
            report = row.eligibility
            lines.append(f"  - {s.name}: {s.description}")
            availability = row.availability
            if (
                availability is not None
                and not availability.offered
                # Ineligibility already has a richer rendering below; the
                # reasons worth adding are the ones nothing else reports —
                # model invocation switched off, a missing tool, a native
                # tool superseding a fallback skill.
                and availability.reason != REASON_INELIGIBLE
            ):
                lines.append(f"      [not offered] {availability.detail}")
            if not report.eligible:
                missing = []
                for b in report.missing_bins:
                    missing.append(f"{b} (binary)")
                for e in report.missing_env:
                    missing.append(f"{e} (env var)")
                if report.disabled:
                    missing.append("disabled")
                if report.wrong_os:
                    missing.append("wrong OS")
                if missing:
                    lines.append(f"      [unavailable] Missing: {', '.join(missing)}")
                for hint in report.install_hints:
                    lines.append(f"      Install: {hint.command}")
                for declared in report.missing_env_detail:
                    # What the variable is for and where a value comes from.
                    # How to set it belongs in skill_view, where the agent is
                    # actually trying to use the skill — a listing of fifty
                    # skills does not need fifty sets of instructions.
                    detail = f" — {declared.description}" if declared.description else ""
                    source = f" (get one at {declared.url})" if declared.url else ""
                    lines.append(f"      Needs {declared.name}{detail}{source}")
        return "\n".join(lines)

    def _view_budget() -> int:
        """The character ceiling for one ``skill_view`` body, 0 when disabled.

        Read off the gateway config the control tools already hold, the same way
        the config block is. A missing config means the default rather than an
        unbounded read: the ceiling exists to stop a 87k-character skill from
        landing in one tool result, and a boot ordering detail should not be
        what turns that off.
        """
        from agentos.skills.outline import DEFAULT_MAX_SKILL_VIEW_CHARS

        try:
            from agentos.tools.builtin.control import _gateway_config

            configured = getattr(
                getattr(_gateway_config, "skills", None),
                "max_skill_view_chars",
                None,
            )
            if isinstance(configured, int) and configured >= 0:
                return configured
        except Exception:  # pragma: no cover — never block reading a skill
            logger.debug("skill_view.budget_lookup_failed", exc_info=True)
        return DEFAULT_MAX_SKILL_VIEW_CHARS

    def _view_persist_budget() -> int:
        """How much of a `skill_view` result is kept in the transcript.

        A skill body is not output, it is instructions, and the turn that acts
        on them is usually not the turn that read them. Persisting only the
        opening leaves every later turn holding a skill it half-remembers, which
        is worse than not having read it, because it does not know that.

        The view ceiling bounds the body; the outline index, the directory note
        and the configured-settings block are appended after it. Measured over
        the whole bundled tree at a 10,000 ceiling, the widest view runs 2,696
        characters past it (`senior-unilp-manager`), and none reaches the
        headroom below — which is what it is for, rather than a round number.
        A ceiling of 0 means the operator turned the read cap off, and
        persistence follows rather than re-imposing one.
        """

        budget = _view_budget()
        if budget <= 0:
            return 0
        return budget + _SKILL_VIEW_PERSIST_HEADROOM

    register_persisted_result_budget("skill_view", _view_persist_budget)

    def _linked_files(skill: Any) -> list[str]:
        """Supporting files, relative to the skill directory, POSIX-style.

        Always forward slashes, including on Windows. These paths are handed to
        the model to quote back as ``file_path``, where a backslash is an escape
        character in the tool call's JSON, and they have to match how a SKILL.md
        writes its own links.
        """
        try:
            base = Path(skill.base_dir)
            from agentos.skills.resources import SkillResources

            resources = SkillResources(base)
            found = [
                *resources.list_references(),
                *resources.list_scripts(),
                *resources.list_assets(),
            ]
            return [path.relative_to(base).as_posix() for path in found]
        except Exception:  # pragma: no cover — a listing is never worth failing on
            logger.debug("skill_view.linked_files_failed", exc_info=True)
            return []

    def _skill_body_within_budget(skill: Any, raw: str) -> str:
        """Return the body whole, or its opening plus an index of the rest."""
        from agentos.skills.outline import (
            always_sections,
            head_sections,
            parse_sections,
            render_linked_files,
            render_outline,
            render_pinned,
            within_always_budget,
        )

        budget = _view_budget()
        if budget <= 0 or len(raw) <= budget:
            return raw

        sections = parse_sections(raw)
        if not sections:
            # Nothing to index means nothing to ask for afterwards, and a body
            # cut off with no way back is worse than an expensive one. Hand it
            # over whole and say so in the log.
            logger.info(
                "skill_view.oversized_without_headings",
                skill=skill.name,
                chars=len(raw),
                budget=budget,
            )
            return raw

        # Pinned sections are taken out of the budget before the head, so a
        # rule the skill marked as one survives no matter how far down it sits.
        pinned = within_always_budget(always_sections(raw, sections), budget)
        head, shown_through = head_sections(
            raw, sections, budget - sum(section.size for section in pinned)
        )
        # One the head already reached needs no second copy. The head keeps the
        # reduced budget rather than being recomputed against the freed space:
        # the cost is a slightly shorter opening, and a single pass is easier to
        # reason about than a loop that re-decides what it has already shown.
        pinned = [section for section in pinned if section.end > shown_through]
        outline = render_outline(
            sections,
            shown_through=shown_through,
            skill_name=skill.name,
            also_shown=pinned,
        )
        linked = render_linked_files(_linked_files(skill), skill.name)
        parts = [
            head,
            "---",
            (
                f"`{skill.name}` is {len(raw):,} characters; the {shown_through:,} above are "
                "its opening sections. The rest is indexed below — read only what the task "
                "needs rather than the whole skill."
            ),
            render_pinned(raw, pinned),
            outline,
        ]
        if linked:
            parts.append(linked)
        outlined = "\n\n".join(part for part in parts if part)

        # A body only a little over the ceiling costs more to index than to
        # send: the head is nearly the whole thing and the index is pure
        # addition. Never hand back more than the skill.
        if len(outlined) >= len(raw):
            return raw

        # INFO, not DEBUG: this is the event that says most of a skill did not
        # reach the model. A rule written into the indexed tail is invisible
        # until someone asks for it, and nothing else reports that.
        logger.info(
            "skill_view.outlined",
            skill=skill.name,
            chars=len(raw),
            returned=len(outlined),
            budget=budget,
            pinned=len(pinned),
        )
        return outlined

    def _skill_section(skill: Any, raw: str, wanted: str) -> str:
        """Return one section of a skill body, addressed by heading title."""
        from agentos.skills.outline import (
            Section,
            find_section,
            indexable,
            parse_sections,
            render_outline,
        )

        sections = parse_sections(raw)
        if not sections:
            return (
                f"Skill '{skill.name}' has no sections to address. "
                "Call skill_view without section to read it."
            )

        match = find_section(sections, wanted)
        if match is None:
            listed = ", ".join(f'"{s.path}"' for s in indexable(sections))
            return f"No section '{wanted}' in skill '{skill.name}'. Sections: {listed}"
        if isinstance(match, list):
            listed = ", ".join(f'"{s.path}"' for s in match)
            return (
                f"'{wanted}' matches more than one section in skill '{skill.name}'. "
                f"Ask for one by its full path: {listed}"
            )

        text = raw[match.start : match.end].rstrip()
        budget = _view_budget()
        if budget <= 0 or len(text) <= budget:
            return text

        # The section is itself over budget. Index its children the same way the
        # whole body is indexed, so there is always a next step to take.
        from agentos.skills.outline import head_sections

        children = [
            Section(
                level=child.level,
                title=child.title,
                start=child.start - match.start,
                end=child.end - match.start,
                ancestors=child.ancestors,
            )
            for child in sections
            if match.start < child.start < match.end
        ]
        if not children:
            # A leaf section with no subheadings: there is nothing finer to
            # offer, so return it whole rather than cutting it into a dead end.
            return text

        head, shown_through = head_sections(text, children, budget)
        outline = render_outline(children, shown_through=shown_through, skill_name=skill.name)
        note = (
            f"Section '{match.title}' of `{skill.name}` is {len(text):,} characters; "
            f"the {shown_through:,} above are its opening."
        )
        return "\n\n".join(part for part in (head, "---", note, outline) if part)

    def _session_has_tool(tool_name: str) -> bool:
        """Whether this session can actually call ``tool_name`` right now.

        Registration is not availability: a cron turn runs under an allowlist,
        and an operator can deny a tool. Naming one the caller cannot reach
        turns a useful next step into an instruction it fails to follow.
        """
        try:
            from agentos.tools.registry import get_default_registry
            from agentos.tools.types import current_tool_context
            from agentos.tools.visibility import is_tool_visible

            registered = get_default_registry().get(tool_name)
            if registered is None:
                return False
            return is_tool_visible(registered, current_tool_context.get())
        except Exception:  # pragma: no cover — a hint is never worth failing on
            logger.debug("skill_view.tool_probe_failed", tool=tool_name, exc_info=True)
            return False

    def _near_names(name: str, limit: int = 3) -> list[str]:
        """Installed skill names close enough to ``name`` to be worth naming."""
        if _loader is None:
            return []
        try:
            import difflib

            names = [s.name for s in _loader.load_all()]
            wanted = name.strip().casefold()
            close = difflib.get_close_matches(wanted, [n.casefold() for n in names], n=limit)
            by_fold = {n.casefold(): n for n in names}
            return [by_fold[c] for c in close if c in by_fold]
        except Exception:  # pragma: no cover
            logger.debug("skill_view.near_names_failed", exc_info=True)
            return []

    def _skill_not_found(name: str) -> str:
        """What to say when the skill is not installed.

        The old text said what *not* to do — do not go looking on disk — and
        then to tell the user it is not installed, which is a dead end even
        though a hub may carry the skill and the tools to fetch it are right
        there. A model handed a dead end reports a failure instead of the next
        step: the report behind this said `skill_view` "returned error: 14",
        a code that exists nowhere in this codebase, invented while paraphrasing
        the old message.
        """
        lines = [
            f"Skill not found: {name}. It is not installed, so there is nothing "
            "to read yet. Do not search host filesystem paths to recover it.",
        ]
        near = [n for n in _near_names(name) if n != name]
        if near:
            quoted = ", ".join(f"`{n}`" for n in near)
            lines.append(f"Installed skills with similar names: {quoted}.")
        if _session_has_tool("skill_search_community"):
            install = (
                " and offer to install it with skill_install_community"
                if _session_has_tool("skill_install_community")
                else ""
            )
            lines.append(
                f"It may still be published on a configured skill hub — search with "
                f'skill_search_community(query="{name}"){install}. Installing changes '
                "this machine, so ask the user before doing it rather than installing "
                "on your own."
            )
        lines.append(
            "Otherwise use skill_list to see what is installed, continue with the "
            "tools available in this session, or tell the user the skill is not "
            "installed. Do not report this as a tool error — the lookup worked, "
            "the skill simply is not here."
        )
        return " ".join(lines)

    @tool(
        name="skill_view",
        description=(
            "Read a skill's SKILL.md content by name. A large skill comes back as its "
            "opening sections plus an index of the rest — pass section to read one of "
            "those, or file_path to read a supporting file."
        ),
        params={
            "name": {
                "type": "string",
                "description": "Exact skill name to view",
            },
            "file_path": {
                "type": "string",
                "description": "Optional sub-file path (references/, scripts/)",
            },
            "section": {
                "type": "string",
                "description": (
                    "Optional section title, quoted from the index a large skill "
                    'returns. Accepts "Parent > Child" when a title repeats.'
                ),
            },
        },
        required=["name"],
    )
    async def skill_view(
        name: str,
        file_path: str | None = None,
        section: str | None = None,
    ) -> str:
        if _loader is None:
            return "No skill loader available."
        skill = _loader.get_by_name(name)
        if skill is None:
            return _skill_not_found(name)

        _register_skill_env_passthrough(skill)

        from agentos.skills.resources import expand_skill_placeholders

        if file_path:
            normalized_path = file_path.strip().lstrip("./")
            if normalized_path in {"", "SKILL.md"}:
                if not skill.content:
                    return f"(Skill '{name}' has no body content)"
                return expand_skill_placeholders(skill.content, skill.base_dir)

            from pathlib import Path

            from agentos.skills.resources import SkillResources

            resources = SkillResources(Path(skill.base_dir))
            content = resources.read_resource(normalized_path)
            if content is None:
                return f"File not found in skill '{name}': {file_path}"
            # Scripts and references come back verbatim: they are source, not a
            # playbook, and a placeholder inside one is that file's own business.
            return content

        # Expand before slicing or budgeting, so an outline's character offsets
        # and a section lookup both run over the exact text the model receives.
        raw = expand_skill_placeholders(skill.content or "", skill.base_dir)
        note = _skill_dir_note(skill)
        if section:
            return note + _skill_section(skill, raw, section)

        body = raw or f"(Skill '{name}' has no body content)"
        if raw:
            body = _skill_body_within_budget(skill, raw)
        body += _skill_setup_note(skill)
        # Append whatever the operator configured for this skill, so the agent
        # starts from the values in effect rather than asking the user or
        # going to read the config file itself. Skills that declare no
        # settings pay nothing for this.
        return note + body + _skill_config_block(skill)

    @tool(
        name="skill_search_community",
        description=(
            "Search Community skill sources such as ClawHub. Use this when the user asks to "
            "find, search, browse, or locate installable skills from the community marketplace. "
            "A skill that needs setup is hidden from your skill list but is still installed, so "
            "check skill_list before concluding one is missing. When the reply carries "
            "installed_match, that skill is already on this machine: report what it needs and "
            "stop, rather than installing a catalog row of the same name over it."
        ),
        params={
            "query": {
                "type": "string",
                "description": "Search query for Community skills.",
            },
            "source": {
                "type": "string",
                "description": (
                    "Source id to search, usually 'clawhub'. Use 'all' to search all sources."
                ),
                "default": "clawhub",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results to return.",
                "default": 10,
            },
        },
        required=["query"],
    )
    async def skill_search_community(
        query: str,
        source: str = "clawhub",
        limit: int = 10,
    ) -> str:
        clean_query = str(query or "").strip()
        if not clean_query:
            raise ToolError("query must not be empty")
        try:
            result_limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            result_limit = 10

        source_id: str | None = str(source or "clawhub").strip() or "clawhub"
        if source_id in {"all", "*"}:
            source_id = None
        local = _local_match(_loader, clean_query) if _loader is not None else None
        router = get_default_skill_router()
        results = await router.search(clean_query, limit=result_limit, source_id=source_id)
        installed = installed_skill_names()
        identifiers = installed_skill_identifiers()
        payload: dict[str, Any] = {
            "status": "ok",
            "query": clean_query,
            "source": source_id or "all",
        }
        # Ahead of the results: whatever the catalog says, the machine's own
        # answer comes first. Browsing still works — nothing is withheld.
        if local is not None:
            payload["installed_match"] = local
        payload["results"] = [
            _community_result_to_dict(row, installed, identifiers) for row in results
        ]
        return json.dumps(payload)

    @tool(
        name="skill_install_community",
        description=(
            "Install a Community skill from ClawHub or another configured source. "
            "Use only when the user clearly asked to install a specific skill identifier "
            "or chose one exact result from skill_search_community. Do not use skill_create "
            "for Community installs. Installing a skill whose name matches one AgentOS "
            "already ships is refused, because it would outrank and replace the built-in."
        ),
        params={
            "identifier": {
                "type": "string",
                "description": (
                    "Exact source identifier or slug returned by skill_search_community."
                ),
            },
            "source": {
                "type": "string",
                "description": "Source id, usually 'clawhub'.",
                "default": "clawhub",
            },
            "force": {
                "type": "boolean",
                "description": (
                    "Override a dangerous security scan, or a refusal to shadow a bundled "
                    "skill, only after the user explicitly asks."
                ),
                "default": False,
            },
        },
        required=["identifier"],
        exposed_by_default=False,
    )
    async def skill_install_community(
        identifier: str,
        source: str = "clawhub",
        force: bool = False,
    ) -> str:
        if _loader is None:
            raise ToolError("Skill loader not available")
        clean_identifier = str(identifier or "").strip()
        if not clean_identifier:
            raise ToolError("identifier must not be empty")
        source_id = str(source or "clawhub").strip() or "clawhub"

        installer = build_default_skill_installer(managed_dir=_loader.managed_dir)
        result = await installer.install(clean_identifier, source_id, force=bool(force))
        if result.success:
            _loader.invalidate_cache()

        payload: dict[str, Any] = {
            "status": "installed" if result.success else "failed",
            "success": result.success,
            "name": result.name,
            "identifier": clean_identifier,
            "source": source_id,
            "message": result.message,
        }
        if result.path:
            payload["path"] = result.path
        if result.scan:
            payload["scan_verdict"] = result.scan.verdict
            payload["scan_findings"] = [finding.__dict__ for finding in result.scan.findings]
        return json.dumps(payload)

    @tool(
        name="install_skill_deps",
        description=(
            "Preview or install a skill dependency declared in skill metadata. "
            "Supports brew, npm, go, and uv install specs. This does not install "
            "Community skills; use skill_install_community for ClawHub installs."
        ),
        params={
            "skill_name": {
                "type": "string",
                "description": "Exact skill name containing the install metadata.",
            },
            "install_id": {
                "type": "string",
                "description": "Install spec id from the skill metadata install list.",
            },
            "confirmed": {
                "type": "boolean",
                "description": "When false, return preview JSON. When true, execute argv.",
                "default": False,
            },
        },
        required=["skill_name", "install_id"],
        exposed_by_default=False,
    )
    async def install_skill_deps(
        skill_name: str,
        install_id: str,
        confirmed: bool = False,
    ) -> str:
        spec = _find_install_spec(skill_name, install_id)
        argv = _argv_for_install_spec(spec)
        label = spec.label or spec.id or "Install dependency"

        if not confirmed:
            return json.dumps(
                {
                    "status": "preview",
                    "skill_name": skill_name,
                    "install_id": install_id,
                    "kind": spec.kind,
                    "label": label,
                    "argv": argv,
                }
            )

        exit_code, stdout, stderr, timed_out = await _run_install_argv(argv)
        return json.dumps(
            {
                "status": "timeout" if timed_out else "executed",
                "skill_name": skill_name,
                "install_id": install_id,
                "kind": spec.kind,
                "label": label,
                "argv": argv,
                "exit_code": exit_code,
                "stdout": stdout,
                "stderr": stderr,
            }
        )

    # ── Mutation tools (workspace layer only) ──────────────────────────

    @tool(
        name="skill_create",
        description=(
            "Create a new local authored skill in the workspace layer. "
            "Writes a SKILL.md file with frontmatter and body content. "
            "Do not use this for Community or ClawHub installs."
        ),
        params={
            "name": {
                "type": "string",
                "description": "Skill name (lowercase, hyphens allowed, e.g. 'my-helper').",
            },
            "description": {
                "type": "string",
                "description": "One-line description of what the skill does.",
            },
            "content": {
                "type": "string",
                "description": "Skill body content (markdown).",
            },
            "triggers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional trigger phrases for auto-activation.",
            },
        },
        required=["name", "description", "content"],
    )
    async def skill_create(
        name: str,
        description: str,
        content: str,
        triggers: list[str] | None = None,
    ) -> str:
        if _loader is None:
            raise ToolError("Skill loader not available")

        if not _SKILL_NAME_RE.match(name):
            raise ToolError(
                f"Invalid skill name: '{name}'. "
                "Use lowercase letters, digits, and hyphens (e.g. 'my-helper')."
            )

        if not description.strip():
            raise ToolError("Description must not be empty")

        if not content.strip():
            raise ToolError("Content must not be empty")

        # Check for name collision
        existing = _loader.get_by_name(name)
        if existing is not None:
            raise ToolError(
                f"Skill '{name}' already exists in layer '{existing.layer.value}'. "
                "Use skill_edit to modify it, or choose a different name."
            )

        # Write to workspace layer
        workspace_dir = _loader.workspace_dir
        if workspace_dir is None:
            raise ToolError("No workspace skill directory configured")

        skill_dir = workspace_dir / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_file = skill_dir / "SKILL.md"

        skill_md = _render_skill_md(name, description, content, triggers)
        skill_file.write_text(skill_md, encoding="utf-8")

        # Invalidate loader cache so new skill is discoverable
        _loader.invalidate_cache()

        logger.info("skill_create.success", name=name)
        # Say this at creation time rather than in the tool description, which
        # would cost tokens on every turn: a skill author has no way to guess
        # that the mime alone decides whether output draws inline or arrives as
        # a download chip.
        return (
            f"Skill '{name}' created at {skill_file}. "
            "For output that should draw inline in Web chat instead of as a "
            "download chip, have the skill publish_artifact with an inline mime "
            "(application/vnd.agentos.chart+json renders a candlestick chart); "
            "docs/artifacts-and-media.md documents the payload."
        )

    @tool(
        name="skill_edit",
        description=(
            "Edit an existing skill's content or description. "
            "Only workspace-layer skills can be edited."
        ),
        params={
            "name": {
                "type": "string",
                "description": "Exact name of the skill to edit.",
            },
            "content": {
                "type": "string",
                "description": "New body content (replaces existing).",
            },
            "description": {
                "type": "string",
                "description": "New description (optional, keeps existing if omitted).",
            },
            "triggers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "New trigger list (optional, keeps existing if omitted).",
            },
        },
        required=["name"],
    )
    async def skill_edit(
        name: str,
        content: str | None = None,
        description: str | None = None,
        triggers: list[str] | None = None,
    ) -> str:
        if _loader is None:
            raise ToolError("Skill loader not available")

        existing = _loader.get_by_name(name)
        if existing is None:
            raise ToolError(f"Skill not found: {name}")

        if existing.layer not in _MUTABLE_LAYERS:
            raise ToolError(
                f"Skill '{name}' is in layer '{existing.layer.value}' and cannot be edited. "
                "Only workspace-layer skills can be modified. "
                "Create a workspace override with skill_create instead."
            )

        if content is None and description is None and triggers is None:
            raise ToolError("Nothing to edit — provide content, description, or triggers")

        # Build updated SKILL.md
        new_description = description if description is not None else existing.description
        new_content = content if content is not None else (existing.content or "")
        new_triggers = triggers if triggers is not None else existing.triggers

        skill_file = Path(existing.file_path)
        if not skill_file.exists():
            raise ToolError(f"Skill file missing: {skill_file}")

        skill_md = _render_skill_md(name, new_description, new_content, new_triggers or None)
        skill_file.write_text(skill_md, encoding="utf-8")

        _loader.invalidate_cache()

        logger.info("skill_edit.success", name=name)
        return f"Skill '{name}' updated"

    @tool(
        name="skill_delete",
        description=(
            "Delete a skill from the workspace layer. Cannot delete bundled or managed skills."
        ),
        params={
            "name": {
                "type": "string",
                "description": "Exact name of the skill to delete.",
            },
        },
        required=["name"],
    )
    async def skill_delete(name: str) -> str:
        import shutil

        if _loader is None:
            raise ToolError("Skill loader not available")

        existing = _loader.get_by_name(name)
        if existing is None:
            raise ToolError(f"Skill not found: {name}")

        if existing.layer not in _MUTABLE_LAYERS:
            raise ToolError(
                f"Skill '{name}' is in layer '{existing.layer.value}' and cannot be deleted. "
                "Only workspace-layer skills can be removed."
            )

        skill_dir = Path(existing.base_dir)
        if not skill_dir.exists():
            raise ToolError(f"Skill directory missing: {skill_dir}")

        shutil.rmtree(skill_dir)
        _loader.invalidate_cache()

        logger.info("skill_delete.success", name=name)
        return f"Skill '{name}' deleted from workspace layer"

    logger.info("skill_tools.registered")
