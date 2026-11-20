import { keepFocus } from '../panes/focus'

/**
 * Keyboard position in Settings › Memory after a write. Saving, removing or
 * jumping takes away the control that had focus (an editor closes, a row
 * goes, a tab is hidden), and the browser drops focus to `<body>`, outside
 * the Settings sheet. These put it somewhere sensible instead.
 */

function lost(): boolean {
  const active = document.activeElement
  return !active || active === document.body || !active.isConnected || active.matches(':disabled')
}

/** How often and how long to look for the target: a refreshed list renders a moment after the write. */
const STEP_MS = 40
const TRIES = 10

/**
 * Once the list has re-rendered, focus `find()`'s element. Acts only while
 * focus is lost, so it never takes focus from where the user moved it. When
 * the target never shows up, falls back to the dialog that holds `anchor`
 * (`keepFocus`), so Escape still closes the sheet.
 */
export function refocus(
  anchor: Element | null | undefined,
  find: () => HTMLElement | null | undefined,
): void {
  // Found now: `anchor` may be the very control that is about to go.
  const dialog = anchor?.closest<HTMLElement>('[role="dialog"], [role="alertdialog"]') ?? null
  let tries = 0
  const attempt = () => {
    if (lost()) {
      const target = find()
      if (target?.isConnected && !target.matches(':disabled')) {
        target.focus()
        if (!lost()) return
      }
    }
    tries += 1
    if (tries < TRIES) window.setTimeout(attempt, STEP_MS)
    else keepFocus(dialog)
  }
  window.setTimeout(attempt, 0)
}

/**
 * Smooth, unless reduced motion is asked for, and at once while the window is
 * hidden: a smooth scroll runs on animation frames, which a hidden window
 * does not get. The same rule as the Capabilities jump.
 */
export function scrollBehavior(): ScrollBehavior {
  if (document.visibilityState === 'hidden') return 'instant'
  const reduce =
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  return reduce ? 'instant' : 'smooth'
}

/** A fact row's first action (its Edit button), by the fact's text. */
export function factRowButton(
  list: ParentNode | null | undefined,
  text: string,
): HTMLElement | null {
  const row = Array.from(list?.querySelectorAll<HTMLElement>('li.mem-fact') ?? []).find(
    (li) => li.querySelector('.mem-fact__text')?.textContent === text,
  )
  return row?.querySelector<HTMLElement>('button') ?? null
}

/** The row now at `index` (the one after a removed row), else the one before it. */
export function rowButtonAt(
  list: ParentNode | null | undefined,
  rowSelector: string,
  index: number,
): HTMLElement | null {
  const rows = Array.from(list?.querySelectorAll<HTMLElement>(rowSelector) ?? [])
  const row = rows[Math.min(index, rows.length - 1)]
  return row?.querySelector<HTMLElement>('button') ?? null
}

/** Where a settings card's keyboard position goes once its Save greys out: its chosen option or first control. */
export function cardControl(card: Element | null | undefined): HTMLElement | null {
  if (!card) return null
  return (
    card.querySelector<HTMLElement>('[role="radio"][aria-checked="true"]:not(:disabled)') ??
    card.querySelector<HTMLElement>(
      '[role="switch"]:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), button:not(:disabled)',
    )
  )
}
