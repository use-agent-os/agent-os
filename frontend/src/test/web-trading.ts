// Fake trading engine answers for the web console's Trading page tests: a
// FakeRpc that answers every read of the page's allow-list with small,
// realistic figures, each overridable per test.
import type {
  ChainRead,
  Entry,
  Holding,
  LpBook,
  LpPosition,
  Order,
  Portfolio,
  Token,
  Totals,
  TradingStatus,
  Wallet,
  WalletStatus,
} from '@/views/trading/types'
import { FakeRpc } from './fake-rpc'

export const WALLET_A = `0x${'a1'.repeat(20)}`
export const WALLET_B = `0x${'b2'.repeat(20)}`
export const TX_HASH = `0x${'c3'.repeat(32)}`

export function token(symbol: string, over: Partial<Token> = {}): Token {
  return {
    chainId: 8453,
    address: `0x${symbol
      .toLowerCase()
      .padEnd(40, '0')
      .slice(0, 40)
      .replace(/[^0-9a-f]/g, 'e')}`,
    symbol,
    name: symbol,
    decimals: 18,
    logoUrl: null,
    native: false,
    verified: true,
    ...over,
  }
}

export const ETH = token('ETH', { name: 'Ether', native: true, address: `0x${'0'.repeat(40)}` })
export const USDC = token('USDC', { name: 'USD Coin', decimals: 6 })

const NOW = 1_763_900_000

export function totals(over: Partial<Totals> = {}): Totals {
  return {
    valueUsd: 12_480.52,
    costUsd: 11_200,
    unrealizedUsd: 1280.52,
    realizedUsd: -84.1,
    gasUsd: 3.42,
    change24hUsd: 210.4,
    change24hPct: 1.71,
    ...over,
  }
}

export function holding(t: Token, over: Partial<Holding> = {}): Holding {
  return {
    chainId: t.chainId,
    wallet: WALLET_A,
    token: t,
    amount: '1.5',
    raw: '1500000000000000000',
    priceUsd: 3200,
    valueUsd: 4800,
    costUsd: 4500,
    avgCostUsd: 3000,
    unrealizedUsd: 300,
    unrealizedPct: 6.67,
    realizedUsd: 0,
    change24hPct: 2.1,
    allocationPct: 38.5,
    ...over,
  }
}

export function wallet(address: string, label: string, primary = false): Wallet {
  return { address, label, primary, createdAt: NOW - 86_400 * 30, chains: [8453] }
}

export function portfolio(over: Partial<Portfolio> = {}): Portfolio {
  return {
    totals: totals(),
    holdings: [
      holding(ETH),
      holding(USDC, {
        amount: '7680.52',
        raw: '7680520000',
        priceUsd: 1,
        valueUsd: 7680.52,
        costUsd: 7680.52,
        unrealizedUsd: 0,
        unrealizedPct: 0,
        change24hPct: 0,
        allocationPct: 61.5,
      }),
    ],
    wallets: [{ wallet: wallet(WALLET_A, 'Main', true), totals: totals() }],
    updatedAt: NOW,
    syncing: false,
    ...over,
  }
}

export function order(over: Partial<Order> = {}): Order {
  return {
    orderId: 'ord_1',
    // Milliseconds, as the engine sends order and ledger times.
    createdAt: (NOW - 600) * 1000,
    updatedAt: (NOW - 590) * 1000,
    chainId: 8453,
    wallet: WALLET_A,
    kind: 'swap',
    tokenIn: USDC,
    tokenOut: ETH,
    amountIn: '250',
    amountInRaw: '250000000',
    expectedOut: '0.078',
    minOut: '0.077',
    receivedOut: '0.0781',
    valueUsd: 250,
    priceImpactPct: 0.04,
    gasUsd: 0.02,
    status: 'confirmed',
    reason: null,
    initiator: 'manual',
    sessionKey: null,
    note: null,
    txHash: TX_HASH,
    explorerUrl: `https://basescan.org/tx/${TX_HASH}`,
    expiresAt: null,
    ...over,
  }
}

export function entry(over: Partial<Entry> = {}): Entry {
  return {
    id: 'e1',
    ts: (NOW - 600) * 1000,
    chainId: 8453,
    wallet: WALLET_A,
    kind: 'swap',
    txHash: TX_HASH,
    explorerUrl: null,
    tokenIn: USDC,
    amountIn: '250',
    tokenOut: ETH,
    amountOut: '0.0781',
    valueUsd: 250,
    gasUsd: 0.02,
    initiator: 'manual',
    orderId: 'ord_1',
    note: null,
    ...over,
  }
}

export function lpPosition(over: Partial<LpPosition> = {}): LpPosition {
  const lp = (t: Token, priceUsd: number | null) => ({
    address: t.address,
    symbol: t.symbol,
    decimals: t.decimals,
    priceUsd,
  })
  return {
    id: 'base:uniswap_v3:48213',
    chain: { id: 8453, key: 'base', name: 'Base', explorer: 'https://basescan.org' },
    tokenId: '48213',
    owner: { address: WALLET_A, label: 'Main', inApp: true },
    token: lp(ETH, 3200),
    quote: lp(USDC, 1),
    pool: {
      poolId: `0x${'d4'.repeat(20)}`,
      feePct: '0.05%',
      tickSpacing: 10,
      tvlUsd: 4.2e7,
      priceUsd: 3200,
    },
    range: { priceLower: 2800, priceUpper: 3600 },
    status: 'in-range',
    principal: {
      base: { raw: '400000000000000000', human: '0.4', usd: 1280 },
      quote: { raw: '1210000000', human: '1210', usd: 1210 },
      usd: 2490,
    },
    fees: {
      base: { raw: '1000000000000000', human: '0.001', usd: 3.2 },
      quote: { raw: '2900000', human: '2.9', usd: 2.9 },
      usd: 6.1,
    },
    valueUsd: 2490,
    distancePct: null,
    protocol: { key: 'uniswap_v3', name: 'Uniswap v3', kind: 'concentrated' },
    staked: false,
    gauge: null,
    rewards: null,
    share: null,
    ...over,
  }
}

export function lpBook(
  positions: LpPosition[] = [lpPosition()],
  over: Partial<LpBook> = {},
): LpBook {
  const value = positions.reduce((sum, p) => sum + (p.valueUsd ?? 0), 0)
  return {
    version: 1,
    kind: 'book',
    ready: true,
    refreshing: false,
    readAt: NOW,
    partialScan: false,
    warnings: [],
    wallets: [{ address: WALLET_A, label: 'Main' }],
    chains: [],
    protocols: [],
    positions,
    totals: {
      valueUsd: value,
      feesUsd: positions.reduce((sum, p) => sum + (p.fees?.usd ?? 0), 0),
      rewardsUsd: null,
      count: positions.length,
      outOfRange: positions.filter((p) => p.status === 'above-range' || p.status === 'below-range')
        .length,
      unpriced: 0,
    },
    ...over,
  } as LpBook
}

/** One chain read as `wallet.balances` reports it; `ok` unless overridden. */
export function chainRead(over: Partial<ChainRead> = {}): ChainRead {
  return { chainId: 8453, wallet: WALLET_A, status: 'ok', reason: null, readAt: NOW, ...over }
}

export function tradingStatus(over: Partial<TradingStatus> = {}): TradingStatus {
  return {
    enabled: true,
    chains: [
      {
        chainId: 8453,
        key: 'base',
        name: 'Base',
        native: 'ETH',
        explorer: 'https://basescan.org',
        rpcUrl: '',
        healthy: true,
      },
    ],
    limits: { approvalThresholdUsd: 500, dailyCapUsd: 2000, approvalTtlSeconds: 600 },
    unlockMode: 'session',
    unlocked: true,
    syncing: false,
    lastSyncAt: NOW,
    ...over,
  } as TradingStatus
}

export function vaultStatus(over: Partial<WalletStatus> = {}): WalletStatus {
  return {
    initialized: true,
    unlocked: true,
    unlockMode: 'session',
    walletCount: 1,
    primary: WALLET_A,
    vaultPath: '',
    ...over,
  } as WalletStatus
}

const ENVELOPE = { version: 1, fetchedAt: '2026-11-23T10:00:00Z', warnings: [] }

/** A FakeRpc answering every read of the Trading page with a healthy engine. */
export function tradingRpc(): FakeRpc {
  return new FakeRpc()
    .handle('trading.status', tradingStatus())
    .handle('wallet.status', vaultStatus())
    .handle('wallet.list', { wallets: [wallet(WALLET_A, 'Main', true)], primary: WALLET_A })
    .handle('trading.portfolio', portfolio())
    .handle('wallet.balances', { balances: [], chains: [chainRead()], updatedAt: NOW })
    .handle('trading.orders.list', { orders: [order()] })
    .handle('trading.history', { entries: [entry()] })
    .handle('trading.chart', {
      source: 'snapshots',
      range: '1d',
      points: [],
      stats: {
        priceUsd: 3200,
        priceNative: null,
        quoteSymbol: 'USDC',
        marketCapUsd: null,
        market: null,
        changePct: null,
      },
    })
    .handle('trading.lp.book', lpBook())
    .handle('trading.dca.list', {
      ...ENVELOPE,
      kind: 'mandates',
      mandates: [],
      totals: { count: 0, active: 0, spentUsd: 0, capUsd: 0, acquiredUsd: null },
    })
    .handle('trading.trigger.list', {
      ...ENVELOPE,
      kind: 'triggers',
      triggers: [],
      totals: { count: 0, armed: 0, awaiting: 0, triggered: 0 },
    })
    .handle('trading.bracket.list', {
      ...ENVELOPE,
      kind: 'brackets',
      brackets: [],
      totals: { count: 0, armed: 0, awaiting: 0, triggered: 0 },
    })
}
