import { describe, expect, it } from 'vitest'
import { placeMenu } from './PopMenu'

const size = { width: 200, height: 300 }
const viewport = { width: 1000, height: 800 }

describe('placeMenu', () => {
  it('opens a context menu down-right of the point', () => {
    expect(placeMenu({ at: { x: 100, y: 100 } }, size, viewport)).toEqual({
      left: 100,
      top: 100,
      origin: 'top-left',
    })
  })

  it('flips a context menu that would run off the right or bottom edge', () => {
    expect(placeMenu({ at: { x: 900, y: 700 } }, size, viewport)).toEqual({
      left: 700,
      top: 400,
      origin: 'bottom-right',
    })
  })

  it('hangs an anchored menu under its control, right edges aligned', () => {
    const anchor = { left: 500, right: 540, top: 100, bottom: 120 } as DOMRect
    expect(placeMenu({ anchor }, size, viewport)).toEqual({
      left: 340,
      top: 124,
      origin: 'top-right',
    })
    expect(placeMenu({ anchor, align: 'start' }, size, viewport)).toMatchObject({
      left: 500,
      origin: 'top-left',
    })
  })

  it('flips an anchored menu above when there is no room below', () => {
    const anchor = { left: 500, right: 540, top: 700, bottom: 720 } as DOMRect
    expect(placeMenu({ anchor }, size, viewport)).toEqual({
      left: 340,
      top: 396,
      origin: 'bottom-right',
    })
  })

  it('never leaves the viewport, even for a tiny window', () => {
    const p = placeMenu({ at: { x: 2, y: 2 } }, size, { width: 150, height: 200 })
    expect(p.left).toBe(8)
    expect(p.top).toBe(8)
  })
})
