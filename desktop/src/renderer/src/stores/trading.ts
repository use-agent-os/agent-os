import {
  useMutation,
  useQueries,
  useQuery,
  useQueryClient,
  type QueryClient,
} from '@tanstack/react-query'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { toast } from 'sonner'
import { useRpc } from '@/app/providers'
import { useConnection } from '@/stores/connection'
import { t, type MessageKey } from '~/i18n'
import { isTouchIdDeclined, requireBiometric, type GateKind } from '~/lib/biometric-gate'
import { errorText, QUOTE_REFRESH_MS } from '~/views/trading/logic'
import { answeredBracketFire } from '~/views/trading/desk/bracket-logic'
import { answeredFire } from '~/views/trading/desk/trigger-logic'
import {
  bracketAskById,
  bracketTouchId,
  mandateAskById,
  mandateTouchId,
  manualTradeTouchId,
  orderTouchId,
  triggerAskById,
  triggerTouchId,
  vaultReason,
  type BracketTouchOp,
  type MandateTouchOp,
  type TriggerTouchOp,
} from '~/views/trading/touch-id'
import { CHAINS } from '~/views/trading/types'
import type {
  AllowanceList,
  Balance,
  Bracket,
  BracketLeg,
  BracketListPayload,
  BracketPayload,
  ChainRead,
  Chart,
  ChartRange,
  Decoded,
  Entry,
  Limits,
  LpBook,
  Mandate,
  MandateListPayload,
  MandatePayload,
  MarketsParams,
  MarketsPayload,
  NetworkStatus,
  Order,
  OrderApproval,
  OrderKind,
  OrderStatus,
  Portfolio,
  ProbeResult,
  ProviderId,
  Quote,
  SearchToken,
  Token,
  TradingStatus,
  Trigger,
  TriggerListPayload,
  TriggerPayload,
  Wallet,
  WalletStatus,
} from '~/views/trading/types'

/**
 * Everything the Trading page reads from the gateway, as react-query hooks
 * over the wallet.* / trading.* RPC. Each hook names its query key so the
 * event listener below can knock the right ones stale: the gateway
 * broadcasts `trading.changed` after every order, sync, wallet or config
 * change, and `_hello` on reconnect.
 */

export const TRADING_KEYS = {
  status: ['trading', 'status'] as const,
  walletStatus: ['trading', 'wallet-status'] as const,
  wallets: ['trading', 'wallets'] as const,
  portfolio: (wallet?: string, includeHidden = false) =>
    ['trading', 'portfolio', wallet ?? 'all', includeHidden ? 'with-hidden' : 'shown'] as const,
  balances: (wallet?: string, includeHidden = false) =>
    ['trading', 'balances', wallet ?? 'all', includeHidden ? 'hidden' : 'visible'] as const,
  // `limit` and `wallet` shape the answer, so two callers asking for
  // different pages must not share one cache entry.
  orders: (status?: OrderStatus, limit?: number, wallet?: string, kind?: OrderKind) =>
    [
      'trading',
      'orders',
      status ?? 'any',
      limit ?? 'default',
      wallet ?? 'all',
      kind ?? 'any',
    ] as const,
  history: (wallet?: string, chainId?: number) =>
    ['trading', 'history', wallet ?? 'all', chainId ?? 'all'] as const,
  limits: (wallet: string) => ['trading', 'limits', wallet] as const,
  chart: (chainId: number, token: string, range: string) =>
    ['trading', 'chart', chainId, token.toLowerCase(), range] as const,
  allowances: (wallet?: string, chainId?: number) =>
    ['trading', 'allowances', wallet ?? 'primary', chainId ?? 'all'] as const,
  network: ['trading', 'network'] as const,
  /** The liquidity book (docs/lp-book.md), per wallet and chain filter. */
  lpBook: (wallet?: string, chainId?: number) =>
    ['trading', 'lp-book', wallet ?? 'all', chainId ?? 'all'] as const,
  /** DCA mandates (docs/dca.md): the live ones, or every one ever made. */
  dca: (all = false) => ['trading', 'dca', all ? 'all' : 'live'] as const,
  /** Price triggers (docs/triggers.md): the live ones, or every one ever made. */
  trigger: (all = false) => ['trading', 'trigger', all ? 'all' : 'live'] as const,
  /** Brackets (docs/brackets.md): the live ones, or every one ever made. */
  bracket: (all = false) => ['trading', 'bracket', all ? 'all' : 'live'] as const,
}

/** Gateway events after which trading data is stale. */
export const TRADING_EVENTS = [
  'trading.changed',
  'trading.approval.requested',
  'trading.order.finished',
  'trading.dca.changed',
  'trading.trigger.changed',
  'trading.trigger.fired',
  'trading.bracket.changed',
  '_hello',
] as const

export function invalidateTrading(queryClient: QueryClient): void {
  void queryClient.invalidateQueries({ queryKey: ['trading'] })
}

const FINISHED_STATUSES: ReadonlySet<string> = new Set(['done', 'stopped', 'rejected', 'expired'])

/**
 * Writes one refreshed object (a trigger, a bracket) straight into its cached
 * lists, before the invalidation refetches them. Every trigger and bracket
 * write answers with the full object, yet the desk waited for a list read (5–8 s
 * on a live engine) and a just-stopped bracket read "Armed" all along, its
 * Stop offered again. A fetch already in flight is cancelled first: it may
 * have started before the write and would land the old state over the new.
 * The live list drops an object that has finished; a list that does not hold
 * it is left to the refetch.
 */
export async function primeTradingList<T extends { id: string; status: string }>(
  queryClient: QueryClient,
  keyOf: (all?: boolean) => readonly unknown[],
  field: 'triggers' | 'brackets',
  item: T | null | undefined,
): Promise<void> {
  if (!item?.id) return
  await queryClient.cancelQueries({ queryKey: keyOf().slice(0, 2) })
  for (const all of [false, true]) {
    queryClient.setQueryData<Record<string, unknown>>(keyOf(all), (prev) => {
      const list = prev?.[field]
      if (!prev || !Array.isArray(list)) return prev
      const rows = list as T[]
      const at = rows.findIndex((row) => row?.id === item.id)
      if (at < 0) return prev
      const next =
        !all && FINISHED_STATUSES.has(item.status)
          ? rows.filter((row) => row?.id !== item.id)
          : rows.map((row, i) => (i === at ? item : row))
      return { ...prev, [field]: next }
    })
  }
}

/**
 * Bind once from a mounted page: every trading event refetches whatever is
 * on screen (debounced, so a burst of order updates is one round).
 */
export function useTradingInvalidation(enabled = true): void {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  useEffect(() => {
    if (!enabled) return
    let timer: ReturnType<typeof setTimeout> | null = null
    const invalidate = () => {
      if (timer) return
      timer = setTimeout(() => {
        timer = null
        invalidateTrading(queryClient)
      }, 150)
    }
    const offs = TRADING_EVENTS.map((event) => rpc.on(event, invalidate))
    return () => {
      offs.forEach((off) => off())
      if (timer) clearTimeout(timer)
    }
  }, [rpc, queryClient, enabled])
}

function useConnected(): boolean {
  return useConnection((s) => s.state === 'connected')
}

export function useTradingStatus(enabled = true) {
  const rpc = useRpc()
  const connected = useConnected()
  return useQuery<TradingStatus>({
    queryKey: TRADING_KEYS.status,
    enabled: connected && enabled,
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<TradingStatus>('trading.status', {})
    },
    refetchInterval: 30_000,
    refetchOnWindowFocus: true,
  })
}

export function useWalletStatus(enabled = true) {
  const rpc = useRpc()
  const connected = useConnected()
  return useQuery<WalletStatus>({
    queryKey: TRADING_KEYS.walletStatus,
    enabled: connected && enabled,
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<WalletStatus>('wallet.status', {})
    },
    refetchOnWindowFocus: true,
  })
}

interface WalletList {
  wallets?: Wallet[]
  primary?: string | null
}

export function useWallets(enabled = true) {
  const rpc = useRpc()
  const connected = useConnected()
  const query = useQuery<WalletList>({
    queryKey: TRADING_KEYS.wallets,
    enabled: connected && enabled,
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<WalletList>('wallet.list', {})
    },
    refetchOnWindowFocus: true,
  })
  const wallets = useMemo(() => query.data?.wallets ?? [], [query.data])
  return { ...query, wallets, primary: query.data?.primary ?? null }
}

/**
 * Holdings and totals. Junk tokens the engine hid are left out (and never
 * counted); `includeHidden` brings them back flagged `hidden`, for the
 * "show hidden" toggle.
 */
export function usePortfolio(wallet: string | undefined, enabled = true, includeHidden = false) {
  const rpc = useRpc()
  const connected = useConnected()
  return useQuery<Portfolio>({
    queryKey: TRADING_KEYS.portfolio(wallet, includeHidden),
    enabled: connected && enabled,
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<Portfolio>('trading.portfolio', {
        ...(wallet ? { wallet } : {}),
        ...(includeHidden ? { includeHidden: true } : {}),
      })
    },
    refetchInterval: 20_000,
    refetchOnWindowFocus: true,
    placeholderData: (prev) => prev,
  })
}

interface BalanceList {
  balances?: Balance[]
  /** Per wallet/chain freshness; see `ChainRead`. */
  chains?: ChainRead[]
  updatedAt?: number | null
}

/**
 * The ledger's view of a wallet's balances. This never makes the engine read
 * the chain: the engine's sync loop does, every settled swap does, and it
 * announces `trading.changed` when the ledger moves, which refetches this.
 * The interval is only a backstop for a missed event.
 */
/**
 * A wallet's holdings. `includeHidden` asks for the junk the engine normally
 * filters out too — flagged `hidden`, and the only way to see the dust a
 * burn is usually aimed at. It is part of the query key, so the two views
 * never overwrite each other in the cache.
 */
export function useBalances(wallet: string | undefined, enabled = true, includeHidden = false) {
  const rpc = useRpc()
  const connected = useConnected()
  const query = useQuery<BalanceList>({
    queryKey: TRADING_KEYS.balances(wallet, includeHidden),
    enabled: connected && enabled,
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<BalanceList>('wallet.balances', {
        ...(wallet ? { address: wallet } : {}),
        ...(includeHidden ? { includeHidden: true } : {}),
      })
    },
    refetchInterval: 60_000,
    placeholderData: (prev) => prev,
  })
  const balances = useMemo(() => query.data?.balances ?? [], [query.data])
  const chainReads = useMemo(() => query.data?.chains ?? [], [query.data])
  return { ...query, balances, chainReads }
}

interface OrderList {
  orders?: Order[]
  pendingApprovals?: number
}

export function useOrders(
  status: OrderStatus | undefined,
  enabled = true,
  limit = 50,
  wallet?: string,
  kind?: OrderKind,
) {
  const rpc = useRpc()
  const connected = useConnected()
  const query = useQuery<OrderList>({
    queryKey: TRADING_KEYS.orders(status, limit, wallet, kind),
    enabled: connected && enabled,
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<OrderList>('trading.orders.list', {
        ...(status ? { status } : {}),
        ...(wallet ? { wallet } : {}),
        ...(kind ? { kind } : {}),
        limit,
      })
    },
    refetchInterval: 15_000,
    refetchOnWindowFocus: true,
    placeholderData: (prev) => prev,
  })
  const orders = useMemo(() => query.data?.orders ?? [], [query.data])
  return { ...query, orders, pendingApprovals: query.data?.pendingApprovals ?? 0 }
}

interface BatchResult {
  orders?: Order[]
  batchId?: string
  /** The batch's approval: every leg and the digest an approve must echo. */
  approval?: OrderApproval | null
}

const NO_BATCHES: ReadonlyMap<string, Order[]> = new Map()

/**
 * Every leg of each multisend named, keyed by batch id. The orders page is a
 * window over the newest orders, and a batch's legs can fall on both sides of
 * its edge — while the engine decides every awaiting leg as one. A card must
 * therefore be built from the batch, not from the page. Keys start with
 * `trading`, so the event listener refreshes these with everything else.
 */
export function useBatchLegs(
  batchIds: readonly string[],
  enabled = true,
): ReadonlyMap<string, Order[]> {
  const rpc = useRpc()
  const connected = useConnected()
  const results = useQueries({
    queries: batchIds.map((batchId) => ({
      queryKey: ['trading', 'batch', batchId] as const,
      enabled: connected && enabled,
      queryFn: async () => {
        await rpc.waitForConnection()
        return rpc.call<BatchResult>('trading.orders.batch', { batchId })
      },
      refetchInterval: 15_000,
      placeholderData: (prev: BatchResult | undefined) => prev,
    })),
  })
  // `results` is a new array every render; the stamps are what changes.
  const stamps = results.map((r) => r.dataUpdatedAt).join(',')
  const ids = batchIds.join(',')
  return useMemo(() => {
    if (batchIds.length === 0) return NO_BATCHES
    const map = new Map<string, Order[]>()
    results.forEach((r, i) => {
      const id = batchIds[i]
      if (id && r.data?.orders) map.set(id, r.data.orders)
    })
    return map
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stamps, ids])
}

/** The number on the sidebar badge. Cheap: one small query, event-driven. */
export function usePendingApprovals(enabled = true): number {
  const { pendingApprovals, orders } = useOrders('awaiting_approval', enabled, 20)
  return pendingApprovals || orders.length
}

interface HistoryPage {
  entries?: Entry[]
  nextBefore?: number | null
}

export function useHistory(
  wallet: string | undefined,
  chainId: number | undefined,
  enabled = true,
) {
  const rpc = useRpc()
  const connected = useConnected()
  const query = useQuery<HistoryPage>({
    queryKey: TRADING_KEYS.history(wallet, chainId),
    enabled: connected && enabled,
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<HistoryPage>('trading.history', {
        ...(wallet ? { wallet } : {}),
        ...(chainId ? { chainId } : {}),
        limit: 100,
      })
    },
    refetchInterval: 30_000,
    placeholderData: (prev) => prev,
  })
  const entries = useMemo(() => query.data?.entries ?? [], [query.data])
  return { ...query, entries, nextBefore: query.data?.nextBefore ?? null }
}

export function useLimits(wallet: string | null) {
  const rpc = useRpc()
  const connected = useConnected()
  return useQuery<Limits>({
    queryKey: TRADING_KEYS.limits(wallet ?? ''),
    enabled: connected && Boolean(wallet),
    queryFn: async () => {
      await rpc.waitForConnection()
      const raw = await rpc.call<Limits & { approvalThresholdUsd?: number }>('trading.limits', {
        wallet,
      })
      // The engine names the threshold `approvalThresholdUsd`; the desk reads `thresholdUsd`.
      return { ...raw, thresholdUsd: raw.thresholdUsd ?? raw.approvalThresholdUsd ?? 0 }
    },
    refetchInterval: 30_000,
  })
}

export function useChart(chainId: number, token: string | null, range: ChartRange) {
  const rpc = useRpc()
  const connected = useConnected()
  return useQuery<Chart>({
    queryKey: TRADING_KEYS.chart(chainId, token ?? '', range),
    enabled: connected && Boolean(token),
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<Chart>('trading.chart', { chainId, token, range })
    },
    staleTime: 60_000,
  })
}

export interface QuoteParams {
  chainId: number
  wallet: string
  tokenIn: string
  tokenOut: string
  amountIn: string
  slippagePct?: number
}

/**
 * A live price for the swap form. Refetches every 15 s while the form is
 * complete; `fetchedAt` drives the countdown ring so the person can see
 * the price ageing rather than being surprised by a stale one.
 */
export function useQuote(params: QuoteParams | null) {
  const rpc = useRpc()
  const connected = useConnected()
  const key = params
    ? [
        'trading',
        'quote',
        params.chainId,
        params.wallet,
        params.tokenIn.toLowerCase(),
        params.tokenOut.toLowerCase(),
        params.amountIn,
        params.slippagePct ?? 'auto',
      ]
    : ['trading', 'quote', 'none']
  const query = useQuery<Quote>({
    queryKey: key,
    enabled: connected && params !== null,
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<Quote>('trading.quote', { ...params, initiator: 'manual' })
    },
    refetchInterval: QUOTE_REFRESH_MS,
    refetchOnWindowFocus: false,
    retry: false,
    staleTime: QUOTE_REFRESH_MS,
    placeholderData: (prev) => prev,
  })
  // A price shown from the previous params is about another swap: it is
  // already expired as far as the countdown is concerned.
  const fetchedAt = query.isPlaceholderData ? 0 : query.dataUpdatedAt
  return { ...query, fetchedAt }
}

export interface SwapParams {
  chainId: number
  wallets?: string[] | 'all'
  tokenIn: string
  tokenOut: string
  amountIn?: string
  amountPct?: number
  slippagePct?: number
  note?: string
  /** The quote the person confirmed, in base units: the engine re-quotes and
   *  answers `trading.price_moved` instead of filling at a worse price. */
  expectedOutRaw?: string
  minOutRaw?: string
  quoteId?: string
}

interface SwapResult {
  orders: Order[]
}

/**
 * A swap typed by the user: it runs at once, so with Touch ID on the
 * fingerprint comes first (`touchId` names it in the sheet; never sent).
 */
export function useSwap() {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ touchId, ...params }: SwapParams & { touchId?: WriteContext }) =>
      gatedCall<SwapResult>(
        (m, p) => rpc.call(m, p),
        'trading.swap',
        { ...params, initiator: 'manual' },
        touchId,
      ),
    onSettled: () => invalidateTrading(queryClient),
  })
}

export interface SendRecipient {
  to: string
  amount?: string
  amountUsd?: number
}

export interface SendParams {
  chainId: number
  wallet?: string
  token: string
  recipients: SendRecipient[]
  note?: string
}

interface SendResult {
  orders: Order[]
  batchId: string | null
}

/**
 * A send typed by the user: it runs at once, one leg per recipient, so with
 * Touch ID on the fingerprint comes first (`touchId` names it; never sent).
 */
export function useSend() {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ touchId, ...params }: SendParams & { touchId?: WriteContext }) =>
      gatedCall<SendResult>(
        (m, p) => rpc.call(m, p),
        'trading.send',
        { ...params, initiator: 'manual' },
        touchId,
      ),
    onSettled: () => invalidateTrading(queryClient),
  })
}

/**
 * What a wallet has approved others to spend. The engine scans the wallet's
 * own Approval logs incrementally and reads each allowance live, so a row
 * that shows here is spendable right now.
 */
export function useAllowances(wallet: string | undefined, chainId?: number, enabled = true) {
  const rpc = useRpc()
  const connected = useConnected()
  const query = useQuery<AllowanceList>({
    queryKey: TRADING_KEYS.allowances(wallet, chainId),
    enabled: connected && enabled,
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<AllowanceList>('trading.allowances.list', {
        ...(wallet ? { wallet } : {}),
        ...(chainId ? { chainId } : {}),
      })
    },
    // A first pass over an old wallet takes a while; while the engine is
    // still walking blocks the list is re-read often, then settles down.
    refetchInterval: (q) => (q.state.data?.scanning ? 3_000 : 60_000),
    placeholderData: (prev) => prev,
  })
  const allowances = useMemo(() => query.data?.allowances ?? [], [query.data])
  return {
    ...query,
    allowances,
    unlimitedCount: query.data?.unlimitedCount ?? 0,
    scanning: query.data?.scanning === true,
  }
}

/** `approve(spender, 0)`, typed by the user: runs at once. */
export function useRevoke() {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (params: { chainId: number; wallet?: string; token: string; spender: string }) =>
      rpc.call<{ order: Order }>('trading.allowances.revoke', { ...params, initiator: 'manual' }),
    onSettled: () => invalidateTrading(queryClient),
  })
}

/** Head block, block age, gas and RPC latency per chain; the engine caches it briefly. */
export function useNetwork(enabled = true, intervalMs = 20_000) {
  const rpc = useRpc()
  const connected = useConnected()
  const query = useQuery<NetworkStatus>({
    queryKey: TRADING_KEYS.network,
    enabled: connected && enabled,
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<NetworkStatus>('trading.network', {})
    },
    refetchInterval: intervalMs,
    refetchOnWindowFocus: true,
    placeholderData: (prev) => prev,
  })
  const chains = useMemo(() => query.data?.chains ?? [], [query.data])
  return { ...query, chains }
}

/** Explain a transaction hash, or raw calldata, on a chain. */
export function useDecode() {
  const rpc = useRpc()
  return useMutation({
    mutationFn: (params: { chainId: number; txHash?: string; data?: string; to?: string }) =>
      rpc.call<Decoded>('trading.decode', params),
  })
}

/* ── Touch ID: the one door in front of every money-moving write ───────── */

/**
 * What the caller of a write already holds, so the Touch ID sheet can name
 * what the fingerprint approves without a read. Never sent to the engine.
 */
export interface WriteContext {
  order?: Order
  /** A batch's legs: the engine approves every one at once. */
  legs?: readonly Order[]
  mandate?: Mandate
  trigger?: Trigger
  bracket?: Bracket
  /** A swap or send typed at the desk: its words ("0.5 ETH → USDC")… */
  what?: string
  /** …and what it moves in USD, when known (unknown is high-risk). */
  valueUsd?: number | null
  /** The wallet's name, for a vault write. */
  label?: string
}

type RpcCall = (method: string, params: Record<string, unknown>) => Promise<unknown>

interface WriteAsk {
  kind: GateKind
  reason: string
  /** What is asking, for `useTouchIdPrompt`. */
  key: string | null
}

/**
 * How one write is asked for. null: the write is never gated; a gate that
 * resolves null: these params move nothing (a DCA rename).
 */
type WriteGate =
  | ((
      params: Record<string, unknown>,
      ctx: WriteContext,
      call: RpcCall,
    ) => Promise<WriteAsk | null>)
  | null

const idOf = (params: Record<string, unknown>, field: string): string =>
  typeof params[field] === 'string' ? (params[field] as string) : ''

function mandateGate(op: Exclude<MandateTouchOp, 'create'>): NonNullable<WriteGate> {
  return async (params, ctx, call) => {
    const id = idOf(params, 'mandateId')
    const ask =
      ctx.mandate && ctx.mandate.id === id
        ? mandateTouchId(op === 'update' ? patchedMandate(ctx.mandate, params) : ctx.mandate, op)
        : await mandateAskById(call, id, op)
    return { ...ask, key: `mandate:${id}` }
  }
}

function triggerGate(op: TriggerTouchOp): WriteGate {
  return async (params, ctx, call) => {
    const id = idOf(params, 'triggerId')
    const ask =
      ctx.trigger && ctx.trigger.id === id
        ? triggerTouchId(ctx.trigger, op)
        : await triggerAskById(call, id, op)
    return { ...ask, key: `trigger:${id}` }
  }
}

function bracketGate(op: BracketTouchOp): WriteGate {
  return async (params, ctx, call) => {
    const id = idOf(params, 'bracketId')
    const ask =
      ctx.bracket && ctx.bracket.id === id
        ? bracketTouchId(ctx.bracket, op)
        : await bracketAskById(call, id, op)
    return { ...ask, key: `bracket:${id}` }
  }
}

function manualGate(op: 'swap' | 'send'): WriteGate {
  return async (params, ctx) => ({
    ...manualTradeTouchId(op, {
      what: ctx.what ?? '',
      chainId: typeof params.chainId === 'number' ? params.chainId : 0,
      valueUsd: ctx.valueUsd,
    }),
    key: `trade:${op}`,
  })
}

function vaultGate(action: 'export' | 'remove'): WriteGate {
  return async (params, ctx) => {
    const address = idOf(params, 'address')
    const which =
      action === 'remove' ? 'remove' : params.format === 'privateKey' ? 'export' : 'exportKeystore'
    return {
      kind: 'vault',
      reason: vaultReason(which, { address, label: ctx.label }),
      key: `wallet:${address.toLowerCase()}`,
    }
  }
}

/** `trading.dca.update` params read as the mandate they would leave behind. */
function patchedMandate(m: Mandate, patch: Record<string, unknown>): Mandate {
  const num = (k: string): number | undefined =>
    typeof patch[k] === 'number' ? (patch[k] as number) : undefined
  const runsMax = num('runsMax')
  return {
    ...m,
    name: typeof patch.name === 'string' && patch.name ? patch.name : m.name,
    budget: {
      ...m.budget,
      usdPerRun: num('usdPerRun') ?? m.budget?.usdPerRun,
      capUsd: num('capUsd') ?? m.budget?.capUsd,
    },
    runs: { ...m.runs, max: runsMax === undefined ? m.runs?.max : runsMax || null },
  }
}

/** An operator's own `trading.dca.create`: the mandate is active at once. */
const createMandateGate: WriteGate = async (params) => {
  const num = (k: string): number | undefined =>
    typeof params[k] === 'number' ? (params[k] as number) : undefined
  const chainId = num('chainId') ?? 0
  const draft = {
    name: typeof params.name === 'string' ? params.name : '',
    chain: { id: chainId, name: '' },
    budget: { usdPerRun: num('usdPerRun'), capUsd: num('capUsd') },
    runs: { max: num('runsMax') ?? null },
  } as unknown as Mandate
  return { ...mandateTouchId(draft, 'create'), key: 'mandate:new' }
}

/** A `trading.dca.update` that renames only moves nothing; anything else may spend more. */
const updateMandateGate: WriteGate = async (params, ctx, call) => {
  const changes = Object.keys(params).filter((k) => k !== 'mandateId' && k !== 'name')
  return changes.length ? mandateGate('update')(params, ctx, call) : null
}

/**
 * Every trading/wallet write the renderer sends, and how Touch ID stands in
 * front of it (Settings › Security). A gated write is one that places,
 * fills or fires a trade, or grants (or re-grants) a standing permission to
 * spend: approvals, "Fire now" / "Buy now" / "Sell now", resumes, a swap or
 * send typed at the desk, an operator's own DCA, and a key export or a
 * wallet removal. Nothing else is: a reject, pause or stop only takes
 * permission away; an LP collect/remove parks an order that is approved
 * (and gated) on its own card.
 *
 * `gatedCall` is the only way the store sends a write, and a method missing
 * from this table is asked for as high-risk: a new write cannot skip the
 * gate by being forgotten here (`stores/trading.test.ts` checks every write
 * in `DESKTOP_REQUIRED_METHODS` is classified).
 */
export const TRADING_WRITE_GATES: Readonly<Record<string, WriteGate>> = {
  'trading.orders.approve': async (params, ctx) => {
    const orderId = idOf(params, 'orderId')
    const ask = ctx.order
      ? orderTouchId(ctx.order, ctx.legs?.length ? ctx.legs : undefined)
      : {
          kind: 'approve-high' as const,
          reason: t('trading.touchId.reason.orderId').replace('{id}', orderId),
        }
    return { ...ask, key: orderId }
  },
  'trading.orders.reject': null,
  'trading.swap': manualGate('swap'),
  'trading.send': manualGate('send'),
  'trading.allowances.revoke': null,
  'trading.unwrap': null,
  'trading.sync': null,
  'trading.tokens.hide': null,
  'trading.setProvider': null,
  'trading.probe': null,
  'trading.lp.collect': null,
  'trading.lp.remove': null,
  'trading.dca.create': createMandateGate,
  'trading.dca.update': updateMandateGate,
  'trading.dca.approve': mandateGate('approve'),
  'trading.dca.run': mandateGate('run'),
  'trading.dca.resume': mandateGate('resume'),
  'trading.dca.reject': null,
  'trading.dca.pause': null,
  'trading.dca.stop': null,
  'trading.trigger.approve': triggerGate('approve'),
  'trading.trigger.fire': triggerGate('fire'),
  'trading.trigger.resume': triggerGate('resume'),
  'trading.trigger.reject': null,
  'trading.trigger.pause': null,
  'trading.trigger.stop': null,
  'trading.bracket.approve': bracketGate('approve'),
  'trading.bracket.fire': bracketGate('fire'),
  'trading.bracket.resume': bracketGate('resume'),
  'trading.bracket.reject': null,
  'trading.bracket.pause': null,
  'trading.bracket.stop': null,
  'wallet.export': vaultGate('export'),
  'wallet.remove': vaultGate('remove'),
  'wallet.removeBatch': async (params) => {
    const n = Array.isArray(params.addresses) ? params.addresses.length : 0
    return {
      kind: 'vault',
      reason: t('trading.touchId.reason.removeBatch').replace('{n}', String(n)),
      key: 'wallet:remove-batch',
    }
  },
  'wallet.create': null,
  'wallet.createBatch': null,
  // The keys and phrases of the wallets just made. With the vault password it
  // is an export like any other; `touchId: true` means the sheet has just had
  // the operator confirm with Touch ID itself (whatever Settings › Security
  // says), so it is not asked twice.
  'wallet.exportBatch': async (params) =>
    params.touchId === true
      ? null
      : { kind: 'vault', reason: t('trading.touchId.reason.exportBatch'), key: 'wallet:batch' },
  'wallet.import': null,
  'wallet.importBatch': null,
  'wallet.lock': null,
  'wallet.rename': null,
  'wallet.setPrimary': null,
  'wallet.setUnlockMode': null,
  'wallet.setup': null,
  'wallet.unlock': null,
}

/** A write nobody classified: asked for as high-risk, naming the method. */
const unknownWriteGate =
  (method: string): NonNullable<WriteGate> =>
  async () => ({
    kind: 'approve-high',
    reason: t('trading.touchId.reason.write').replace('{method}', method),
    key: null,
  })

/**
 * Touch ID for one write, when Settings › Security asks for it. Resolves
 * when the write may go; throws `TouchIdDeclined` (already toasted) when it
 * may not, with nothing sent.
 */
export async function requireWriteTouchId(
  call: RpcCall,
  method: string,
  params: Record<string, unknown>,
  ctx: WriteContext = {},
): Promise<void> {
  const gate = Object.hasOwn(TRADING_WRITE_GATES, method)
    ? TRADING_WRITE_GATES[method]
    : unknownWriteGate(method)
  if (!gate) return
  const ask = await gate(params, ctx, call)
  if (ask) await requireBiometric(ask.kind, ask.reason, ask.key)
}

/** Send one write through the gate: the prompt first, the RPC only once it passed. */
export async function gatedCall<T>(
  call: RpcCall,
  method: string,
  params: Record<string, unknown>,
  ctx: WriteContext = {},
): Promise<T> {
  await requireWriteTouchId(call, method, params, ctx)
  return (await call(method, params)) as T
}

/** One decision on an order. `order` (and a batch's `legs`) name an approval in the Touch ID sheet. */
export interface OrderDecision {
  orderId: string
  approve: boolean
  order?: Order
  legs?: readonly Order[]
}

/**
 * The one door every order decision goes through (the desk's cards, the
 * BOOK, the Trading tab), so an approval cannot skip Touch ID: with the
 * setting on, the prompt comes first and a cancelled or failed one throws
 * `TouchIdDeclined` (already toasted) without sending anything. A batch leg
 * decided without its legs reads them from the cache, as the card does.
 */
export function useOrderDecision() {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  return useMutation({
    // A reject carries no reason here: the engine records the bare "user"
    // itself, and a placeholder reason was stored as "user: user".
    mutationFn: async ({ orderId, approve, order, legs }: OrderDecision) => {
      const batchId = order?.batchId
      if (!approve) {
        return gatedCall<{ order: Order }>(
          (m, p) => rpc.call(m, p),
          'trading.orders.reject',
          { orderId },
          { order, legs },
        )
      }
      let batch = batchId
        ? queryClient.getQueryData<BatchResult>(['trading', 'batch', batchId])
        : undefined
      if (batchId && !batch?.approval?.digest) {
        // One approve runs every leg, so the engine takes it only with the
        // digest it serves beside all of them (trading.orders.batch).
        batch = await queryClient.fetchQuery({
          queryKey: ['trading', 'batch', batchId] as const,
          queryFn: () => rpc.call<BatchResult>('trading.orders.batch', { batchId }),
          staleTime: 0,
        })
      }
      const cached = batch?.orders
      const digest = batchId ? batch?.approval?.digest : order?.approval?.digest
      return gatedCall<{ order: Order }>(
        (m, p) => rpc.call(m, p),
        'trading.orders.approve',
        digest ? { orderId, approvalDigest: digest } : { orderId },
        { order, legs: legs?.length ? legs : cached },
      )
    },
    onSettled: () => invalidateTrading(queryClient),
  })
}

/**
 * Hide a junk token, or show one the engine hid. The user's call is final:
 * the engine's classifier never reverses it.
 */
export function useTokenVisibility() {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (params: { chainId: number; address: string; hidden: boolean }) =>
      rpc.call<{ token: Token & { hidden: boolean } }>('trading.tokens.hide', params),
    onSettled: () => invalidateTrading(queryClient),
  })
}

/** WETH an L2 handed back instead of ETH, turned into ETH (amount omitted = all). */
export function useUnwrap() {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (params: { chainId: number; wallet: string; amount?: string }) =>
      rpc.call<{ txHash: string }>('trading.unwrap', params),
    onSettled: () => invalidateTrading(queryClient),
  })
}

/**
 * Every LP position of the desk's wallets (Uniswap, Aero, Aerodrome), from
 * the engine's cache. The engine reads the chain only while someone asks:
 * keep this disabled unless the Liquidity tab is on screen. It answers at
 * once and refreshes behind the answer; `trading.changed` (which knocks every
 * `['trading']` key stale) says when the fresh rows are in, so the interval
 * is only what keeps the engine's cache warm while the tab stays open.
 */
export function useLpBook(wallet: string | undefined, chainId: number | undefined, enabled = true) {
  const rpc = useRpc()
  const connected = useConnected()
  return useQuery<LpBook>({
    queryKey: TRADING_KEYS.lpBook(wallet, chainId),
    enabled: connected && enabled,
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<LpBook>('trading.lp.book', {
        ...(wallet ? { wallet } : {}),
        ...(chainId ? { chainId } : {}),
      })
    },
    refetchInterval: 30_000,
    refetchOnWindowFocus: true,
    placeholderData: (prev) => prev,
  })
}

/**
 * Collect a Uniswap V4 position's fees, or take the whole position out. Either
 * parks an order for approval: nothing is signed until its card is approved
 * (which is where Touch ID stands), so the write itself is not gated.
 */
export function useLpWrite() {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (params: { action: 'collect' | 'remove'; chainId: number; tokenId: string }) =>
      gatedCall<{ order: Order }>(
        (m, p) => rpc.call(m, p),
        params.action === 'collect' ? 'trading.lp.collect' : 'trading.lp.remove',
        { chainId: params.chainId, tokenId: params.tokenId, initiator: 'manual' },
      ),
    onSettled: () => invalidateTrading(queryClient),
  })
}

/** Ask the engine to re-read the chain now (it honours one every few seconds). */
export function useLpRefresh() {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (params: { wallet?: string; chainId?: number }) =>
      rpc.call<LpBook>('trading.lp.book', { ...params, refresh: true }),
    onSuccess: (book, params) =>
      queryClient.setQueryData(TRADING_KEYS.lpBook(params.wallet, params.chainId), book),
  })
}

export function useSync() {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (params: { wallet?: string; full?: boolean }) =>
      rpc.call<{ started: boolean }>('trading.sync', params),
    onSettled: () => invalidateTrading(queryClient),
  })
}

/**
 * One mutation for every vault/wallet write; the sheet picks the method.
 * A private-key export and a wallet removal ask for Touch ID
 * first whenever Settings › Security is not Off, whatever the risk: the
 * prompt runs before the password leaves the renderer, and a declined one
 * throws `TouchIdDeclined` (already toasted) with nothing sent.
 */
export function useWalletMutation<T = unknown>() {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({
      method,
      params,
      label,
    }: {
      method: string
      params: Record<string, unknown>
      /** The wallet's name, for the Touch ID sheet; never sent. */
      label?: string
    }) => {
      const address = typeof params.address === 'string' ? params.address : ''
      const known =
        label ??
        queryClient
          .getQueryData<WalletList>(TRADING_KEYS.wallets)
          ?.wallets?.find((w) => w.address.toLowerCase() === address.toLowerCase())?.label
      return gatedCall<T>((m, p) => rpc.call(m, p), method, params, { label: known })
    },
    onSettled: () => invalidateTrading(queryClient),
  })
}

/** Make a provider the one that routes swaps (the toasts belong to the caller). */
export function useSetProvider() {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (provider: ProviderId) =>
      rpc.call<{ provider: ProviderId }>('trading.setProvider', { provider }),
    onSettled: () => invalidateTrading(queryClient),
  })
}

/** Try a provider: the configured one, or a named one, with an optional typed key. */
export function useProbe() {
  const rpc = useRpc()
  return useMutation({
    mutationFn: (params: { provider?: ProviderId; apiKey?: string } = {}) =>
      rpc.call<ProbeResult>('trading.probe', {
        ...(params.provider ? { provider: params.provider } : {}),
        ...(params.apiKey ? { apiKey: params.apiKey } : {}),
      }),
  })
}

interface SearchResult {
  tokens?: SearchToken[]
}

/** Debounced token search for the picker. */
/**
 * Token search across every chain the desk trades, not just the ticket's.
 *
 * A symbol or an address is a poor place to make someone guess which network
 * they are on — a Robinhood Chain address pasted while the ticket sits on Base
 * used to find nothing. One query per chain, run together and merged; each
 * result carries its own chainId, so the picker can show where it lives and
 * move the ticket if you take it. `first` is only an ordering preference: the
 * chain already on screen leads.
 */
export function useTokenSearch(first: number, query: string) {
  const rpc = useRpc()
  const connected = useConnected()
  const [debounced, setDebounced] = useState(query)
  useEffect(() => {
    const id = setTimeout(() => setDebounced(query.trim()), 220)
    return () => clearTimeout(id)
  }, [query])
  const results = useQueries({
    queries: CHAINS.map((c) => ({
      queryKey: ['trading', 'tokens', c.id, debounced.toLowerCase()],
      enabled: connected && debounced.length >= 2,
      queryFn: async () => {
        await rpc.waitForConnection()
        return rpc.call<SearchResult>('trading.tokens.search', {
          chainId: c.id,
          query: debounced,
        })
      },
      staleTime: 30_000,
      retry: false,
    })),
  })
  const isFetching = results.some((r) => r.isFetching)
  // `results` is a new array on every render, so memoising on it would never
  // hit. The stamps are what actually change when a chain answers.
  const stamps = results.map((r) => r.dataUpdatedAt).join(',')
  const rows = results.flatMap((r) => r.data?.tokens ?? [])
  const tokens = useMemo(
    // The ticket's own chain leads; within a chain the engine's own ranking
    // survives, so an exact symbol match still comes first.
    () => [...rows].sort((a, b) => Number(b.chainId === first) - Number(a.chainId === first)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [stamps, first, debounced],
  )
  return { isFetching, tokens, debounced }
}

/* ── Markets (docs/markets.md) ───────────────────────────────────────────── */

/**
 * The query key of one Markets read: every param, with the engine's defaults
 * filled in so `{}` and `{ side: 'all' }` share an entry. Deliberately outside
 * the `trading` prefix: `trading.changed` fires on every order, and a refetch
 * per order would spend GeckoTerminal's 30 requests a minute on nothing new.
 */
export function marketsKey(p: MarketsParams) {
  return [
    'trading-markets',
    p.chainId,
    p.target.trim().toLowerCase(),
    p.side ?? 'all',
    p.minTvlUsd ?? 10_000,
    p.limit ?? 50,
    Boolean(p.lookalikes),
    Boolean(p.deep),
  ] as const
}

/**
 * Every pool a token trades in, on every DEX of the chain (`trading.markets`).
 * A minute fresh: the engine caches the source pages for two, so a refetch
 * inside that is free and outside it is one rate-limited call.
 */
export function useMarkets(params: MarketsParams, enabled = true) {
  const rpc = useRpc()
  const connected = useConnected()
  return useQuery<MarketsPayload>({
    queryKey: marketsKey(params),
    enabled: connected && enabled && params.target.trim().length > 0,
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<MarketsPayload>('trading.markets', { ...params })
    },
    staleTime: 60_000,
    refetchOnWindowFocus: false,
    retry: false,
  })
}

/* ── DCA mandates (docs/dca.md) ──────────────────────────────────────────── */

const NO_MANDATES: Mandate[] = []

/**
 * The engine's DCA mandates: live ones (awaiting approval, active, paused),
 * or every one with `all`. The key starts with `trading`, so `trading.changed`
 * — which the engine emits on every mandate change — refreshes it, and
 * `trading.dca.changed` is in TRADING_EVENTS for the same reason.
 */
export function useMandates(all = false, enabled = true) {
  const rpc = useRpc()
  const connected = useConnected()
  const query = useQuery<MandateListPayload>({
    queryKey: TRADING_KEYS.dca(all),
    enabled: connected && enabled,
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<MandateListPayload>('trading.dca.list', all ? { all: true } : {})
    },
    // A countdown lives on the row; the list itself only moves when the
    // engine says so (events), with a slow backstop for a missed one.
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
    placeholderData: (prev) => prev,
  })
  const mandates = useMemo(
    () => (Array.isArray(query.data?.mandates) ? query.data.mandates : NO_MANDATES),
    [query.data],
  )
  return { ...query, mandates, totals: query.data?.totals ?? null }
}

export type MandateAction = 'approve' | 'reject' | 'pause' | 'resume' | 'stop' | 'run'

/** Operator writes on one mandate. Every answer is the full, refreshed card payload. */
export interface MandateActions {
  /** Resolves null when the engine refused (already toasted). */
  create: (params: Record<string, unknown>) => Promise<MandatePayload | null>
  update: (mandate: Mandate, patch: Record<string, unknown>) => Promise<MandatePayload | null>
  /** Asks for Touch ID first when Settings › Security says so; null when declined. */
  approve: (mandate: Mandate) => Promise<MandatePayload | null>
  reject: (mandate: Mandate, reason?: string) => Promise<MandatePayload | null>
  pause: (mandate: Mandate) => Promise<MandatePayload | null>
  resume: (mandate: Mandate) => Promise<MandatePayload | null>
  stop: (mandate: Mandate, reason?: string) => Promise<MandatePayload | null>
  /** "Buy now": one run at once; the payload carries `run`. */
  run: (mandate: Mandate) => Promise<MandatePayload | null>
  /** The mandate with a write in flight, or null. */
  pending: string | null
  busy: boolean
}

const DONE_KEYS: Record<MandateAction, MessageKey> = {
  approve: 'trading.dca.toast.approved',
  reject: 'trading.dca.toast.rejected',
  pause: 'trading.dca.toast.paused',
  resume: 'trading.dca.toast.resumed',
  stop: 'trading.dca.toast.stopped',
  run: 'trading.dca.toast.ran',
}

/**
 * The "Buy now" answer, said as what happened to the buy: bought, placed,
 * waiting for approval, skipped (and why), failed (and why). A skip is not a
 * success — it used to toast the same green check as a buy, and read as
 * nothing having happened at all. An answer without `run` falls back to the
 * newest manual run in the mandate's history, and says so when there is none.
 */
export function runToast(res: MandatePayload | null | undefined, mandate: Mandate): void {
  const name = res?.mandate?.name || mandate.name
  const id = `dca-${mandate.id}`
  const newest = res?.mandate?.history?.[0]
  const run = res?.run ?? (newest?.manual ? newest : undefined)
  const why = (key: MessageKey) => `${t(key)}${run?.reason ? `: ${run.reason}` : ''} · ${name}`
  switch (run?.status) {
    case 'filled':
      toast.success(`${t('trading.dca.toast.filled')} · ${name}`, { id })
      return
    case 'pending':
      toast.success(`${t('trading.dca.toast.ran')} · ${name}`, { id })
      return
    case 'parked':
      toast.info(`${t('trading.dca.toast.parked')} · ${name}`, { id })
      return
    case 'skipped':
      toast.warning(why('trading.dca.toast.runSkipped'), { id })
      return
    case 'failed':
      toast.error(why('trading.dca.toast.runFailed'), { id })
      return
    case 'expired':
    case 'rejected':
      toast.warning(why('trading.dca.toast.runVoid'), { id })
      return
    default:
      toast.info(`${t('trading.dca.toast.noRun')} · ${name}`, { id })
  }
}

/**
 * The mandate controls the desk offers (the Missions rows, the approval
 * card, the DCA contract). Each write toasts its own outcome, keyed by the
 * mandate so a second click replaces the first toast instead of stacking.
 */
export function useMandateActions(): MandateActions {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const mutation = useMutation({
    mutationFn: ({
      method,
      params,
      ctx,
    }: {
      method: string
      params: Record<string, unknown>
      id: string | null
      ctx?: WriteContext
    }) => gatedCall<MandatePayload>((m, p) => rpc.call(m, p), method, params, ctx),
    onSettled: () => invalidateTrading(queryClient),
  })
  const { mutateAsync } = mutation
  const write = useCallback(
    async (
      method: string,
      params: Record<string, unknown>,
      id: string | null,
      done: (res: MandatePayload) => void,
      ctx?: WriteContext,
    ): Promise<MandatePayload | null> => {
      try {
        const res = await mutateAsync({ method, params, id, ctx })
        done(res)
        return res ?? null
      } catch (err) {
        // Touch ID said no: the gate has toasted why, and nothing was sent.
        if (isTouchIdDeclined(err)) return null
        toast.error(`${t('trading.dca.toast.failed')}: ${errorText(err)}`, {
          id: `dca-${id ?? 'new'}`,
        })
        return null
      }
    },
    [mutateAsync],
  )
  const act = useCallback(
    (action: MandateAction, mandate: Mandate, extra: Record<string, unknown> = {}) =>
      write(
        `trading.dca.${action}`,
        { mandateId: mandate.id, ...extra },
        mandate.id,
        (res) => {
          const name = res?.mandate?.name || mandate.name
          if (action === 'run') {
            runToast(res, mandate)
            return
          }
          toast.success(`${t(DONE_KEYS[action])} · ${name}`, { id: `dca-${mandate.id}` })
        },
        { mandate },
      ),
    [write],
  )
  const pending = mutation.isPending ? (mutation.variables?.id ?? null) : null
  return {
    create: (params) =>
      write('trading.dca.create', params, null, (res) =>
        toast.success(
          `${
            res?.mandate?.status === 'awaiting_approval'
              ? t('trading.dca.toast.proposed')
              : t('trading.dca.toast.created')
          } · ${res?.mandate?.name ?? String(params.name ?? '')}`,
          { id: 'dca-new' },
        ),
      ),
    update: (mandate, patch) =>
      write(
        'trading.dca.update',
        { mandateId: mandate.id, ...patch },
        mandate.id,
        (res) =>
          toast.success(
            `${t('trading.dca.toast.updated')} · ${res?.mandate?.name ?? mandate.name}`,
            { id: `dca-${mandate.id}` },
          ),
        { mandate },
      ),
    // A mandate is a standing permission to spend, and Buy now spends at
    // once: every write goes through `gatedCall` (TRADING_WRITE_GATES), so
    // with Touch ID on the fingerprint comes first and a declined prompt
    // (already toasted) sends nothing and resolves null.
    approve: (mandate) => act('approve', mandate),
    reject: (mandate, reason) => act('reject', mandate, reason ? { reason } : {}),
    pause: (mandate) => act('pause', mandate),
    resume: (mandate) => act('resume', mandate),
    stop: (mandate, reason) => act('stop', mandate, reason ? { reason } : {}),
    run: (mandate) => act('run', mandate),
    pending,
    busy: mutation.isPending,
  }
}

/* ── Price triggers (docs/triggers.md) ───────────────────────────────────── */

const NO_TRIGGERS: Trigger[] = []

/**
 * The engine's price triggers: live ones (awaiting approval, armed,
 * triggered, paused), or every one with `all`. `trading.changed` (emitted on
 * every trigger change) and `trading.trigger.changed` / `.fired` (in
 * TRADING_EVENTS) refresh it; the price and distance move with the engine's
 * own checks, so a slow poll backs up the events.
 */
export function useTriggers(all = false, enabled = true) {
  const rpc = useRpc()
  const connected = useConnected()
  const query = useQuery<TriggerListPayload>({
    queryKey: TRADING_KEYS.trigger(all),
    enabled: connected && enabled,
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<TriggerListPayload>('trading.trigger.list', all ? { all: true } : {})
    },
    // The engine re-reads the price every 30 s without an event per check.
    refetchInterval: 30_000,
    refetchOnWindowFocus: true,
    placeholderData: (prev) => prev,
  })
  const triggers = useMemo(
    () => (Array.isArray(query.data?.triggers) ? query.data.triggers : NO_TRIGGERS),
    [query.data],
  )
  return { ...query, triggers, totals: query.data?.totals ?? null }
}

export type TriggerAction = 'approve' | 'reject' | 'pause' | 'resume' | 'stop' | 'fire'

/** Operator writes on one trigger. Every answer is the full, refreshed card payload. */
export interface TriggerActions {
  /** Asks for Touch ID first when Settings › Security says so; null when declined. */
  approve: (trigger: Trigger) => Promise<TriggerPayload | null>
  reject: (trigger: Trigger, reason?: string) => Promise<TriggerPayload | null>
  pause: (trigger: Trigger) => Promise<TriggerPayload | null>
  resume: (trigger: Trigger) => Promise<TriggerPayload | null>
  stop: (trigger: Trigger, reason?: string) => Promise<TriggerPayload | null>
  /** "Fire now": one fire at once, whatever the condition; the payload carries `fire`. */
  fire: (trigger: Trigger) => Promise<TriggerPayload | null>
  /** The trigger with a write in flight, or null. */
  pending: string | null
  busy: boolean
}

const TRIGGER_DONE_KEYS: Record<Exclude<TriggerAction, 'fire'>, MessageKey> = {
  approve: 'trading.trigger.toast.approved',
  reject: 'trading.trigger.toast.rejected',
  pause: 'trading.trigger.toast.paused',
  resume: 'trading.trigger.toast.resumed',
  stop: 'trading.trigger.toast.stopped',
}

/**
 * The "Fire now" answer, said as what the fire did: alerted, placed, filled,
 * waiting for approval, skipped (and why), failed (and why). A skip is not a
 * success and never toasts a green check.
 */
export function fireToast(res: TriggerPayload | null | undefined, trigger: Trigger): void {
  const name = res?.trigger?.name || trigger.name
  const id = `trigger-${trigger.id}`
  const fire = answeredFire(res?.fire, res?.trigger)
  const why = (key: MessageKey) => `${t(key)}${fire?.reason ? `: ${fire.reason}` : ''} · ${name}`
  switch (fire?.status) {
    case 'alerted':
      toast.success(`${t('trading.trigger.toast.alerted')} · ${name}`, { id })
      return
    case 'filled':
      toast.success(`${t('trading.trigger.toast.filled')} · ${name}`, { id })
      return
    case 'pending':
      toast.success(`${t('trading.trigger.toast.placed')} · ${name}`, { id })
      return
    case 'parked':
      toast.info(`${t('trading.trigger.toast.parked')} · ${name}`, { id })
      return
    case 'skipped':
      toast.warning(why('trading.trigger.toast.skipped'), { id })
      return
    case 'failed':
      toast.error(why('trading.trigger.toast.fireFailed'), { id })
      return
    case 'expired':
    case 'rejected':
      toast.warning(why('trading.trigger.toast.void'), { id })
      return
    default:
      toast.info(`${t('trading.trigger.toast.noFire')} · ${name}`, { id })
  }
}

/**
 * The trigger controls the desk offers (the Missions rows, the approval
 * card). Each write toasts its own outcome, keyed by the trigger so a second
 * click replaces the first toast instead of stacking.
 */
export function useTriggerActions(): TriggerActions {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const mutation = useMutation({
    mutationFn: ({
      method,
      params,
      ctx,
    }: {
      method: string
      params: Record<string, unknown>
      id: string
      ctx?: WriteContext
    }) => gatedCall<TriggerPayload>((m, p) => rpc.call(m, p), method, params, ctx),
    // The answer is the refreshed trigger: the Missions row shows it at once,
    // then the sweep re-reads everything it moved.
    onSuccess: (res) =>
      primeTradingList(queryClient, TRADING_KEYS.trigger, 'triggers', res?.trigger),
    onSettled: () => invalidateTrading(queryClient),
  })
  const { mutateAsync } = mutation
  const act = useCallback(
    async (
      action: TriggerAction,
      trigger: Trigger,
      extra: Record<string, unknown> = {},
    ): Promise<TriggerPayload | null> => {
      try {
        const res = await mutateAsync({
          method: `trading.trigger.${action}`,
          params: { triggerId: trigger.id, ...extra },
          id: trigger.id,
          ctx: { trigger },
        })
        if (action === 'fire') fireToast(res, trigger)
        else
          toast.success(`${t(TRIGGER_DONE_KEYS[action])} · ${res?.trigger?.name || trigger.name}`, {
            id: `trigger-${trigger.id}`,
          })
        return res ?? null
      } catch (err) {
        if (isTouchIdDeclined(err)) return null
        toast.error(`${t('trading.trigger.toast.failed')}: ${errorText(err)}`, {
          id: `trigger-${trigger.id}`,
        })
        return null
      }
    },
    [mutateAsync],
  )
  const pending = mutation.isPending ? (mutation.variables?.id ?? null) : null
  return {
    // A trigger is a standing permission to trade when the market says so,
    // and Fire now trades at once: every write goes through `gatedCall`, so
    // with Touch ID on the fingerprint comes first and a declined prompt
    // (already toasted) sends nothing and resolves null.
    approve: (trigger) => act('approve', trigger),
    reject: (trigger, reason) => act('reject', trigger, reason ? { reason } : {}),
    pause: (trigger) => act('pause', trigger),
    resume: (trigger) => act('resume', trigger),
    stop: (trigger, reason) => act('stop', trigger, reason ? { reason } : {}),
    fire: (trigger) => act('fire', trigger),
    pending,
    busy: mutation.isPending,
  }
}

/* ── Brackets: take-profit + stop-loss as one object (docs/brackets.md) ──── */

const NO_BRACKETS: Bracket[] = []

/**
 * The engine's brackets: live ones, or every one with `all`. A bracket's legs
 * never appear in `trading.trigger.list`; this list is where they are seen.
 * `trading.changed` and `trading.bracket.changed` (in TRADING_EVENTS) refresh
 * it; the distances move with the engine's own checks, so a slow poll backs
 * up the events as for triggers.
 */
export function useBrackets(all = false, enabled = true) {
  const rpc = useRpc()
  const connected = useConnected()
  const query = useQuery<BracketListPayload>({
    queryKey: TRADING_KEYS.bracket(all),
    enabled: connected && enabled,
    queryFn: async () => {
      await rpc.waitForConnection()
      return rpc.call<BracketListPayload>('trading.bracket.list', all ? { all: true } : {})
    },
    refetchInterval: 30_000,
    refetchOnWindowFocus: true,
    placeholderData: (prev) => prev,
  })
  const brackets = useMemo(
    () => (Array.isArray(query.data?.brackets) ? query.data.brackets : NO_BRACKETS),
    [query.data],
  )
  return { ...query, brackets, totals: query.data?.totals ?? null }
}

export type BracketAction = 'approve' | 'reject' | 'pause' | 'resume' | 'stop' | 'fire'

/** Operator writes on one bracket: both legs at once. Every answer is the refreshed payload. */
export interface BracketActions {
  /** Asks for Touch ID first when Settings › Security says so; null when declined. */
  approve: (bracket: Bracket) => Promise<BracketPayload | null>
  reject: (bracket: Bracket, reason?: string) => Promise<BracketPayload | null>
  pause: (bracket: Bracket) => Promise<BracketPayload | null>
  resume: (bracket: Bracket) => Promise<BracketPayload | null>
  stop: (bracket: Bracket, reason?: string) => Promise<BracketPayload | null>
  /**
   * "Sell now" / "Notify now": one leg fired at once, the given one or else
   * the nearest (the engine picks); the sibling goes on hold. Asks for Touch
   * ID like an approval: it trades at once.
   */
  fire: (bracket: Bracket, leg?: BracketLeg) => Promise<BracketPayload | null>
  /** The bracket with a write in flight, or null. */
  pending: string | null
  busy: boolean
}

const BRACKET_DONE_KEYS: Record<Exclude<BracketAction, 'fire'>, MessageKey> = {
  approve: 'trading.bracket.toast.approved',
  reject: 'trading.bracket.toast.rejected',
  pause: 'trading.bracket.toast.paused',
  resume: 'trading.bracket.toast.resumed',
  stop: 'trading.bracket.toast.stopped',
}

/** The Sell now answer, said as what the fire did; a skip never toasts a green check. */
export function bracketFireToast(res: BracketPayload | null | undefined, bracket: Bracket): void {
  const name = res?.bracket?.name || bracket.name
  const id = `bracket-${bracket.id}`
  const fire = answeredBracketFire(res?.fire, res?.bracket)
  const why = (key: MessageKey) => `${t(key)}${fire?.reason ? `: ${fire.reason}` : ''} · ${name}`
  switch (fire?.status) {
    case 'alerted':
      toast.success(`${t('trading.bracket.toast.alerted')} · ${name}`, { id })
      return
    case 'filled':
      toast.success(`${t('trading.bracket.toast.filled')} · ${name}`, { id })
      return
    case 'pending':
      toast.success(`${t('trading.bracket.toast.placed')} · ${name}`, { id })
      return
    case 'parked':
      toast.info(`${t('trading.bracket.toast.parked')} · ${name}`, { id })
      return
    case 'skipped':
      toast.warning(why('trading.bracket.toast.skipped'), { id })
      return
    case 'failed':
      toast.error(why('trading.bracket.toast.fireFailed'), { id })
      return
    case 'expired':
    case 'rejected':
      toast.warning(why('trading.bracket.toast.void'), { id })
      return
    default:
      toast.info(`${t('trading.bracket.toast.noFire')} · ${name}`, { id })
  }
}

/**
 * The bracket controls the desk offers (the Missions rows, the approval
 * card). One write acts on both legs; each toasts its own outcome, keyed by
 * the bracket so a second click replaces the first toast.
 */
export function useBracketActions(): BracketActions {
  const rpc = useRpc()
  const queryClient = useQueryClient()
  const mutation = useMutation({
    mutationFn: ({
      method,
      params,
      ctx,
    }: {
      method: string
      params: Record<string, unknown>
      id: string
      ctx?: WriteContext
    }) => gatedCall<BracketPayload>((m, p) => rpc.call(m, p), method, params, ctx),
    // The answer is the refreshed bracket: the Missions row shows it at once
    // (onSuccess is awaited before onSettled, so the sweep's refetch is not
    // the one cancelled). Then both lists move — the bracket, and its legs
    // behind every trigger read — and both keys sit under the `trading`
    // prefix this sweeps.
    onSuccess: (res) =>
      primeTradingList(queryClient, TRADING_KEYS.bracket, 'brackets', res?.bracket),
    onSettled: () => invalidateTrading(queryClient),
  })
  const { mutateAsync } = mutation
  const act = useCallback(
    async (
      action: BracketAction,
      bracket: Bracket,
      extra: Record<string, unknown> = {},
    ): Promise<BracketPayload | null> => {
      try {
        const res = await mutateAsync({
          method: `trading.bracket.${action}`,
          params: { bracketId: bracket.id, ...extra },
          id: bracket.id,
          ctx: { bracket },
        })
        if (action === 'fire') bracketFireToast(res, bracket)
        else
          toast.success(`${t(BRACKET_DONE_KEYS[action])} · ${res?.bracket?.name || bracket.name}`, {
            id: `bracket-${bracket.id}`,
          })
        return res ?? null
      } catch (err) {
        if (isTouchIdDeclined(err)) return null
        toast.error(`${t('trading.bracket.toast.failed')}: ${errorText(err)}`, {
          id: `bracket-${bracket.id}`,
        })
        return null
      }
    },
    [mutateAsync],
  )
  const pending = mutation.isPending ? (mutation.variables?.id ?? null) : null
  return {
    // One decision arms both legs, and Sell now trades at once: every write
    // goes through `gatedCall`, so with Touch ID on the fingerprint comes
    // first and a declined prompt (already toasted) sends nothing.
    approve: (bracket) => act('approve', bracket),
    reject: (bracket, reason) => act('reject', bracket, reason ? { reason } : {}),
    pause: (bracket) => act('pause', bracket),
    resume: (bracket) => act('resume', bracket),
    stop: (bracket, reason) => act('stop', bracket, reason ? { reason } : {}),
    fire: (bracket, leg) => act('fire', bracket, leg ? { leg } : {}),
    pending,
    busy: mutation.isPending,
  }
}
