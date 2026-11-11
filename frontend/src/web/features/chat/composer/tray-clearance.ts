import { useLayoutEffect, type RefObject } from 'react'

/** The shell's approval tray adds this to its bottom offset to clear the composer. */
export const TRAY_CLEARANCE_VAR = '--tray-clearance'

/** How far up from the viewport's bottom edge the composer block reaches, in px. */
export function composerClearance(block: Element, viewportHeight: number): number {
  return Math.max(0, Math.round(viewportHeight - block.getBoundingClientRect().top))
}

/**
 * Publishes the height the composer occupies at the bottom of the viewport as
 * `--tray-clearance` on `:root`, so the shell's fixed approval tray sits above
 * it instead of on Send. It follows the composer as it grows (text,
 * attachments, the queue) and the window as it resizes, and is removed when
 * the composer unmounts.
 */
export function useTrayClearance(ref: RefObject<HTMLElement | null>): void {
  useLayoutEffect(() => {
    const wrapper = ref.current
    if (!wrapper) return
    const root = document.documentElement
    const update = () => {
      // The queue tab sits on top of the composer; the slash menu is transient
      // and does not move the tray.
      const block =
        wrapper.querySelector('.chat-queue') ?? wrapper.querySelector('.chat-composer') ?? wrapper
      root.style.setProperty(
        TRAY_CLEARANCE_VAR,
        `${composerClearance(block, window.innerHeight)}px`,
      )
    }
    update()
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(update)
    observer?.observe(wrapper)
    window.addEventListener('resize', update)
    return () => {
      observer?.disconnect()
      window.removeEventListener('resize', update)
      root.style.removeProperty(TRAY_CLEARANCE_VAR)
    }
  }, [ref])
}
