"""Files read and modified, carried across compactions by the engine itself.

A model-written summary is free to forget that ``src/app.py`` was already
edited, or to name a file that was never opened. After two or three
compactions that is how a long coding session starts re-reading files and
redoing finished steps. This module keeps that record out of the model's
hands:

* :func:`file_activity_from_messages` (provider messages) and
  :func:`file_activity_from_entries` (persisted transcript rows, for the
  session-level ``/compact``) read the tool calls being compacted and list
  the files they read and the files they modified. Only calls whose result
  came back without an error count. The mutating tools are the ones the
  verification ledger tracks
  (:data:`~agentos.engine.verification_ledger.MUTATING_TOOL_NAMES`); shell
  and code-execution commands are not parsed for paths, so a file that
  only a shell command touched is not listed.
* :func:`merge_file_activity` folds the new lists into the ones carried by the
  previous summary. A modified file is listed once, under "Modified", even if
  it was also read; the most recently touched paths win when the lists are
  capped.
* :func:`attach_file_activity` appends the lists as a fixed-format block after
  the model's text, and :func:`split_file_activity` takes that block back off
  the next time, so the merge is deterministic and the summarizer never sees
  (or rewrites) the lists.

The block is bounded (:data:`FILE_ACTIVITY_MAX_CHARS`) so that it fits in the
summary's token budget together with the model's text.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agentos.engine.verification_ledger import MUTATING_TOOL_NAMES, mutated_paths
from agentos.provider.types import ContentBlockToolResult, ContentBlockToolUse, Message

# Built-in tools whose ``path`` argument names one local file they read.
# Directory listings and searches (list_dir, glob_search, grep_search) do not
# read a particular file and are left out.
READ_TOOL_NAMES = frozenset({"read_file", "read_spreadsheet", "pdf"})

FILE_ACTIVITY_HEADER = (
    "[Files touched before this point -- recorded by the engine, not the summary]"
)
_MODIFIED_LABEL = "Modified:"
_READ_LABEL = "Read:"
_ITEM_PREFIX = "- "
_OMITTED_RE = re.compile(r"^\((\d+) older paths? not listed\)$")

# Paths kept per list, newest first to survive.
FILE_ACTIVITY_MAX_PATHS = 20
# Upper bound on the rendered block (about 400 tokens at 4 chars per token).
FILE_ACTIVITY_MAX_CHARS = 1_600
FILE_ACTIVITY_MAX_TOKENS = FILE_ACTIVITY_MAX_CHARS // 4
# A path this long is almost certainly not a real path; never list it.
_MAX_PATH_CHARS = 400


@dataclass(frozen=True)
class FileActivity:
    """Files read and modified, oldest first; counts of paths dropped by the cap."""

    read: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    read_omitted: int = 0
    modified_omitted: int = 0

    @property
    def is_empty(self) -> bool:
        return not (self.read or self.modified or self.read_omitted or self.modified_omitted)


def _clean_path(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    path = value.strip()
    if not path or len(path) > _MAX_PATH_CHARS:
        return None
    if "\n" in path or "\r" in path or "://" in path:
        # Not a local path (a URL), or one that would break the block format.
        return None
    return path


def _touch(paths: dict[str, None], path: str) -> None:
    """Record *path* as the most recently touched one."""
    paths.pop(path, None)
    paths[path] = None


def _activity_from_calls(
    calls: Iterable[tuple[str, str, dict[str, Any]]], failed: dict[str, bool]
) -> FileActivity:
    """Files touched by *calls* (``(id, tool name, arguments)``), in call order.

    *failed* maps a call id to whether its result was an error; a call with
    no result never ran, and counts as much as a failed one: not at all.
    """
    read: dict[str, None] = {}
    modified: dict[str, None] = {}
    for call_id, name, arguments in calls:
        if failed.get(call_id, True):
            continue
        if name in MUTATING_TOOL_NAMES:
            for raw in mutated_paths(arguments):
                path = _clean_path(raw)
                if path is not None:
                    _touch(modified, path)
        elif name in READ_TOOL_NAMES:
            path = _clean_path(arguments.get("path") or arguments.get("file_path"))
            if path is not None:
                _touch(read, path)
    return FileActivity(
        read=tuple(path for path in read if path not in modified),
        modified=tuple(modified),
    )


def file_activity_from_messages(messages: Sequence[Message]) -> FileActivity:
    """Files read and modified by the successful tool calls in *messages*."""
    failed: dict[str, bool] = {}
    calls: list[tuple[str, str, dict[str, Any]]] = []
    for message in messages:
        if not isinstance(message.content, list):
            continue
        for block in message.content:
            if isinstance(block, ContentBlockToolResult):
                failed[block.tool_use_id] = bool(block.is_error)
            elif isinstance(block, ContentBlockToolUse) and message.role == "assistant":
                arguments = block.input if isinstance(block.input, dict) else {}
                calls.append((block.id, block.name, arguments))
    return _activity_from_calls(calls, failed)


def _segment_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def file_activity_from_entries(entries: Sequence[Mapping[str, Any]]) -> FileActivity:
    """The same lists from persisted transcript rows (``tool_calls`` segments).

    Assistant rows carry ``tool_use`` and ``tool_result`` segments; rows in
    the chat-completions shape carry ``function`` calls, answered by
    ``role: tool`` rows.
    """
    failed: dict[str, bool] = {}
    calls: list[tuple[str, str, dict[str, Any]]] = []
    for entry in entries:
        if entry.get("role") == "tool" and entry.get("tool_call_id"):
            failed[str(entry["tool_call_id"])] = bool(entry.get("is_error"))
        segments = entry.get("tool_calls")
        if not isinstance(segments, list):
            continue
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            kind = segment.get("type")
            function = segment.get("function")
            if kind == "tool_result":
                call_id = segment.get("tool_use_id")
                if call_id:
                    failed[str(call_id)] = bool(segment.get("is_error"))
            elif kind == "tool_use" or isinstance(function, dict):
                call_id = segment.get("tool_use_id") or segment.get("id")
                fn = function if isinstance(function, dict) else {}
                name = segment.get("name") or fn.get("name")
                raw = segment.get("input") if "input" in segment else fn.get("arguments")
                if call_id and isinstance(name, str):
                    calls.append((str(call_id), name, _segment_arguments(raw)))
    return _activity_from_calls(calls, failed)


def _cap(
    activity: FileActivity,
    *,
    max_paths: int = FILE_ACTIVITY_MAX_PATHS,
    max_chars: int = FILE_ACTIVITY_MAX_CHARS,
) -> FileActivity:
    """Keep the newest paths of each list within the count and size bounds.

    Read paths go first when the block is too large: knowing what was changed
    matters more than knowing what was looked at.
    """
    read = list(activity.read)
    modified = list(activity.modified)
    read_omitted = activity.read_omitted
    modified_omitted = activity.modified_omitted
    if len(read) > max_paths:
        read_omitted += len(read) - max_paths
        read = read[-max_paths:]
    if len(modified) > max_paths:
        modified_omitted += len(modified) - max_paths
        modified = modified[-max_paths:]
    capped = FileActivity(tuple(read), tuple(modified), read_omitted, modified_omitted)
    while len(render_file_activity(capped)) > max_chars and (read or modified):
        if read:
            read.pop(0)
            read_omitted += 1
        else:
            modified.pop(0)
            modified_omitted += 1
        capped = FileActivity(tuple(read), tuple(modified), read_omitted, modified_omitted)
    return capped


def merge_file_activity(
    previous: FileActivity | None,
    new: FileActivity | None,
    *,
    max_paths: int = FILE_ACTIVITY_MAX_PATHS,
    max_chars: int = FILE_ACTIVITY_MAX_CHARS,
) -> FileActivity:
    """*previous* updated with *new*, capped.

    A path touched again moves to the newest end of its list; a path that was
    modified at any point is listed only under modified.
    """
    parts = [part for part in (previous, new) if part is not None]
    read: dict[str, None] = {}
    modified: dict[str, None] = {}
    for part in parts:
        for path in part.read:
            _touch(read, path)
        for path in part.modified:
            _touch(modified, path)
    merged = FileActivity(
        read=tuple(path for path in read if path not in modified),
        modified=tuple(modified),
        read_omitted=sum(part.read_omitted for part in parts),
        modified_omitted=sum(part.modified_omitted for part in parts),
    )
    return _cap(merged, max_paths=max_paths, max_chars=max_chars)


def _section(label: str, paths: Iterable[str], omitted: int) -> list[str]:
    items = [f"{_ITEM_PREFIX}{path}" for path in paths]
    if not items and not omitted:
        return []
    lines = [label]
    if omitted:
        noun = "path" if omitted == 1 else "paths"
        lines.append(f"({omitted} older {noun} not listed)")
    lines.extend(items)
    return lines


def render_file_activity(activity: FileActivity) -> str:
    """The block appended to a summary; empty when nothing was touched."""
    if activity.is_empty:
        return ""
    lines = [FILE_ACTIVITY_HEADER]
    lines += _section(_MODIFIED_LABEL, activity.modified, activity.modified_omitted)
    lines += _section(_READ_LABEL, activity.read, activity.read_omitted)
    return "\n".join(lines)


def _parse_block(lines: Sequence[str]) -> FileActivity | None:
    """Parse the lines after the header; ``None`` when they are not a block."""
    read: list[str] = []
    modified: list[str] = []
    omitted = {"read": 0, "modified": 0}
    section: str | None = None
    for raw in lines:
        line = raw.rstrip()
        if not line:
            continue
        if line == _MODIFIED_LABEL:
            section = "modified"
            continue
        if line == _READ_LABEL:
            section = "read"
            continue
        if section is None:
            return None
        if line.startswith(_ITEM_PREFIX):
            path = line[len(_ITEM_PREFIX) :].strip()
            if path:
                (modified if section == "modified" else read).append(path)
            continue
        match = _OMITTED_RE.match(line)
        if match is None:
            return None
        omitted[section] += int(match.group(1))
    return FileActivity(
        read=tuple(read),
        modified=tuple(modified),
        read_omitted=omitted["read"],
        modified_omitted=omitted["modified"],
    )


def split_file_activity(summary: str) -> tuple[str, FileActivity | None]:
    """Split *summary* into its text and its trailing file block, if it has one.

    Only a block at the very end that parses cleanly counts; anything else is
    left in the text and ``None`` is returned for the block.
    """
    if summary.startswith(FILE_ACTIVITY_HEADER):
        start = 0
    else:
        found = summary.rfind("\n" + FILE_ACTIVITY_HEADER)
        if found < 0:
            return summary, None
        start = found + 1
    tail = summary[start + len(FILE_ACTIVITY_HEADER) :]
    if tail and not tail.startswith("\n"):
        return summary, None
    activity = _parse_block(tail.split("\n"))
    if activity is None:
        return summary, None
    return summary[:start].rstrip(), activity


def attach_file_activity(text: str, activity: FileActivity | None) -> str:
    """*text* followed by the file block (when there is anything to list)."""
    block = render_file_activity(activity) if activity is not None else ""
    if not block:
        return text
    body = text.rstrip()
    return f"{body}\n\n{block}" if body else block
