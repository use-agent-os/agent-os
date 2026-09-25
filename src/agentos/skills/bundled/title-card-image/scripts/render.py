#!/usr/bin/env python3
"""Render a title/ending card as a static PNG using Pillow.

Designed as the "cover" and "ending" PNG generators for meta-short-drama:
deterministic, free, no API, CJK-friendly font fallback. The output PNG
can then be fed to video-still-animator to become a short MP4 clip.

Usage:
    python render.py --text "咖啡店偶遇" --output cover.png \\
        [--subtitle "短剧"] [--background "#101018"] \\
        [--text-color "#ffffff"] [--font-size 96] \\
        [--width 720] [--height 1280]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Bundled scripts run under AgentOS's own interpreter; the path insert only
# matters in a source checkout where the package is not installed (#2804).
_SRC_ROOT = str(Path(__file__).resolve().parents[5])
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)
from agentos.skill_stdio import configure_utf8_stdio  # noqa: E402

_CJK_FONT_CANDIDATES = (
    # Windows
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/Library/Fonts/Songti.ttc",
    # Linux common
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
)


def _pick_font(size: int, explicit: str | None = None):
    from PIL import ImageFont  # type: ignore
    if explicit:
        try:
            return ImageFont.truetype(explicit, size)
        except OSError:
            print(f"Warning: --font {explicit!r} not loadable; falling back.", file=sys.stderr)
    for candidate in _CJK_FONT_CANDIDATES:
        if os.path.isfile(candidate):
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    # Final fallback — bundled bitmap; CJK characters render as tofu but doesn't crash.
    return ImageFont.load_default()


def _parse_color(spec: str) -> tuple[int, int, int]:
    s = spec.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        raise ValueError(f"invalid color: {spec!r}")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _is_cjk_ideograph(char: str) -> bool:
    """Return True only for CJK Unified Ideograph code points.

    Hangul (U+AC00–U+D7AF), Cyrillic, emoji, and other non-ideographic scripts
    are excluded so they take the whitespace-wrap path instead of the fixed-width
    CJK chunker.
    """
    cp = ord(char)
    return (
        0x4E00 <= cp <= 0x9FFF       # CJK Unified Ideographs
        or 0x3400 <= cp <= 0x4DBF    # CJK Unified Ideographs Extension A
        or 0xF900 <= cp <= 0xFAFF    # CJK Compatibility Ideographs
        or 0x20000 <= cp <= 0x2A6DF  # CJK Unified Ideographs Extension B
        or 0x2A700 <= cp <= 0x2B73F  # Extension C
        or 0x2B740 <= cp <= 0x2B81F  # Extension D
        or 0x2B820 <= cp <= 0x2CEAF  # Extension E
        or 0x2CEB0 <= cp <= 0x2EBEF  # Extension F
        or 0x30000 <= cp <= 0x3134F  # Extension G
    )


def _wrap_text(text: str, max_chars: int) -> list[str]:
    """Greedy line wrap that respects CJK (no spaces) and ASCII (whitespace)."""
    if not text:
        return [""]
    # If text already has explicit newlines, honour them.
    if "\n" in text:
        return text.split("\n")
    # Use the fixed-width chunker only when the text is predominantly CJK
    # ideographs.  Space-delimited scripts (Latin, Cyrillic, Hangul, etc.)
    # and emoji take the whitespace-wrap path.
    if not any(_is_cjk_ideograph(c) for c in text):
        words = text.split()
        out: list[str] = []
        line = ""
        for word in words:
            candidate = f"{line} {word}".strip()
            if len(candidate) <= max_chars:
                line = candidate
            else:
                if line:
                    out.append(line)
                line = word
        if line:
            out.append(line)
        return out or [text]
    # CJK / mixed: break at character count.
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]


def _stack_height(
    title_size: int, sub_size: int, title_lines: list[str], sub_lines: list[str]
) -> int:
    """Pixel height of the title (+ subtitle, if any) stacked with their gaps."""
    gap_t = int(title_size * 0.25)
    gap_s = int(sub_size * 0.25)
    pad = int(title_size * 0.6)
    height = title_size * len(title_lines) + gap_t * max(0, len(title_lines) - 1)
    if sub_lines:
        height += pad + sub_size * len(sub_lines) + gap_s * max(0, len(sub_lines) - 1)
    return height


def fit_stack_to_height(
    title_size: int,
    sub_size: int,
    title_lines: list[str],
    sub_lines: list[str],
    canvas_height: int,
    *,
    shrink_floor: int = 12,
) -> tuple[int, int, bool]:
    """Shrink ``title_size``/``sub_size`` in lockstep until the stacked lines fit
    ``canvas_height``, or until neither can shrink further.

    Wrapping is character-count based (see ``_wrap_text``), so shrinking a font
    size only makes each line narrower and shorter -- it never changes how many
    lines there are. That makes the stacked height a simple, monotonically
    decreasing function of the two sizes, so lockstep shrinking is enough; no
    search over the wrap width is needed here the way ``_fit_font`` needs one.

    Returns ``(title_size, sub_size, fits)`` -- ``fits`` is False only when the
    stack still exceeds ``canvas_height`` at the floor, i.e. there is no font
    size this function can offer that makes it fit.
    """
    while (
        title_size > shrink_floor or (sub_lines and sub_size > shrink_floor)
    ) and _stack_height(title_size, sub_size, title_lines, sub_lines) > canvas_height:
        if title_size > shrink_floor:
            title_size = max(shrink_floor, int(title_size * 0.92))
        if sub_lines and sub_size > shrink_floor:
            sub_size = max(shrink_floor, int(sub_size * 0.92))
    fits = _stack_height(title_size, sub_size, title_lines, sub_lines) <= canvas_height
    return title_size, sub_size, fits


def main() -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text", required=True, help="Main headline text.")
    parser.add_argument("--output", "-o", required=True)
    parser.add_argument("--subtitle", default="", help="Smaller line under the headline.")
    parser.add_argument("--background", default="#101018", help="Hex color #RRGGBB.")
    parser.add_argument("--text-color", default="#ffffff")
    parser.add_argument("--subtitle-color", default="#c8c8d0")
    parser.add_argument("--font-size", type=int, default=80)
    parser.add_argument("--subtitle-size", type=int, default=32)
    parser.add_argument("--width", type=int, default=720)
    parser.add_argument("--height", type=int, default=1280)
    parser.add_argument(
        "--max-chars-per-line", type=int, default=12,
        help="Soft wrap threshold for the headline (CJK char count). "
             "Subtitle uses 1.5× this value.",
    )
    parser.add_argument(
        "--auto-shrink", default="yes", choices=["yes", "no"],
        help="When rendered text exceeds 88%% of canvas width, or the stacked "
        "lines exceed the canvas height, shrink the font until it fits. "
        "Default yes.",
    )
    parser.add_argument("--font", default=None, help="Optional explicit font path.")
    args = parser.parse_args()

    try:
        from PIL import Image, ImageDraw  # type: ignore
    except ImportError as exc:
        print(f"Error: Pillow not installed ({exc}).", file=sys.stderr)
        return 1

    try:
        bg = _parse_color(args.background)
        fg = _parse_color(args.text_color)
        sfg = _parse_color(args.subtitle_color)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    img = Image.new("RGB", (args.width, args.height), color=bg)
    draw = ImageDraw.Draw(img)

    title_lines = _wrap_text(args.text, args.max_chars_per_line)
    sub_lines = (
        _wrap_text(args.subtitle, int(args.max_chars_per_line * 1.5))
        if args.subtitle else []
    )

    def _max_text_width(lines, font) -> int:
        widest = 0
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=font)
            widest = max(widest, bbox[2] - bbox[0])
        return widest

    def _fit_font(size: int, lines: list[str]) -> tuple[int, object]:
        max_safe = int(args.width * 0.88)
        font = _pick_font(size, args.font)
        if args.auto_shrink == "no" or not lines:
            return size, font
        # Shrink until rendered max line fits.
        while size > 12 and _max_text_width(lines, font) > max_safe:
            size = int(size * 0.92)
            font = _pick_font(size, args.font)
        return size, font

    title_size, font_title = _fit_font(args.font_size, title_lines)
    sub_size, font_sub = _fit_font(args.subtitle_size, sub_lines) if sub_lines else (args.subtitle_size, None)

    # The width-based shrink above can still leave the *stacked* lines taller
    # than the canvas -- it only ever looked at how wide a line renders, never
    # how many lines there are stacked up. Shrink further, in lockstep, until
    # the stack fits the requested height too -- the same "auto-shrink"
    # promise already applied to width, extended to the other dimension of
    # the same canvas.
    if args.auto_shrink == "yes":
        title_size, sub_size, fits = fit_stack_to_height(
            title_size, sub_size, title_lines, sub_lines, args.height
        )
        font_title = _pick_font(title_size, args.font)
        if sub_lines:
            font_sub = _pick_font(sub_size, args.font)
        if not fits:
            print(
                f"Warning: text still exceeds the {args.height}px canvas height "
                "at the minimum font size (12px); some lines will render "
                "outside the image.",
                file=sys.stderr,
            )

    # Stack all lines, vertically centered, using the (potentially shrunken) sizes.
    line_gap_title = int(title_size * 0.25)
    line_gap_sub = int(sub_size * 0.25)
    pad_between_groups = int(title_size * 0.6)

    total_h = _stack_height(title_size, sub_size, title_lines, sub_lines)
    y = (args.height - total_h) // 2

    def _draw_line(text: str, font, color, y_pos: int) -> None:
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        x = (args.width - text_w) // 2
        draw.text((x, y_pos), text, fill=color, font=font)

    for line in title_lines:
        _draw_line(line, font_title, fg, y)
        y += title_size + line_gap_title

    if sub_lines:
        y += pad_between_groups - line_gap_title
        for line in sub_lines:
            _draw_line(line, font_sub, sfg, y)
            y += sub_size + line_gap_sub

    out_path = Path(args.output).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")
    print(str(out_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
