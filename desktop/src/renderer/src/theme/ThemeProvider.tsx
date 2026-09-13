import { useEffect, useState, type ReactNode } from 'react'
import { initTheme, useTheme } from './theme-store'

/**
 * Boots the theme store once and holds children back until persisted
 * settings have been painted, so the first frame is already the right theme.
 * The store itself is global (zustand); this component only owns lifecycle.
 */
export function ThemeProvider({
  children,
  fallback = null,
}: {
  children: ReactNode
  fallback?: ReactNode
}) {
  const ready = useTheme((s) => s.ready)
  const [error, setError] = useState<Error | null>(null)

  useEffect(() => {
    let dispose: (() => void) | undefined
    let cancelled = false
    initTheme()
      .then((off) => {
        if (cancelled) off()
        else dispose = off
      })
      .catch((err: unknown) => setError(err instanceof Error ? err : new Error(String(err))))
    return () => {
      cancelled = true
      dispose?.()
    }
  }, [])

  if (error) throw error
  if (!ready) return <>{fallback}</>
  return <>{children}</>
}
