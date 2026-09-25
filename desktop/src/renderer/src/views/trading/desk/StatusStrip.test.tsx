import { fireEvent, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { renderDesk, WALLET } from '../test-utils'
import { ComposerSeats } from './ComposerSeats'
import { StatusStrip } from './StatusStrip'

const pill = { mode: 'trading' as const, onSwitchMode: vi.fn() }

describe('StatusStrip', () => {
  it('says nothing while idle, then carries one status word and a pin', () => {
    const onOpenApprovals = vi.fn()
    const { rerender } = renderDesk(
      <StatusStrip
        {...pill}
        missions={[]}
        running={new Set()}
        sessionPending={0}
        globalPending={null}
        streaming={false}
        deskMode={false}
        onToggleDesk={vi.fn()}
        onOpenApprovals={onOpenApprovals}
      />,
    )
    expect(screen.queryByTestId('status-word')).toBeNull()
    expect(screen.getByTestId('status-strip')).not.toHaveTextContent('Idle')
    expect(screen.queryByTestId('strip-pin')).toBeNull()
    rerender(
      <StatusStrip
        {...pill}
        missions={[
          {
            id: 'j1',
            name: 'DCA ETH',
            enabled: true,
            next_run: new Date(Date.now() + 60_000).toISOString(),
          },
        ]}
        running={new Set(['j1'])}
        sessionPending={2}
        globalPending={3}
        streaming={true}
        deskMode={false}
        onToggleDesk={vi.fn()}
        onOpenApprovals={onOpenApprovals}
      />,
    )
    expect(screen.getByTestId('status-word')).toHaveTextContent('Awaiting')
    expect(screen.getByTestId('status-strip')).toHaveTextContent('DCA ETH')
    expect(screen.getByTestId('status-strip')).toHaveTextContent('Running')
    fireEvent.click(screen.getByTestId('strip-pin'))
    expect(onOpenApprovals).toHaveBeenCalled()
    expect(screen.getByTestId('strip-pin')).toHaveTextContent('3')
  })

  it('offers the desk toggle in both directions', () => {
    const onToggleDesk = vi.fn()
    renderDesk(
      <StatusStrip
        {...pill}
        missions={[]}
        running={new Set()}
        sessionPending={0}
        globalPending={0}
        streaming={false}
        deskMode={true}
        onToggleDesk={onToggleDesk}
        onOpenApprovals={vi.fn()}
      />,
    )
    const toggle = screen.getByTestId('desk-toggle')
    expect(toggle).toHaveTextContent('Chat')
    fireEvent.click(toggle)
    expect(onToggleDesk).toHaveBeenCalled()
  })

  it('is only the mode pill in Chat mode, and the pill switches modes', () => {
    const onSwitchMode = vi.fn()
    renderDesk(<StatusStrip mode="chat" onSwitchMode={onSwitchMode} />)
    expect(screen.queryByTestId('status-word')).toBeNull()
    expect(screen.queryByTestId('desk-toggle')).toBeNull()
    expect(screen.getByTestId('mode-chat')).toHaveAttribute('aria-selected', 'true')
    fireEvent.click(screen.getByTestId('mode-chat'))
    expect(onSwitchMode).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('mode-trading'))
    expect(onSwitchMode).toHaveBeenCalledWith('trading')
    fireEvent.keyDown(screen.getByTestId('mode-pill'), { key: 'ArrowRight' })
    expect(onSwitchMode).toHaveBeenCalledTimes(2)
  })

  it("keeps a slot right of the pill for the chat's actions in Chat mode, none at the desk", () => {
    const sessionSlot = vi.fn()
    const { rerender } = renderDesk(
      <StatusStrip mode="chat" onSwitchMode={vi.fn()} sessionSlot={sessionSlot} />,
    )
    const slot = screen.getByTestId('strip-session')
    expect(slot.parentElement).toHaveClass('trd-strip__right')
    // After the tab list, so Tab reaches the pill before the chat's actions.
    expect(screen.getByTestId('mode-pill').compareDocumentPosition(slot)).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    )
    expect(sessionSlot).toHaveBeenLastCalledWith(slot)
    // At the desk the right of the strip is the desk's: no slot, so no gap.
    rerender(
      <StatusStrip
        {...pill}
        missions={[]}
        running={new Set()}
        sessionPending={0}
        globalPending={null}
        streaming={false}
        deskMode={false}
        onToggleDesk={vi.fn()}
        onOpenApprovals={vi.fn()}
        sessionSlot={sessionSlot}
      />,
    )
    expect(screen.queryByTestId('strip-session')).toBeNull()
    expect(sessionSlot.mock.lastCall?.[0]).toBeNull()
  })
})

describe('ComposerSeats', () => {
  it('names the authority it has and prefills a quick action', () => {
    const onQuick = vi.fn()
    const onOpenSettings = vi.fn()
    renderDesk(
      <ComposerSeats
        limits={{
          dailyCapUsd: 1000,
          spentTodayUsd: 12,
          thresholdUsd: 100,
          approvalTtlSeconds: 900,
        }}
        provider="uniswap"
        wallet={WALLET}
        typing={false}
        onOpenSettings={onOpenSettings}
        onOpenWallets={vi.fn()}
        onQuick={onQuick}
      />,
    )
    expect(screen.getByTestId('permission-seat')).toHaveTextContent(
      'Asks above $100.00 · $1,000.00/day',
    )
    expect(screen.getByTestId('provider-seat')).toHaveTextContent('Uniswap')
    expect(screen.getByTestId('wallet-seat')).toHaveTextContent('Main')
    fireEvent.click(screen.getByTestId('permission-seat'))
    expect(onOpenSettings).toHaveBeenCalled()
    // Swap is the only quick action left: DCA, dip and rebalance are
    // missions now, and missions start from the catalogue.
    expect(screen.queryByTestId('quick-dca')).toBeNull()
    expect(screen.queryByTestId('quick-dip')).toBeNull()
    expect(screen.queryByTestId('quick-rebalance')).toBeNull()
    fireEvent.click(screen.getByTestId('quick-swap'))
    expect(onQuick).toHaveBeenCalledWith('swap')
  })

  it('takes the chips out of the tab order while typing', () => {
    renderDesk(
      <ComposerSeats
        limits={null}
        provider="aggregator"
        wallet={null}
        typing
        onOpenSettings={vi.fn()}
        onOpenWallets={vi.fn()}
        onQuick={vi.fn()}
      />,
    )
    expect(screen.getByTestId('composer-seats')).toHaveAttribute('data-typing')
    expect(screen.getByTestId('quick-swap')).toHaveAttribute('tabindex', '-1')
    expect(screen.getByTestId('provider-seat')).toHaveTextContent('AgentOS Aggregator')
  })
})

describe('ProviderSeat', () => {
  it('switches the swap provider from the desk and explains each choice', () => {
    const onSwitchProvider = vi.fn()
    renderDesk(
      <ComposerSeats
        limits={null}
        provider="uniswap"
        providers={[
          {
            id: 'uniswap',
            label: 'Uniswap',
            needsKey: true,
            keyConfigured: false,
            healthy: null,
          },
          {
            id: 'aggregator',
            label: 'AgentOS Aggregator',
            needsKey: false,
            keyConfigured: true,
            healthy: null,
          },
        ]}
        wallet={WALLET}
        typing={false}
        onOpenSettings={vi.fn()}
        onOpenWallets={vi.fn()}
        onSwitchProvider={onSwitchProvider}
        onQuick={vi.fn()}
      />,
    )
    fireEvent.click(screen.getByTestId('provider-seat'))
    const aggregator = screen.getByRole('menuitemradio', { name: /AgentOS Aggregator/ })
    expect(screen.getByRole('menuitemradio', { name: /^Uniswap/ })).toHaveTextContent(
      'needs an API key',
    )
    fireEvent.click(aggregator)
    expect(onSwitchProvider).toHaveBeenCalledWith('aggregator')
    // Picking the active one is a no-op.
    fireEvent.click(screen.getByTestId('provider-seat'))
    fireEvent.click(screen.getByRole('menuitemradio', { name: /^Uniswap/ }))
    expect(onSwitchProvider).toHaveBeenCalledTimes(1)
  })
})
