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
  // The form as last set, so an update is computed outside React's updater:
  // the parked copy is written from the event, never while rendering.
  const draftRef = useRef(draft)
  const savedRef = useRef(saved)
  const adoptRef = useRef(false)

  const show = useCallback((next: T) => {
    draftRef.current = next
    setDraftState(next)
  }, [])

  useEffect(() => {
    if (same(saved, savedRef.current)) return
    const wasClean = same(draft, savedRef.current)
    savedRef.current = saved
    if (adoptRef.current || wasClean || same(draft, saved)) {
      adoptRef.current = false
      show(saved)
      useDraftStore.getState().drop(id)
    }
  }, [id, saved, draft, show])

  const setDraft = useCallback(
    (update: T | ((prev: T) => T)) => {
      const next = typeof update === 'function' ? (update as (p: T) => T)(draftRef.current) : update
      show(next)
      if (same(next, savedRef.current)) useDraftStore.getState().drop(id)
      else useDraftStore.getState().put(id, next)
    },
    [id, show],
  )

  const revert = useCallback(() => {
    useDraftStore.getState().drop(id)
    show(savedRef.current)
  }, [id, show])

  /** The draft was written: take the next saved value as the form, whatever it holds. */
  const settle = useCallback(() => {
    adoptRef.current = true
    useDraftStore.getState().drop(id)
  }, [id])

  return { draft, setDraft, revert, settle, edited: !same(draft, saved) }
}
