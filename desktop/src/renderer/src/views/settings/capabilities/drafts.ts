import { useCallback, useEffect, useRef, useState } from 'react'
import { create } from 'zustand'
import { useUi } from '~/stores/ui'
import { draftDirty, type CapabilityId, type CapDraft } from './logic'

/**
 * Unsaved Capabilities edits, kept while the card is not on screen: a
 * service picked before following "Open Environment" is still picked on the
 * way back. Its own store, not Memory's: Memory marks its tab when anything
 * in its store is unsaved.
 */
interface CapDraftStore {
  drafts: Partial<Record<CapabilityId, CapDraft>>
  put(id: CapabilityId, draft: CapDraft): void
  drop(id: CapabilityId): void
}

export const useCapDrafts = create<CapDraftStore>((set) => ({
  drafts: {},
  put: (id, draft) => set((s) => ({ drafts: { ...s.drafts, [id]: draft } })),
  drop: (id) =>
    set((s) => {
      if (!(id in s.drafts)) return s
      const next = { ...s.drafts }
      delete next[id]
      return { drafts: next }
    }),
}))

/** A pasted key rides along between sections, never past closing Settings. */
export function forgetPastedKeys(): void {
  useCapDrafts.setState((s) => ({
    drafts: Object.fromEntries(
      Object.entries(s.drafts).map(([id, d]) => [
        id,
        d && d.values.api_key ? { ...d, values: { ...d.values, api_key: '' } } : d,
      ]),
    ),
  }))
}

useUi.subscribe((state, prev) => {
  if (prev.settingsOpen && !state.settingsOpen) forgetPastedKeys()
})

/**
 * A card's form over its saved state. Every edit is parked (or dropped once
 * it matches what is saved again), so leaving the section and coming back
 * shows the same form. The card remounts on a new config revision, so the
 * saved state never changes under a mounted form.
 */
export function useCapDraft(id: CapabilityId, saved: CapDraft) {
  const [draft, setDraftState] = useState<CapDraft>(() => {
    const parked = useCapDrafts.getState().drafts[id]
    return parked && draftDirty(saved, parked) ? parked : saved
  })
  const draftRef = useRef(draft)
  const savedRef = useRef(saved)
  useEffect(() => {
    savedRef.current = saved
  }, [saved])

  // A parked copy that no longer differs (the key it held was forgotten, or
  // the config caught up) is not an edit any more.
  useEffect(() => {
    const parked = useCapDrafts.getState().drafts[id]
    if (parked && !draftDirty(savedRef.current, parked)) useCapDrafts.getState().drop(id)
  }, [id])

  const show = useCallback((next: CapDraft) => {
    draftRef.current = next
    setDraftState(next)
  }, [])

  const setDraft = useCallback(
    (update: CapDraft | ((prev: CapDraft) => CapDraft)) => {
      const next = typeof update === 'function' ? update(draftRef.current) : update
      show(next)
      if (draftDirty(savedRef.current, next)) useCapDrafts.getState().put(id, next)
      else useCapDrafts.getState().drop(id)
    },
    [id, show],
  )

  const revert = useCallback(() => {
    useCapDrafts.getState().drop(id)
    show(savedRef.current)
  }, [id, show])

  /**
   * Written to the gateway: nothing is parked any more, and a pasted key is
   * forgotten at once (it was sent once). The new revision remounts the card
   * from what is saved.
   */
  const settle = useCallback(() => {
    useCapDrafts.getState().drop(id)
    show({ ...draftRef.current, values: { ...draftRef.current.values, api_key: '' } })
  }, [id, show])

  return { draft, setDraft, revert, settle }
}
