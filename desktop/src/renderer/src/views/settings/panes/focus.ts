import { useEffect, useRef } from 'react'

/**
 * Keyboard focus across a pane's own re-renders.
 *
 * When the focused control unmounts (a picked provider tile swapped for its
 * form, a removed variable's row, a confirm's Keep button), the browser drops
 * focus to `<body>`. That is outside every dialog, so Escape reaches no
 * handler and the Settings sheet can no longer be closed from the keyboard.
 * A pane that knows it is about to remove the focused control moves focus
 * somewhere sensible instead.
 */

const DIALOG = '[role="dialog"], [role="alertdialog"]'

function lost(): boolean {
  const active = document.activeElement
  // A control greyed out under focus still reads as active until the browser
  // gets round to dropping it to <body>: count it as lost already.
  return !active || active === document.body || !active.isConnected || active.matches(':disabled')
}

/**
 * After the current render: when focus was lost, put it on `target` (if it
 * is still there), else on the dialog that holds `anchor` (the Settings
 * sheet, or a setup sheet), so Escape works again. The dialog is found now,
 * so `anchor` may be the very control that is about to go.
 *
 * Disabling the focused control drops focus too (Chromium), so a Save that
 * greys out once saved needs this as much as one that unmounts.
 */
export function keepFocus(
  anchor: Element | null | undefined,
  target?: () => HTMLElement | null | undefined,
): void {
  const dialog = anchor?.closest<HTMLElement>(DIALOG) ?? null
  window.setTimeout(() => {
    if (!lost()) return
    const preferred = target?.()
    if (preferred?.isConnected) {
      preferred.focus({ preventScroll: true })
      if (!lost()) return
    }
    if (dialog?.isConnected) dialog.focus({ preventScroll: true })
  }, 0)
}

/**
 * A pane that was jumped to (an Overview or Capabilities button) mounts after
 * the button that sent it is gone, so focus is on <body>. Once mounted, put
 * it on the pane's natural first stop, else on the section's rail item, as
 * if that had been clicked. A rail click keeps focus on the rail: untouched.
 */
export function useArrivalFocus(
  section: string,
  preferred?: () => HTMLElement | null | undefined,
): void {
  const first = useRef(preferred)
  useEffect(() => {
    keepFocus(null, () => first.current?.() ?? document.getElementById(`stg-rail-${section}`))
  }, [section])
}

/** The dialog an event happened in, captured before an async write lets its target go. */
export function sheetOf(e: { currentTarget: EventTarget | null }): HTMLElement | null {
  return e.currentTarget instanceof Element ? e.currentTarget.closest<HTMLElement>(DIALOG) : null
}

/**
 * Escape inside an in-section text field: the first press clears what was
 * typed and goes no further; with nothing typed it bubbles on and closes the
 * sheet, the way the rail's own search works.
 */
export function clearOnEscape(
  e: React.KeyboardEvent<HTMLInputElement>,
  value: string,
  clear: () => void,
): void {
  if (e.key !== 'Escape' || !value) return
  e.preventDefault()
  e.stopPropagation()
  clear()
}
