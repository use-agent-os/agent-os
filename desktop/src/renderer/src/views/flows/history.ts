/**
 * Undo/redo over whole-graph snapshots, the change tracker's posture: one
 * entry per committed change (a drop, a link, a delete, a value), capped at
 * 50, and a snapshot equal to the present is not a change. The viewport is
 * never part of a snapshot, so panning and zooming never fill the stack.
 *
 * Typing is coalesced: a commit carrying the same `coalesce` key as the one
 * before it (the same widget being edited, within the window) replaces the
 * present instead of pushing, so one word is one undo step, not five.
 */

export const HISTORY_LIMIT = 50
export const COALESCE_MS = 1200

export interface History<T> {
  past: T[]
  present: T
  future: T[]
  /** Comparison key of `present`. */
  key: string
  lastCoalesce: string | null
  lastAt: number
}

export function createHistory<T>(present: T, keyOf: (s: T) => string): History<T> {
  return { past: [], present, future: [], key: keyOf(present), lastCoalesce: null, lastAt: 0 }
}

export interface CommitOptions {
  coalesce?: string
  now?: number
  limit?: number
}

export function commit<T>(
  h: History<T>,
  next: T,
  keyOf: (s: T) => string,
  opts: CommitOptions = {},
): History<T> {
  const key = keyOf(next)
  if (key === h.key) return h
  const now = opts.now ?? Date.now()
  const limit = opts.limit ?? HISTORY_LIMIT
  const coalesce = opts.coalesce ?? null
  if (
    coalesce !== null &&
    coalesce === h.lastCoalesce &&
    now - h.lastAt <= COALESCE_MS &&
    h.past.length > 0
  ) {
    return { ...h, present: next, key, future: [], lastAt: now }
  }
  const past = [...h.past, h.present]
  while (past.length > limit) past.shift()
  return { past, present: next, future: [], key, lastCoalesce: coalesce, lastAt: now }
}

export function canUndo<T>(h: History<T>): boolean {
  return h.past.length > 0
}

export function canRedo<T>(h: History<T>): boolean {
  return h.future.length > 0
}

export function undo<T>(h: History<T>, keyOf: (s: T) => string): History<T> {
  if (!h.past.length) return h
  const present = h.past[h.past.length - 1]!
  return {
    past: h.past.slice(0, -1),
    present,
    future: [h.present, ...h.future],
    key: keyOf(present),
    lastCoalesce: null,
    lastAt: 0,
  }
}

export function redo<T>(h: History<T>, keyOf: (s: T) => string): History<T> {
  if (!h.future.length) return h
  const present = h.future[0]!
  return {
    past: [...h.past, h.present],
    present,
    future: h.future.slice(1),
    key: keyOf(present),
    lastCoalesce: null,
    lastAt: 0,
  }
}

/** Start over from a new present (a reload, a restored graph from elsewhere). */
export function reset<T>(present: T, keyOf: (s: T) => string): History<T> {
  return createHistory(present, keyOf)
}
