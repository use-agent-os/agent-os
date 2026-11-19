import { create } from 'zustand'

/**
 * Whether the wallet's figures are masked. One switch for every surface that
 * shows them — the full desk, the BOOK beside the chat, the wallet manager —
 * so hiding in one is hidden in all, and it stays hidden across launches:
 * a privacy switch that resets on restart shows the figures to whoever is
 * looking at the screen when the app comes back up.
 */

const KEY = 'agentos-desktop.trading.balancesHidden'

/** What a masked figure reads as. Fixed width, so it says nothing about size. */
export const BALANCE_MASK = '******'

function load(): boolean {
  try {
    return localStorage.getItem(KEY) === 'true'
  } catch {
    return false
  }
}

interface BalancePrivacyStore {
  hidden: boolean
  toggle(): void
  setHidden(hidden: boolean): void
}

export const useBalancePrivacy = create<BalancePrivacyStore>((set, get) => ({
  hidden: load(),
  toggle() {
    get().setHidden(!get().hidden)
  },
  setHidden(hidden) {
    try {
      localStorage.setItem(KEY, String(hidden))
    } catch {
      /* storage unavailable */
    }
    set({ hidden })
  },
}))

/**
 * The mask for a component that renders wallet figures.
 *
 * `mask(text)` is the figure or the asterisks; `title(text)` is for hover
 * titles, which must go away entirely — a masked cell whose tooltip still
 * carries the exact figure hides nothing.
 */
export function useBalanceMask(): {
  hidden: boolean
  mask: (text: string) => string
  title: (text: string | undefined) => string | undefined
} {
  const hidden = useBalancePrivacy((s) => s.hidden)
  return {
    hidden,
    mask: (text) => (hidden ? BALANCE_MASK : text),
    title: (text) => (hidden ? undefined : text),
  }
}
