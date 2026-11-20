import { useCallback, useEffect, useRef, useState } from 'react'
import { create } from 'zustand'

interface DraftStore {
  /** Unsaved edits per Setup card, kept while the pane is closed. */
  drafts: Record<string, unknown>
  put(id: string, value: unknown): void
  drop(id: string): void
}

export const useDraftStore = create<DraftStore>((set) => ({
  drafts: {},
  put: (id, value) => set((s) => ({ drafts: { ...s.drafts, [id]: value } })),
  drop: (id) =>
    set((s) => {
      if (!(id in s.drafts)) return s
      const next = { ...s.drafts }
      delete next[id]
      return { drafts: next }
    }),
}))

function same(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b)
}

/**
 * A card's form state over the saved value. An edit is parked in a module
 * store, so switching tabs or sections never drops it silently; a newer saved
 * value replaces the form only while nothing is being edited.
 */
export function useDraft<T>(id: string, saved: T) {
  const parked = useDraftStore((s) => s.drafts[id]) as T | undefined
  const [draft, setDraftState] = useState<T>(() => parked ?? saved)
  const savedRef = useRef(saved)
  const adoptRef = useRef(false)

  useEffect(() => {
    if (same(saved, savedRef.current)) return
    const wasClean = same(draft, savedRef.current)
    savedRef.current = saved
    if (adoptRef.current || wasClean || same(draft, saved)) {
      adoptRef.current = false
      setDraftState(saved)
      useDraftStore.getState().drop(id)
    }
  }, [id, saved, draft])

  const setDraft = useCallback(
    (update: T | ((prev: T) => T)) => {
      setDraftState((prev) => {
        const next = typeof update === 'function' ? (update as (p: T) => T)(prev) : update
        if (same(next, savedRef.current)) useDraftStore.getState().drop(id)
        else useDraftStore.getState().put(id, next)
        return next
      })
    },
    [id],
  )

  const revert = useCallback(() => {
    useDraftStore.getState().drop(id)
    setDraftState(savedRef.current)
  }, [id])

  /** The draft was written: take the next saved value as the form, whatever it holds. */
  const settle = useCallback(() => {
    adoptRef.current = true
    useDraftStore.getState().drop(id)
  }, [id])

  return { draft, setDraft, revert, settle, edited: !same(draft, saved) }
}
