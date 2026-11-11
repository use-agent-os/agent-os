import { describe, expect, it } from 'vitest'
import { composerClearance } from './tray-clearance'

function block(top: number): Element {
  const el = document.createElement('div')
  el.getBoundingClientRect = () => ({ top }) as DOMRect
  return el
}

describe('composerClearance', () => {
  it('is the distance from the composer top to the viewport bottom', () => {
    expect(composerClearance(block(756), 860)).toBe(104)
    expect(composerClearance(block(608.4), 860)).toBe(252)
  })

  it('is never negative (a composer scrolled below the fold)', () => {
    expect(composerClearance(block(900), 860)).toBe(0)
  })
})
