import { create } from 'zustand'
import { useUi } from '~/stores/ui'

export const MEMORY_TABS = ['remembered', 'knowledge', 'try', 'setup'] as const
export type MemoryTab = (typeof MEMORY_TABS)[number]

/** A control a tab should bring into view and focus once it is shown. */
export type MemoryFocus = 'limit-work' | 'limit-you'

interface MemoryTabStore {
  tab: MemoryTab
  /** Asked for by a jump ("Raise the limit"); the tab that owns it clears it. */
  focus: MemoryFocus | null
  setTab(tab: MemoryTab, focus?: MemoryFocus): void
  clearFocus(): void
}

/** The Memory section's open tab. Module-level, so a link can pick it before the pane mounts. */
export const useMemoryTab = create<MemoryTabStore>((set) => ({
  tab: 'remembered',
  focus: null,
  setTab: (tab, focus) => set({ tab, focus: focus ?? null }),
  clearFocus: () => set({ focus: null }),
}))

/** Open Settings › Memory on one tab (Overview links straight to Setup). */
export function openMemoryTab(tab: MemoryTab): void {
  useMemoryTab.getState().setTab(tab)
  useUi.getState().openSettings('memory')
}
