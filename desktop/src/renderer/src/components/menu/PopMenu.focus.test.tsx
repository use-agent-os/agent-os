import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MenuItem, PopMenu } from './PopMenu'

afterEach(() => {
  vi.restoreAllMocks()
})

/**
 * A browser refuses focus to an element that is not visible, and jsdom does
 * not. So this holds what the browser needs: whenever the menu hands out its
 * opening focus, it is not `visibility: hidden` — even on the frame before it
 * has been measured and placed.
 */
describe('PopMenu · opening focus', () => {
  function focusesWhileHidden(ui: React.ReactNode): string[] {
    const seen: string[] = []
    const focus = HTMLElement.prototype.focus
    vi.spyOn(HTMLElement.prototype, 'focus').mockImplementation(function (
      this: HTMLElement,
      opts?: FocusOptions,
    ) {
      const menu = this.closest<HTMLElement>('[role="menu"]')
      if (menu?.style.visibility === 'hidden') seen.push(this.tagName)
      else focus.call(this, opts)
    })
    render(ui)
    return seen
  }

  const anchor = DOMRect.fromRect({ x: 10, y: 10, width: 80, height: 20 })

  it('lands on the first row', () => {
    const refused = focusesWhileHidden(
      <PopMenu place={{ anchor }} onClose={() => {}} label="Menu">
        <MenuItem label="One" onSelect={() => {}} />
        <MenuItem label="Two" onSelect={() => {}} />
      </PopMenu>,
    )
    expect(refused).toEqual([])
    expect(screen.getByRole('menuitem', { name: 'One' })).toHaveFocus()
  })

  it('lands on a field the menu leads with, when it marks one', () => {
    const refused = focusesWhileHidden(
      <PopMenu place={{ anchor }} onClose={() => {}} label="Menu">
        <input aria-label="Find" data-menu-autofocus="" />
        <MenuItem label="One" onSelect={() => {}} />
      </PopMenu>,
    )
    expect(refused).toEqual([])
    expect(screen.getByRole('textbox', { name: 'Find' })).toHaveFocus()
  })
})
