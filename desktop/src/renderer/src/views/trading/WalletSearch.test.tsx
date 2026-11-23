import { fireEvent, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { renderDesk, WALLET } from './test-utils'
import type { Wallet } from './types'
import { WalletHead } from './WalletHead'
import { WalletRail } from './WalletRail'
import { WALLET_SEARCH_MIN } from './WalletSearch'

function wallet(n: number, label: string): Wallet {
  return {
    address: `0x${String(n).padStart(4, '0')}${'ab'.repeat(16)}${String(n).padStart(4, '0')}`,
    label,
    primary: false,
    createdAt: 1_700_000_000_000,
    chains: [8453],
  }
}

// Enough to grow a search: the user's own desk runs to dozens.
const MANY: Wallet[] = [
  WALLET,
  wallet(1, 'CAP 05'),
  wallet(2, 'MM2'),
  wallet(3, 'CAP 06'),
  wallet(4, 'OS 10'),
  wallet(5, 'OS 09'),
]

function rail(wallets: Wallet[] = MANY) {
  const onSelect = vi.fn()
  renderDesk(
    <WalletRail
      wallets={wallets}
      totals={new Map()}
      selected="all"
      onSelect={onSelect}
      onAction={vi.fn()}
      onSetPrimary={vi.fn()}
      limits={undefined}
      limitsWallet={null}
      chains={[]}
      manualUnlock={false}
    />,
  )
  return { onSelect }
}

function head(wallets: Wallet[] = MANY) {
  const onSelect = vi.fn()
  renderDesk(
    <WalletHead
      wallets={wallets}
      selected={WALLET.address}
      onSelect={onSelect}
      totals={new Map()}
      onAction={vi.fn()}
      onSetPrimary={vi.fn()}
      chains={[]}
      compact
    />,
  )
  return { onSelect }
}

const rowNames = () => screen.getAllByTestId('wallet-row').map((r) => r.textContent ?? '')

describe('WalletRail · search', () => {
  it('stays out of the way of a short list', () => {
    rail(MANY.slice(0, WALLET_SEARCH_MIN - 1))
    expect(screen.queryByTestId('wallet-search')).toBeNull()
  })

  it('narrows the rows by label, and puts the "all" row aside meanwhile', () => {
    rail()
    expect(screen.getByRole('option', { name: /All wallets/ })).toBeInTheDocument()
    fireEvent.change(screen.getByTestId('wallet-search'), { target: { value: 'cap' } })
    expect(rowNames()).toEqual([
      expect.stringContaining('CAP 05'),
      expect.stringContaining('CAP 06'),
    ])
    expect(screen.queryByRole('option', { name: /All wallets/ })).toBeNull()
    // The commands at the foot are not search results: they stay.
    expect(screen.getByRole('button', { name: /Create new/ })).toBeInTheDocument()
  })

  it('narrows by address, short form included, and says when nothing matches', () => {
    rail()
    const field = screen.getByTestId('wallet-search')
    fireEvent.change(field, { target: { value: MANY[3]!.address.toUpperCase().slice(2, 12) } })
    expect(rowNames()).toEqual([expect.stringContaining('CAP 06')])

    fireEvent.change(field, { target: { value: '0x0004…0004' } })
    expect(rowNames()).toEqual([expect.stringContaining('OS 10')])

    fireEvent.change(field, { target: { value: 'nope' } })
    expect(screen.queryAllByTestId('wallet-row')).toHaveLength(0)
    expect(screen.getByText('No wallet matches')).toBeInTheDocument()
  })

  it('takes the first match on Return and clears on Escape', () => {
    const { onSelect } = rail()
    const field = screen.getByTestId('wallet-search')
    fireEvent.change(field, { target: { value: 'os' } })
    fireEvent.keyDown(field, { key: 'Enter' })
    expect(onSelect).toHaveBeenCalledWith(MANY[4]!.address)

    fireEvent.keyDown(field, { key: 'Escape' })
    expect(field).toHaveValue('')
    expect(screen.getAllByTestId('wallet-row')).toHaveLength(MANY.length)
  })
})

describe('WalletHead · switcher search', () => {
  it('opens onto the field once the list is long, and the field narrows the rows', () => {
    head()
    fireEvent.click(screen.getByTestId('wallet-switcher'))
    const field = screen.getByTestId('wallet-search')
    expect(field).toHaveFocus()

    fireEvent.change(field, { target: { value: 'os 0' } })
    const rows = screen.getAllByRole('menuitemradio')
    expect(rows.map((r) => r.textContent)).toEqual([expect.stringContaining('OS 09')])
    // The address is shown beside a result, to confirm a search by address.
    expect(rows[0]).toHaveTextContent('0x0005…0005')
    // "All wallets" is not a match for anything typed.
    expect(screen.queryByRole('menuitemradio', { name: /All wallets/ })).toBeNull()
    // The commands stay in reach.
    expect(screen.getByRole('menuitem', { name: /Import/ })).toBeInTheDocument()
  })

  it('leaves the caret its Home and End, and hands the arrows to the rows', () => {
    head()
    fireEvent.click(screen.getByTestId('wallet-switcher'))
    const field = screen.getByTestId('wallet-search')
    fireEvent.keyDown(field, { key: 'End' })
    expect(field).toHaveFocus()
    fireEvent.keyDown(field, { key: 'ArrowDown' })
    expect(screen.getByRole('menuitemradio', { name: /All wallets/ })).toHaveFocus()
  })

  it('picks the first match on Return and closes; the next opening starts afresh', () => {
    const { onSelect } = head()
    fireEvent.click(screen.getByTestId('wallet-switcher'))
    const field = screen.getByTestId('wallet-search')
    fireEvent.change(field, { target: { value: '0x0002' } })
    fireEvent.keyDown(field, { key: 'Enter' })
    expect(onSelect).toHaveBeenCalledWith(MANY[2]!.address)
    expect(screen.queryByRole('menu')).toBeNull()

    fireEvent.click(screen.getByTestId('wallet-switcher'))
    expect(screen.getByTestId('wallet-search')).toHaveValue('')
  })

  it('keeps the plain menu, focus on the first row, for a short list', () => {
    head(MANY.slice(0, 2))
    fireEvent.click(screen.getByTestId('wallet-switcher'))
    expect(screen.queryByTestId('wallet-search')).toBeNull()
    const menu = screen.getByRole('menu')
    expect(within(menu).getAllByRole('menuitemradio')[0]).toHaveFocus()
  })
})
