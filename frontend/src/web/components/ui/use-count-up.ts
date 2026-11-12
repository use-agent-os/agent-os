import { useEffect, useRef, useState } from 'react'

const reducedMotion = () =>
  typeof window !== 'undefined' &&
  typeof window.matchMedia === 'function' &&
  window.matchMedia('(prefers-reduced-motion: reduce)').matches

/**
 * Counts a stat figure up to `target` (from 0 on mount, from the shown value
 * on change) with an ease-out, so a figure arrives rather than blinks in.
 * Returns the number to format; integers stay integers. Reduced motion and
 * non-finite targets return `target` at once.
 */
export function useCountUp(target: number, duration = 450): number {
  const [value, setValue] = useState(() => (reducedMotion() ? target : 0))
  const shown = useRef(value)
  useEffect(() => {
    if (reducedMotion() || !Number.isFinite(target) || !Number.isFinite(shown.current)) {
      shown.current = target
      setValue(target)
      return
    }
    const from = shown.current
    const start = performance.now()
    let frame = 0
    const tick = (now: number) => {
      // The first rAF timestamp is the frame's start, which can precede the
      // `performance.now()` above: clamp, or the ease-out goes negative.
      const p = Math.min(1, Math.max(0, (now - start) / duration))
      const eased = 1 - (1 - p) ** 3
      const next = from + (target - from) * eased
      shown.current = next
      setValue(Number.isInteger(target) ? Math.round(next) : next)
      if (p < 1) frame = requestAnimationFrame(tick)
    }
    frame = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(frame)
  }, [target, duration])
  return value
}
