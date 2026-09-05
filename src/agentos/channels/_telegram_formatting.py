"""Safe Markdown-to-HTML rendering for Telegram Bot API messages."""

from __future__ import annotations

import html
import re

_TABLE_DELIMITER_RE = re.compile(r"^:?-{3,}:?$")
_FENCE_RE = re.compile(r"^\s*```(?P<language>[A-Za-z0-9_+-]{0,32})\s*$")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(?P<text>.+?)\s*#*\s*$")
_ORDERED_LIST_RE = re.compile(r"^(?P<indent>\s*)(?P<number>\d+)[.)]\s+(?P<text>.+)$")
_UNORDERED_LIST_RE = re.compile(r"^(?P<indent>\s*)[-+*]\s+(?P<text>.+)$")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)<]+)\)")


def _replace_code_spans(text: str) -> tuple[str, list[str]]:
    """Replace balanced Markdown code spans with private placeholders."""
    chunks: list[str] = []
    output: list[str] = []
    cursor = 0
    while cursor < len(text):
        if text[cursor] != "`":
            output.append(text[cursor])
            cursor += 1
            continue
        marker_end = cursor
        while marker_end < len(text) and text[marker_end] == "`":
            marker_end += 1
        marker = text[cursor:marker_end]
        closing = text.find(marker, marker_end)
        if closing < 0:
            output.append(marker)
            cursor = marker_end
            continue
        content = text[marker_end:closing].strip()
        placeholder = f"\x00TG_CODE_{len(chunks)}\x00"
        chunks.append(f"<code>{html.escape(content)}</code>")
        output.append(placeholder)
        cursor = closing + len(marker)
    return "".join(output), chunks


def _render_inline(text: str) -> str:
    protected, code_chunks = _replace_code_spans(text)
    rendered = html.escape(protected)
    rendered = _LINK_RE.sub(r'<a href="\2">\1</a>', rendered)
    rendered = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<b>\1</b>", rendered)
    rendered = re.sub(r"__(?=\S)(.+?)(?<=\S)__", r"<b>\1</b>", rendered)
    rendered = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<s>\1</s>", rendered)
    rendered = re.sub(r"(?<!\*)\*(?=\S)(.+?)(?<=\S)\*(?!\*)", r"<i>\1</i>", rendered)
    for index, chunk in enumerate(code_chunks):
        rendered = rendered.replace(f"\x00TG_CODE_{index}\x00", chunk)
    return rendered


def _plain_inline(text: str) -> str:
    """Remove common inline Markdown markers for table labels."""
    text = _LINK_RE.sub(r"\1 (\2)", text)
    text = text.replace("`", "")
    for marker in ("**", "__", "~~"):
        text = text.replace(marker, "")
    return text.strip()


def _split_table_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith(r"\|"):
        stripped = stripped[:-1]

    cells: list[str] = []
    current: list[str] = []
    escaped = False
    code_marker_length = 0
    cursor = 0
    while cursor < len(stripped):
        char = stripped[cursor]
        if escaped:
            current.append(char)
            escaped = False
            cursor += 1
            continue
        if char == "\\":
            escaped = True
            current.append(char)
            cursor += 1
            continue
        if char == "`":
            marker_end = cursor
            while marker_end < len(stripped) and stripped[marker_end] == "`":
                marker_end += 1
            marker_length = marker_end - cursor
            if code_marker_length == 0:
                code_marker_length = marker_length
            elif code_marker_length == marker_length:
                code_marker_length = 0
            current.append(stripped[cursor:marker_end])
            cursor = marker_end
            continue
        if char == "|" and code_marker_length == 0:
            cells.append("".join(current).strip().replace(r"\|", "|"))
            current = []
        else:
            current.append(char)
        cursor += 1
    cells.append("".join(current).strip().replace(r"\|", "|"))
    return cells


def _is_table_start(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines) or "|" not in lines[index]:
        return False
    header = _split_table_row(lines[index])
    delimiter = _split_table_row(lines[index + 1])
    return (
        len(header) >= 2
        and len(header) == len(delimiter)
        and all(_TABLE_DELIMITER_RE.fullmatch(cell) for cell in delimiter)
    )


def _render_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    clean_headers = [_plain_inline(header) for header in headers]
    if len(headers) == 2:
        rendered = [
            f"<b>{html.escape(clean_headers[0])} — {html.escape(clean_headers[1])}</b>"
        ]
        for row in rows:
            # Pad ragged rows so single-cell rows do not crash tuple unpack.
            padded = (row + ["", ""])[:2]
            label, value = padded
            clean_label = _plain_inline(label)
            if clean_label:
                rendered.append(f"<b>{html.escape(clean_label)}:</b> {_render_inline(value)}")
            elif value:
                rendered.append(_render_inline(value))
        return rendered

    rendered = [f"<b>{' · '.join(html.escape(header) for header in clean_headers)}</b>"]
    for row in rows:
        # Pad or truncate ragged rows so column-count mismatches do not raise.
        padded = row[:len(clean_headers)] + [""] * (len(clean_headers) - len(row))
        cells = [
            f"<b>{html.escape(header)}:</b> {_render_inline(value)}"
            for header, value in zip(clean_headers, padded)
            if value
        ]
        if cells:
            rendered.append(" · ".join(cells))
    return rendered


def render_telegram_html(markdown: str) -> str:
    """Render a safe, mobile-friendly Telegram HTML subset from Markdown."""
    lines = markdown.splitlines()
    rendered: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        fence = _FENCE_RE.match(line)
        if fence:
            language = fence.group("language")
            code_lines: list[str] = []
            index += 1
            while index < len(lines) and not _FENCE_RE.match(lines[index]):
                code_lines.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            code = html.escape("\n".join(code_lines))
            if language:
                rendered.append(f'<pre><code class="language-{language}">{code}</code></pre>')
            else:
                rendered.append(f"<pre>{code}</pre>")
            continue

        if _is_table_start(lines, index):
            headers = _split_table_row(line)
            rows: list[list[str]] = []
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                row = _split_table_row(lines[index])
                # Pad or truncate ragged rows instead of aborting the table.
                if len(row) != len(headers):
                    row = row[:len(headers)] + [""] * (len(headers) - len(row))
                rows.append(row)
                index += 1
            rendered.extend(_render_table(headers, rows))
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            rendered.append(f"<b>{_render_inline(heading.group('text'))}</b>")
            index += 1
            continue
        if line.startswith("> "):
            rendered.append(f"<blockquote>{_render_inline(line[2:])}</blockquote>")
            index += 1
            continue
        ordered = _ORDERED_LIST_RE.match(line)
        if ordered:
            rendered.append(
                f"{ordered.group('indent')}{ordered.group('number')}. "
                f"{_render_inline(ordered.group('text'))}"
            )
            index += 1
            continue
        unordered = _UNORDERED_LIST_RE.match(line)
        if unordered:
            rendered.append(
                f"{unordered.group('indent')}• {_render_inline(unordered.group('text'))}"
            )
            index += 1
            continue
        rendered.append(_render_inline(line))
        index += 1
    return "\n".join(rendered)
