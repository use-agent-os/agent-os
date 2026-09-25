import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AIM_MS, headsInto, MenuHeading, MenuItem, MenuSep, MenuSub, PopMenu } from './PopMenu'

// The pointer paths below walk the layout the app gives the Sessions view
// menu: a 200px menu of 26px rows (Grouping at y 4-30, Ordering 30-56,
// Inbox style 56-82, a separator, the Filters heading at 91-114, Status
// 114-140). The rows end at x 196, and each submenu panel hangs from x 198,
// level with its row. jsdom lays nothing out, so the panels get their
// boxes here.
const PANELS: Record<string, { left: number; top: number; width: number; height: number }> = {
  Grouping: { left: 198, top: 0, width: 200, height: 112 },
  Ordering: { left: 198, top: 26, width: 200, height: 86 },
  Status: { left: 198, top: 110, width: 200, height: 112 },
}

let under: Element | null = null

beforeEach(() => {
  vi.useFakeTimers()
  under = null
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (
    this: HTMLElement,
  ) {
    const box = PANELS[this.getAttribute('aria-label') ?? '']
    return box
      ? DOMRect.fromRect({ x: box.left, y: box.top, width: box.width, height: box.height })
      : DOMRect.fromRect()
  })
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

const noop = () => {}

function renderMenu() {
  const onClose = vi.fn()
  render(
    <PopMenu place={{ at: { x: 0, y: 0 } }} onClose={onClose} label="View">
      <MenuSub label="Grouping">
        {['Date', 'Agent', 'Project', 'None'].map((g) => (
          <MenuItem key={g} role="menuitemradio" checked={g === 'Date'} label={g} onSelect={noop} />
        ))}
      </MenuSub>
      <MenuSub label="Ordering">
        {['Recent activity', 'Oldest first', 'Name'].map((o) => (
          <MenuItem
            key={o}
            role="menuitemradio"
            checked={o === 'Recent activity'}
            label={o}
            onSelect={noop}
          />
        ))}
      </MenuSub>
      <MenuItem checked={false} label="Inbox style" onSelect={noop} />
      <MenuSep />
      <MenuHeading>Filters</MenuHeading>
      <MenuSub label="Status">
        {['Any', 'Running', 'Needs attention', 'Idle'].map((s) => (
          <MenuItem key={s} role="menuitemradio" checked={s === 'Any'} label={s} onSelect={noop} />
        ))}
      </MenuSub>
    </PopMenu>,
  )
  return onClose
}

const menu = () => screen.getByRole('menu', { name: 'View' })
const row = (name: string) => screen.getByRole('menuitem', { name })
const radio = (name: string) => screen.getByRole('menuitemradio', { name })
const panel = (name: string) => screen.queryByRole('menu', { name })

/** The pointer arrives on `el` at (x, y): the events a browser sends, in order. */
function pointAt(el: Element, x: number, y: number) {
  const at = { clientX: x, clientY: y }
  if (el !== under) {
    if (under) fireEvent.pointerOut(under, { ...at, relatedTarget: el })
    fireEvent.pointerOver(el, { ...at, relatedTarget: under })
    under = el
  }
  fireEvent.pointerMove(el, at)
}

/** The pointer goes off the menu into the window. */
function pointAway() {
  if (under) fireEvent.pointerOut(under, { relatedTarget: document.body })
  under = null
}

/** Time passes with the pointer at rest. */
const rest = (ms: number) => act(() => vi.advanceTimersByTime(ms))

describe('MenuSub under the pointer', () => {
  it('opens its submenu the moment the pointer reaches the row', () => {
    renderMenu()
    pointAt(row('Grouping'), 100, 17)
    expect(row('Grouping')).toHaveAttribute('aria-expanded', 'true')
    expect(panel('Grouping')).toBeInTheDocument()
  })

  it('moves the highlight and the open submenu with the pointer, not a beat later', () => {
    renderMenu()
    pointAt(row('Grouping'), 100, 17)
    rest(300)
    pointAt(row('Ordering'), 100, 43)
    // The row the pointer left lets go at once: never two rows lit.
    expect(row('Grouping')).toHaveAttribute('data-active', 'false')
    expect(row('Ordering')).toHaveAttribute('data-active', 'true')
    expect(panel('Grouping')).toBeNull()
    expect(panel('Ordering')).toBeInTheDocument()
    pointAt(screen.getByRole('menuitemcheckbox', { name: 'Inbox style' }), 100, 69)
    expect(row('Ordering')).toHaveAttribute('data-active', 'false')
    expect(panel('Ordering')).toBeNull()
  })

  it('does not take a submenu back from a pointer resting on its row', () => {
    // The old close timer, armed on the way down, shut the next row's panel
    // right after its own open timer had shown it.
    renderMenu()
    pointAt(row('Grouping'), 100, 17)
    rest(300)
    pointAt(row('Ordering'), 100, 43)
    rest(1000)
    expect(panel('Ordering')).toBeInTheDocument()
  })

  it('keeps the open submenu while the pointer cuts across another row into it', () => {
    renderMenu()
    pointAt(row('Grouping'), 100, 17)
    rest(300)
    // Down and to the right, toward the Grouping panel, over Ordering.
    pointAt(row('Ordering'), 140, 36)
    expect(panel('Grouping')).toBeInTheDocument()
    expect(panel('Ordering')).toBeNull()
    rest(30)
    pointAt(radio('Agent'), 220, 43)
    rest(1000)
    expect(panel('Grouping')).toBeInTheDocument()
    expect(panel('Ordering')).toBeNull()
  })

  it('follows the pointer to the row it stops on short of the panel', () => {
    renderMenu()
    pointAt(row('Grouping'), 100, 17)
    rest(300)
    pointAt(row('Ordering'), 140, 36)
    rest(AIM_MS - 1)
    expect(panel('Grouping')).toBeInTheDocument()
    rest(1)
    expect(panel('Grouping')).toBeNull()
    expect(panel('Ordering')).toBeInTheDocument()
  })

  it('leaves the open submenu alone when the pointer leaves the menu', () => {
    renderMenu()
    pointAt(row('Grouping'), 100, 17)
    rest(300)
    pointAt(row('Ordering'), 140, 36)
    pointAway()
    rest(1000)
    expect(panel('Grouping')).toBeInTheDocument()
    expect(panel('Ordering')).toBeNull()
  })

  it('keeps a hovered submenu open on click and hands it the keyboard', () => {
    renderMenu()
    pointAt(row('Grouping'), 100, 17)
    rest(300)
    fireEvent.click(row('Grouping'))
    expect(panel('Grouping')).toBeInTheDocument()
    expect(radio('Date')).toHaveFocus()
  })

  it('keeps the submenu as the pointer comes back from it to its row', () => {
    renderMenu()
    pointAt(row('Grouping'), 100, 17)
    const open = panel('Grouping')
    pointAt(radio('Date'), 210, 17)
    expect(panel('Grouping')).toBe(open)
    // The 2px strip between the panel and the row is the menu's own padding.
    pointAt(menu(), 197, 17)
    expect(panel('Grouping')).toBe(open)
    pointAt(row('Grouping'), 190, 17)
    expect(panel('Grouping')).toBe(open)
  })

  it('keeps the keyboard in a clicked submenu as the pointer comes back to its row', () => {
    renderMenu()
    pointAt(row('Grouping'), 100, 17)
    fireEvent.click(row('Grouping'))
    expect(radio('Date')).toHaveFocus()
    pointAt(radio('Date'), 210, 17)
    pointAt(menu(), 197, 17)
    pointAt(row('Grouping'), 190, 17)
    expect(radio('Date')).toHaveFocus()
  })

  it('changes nothing over the gaps between rows, only on the next row', () => {
    renderMenu()
    pointAt(row('Status'), 100, 127)
    const open = panel('Status')
    // Out of the panel's top edge: the strip, then the heading right above
    // the row, then back down onto the row.
    pointAt(open!, 210, 112)
    pointAt(menu(), 197, 112)
    pointAt(screen.getByText('Filters'), 190, 112)
    expect(panel('Status')).toBe(open)
    pointAt(row('Status'), 186, 118)
    expect(panel('Status')).toBe(open)
    // Up over the heading and the separator: still no other row.
    pointAt(screen.getByText('Filters'), 150, 100)
    pointAt(screen.getByRole('separator'), 150, 86)
    expect(panel('Status')).toBe(open)
    pointAt(screen.getByRole('menuitemcheckbox', { name: 'Inbox style' }), 150, 70)
    expect(panel('Status')).toBeNull()
  })
})

describe('MenuSub from the keyboard', () => {
  it('opens on Return and Right, and Escape closes the submenu before the menu', () => {
    const onClose = renderMenu()
    expect(row('Grouping')).toHaveFocus()
    fireEvent.keyDown(row('Grouping'), { key: 'Enter' })
    expect(radio('Date')).toHaveFocus()
    fireEvent.keyDown(radio('Date'), { key: 'ArrowDown' })
    expect(radio('Agent')).toHaveFocus()
    fireEvent.keyDown(radio('Agent'), { key: 'Escape' })
    expect(panel('Grouping')).toBeNull()
    expect(row('Grouping')).toHaveFocus()
    fireEvent.keyDown(row('Grouping'), { key: 'ArrowDown' })
    expect(row('Ordering')).toHaveFocus()
    fireEvent.keyDown(row('Ordering'), { key: 'ArrowRight' })
    expect(panel('Ordering')).toBeInTheDocument()
    fireEvent.keyDown(row('Ordering'), { key: 'Escape' })
    expect(panel('Ordering')).toBeNull()
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.keyDown(row('Ordering'), { key: 'Escape' })
    expect(onClose).toHaveBeenCalledOnce()
  })
})

describe('headsInto', () => {
  const right = { left: 200, right: 400, top: 0, bottom: 100 }
  const left = { left: 0, right: 200, top: 0, bottom: 100 }

  it('holds for a pointer moving toward the near edge of the panel', () => {
    expect(headsInto({ x: 100, y: 20 }, { x: 120, y: 30 }, right, 'right')).toBe(true)
    expect(headsInto({ x: 100, y: 20 }, { x: 130, y: 20 }, right, 'right')).toBe(true)
    expect(headsInto({ x: 300, y: 20 }, { x: 280, y: 30 }, left, 'left')).toBe(true)
  })

  it('lets go of a pointer moving down the menu, away, or past the panel', () => {
    expect(headsInto({ x: 100, y: 20 }, { x: 100, y: 46 }, right, 'right')).toBe(false)
    expect(headsInto({ x: 100, y: 20 }, { x: 80, y: 30 }, right, 'right')).toBe(false)
    expect(headsInto({ x: 100, y: 90 }, { x: 120, y: 130 }, right, 'right')).toBe(false)
    expect(headsInto({ x: 100, y: 20 }, { x: 110, y: -10 }, right, 'right')).toBe(false)
    expect(headsInto({ x: 300, y: 20 }, { x: 320, y: 30 }, left, 'left')).toBe(false)
  })
})
