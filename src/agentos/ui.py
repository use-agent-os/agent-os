"""Shared terminal presentation helpers."""

from __future__ import annotations

import os
import sys
from typing import IO, cast

from rich import box
from rich.console import Console, Group
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table


class _DynamicStream:
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def _stream(self):
        return getattr(sys, self._name)

    def write(self, data: str) -> int:
        return int(self._stream.write(data))

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


def _no_color_requested() -> bool:
    """Honor the NO_COLOR convention (https://no-color.org/)."""
    return os.environ.get("NO_COLOR", "") != ""


class _CliConsole(Console):
    """Console that trusts the live output stream over ambient FORCE_COLOR.

    The shared CLI consoles are module-level singletons, so their terminal and
    color decisions must be evaluated *per print* against the current
    environment and the real underlying stream -- not frozen at import. This
    keeps colored output on a genuine interactive TTY while producing clean,
    plain text whenever output is captured/piped (a non-TTY) or when the user
    sets ``NO_COLOR``. Rich's default detection short-circuits on ``FORCE_COLOR``
    before it ever consults ``file.isatty()``, which would otherwise leak ANSI
    into redirected output and defeat ``NO_COLOR``.
    """

    @property
    def is_terminal(self) -> bool:
        if _no_color_requested():
            # Fully plain output: suppress box/style escape sequences too.
            return False
        isatty = getattr(self.file, "isatty", None)
        if isatty is None:
            return False
        try:
            return bool(isatty())
        except ValueError:
            # isatty() can raise on a closed stream (e.g. end of a pytest run).
            return False

    @property
    def no_color(self) -> bool:
        return _no_color_requested()

    @no_color.setter
    def no_color(self, value: bool | None) -> None:  # pragma: no cover - Console.__init__ sets this
        # NO_COLOR is resolved live from the environment; ignore the frozen value.
        pass


console = _CliConsole(file=cast(IO[str], _DynamicStream("stdout")), highlight=False)
error_console = _CliConsole(file=cast(IO[str], _DynamicStream("stderr")), highlight=False)

ACCENT = "#CCFF00"
ACCENT_SOFT = "#DDFF66"
ACCENT_DEEP = "#8FB300"
ACCENT_DIM = "#6E9000"
ACCENT_INK = "#0F1400"
ACCENT_HEADER = f"bold {ACCENT}"
ACCENT_MARKUP = ACCENT

AGENTOS_ASCII = "\n".join(
    (
        r" █████╗  ██████╗ ███████╗███╗   ██╗████████╗ ██████╗ ███████╗",
        r"██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝██╔═══██╗██╔════╝",
        r"███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║   ██║   ██║███████╗",
        r"██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║   ██║   ██║╚════██║",
        r"██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║   ╚██████╔╝███████║",
        r"╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝    ╚═════╝ ╚══════╝",
    )
)


def _apply_typer_help_theme() -> None:
    try:
        from typer import rich_utils
    except ImportError:
        return

    import click
    from rich import box
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    rich_utils.STYLE_OPTIONS_PANEL_BORDER = ACCENT
    rich_utils.STYLE_COMMANDS_PANEL_BORDER = ACCENT
    rich_utils.STYLE_OPTION = ACCENT_HEADER
    rich_utils.STYLE_SWITCH = ACCENT_HEADER
    rich_utils.STYLE_COMMANDS_TABLE_FIRST_COLUMN = ACCENT_HEADER
    rich_utils.STYLE_USAGE = ACCENT_SOFT
    rich_utils.STYLE_METAVAR = f"bold {ACCENT_SOFT}"

    def _is_argument(param: object) -> bool:
        return (
            isinstance(param, click.Argument)
            or getattr(param, "param_type_name", "") == "argument"
        )

    def _is_option(param: object) -> bool:
        return isinstance(param, click.Option) or getattr(param, "param_type_name", "") == "option"

    def _make_metavar(param: click.Parameter) -> str:
        metavar = getattr(param, "metavar", None)
        if metavar:
            return str(metavar)
        name = getattr(param, "name", None)
        if _is_argument(param) and name:
            return str(name).upper()
        param_type = getattr(param, "type", None)
        type_name = getattr(param_type, "name", "text")
        return str(type_name).upper()

    def _parameter_label(param: click.Parameter, ctx: click.Context) -> Text:
        if _is_argument(param):
            metavar = _make_metavar(param)
            return Text(metavar, style=rich_utils.STYLE_METAVAR)

        if not _is_option(param):
            return Text(str(getattr(param, "name", "") or ""), style=rich_utils.STYLE_OPTION)
        opt_parts = [*getattr(param, "opts", ())]
        secondary_opts = getattr(param, "secondary_opts", ())
        if secondary_opts:
            opt_parts.append("/".join(secondary_opts))
        label = Text(", ".join(opt_parts), style=rich_utils.STYLE_OPTION)

        metavar = _make_metavar(param)
        if metavar != "BOOLEAN":
            label.append(" ")
            label.append(metavar, style=rich_utils.STYLE_METAVAR)
        if getattr(param, "required", False):
            label.append(" ")
            label.append(rich_utils.REQUIRED_SHORT_STRING, style=rich_utils.STYLE_REQUIRED_SHORT)
        return label

    def _parameter_help(param: click.Parameter, ctx: click.Context) -> Text:
        if _is_option(param):
            try:
                help_record = param.get_help_record(ctx)
            except (AttributeError, TypeError):
                help_text = str(getattr(param, "help", "") or "")
                default = getattr(param, "default", None)
                if getattr(param, "show_default", False) and default not in (None, "", False):
                    help_text = f"{help_text}  [default: {default}]".strip()
                return Text(help_text, style=rich_utils.STYLE_OPTION_HELP)
            else:
                if help_record is not None:
                    return Text(help_record[1] or "", style=rich_utils.STYLE_OPTION_HELP)
        return Text(str(getattr(param, "help", "") or ""), style=rich_utils.STYLE_OPTION_HELP)

    def _print_compact_options_panel(
        *,
        name: str,
        params: list[click.Option] | list[click.Argument],
        ctx: click.Context,
        markup_mode: rich_utils.MarkupModeStrict,
        console,
    ) -> None:
        if not params:
            return

        table = Table(
            highlight=False,
            show_header=False,
            expand=True,
            box=getattr(box, rich_utils.STYLE_OPTIONS_TABLE_BOX, None),
            border_style=rich_utils.STYLE_OPTIONS_TABLE_BORDER_STYLE,
            row_styles=rich_utils.STYLE_OPTIONS_TABLE_ROW_STYLES,
            pad_edge=rich_utils.STYLE_OPTIONS_TABLE_PAD_EDGE,
            padding=rich_utils.STYLE_OPTIONS_TABLE_PADDING,
            show_lines=rich_utils.STYLE_OPTIONS_TABLE_SHOW_LINES,
            leading=rich_utils.STYLE_OPTIONS_TABLE_LEADING,
        )
        table.add_column("Option", no_wrap=True)
        table.add_column("Help", ratio=1)
        for param in params:
            table.add_row(
                _parameter_label(param, ctx),
                _parameter_help(param, ctx),
            )

        console.print(
            Panel(
                table,
                border_style=rich_utils.STYLE_OPTIONS_PANEL_BORDER,
                title=name,
                title_align=rich_utils.ALIGN_OPTIONS_PANEL,
            )
        )

    rich_utils._print_options_panel = _print_compact_options_panel  # noqa: SLF001


_apply_typer_help_theme()


def error_panel(message: str, *, title: str = "Error") -> Panel:
    """Return a compact operator-facing error panel."""
    return Panel(f"[red]{markup_escape(message)}[/red]", title=title, border_style="red")


def warning_panel(message: str, *, title: str = "Warning") -> Panel:
    """Return a brand-tinted warning panel for recoverable setup gaps."""
    body = f"[bold {ACCENT}]▌ {markup_escape(title)}[/bold {ACCENT}]"
    body += f"\n[dim]{markup_escape(message)}[/dim]"
    return Panel(body, border_style=ACCENT_SOFT, padding=(0, 2))


def markup_escape(value: object) -> str:
    """Escape dynamic text before interpolating it into Rich markup."""
    return escape(str(value))


def banner_panel(title: str, subtitle: str = "") -> Panel:
    """Brand-tinted header panel used by onboarding / setup surfaces."""
    body = f"[bold {ACCENT}]▌ {markup_escape(title)}[/bold {ACCENT}]"
    if subtitle:
        body += f"\n[dim]{markup_escape(subtitle)}[/dim]"
    return Panel(
        body,
        border_style=ACCENT,
        box=box.ROUNDED,
        padding=(1, 2),
    )


def setup_cockpit_panel(
    *,
    title: str,
    subtitle: str,
    steps: list[tuple[str, str, str]],
    config_path: object | None = None,
) -> Panel:
    """Return the richer first-run setup cockpit shown before prompts."""
    table = Table.grid(expand=True)
    table.add_column(ratio=1)
    table.add_column(ratio=1)
    table.add_column(ratio=1)
    for idx in range(0, len(steps), 3):
        row = []
        for label, detail, state in steps[idx : idx + 3]:
            state_style = (
                ACCENT_SOFT
                if state == "ready"
                else "yellow" if state == "later" else ACCENT
            )
            row.append(
                "\n".join(
                    [
                        f"[bold {ACCENT_SOFT}]{markup_escape(label)}[/]",
                        f"[dim]{markup_escape(detail)}[/dim]",
                        f"[{state_style}]● {markup_escape(state)}[/]",
                    ]
                )
            )
        while len(row) < 3:
            row.append("")
        table.add_row(*row)

    body = Group(
        f"[bold {ACCENT}]{AGENTOS_ASCII}[/]",
        "",
        f"[bold {ACCENT}]▌ {markup_escape(title)}[/]",
        f"[dim]{markup_escape(subtitle)}[/dim]",
        "",
        table,
        "",
        (
            f"[dim]Config:[/] [{ACCENT_SOFT}]{markup_escape(config_path)}[/]"
            if config_path
            else "[dim]Config: default profile[/]"
        ),
        f"[dim]Keys:[/] [{ACCENT_SOFT}]↑/↓[/] move  "
        f"[{ACCENT_SOFT}]Enter[/] select  [{ACCENT_SOFT}]Ctrl+C[/] cancel",
    )
    return Panel(
        body,
        border_style=ACCENT,
        box=box.HEAVY_HEAD,
        padding=(1, 2),
        title="AgentOS",
        title_align="left",
    )


def setup_step_panel(title: str, detail: str = "") -> Panel:
    """Compact section header for a setup step."""
    body = f"[bold {ACCENT}]◆ {markup_escape(title)}[/]"
    if detail:
        body += f"\n[dim]{markup_escape(detail)}[/dim]"
    return Panel(body, border_style=ACCENT_DIM, box=box.ROUNDED, padding=(0, 2))


def section_rule(label: str) -> str:
    """A compact rule string with the brand accent for inline section markers."""
    return (
        f"[bold {ACCENT}]┄┄┄ {markup_escape(label)} "
        f"[/bold {ACCENT}][{ACCENT_DIM}]"
        + "─" * 6
        + "[/]"
    )


def questionary_style():
    """Build a questionary Style aligned with the WebUI brand orange.

    Returns ``None`` if questionary is unavailable or stubbed in tests.
    """
    try:
        from questionary import Style
    except (ImportError, AttributeError):
        return None

    return Style(
        [
            ("qmark", f"fg:{ACCENT_SOFT} bold"),
            ("question", f"fg:{ACCENT} bold"),
            ("answer", f"fg:{ACCENT_SOFT} bold"),
            ("pointer", f"fg:{ACCENT} bold noreverse"),
            ("highlighted", f"fg:{ACCENT} bold noreverse"),
            ("selected", f"fg:{ACCENT_SOFT} bold noreverse"),
            ("separator", f"fg:{ACCENT_DIM}"),
            ("instruction", f"fg:{ACCENT_DIM}"),
            ("text", ""),
            ("disabled", "fg:#666666 italic"),
        ]
    )
