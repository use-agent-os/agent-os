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
})
