import {
  PANEL_HEIGHT_MAX,
  PANEL_HEIGHT_MIN,
  SIDE_PANEL_MAX,
  SIDE_PANEL_MIN,
  type PanelDock,
} from './panel-store'

// Where the panel goes and how big it is, from the saved layout and the room
// the chat pane has. Pure, so the geometry is testable without a browser.

/** The conversation never gets narrower or shorter than this beside the panel. */
export const MIN_CHAT_WIDTH = 380
export const MIN_CHAT_HEIGHT = 260

/**
 * `right` / `bottom`: docked beside the conversation. `expanded`: over the
 * chat pane, by choice. `takeover`: over the chat pane because the pane is
 * too small to share (the saved layout is untouched, and a wider pane puts
 * the panel back beside the chat). `sheet`: a full-screen sheet on phones.
 */
export type Presentation = 'right' | 'bottom' | 'expanded' | 'takeover' | 'sheet'

export interface PresentationInput {
  phone: boolean
  dock: PanelDock
  expanded: boolean
  /** The chat pane plus panel, measured; 0 when not measurable yet. */
  containerWidth: number
  containerHeight: number
}

export function presentationFor(input: PresentationInput): Presentation {
  if (input.phone) return 'sheet'
  if (input.expanded) return 'expanded'
  if (input.dock === 'bottom') {
    if (input.containerHeight > 0 && roomFor('y', input.containerHeight) < PANEL_HEIGHT_MIN)
      return 'takeover'
    return 'bottom'
  }
  if (input.containerWidth > 0 && roomFor('x', input.containerWidth) < SIDE_PANEL_MIN)
    return 'takeover'
  return 'right'
}

/** The most the panel may take along an axis and leave the chat usable. */
export function roomFor(axis: 'x' | 'y', container: number): number {
  return container - (axis === 'x' ? MIN_CHAT_WIDTH : MIN_CHAT_HEIGHT)
}

/** The saved size, cut to the room there is (never below the minimum). */
export function fittedSize(axis: 'x' | 'y', saved: number, container: number): number {
  const [min, max] =
    axis === 'x' ? [SIDE_PANEL_MIN, SIDE_PANEL_MAX] : [PANEL_HEIGHT_MIN, PANEL_HEIGHT_MAX]
  const room = container > 0 ? roomFor(axis, container) : max
  return Math.round(Math.max(min, Math.min(saved, max, room)))
}

/** The largest size a drag or a key may set, given the room. */
export function maxSize(axis: 'x' | 'y', container: number): number {
  const [min, max] =
    axis === 'x' ? [SIDE_PANEL_MIN, SIDE_PANEL_MAX] : [PANEL_HEIGHT_MIN, PANEL_HEIGHT_MAX]
  if (container <= 0) return max
  return Math.max(min, Math.min(max, roomFor(axis, container)))
}

/**
 * The size a drag sets. The divider sits on the panel's leading edge: in a
 * left-to-right page the panel is on the right, so moving left grows it; in a
 * right-to-left page the panel is on the left and moving right grows it. A
 * bottom panel grows as the divider moves up.
 */
export function dragSize(
  axis: 'x' | 'y',
  start: number,
  from: number,
  to: number,
  rtl: boolean,
): number {
  if (axis === 'y') return start + (from - to)
  return start + (rtl ? to - from : from - to)
}

export const KEY_STEP = 16
export const KEY_STEP_LARGE = 64

/**
 * The size an arrow key sets on the divider, or null for a key it ignores.
 * The arrow pointing away from the panel grows it (Left in LTR, Right in
 * RTL, Up for a bottom panel); Home and End go to the minimum and maximum.
 */
export function keySize(
  axis: 'x' | 'y',
  key: string,
  current: number,
  { min, max, rtl, large }: { min: number; max: number; rtl: boolean; large: boolean },
): number | null {
  const step = large ? KEY_STEP_LARGE : KEY_STEP
  const grow = axis === 'y' ? 'ArrowUp' : rtl ? 'ArrowRight' : 'ArrowLeft'
  const shrink = axis === 'y' ? 'ArrowDown' : rtl ? 'ArrowLeft' : 'ArrowRight'
  let next: number
  if (key === grow) next = current + step
  else if (key === shrink) next = current - step
  else if (key === 'Home') next = min
  else if (key === 'End') next = max
  else return null
  return Math.max(min, Math.min(max, next))
}
