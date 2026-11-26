import { beforeEach, describe, expect, it } from 'vitest'
import {
  PANEL_LAYOUT_INDEX,
  PANEL_LAYOUT_LIMIT,
  PANEL_LAYOUT_PREFIX,
  SIDE_PANEL_MAX,
  SIDE_PANEL_MIN,
  SIDE_PANEL_WIDTH_KEY,
  parseLayout,
  readLayout,
  resetPanelsForTests,
  serializeLayout,
  toolTabId,
  usePanels,
} from './panel-store'

const A = 'agent:main:webchat:a'
const B = 'agent:main:webchat:b'
const store = () => usePanels.getState()

beforeEach(() => {
  localStorage.clear()
  resetPanelsForTests()
})

describe('panel store', () => {
  it('opens a tab, selects it and shows the panel', () => {
    store().bindSession(A)
    store().openTool('files')
    expect(store().tabs.map((t) => t.id)).toEqual([toolTabId('files')])
    expect(store().activeId).toBe('tool:files')
    expect(store().visible).toBe(true)
    // Opening it again selects the same tab, never a second one.
    store().open({ kind: 'tool-output', id: 'i1', title: 'bash' })
    store().openTool('files')
    expect(store().tabs).toHaveLength(2)
    expect(store().activeId).toBe('tool:files')
  })

  it('closing the active tab selects the one that slides into its place', () => {
    store().bindSession(A)
    store().openTool('files')
    store().openTool('browser')
    store().open({ kind: 'thinking', id: 'r1', title: 'Reasoning' })
    store().setActive('tool:browser')
    store().closeTab('tool:browser')
    expect(store().activeId).toBe('r1')
    store().closeTab('r1')
    expect(store().activeId).toBe('tool:files')
  })

  it('closing the last tab closes the panel', () => {
    store().bindSession(A)
    store().openTool('files')
    store().closeTab('tool:files')
    expect(store().visible).toBe(false)
    expect(store().tabs).toEqual([])
  })

  it('moves a tab within bounds', () => {
    store().bindSession(A)
    store().openTool('files')
    store().openTool('browser')
    store().moveTab('tool:browser', 0)
    expect(store().tabs.map((t) => t.kind)).toEqual(['browser', 'files'])
    store().moveTab('tool:browser', 99)
    expect(store().tabs.map((t) => t.kind)).toEqual(['files', 'browser'])
    store().moveTab('nope', 0)
    expect(store().tabs.map((t) => t.kind)).toEqual(['files', 'browser'])
  })

  it('toggles visibility without losing tabs, and asks for focus when shown', () => {
    store().bindSession(A)
    store().openTool('files')
    const tick = store().focusTick
    store().toggle()
    expect(store().visible).toBe(false)
    expect(store().tabs).toHaveLength(1)
    store().toggle(document.body)
    expect(store().visible).toBe(true)
    expect(store().focusTick).toBe(tick + 1)
    expect(store().opener).toBe(document.body)
  })

  it('clamps width and height', () => {
    store().bindSession(A)
    store().setWidth(5)
    expect(store().width).toBe(SIDE_PANEL_MIN)
    store().setWidth(99_999)
    expect(store().width).toBe(SIDE_PANEL_MAX)
    store().setHeight(Number.NaN)
    expect(store().height).toBeGreaterThan(0)
  })

  it('reveals a file in the Files tab with a fresh nonce each time', () => {
    store().bindSession(A)
    store().revealFile('docs/a.md')
    const first = store().reveal!
    store().revealFile('docs/a.md')
    expect(store().reveal!.nonce).toBe(first.nonce + 1)
    expect(store().activeId).toBe('tool:files')
  })
})

describe('per-session layout', () => {
  it('saves each session on its own and restores it', () => {
    store().bindSession(A)
    store().openTool('browser')
    store().openTool('files')
    store().setActive('tool:browser')
    store().setWidth(612)
    store().setDock('bottom')
    store().bindSession(B)
    expect(store().tabs).toEqual([])
    expect(store().visible).toBe(false)
    store().bindSession(A)
    expect(store().tabs.map((t) => t.kind)).toEqual(['browser', 'files'])
    expect(store().activeId).toBe('tool:browser')
    expect(store().width).toBe(612)
    expect(store().dock).toBe('bottom')
    expect(store().visible).toBe(true)
  })

  it('a session with no saved layout starts closed instead of inheriting one', () => {
    store().bindSession(A)
    store().openTool('files')
    store().setExpanded(true)
    store().bindSession(B)
    expect(store()).toMatchObject({ tabs: [], visible: false, expanded: false, dock: 'right' })
    expect(localStorage.getItem(PANEL_LAYOUT_PREFIX + B)).toBeNull()
  })

  it('keeps only tool tabs: a panel that held nothing else comes back closed', () => {
    store().bindSession(A)
    store().open({ kind: 'tool-output', id: 'i1', title: 'bash' })
    expect(readLayout(A)).toMatchObject({ tabs: [], visible: false })
    store().openTool('files')
    expect(readLayout(A).tabs.map((t) => t.id)).toEqual(['tool:files'])
  })

  it('remembers the last width as the default for a new session', () => {
    store().bindSession(A)
    store().setWidth(700)
    expect(localStorage.getItem(SIDE_PANEL_WIDTH_KEY)).toBe('700')
    store().bindSession(B)
    expect(store().width).toBe(700)
  })

  it('reads a damaged or foreign layout as none', () => {
    expect(parseLayout('not json')).toBeNull()
    expect(parseLayout('42')).toBeNull()
    const odd = parseLayout(
      JSON.stringify({
        tabs: [{ kind: 'files' }, { kind: 'files' }, { kind: 'terminal' }, null, 'x'],
        activeId: 'tool:terminal',
        open: true,
        width: 'wide',
        dock: 'left',
      }),
    )!
    expect(odd.tabs.map((t) => t.id)).toEqual(['tool:files'])
    expect(odd.activeId).toBe('tool:files')
    expect(odd.dock).toBe('right')
    expect(Number.isFinite(odd.width)).toBe(true)
  })

  it('round-trips through storage', () => {
    store().bindSession(A)
    store().openTool('files')
    store().setHeight(420)
    const saved = parseLayout(serializeLayout(store()))!
    expect(saved).toMatchObject({ height: 420, visible: true, activeId: 'tool:files' })
  })

  it('forgets the least recently used sessions past the limit', () => {
    for (let i = 0; i <= PANEL_LAYOUT_LIMIT; i += 1) {
      store().bindSession(`agent:main:webchat:s${i}`)
      store().openTool('files')
    }
    const index = JSON.parse(localStorage.getItem(PANEL_LAYOUT_INDEX)!) as string[]
    expect(index).toHaveLength(PANEL_LAYOUT_LIMIT)
    expect(localStorage.getItem(`${PANEL_LAYOUT_PREFIX}agent:main:webchat:s0`)).toBeNull()
    expect(localStorage.getItem(`${PANEL_LAYOUT_PREFIX}agent:main:webchat:s1`)).not.toBeNull()
  })
})
