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
