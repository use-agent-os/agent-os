import { create } from 'zustand'
import { useUi } from '~/stores/ui'

export const MEMORY_TABS = ['remembered', 'knowledge', 'try', 'setup'] as const
export type MemoryTab = (typeof MEMORY_TABS)[number]

interface MemoryTabStore {
  tab: MemoryTab
  setTab(tab: MemoryTab): void
}

/** The Memory section's open tab. Module-level, so a link can pick it before the pane mounts. */
export const useMemoryTab = create<MemoryTabStore>((set) => ({
  tab: 'remembered',
  setTab: (tab) => set({ tab }),
}))

/** Open Settings › Memory on one tab (Overview links straight to Setup). */
export function openMemoryTab(tab: MemoryTab): void {
  useMemoryTab.getState().setTab(tab)
  useUi.getState().openSettings('memory')
}
