import { fireEvent, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { renderDesk, WALLET } from './test-utils'
import { WalletRail } from './WalletRail'

function render() {
  const onSelect = vi.fn()
  renderDesk(
    <WalletRail
      wallets={[WALLET]}
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
  const row = screen.getByTestId('wallet-row')
  return { onSelect, row, trigger: within(row).getByRole('button', { name: 'Wallet actions' }) }
}

describe("WalletRail · a row's menu", () => {
  // The list scrolls, so a menu hung from a low row is cut at the list's foot,
  // with Export and Remove under it.
  it('floats out of the list that would clip it', () => {
    const { row, trigger } = render()
    fireEvent.click(trigger)
    const menu = screen.getByRole('menu')
    expect(menu).toHaveAttribute('data-placement', 'float')
    expect(row).not.toContainElement(menu)
    expect(menu.closest('.trd-rail__list')).toBeNull()
  })

  it('opens without selecting the row, and closes back onto its button', () => {
    const { onSelect, trigger } = render()
    fireEvent.click(trigger)
    expect(onSelect).not.toHaveBeenCalled()

    fireEvent.keyDown(document.activeElement!, { key: 'Escape' })
    expect(screen.queryByRole('menu')).toBeNull()
    expect(trigger).toHaveFocus()
  })

  const SECOND = {
    ...WALLET,
    address: '0x2222222222222222222222222222222222222222',
    label: 'Second',
    primary: false,
  }

  function rail(wallets: (typeof WALLET)[], onSetPrimary = vi.fn()) {
    return (
      <WalletRail
        wallets={wallets}
        totals={new Map()}
        selected="all"
        onSelect={vi.fn()}
        onAction={vi.fn()}
        onSetPrimary={onSetPrimary}
        limits={undefined}
        limitsWallet={null}
        chains={[]}
        manualUnlock={false}
      />
    )
  }

  function openMenu(row: HTMLElement) {
    fireEvent.click(within(row).getByRole('button', { name: 'Wallet actions' }))
    return screen.getByRole('menuitem', { name: 'Active' })
  }

  it('lists the active wallet first and marks it with the enable mark, not a star', () => {
    renderDesk(rail([SECOND, WALLET]))
    const rows = screen.getAllByTestId('wallet-row')
    expect(rows.map((r) => r.textContent)).toEqual([
      expect.stringContaining(WALLET.label),
      expect.stringContaining(SECOND.label),
    ])
    expect(within(rows[0]!).getByLabelText('Active')).toBeInTheDocument()
    expect(within(rows[1]!).queryByLabelText('Active')).toBeNull()
    expect(document.querySelector('.lucide-star')).toBeNull()
  })

  it('offers Active on an inactive wallet and shows it as already on for the active one', () => {
    const onSetPrimary = vi.fn()
    renderDesk(rail([WALLET, SECOND], onSetPrimary))
    const [active, inactive] = screen.getAllByTestId('wallet-row')

    expect(openMenu(active!)).toBeDisabled()
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' })

    fireEvent.click(openMenu(inactive!))
    expect(onSetPrimary).toHaveBeenCalledTimes(1)
    expect(onSetPrimary).toHaveBeenCalledWith(SECOND)
  })

  it('scrolls back to the top when another wallet becomes active', () => {
    const { rerender } = renderDesk(rail([WALLET, SECOND]))
    const list = document.querySelector('.trd-rail__list')!
    const aside = document.querySelector('.trd-rail')!
    list.scrollTop = 300
    aside.scrollLeft = 200

    // A refetch that changes nothing must not move the list under the pointer.
    rerender(rail([{ ...WALLET }, { ...SECOND }]))
    expect(list.scrollTop).toBe(300)

    rerender(
      rail([
        { ...WALLET, primary: false },
        { ...SECOND, primary: true },
      ]),
    )
    expect(list.scrollTop).toBe(0)
    expect(aside.scrollLeft).toBe(0)
  })
})
