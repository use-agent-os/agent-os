import { fireEvent, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { renderDesk, WALLET } from './test-utils'
import type { Totals, Wallet } from './types'
import { WalletHead } from './WalletHead'

const SECOND: Wallet = {
  address: '0x89e0fa1b2c3d4e5f60718293a4b5c6d7e8f9da97',
  label: 'Wallet 02',
  primary: false,
  createdAt: 1_700_000_000_000,
  chains: [8453],
}

function totals(valueUsd: number): Totals {
  return {
    valueUsd,
    costUsd: valueUsd,
    unrealizedUsd: 0,
    realizedUsd: 0,
    gasUsd: 0,
    change24hUsd: 0,
    change24hPct: 0,
  }
}

function render(opts: { wallets?: Wallet[]; selected?: string; railOpen?: boolean } = {}) {
  const wallets = opts.wallets ?? [WALLET, SECOND]
  const onSelect = vi.fn()
  const onAction = vi.fn()
  const onToggleRail = vi.fn()
  renderDesk(
    <WalletHead
      wallets={wallets}
      selected={opts.selected ?? WALLET.address}
      onSelect={onSelect}
      totals={
        new Map([
          [WALLET.address.toLowerCase(), totals(1240.5)],
          [SECOND.address.toLowerCase(), totals(0.49)],
        ])
      }
      onAction={onAction}
      onSetPrimary={vi.fn()}
      chains={[]}
      railOpen={opts.railOpen ?? true}
      onToggleRail={onToggleRail}
    />,
  )
  return { onSelect, onAction, onToggleRail }
}

describe('WalletHead · whose desk this is', () => {
  it('leads with the address, the wallet name under it, and a local mark', () => {
    render({ selected: SECOND.address })
    expect(screen.getByTestId('wallet-head-address')).toHaveTextContent('0x89e0…da97')
    expect(screen.getByText('Wallet 02')).toBeInTheDocument()

    // The mark is drawn here: a remote identicon would both leak the address
    // and fail the window's CSP.
    const mark = document.querySelector('.trd-head__mark')
    expect(mark?.getAttribute('src')).toMatch(/^data:image\/svg\+xml;base64,/)
  })

  it('switches wallet from the address itself, all wallets included', () => {
    const { onSelect } = render()
    fireEvent.click(screen.getByTestId('wallet-switcher'))

    const rows = screen.getAllByRole('menuitemradio')
    expect(rows.map((r) => r.textContent)).toEqual([
      expect.stringContaining('All wallets'),
      expect.stringContaining('Main'),
      expect.stringContaining('Wallet 02'),
    ])
    // Each row carries what that wallet is worth, so the choice is informed.
    expect(rows[1]).toHaveTextContent('$1,240.50')
    expect(rows[2]).toHaveTextContent('$0.49')
    expect(rows[0]).toHaveAttribute('aria-checked', 'false')
    expect(rows[1]).toHaveAttribute('aria-checked', 'true')

    fireEvent.click(rows[2]!)
    expect(onSelect).toHaveBeenCalledWith(SECOND.address)
  })

  it('keeps the menu, without an "all" row, and drops the list toggle with one wallet', () => {
    const { onAction } = render({ wallets: [WALLET] })
    expect(screen.queryByTestId('rail-toggle')).toBeNull()
    // Create, Import and Manage live in this menu: one wallet is no reason to lose them.
    fireEvent.click(screen.getByTestId('wallet-switcher'))
    expect(screen.queryByText('All wallets')).toBeNull()
    expect(screen.getAllByRole('menuitemradio')).toHaveLength(1)
    fireEvent.click(screen.getByRole('menuitem', { name: /Import/ }))
    expect(onAction).toHaveBeenCalledWith({ kind: 'import' })
  })

  // The head sits in .trd-hero, whose overflow: hidden keeps the corner light
  // inside the card and would cut anything hanging out of it. jsdom has no
  // layout, so what is held here is that neither menu is inside the card.
  it.each([
    ['switcher', () => screen.getByTestId('wallet-switcher')],
    ['wallet actions', () => screen.getByRole('button', { name: 'Wallet actions' })],
  ])('floats the %s menu out of the card that would clip it', (_, trigger) => {
    render()
    fireEvent.click(trigger())
    const menu = screen.getByRole('menu')
    expect(menu).toHaveAttribute('data-placement', 'float')
    expect(menu.closest('.trd-head')).toBeNull()
  })

  it.each([
    ['switcher', () => screen.getByTestId('wallet-switcher')],
    ['wallet actions', () => screen.getByRole('button', { name: 'Wallet actions' })],
  ])('closes the %s menu from its own button and hands focus back', (_, trigger) => {
    render()
    fireEvent.click(trigger())
    // A press on the button is not a press outside: it must not close the
    // menu only for the click that follows to open it again.
    fireEvent.mouseDown(trigger())
    fireEvent.click(trigger())
    expect(screen.queryByRole('menu')).toBeNull()

    fireEvent.click(trigger())
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' })
    expect(screen.queryByRole('menu')).toBeNull()
    expect(trigger()).toHaveFocus()
  })

  it('scrolls only the wallets, so the commands stay in reach with any number of them', () => {
    render()
    fireEvent.click(screen.getByTestId('wallet-switcher'))
    const list = document.querySelector('.trd-head__wallets')!
    expect(list).toContainElement(screen.getByRole('menuitemradio', { name: /Wallet 02/ }))
    expect(list).not.toContainElement(screen.getByRole('menuitemradio', { name: /All wallets/ }))
    expect(list).not.toContainElement(screen.getByRole('menuitem', { name: /Import/ }))
  })

  it('opens the QR sheet for the wallet on show, not for some other one', () => {
    const { onAction } = render({ selected: SECOND.address })
    fireEvent.click(screen.getByRole('button', { name: 'Show QR' }))
    expect(onAction).toHaveBeenCalledWith({ kind: 'receive', wallet: SECOND })
  })

  it('drops the wallet-only actions when every wallet is in view', () => {
    render({ selected: 'all' })
    expect(screen.getByTestId('wallet-head-address')).toHaveTextContent('All wallets')
    expect(screen.getByText('2 wallets')).toBeInTheDocument()
    // Nothing to copy and nothing to receive into when the value is a sum.
    expect(screen.queryByRole('button', { name: 'Copy address' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Show QR' })).toBeNull()
  })

  it('says whether the list toggle will hide or bring back the rail', () => {
    const { onToggleRail } = render({ railOpen: false })
    const toggle = screen.getByRole('button', { name: 'Show wallet list' })
    fireEvent.click(toggle)
    expect(onToggleRail).toHaveBeenCalled()
  })

  it('marks the active wallet with the enable mark, not a star', () => {
    render()
    expect(screen.getByLabelText('Active')).toBeInTheDocument()
    expect(document.querySelector('.lucide-star')).toBeNull()
  })

  it('lists the active wallet first in the switcher, whatever order the vault returns', () => {
    render({ wallets: [SECOND, WALLET] })
    fireEvent.click(screen.getByTestId('wallet-switcher'))

    const rows = screen.getAllByRole('menuitemradio')
    expect(rows.map((r) => r.textContent)).toEqual([
      expect.stringContaining('All wallets'),
      expect.stringContaining('Main'),
      expect.stringContaining('Wallet 02'),
    ])
  })
})
