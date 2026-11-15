// Where the pet sits, as a fraction of the room it has: 0 is the left/top
// edge, 1 the right/bottom edge. A window that grows or shrinks moves the
// pet with it and can never leave it off screen, which absolute pixels did.

export interface Size {
  w: number
  h: number
}

export interface Point {
  x: number
  y: number
}

/** Position as a share of the free space in each axis, 0..1. */
export interface PetAnchor {
  fx: number
  fy: number
}

/** Bottom-right corner, a little in from the edges, where the pet starts. */
export const DEFAULT_ANCHOR: PetAnchor = { fx: 1, fy: 1 }
/** Default inset from the window edges (the old fixed right/bottom offsets). */
export const DEFAULT_INSET: Point = { x: 28, y: 96 }

function clamp01(n: number): number {
  return Number.isFinite(n) ? Math.min(1, Math.max(0, n)) : 0
}

/** The pixels the pet can move through in each axis; 0 when it is bigger than the window. */
function room(size: Size, win: Size): Size {
  return { w: Math.max(0, win.w - size.w), h: Math.max(0, win.h - size.h) }
}

export function anchorFromPixels(pos: Point, size: Size, win: Size): PetAnchor {
  const r = room(size, win)
  return {
    fx: r.w > 0 ? clamp01(pos.x / r.w) : 0,
    fy: r.h > 0 ? clamp01(pos.y / r.h) : 0,
  }
}

/** Whole pixels, always inside the window. */
export function pixelsFromAnchor(anchor: PetAnchor, size: Size, win: Size): Point {
  const r = room(size, win)
  return { x: Math.round(clamp01(anchor.fx) * r.w), y: Math.round(clamp01(anchor.fy) * r.h) }
}

/** Where a pet with no saved spot goes: the default corner, inset. */
export function defaultAnchor(size: Size, win: Size): PetAnchor {
  return anchorFromPixels(
    { x: win.w - size.w - DEFAULT_INSET.x, y: win.h - size.h - DEFAULT_INSET.y },
    size,
    win,
  )
}

/**
 * Read a saved position. New saves are anchors; an older save holds the
 * pixels of the window it was dragged in, converted against the window it
 * is read in (the best guess available, and clamped either way).
 */
export function parseStoredAnchor(raw: unknown, size: Size, win: Size): PetAnchor | null {
  if (!raw || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  if (typeof o.fx === 'number' && typeof o.fy === 'number') {
    return { fx: clamp01(o.fx), fy: clamp01(o.fy) }
  }
  if (typeof o.x === 'number' && typeof o.y === 'number') {
    return anchorFromPixels({ x: o.x, y: o.y }, size, win)
  }
  return null
}

/**
 * What the mascot must not take a click away from when the press lands on a
 * see-through part of its box.
 *
 * The pet is a 192×208 sprite floating at the top of the stacking order. When
 * its whole box took presses, anything it stood over stopped responding: the
 * composer's route button, the "Inspect tx" and "View transaction" buttons on
 * a ledger card. So over a control the pet only answers a press on its own
 * drawn pixels (see petClaimsPress); the transparent rest of the box belongs
 * to whatever is underneath.
 */
export const PET_YIELDS_TO =
  'button, a[href], input, select, textarea, summary, label, [role="button"],' +
  ' [role="tab"], [role="menuitem"], [role="menuitemradio"], [role="checkbox"],' +
  ' [role="switch"], [role="link"], [role="option"], [contenteditable="true"],' +
  ' [tabindex]:not([tabindex="-1"])'

/** True when this point of the window belongs to a control, not to the pet. */
export function petYieldsAt(target: Element | null): boolean {
  if (!target) return false
  const control = target.closest(PET_YIELDS_TO)
  // The pet is itself a <button>; it must not read itself as a reason to yield.
  return control !== null && !control.classList.contains('pet')
}

/** Is this point inside the pet's box? */
export function pointInRect(rect: DOMRect, x: number, y: number): boolean {
  return x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom
}

/**
 * Does the pet take this press?
 *
 * Over plain content, anywhere in its box will do. Over a control, only a
 * press on the drawn sprite counts: yielding there too made the pet
 * ungrabbable wherever every pixel is a control, such as the sidebar's
 * project and session rows. `opaque` is null when the sheet's pixels cannot
 * be read; the control wins then, as it always did.
 */
export function petClaimsPress(onControl: boolean, opaque: boolean | null): boolean {
  return !onControl || opaque === true
}

/** A rectangle of sprite-sheet pixels. */
export interface SheetBox {
  x: number
  y: number
  w: number
  h: number
}

/**
 * The patch of the sprite sheet under a press, `slop` display pixels either
 * side of it, clipped to the frame the press landed in. Null when the press
 * maps outside the sheet.
 *
 * `local` is the press relative to the pet's box, `bg` the current
 * background-position (the walk steps its x), `drawn` the background-size,
 * `natural` the sheet's real size and `frame` one frame's real size.
 */
export function sheetHitBox(
  local: Point,
  bg: Point,
  drawn: Size,
  natural: Size,
  frame: Size,
  slop: number,
): SheetBox | null {
  if (drawn.w <= 0 || drawn.h <= 0) return null
  const kx = natural.w / drawn.w
  const ky = natural.h / drawn.h
  const cx = Math.floor((local.x - bg.x) * kx)
  const cy = Math.floor((local.y - bg.y) * ky)
  if (cx < 0 || cy < 0 || cx >= natural.w || cy >= natural.h) return null
  const fx = Math.floor(cx / frame.w) * frame.w
  const fy = Math.floor(cy / frame.h) * frame.h
  const x0 = Math.max(fx, Math.floor(cx - slop * kx))
  const y0 = Math.max(fy, Math.floor(cy - slop * ky))
  const x1 = Math.min(fx + frame.w - 1, Math.ceil(cx + slop * kx))
  const y1 = Math.min(fy + frame.h - 1, Math.ceil(cy + slop * ky))
  return { x: x0, y: y0, w: x1 - x0 + 1, h: y1 - y0 + 1 }
}
