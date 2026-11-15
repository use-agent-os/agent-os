import { useEffect, useState } from 'react'

/**
 * The current time, re-read every `intervalMs`. For countdowns and "3m ago"
 * labels that must keep moving while the view is open. Pass 0 to freeze it;
 * switching back to a positive interval re-reads the clock at once, so a view
 * that was frozen for hours does not show stale ages until the first tick.
 */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (intervalMs <= 0) return
    const tick = () => setNow(Date.now())
    const first = setTimeout(tick, 0)
    const id = setInterval(tick, intervalMs)
    return () => {
      clearTimeout(first)
      clearInterval(id)
    }
  }, [intervalMs])
  return now
}
