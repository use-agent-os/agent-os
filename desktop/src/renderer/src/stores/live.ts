import { create } from 'zustand'

interface LiveStore {
  /** Session ids with a turn currently streaming. */
  ids: ReadonlySet<string>
  setLive(id: string, live: boolean): void
}

/**
 * Which sessions are mid-turn right now. The sidebar reads it to light the
 * session's dot; whoever owns the turn (today the placeholder in SessionView,
 * later the gateway event stream) writes it.
 */
export const useLive = create<LiveStore>((set) => ({
  ids: new Set(),
  setLive(id, live) {
    set((s) => {
      if (s.ids.has(id) === live) return s
      const next = new Set(s.ids)
      if (live) next.add(id)
      else next.delete(id)
      return { ids: next }
    })
  },
}))
