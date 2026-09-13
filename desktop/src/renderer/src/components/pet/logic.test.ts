import { describe, expect, it } from 'vitest'
import { anchorFromPixels, defaultAnchor, parseStoredAnchor, pixelsFromAnchor } from './logic'

const pet = { w: 64, h: 69 }
const win = { w: 1000, h: 700 }

describe('pet anchor', () => {
  it('round-trips a pixel position through the free-space fraction', () => {
    const anchor = anchorFromPixels({ x: 468, y: 315.5 }, pet, win)
    expect(anchor.fx).toBeCloseTo(0.5)
    expect(anchor.fy).toBeCloseTo(0.5)
    expect(pixelsFromAnchor(anchor, pet, win)).toEqual({ x: 468, y: 316 })
  })

  it('moves the pet with the window and keeps it inside a smaller one', () => {
    // Dragged to the bottom-right corner of a big window…
    const anchor = anchorFromPixels({ x: 936, y: 631 }, pet, win)
    expect(anchor).toEqual({ fx: 1, fy: 1 })
    // …it is still in the corner, and on screen, after the window shrinks.
    expect(pixelsFromAnchor(anchor, pet, { w: 400, h: 300 })).toEqual({ x: 336, y: 231 })
  })

  it('clamps pixels that are already off screen', () => {
    expect(anchorFromPixels({ x: -50, y: 5000 }, pet, win)).toEqual({ fx: 0, fy: 1 })
  })

  it('pins to the origin when the pet is bigger than the window', () => {
    expect(pixelsFromAnchor({ fx: 1, fy: 1 }, { w: 500, h: 500 }, { w: 300, h: 300 })).toEqual({
      x: 0,
      y: 0,
    })
  })

  it('starts in the bottom-right corner, inset from the edges', () => {
    expect(pixelsFromAnchor(defaultAnchor(pet, win), pet, win)).toEqual({ x: 908, y: 535 })
  })

  it('reads a new anchor, converts an old pixel save, rejects junk', () => {
    expect(parseStoredAnchor({ fx: 0.25, fy: 2 }, pet, win)).toEqual({ fx: 0.25, fy: 1 })
    expect(parseStoredAnchor({ x: 936, y: 631 }, pet, win)).toEqual({ fx: 1, fy: 1 })
    expect(parseStoredAnchor(null, pet, win)).toBeNull()
    expect(parseStoredAnchor({ x: 'a' }, pet, win)).toBeNull()
  })
})
