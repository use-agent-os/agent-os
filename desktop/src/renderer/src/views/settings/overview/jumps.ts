import { useUi } from '~/stores/ui'
import type { SettingsSection } from '../sections'

/**
 * How the Overview's buttons land somewhere useful, not just on a section's
 * top. Two hook points, so a section can take part without the Overview
 * importing it:
 *
 * - `registerJump(section, fn)`: run `fn` after every jump to `section`
 *   (Memory can open its setup tab this way).
 * - `takeFocus(section)`: a pane reads which part of it the jump was for
 *   (Capabilities scrolls to the Images card).
 */

type Hook = (focus?: string) => void

const hooks = new Map<SettingsSection, Set<Hook>>()
let pendingFocus: { section: SettingsSection; focus: string; at: number } | null = null

/**
 * How long a jump's focus stays readable. Not cleared on read: React runs a
 * mount effect twice in development, and both runs must see it; a later,
 * unrelated visit to the section must not.
 */
export const FOCUS_TTL_MS = 2000

/** Returns the unregister function. */
export function registerJump(section: SettingsSection, fn: Hook): () => void {
  const set = hooks.get(section) ?? new Set<Hook>()
  set.add(fn)
  hooks.set(section, set)
  return () => {
    set.delete(fn)
  }
}

export function jumpTo(section: SettingsSection, focus?: string): void {
  pendingFocus = focus ? { section, focus, at: Date.now() } : null
  useUi.getState().setSettingsSection(section)
  for (const fn of hooks.get(section) ?? []) fn(focus)
  land(section, focus)
}

/**
 * After the destination pane has had its turn (Capabilities puts the
 * keyboard on the card it scrolled to, at 30 ms): the button that was
 * clicked is gone with the old section, so focus sits on <body>.
 */
export const LAND_MS = 120

/**
 * Keyboard focus after a jump: the clicked button left with the old
 * section, and focus on <body> loses the keyboard position (Tab starts over,
 * Escape reaches nothing). If the destination pane has not placed focus
 * itself, land on the field holding what the jump was for (Environment's
 * search, filled with the variable), else on the section's heading.
 */
function land(section: SettingsSection, focus?: string): void {
  window.setTimeout(() => {
    const root = document.querySelector<HTMLElement>(`.stg-section[data-section="${section}"]`)
    if (!root) return
    const active = document.activeElement
    if (active && active !== document.body && root.contains(active)) return
    const field = focus
      ? Array.from(root.querySelectorAll<HTMLInputElement>('input')).find((i) => i.value === focus)
      : undefined
    const heading = root.querySelector<HTMLElement>('h1')
    if (!field && heading && !heading.hasAttribute('tabindex')) heading.tabIndex = -1
    ;(field ?? heading)?.focus({ preventScroll: true })
  }, LAND_MS)
}

/** The part of `section` the last jump asked for, while that jump is fresh. */
export function takeFocus(section: SettingsSection, now = Date.now()): string | null {
  if (!pendingFocus || pendingFocus.section !== section) return null
  if (now - pendingFocus.at > FOCUS_TTL_MS) {
    pendingFocus = null
    return null
  }
  return pendingFocus.focus
}
