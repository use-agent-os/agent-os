# Filesystem built-in tools: read_file, write_file, edit_file, list_dir, glob_search, grep_search.

from __future__ import annotations

import asyncio
import bisect
import csv
import fnmatch
import functools
import io
import json
import os
import posixpath
import re
import threading
import zipfile
from collections.abc import Iterable
from pathlib import Path
from xml.etree import ElementTree as ET

import structlog

from agentos.identity.workspace import BOOTSTRAP_FILENAMES
from agentos.redact import redact_file_output
from agentos.sandbox.integration import get_runtime, sandboxed
from agentos.tools.fuzzy_match import (
    AmbiguousMatchError,
    FuzzyMatchError,
    FuzzyMatchResult,
    fuzzy_find_and_replace,
)
from agentos.tools.path_policy import reject_foreign_host_path
from agentos.tools.registry import tool
from agentos.tools.types import ToolError, WorkspaceAccessError, current_tool_context
from agentos.tools.write_tracking import record_workspace_file_write

log = structlog.get_logger(__name__)

_SPREADSHEET_EXTENSIONS = {".csv", ".tsv", ".xlsx"}
_OFFICE_BINARY_EXTENSIONS = {".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"}
_BINARY_EXTENSIONS = {
    ".7z",
    ".bin",
    ".bz2",
    ".dmg",
    ".exe",
    ".gz",
    ".rar",
    ".tar",
    ".zip",
    *_OFFICE_BINARY_EXTENSIONS,
}
_XLSX_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_XLSX_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_XLSX_OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_XLSX_MAX_ROWS = 1_048_576
_BOOTSTRAP_SOURCE_FILENAMES = frozenset(BOOTSTRAP_FILENAMES)


def _workspace_root() -> Path | None:
    ctx = current_tool_context.get()
    if ctx is not None and ctx.workspace_dir:
        return Path(ctx.workspace_dir).expanduser().resolve()
    runtime = get_runtime()
    if runtime is not None and runtime.effective.sandbox_enabled:
        return runtime.workspace.expanduser().resolve()
    return None


def _memory_source_root() -> Path | None:
    ctx = current_tool_context.get()
    if ctx is None or not ctx.memory_source_dir:
        return None
    return Path(ctx.memory_source_dir).expanduser().resolve()


def _memory_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    for root in (_workspace_root(), _memory_source_root()):
        if root is None or root in roots:
            continue
        roots.append(root)
    return tuple(roots)


def _resolve_path(path: str) -> Path:
    """Resolve *path* against the active workspace when relative.

    Reads are always allowed; any workspace enforcement for writes happens in
    :func:`_gate_out_of_workspace_write` via the approval queue, not here.

    Sandbox-visible alias paths (``/workspace/...`` from ``execute_code``
    stdout, ``default_workspace_dir()/...`` from LLM training priors)
    are translated back to the active host workspace before any
    sensitive-path / workspace-strict enforcement runs. Without this,
    model-guessed default-workspace paths are hard-blocked by the
    sensitive_path check even though the same file written under the
    gateway-configured workspace would be valid.
    """
    from agentos.tools.path_aliases import resolve_workspace_alias

    raw = Path(path).expanduser()
    root = _workspace_root()
    reject_foreign_host_path(str(path), platform=os.name, workspace=root)
    alias = resolve_workspace_alias(raw, root)
    if alias is not None:
        return alias
    if root is not None and not raw.is_absolute():
        return (root / raw).resolve(strict=False)
    return raw.resolve(strict=False) if raw.is_absolute() else raw


def _resolve_base(path: str | None) -> Path:
    if path:
        return _resolve_path(path)
    root = _workspace_root()
    return root if root is not None else Path.cwd()


def _memory_source_rel_path(path: Path, roots: Iterable[Path] | None = None) -> str | None:
    """Return *path* relative to the memory root it is a source file of, or ``None``.

    This is the single definition of "which Markdown files feed the memory
    snapshot"; ``apply_patch`` delegates here rather than keeping its own
    copy, so the two tools cannot disagree about what counts as a source.
    *roots* defaults to :func:`_memory_roots`.
    """
    resolved = path.resolve(strict=False)
    for root in _memory_roots() if roots is None else roots:
        try:
            rel = resolved.relative_to(root)
        except ValueError:
            continue

        # USER.md is a curated store in its own right -- CuratedMemoryStore
        # loads, sanitizes, and injects it alongside MEMORY.md. Editing it
        # through a filesystem tool must refresh the frozen snapshot the same
        # way, or the change stays invisible to the model until the session
        # ends. (It is also a bootstrap file, so it notifies both paths.)
        if rel.parts in {("MEMORY.md",), ("memory.md",), ("USER.md",)}:
            return rel.as_posix()
        if len(rel.parts) >= 2 and rel.parts[0] == "memory" and rel.suffix == ".md":
            return rel.as_posix()
    return None


def _bootstrap_source_rel_path(path: Path) -> str | None:
    root = _workspace_root()
    if root is None:
        return None
    resolved = path.resolve(strict=False)
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        return None
    rel_path = rel.as_posix()
    if len(rel.parts) == 1 and rel_path in _BOOTSTRAP_SOURCE_FILENAMES:
        return rel_path
    return None


def _notify_memory_source_write(path: Path) -> None:
    ctx = current_tool_context.get()
    if ctx is None or ctx.on_memory_source_write is None:
        return
    rel = _memory_source_rel_path(path)
    if rel is None:
        return
    ctx.on_memory_source_write(ctx.agent_id or "main", rel)


def _notify_bootstrap_source_write(path: Path) -> None:
    ctx = current_tool_context.get()
    if ctx is None or ctx.on_bootstrap_source_write is None:
        return
    rel = _bootstrap_source_rel_path(path)
    if rel is None:
        return
    ctx.on_bootstrap_source_write(ctx.agent_id or "main", rel)


def _binary_file_error(path: str, p: Path, *, reason: str | None = None) -> ToolError:
    hint = ""
    if p.suffix.lower() in _SPREADSHEET_EXTENSIONS:
        hint = " Use read_spreadsheet(path=...) for CSV/TSV/Excel workbook data."
    detail = f" ({reason})" if reason else ""
    return ToolError(f"Cannot read binary file as text: {path}{detail}.{hint}")


def _looks_binary(raw: bytes, p: Path) -> str | None:
    ext = p.suffix.lower()
    if ext in _OFFICE_BINARY_EXTENSIONS:
        return f"{ext} Office document"
    if ext in _BINARY_EXTENSIONS:
        return f"{ext} binary/container file"
    sample = raw[:8192]
    if b"\x00" in sample:
        return "contains NUL bytes"
    return None


def _read_binary_sample(p: Path, size: int = 8192) -> bytes:
    with p.open("rb") as fh:
        return fh.read(size)


def _stream_numbered_lines_from_file(
    p: Path,
    original_path: str,
    *,
    offset: int | None = None,
    limit: int | None = None,
) -> str:
    """Read a numbered UTF-8 line window without loading the whole file.

    Counting offsets still requires decoding prior lines; invalid UTF-8 before
    the selected window therefore raises the same text/binary error style.
    """

    start_line = offset if offset and offset > 0 else 1
    selected: list[str] = []
    emitted = 0
    try:
        with p.open("rb") as fh:
            for lineno, raw_line in enumerate(fh, start=1):
                line = raw_line.decode("utf-8")
                if lineno < start_line:
                    continue
                if limit is not None and emitted >= limit:
                    break
                selected.append(f"{lineno}\t{line}")
                emitted += 1
    except UnicodeDecodeError as exc:
        raise _binary_file_error(original_path, p, reason="not valid UTF-8") from exc
    return "".join(selected)


def _is_outside_workspace(resolved: Path) -> bool:
    """True when *resolved* is not contained in the active workspace.

    No workspace configured → writes aren't gated at all (no root to compare).
    """
    root = _workspace_root()
    if root is None:
        return False
    try:
        resolved.relative_to(root)
        return False
    except ValueError:
        return True


def _strict_read_workspace_root() -> Path | None:
    """Return the read-containment root when workspace-strict mode is active.

    Unlike :func:`_workspace_root`, strict read containment is intentionally
    opt-in through the entry-point ``ToolContext``. Runtime sandbox workspaces
    still provide relative-path resolution, but they do not by themselves turn
    every read into a strict containment check.
    """

    ctx = current_tool_context.get()
    if ctx is None or not ctx.workspace_strict or not ctx.workspace_dir:
        return None
    return Path(ctx.workspace_dir).expanduser().resolve(strict=False)


def _strict_read_material_root() -> Path | None:
    ctx = current_tool_context.get()
    if (
        ctx is None
        or not ctx.workspace_strict
        or not ctx.artifact_media_root
        or not ctx.artifact_session_id
    ):
        return None

    from agentos.attachment_refs import transcript_material_dir

    return transcript_material_dir(
        Path(ctx.artifact_media_root).expanduser(),
        ctx.artifact_session_id,
    ).resolve(strict=False)


def _strict_read_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    workspace_root = _strict_read_workspace_root()
    if workspace_root is not None:
        roots.append(workspace_root)
    material_root = _strict_read_material_root()
    if material_root is not None:
        roots.append(material_root)
    return tuple(roots)


def _is_within_any_root(candidate: Path, roots: tuple[Path, ...]) -> bool:
    for root in roots:
        try:
            candidate.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _workspace_strict_read_block(
    tool_name: str,
    resolved: Path,
    original_path: str,
) -> dict[str, object] | None:
    """Return a block envelope when *resolved* escapes the strict workspace."""

    roots = _strict_read_roots()
    if not roots:
        return None
    candidate = resolved.expanduser().resolve(strict=False)
    if not _is_within_any_root(candidate, roots):
        root_labels = ", ".join(str(root) for root in roots)
        return {
            "status": "blocked",
            "reason": "workspace_strict",
            "tool": tool_name,
            "path": original_path,
            "resolved_path": str(candidate),
            "workspace": str(roots[0]),
            "allowed_roots": [str(root) for root in roots],
            "message": (
                f"{tool_name} blocked: {candidate} is outside active read roots "
                f"({root_labels})."
            ),
            "retryable": False,
        }
    return None


def _gate_workspace_strict_read(tool_name: str, resolved: Path, original_path: str) -> None:
    """Raise when a read target/base escapes the strict workspace.

    Call this after sensitive-path checks so sensitive hard-blocks keep higher
    priority, and before existence/metadata checks so strict mode does not
    become an existence oracle for outside paths.
    """

    blocked = _workspace_strict_read_block(tool_name, resolved, original_path)
    if blocked is not None:
        raise WorkspaceAccessError(str(blocked["message"]))


def _workspace_strict_candidate_marker(
    tool_name: str,
    candidate: Path,
    original_path: str | None = None,
    strict_root: Path | None = None,
    strict_roots: tuple[Path, ...] | None = None,
) -> str | None:
    """Return a per-candidate blocked marker for directory/search tools."""

    roots = (strict_root,) if strict_root is not None else (strict_roots or _strict_read_roots())
    if not roots:
        return None
    resolved = candidate.expanduser().resolve(strict=False)
    if not _is_within_any_root(resolved, roots):
        root_labels = ", ".join(str(root) for root in roots)
        return f"[blocked] {candidate}: outside active read roots ({root_labels})"
    return None


def _sensitive_access_block(tool_name: str, resolved: Path, original_path: str) -> dict | None:
    """Return a hard-block envelope for sensitive host paths, unless fully elevated."""
    from agentos.sandbox.sensitive_paths import build_block_envelope, sensitive_path_marker
    from agentos.tools.builtin.shell import _context_elevated_mode

    if _context_elevated_mode() == "full":
        return None
    sensitive = sensitive_path_marker(str(resolved), workspace=_workspace_root())
    if sensitive is None:
        return None
    return build_block_envelope(f"{tool_name} {original_path}", sensitive, tool_name=tool_name)


def _is_sensitive_access_path(resolved: Path, workspace: Path | None = None) -> bool:
    from agentos.sandbox.sensitive_paths import sensitive_path_marker
    from agentos.tools.builtin.shell import _context_elevated_mode

    root = workspace if workspace is not None else _workspace_root()
    return (
        _context_elevated_mode() != "full"
        and sensitive_path_marker(str(resolved), workspace=root) is not None
    )


def _workspace_lockdown_roots() -> list[Path]:
    ctx = current_tool_context.get()
    if ctx is None or not ctx.workspace_lockdown:
        return []
    roots: list[Path] = []
    if ctx.workspace_dir:
        roots.append(Path(ctx.workspace_dir).expanduser().resolve(strict=False))
    if ctx.scratch_dir:
        roots.append(Path(ctx.scratch_dir).expanduser().resolve(strict=False))
    return roots


def _inside_any_root(candidate: Path, roots: list[Path]) -> bool:
    resolved = candidate.expanduser().resolve(strict=False)
    for root in roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _gate_workspace_lockdown_write(tool_name: str, resolved: Path, original_path: str) -> None:
    roots = _workspace_lockdown_roots()
    if not roots or _inside_any_root(resolved, roots):
        return
    allowed = ", ".join(str(root) for root in roots)
    raise ToolError(
        f"{tool_name} blocked by workspace lockdown: {original_path} resolves to "
        f"{resolved}, outside allowed roots: {allowed}."
    )


async def _gate_out_of_workspace_write(
    tool_name: str,
    resolved: Path,
    original_path: str,
    approval_id: str | None,
) -> dict | None:
    """Return an approval-required/denied/blocked dict, or None to proceed.

    Writes that stay inside the workspace pass through immediately. Writes
    that target absolute paths outside the workspace get routed through the
    same approval queue that shell warnlist hits use. Writes targeting
    sensitive host paths (SSH keys, /etc, etc.) are hard-blocked regardless
    of approval.
    """
    # Sensitive-path hard block — takes precedence over approval flow.
    from agentos.sandbox.sensitive_paths import build_block_envelope, sensitive_path_marker
    from agentos.tools.builtin.shell import _context_elevated_mode

    elevated_full = _context_elevated_mode() == "full"
    if not elevated_full:
        sensitive = sensitive_path_marker(str(resolved), workspace=_workspace_root())
        if sensitive is not None:
            return build_block_envelope(
                f"{tool_name} {original_path}", sensitive, tool_name=tool_name
            )

    _gate_workspace_lockdown_write(tool_name, resolved, original_path)
    from agentos.tools.write_policy import gate_workspace_write_deny

    gate_workspace_write_deny(
        tool_name,
        resolved,
        original_path=original_path,
        workspace=_workspace_root(),
    )

    if not _is_outside_workspace(resolved):
        return None
    if _memory_source_rel_path(resolved) is not None:
        return None
    from agentos.tools.builtin.shell import (
        _approval_elevation_state,
        _check_exec_approval,
        _restore_approval_elevation,
    )

    workspace = _workspace_root()
    warning = (
        f"writing outside active workspace ({workspace}): {resolved}"
        if workspace is not None
        else f"writing to absolute path: {resolved}"
    )
    prior_elevation = _approval_elevation_state()
    try:
        return await _check_exec_approval(
            tool_name=tool_name,
            command=f"{tool_name} {original_path}",
            workdir=None,
            warning=warning,
            approval_id=approval_id,
            background=False,
        )
    finally:
        _restore_approval_elevation(prior_elevation)


@tool(
    name="read_file",
    description=(
        "Read UTF-8 text file contents with line numbers. Supports offset and limit. "
        "For CSV/TSV/Excel workbook data, use read_spreadsheet."
    ),
    params={
        "path": {"type": "string", "description": "Absolute path to the file."},
        "offset": {
            "type": "integer",
            "description": "Line offset to start reading from (1-indexed).",
        },
        "limit": {"type": "integer", "description": "Maximum number of lines to read."},
    },
    required=["path"],
)
async def read_file(path: str, offset: int | None = None, limit: int | None = None) -> str:
    p = _resolve_path(path)
    blocked = _sensitive_access_block("read_file", p, path)
    if blocked is not None:
        return json.dumps(blocked)
    _gate_workspace_strict_read("read_file", p, path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if not p.is_file():
        raise IsADirectoryError(f"Path is a directory: {path}")

    loop = asyncio.get_running_loop()
    sample: bytes = await loop.run_in_executor(None, _read_binary_sample, p)
    if not sample:
        return ""

    binary_reason = _looks_binary(sample, p)
    if binary_reason:
        raise _binary_file_error(path, p, reason=binary_reason)

    return await loop.run_in_executor(
        None,
        lambda: redact_file_output(
            _stream_numbered_lines_from_file(p, path, offset=offset, limit=limit), path=p
        ),
    )


@tool(
    name="read_spreadsheet",
    description=(
        "Read CSV, TSV, or Excel .xlsx files as structured text tables. "
        "When reading .xlsx, all sheets are returned by default; pass sheet as "
        "a sheet name or 1-based index to read one sheet."
    ),
    params={
        "path": {"type": "string", "description": "Path to a .csv, .tsv, or .xlsx file."},
        "sheet": {
            "type": "string",
            "description": "Optional sheet name or 1-based sheet index for .xlsx files.",
        },
        "offset": {
            "type": "integer",
            "description": "Row offset to start reading from (1-indexed, default 1).",
        },
        "limit": {
            "type": "integer",
            "description": "Maximum rows per sheet to return (default 200).",
        },
    },
    required=["path"],
)
async def read_spreadsheet(
    path: str,
    sheet: str | int | None = None,
    offset: int | None = None,
    limit: int | None = None,
) -> str:
    p = _resolve_path(path)
    blocked = _sensitive_access_block("read_spreadsheet", p, path)
    if blocked is not None:
        return json.dumps(blocked)
    _gate_workspace_strict_read("read_spreadsheet", p, path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if not p.is_file():
        raise IsADirectoryError(f"Path is a directory: {path}")

    ext = p.suffix.lower()
    row_offset = offset if offset and offset > 0 else 1
    row_limit = limit if limit and limit > 0 else 200
    loop = asyncio.get_running_loop()

    if ext in {".csv", ".tsv"}:
        delimiter = "\t" if ext == ".tsv" else ","
        sheets = await loop.run_in_executor(None, _read_delimited_rows, p, delimiter)
    elif ext == ".xlsx":
        sheets = await loop.run_in_executor(None, _read_xlsx_sheets, p)
    else:
        raise ToolError(
            f"Unsupported spreadsheet format: {ext or '(none)'}. Use .csv, .tsv, or .xlsx."
        )

    selected = _select_spreadsheet_sheets(sheets, sheet)
    return await loop.run_in_executor(
        None,
        lambda: redact_file_output(
            _format_spreadsheet(path=p, sheets=selected, offset=row_offset, limit=row_limit),
            path=p,
        ),
    )


#: csv.field_size_limit() is process-global state shared by every thread in
#: the executor pool _read_delimited_rows runs on. Guarding the read/raise/
#: restore excursion below with a lock stops one thread's temporarily-raised
#: limit from leaking into another thread's concurrent parse.
_CSV_FIELD_LIMIT_LOCK = threading.Lock()


def _read_delimited_rows(path: Path, delimiter: str) -> list[tuple[str, dict[int, list[str]], int]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ToolError(f"Cannot read spreadsheet as UTF-8 text: {path}") from exc

    # A single field's raw length can never exceed the file's own length, so
    # raising the limit to len(text) is enough to parse any legitimate large
    # cell (an embedded JSON blob, log line, base64 column -- ordinary data,
    # not a crafted edge case) while staying bounded by memory already spent
    # reading the file. Never raise it to sys.maxsize: a malformed quote
    # would then let the parser treat the rest of an arbitrarily large file
    # as one field with no ceiling at all.
    with _CSV_FIELD_LIMIT_LOCK:
        previous_limit = csv.field_size_limit()
        csv.field_size_limit(max(previous_limit, len(text)))
        try:
            parsed = [list(row) for row in csv.reader(io.StringIO(text), delimiter=delimiter)]
        except csv.Error as exc:
            raise ToolError(f"Cannot parse {path.name} as delimited text: {exc}") from exc
        finally:
            csv.field_size_limit(previous_limit)

    rows = dict(enumerate(parsed, start=1))
    return [(path.name, rows, len(parsed))]


def _read_xlsx_sheets(path: Path) -> list[tuple[str, dict[int, list[str]], int]]:
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            if "xl/workbook.xml" not in names:
                raise ToolError(f"Invalid .xlsx workbook: missing xl/workbook.xml in {path}")
            shared_strings = _read_xlsx_shared_strings(zf, names)
            workbook = ET.fromstring(zf.read("xl/workbook.xml"))
            rels = _read_xlsx_workbook_relationships(zf, names)
            sheets: list[tuple[str, dict[int, list[str]], int]] = []
            for sheet_el in workbook.findall(f".//{{{_XLSX_MAIN_NS}}}sheet"):
                sheet_name = sheet_el.attrib.get("name") or f"Sheet{len(sheets) + 1}"
                rel_id = sheet_el.attrib.get(f"{{{_XLSX_OFFICE_REL_NS}}}id")
                target = rels.get(rel_id or "")
                if not target:
                    continue
                worksheet_path = _normalize_xlsx_target(target)
                if worksheet_path not in names:
                    continue
                rows, total_rows = _read_xlsx_worksheet(zf.read(worksheet_path), shared_strings)
                sheets.append((sheet_name, rows, total_rows))
            if not sheets:
                raise ToolError(f"No readable worksheets found in {path}")
            return sheets
    except zipfile.BadZipFile as exc:
        raise ToolError(f"Invalid .xlsx workbook: {path}") from exc
    except ET.ParseError as exc:
        raise ToolError(f"Invalid .xlsx XML content in {path}: {exc}") from exc


def _xlsx_rich_text(element: ET.Element) -> str:
    """The displayed text of a shared-string or inline-string node.

    A rich string (``CT_Rst``) keeps its value in a direct ``<t>`` child or, when
    the cell mixes formatting, in the ``<t>`` of each direct ``<r>`` run. Its
    ``<rPh>`` siblings hold the phonetic guide instead -- the furigana Excel
    writes by itself whenever text is entered through a Japanese IME -- and are
    not part of the value the sheet displays.

    A descendant search over ``<t>`` cannot tell the two apart and appends the
    reading to the cell, so walk the runs the schema actually defines.
    """

    parts: list[str] = []
    for child in element:
        if child.tag == f"{{{_XLSX_MAIN_NS}}}t":
            parts.append(child.text or "")
        elif child.tag == f"{{{_XLSX_MAIN_NS}}}r":
            for run_text in child.findall(f"{{{_XLSX_MAIN_NS}}}t"):
                parts.append(run_text.text or "")
    return "".join(parts)


def _read_xlsx_shared_strings(zf: zipfile.ZipFile, names: set[str]) -> list[str]:
    if "xl/sharedStrings.xml" not in names:
        return []
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    return [_xlsx_rich_text(si) for si in root.findall(f".//{{{_XLSX_MAIN_NS}}}si")]


def _read_xlsx_workbook_relationships(
    zf: zipfile.ZipFile,
    names: set[str],
) -> dict[str, str]:
    rels_path = "xl/_rels/workbook.xml.rels"
    if rels_path not in names:
        return {}
    root = ET.fromstring(zf.read(rels_path))
    rels: dict[str, str] = {}
    for rel in root.findall(f".//{{{_XLSX_PACKAGE_REL_NS}}}Relationship"):
        rel_id = rel.attrib.get("Id")
        target = rel.attrib.get("Target")
        if rel_id and target:
            rels[rel_id] = target
    return rels


def _normalize_xlsx_target(target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join("xl", target))


def _read_xlsx_worksheet(
    raw_xml: bytes, shared_strings: list[str]
) -> tuple[dict[int, list[str]], int]:
    """Parse a worksheet into a sparse row map (real row number -> cells).

    OpenXML omits empty rows from ``<sheetData>`` by default, giving each
    present ``<row>`` its real 1-indexed row number via the ``r`` attribute.
    Keying the result by that number directly -- instead of padding a list
    with an empty placeholder for every omitted row up to it -- costs memory
    proportional to how many ``<row>`` elements the XML actually contains,
    not to the largest declared row number. A padded-list design lets either
    a crafted/corrupt ``r`` or, combined with a large enough render window,
    an entirely ordinary sparse sheet cost memory and time proportional to
    that number instead of the file's real size (#1149 follow-ups).

    Returns ``(rows, total_row_count)``; a row missing from ``rows`` is
    exactly that sheet's real empty row, distinguishable from "out of
    range" only by comparing its number against ``total_row_count``.
    """
    root = ET.fromstring(raw_xml)
    rows: dict[int, list[str]] = {}
    total_rows = 0
    next_implicit = 1
    for row_el in root.findall(f".//{{{_XLSX_MAIN_NS}}}row"):
        row_r = row_el.attrib.get("r")
        if row_r and row_r.isdigit():
            row_num = int(row_r)
            if row_num < 1 or row_num > _XLSX_MAX_ROWS:
                continue
        else:
            row_num = next_implicit
        next_implicit = row_num + 1
        total_rows = max(total_rows, row_num)
        # Keyed by resolved column, not append order: a <c> with an explicit
        # r="..." can appear out of column order in the XML (#2717), and
        # appending on sight then displaces every cell after the first
        # out-of-order one. A <c> with no r inherits the column immediately
        # after the previous cell, per the OOXML spec -- correct even when
        # that previous cell's own column came from an out-of-order ref.
        cells: dict[int, str] = {}
        next_column = 0
        for cell_el in row_el.findall(f"{{{_XLSX_MAIN_NS}}}c"):
            cell_ref = cell_el.attrib.get("r")
            column_index = _xlsx_column_index(cell_ref) if cell_ref else next_column
            cells[column_index] = _xlsx_cell_value(cell_el, shared_strings)
            next_column = column_index + 1
        row = [cells.get(i, "") for i in range(max(cells, default=-1) + 1)]
        while row and row[-1] == "":
            row.pop()
        rows[row_num] = row
    return rows, total_rows


def _xlsx_column_index(cell_ref: str) -> int:
    match = re.match(r"([A-Za-z]+)", cell_ref)
    if not match:
        return 0
    index = 0
    for char in match.group(1).upper():
        index = index * 26 + (ord(char) - ord("A") + 1)
    return max(0, index - 1)


def _xlsx_cell_value(cell_el: ET.Element, shared_strings: list[str]) -> str:
    cell_type = cell_el.attrib.get("t")
    if cell_type == "inlineStr":
        # The value lives in <is>; a writer that omits it and hangs <t> straight
        # off <c> still reads correctly, since _xlsx_rich_text takes direct
        # children either way.
        inline = cell_el.find(f"{{{_XLSX_MAIN_NS}}}is")
        return _xlsx_rich_text(cell_el if inline is None else inline)

    value_el = cell_el.find(f"{{{_XLSX_MAIN_NS}}}v")
    raw = value_el.text if value_el is not None else ""
    if cell_type == "s" and raw:
        try:
            return shared_strings[int(raw)]
        except (IndexError, ValueError):
            return ""
    if cell_type == "b":
        return "TRUE" if raw == "1" else "FALSE"
    return raw or ""


def _select_spreadsheet_sheets(
    sheets: list[tuple[str, dict[int, list[str]], int]],
    requested: str | int | None,
) -> list[tuple[str, dict[int, list[str]], int]]:
    if requested is None or requested == "":
        return sheets

    requested_name = str(requested)
    # A sheet literally named "1" has to win over the positional reading of
    # "1". Testing the index first made every numeric sheet name unreachable
    # and silently returned whichever sheet sat at that 1-based position.
    for name, rows, total_rows in sheets:
        if name == requested_name:
            return [(name, rows, total_rows)]

    if isinstance(requested, int) or (isinstance(requested, str) and requested.isdigit()):
        index = int(requested) - 1
        if 0 <= index < len(sheets):
            return [sheets[index]]

    for name, rows, total_rows in sheets:
        if name.lower() == requested_name.lower():
            return [(name, rows, total_rows)]

    available = ", ".join(name for name, _, _ in sheets)
    raise ToolError(f"Sheet not found: {requested_name}. Available sheets: {available}")


def _format_spreadsheet(
    *,
    path: Path,
    sheets: list[tuple[str, dict[int, list[str]], int]],
    offset: int,
    limit: int,
) -> str:
    parts = [f"Workbook: {path.name}"]
    # Normalise once so a non-positive offset can't leak into the
    # continuation message below: the slice already floors at row 1, but a
    # raw offset=0 used to print "Showing rows 0-10" instead of "1-10".
    offset = max(1, offset)
    start = offset - 1
    multi_sheet = len(sheets) > 1
    for sheet_name, rows, total_rows in sheets:
        width = max((len(row) for row in rows.values()), default=0)
        parts.append("")
        parts.append(f"Sheet: {sheet_name} ({total_rows} rows x {width} columns)")
        if multi_sheet and total_rows and start >= total_rows:
            # One offset is shared across every sheet in a multi-sheet read,
            # so a sheet smaller than the requested offset would otherwise
            # render as a silent, unexplained empty table.
            parts.append(
                f"(Offset {offset} exceeds this sheet's {total_rows} rows; no rows shown.)"
            )
            continue
        # Window applied here, at render time, against the sparse map --
        # not by slicing a materialised prefix. A gap between real rows
        # wider than `limit` must not stall the continuation offset the
        # way a materialised-prefix length would (#1149 follow-ups).
        end = min(start + limit, total_rows)
        for idx in range(start + 1, end + 1):
            parts.append(f"{idx}\t" + "\t".join(rows.get(idx, [])))
        if end < total_rows:
            parts.append(
                f"(Showing rows {offset}-{end} of {total_rows}. "
                f"Use offset={end + 1} to continue.)"
            )
    return "\n".join(parts)


@tool(
    name="write_file",
    description="Write content to a file, creating directories as needed.",
    params={
        "path": {"type": "string", "description": "Absolute path to write to."},
        "content": {"type": "string", "description": "File content to write."},
        "approval_id": {
            "type": "string",
            "description": "Approval record to consume for writes outside the workspace.",
        },
    },
    required=["path", "content"],
)
@sandboxed(
    kind="fs.write",
    argv_factory=lambda a: ("fs.write", str(a.get("path", ""))),
    record_payload=False,
)
async def write_file(path: str, content: str, approval_id: str | None = None) -> str:
    p = _resolve_path(path)
    approval = await _gate_out_of_workspace_write("write_file", p, path, approval_id)
    if approval is not None:
        return json.dumps(approval)

    loop = asyncio.get_running_loop()

    def _write() -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        # newline="" so the content is the sole authority on line endings;
        # write_text() would stamp os.linesep onto every line.
        with p.open("w", encoding="utf-8", newline="") as handle:
            handle.write(content)

    await loop.run_in_executor(None, _write)
    record_workspace_file_write(p)
    _notify_memory_source_write(p)
    _notify_bootstrap_source_write(p)
    return f"Written {len(content)} bytes to {p}"


def _read_raw_text(p: Path) -> str:
    """Read *p* with universal-newline translation off, so CRLF survives."""
    with p.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


_NEWLINE_RE = re.compile(r"\r\n|\r|\n")


def _dominant_newline(text: str) -> str:
    """Return the line ending a line inserted into *text* should use.

    Majority convention, first ending seen breaking a tie, ``"\n"`` when the
    text has no line ending at all — the same rule ``patch._detect_newline``
    applies to an added hunk line, extended to a lone ``"\r"`` because this
    tool also has to leave a CR-only file the way it found it.
    """
    crlf = text.count("\r\n")
    counts = {"\r\n": crlf, "\n": text.count("\n") - crlf, "\r": text.count("\r") - crlf}
    best = max(counts.values())
    if best == 0:
        return "\n"
    for found in _NEWLINE_RE.finditer(text):
        if counts[found.group()] == best:
            return found.group()
    return "\n"  # pragma: no cover — a leader exists, so the loop returns


def _normalise_newlines(raw: str) -> str:
    """Fold CRLF and lone CR to LF — what universal-newline mode showed before."""
    return raw.replace("\r\n", "\n").replace("\r", "\n")


def _splice_edit(raw: str, normalised: str, match: FuzzyMatchResult) -> str:
    """Apply *match* (found in *normalised*) to *raw*, keeping untouched endings.

    The matcher sees LF-only text because the model writes LF-separated
    old_text; its offsets are mapped back onto the raw text, so every line the
    edit did not name keeps its own ending byte-for-byte. The replacement
    takes the file's dominant convention.
    """
    ((start, end),) = match.spans
    replacement = match.updated[start : len(match.updated) - (len(normalised) - end)]
    newline = _dominant_newline(raw)
    replacement = _NEWLINE_RE.sub(newline, replacement)

    # Folding CRLF to LF drops one character per CRLF (a lone CR folds in
    # place), so a normalised offset is behind the raw one by the number of
    # CRLFs that precede it. Record the normalised index of each folded CRLF
    # and count them with bisect.
    folded = [m.start() - k for k, m in enumerate(re.finditer("\r\n", raw))]

    def to_raw(offset: int) -> int:
        return offset + bisect.bisect_left(folded, offset)

    return raw[: to_raw(start)] + replacement + raw[to_raw(end) :]


def _locate_edit(original: str, old_text: str, new_text: str, *, path: str) -> FuzzyMatchResult:
    """Find *old_text* in *original*, tolerating whitespace and quote drift.

    Exact equality is tried first and costs nothing extra. The fallback chain
    only runs once exact has missed, so the common case is unchanged. Both
    failure modes keep the wording the model already knows, enriched with
    whatever the matcher learned.
    """

    try:
        return fuzzy_find_and_replace(original, old_text, new_text)
    except AmbiguousMatchError as exc:
        lines = ", ".join(str(line) for line in exc.lines)
        raise ValueError(
            f"old_text matches {exc.match_count} locations in {path} (lines {lines});"
            " be more specific"
        ) from exc
    except FuzzyMatchError as exc:
        # The hint quotes real file lines back at the model, so it is a file-read
        # channel like any other and gets the same mask.
        hint = redact_file_output(exc.hint, path=path) if exc.hint else ""
        detail = f" Closest match: {hint}" if hint else ""
        raise ValueError(f"old_text not found in {path}.{detail}") from exc


@tool(
    name="edit_file",
    description=(
        "Edit a file by replacing old_text with new_text. Matching tolerates"
        " indentation, whitespace and smart-quote drift; text appearing more"
        " than once is rejected rather than guessed."
    ),
    params={
        "path": {"type": "string", "description": "Absolute path to the file to edit."},
        "old_text": {"type": "string", "description": "Text to find and replace."},
        "new_text": {"type": "string", "description": "Replacement text."},
        "approval_id": {
            "type": "string",
            "description": "Approval record to consume for edits outside the workspace.",
        },
    },
    required=["path", "old_text", "new_text"],
)
@sandboxed(
    kind="fs.edit",
    argv_factory=lambda a: ("fs.edit", str(a.get("path", ""))),
    record_payload=False,
)
async def edit_file(path: str, old_text: str, new_text: str, approval_id: str | None = None) -> str:
    p = _resolve_path(path)
    approval = await _gate_out_of_workspace_write("edit_file", p, path, approval_id)
    if approval is not None:
        return json.dumps(approval)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")

    loop = asyncio.get_running_loop()
    raw = await loop.run_in_executor(None, _read_raw_text, p)
    # Match against LF-only text — old_text from a model is LF-separated and
    # would miss every multi-line edit on a CRLF file — then splice the result
    # back into the raw text so the file's own endings survive.
    original = _normalise_newlines(raw)

    # The matcher is the CPU-bound part of an edit, not the read or the write:
    # a miss on a large file sweeps every window in it. Run it in the same
    # executor so a slow match costs a worker thread, never the event loop.
    match = await loop.run_in_executor(
        None,
        functools.partial(_locate_edit, original, old_text, new_text, path=path),
    )
    updated = _splice_edit(raw, original, match)

    def _write() -> None:
        with p.open("w", encoding="utf-8", newline="") as handle:
            handle.write(updated)

    await loop.run_in_executor(None, _write)
    # Same bookkeeping as write_file / apply_patch: an edited deliverable is
    # still a workspace write, and artifact delivery only sees the ones recorded.
    record_workspace_file_write(p)
    _notify_memory_source_write(p)
    _notify_bootstrap_source_write(p)
    summary = f"replaced {len(old_text)} chars with {len(new_text)} chars"
    if match.strategy == "exact":
        return f"Edited {p}: {summary}"
    # Surface the fallback: an edit that landed via similarity deserves a
    # second look, and the operator log needs to show how often this happens.
    log.info(
        "tools.edit_file.fuzzy_match",
        strategy=match.strategy,
        path=str(p),
        match_count=match.match_count,
    )
    return f"Edited {p} [match={match.strategy}]: {summary}"


@tool(
    name="list_dir",
    description="List directory contents with type and size.",
    params={
        "path": {"type": "string", "description": "Directory path to list."},
    },
    required=["path"],
)
async def list_dir(path: str) -> str:
    p = _resolve_path(path)
    blocked = _sensitive_access_block("list_dir", p, path)
    if blocked is not None:
        return json.dumps(blocked)
    _gate_workspace_strict_read("list_dir", p, path)
    if not p.exists():
        raise FileNotFoundError(f"Path not found: {path}")
    if not p.is_dir():
        raise NotADirectoryError(f"Not a directory: {path}")

    loop = asyncio.get_running_loop()
    strict_roots = _strict_read_roots()
    workspace_root = _workspace_root()

    def _list() -> list[str]:
        dirs: list[str] = []
        files: list[str] = []
        blocked_entries: list[str] = []
        for entry in sorted(p.iterdir(), key=lambda e: e.name):
            marker = _workspace_strict_candidate_marker(
                "list_dir",
                entry,
                strict_roots=strict_roots,
            )
            if marker is not None:
                blocked_entries.append(marker)
                continue
            if _is_sensitive_access_path(entry.resolve(strict=False), workspace=workspace_root):
                continue
            if entry.is_dir():
                dirs.append(f"[dir]  {entry.name}/")
            else:
                try:
                    size = entry.stat().st_size
                except OSError:
                    try:
                        size = entry.lstat().st_size
                    except OSError:
                        size = 0
                files.append(f"[file] {entry.name} ({size} bytes)")
        return dirs + files + blocked_entries

    entries = await loop.run_in_executor(None, _list)
    if not entries:
        return f"{path}: (empty directory)"
    return "\n".join(entries)


@tool(
    name="glob_search",
    description="Find files matching a glob pattern.",
    params={
        "pattern": {"type": "string", "description": "Glob pattern (e.g. '**/*.py')."},
        "path": {"type": "string", "description": "Base directory to search from (default: cwd)."},
    },
    required=["pattern"],
)
async def glob_search(pattern: str, path: str | None = None) -> str:
    base = _resolve_base(path)
    blocked = _sensitive_access_block("glob_search", base, path or str(base))
    if blocked is not None:
        return json.dumps(blocked)
    _gate_workspace_strict_read("glob_search", base, path or str(base))
    # After the access gates, never before: a blocked path must report as
    # blocked rather than leak its existence through this error. "No matches"
    # for a path that is not there is not self-correcting -- the model reads
    # it as "the symbol does not exist" and stops looking.
    if not base.exists():
        raise FileNotFoundError(f"Path not found: {path or base}")

    loop = asyncio.get_running_loop()
    strict_roots = _strict_read_roots()
    workspace_root = _workspace_root()

    def _glob() -> list[str]:
        matches: list[str] = []
        for candidate in sorted(base.glob(pattern), key=lambda item: str(item)):
            marker = _workspace_strict_candidate_marker(
                "glob_search",
                candidate,
                strict_roots=strict_roots,
            )
            if marker is not None:
                matches.append(marker)
                continue
            if _is_sensitive_access_path(candidate.resolve(strict=False), workspace=workspace_root):
                continue
            matches.append(str(candidate))
        return matches

    matches = await loop.run_in_executor(None, _glob)
    if not matches:
        return f"No files matched pattern '{pattern}' in {base}"
    return "\n".join(matches)


def _include_matches(fp: Path, base: Path, include: str) -> bool:
    """Return True when ``include`` names ``fp`` by filename or by relative path.

    A bare glob (``*.py``, ``test_*.py``) describes the filename, so it is
    matched against ``fp.name`` at any depth exactly as before. A glob that
    carries a directory (``tests/*.py``) can only be matched against the path
    relative to the search base -- matched against the filename alone it never
    fires, and the tool answers "no matches" for code that exists.

    Matching is ``fnmatch``, so ``*`` also spans ``/``; ``**/`` additionally
    matches zero directories (``src/**/*.py`` includes ``src/a.py``), which
    ``fnmatch`` alone would not give it.
    """
    if fnmatch.fnmatch(fp.name, include):
        return True
    try:
        relative = fp.relative_to(base).as_posix()
    except ValueError:  # rglob yields base-prefixed paths; defensive only
        return False
    if fnmatch.fnmatch(relative, include):
        return True
    return "**/" in include and fnmatch.fnmatch(relative, include.replace("**/", ""))


@tool(
    name="grep_search",
    description="Search file contents for a regex pattern.",
    params={
        "pattern": {"type": "string", "description": "Regex pattern to search for."},
        "path": {"type": "string", "description": "File or directory to search (default: cwd)."},
        "include": {
            "type": "string",
            "description": (
                "Glob pattern to filter files, matched against the filename or the "
                "path relative to the search directory (e.g. '*.py', 'tests/*.py')."
            ),
        },
        "max_results": {
            "type": "integer",
            "description": "Maximum number of matches to return (default 100).",
        },
    },
    required=["pattern"],
)
async def grep_search(
    pattern: str,
    path: str | None = None,
    include: str | None = None,
    max_results: int = 100,
) -> str:
    base = _resolve_base(path)
    blocked = _sensitive_access_block("grep_search", base, path or str(base))
    if blocked is not None:
        return json.dumps(blocked)
    _gate_workspace_strict_read("grep_search", base, path or str(base))
    # After the access gates, never before: a blocked path must report as
    # blocked rather than leak its existence through this error. "No matches"
    # for a path that is not there is not self-correcting -- the model reads
    # it as "the symbol does not exist" and stops looking.
    if not base.exists():
        raise FileNotFoundError(f"Path not found: {path or base}")

    loop = asyncio.get_running_loop()
    strict_roots = _strict_read_roots()
    workspace_root = _workspace_root()

    def _search() -> list[str]:
        try:
            regex = re.compile(pattern)
        except re.error as e:
            raise ValueError(f"Invalid regex pattern: {e}") from e

        results: list[str] = []

        def search_file(fp: Path) -> None:
            if _is_sensitive_access_path(fp.resolve(strict=False), workspace=workspace_root):
                return
            try:
                text = fp.read_text(encoding="utf-8", errors="replace")
                for lineno, line in enumerate(text.splitlines(), 1):
                    if regex.search(line):
                        shown = redact_file_output(line.rstrip(), path=fp)
                        results.append(f"{fp}:{lineno}: {shown}")
                        if len(results) >= max_results:
                            return
            except (PermissionError, OSError):
                pass

        if base.is_file():
            search_file(base)
        else:
            for fp in base.rglob("*"):
                if len(results) >= max_results:
                    break
                marker = _workspace_strict_candidate_marker(
                    "grep_search",
                    fp,
                    strict_roots=strict_roots,
                )
                if marker is not None:
                    results.append(marker)
                    continue
                if not fp.is_file():
                    continue
                if include and not _include_matches(fp, base, include):
                    continue
                search_file(fp)

        return results

    matches = await loop.run_in_executor(None, _search)
    if not matches:
        return f"No matches for '{pattern}'"
    return "\n".join(matches)
