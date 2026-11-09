import { useEffect, useRef } from 'react'

const EDGE_PX = 24
const OPEN_DISTANCE_PX = 56

/**
 * Open the drawer on a deliberate rightward swipe that starts at the left
 * edge. A gesture that is more vertical than horizontal (scrolling the
 * transcript) never opens it.
 */
export function useEdgeSwipe(enabled: boolean, onOpen: () => void): void {
  const start = useRef<{ x: number; y: number } | null>(null)
  useEffect(() => {
    if (!enabled) return
    const onStart = (e: TouchEvent) => {
      const touch = e.touches[0]
      start.current =
        touch && touch.clientX <= EDGE_PX ? { x: touch.clientX, y: touch.clientY } : null
    }
    const onMove = (e: TouchEvent) => {
      const origin = start.current
      const touch = e.touches[0]
      if (!origin || !touch) return
      const dx = touch.clientX - origin.x
      const dy = Math.abs(touch.clientY - origin.y)
      if (dy > Math.abs(dx)) {
        start.current = null
        return
      }
      if (dx >= OPEN_DISTANCE_PX) {
        start.current = null
        onOpen()
      }
    }
    const onEnd = () => {
      start.current = null
    }
    document.addEventListener('touchstart', onStart, { passive: true })
    document.addEventListener('touchmove', onMove, { passive: true })
    document.addEventListener('touchend', onEnd)
    document.addEventListener('touchcancel', onEnd)
    return () => {
      document.removeEventListener('touchstart', onStart)
      document.removeEventListener('touchmove', onMove)
      document.removeEventListener('touchend', onEnd)
      document.removeEventListener('touchcancel', onEnd)
    }
  }, [enabled, onOpen])
}

/** Focus the chat composer if the new route has one, else the main area. */
export function focusMainTarget(): void {
  window.setTimeout(() => {
    const target =
      document.querySelector<HTMLElement>('[data-composer-input]') ??
      document.getElementById('main-content')
    target?.focus({ preventScroll: true })
  }, 0)
}
