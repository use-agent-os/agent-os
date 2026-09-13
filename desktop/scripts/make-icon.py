"""Render the macOS app icon from the AgentOS mark's geometry.

The only mark in the repo is a 234 px PNG, too small for a 1024 px icon, so
the mark is redrawn as vectors (four discs joined by spokes) at 4x and
downsampled. Output: desktop/resources/icon.png (1024x1024, transparent
corners) — electron-builder derives the .icns from it.

    uv run python desktop/scripts/make-icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

SIZE = 1024
SS = 4  # supersampling
LIME = (205, 255, 2, 255)
GROUND_TOP = (20, 23, 16, 255)
GROUND_BOTTOM = (7, 8, 6, 255)

# Mark geometry, relative to a unit square that the original PNG occupies.
CENTER = ((0.5, 0.56), 0.117)
NODES = [((0.5, 0.195), 0.069), ((0.14, 0.80), 0.064), ((0.867, 0.80), 0.064)]
SPOKE = 0.036


def squircle(size: int, radius: float) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    return mask


def main() -> None:
    s = SIZE * SS
    # Apple's grid: the icon body sits inside a margin so it aligns with other apps.
    body = int(s * 0.805)
    offset = (s - body) // 2
    radius = body * 0.2237

    # Ground: vertical gradient inside the squircle.
    ground = Image.new("RGBA", (body, body), GROUND_BOTTOM)
    grad = ImageDraw.Draw(ground)
    for y in range(body):
        t = y / (body - 1)
        color = tuple(int(GROUND_TOP[i] * (1 - t) + GROUND_BOTTOM[i] * t) for i in range(3)) + (255,)
        grad.line([(0, y), (body, y)], fill=color)

    # A soft lime glow behind the mark, like the setup screen's hero tile.
    glow = Image.new("RGBA", (body, body), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gr = int(body * 0.34)
    cx, cy = body // 2, int(body * 0.56)
    gd.ellipse((cx - gr, cy - gr, cx + gr, cy + gr), fill=(205, 255, 2, 70))
    glow = glow.filter(ImageFilter.GaussianBlur(body * 0.12))
    ground.alpha_composite(glow)

    # The mark: spokes first, then discs on top.
    mark_size = int(body * 0.62)
    mx = (body - mark_size) // 2
    my = (body - mark_size) // 2 - int(body * 0.01)
    md = ImageDraw.Draw(ground)

    def pt(rel: tuple[float, float]) -> tuple[int, int]:
        return (mx + int(rel[0] * mark_size), my + int(rel[1] * mark_size))

    (ccx, ccy), cr = CENTER
    c = pt((ccx, ccy))
    for (nx, ny), _ in NODES:
        md.line([c, pt((nx, ny))], fill=LIME, width=int(SPOKE * mark_size))
    r = int(cr * mark_size)
    md.ellipse((c[0] - r, c[1] - r, c[0] + r, c[1] + r), fill=LIME)
    for (nx, ny), nr in NODES:
        n = pt((nx, ny))
        rr = int(nr * mark_size)
        md.ellipse((n[0] - rr, n[1] - rr, n[0] + rr, n[1] + rr), fill=LIME)

    # Inner top highlight, then clip to the squircle.
    hl = Image.new("RGBA", (body, body), (0, 0, 0, 0))
    ImageDraw.Draw(hl).rounded_rectangle(
        (0, 0, body - 1, body - 1), radius=radius, outline=(255, 255, 255, 28), width=SS * 2
    )
    ground.alpha_composite(hl)
    ground.putalpha(squircle(body, radius))

    canvas = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    # Drop shadow under the body, the way macOS icons carry one.
    shadow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    sh = squircle(body, radius)
    shadow.paste((0, 0, 0, 110), (offset, offset + int(s * 0.012)), sh)
    shadow = shadow.filter(ImageFilter.GaussianBlur(s * 0.012))
    canvas.alpha_composite(shadow)
    canvas.alpha_composite(ground, (offset, offset))

    out = canvas.resize((SIZE, SIZE), Image.LANCZOS)
    dest = Path(__file__).resolve().parents[1] / "resources" / "icon.png"
    out.save(dest)
    print(f"wrote {dest}")


if __name__ == "__main__":
    main()
