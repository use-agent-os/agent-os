import { describe, expect, it } from 'vitest'
import {
  dragSize,
  fittedSize,
  keySize,
  KEY_STEP,
  KEY_STEP_LARGE,
  MIN_CHAT_WIDTH,
  maxSize,
  presentationFor,
} from './layout'
import { SIDE_PANEL_MIN } from './panel-store'

const base = { phone: false, dock: 'right' as const, expanded: false }

describe('panel presentation', () => {
  it('is a sheet on phones and over the chat when expanded', () => {
    expect(
      presentationFor({ ...base, phone: true, containerWidth: 390, containerHeight: 800 }),
    ).toBe('sheet')
    expect(
      presentationFor({ ...base, expanded: true, containerWidth: 1400, containerHeight: 800 }),
    ).toBe('expanded')
  })

  it('docks beside the chat while both fit, and takes over when they do not', () => {
    const fits = MIN_CHAT_WIDTH + SIDE_PANEL_MIN
    expect(presentationFor({ ...base, containerWidth: fits, containerHeight: 800 })).toBe('right')
    expect(presentationFor({ ...base, containerWidth: fits - 1, containerHeight: 800 })).toBe(
      'takeover',
    )
    expect(
      presentationFor({ ...base, dock: 'bottom', containerWidth: 900, containerHeight: 300 }),
    ).toBe('takeover')
    // Not measured yet: no takeover on a guess.
    expect(presentationFor({ ...base, containerWidth: 0, containerHeight: 0 })).toBe('right')
  })

  it('fits the saved size into the room the chat leaves', () => {
    expect(fittedSize('x', 900, 1000)).toBe(1000 - MIN_CHAT_WIDTH)
    expect(fittedSize('x', 440, 1400)).toBe(440)
    expect(fittedSize('x', 440, 0)).toBe(440)
    expect(fittedSize('x', 100, 1400)).toBe(SIDE_PANEL_MIN)
    expect(maxSize('x', 1000)).toBe(1000 - MIN_CHAT_WIDTH)
  })
})

describe('panel resizing', () => {
  it('grows as the divider moves away from the panel, in both directions of text', () => {
    // LTR: the panel is on the right, dragging left grows it.
    expect(dragSize('x', 400, 1000, 950, false)).toBe(450)
    // RTL: the panel is on the left, dragging right grows it.
    expect(dragSize('x', 400, 300, 350, true)).toBe(450)
    // Bottom: dragging up grows it.
    expect(dragSize('y', 300, 600, 560, false)).toBe(340)
  })

  it('maps arrow keys by reading direction, with Shift for a large step', () => {
    const opts = { min: 300, max: 900, rtl: false, large: false }
    expect(keySize('x', 'ArrowLeft', 400, opts)).toBe(400 + KEY_STEP)
    expect(keySize('x', 'ArrowRight', 400, opts)).toBe(400 - KEY_STEP)
    expect(keySize('x', 'ArrowRight', 400, { ...opts, rtl: true })).toBe(400 + KEY_STEP)
    expect(keySize('x', 'ArrowLeft', 400, { ...opts, large: true })).toBe(400 + KEY_STEP_LARGE)
    expect(keySize('y', 'ArrowUp', 400, opts)).toBe(400 + KEY_STEP)
    expect(keySize('x', 'Home', 400, opts)).toBe(300)
    expect(keySize('x', 'End', 400, opts)).toBe(900)
    expect(keySize('x', 'ArrowRight', 305, opts)).toBe(300)
    expect(keySize('x', 'Enter', 400, opts)).toBeNull()
  })
})
