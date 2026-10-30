import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { useOverlayLayer } from '@/components/overlay-layer'
import { cn } from '~/lib/utils'

export interface Box {
  left: number
  top: number
  right: number
  bottom: number
}

export interface FloatPosition {
  left: number
  /** Below the anchor: the panel's top edge. */
  top?: number
  /** Above the anchor: the panel's bottom edge, from the bottom of the window. */
  bottom?: number
  maxHeight: number
}

/** Panels never grow past this, wherever they open. */
export const FLOAT_MAX_HEIGHT = 460
/** Below the anchor unless less than this fits there and more fits above. */
const MIN_BELOW = 220
const GAP = 4
const EDGE = 8

/**
 * Where a panel hung on `anchor` goes inside `bounds` (the canvas): directly
 * under it, or directly over it when there is clearly more room above, never
 * detached from it. The side it opens on caps its height instead of pushing
 * it away, so it always touches its anchor; horizontally it starts at the
 * anchor's left edge and is kept inside the bounds.
 */
export function placeFloating(
  anchor: Box,
  width: number,
  bounds: Box,
  windowHeight: number,
): FloatPosition {
  const inner = {
    left: bounds.left + EDGE,
    right: bounds.right - EDGE,
    top: bounds.top + EDGE,
    bottom: bounds.bottom - EDGE,
  }
  const below = inner.bottom - anchor.bottom - GAP
  const above = anchor.top - inner.top - GAP
  const maxLeft = Math.max(inner.left, inner.right - width)
  const left = Math.min(maxLeft, Math.max(inner.left, anchor.left))
  if (below >= MIN_BELOW || below >= above) {
    return {
      left,
      top: anchor.bottom + GAP,
      maxHeight: Math.max(120, Math.min(FLOAT_MAX_HEIGHT, below)),
    }
  }
  return {
    left,
    bottom: windowHeight - anchor.top + GAP,
    maxHeight: Math.max(120, Math.min(FLOAT_MAX_HEIGHT, above)),
  }
}

function windowBox(): Box {
  return { left: 0, top: 0, right: window.innerWidth, bottom: window.innerHeight }
}

/**
 * A panel floating over the window, hung on a control or a point: the
 * add-node search and the model picker. Portalled out of the canvas so the
 * canvas zoom never scales it; kept inside `bounds` (the canvas) and against
 * its anchor; Escape or a press outside closes it. Once it is placed and
 * visible, focus goes to its `[data-autofocus]` element (a field cannot take
 * focus while the panel is still hidden for measuring). Counts as an overlay
 * layer, so the editor's document shortcuts stand down while it is up.
 */
export function Floating({
  anchor,
  bounds,
  onClose,
  label,
  className,
  children,
}: {
  anchor: Box
  bounds?: Box | null
  onClose: () => void
  label: string
  className?: string
  children: ReactNode
}) {
  useOverlayLayer(true)
  const ref = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState<FloatPosition | null>(null)
  const closeRef = useRef(onClose)
  useEffect(() => {
    closeRef.current = onClose
  }, [onClose])

  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    setPos(placeFloating(anchor, el.offsetWidth, bounds ?? windowBox(), window.innerHeight))
  }, [anchor, bounds])

  const placed = pos !== null
  useEffect(() => {
    if (!placed) return
    const frame = requestAnimationFrame(() => {
      ref.current?.querySelector<HTMLElement>('[data-autofocus]')?.focus()
    })
    return () => cancelAnimationFrame(frame)
  }, [placed])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      e.preventDefault()
      e.stopPropagation()
      closeRef.current()
    }
    const onDown = (e: MouseEvent) => {
      if (ref.current?.contains(e.target as Node)) return
      closeRef.current()
    }
    const onBlur = () => closeRef.current()
    document.addEventListener('keydown', onKey, true)
    document.addEventListener('mousedown', onDown, true)
    window.addEventListener('blur', onBlur)
    window.addEventListener('resize', onBlur)
    return () => {
      document.removeEventListener('keydown', onKey, true)
      document.removeEventListener('mousedown', onDown, true)
      window.removeEventListener('blur', onBlur)
      window.removeEventListener('resize', onBlur)
    }
  }, [])

  return createPortal(
    <div
      ref={ref}
      role="dialog"
      aria-label={label}
      className={cn('flw-pop app-no-drag', className)}
      style={
        pos
          ? { left: pos.left, top: pos.top, bottom: pos.bottom, maxHeight: pos.maxHeight }
          : { left: 0, top: 0, visibility: 'hidden' }
      }
      onContextMenu={(e) => e.preventDefault()}
    >
      {children}
    </div>,
    document.body,
  )
}

/** A zero-size box at a point (the cursor), for panels opened where the pointer is. */
export function pointBox(x: number, y: number): Box {
  return { left: x, top: y, right: x, bottom: y }
}

/** Arrow/Enter handling for a list of rows inside a floating panel. */
export function moveActive(key: string, active: number, count: number): number | null {
  if (count === 0) return null
  if (key === 'ArrowDown') return (active + 1) % count
  if (key === 'ArrowUp') return (active - 1 + count) % count
  if (key === 'Home') return 0
  if (key === 'End') return count - 1
  return null
}
