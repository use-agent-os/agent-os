import { create } from 'zustand'

// Side panel and Details state (overhaul-control-ui D7, chat spec "Side
// panel", "Details column").

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
/** The chat pane is wide enough to dock Details beside the transcript. */
export const DETAILS_DOCK_MIN = 1040

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
  /** Per-session explicit Details choice: true open, false closed. */
  details: Record<string, boolean>
  open(tab: PanelTab, opener?: HTMLElement | null): void
  closeTab(id: string): void
  closeAll(): void
  setActive(id: string): void
  setWidth(width: number, persist?: boolean): void
  setDetails(sessionKey: string, open: boolean): void
}

export const usePanels = create<PanelState>((set, get) => ({
  tabs: [],
  activeId: null,
  width: readWidth(),
  opener: null,
  details: {},
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
  setDetails(sessionKey, open) {
    set({ details: { ...get().details, [sessionKey]: open } })
  },
}))

/**
 * Whether Details shows for a session: an explicit choice for that session
 * wins (an explicit close sticks); otherwise it opens by itself only when it
 * can dock.
 */
export function detailsVisible(explicit: boolean | undefined, canDock: boolean): boolean {
  return explicit ?? canDock
}

export function resetPanelsForTests(): void {
  usePanels.setState({ tabs: [], activeId: null, width: readWidth(), opener: null, details: {} })
}
