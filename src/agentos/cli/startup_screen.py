"""AgentOS interactive-chat startup screen.

Renders the branded welcome surface shown when ``agentos chat`` launches:
a block-letter ``AGENTOS`` wordmark with a vertical orange gradient, followed by
a full-width rounded panel that pairs a small abstract mark + session details
on the left with the available tool/skill catalogue on the right.

Everything here is defensive: data-gathering helpers never raise (each registry
call is wrapped in ``try``/``except`` and degrades to empty), so a broken
registry can never crash startup. All public helpers return Rich renderables and
are pure, which keeps them unit-testable without a live gateway.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.align import Align
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from agentos.cli.ui import ACCENT, ACCENT_DEEP, ACCENT_DIM, ACCENT_SOFT

# ---------------------------------------------------------------------------
# Layout constants
# ---------------------------------------------------------------------------

#: Render below this console width and we drop the giant art for a compact head.
COMPACT_WIDTH = 80
#: Maximum item lines shown per catalogue section before truncating with "...".
MAX_SECTION_LINES = 8
#: Maximum item names listed inline per group before a trailing ellipsis.
MAX_ITEMS_PER_GROUP = 4


# ---------------------------------------------------------------------------
# Block-letter wordmark
# ---------------------------------------------------------------------------

# Each glyph is six rows of equal width; rows are joined with a two-space gutter
# to keep letters distinct. Kept as data (not markup) so the gradient is applied
# per-row via ``Text`` styles, sidestepping Rich markup escaping entirely.
_GLYPHS: dict[str, list[str]] = {
    "O": ["██████", "██  ██", "██  ██", "██  ██", "██  ██", "██████"],
    "P": ["██████", "██  ██", "██████", "██    ", "██    ", "██    "],
    "E": ["██████", "██    ", "█████ ", "██    ", "██    ", "██████"],
    "N": ["██  ██", "███ ██", "██████", "██ ███", "██  ██", "██  ██"],
    "C": ["██████", "██    ", "██    ", "██    ", "██    ", "██████"],
    "A": [" ████ ", "██  ██", "██  ██", "██████", "██  ██", "██  ██"],
    "G": ["██████", "██    ", "██ ███", "██  ██", "██  ██", "██████"],
    "T": ["██████", "  ██  ", "  ██  ", "  ██  ", "  ██  ", "  ██  "],
    "S": ["██████", "██    ", "██████", "    ██", "    ██", "██████"],
}
_GLYPH_ROWS = 6
_WORDMARK = "AGENTOS"

# Top rows lighter, bottom rows deeper — a warm vertical gradient in AgentOS orange.
_GRADIENT = [ACCENT_SOFT, ACCENT_SOFT, ACCENT, ACCENT, ACCENT_DEEP, ACCENT_DEEP]

# Small abstract braille "cap wave" mark for the left column (dim orange). Every
# row is padded to the same display width so it never wrap-garbles.
_LEFT_MARK: list[str] = [
    "  ·· ⣀⣤⣤⣀ ··  ",
    "  ⢀⣴⠟⠉⠉⠻⣦⡀  ",
    " ⣰⡟⠁ ·· ⠈⢻⣆ ",
    " ⣿⡇ ⢀⣤⣤⡀ ⢸⣿ ",
    " ⠹⣧⡀⠈⠛⠛⠁⢀⣼⠏ ",
    "  ⠙⠿⣷⣶⣶⡾⠟⠋  ",
    "   ·· ⠉⠉ ··   ",
]


def _wordmark_lines() -> list[str]:
    """Return the assembled wordmark as plain text rows (no styling)."""
    return ["  ".join(_GLYPHS[ch][row] for ch in _WORDMARK) for row in range(_GLYPH_ROWS)]


def render_wordmark() -> Text:
    """Return the gradient AGENTOS wordmark as a single multi-line ``Text``."""
    art = Text()
    for index, line in enumerate(_wordmark_lines()):
        art.append(line, style=_GRADIENT[index])
        if index < _GLYPH_ROWS - 1:
            art.append("\n")
    return art


def render_left_mark() -> Text:
    """Return the dim-orange abstract cap mark used in the panel's left column."""
    mark = Text()
    for index, line in enumerate(_LEFT_MARK):
        mark.append(line, style=ACCENT_DIM)
        if index < len(_LEFT_MARK) - 1:
            mark.append("\n")
    return mark


# ---------------------------------------------------------------------------
# Startup data (defensive — every call degrades to empty on failure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StartupData:
    """Resolved, render-ready data for the startup screen.

    Every field has a safe default so a render can proceed even if all
    gather steps fail.
    """

    version: str = "--"
    model: str = "--"
    workdir: str = "--"
    session_key: str = "--"
    session_title: str | None = None
    tool_groups: list[tuple[str, list[str]]] = field(default_factory=list)
    skill_groups: list[tuple[str, list[str]]] = field(default_factory=list)
    tool_count: int = 0
    skill_count: int = 0


def _gather_version() -> str:
    try:
        from agentos import __version__

        return str(__version__) or "--"
    except Exception:
        return "--"


def _gather_default_model() -> str:
    try:
        from agentos.gateway.config import LlmProviderConfig

        return str(LlmProviderConfig().model) or "--"
    except Exception:
        return "--"


def _gather_workdir() -> str:
    try:
        from agentos.paths import default_agentos_home

        return str(default_agentos_home() / "workspace")
    except Exception:
        return "--"


def _gather_tool_groups() -> tuple[list[tuple[str, list[str]]], int]:
    """Return ``([(toolset, [tool, ...]), ...], total_tool_count)``."""
    try:
        import agentos.tools  # noqa: F401  (side-effect: registers builtin tools)
        from agentos.tools.registry import get_default_registry

        registry = get_default_registry()
        grouped: dict[str, list[str]] = {}
        for registered in registry.all_tools():
            module = getattr(registered.handler, "__module__", "") or ""
            toolset = module.rsplit(".", 1)[-1] or "tools"
            grouped.setdefault(toolset, []).append(registered.spec.name)
        groups = sorted(
            ((name, sorted(items)) for name, items in grouped.items()),
            key=lambda pair: (-len(pair[1]), pair[0]),
        )
        total = sum(len(items) for _, items in groups)
        return groups, total
    except Exception:
        return [], 0


def _gather_skill_groups() -> tuple[list[tuple[str, list[str]]], int]:
    """Return ``([(kind, [skill, ...]), ...], total_skill_count)``."""
    try:
        from pathlib import Path

        import agentos
        from agentos.skills.loader import SkillLoader

        bundled = Path(agentos.__file__).resolve().parent / "skills" / "bundled"
        loader = SkillLoader(bundled_dir=bundled)
        grouped: dict[str, list[str]] = {}
        for skill in loader.load_all():
            kind = str(getattr(skill, "kind", "") or "skill")
            grouped.setdefault(kind, []).append(skill.name)
        groups = sorted(
            ((name, sorted(items)) for name, items in grouped.items()),
            key=lambda pair: (-len(pair[1]), pair[0]),
        )
        total = sum(len(items) for _, items in groups)
        return groups, total
    except Exception:
        return [], 0


def gather_startup_data(
    *,
    session_key: str | None = None,
    session_title: str | None = None,
    model: str | None = None,
    workdir: str | None = None,
) -> StartupData:
    """Collect every value the startup screen needs, never raising.

    Caller-provided ``session_key``/``model``/``workdir`` win over discovered
    defaults; missing values fall back to ``"--"``. ``session_title`` is the
    friendly display name (set via ``/new <title>``) surfaced alongside the
    opaque key when known.
    """
    tool_groups, tool_count = _gather_tool_groups()
    skill_groups, skill_count = _gather_skill_groups()
    resolved_model = (model or "").strip() or _gather_default_model()
    resolved_workdir = (workdir or "").strip() or _gather_workdir()
    resolved_session = (session_key or "").strip() or "--"
    resolved_title = (session_title or "").strip() or None
    return StartupData(
        version=_gather_version(),
        model=resolved_model,
        workdir=resolved_workdir,
        session_key=resolved_session,
        session_title=resolved_title,
        tool_groups=tool_groups,
        skill_groups=skill_groups,
        tool_count=tool_count,
        skill_count=skill_count,
    )


# ---------------------------------------------------------------------------
# Panel column renderables
# ---------------------------------------------------------------------------


def _left_column(data: StartupData) -> RenderableType:
    """Abstract mark + model / workdir / session details (left of the panel)."""
    body = Text()
    body.append(render_left_mark())
    body.append("\n\n")
    body.append(f"{data.model}\n", style=f"bold {ACCENT_SOFT}")
    body.append(f"{data.workdir}\n", style="dim")
    if data.session_title and data.session_title != data.session_key:
        body.append(f"Session: {data.session_title} ({data.session_key})", style="dim")
    else:
        body.append(f"Session: {data.session_key}", style="dim")
    return body


def _catalogue_section(title: str, groups: list[tuple[str, list[str]]]) -> Text:
    """Render one "Available X" section: a heading then ``category: items``."""
    section = Text()
    section.append(title, style=f"bold {ACCENT}")
    section.append("\n")
    if not groups:
        section.append("  (none available)", style="dim")
        return section

    shown = groups[:MAX_SECTION_LINES]
    for name, items in shown:
        section.append(f"  {name}: ", style=ACCENT_DIM)
        head = items[:MAX_ITEMS_PER_GROUP]
        section.append(", ".join(head), style=ACCENT_SOFT)
        if len(items) > MAX_ITEMS_PER_GROUP:
            section.append(" ...", style="dim")
        section.append("\n")
    remaining = len(groups) - len(shown)
    if remaining > 0:
        section.append(f"  (and {remaining} more groups...)", style="dim")
        section.append("\n")
    return section


def _right_column(data: StartupData) -> RenderableType:
    """Available Tools + Available Skills sections plus the count footer."""
    group = Group(
        _catalogue_section("Available Tools", data.tool_groups),
        Text(""),
        _catalogue_section("Available Skills", data.skill_groups),
        Text(
            f"{data.tool_count} tools · {data.skill_count} skills · "
            "/help for commands",
            style="dim",
        ),
    )
    return group


def _panel_title(data: StartupData) -> str:
    return f"AgentOS Agent v{data.version}"


def render_info_panel(data: StartupData) -> Panel:
    """Return the full-width rounded panel (left details + right catalogue)."""
    table = Table.grid(expand=True, padding=(0, 3))
    table.add_column(justify="left", ratio=None, no_wrap=True)
    table.add_column(justify="left", ratio=1)
    table.add_row(_left_column(data), _right_column(data))
    return Panel(
        table,
        title=_panel_title(data),
        title_align="center",
        border_style=ACCENT,
        padding=(1, 2),
    )


# ---------------------------------------------------------------------------
# Compact fallback (narrow terminals)
# ---------------------------------------------------------------------------


def render_compact_header(data: StartupData) -> Panel:
    """A small branded header for narrow terminals (no giant art)."""
    body = Text()
    body.append("AgentOS Agent", style=f"bold {ACCENT}")
    body.append(f"  v{data.version}\n", style=ACCENT_SOFT)
    body.append(f"{data.model}\n", style="dim")
    body.append(
        f"{data.tool_count} tools · {data.skill_count} skills · "
        "/help for commands",
        style="dim",
    )
    return Panel(body, border_style=ACCENT, padding=(0, 2))


# ---------------------------------------------------------------------------
# Footer (welcome line + tip), outside the panel
# ---------------------------------------------------------------------------

_TIP = (
    "+ Tip: run `agentos gateway start` in another terminal to bring up the "
    "local gateway."
)


def render_footer() -> Text:
    """Plain welcome line and a dim tip, shown below the panel."""
    footer = Text()
    footer.append(
        "Welcome to AgentOS! Type your message or /help for commands.\n",
        style=f"bold {ACCENT_SOFT}",
    )
    footer.append(_TIP, style="dim")
    return footer


# ---------------------------------------------------------------------------
# Top-level renderable + entrypoint
# ---------------------------------------------------------------------------


def build_startup_renderable(
    data: StartupData,
    *,
    width: int,
) -> RenderableType:
    """Assemble the full startup renderable, choosing wide vs compact layout."""
    if width < COMPACT_WIDTH:
        return Group(
            render_compact_header(data),
            Text(""),
            render_footer(),
        )
    return Group(
        Text(""),
        Align.center(render_wordmark(), width=width),
        Text(""),
        render_info_panel(data),
        Text(""),
        render_footer(),
    )


def render_startup_screen(
    console: Console,
    *,
    session_key: str | None = None,
    session_title: str | None = None,
    model: str | None = None,
    workdir: str | None = None,
) -> None:
    """Render the AgentOS startup screen to ``console``.

    Never raises on data-gathering failures; the worst case is a screen with
    ``"--"`` placeholders and empty catalogue sections.
    """
    data = gather_startup_data(
        session_key=session_key,
        session_title=session_title,
        model=model,
        workdir=workdir,
    )
    width = getattr(console, "width", 0) or 100
    console.print(build_startup_renderable(data, width=width))
