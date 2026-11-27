import { create } from 'zustand'

const KEY = 'agentos-desktop.composerDrafts'

/** The slot of the keyless home: a new session has no key until its first send. */
export const NEW_SESSION_DRAFT = 'new'

/** Least recently edited drafts beyond this are dropped. */
export const MAX_DRAFTS = 50

type Drafts = Readonly<Record<string, string>>

function load(): Drafts {
  try {
    const raw: unknown = JSON.parse(localStorage.getItem(KEY) || '{}')
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {}
    const out: Record<string, string> = {}
    for (const [k, v] of Object.entries(raw)) {
      if (typeof v === 'string' && v) out[k] = v
    }
    return out
  } catch {
    return {}
  }
}

function save(drafts: Drafts): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(drafts))
  } catch {
    /* storage unavailable or full: the draft still lives in memory */
  }
}

interface ComposerDraftsStore {
  drafts: Drafts
  /** Keeps `text` as the slot's draft; an empty text drops the slot. */
  setDraft(slot: string, text: string): void
  /** Hands one slot's draft to another (the home's session got its key). */
  moveDraft(from: string, to: string): void
  /** The session is gone: drop its draft. */
  forget(slot: string): void
}

/**
 * What was typed into the composer and not sent, per session, kept on this
 * Mac (localStorage). The composer's own state dies with the chat view (any
 * other page unmounts it) and is shared by every session the view shows, so
 * without this a draft is lost on the way to Settings and follows the user
 * into whichever session they open next.
 */
export const useComposerDrafts = create<ComposerDraftsStore>((set, get) => {
  const commit = (drafts: Drafts) => {
    save(drafts)
    set({ drafts })
  }
  return {
    drafts: load(),
    setDraft: (slot, text) => {
      const current = get().drafts
      if ((current[slot] ?? '') === text) return
      // Re-inserted last, so insertion order is edit order.
      const next: Record<string, string> = { ...current }
      delete next[slot]
      if (text) next[slot] = text
      const keys = Object.keys(next)
      for (const stale of keys.slice(0, Math.max(0, keys.length - MAX_DRAFTS))) delete next[stale]
      commit(next)
    },
    moveDraft: (from, to) => {
      const text = get().drafts[from]
      if (!text || from === to) return
      get().setDraft(from, '')
      get().setDraft(to, text)
    },
    forget: (slot) => get().setDraft(slot, ''),
  }
})

export function readDraft(slot: string): string {
  return useComposerDrafts.getState().drafts[slot] ?? ''
}
