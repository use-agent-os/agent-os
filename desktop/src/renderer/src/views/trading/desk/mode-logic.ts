import { canonicalSessionKey, webchatSessionKey } from '@/views/chat/logic'
import { sessionPath } from '~/components/sidebar/SessionRow'
import { isTradingAgentKey, TRADING_AGENT_ID } from './agent'

/**
 * Trading is not a page: it is what the chat becomes when the open session
 * is one of the desk's. Pure rules for that derivation, the pill's navigation
 * and the one entrance animation, so the shell stays thin.
 */

export type DeskMode = 'chat' | 'trading'

/** Route state that marks a navigation as "the user asked for the desk". */
export const ENTER_STATE = { enterDesk: true } as const

/** A desk session runs under the desk's own agent, never `main`. */
export function mintTradingSessionKey(): string {
  return webchatSessionKey(TRADING_AGENT_ID, 'trading-' + Math.random().toString(36).slice(2, 8))
}

/**
 * The mode the route is in, read off the key alone so it holds for every desk
 * chat in the sidebar, not just the newest. A conversation with the trading
 * agent is a desk chat: minted by the desk (`agent:trading:webchat:…`) or by
 * the desk project's "+" through `sessions.create` (`agent:trading:<id>`). Its
 * scheduled runs, helpers and channels (`agent:trading:cron:…`) stay plain chats.
 */
export function deriveMode(paramKey: string | null | undefined): DeskMode {
  if (!paramKey) return 'chat'
  const key = canonicalSessionKey(paramKey)
  if (!isTradingAgentKey(key)) return 'chat'
  const rest = key.slice(`agent:${TRADING_AGENT_ID}:`.length)
  return rest.startsWith('webchat:') || !rest.includes(':') ? 'trading' : 'chat'
}

/**
 * Where the pill goes. Into Trading: the newest desk chat, remembering the
 * chat we left. Back to Chat: the remembered chat, or the keyless home.
 */
export function toggleTarget(input: {
  mode: DeskMode
  tradingKey: string
  currentKey: string | null
  returnKey: string | null
}): { path: string; remember: string | null } {
  if (input.mode === 'trading') {
    const back = input.returnKey && input.returnKey !== input.tradingKey ? input.returnKey : null
    return { path: back ? sessionPath(back) : '/sessions', remember: null }
  }
  const remember =
    input.currentKey && input.currentKey !== input.tradingKey ? input.currentKey : null
  return { path: sessionPath(input.tradingKey), remember }
}

/** `/trading?order=…&desk=1` → the desk session, query preserved. */
export function redirectTarget(tradingKey: string, search: string): string {
  const q = search.startsWith('?') ? search : search ? `?${search}` : ''
  return `${sessionPath(tradingKey)}${q}`
}

/** Timings, mirrored by the custom properties at the top of desk.css. */
export const ENTRANCE_MS = 880
export const LEAVE_MS = 320

/**
 * The entrance plays once per user-initiated switch into Trading — never on
 * a re-render, a reconnect, or while an ask is pending (the desk is still).
 */
export function shouldPlayEntrance(input: {
  prevMode: DeskMode | null
  mode: DeskMode
  reducedMotion: boolean
  still: boolean
  /** The route was reached with ENTER_STATE (pill, ⌘⇧T, /trading redirect). */
  requested: boolean
}): boolean {
  if (input.mode !== 'trading') return false
  if (input.reducedMotion || input.still) return false
  if (input.prevMode === 'chat') return true
  return input.prevMode === null && input.requested
}
