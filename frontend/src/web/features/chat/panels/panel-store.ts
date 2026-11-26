import { create } from 'zustand'

// The chat's workspace panel (chat spec "Side panel"): one panel beside the
// conversation hosting tool tabs (Files, Browser) and item tabs (tool output,
// artifacts, reasoning). Its layout is the open session's: every session
// keeps its own tabs, order, active tab, size and dock in localStorage, and a
// session with nothing saved starts closed rather than inheriting another's.

/** Tools opened from the panel's + menu; one tab each (registry.ts). */
export type PanelToolKind = 'files' | 'browser'
/** Transcript items shown in a tab; they last until the session closes. */
export type PanelItemKind = 'tool-output' | 'artifact' | 'thinking'
export type PanelTabKind = PanelToolKind | PanelItemKind

export interface PanelTab {
  kind: PanelTabKind
  /** Unique in the panel: `tool:<kind>` for a tool, the item id for an item. */
  id: string
  title: string
}

export type PanelDock = 'right' | 'bottom'

export interface PanelLayout {
  tabs: PanelTab[]
  activeId: string | null
  /** Shown. False is minimized: the tabs stay, the panel is out of the way. */
  visible: boolean
  width: number
  height: number
  dock: PanelDock
  /** Over the whole chat pane instead of beside it. */
  expanded: boolean
}

/** The panel's element id: the chat header's toggle controls it. */
export const PANEL_ID = 'chat-workspace-panel'
/** The chat header's toggle, where focus goes back when nothing else can take it. */
export const PANEL_TOGGLE_ID = 'chat-workspace-panel-toggle'

export const SIDE_PANEL_WIDTH_KEY = 'agentos.ui.sidePanelWidth'
export const PANEL_LAYOUT_PREFIX = 'agentos.ui.panelLayout.v1:'
export const PANEL_LAYOUT_INDEX = 'agentos.ui.panelLayout.v1.index'
/** Sessions whose layout is kept; the least recently used go first. */
export const PANEL_LAYOUT_LIMIT = 60

export const SIDE_PANEL_MIN = 300
export const SIDE_PANEL_MAX = 1100
export const SIDE_PANEL_DEFAULT = 440
export const PANEL_HEIGHT_MIN = 200
export const PANEL_HEIGHT_MAX = 1200
export const PANEL_HEIGHT_DEFAULT = 340

const TOOL_KINDS: ReadonlySet<string> = new Set<PanelToolKind>(['files', 'browser'])

export function isToolKind(kind: PanelTabKind): kind is PanelToolKind {
  return TOOL_KINDS.has(kind)
}

export function toolTabId(kind: PanelToolKind): string {
  return `tool:${kind}`
}

export function clampPanelWidth(width: number): number {
  if (!Number.isFinite(width)) return SIDE_PANEL_DEFAULT
  return Math.round(Math.max(SIDE_PANEL_MIN, Math.min(SIDE_PANEL_MAX, width)))
}

export function clampPanelHeight(height: number): number {
  if (!Number.isFinite(height)) return PANEL_HEIGHT_DEFAULT
  return Math.round(Math.max(PANEL_HEIGHT_MIN, Math.min(PANEL_HEIGHT_MAX, height)))
}

/** The width a session starts with: the last width set anywhere, else the default. */
function defaultWidth(): number {
  try {
    const raw = localStorage.getItem(SIDE_PANEL_WIDTH_KEY)
    return raw ? clampPanelWidth(Number(raw)) : SIDE_PANEL_DEFAULT
  } catch {
    return SIDE_PANEL_DEFAULT
  }
}

export function emptyLayout(): PanelLayout {
  return {
    tabs: [],
    activeId: null,
    visible: false,
    width: defaultWidth(),
    height: PANEL_HEIGHT_DEFAULT,
    dock: 'right',
    expanded: false,
  }
}

/* ── Persistence ──────────────────────────────────────────────────────── */

interface StoredLayout {
  tabs: { kind: PanelToolKind }[]
  activeId: string | null
  open: boolean
  width: number
  height: number
  dock: PanelDock
  expanded: boolean
}

/**
 * A stored layout, or null when there is none or it is not one. Only tool
 * tabs are kept (an item tab points into a transcript that may be gone), so
 * a panel that held nothing else comes back closed.
 */
export function parseLayout(raw: string | null): PanelLayout | null {
  if (!raw) return null
  let body: unknown
  try {
    body = JSON.parse(raw)
  } catch {
    return null
  }
  if (!body || typeof body !== 'object') return null
  const stored = body as Partial<StoredLayout>
  const seen = new Set<string>()
  const tabs: PanelTab[] = []
  for (const row of Array.isArray(stored.tabs) ? stored.tabs : []) {
    const kind = (row as { kind?: unknown } | null)?.kind
    if (typeof kind !== 'string' || !TOOL_KINDS.has(kind) || seen.has(kind)) continue
    seen.add(kind)
    tabs.push({ kind: kind as PanelToolKind, id: toolTabId(kind as PanelToolKind), title: '' })
  }
  const activeId =
    typeof stored.activeId === 'string' && tabs.some((tab) => tab.id === stored.activeId)
      ? stored.activeId
      : (tabs[0]?.id ?? null)
  return {
    tabs,
    activeId,
    visible: stored.open === true && tabs.length > 0,
    width: clampPanelWidth(Number(stored.width)),
    height: clampPanelHeight(Number(stored.height)),
    dock: stored.dock === 'bottom' ? 'bottom' : 'right',
    expanded: stored.expanded === true,
  }
}

export function serializeLayout(layout: PanelLayout): string {
  const stored: StoredLayout = {
    tabs: layout.tabs
      .filter((tab) => isToolKind(tab.kind))
      .map((tab) => ({ kind: tab.kind as PanelToolKind })),
    activeId: layout.activeId,
    open: layout.visible,
    width: layout.width,
    height: layout.height,
    dock: layout.dock,
    expanded: layout.expanded,
  }
  return JSON.stringify(stored)
}

export function readLayout(sessionKey: string): PanelLayout {
  try {
    return parseLayout(localStorage.getItem(PANEL_LAYOUT_PREFIX + sessionKey)) ?? emptyLayout()
  } catch {
    return emptyLayout()
  }
}

function readIndex(): string[] {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(PANEL_LAYOUT_INDEX) ?? '[]')
    return Array.isArray(parsed) ? parsed.filter((k): k is string => typeof k === 'string') : []
  } catch {
    return []
  }
}

export function writeLayout(sessionKey: string, layout: PanelLayout): void {
  try {
    localStorage.setItem(PANEL_LAYOUT_PREFIX + sessionKey, serializeLayout(layout))
    const index = [sessionKey, ...readIndex().filter((key) => key !== sessionKey)]
    for (const stale of index.splice(PANEL_LAYOUT_LIMIT)) {
      localStorage.removeItem(PANEL_LAYOUT_PREFIX + stale)
    }
    localStorage.setItem(PANEL_LAYOUT_INDEX, JSON.stringify(index))
  } catch {
    /* storage unavailable or full: the layout lives for this tab only */
  }
}

/* ── Store ────────────────────────────────────────────────────────────── */

/** A file the Files tab should show next (a transcript path, a Show in Files). */
export interface FileReveal {
  path: string
  nonce: number
}

interface PanelState extends PanelLayout {
  /** The session the layout belongs to; null before a chat binds one. */
  sessionKey: string | null
  /** The element that opened the panel, to return focus to on close. */
  opener: HTMLElement | null
  reveal: FileReveal | null
  /** Bumped when a person opens the panel on purpose: focus moves into it. */
  focusTick: number
  bindSession(sessionKey: string | null): void
  /** Open (or select) a tab and show the panel. */
  open(tab: PanelTab, opener?: HTMLElement | null): void
  openTool(kind: PanelToolKind, opener?: HTMLElement | null): void
  /** Show Files on `path` (relative to the workspace). */
  revealFile(path: string, opener?: HTMLElement | null): void
  closeTab(id: string): void
  /** Close every tab; the panel closes with them. */
  closeAll(): void
  setActive(id: string): void
  moveTab(id: string, toIndex: number): void
  /** Show or hide the panel, keeping its tabs. */
  toggle(opener?: HTMLElement | null): void
  setVisible(visible: boolean): void
  setWidth(width: number, persist?: boolean): void
  setHeight(height: number, persist?: boolean): void
  setDock(dock: PanelDock): void
  setExpanded(expanded: boolean): void
}

function layoutOf(state: PanelLayout): PanelLayout {
  const { tabs, activeId, visible, width, height, dock, expanded } = state
  return { tabs, activeId, visible, width, height, dock, expanded }
}

export const usePanels = create<PanelState>((set, get) => {
  /** Apply a layout change and save it for the bound session. */
  const commit = (
    patch: Partial<PanelLayout & Pick<PanelState, 'opener' | 'reveal' | 'focusTick'>>,
  ) => {
    set(patch)
    const state = get()
    if (state.sessionKey) writeLayout(state.sessionKey, layoutOf(state))
  }
  return {
    ...emptyLayout(),
    sessionKey: null,
    opener: null,
    reveal: null,
    focusTick: 0,
    bindSession(sessionKey) {
      if (get().sessionKey === sessionKey) return
      const layout = sessionKey ? readLayout(sessionKey) : emptyLayout()
      set({ ...layout, sessionKey, opener: null, reveal: null })
    },
    open(tab, opener) {
      const { tabs } = get()
      const known = tabs.find((t) => t.id === tab.id)
      const next = known
        ? tabs.map((t) => (t.id === tab.id && tab.title ? { ...t, title: tab.title } : t))
        : [...tabs, tab]
      commit({
        tabs: next,
        activeId: tab.id,
        visible: true,
        opener: opener ?? get().opener,
        focusTick: opener ? get().focusTick + 1 : get().focusTick,
      })
    },
    openTool(kind, opener) {
      get().open({ kind, id: toolTabId(kind), title: '' }, opener)
    },
    revealFile(path, opener) {
      get().openTool('files', opener)
      set({ reveal: { path, nonce: (get().reveal?.nonce ?? 0) + 1 } })
    },
    closeTab(id) {
      const { tabs, activeId } = get()
      const index = tabs.findIndex((t) => t.id === id)
      if (index < 0) return
      const next = tabs.filter((t) => t.id !== id)
      // The neighbour that slides into the closed tab's place, else the one before.
      const neighbour = next[Math.min(index, next.length - 1)]
      commit({
        tabs: next,
        activeId: activeId === id ? (neighbour?.id ?? null) : activeId,
        visible: next.length > 0 && get().visible,
        expanded: next.length > 0 && get().expanded,
      })
    },
    closeAll() {
      commit({ tabs: [], activeId: null, visible: false, expanded: false })
    },
    setActive(activeId) {
      if (!get().tabs.some((t) => t.id === activeId)) return
      commit({ activeId })
    },
    moveTab(id, toIndex) {
      const tabs = [...get().tabs]
      const from = tabs.findIndex((t) => t.id === id)
      if (from < 0) return
      const target = Math.max(0, Math.min(tabs.length - 1, toIndex))
      if (target === from) return
      const [tab] = tabs.splice(from, 1)
      tabs.splice(target, 0, tab!)
      commit({ tabs })
    },
    toggle(opener) {
      if (get().visible) commit({ visible: false })
      else
        commit({
          visible: true,
          opener: opener ?? get().opener,
          focusTick: get().focusTick + 1,
        })
    },
    setVisible(visible) {
      commit({ visible })
    },
    setWidth(width, persist = true) {
      const next = clampPanelWidth(width)
      if (!persist) return set({ width: next })
      commit({ width: next })
      try {
        localStorage.setItem(SIDE_PANEL_WIDTH_KEY, String(next))
      } catch {
        /* storage unavailable */
      }
    },
    setHeight(height, persist = true) {
      const next = clampPanelHeight(height)
      if (!persist) return set({ height: next })
      commit({ height: next })
    },
    setDock(dock) {
      commit({ dock, expanded: false })
    },
    setExpanded(expanded) {
      commit({ expanded })
    },
  }
})

export function resetPanelsForTests(): void {
  usePanels.setState({
    ...emptyLayout(),
    sessionKey: null,
    opener: null,
    reveal: null,
    focusTick: 0,
  })
}
