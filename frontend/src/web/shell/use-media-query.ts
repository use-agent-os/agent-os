import { useSyncExternalStore } from 'react'

export const PHONE_QUERY = '(max-width: 768px)'

/** Live `matchMedia` result; false where matchMedia is unavailable. */
export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (onChange) => {
      let list: MediaQueryList
      try {
        list = window.matchMedia(query)
      } catch {
        return () => {}
      }
      list.addEventListener?.('change', onChange)
      return () => list.removeEventListener?.('change', onChange)
    },
    () => {
      try {
        return window.matchMedia(query).matches
      } catch {
        return false
      }
    },
    () => false,
  )
}
