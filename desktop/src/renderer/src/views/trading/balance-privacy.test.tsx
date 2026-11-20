import { fireEvent, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { BALANCE_MASK, useBalancePrivacy } from '~/stores/balance-privacy'
import { Holdings } from './Holdings'
import { Overview } from './Overview'
import { Money } from './parts'
import { holding, renderDesk, WALLET } from './test-utils'
import type { Totals, Wallet } from './types'
import { WalletHead } from './WalletHead'
import { WalletRail } from './WalletRail'

vi.mock('@/app/providers', () => ({
  useRpc: () => ({ call: vi.fn(), waitForConnection: async () => {}, on: () => () => {} }),
}))

const noop = () => {}

const TOTALS: Totals = {
  valueUsd: 1240.5,
  costUsd: 1000,
  unrealizedUsd: 240.5,
  realizedUsd: -12.25,
  gasUsd: 3.4,
  change24hUsd: 18.75,
  change24hPct: 1.53,
}

const SECOND: Wallet = { ...WALLET, address: '0x89e0fa1b2c3d4e5f60718293a4b5c6d7e8f9da97' }

const perWallet = new Map<string, Totals>([
  [WALLET.address.toLowerCase(), TOTALS],
  [SECOND.address.toLowerCase(), { ...TOTALS, valueUsd: 77.01 }],
])

function head() {
  return (
    <WalletHead
      wallets={[WALLET, SECOND]}
      selected="all"
      onSelect={noop}
      totals={perWallet}
      onAction={noop}
      onSetPrimary={noop}
      chains={[]}
    />
  )
}

/** The desk's portfolio surfaces, side by side, as the one switch sees them. */
function desk() {
  return (
    <>
      <Overview
        head={head()}
        totals={TOTALS}
        holdings={[holding()]}
        syncing={false}
        lastSyncAt={null}
        now={Date.now()}
        onSync={noop}
        loading={false}
      />
      <WalletRail
        wallets={[WALLET, SECOND]}
        totals={perWallet}
        selected="all"
        onSelect={noop}
        onAction={noop}
        onSetPrimary={noop}
        limits={undefined}
        limitsWallet={null}
        chains={[]}
        manualUnlock={false}
      />
      <Holdings
        holdings={[holding()]}
        loading={false}
        selected={null}
        onSelect={noop}
        onSwap={noop}
        showChain
      />
    </>
  )
}

afterEach(() => {
  useBalancePrivacy.getState().setHidden(false)
})

describe('hide balances', () => {
  it('shows every figure until asked, then masks them all from one button', () => {
    const { container } = renderDesk(desk())
    const text = () => container.textContent ?? ''
    expect(text()).toContain('$1,240.50')
    expect(text()).toContain('+$240.50')
    expect(text()).toContain('$77.01')
    expect(text()).not.toContain(BALANCE_MASK)

    const toggle = screen.getByTestId('balance-toggle')
    expect(toggle).toHaveAttribute('aria-pressed', 'false')
    expect(toggle).toHaveAccessibleName('Hide balances')
    fireEvent.click(toggle)

    expect(toggle).toHaveAttribute('aria-pressed', 'true')
    expect(toggle).toHaveAccessibleName('Show balances')
    // Totals, the day's move, the tiles, the rail and the row's own figures.
    for (const figure of ['1,240.50', '240.50', '12.25', '1,000.00', '3.40', '18.75', '77.01']) {
      expect(text()).not.toContain(figure)
    }
    expect(screen.getByTestId('portfolio-value')).toHaveTextContent(BALANCE_MASK)
    const row = screen.getByTestId('holding-row')
    expect(row.querySelector('.trd-col--amount')).toHaveTextContent(BALANCE_MASK)
    expect(row.querySelector('.trd-col--value')).toHaveTextContent(BALANCE_MASK)
    expect(row.querySelector('.trd-col--pnl')?.textContent).toBe(BALANCE_MASK)
    // The market's own numbers are not the wallet's: price and 24h stay.
    expect(row.querySelector('.trd-col--price')).toHaveTextContent('$1.00')
    expect(row.querySelector('.trd-col--change')).toHaveTextContent('+0.40%')

    fireEvent.click(toggle)
    expect(text()).toContain('$1,240.50')
    expect(text()).not.toContain(BALANCE_MASK)
  })

  it('leaves nothing behind in tooltips, tones or rates', () => {
    useBalancePrivacy.getState().setHidden(true)
    const { container } = renderDesk(desk())
    // Price and average cost are per token, not per wallet: their titles stay.
    const titled = container.querySelectorAll('[title]:not(.trd-col--price):not(.trd-col--cost)')
    for (const el of titled) {
      expect(el.getAttribute('title')).not.toMatch(/\$\d/)
    }
    expect(container.querySelector('.trd-hero')).toHaveAttribute('data-tone', 'flat')
    expect(screen.getByTestId('portfolio-delta')).toHaveAttribute('data-tone', 'flat')
    expect(screen.getByTestId('portfolio-delta')).not.toHaveTextContent('1.53%')
    expect(screen.getByTestId('holding-row').querySelector('.trd-col--pnl')).not.toHaveAttribute(
      'data-tone',
    )
    for (const delta of container.querySelectorAll('.trd-wallet__delta')) {
      expect(delta.textContent).toBe('')
    }
  })

  it('masks the switcher rows too', () => {
    useBalancePrivacy.getState().setHidden(true)
    renderDesk(head())
    fireEvent.click(screen.getByTestId('wallet-switcher'))
    const menu = screen.getByRole('menu')
    expect(menu).toHaveTextContent(BALANCE_MASK)
    expect(menu.textContent).not.toMatch(/\$\d/)
  })

  it('is one switch: a second surface mounted later is already hidden', () => {
    const first = renderDesk(head())
    fireEvent.click(screen.getByTestId('balance-toggle'))
    first.unmount()

    // The BOOK beside the chat wears the same head over its own figure.
    renderDesk(
      <>
        <WalletHead
          compact
          wallets={[WALLET, SECOND]}
          selected="all"
          onSelect={noop}
          totals={perWallet}
          onAction={noop}
          onSetPrimary={noop}
          chains={[]}
        />
        <b data-testid="figure">
          <Money value={1240.5} />
        </b>
      </>,
    )
    expect(screen.getByTestId('balance-toggle')).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByTestId('figure').textContent).toBe(BALANCE_MASK)
  })

  it('still states what a wallet holds where the figure is a warning', () => {
    useBalancePrivacy.getState().setHidden(true)
    renderDesk(
      <b data-testid="worth">
        <Money value={1240.5} plain />
      </b>,
    )
    expect(screen.getByTestId('worth')).toHaveTextContent('$1,240.50')
  })

  it('remembers the choice on this Mac', () => {
    const set = vi.spyOn(Storage.prototype, 'setItem')
    useBalancePrivacy.getState().toggle()
    expect(set).toHaveBeenCalledWith('agentos-desktop.trading.balancesHidden', 'true')
    useBalancePrivacy.getState().toggle()
    expect(set).toHaveBeenLastCalledWith('agentos-desktop.trading.balancesHidden', 'false')
    set.mockRestore()
  })
})
