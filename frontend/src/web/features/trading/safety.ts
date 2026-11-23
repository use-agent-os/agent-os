import { create } from 'zustand'

// The page's two safety helpers: hiding the figures (the desktop desk's
// balance privacy, kept in this browser), and explorer links that are built
// here from a fixed base per network, never taken from a payload.

const PRIVACY_KEY = 'agentos-web.trading.balancesHidden'

/** What a hidden figure reads as. Fixed width, so it says nothing about size. */
export const BALANCE_MASK = '******'

function loadHidden(): boolean {
  try {
    return localStorage.getItem(PRIVACY_KEY) === 'true'
  } catch {
    return false
  }
}

interface PrivacyStore {
  hidden: boolean
  toggle(): void
}

/**
 * Whether amounts are masked. It persists: a privacy switch that resets on
 * reload shows the figures to whoever is looking when the page comes back.
 */
export const useBalancePrivacy = create<PrivacyStore>((set, get) => ({
  hidden: loadHidden(),
  toggle() {
    const hidden = !get().hidden
    try {
      localStorage.setItem(PRIVACY_KEY, String(hidden))
    } catch {
      /* storage unavailable: the switch still works for this page */
    }
    set({ hidden })
  },
}))

/** Re-read the stored choice (tests, or another tab having changed it). */
export function reloadBalancePrivacy(): void {
  useBalancePrivacy.setState({ hidden: loadHidden() })
}

/** The figure, or the mask. */
export function useMask(): (text: string) => string {
  const hidden = useBalancePrivacy((s) => s.hidden)
  return (text) => (hidden ? BALANCE_MASK : text)
}

/** Block explorers by chain id (src/agentos/trading/chains.py). */
const EXPLORERS: Readonly<Record<number, string>> = {
  8453: 'https://basescan.org',
  4663: 'https://robin.etherscan.io',
}

const TX_HASH = /^0x[0-9a-fA-F]{64}$/
const ADDRESS = /^0x[0-9a-fA-F]{40}$/

/** A transaction's explorer page, or null for an unknown chain or a malformed hash. */
export function txUrl(chainId: number, hash: string | null | undefined): string | null {
  const base = EXPLORERS[chainId]
  if (!base || !hash || !TX_HASH.test(hash)) return null
  return `${base}/tx/${hash}`
}

/** An address's explorer page, or null for an unknown chain or a malformed address. */
export function addressUrl(chainId: number, address: string | null | undefined): string | null {
  const base = EXPLORERS[chainId]
  if (!base || !address || !ADDRESS.test(address)) return null
  return `${base}/address/${address}`
}

/** Untrusted names (token names, labels from chains and third parties) cut to a length a row can hold. */
export function clampText(text: string | null | undefined, max = 32): string {
  const s = (text ?? '').replace(/\s+/g, ' ').trim()
  return s.length > max ? `${s.slice(0, max - 1)}…` : s
}
