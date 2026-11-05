import { create } from 'zustand'
import { isTradingAgentKey } from '~/views/trading/desk/agent'
import { BOOK_DEFAULT, BOOK_MAX, BOOK_MIN } from '~/views/trading/desk/desk-logic'
import { DEFAULT_MIN_TVL } from '~/views/trading/markets-logic'
import type { Token } from '~/views/trading/types'

/**
 * The desk's own chrome state: the BOOK's width and whether it is open, the
 * tab it shows, and the full-desk toggle. Width and open-ness persist; the
 * tab and mode are per launch.
 */

const WIDTH_KEY = 'agentos-desktop.trading.bookWidth'
const OPEN_KEY = 'agentos-desktop.trading.bookOpen'
const SESSION_KEY = 'agentos-desktop.trading.sessionKey'
const FILED_KEY = 'agentos-desktop.trading.sessionFiled'
/** The spec version of the `trading` agent this desktop last wrote to the gateway. */
const AGENT_VERSION_KEY = 'agentos-desktop.trading.agentVersion'
/** The chat the user left to enter Trading mode; the pill's "Chat" goes back there. */
const RETURN_KEY = 'agentos-desktop.trading.returnSession'

function loadWidth(): number {
  try {
    const raw = Number(localStorage.getItem(WIDTH_KEY))
    if (Number.isFinite(raw) && raw >= BOOK_MIN && raw <= BOOK_MAX) return raw
  } catch {
    /* storage unavailable */
  }
  return BOOK_DEFAULT
}

function loadOpen(): boolean {
  try {
    return localStorage.getItem(OPEN_KEY) !== 'false'
  } catch {
    return true
  }
}

function save(key: string, value: string): void {
  try {
    localStorage.setItem(key, value)
  } catch {
    /* storage unavailable */
  }
}

export type BookTab =
  'portfolio' | 'swap' | 'markets' | 'orders' | 'history' | 'tools' | 'automations'

/**
 * A swap asked for from anywhere — a Markets row, a chat card's Swap, a
 * Holdings row — for whichever ticket is on screen (the BOOK's or the full
 * desk's) to take. `seq` grows by one per request, so the same pair asked
 * for twice fills the ticket twice. `tokenOut` and `wallet` are optional:
 * a Holdings row names only what to sell and whose it is.
 */
export interface SwapRequest {
  chainId: number
  tokenIn: Token
  tokenOut?: Token
  wallet?: string
  seq: number
}

/**
 * The Markets tab's search and filters. Kept here rather than in the tab so
 * a trip to the ticket from a row's Swap, and back, finds the list as it was.
 */
export interface MarketsView {
  /** What the search field submitted: a symbol or an address. */
  query: string
  /** The token's address when the query came from a row that already knows it. */
  address: string | null
  /** A chain the user picked in the tab; null follows the desk (Robinhood by default). */
  chainId: number | null
  minTvlUsd: number
  lookalikes: boolean
  deep: boolean
}

const MARKETS_VIEW: MarketsView = {
  query: '',
  address: null,
  chainId: null,
  minTvlUsd: DEFAULT_MIN_TVL,
  lookalikes: false,
  deep: false,
}

/** A sheet the BOOK asks the chat to open: Send posts into the chat, so it lives there. */
export type DeskSheet =
  'pick' | 'send' | 'multisend' | 'allowances' | 'inspect' | 'network' | 'burn' | null

interface TradingUiStore {
  bookWidth: number
  bookOpen: boolean
  bookTab: BookTab
  /** The full-width desk instead of chat + BOOK. */
  deskMode: boolean
  sheet: DeskSheet
  /**
   * The active desk's "Start fresh" (pause the missions filed to this chat,
   * then mint a new desk session), published while the desk is up so the
   * shell's ⌘N can start a fresh desk session instead of leaving for Chat.
   */
  startFreshDesk: (() => void) | null
  /** The latest swap asked for; the ticket that takes it clears it. */
  swapRequest: SwapRequest | null
  markets: MarketsView
  setBookWidth(width: number): void
  toggleBook(): void
  setBookOpen(open: boolean): void
  setBookTab(tab: BookTab): void
  setDeskMode(on: boolean): void
  openSheet(sheet: DeskSheet): void
  setStartFreshDesk(fn: (() => void) | null): void
  /**
   * Fill a ticket with this pair. Beside the chat it also brings the BOOK
   * up on its Swap tab; at the full desk the ticket is always on screen and
   * the BOOK's own tab and open-ness are left as they were.
   */
  requestSwap(req: Omit<SwapRequest, 'seq'>): void
  /** The ticket took request `seq`; a newer one stays. */
  clearSwapRequest(seq: number): void
  /** Patch the Markets tab's search and filters. */
  setMarkets(patch: Partial<MarketsView>): void
  /**
   * Open Markets on one token (a Holdings row's Markets action). Beside the
   * chat this selects the BOOK's Markets tab; the full desk switches its own.
   */
  openMarkets(req: { chainId: number; address: string; symbol: string }): void
}

/** Survives a cleared request, so a seq is never handed out twice in one launch. */
let lastSwapSeq = 0

export const useTradingUi = create<TradingUiStore>((set) => ({
  bookWidth: loadWidth(),
  bookOpen: loadOpen(),
  bookTab: 'portfolio',
  deskMode: false,
  sheet: null,
  startFreshDesk: null,
  swapRequest: null,
  markets: MARKETS_VIEW,
  setBookWidth(width) {
    const clamped = Math.round(Math.min(BOOK_MAX, Math.max(BOOK_MIN, width)))
    save(WIDTH_KEY, String(clamped))
    set({ bookWidth: clamped })
  },
  toggleBook() {
    set((s) => {
      save(OPEN_KEY, String(!s.bookOpen))
      return { bookOpen: !s.bookOpen }
    })
  },
  setBookOpen(open) {
    save(OPEN_KEY, String(open))
    set({ bookOpen: open })
  },
  setBookTab(tab) {
    set({ bookTab: tab, bookOpen: true })
    save(OPEN_KEY, 'true')
  },
  setDeskMode(on) {
    set({ deskMode: on })
  },
  openSheet(sheet) {
    set({ sheet })
  },
  setStartFreshDesk(fn) {
    set({ startFreshDesk: fn })
  },
  requestSwap(req) {
    set((s) => {
      const swapRequest = { ...req, seq: ++lastSwapSeq }
      if (s.deskMode) return { swapRequest }
      save(OPEN_KEY, 'true')
      return { swapRequest, bookTab: 'swap', bookOpen: true }
    })
  },
  clearSwapRequest(seq) {
    set((s) => (s.swapRequest?.seq === seq ? { swapRequest: null } : {}))
  },
  setMarkets(patch) {
    set((s) => ({ markets: { ...s.markets, ...patch } }))
  },
  openMarkets({ chainId, address, symbol }) {
    set((s) => {
      const markets = { ...s.markets, query: symbol || address, address, chainId, deep: false }
      if (s.deskMode) return { markets }
      save(OPEN_KEY, 'true')
      return { markets, bookTab: 'markets', bookOpen: true }
    })
  },
}))

/**
 * The desk's newest chat, remembered across launches: where the pill, ⌘⇧T and
 * `/trading` enter. Older desk chats are still the desk's when opened from the
 * sidebar (`deriveMode`). A key from before the desk had its own agent
 * (`agent:main:…`) is not the desk's any more: it reads as absent, so a fresh
 * one is minted and the old chat stays in the sidebar.
 */
export function readTradingSessionKey(): string {
  try {
    const key = localStorage.getItem(SESSION_KEY) || ''
    return isTradingAgentKey(key) ? key : ''
  } catch {
    return ''
  }
}

export function writeTradingSessionKey(key: string): void {
  save(SESSION_KEY, key)
  save(FILED_KEY, 'false')
}

/** The desk's key, minted on first need so mode derivation can read it synchronously. */
export function ensureTradingSessionKey(mint: () => string): string {
  const existing = readTradingSessionKey()
  if (existing) return existing
  const fresh = mint()
  writeTradingSessionKey(fresh)
  return fresh
}

export function readReturnSession(): string | null {
  try {
    return sessionStorage.getItem(RETURN_KEY) || null
  } catch {
    return null
  }
}

export function writeReturnSession(key: string | null): void {
  try {
    if (key) sessionStorage.setItem(RETURN_KEY, key)
    else sessionStorage.removeItem(RETURN_KEY)
  } catch {
    /* storage unavailable */
  }
}

export function readTradingSessionFiled(): boolean {
  try {
    return localStorage.getItem(FILED_KEY) === 'true'
  } catch {
    return false
  }
}

export function writeTradingSessionFiled(filed: boolean): void {
  save(FILED_KEY, String(filed))
}

export function readTradingAgentVersion(): number {
  try {
    const raw = Number(localStorage.getItem(AGENT_VERSION_KEY))
    return Number.isFinite(raw) ? raw : 0
  } catch {
    return 0
  }
}

export function writeTradingAgentVersion(version: number): void {
  save(AGENT_VERSION_KEY, String(version))
}
