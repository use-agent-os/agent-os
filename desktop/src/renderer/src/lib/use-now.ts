import { useEffect, useState } from 'react'

/**
 * The current time, re-read every `intervalMs`. For countdowns and "3m ago"
 * labels that must keep moving while the view is open. Pass 0 to freeze it.
 */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (intervalMs <= 0) return
    const id = setInterval(() => setNow(Date.now()), intervalMs)
    return () => clearInterval(id)
  }, [intervalMs])
  return now
}
