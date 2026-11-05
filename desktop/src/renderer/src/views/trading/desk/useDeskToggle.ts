import { useCallback } from 'react'
import { useLocation, useNavigate } from 'react-router'
import { canonicalSessionKey } from '@/views/chat/logic'
import { ensureTradingSessionKey, readReturnSession, writeReturnSession } from '~/stores/trading-ui'
import { deriveMode, ENTER_STATE, mintTradingSessionKey, toggleTarget } from './mode-logic'

/** The session key in the current route, if the route is a session. */
export function sessionKeyFromPath(pathname: string): string | null {
  const m = /^\/sessions\/([^/]+)$/.exec(pathname)
  if (!m || !m[1]) return null
  try {
    return canonicalSessionKey(decodeURIComponent(m[1])) || null
  } catch {
    return null
  }
}

/**
 * One way in and out of Trading, shared by the pill, ⌘⇧T and the sidebar:
 * the desk's session is a route, so switching is a navigation that carries
 * ENTER_STATE when it enters (the entrance plays on that) and remembers the
 * chat it left so "Chat" leads back there.
 */
export function useDeskToggle(): { mode: 'chat' | 'trading'; toggle: () => void } {
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const currentKey = sessionKeyFromPath(pathname)
  const mode = deriveMode(currentKey)

  const toggle = useCallback(() => {
    const tradingKey = ensureTradingSessionKey(mintTradingSessionKey)
    const current = sessionKeyFromPath(pathname)
    const now = deriveMode(current)
    const target = toggleTarget({
      mode: now,
      tradingKey,
      currentKey: current,
      returnKey: readReturnSession(),
    })
    if (now === 'chat') {
      writeReturnSession(target.remember)
      void navigate(target.path, { state: ENTER_STATE })
    } else {
      void navigate(target.path)
    }
  }, [navigate, pathname])

  return { mode, toggle }
}
