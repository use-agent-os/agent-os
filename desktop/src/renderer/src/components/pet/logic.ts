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
