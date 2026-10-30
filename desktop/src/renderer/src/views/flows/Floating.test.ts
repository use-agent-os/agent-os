import { describe, expect, it } from 'vitest'
import { FLOAT_MAX_HEIGHT, placeFloating } from './Floating'

const canvas = { left: 200, top: 100, right: 1400, bottom: 800 }
const WINDOW_H = 900

describe('placeFloating', () => {
  it('opens right under a widget when there is room, at the widget left edge', () => {
    const p = placeFloating({ left: 700, top: 200, right: 900, bottom: 225 }, 320, canvas, WINDOW_H)
    expect(p).toEqual({ left: 700, top: 229, maxHeight: FLOAT_MAX_HEIGHT })
  })

  it('a widget low on the canvas opens upward, touching it, never further up', () => {
    const p = placeFloating({ left: 700, top: 700, right: 900, bottom: 725 }, 320, canvas, WINDOW_H)
    // Hung by its bottom edge just above the widget; height capped by the room above.
    expect(p.top).toBeUndefined()
    expect(p.bottom).toBe(WINDOW_H - 700 + 4)
    expect(p.maxHeight).toBe(Math.min(FLOAT_MAX_HEIGHT, 700 - 108 - 4))
  })

  it('caps the height to the room below rather than moving away from the widget', () => {
    const p = placeFloating({ left: 700, top: 450, right: 900, bottom: 475 }, 320, canvas, WINDOW_H)
    expect(p.top).toBe(479)
    expect(p.maxHeight).toBe(792 - 475 - 4)
  })

  it('stays inside the canvas horizontally', () => {
    expect(
      placeFloating({ left: 1300, top: 200, right: 1350, bottom: 220 }, 320, canvas, WINDOW_H).left,
    ).toBe(1392 - 320)
    expect(
      placeFloating({ left: 50, top: 200, right: 80, bottom: 220 }, 320, canvas, WINDOW_H).left,
    ).toBe(208)
  })
})
