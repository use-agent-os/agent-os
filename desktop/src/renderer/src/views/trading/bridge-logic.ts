import { nativeToken, POPULAR_TOKENS } from './TokenPicker'
import type { ChainId, Token } from './types'

/**
 * What crosses between Base and Robinhood Chain (docs/bridge.md): the gas
 * coin, and the USD stable, which is USDC on Base and USDG on Robinhood
 * Chain. This mirrors the engine's `CHAIN_BRIDGE_TOKENS`, the one table that
 * decides a route; anything else is `trading.bridge.unsupported_route`.
 */
export type BridgeAsset = 'ETH' | 'USD'

export const BRIDGE_ASSETS: readonly BridgeAsset[] = ['ETH', 'USD']

const USDC_BASE = POPULAR_TOKENS[8453]?.find((tk) => tk.symbol === 'USDC') as Token

const USDG_ROBINHOOD: Token = {
  chainId: 4663,
  address: '0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168',
  symbol: 'USDG',
  name: 'Global Dollar',
  decimals: 6,
  logoUrl: null,
  native: false,
  verified: true,
}

const USD: Record<ChainId, Token> = { 8453: USDC_BASE, 4663: USDG_ROBINHOOD }

/** The token an asset is on a chain: ETH on both, USDC on Base, USDG on Robinhood Chain. */
export function bridgeToken(chainId: ChainId, asset: BridgeAsset): Token {
  return asset === 'ETH' ? nativeToken(chainId) : USD[chainId]
}

/** A bridge has two chains, so the destination is always the other one. */
export function otherChain(chainId: ChainId): ChainId {
  return chainId === 8453 ? 4663 : 8453
}

/** Relay's fill estimate in the fewest words: "~2s", "~3 min"; null when it gave none. */
export function formatEta(seconds: number | null | undefined): string | null {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return null
  if (seconds < 90) return `~${Math.max(1, Math.round(seconds))}s`
  return `~${Math.round(seconds / 60)} min`
}
