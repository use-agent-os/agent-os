import { useRef } from 'react'

type AutoFocusHandler = (event: Event) => void

/**
 * Radix modals return focus on close only to their `<…Trigger>`. Most console
 * dialogs open from controlled state (a button's onClick, a shortcut, a row
 * menu), so without this focus would fall to <body> on Escape (WCAG 2.4.3).
 *
 * Remembers what had focus when the content opened — `onOpenAutoFocus` runs
 * before focus moves inside — and returns there on close. A caller's own
 * `onCloseAutoFocus` runs first and wins by calling `preventDefault()`.
 */
export function useRestoreFocus(
  onOpenAutoFocus?: AutoFocusHandler,
  onCloseAutoFocus?: AutoFocusHandler,
): { onOpenAutoFocus: AutoFocusHandler; onCloseAutoFocus: AutoFocusHandler } {
  const opener = useRef<HTMLElement | null>(null)
  return {
    onOpenAutoFocus: (event) => {
      const active = document.activeElement
      opener.current = active instanceof HTMLElement && active !== document.body ? active : null
      onOpenAutoFocus?.(event)
    },
    onCloseAutoFocus: (event) => {
      onCloseAutoFocus?.(event)
      const target = opener.current
      opener.current = null
      if (event.defaultPrevented || !target?.isConnected) return
      event.preventDefault()
      target.focus({ preventScroll: true })
    },
  }
}
