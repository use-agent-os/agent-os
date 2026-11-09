import { useMatches } from 'react-router'
import type { RouteHandle } from '@/web/app/routes'

/** The deepest matched route's handle: its title key and destination. */
export function useRouteHandle(): RouteHandle | null {
  const matches = useMatches()
  for (let i = matches.length - 1; i >= 0; i--) {
    const handle = matches[i]?.handle as RouteHandle | undefined
    if (handle?.titleKey) return handle
  }
  return null
}
