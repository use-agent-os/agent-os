import { useEffect } from 'react'

/**
 * ⌘↵ presses the one action a surface is there for: the swap / bridge
 * ticket's review button, a sheet's primary footer button, a chat card's
 * approve. It acts only while focus is inside that surface, so with two
 * cards on screen the shortcut never guesses which one is meant.
 */

/** Marks the button ⌘↵ presses inside a scope that has no footer to search. */
export const CMD_ENTER_ATTR = 'data-cmd-enter'

export function isCmdEnter(e: KeyboardEvent): boolean {
  return (
    e.key === 'Enter' &&
    e.metaKey &&
    !e.ctrlKey &&
    !e.altKey &&
    !e.shiftKey &&
    // A held key auto-repeats: one press, one click, or an armed
    // two-step button would arm and then fire from a single press.
    !e.repeat &&
    !e.isComposing
  )
}

/** A button the person could press right now. */
export function isPressable(el: Element | null | undefined): el is HTMLElement {
  if (!(el instanceof HTMLElement) || !el.isConnected) return false
  if (el instanceof HTMLButtonElement && el.disabled) return false
  return el.getAttribute('aria-disabled') !== 'true' && el.getAttribute('aria-busy') !== 'true'
}

/** The last pressable element matching `selector` in `root` (the rightmost
 *  in a footer, where the committing action sits). */
export function lastPressable(root: ParentNode | null | undefined, selector: string) {
  if (!root) return null
  const found = Array.from(root.querySelectorAll(selector)).filter(isPressable)
  return found.at(-1) ?? null
}

/**
 * Listen for ⌘↵ on the element `getScope` returns and click what `pick`
 * returns. A native listener on purpose: DOM bubbling stops at a portal, so
 * ⌘↵ inside a sheet opened from a card never reaches the card behind it,
 * which React's tree-shaped bubbling would allow. When `pick` finds nothing
 * pressable the key is left alone for the focused control's own handling.
 */
export function useCmdEnter(
  getScope: () => HTMLElement | null | undefined,
  pick: (scope: HTMLElement) => HTMLElement | null = (scope) =>
    lastPressable(scope, `[${CMD_ENTER_ATTR}]`),
): void {
  // Re-bound every render: the scope can remount and `pick` closes over
  // render state, and adding one listener is cheaper than tracking either.
  useEffect(() => {
    const scope = getScope()
    if (!scope) return
    const onKeyDown = (e: KeyboardEvent) => {
      if (!isCmdEnter(e)) return
      const target = pick(scope)
      if (!isPressable(target)) return
      e.preventDefault()
      e.stopPropagation()
      target.click()
    }
    scope.addEventListener('keydown', onKeyDown)
    return () => scope.removeEventListener('keydown', onKeyDown)
  })
}
