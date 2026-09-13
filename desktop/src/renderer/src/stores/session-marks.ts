import { create } from 'zustand'

const KEY = 'agentos-desktop.sessionMarks'

/** What the app remembers about a session that the gateway does not. */
export interface SessionMarks {
  pinned: ReadonlySet<string>
  archived: ReadonlySet<string>
  unread: ReadonlySet<string>
}

interface Persisted {
  pinned?: unknown
  archived?: unknown
  unread?: unknown
}

function toSet(value: unknown): Set<string> {
  return new Set(Array.isArray(value) ? value.filter((x) => typeof x === 'string') : [])
}

function load(): SessionMarks {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) || '{}') as Persisted
    return { pinned: toSet(raw.pinned), archived: toSet(raw.archived), unread: toSet(raw.unread) }
  } catch {
    return { pinned: new Set(), archived: new Set(), unread: new Set() }
  }
}

function save(m: SessionMarks): void {
  try {
    localStorage.setItem(
      KEY,
      JSON.stringify({ pinned: [...m.pinned], archived: [...m.archived], unread: [...m.unread] }),
    )
  } catch {
    /* storage unavailable */
  }
}

function withKey(set: ReadonlySet<string>, key: string, on: boolean): ReadonlySet<string> {
  if (set.has(key) === on) return set
  const next = new Set(set)
  if (on) next.add(key)
  else next.delete(key)
  return next
}

interface SessionMarksStore extends SessionMarks {
  setPinned(key: string, on: boolean): void
  setArchived(key: string, on: boolean): void
  setUnread(key: string, on: boolean): void
  markAllRead(): void
  /** The session is gone: drop every mark so the sets never grow stale. */
  forget(key: string): void
}

/**
 * Pinned, archived and unread, kept on this Mac (localStorage) because the
 * gateway has no such fields. Pinned rows float to the top of the sidebar,
 * archived rows hide unless the view asks for them, unread rows read bold
 * until opened.
 */
export const useSessionMarks = create<SessionMarksStore>((set, get) => {
  const commit = (patch: Partial<SessionMarks>) => {
    const next = { ...get(), ...patch }
    save(next)
    set(patch)
  }
  return {
    ...load(),
    setPinned: (key, on) => commit({ pinned: withKey(get().pinned, key, on) }),
    setArchived: (key, on) => commit({ archived: withKey(get().archived, key, on) }),
    setUnread: (key, on) => {
      if (get().unread.has(key) === on) return
      commit({ unread: withKey(get().unread, key, on) })
    },
    markAllRead: () => {
      if (get().unread.size === 0) return
      commit({ unread: new Set() })
    },
    forget: (key) =>
      commit({
        pinned: withKey(get().pinned, key, false),
        archived: withKey(get().archived, key, false),
        unread: withKey(get().unread, key, false),
      }),
  }
})
