import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo } from 'react'
import { useRpc } from '@/app/providers'
import type { WsRpcClient } from '@/lib/ws-rpc'
import { useConnection } from '@/stores/connection'
import type {
  BracketListPayload,
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
    let timer: ReturnType<typeof setTimeout> | null = null
    const invalidate = () => {
      if (timer) return
      timer = setTimeout(() => {
        timer = null
        void queryClient.invalidateQueries({ queryKey: [ROOT] })
      }, 150)
    }
    const offs = TRADING_EVENTS.map((event) => rpc.on(event, invalidate))
    return () => {
      offs.forEach((off) => off())
      if (timer) clearTimeout(timer)
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
