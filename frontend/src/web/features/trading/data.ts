import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo } from 'react'
import { useRpc } from '@/app/providers'
import { createEventInvalidator } from '@/lib/event-invalidation'
import type { WsRpcClient } from '@/lib/ws-rpc'
import { useConnection } from '@/stores/connection'
import { errorCode, sameAddress } from '@/views/trading/logic'
import type {
  BracketListPayload,
  ChainRead,
  Chart,
  ChartRange,
  Entry,
  LpBook,
  MandateListPayload,
  MarketsParams,
  MarketsPayload,
  Order,
  Portfolio,
  TradingStatus,
  TriggerListPayload,
  Wallet,
  WalletStatus,
} from '@/views/trading/types'

// The web console's Trading page looks and never acts. Every gateway call it
// makes goes through `read()`, which refuses anything off this list: a write
// (swap, send, approve, cancel, unlock, wallet changes) cannot be reached
// from here even by mistake. Each method was checked in
// src/agentos/gateway/rpc_trading.py to be a read: none is operator-only,
// none writes the ledger or the vault. Two of them take a flag that makes
// the engine go to the chain (`refresh` on wallet.balances and
// trading.lp.book); `read()` strips it.

export const TRADING_READ_METHODS = [
  'trading.status',
  'wallet.status',
  'wallet.list',
  'wallet.balances',
  'trading.portfolio',
  'trading.orders.list',
  'trading.history',
  'trading.chart',
  'trading.markets',
  'trading.lp.book',
  'trading.dca.list',
  'trading.trigger.list',
  'trading.bracket.list',
] as const

export type TradingReadMethod = (typeof TRADING_READ_METHODS)[number]

const READS: ReadonlySet<string> = new Set(TRADING_READ_METHODS)

/** Parameters that would make a read do work on the chain; never sent. */
const NEVER_SENT = ['refresh', 'checkRpc', 'full', 'fresh'] as const

type RpcLike = Pick<WsRpcClient, 'call' | 'waitForConnection'>

export async function read<T>(
  rpc: RpcLike,
  method: TradingReadMethod,
  params: Record<string, unknown> = {},
): Promise<T> {
  if (!READS.has(method)) throw new Error(`${method} is not a trading read`)
  const clean = { ...params }
  for (const key of NEVER_SENT) delete clean[key]
  await rpc.waitForConnection()
  return rpc.call<T>(method, clean)
}

/** Gateway events after which what is on screen is stale. */
export const TRADING_EVENTS = [
  'trading.changed',
  'trading.order.finished',
  'trading.dca.changed',
  'trading.trigger.changed',
  'trading.trigger.fired',
  'trading.bracket.changed',
  '_hello',
] as const

const ROOT = 'web-trading'

export const TRADING_KEYS = {
  status: [ROOT, 'status'] as const,
  vault: [ROOT, 'vault'] as const,
  wallets: [ROOT, 'wallets'] as const,
  portfolio: (wallet?: string) => [ROOT, 'portfolio', wallet ?? 'all'] as const,
  chainReads: (wallet?: string) => [ROOT, 'chain-reads', wallet ?? 'all'] as const,
  orders: (wallet?: string) => [ROOT, 'orders', wallet ?? 'all'] as const,
  history: (wallet?: string, chainId?: number) =>
    [ROOT, 'history', wallet ?? 'all', chainId ?? 'all'] as const,
  chart: (chainId: number, token: string, range: ChartRange) =>
    [ROOT, 'chart', chainId, token.toLowerCase(), range] as const,
  lpBook: (wallet?: string) => [ROOT, 'lp-book', wallet ?? 'all'] as const,
  dca: [ROOT, 'dca'] as const,
  triggers: [ROOT, 'triggers'] as const,
  brackets: [ROOT, 'brackets'] as const,
}

/**
 * Markets sit outside the `web-trading` prefix: `trading.changed` fires on
 * every order, and refetching per order would spend the price source's rate
 * limit on nothing new (the desktop keys them apart for the same reason).
 */
export function marketsKey(p: MarketsParams) {
  return ['web-trading-markets', p.chainId, p.target.trim().toLowerCase()] as const
}

function useConnected(): boolean {
  return useConnection((s) => s.state === 'connected')
}

/** Bind once from the page: a trading event refetches what is on screen (debounced). */
export function useTradingInvalidation(): void {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  useEffect(() => {
    // One request per query on the wire, however fast the engine emits.
    const invalidator = createEventInvalidator(queryClient, [ROOT])
    const offs = TRADING_EVENTS.map((event) => rpc.on(event, invalidator.notify))
    return () => {
      offs.forEach((off) => off())
      invalidator.dispose()
    }
  }, [rpc, queryClient])
}

export function useTradingStatus() {
  const rpc = useRpc()
  return useQuery<TradingStatus>({
    queryKey: TRADING_KEYS.status,
    enabled: useConnected(),
    queryFn: () => read(rpc, 'trading.status'),
    refetchInterval: 30_000,
    retry: false,
  })
}

export function useVaultStatus(enabled: boolean) {
  const rpc = useRpc()
  const connected = useConnected()
  return useQuery<WalletStatus>({
    queryKey: TRADING_KEYS.vault,
    enabled: connected && enabled,
    queryFn: () => read(rpc, 'wallet.status'),
    retry: false,
  })
}

export function useWallets(enabled: boolean) {
  const rpc = useRpc()
  const connected = useConnected()
  const query = useQuery<{ wallets?: Wallet[]; primary?: string | null }>({
    queryKey: TRADING_KEYS.wallets,
    enabled: connected && enabled,
    queryFn: () => read(rpc, 'wallet.list'),
  })
  const wallets = useMemo(() => query.data?.wallets ?? [], [query.data])
  return { ...query, wallets }
}

// The wallet-keyed reads keep no placeholder from the previous key: while
// wallet B loads, wallet A's figures must not stand under B's name.
export function usePortfolio(wallet: string | undefined, enabled: boolean) {
  const rpc = useRpc()
  const connected = useConnected()
  return useQuery<Portfolio>({
    queryKey: TRADING_KEYS.portfolio(wallet),
    enabled: connected && enabled,
    queryFn: () => read(rpc, 'trading.portfolio', wallet ? { wallet } : {}),
    refetchInterval: 20_000,
  })
}

/**
 * How the engine's last chain read of each shown wallet and network went, from
 * `wallet.balances` (the ledger's view, as the desktop's `useBalances` reads
 * it; never with `refresh`). Only reads that went wrong are returned: a
 * failed or partial read leaves the figures at their last good values, or at
 * nothing on a first sync. A failure of this read itself hides nothing.
 */
export function useChainReadProblems(wallet: string | undefined, enabled: boolean): ChainRead[] {
  const rpc = useRpc()
  const connected = useConnected()
  const query = useQuery<{ chains?: ChainRead[] }>({
    queryKey: TRADING_KEYS.chainReads(wallet),
    enabled: connected && enabled,
    queryFn: () => read(rpc, 'wallet.balances', wallet ? { address: wallet } : {}),
    refetchInterval: 60_000,
    retry: false,
  })
  return useMemo(() => chainReadProblems(query.data?.chains, wallet), [query.data, wallet])
}

/** The failed and partial reads among `reads`, for `wallet` when one is picked. */
export function chainReadProblems(reads: unknown, wallet: string | undefined): ChainRead[] {
  if (!Array.isArray(reads)) return []
  return (reads as ChainRead[]).filter(
    (r) =>
      r &&
      typeof r === 'object' &&
      typeof r.chainId === 'number' &&
      (r.status === 'failed' || r.status === 'partial') &&
      (!wallet || sameAddress(r.wallet, wallet)),
  )
}

export function useOrders(wallet: string | undefined, enabled: boolean) {
  const rpc = useRpc()
  const connected = useConnected()
  const query = useQuery<{ orders?: Order[] }>({
    queryKey: TRADING_KEYS.orders(wallet),
    enabled: connected && enabled,
    queryFn: () => read(rpc, 'trading.orders.list', { ...(wallet ? { wallet } : {}), limit: 50 }),
    refetchInterval: 15_000,
  })
  const orders = useMemo(() => query.data?.orders ?? [], [query.data])
  return { ...query, orders }
}

export function useHistory(wallet: string | undefined, chainId: number | undefined, on: boolean) {
  const rpc = useRpc()
  const connected = useConnected()
  const query = useQuery<{ entries?: Entry[] }>({
    queryKey: TRADING_KEYS.history(wallet, chainId),
    enabled: connected && on,
    queryFn: () =>
      read(rpc, 'trading.history', {
        ...(wallet ? { wallet } : {}),
        ...(chainId ? { chainId } : {}),
        limit: 100,
      }),
    refetchInterval: 30_000,
  })
  const entries = useMemo(() => query.data?.entries ?? [], [query.data])
  return { ...query, entries }
}

export function useChart(chainId: number, token: string | null, range: ChartRange) {
  const rpc = useRpc()
  const connected = useConnected()
  return useQuery<Chart>({
    queryKey: TRADING_KEYS.chart(chainId, token ?? '', range),
    enabled: connected && Boolean(token),
    queryFn: () => read(rpc, 'trading.chart', { chainId, token, range }),
    staleTime: 60_000,
  })
}

export function useMarkets(params: MarketsParams | null) {
  const rpc = useRpc()
  const connected = useConnected()
  const target = params?.target.trim() ?? ''
  return useQuery<MarketsPayload>({
    queryKey: params ? marketsKey(params) : ['web-trading-markets', 'none'],
    enabled: connected && target.length > 0,
    queryFn: () => read(rpc, 'trading.markets', { ...params, target }),
    staleTime: 60_000,
    refetchOnWindowFocus: false,
    retry: false,
  })
}

/** A token the engine offers when a symbol names several on one network. */
export interface SymbolCandidate {
  address: string
  symbol: string
  name: string
}

/** How many of the engine's candidates the page lists. */
export const CANDIDATES_MAX = 8

const CANDIDATE_ADDRESS = /^0x[0-9a-fA-F]{40}$/

/**
 * The tokens an ambiguous symbol could mean, from a `trading.markets` failure
 * (`trading.invalid` with `details.candidates`, src/agentos/trading/service.py
 * `resolve_token`), or null for any other failure. The list is third-party
 * data: only well-formed addresses on the asked network are kept, and only
 * text fields; the logo URL is never read. Null when none survives, so the
 * plain error shows instead of an empty choice.
 */
export function ambiguousCandidates(
  err: unknown,
  chainId: number,
): { shown: SymbolCandidate[]; omitted: number } | null {
  if (errorCode(err) !== 'trading.invalid') return null
  const details = (err as { details?: unknown }).details
  if (!details || typeof details !== 'object') return null
  const raw = (details as { candidates?: unknown }).candidates
  if (!Array.isArray(raw)) return null
  const seen = new Set<string>()
  const valid: SymbolCandidate[] = []
  for (const c of raw as unknown[]) {
    if (!c || typeof c !== 'object') continue
    const { address, symbol, name, chainId: chain } = c as Record<string, unknown>
    if (typeof address !== 'string' || !CANDIDATE_ADDRESS.test(address)) continue
    if (chain !== undefined && chain !== chainId) continue
    const key = address.toLowerCase()
    if (seen.has(key)) continue
    seen.add(key)
    valid.push({
      address,
      symbol: typeof symbol === 'string' ? symbol : '',
      name: typeof name === 'string' ? name : '',
    })
  }
  if (valid.length === 0) return null
  return {
    shown: valid.slice(0, CANDIDATES_MAX),
    omitted: Math.max(0, valid.length - CANDIDATES_MAX),
  }
}

/** The engine reads LP positions only while asked: keep this off unless its view is open. */
export function useLpBook(wallet: string | undefined, on: boolean) {
  const rpc = useRpc()
  const connected = useConnected()
  return useQuery<LpBook>({
    queryKey: TRADING_KEYS.lpBook(wallet),
    enabled: connected && on,
    queryFn: () => read(rpc, 'trading.lp.book', wallet ? { wallet } : {}),
    refetchInterval: 30_000,
  })
}

/** DCA mandates, triggers and brackets, live and finished, for the Orders view. */
export function useAutomations(on: boolean) {
  const rpc = useRpc()
  const connected = useConnected()
  const enabled = connected && on
  const dca = useQuery<MandateListPayload>({
    queryKey: TRADING_KEYS.dca,
    enabled,
    queryFn: () => read(rpc, 'trading.dca.list', { all: true }),
    refetchInterval: 60_000,
  })
  const triggers = useQuery<TriggerListPayload>({
    queryKey: TRADING_KEYS.triggers,
    enabled,
    queryFn: () => read(rpc, 'trading.trigger.list', { all: true }),
    refetchInterval: 60_000,
  })
  const brackets = useQuery<BracketListPayload>({
    queryKey: TRADING_KEYS.brackets,
    enabled,
    queryFn: () => read(rpc, 'trading.bracket.list', { all: true }),
    refetchInterval: 60_000,
  })
  return { dca, triggers, brackets }
}
