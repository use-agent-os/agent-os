import { create } from 'zustand'

// Side panel state (overhaul-control-ui D7, chat spec "Side panel").

export type PanelTabKind = 'tool-output' | 'artifact' | 'thinking'

export interface PanelTab {
  kind: PanelTabKind
  /** Item id within the transcript model. */
  id: string
  title: string
}

export const SIDE_PANEL_WIDTH_KEY = 'agentos.ui.sidePanelWidth'
export const SIDE_PANEL_MIN = 320
export const SIDE_PANEL_MAX = 900
export const SIDE_PANEL_DEFAULT = 440

export function clampPanelWidth(width: number): number {
  if (!Number.isFinite(width)) return SIDE_PANEL_DEFAULT
  return Math.round(Math.max(SIDE_PANEL_MIN, Math.min(SIDE_PANEL_MAX, width)))
}

function readWidth(): number {
  try {
    const raw = localStorage.getItem(SIDE_PANEL_WIDTH_KEY)
    return raw ? clampPanelWidth(Number(raw)) : SIDE_PANEL_DEFAULT
  } catch {
    return SIDE_PANEL_DEFAULT
  }
}

interface PanelState {
  tabs: PanelTab[]
  activeId: string | null
  width: number
  /** The element that opened the panel, to return focus to on close. */
  opener: HTMLElement | null
  open(tab: PanelTab, opener?: HTMLElement | null): void
  closeTab(id: string): void
  closeAll(): void
  setActive(id: string): void
  setWidth(width: number, persist?: boolean): void
}

export const usePanels = create<PanelState>((set, get) => ({
  tabs: [],
  activeId: null,
  width: readWidth(),
  opener: null,
  open(tab, opener) {
    const tabs = get().tabs.some((t) => t.id === tab.id) ? get().tabs : [...get().tabs, tab]
    set({ tabs, activeId: tab.id, opener: opener ?? get().opener })
  },
  closeTab(id) {
    const tabs = get().tabs.filter((t) => t.id !== id)
    const activeId = get().activeId === id ? (tabs.at(-1)?.id ?? null) : get().activeId
    set({ tabs, activeId })
  },
  closeAll() {
    set({ tabs: [], activeId: null })
  },
  setActive(activeId) {
    set({ activeId })
  },
  setWidth(width, persist = true) {
    const next = clampPanelWidth(width)
    set({ width: next })
    if (!persist) return
    try {
      localStorage.setItem(SIDE_PANEL_WIDTH_KEY, String(next))
    } catch {
      /* storage unavailable */
    }
  },
}))

export function resetPanelsForTests(): void {
  usePanels.setState({ tabs: [], activeId: null, width: readWidth(), opener: null })
}
