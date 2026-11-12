import { useLayoutEffect, useRef, type ComponentProps } from 'react'

// The approval tray floats above `--tray-clearance` (on :root), so a bar
// docked at the bottom of the settings pane (the save bar, its "Saved"
// confirmation, the restart notice) is never covered by it. Each docked
// element reports the room it takes above the viewport's bottom edge (its
// height plus its CSS `bottom`); the largest one wins, and the variable is
// removed when none is shown.

const claims = new Map<object, number>()

function apply() {
  const root = document.documentElement.style
  const max = Math.max(0, ...claims.values())
  if (max > 0) root.setProperty('--tray-clearance', `${Math.ceil(max)}px`)
  else root.removeProperty('--tray-clearance')
}

/**
 * Put the returned ref on a bottom-docked element: while it is mounted and
 * laid out, it keeps the approval tray above itself. A hidden element
 * (display: none) claims nothing.
 */
export function useTrayClearance<T extends HTMLElement>() {
  const ref = useRef<T>(null)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const key = {}
    const measure = () => {
      const height = el.offsetHeight
      const bottom = parseFloat(getComputedStyle(el).bottom) || 0
      claims.set(key, height > 0 ? height + bottom : 0)
      apply()
    }
    measure()
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure)
    observer?.observe(el)
    return () => {
      observer?.disconnect()
      claims.delete(key)
      apply()
    }
  }, [])
  return ref
}

/** A bar docked at the pane's bottom that keeps the approval tray above it. */
export function Docked(props: ComponentProps<'div'>) {
  const ref = useTrayClearance<HTMLDivElement>()
  return <div ref={ref} {...props} />
}
