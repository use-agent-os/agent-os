import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router'
import { useConnection } from '@/stores/connection'
import type { Holding, LpPosition, Order, Quote, Token, Wallet } from './types'

/** Test doubles for the desk: a connected client and the shapes the RPC returns. */

export function renderDesk(ui: ReactNode) {
  useConnection.getState().setState('connected')
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  })
  const wrap = (node: ReactNode) => (
    <MemoryRouter>
      <QueryClientProvider client={client}>{node}</QueryClientProvider>
    </MemoryRouter>
  )
  const result = render(wrap(ui))
  // `rerender` keeps the same providers, so component state survives a prop change.
  return { ...result, rerender: (next: ReactNode) => result.rerender(wrap(next)) }
}

export const ETH: Token = {
  chainId: 8453,
  address: '0x0000000000000000000000000000000000000000',
  symbol: 'ETH',
  name: 'Base',
  decimals: 18,
  logoUrl: null,
  native: true,
  verified: true,
}

export const USDC: Token = {
  chainId: 8453,
  address: '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913',
  symbol: 'USDC',
  name: 'USD Coin',
  decimals: 6,
  logoUrl: null,
  native: false,
  verified: true,
}

export const WALLET: Wallet = {
  address: '0x1111111111111111111111111111111111111111',
  label: 'Main',
  primary: true,
  createdAt: 1_700_000_000_000,
  chains: [8453, 4663],
}

export function holding(extra: Partial<Holding> = {}): Holding {
  return {
    chainId: 8453,
    wallet: WALLET.address,
    token: USDC,
    amount: '900',
    raw: '900000000',
    priceUsd: 1,
    valueUsd: 900,
    costUsd: 880,
    avgCostUsd: 0.98,
    unrealizedUsd: 20,
    unrealizedPct: 2.27,
    realizedUsd: 0,
    change24hPct: 0.4,
    allocationPct: 72.5,
    ...extra,
  }
}

export function quote(extra: Partial<Quote> = {}): Quote {
  return {
    quoteId: 'q1',
    routing: 'CLASSIC',
    chainId: 8453,
    wallet: WALLET.address,
    tokenIn: ETH,
    tokenOut: USDC,
    amountIn: '0.1',
    amountOut: '250.12',
    minOut: '248.87',
    priceImpactPct: 0.12,
    gasUsd: 0.03,
    valueUsd: 250.5,
    rate: '2501.2',
    slippagePct: 0.5,
    expiresAt: Date.now() + 30_000,
    guard: { decision: 'allow', spentTodayUsd: 0, dailyCapUsd: 1000, thresholdUsd: 100 },
    ...extra,
  }
}

/**
 * A bridge order as the engine files it: 0.01 ETH leaving Base for
 * Robinhood Chain through Relay, parked for the user. `chainId`/`tokenIn`
 * are the origin, `tokenOut` the destination token.
 */
export function bridgeOrder(extra: Partial<Order> = {}): Order {
  return order({
    kind: 'bridge',
    chainId: 8453,
    tokenIn: ETH,
    tokenOut: { ...ETH, chainId: 4663 },
    amountIn: '0.01',
    amountInRaw: '10000000000000000',
    expectedOut: '0.0099',
    minOut: '0.0098',
    receivedOut: null,
    valueUsd: 25,
    priceImpactPct: null,
    gasUsd: 0.02,
    recipient: '0xa5f565650890fba1824ee0f21ebbbf660a179934',
    recipientLabel: 'Relay solver',
    provider: 'relay',
    note: null,
    bridge: {
      originChainId: 8453,
      destinationChainId: 4663,
      destinationChain: {
        chainId: 4663,
        key: 'robinhood',
        name: 'Robinhood Chain',
        nativeSymbol: 'ETH',
        explorerUrl: 'https://explorer.robinhood.com',
      },
      asset: 'ETH',
      requestId: null,
      fillStatus: null,
      feesUsd: 0.12,
      fees: { gasUsd: 0.02, relayerUsd: 0.1, appUsd: 0 },
      rate: '0.99',
      timeEstimateSeconds: 30,
      approvalTxHash: null,
      depositTxHash: null,
      depositExplorerUrl: null,
      destinationTxHash: null,
      destinationExplorerUrl: null,
      checkedAt: null,
    },
    ...extra,
  })
}

export function order(extra: Partial<Order> = {}): Order {
  const now = Date.now()
  return {
    orderId: 'o1',
    createdAt: now - 5_000,
    updatedAt: now - 5_000,
    chainId: 8453,
    wallet: WALLET.address,
    tokenIn: ETH,
    tokenOut: USDC,
    amountIn: '0.2',
    amountInRaw: '200000000000000000',
    expectedOut: '500',
    minOut: '497',
    valueUsd: 500,
    priceImpactPct: 0.2,
    gasUsd: 0.04,
    status: 'awaiting_approval',
    reason: null,
    initiator: 'agent',
    sessionKey: 'agent:main:cron',
    note: 'DCA',
    txHash: null,
    explorerUrl: null,
    expiresAt: now + 600_000,
    ...extra,
  }
}

const LP_AMOUNT = { raw: '1', human: '1', usd: 1 }
/** One concentrated LP position on Base, out of range above. */
export const LP_ROW: LpPosition = {
  id: '8453:uniswap_v3:77:0xabc',
  chain: { id: 8453, key: 'base', name: 'Base', explorer: 'https://basescan.org' },
  tokenId: '77',
  owner: { address: WALLET.address, label: 'Main', inApp: true },
  token: {
    address: '0x4200000000000000000000000000000000000006',
    symbol: 'WETH',
    decimals: 18,
    priceUsd: 2500,
  },
  quote: { address: USDC.address, symbol: 'USDC', decimals: 6, priceUsd: 1 },
  pool: { poolId: '0xpool', feePct: '0.05%', tickSpacing: 10, tvlUsd: null, priceUsd: 2500 },
  range: { priceLower: 2000, priceUpper: 2400, mcapLower: 2_100_000, mcapUpper: 9_800_000 },
  status: 'above-range',
  principal: { base: LP_AMOUNT, quote: LP_AMOUNT, usd: 1300 },
  fees: { base: LP_AMOUNT, quote: LP_AMOUNT, usd: 4 },
  valueUsd: 1304,
  distancePct: 4.2,
  protocol: { key: 'uniswap_v3', name: 'Uniswap V3', kind: 'concentrated' },
  staked: false,
  gauge: null,
  rewards: null,
  share: null,
}
