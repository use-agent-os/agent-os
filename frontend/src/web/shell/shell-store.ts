import { create } from 'zustand'
import { NO_FILTERS, type SessionFilters } from './session-filters'

// Shell layout state (overhaul-control-ui D6). Only the list-pane width is
// remembered; visibility is component state, so every fresh load starts with
// the pane expanded.

export type ListView = 'sessions' | 'pages'

export const LIST_PANE_WIDTH_KEY = 'agentos.ui.listPaneWidth'
export const LIST_PANE_MIN = 240
export const LIST_PANE_MAX = 420
export const LIST_PANE_DEFAULT = 288

export function clampPaneWidth(width: number): number {
  if (!Number.isFinite(width)) return LIST_PANE_DEFAULT
  return Math.round(Math.max(LIST_PANE_MIN, Math.min(LIST_PANE_MAX, width)))
}

function readPaneWidth(): number {
  try {
    const raw = localStorage.getItem(LIST_PANE_WIDTH_KEY)
    return raw ? clampPaneWidth(Number(raw)) : LIST_PANE_DEFAULT
  } catch {
    return LIST_PANE_DEFAULT
  }
}

interface ShellState {
  view: ListView
  paneOpen: boolean
  paneWidth: number
  /** The agent New session preselects. */
  activeAgent: string | null
  filters: SessionFilters
  drawerOpen: boolean
  paletteOpen: boolean
  shortcutsOpen: boolean
  /** Re-selecting the open view collapses the pane; otherwise open that view. */
  selectView(view: ListView): void
  togglePane(): void
  setPaneWidth(width: number, persist?: boolean): void
  chooseAgent(agent: string | null): void
  setFilters(patch: Partial<SessionFilters>): void
  clearFilters(): void
  setDrawerOpen(open: boolean): void
  setPaletteOpen(open: boolean): void
  setShortcutsOpen(open: boolean): void
}

export const useShell = create<ShellState>((set, get) => ({
  view: 'sessions',
  paneOpen: true,
  paneWidth: readPaneWidth(),
  activeAgent: null,
  filters: NO_FILTERS,
  drawerOpen: false,
  paletteOpen: false,
  shortcutsOpen: false,
  selectView(view) {
    const { view: current, paneOpen } = get()
    if (paneOpen && current === view) set({ paneOpen: false })
    else set({ view, paneOpen: true })
  },
  togglePane() {
    set({ paneOpen: !get().paneOpen })
  },
  setPaneWidth(width, persist = true) {
    const paneWidth = clampPaneWidth(width)
    set({ paneWidth })
    if (!persist) return
    try {
      localStorage.setItem(LIST_PANE_WIDTH_KEY, String(paneWidth))
    } catch {
      /* storage unavailable: the width still applies to this tab */
    }
  },
  chooseAgent(agent) {
    set({ activeAgent: agent, filters: { ...get().filters, agent } })
  },
  setFilters(patch) {
    set({ filters: { ...get().filters, ...patch } })
  },
  clearFilters() {
    set({ filters: NO_FILTERS })
  },
  setDrawerOpen(drawerOpen) {
    set({ drawerOpen })
  },
  setPaletteOpen(paletteOpen) {
    set({ paletteOpen })
  },
  setShortcutsOpen(shortcutsOpen) {
    set({ shortcutsOpen })
  },
}))

/** Test seam: back to a fresh load. */
export function resetShellForTests(): void {
  useShell.setState({
    view: 'sessions',
    paneOpen: true,
    paneWidth: readPaneWidth(),
    activeAgent: null,
    filters: NO_FILTERS,
    drawerOpen: false,
    paletteOpen: false,
    shortcutsOpen: false,
  })
}
